"""Blender 侧的 Operator 集合（UI 按钮与 modal 入口）。

这个模块把其它纯逻辑模块串起来：
- `game.py`：棋盘与规则
- `runtime.py`：把状态写到 points mesh（再由 GN 渲染）
- `looks.py/geo_nodes.py`：外观与 GN
- `ai.py`：Auto Play
- `session.py`：会话与录制
- `replay.py`：录制烘焙为时间线回放
- `audio.py`：音效/背景音乐

注意：
- 这里尽量只做“编排”，避免把复杂逻辑塞进 operator。
- modal 中的输入事件会触发录制（若 recording_active=true），并触发音效。
"""

from __future__ import annotations

import time

import bpy

from ..core import ai
from ..utils import audio
from ..utils import assets
from ..utils import geo_nodes
from ..utils import looks
from ..data import properties
from . import replay
from ..utils import runtime
from ..data import session
from ..data.constants import LOOKS_COLLECTION_NAME
from ..core.tetrominoes import TETROMINO_KEYS


_KEYBIND_PREFIXES = {
    "kb_move_left",
    "kb_move_right",
    "kb_soft_drop",
    "kb_rotate_cw",
    "kb_rotate_cw_alt",
    "kb_rotate_ccw",
    "kb_hold",
    "kb_hard_drop",
    "kb_pause",
}


def _event_matches_keybind(
    event: bpy.types.Event,
    *,
    key: str,
    shift: bool,
    ctrl: bool,
    alt: bool,
    oskey: bool,
) -> bool:
    if not key:
        return False

    if str(getattr(event, "type", "")) != str(key):
        return False

    if bool(getattr(event, "shift", False)) != bool(shift):
        return False
    if bool(getattr(event, "ctrl", False)) != bool(ctrl):
        return False
    if bool(getattr(event, "alt", False)) != bool(alt):
        return False
    if bool(getattr(event, "oskey", False)) != bool(oskey):
        return False

    return True


def _event_matches_kb_prefix(event: bpy.types.Event, *, settings, prefix: str) -> bool:
    key = str(getattr(settings, f"{prefix}_key", "") or "")
    return _event_matches_keybind(
        event,
        key=key,
        shift=bool(getattr(settings, f"{prefix}_shift", False)),
        ctrl=bool(getattr(settings, f"{prefix}_ctrl", False)),
        alt=bool(getattr(settings, f"{prefix}_alt", False)),
        oskey=bool(getattr(settings, f"{prefix}_oskey", False)),
    )


def _event_type_matches_kb_prefix(event: bpy.types.Event, *, settings, prefix: str) -> bool:
    key = str(getattr(settings, f"{prefix}_key", "") or "")
    return bool(key) and str(getattr(event, "type", "")) == key


def _seed_or_none(seed_value: int) -> int | None:
    """将 UI 里的 seed 值规范化。

    约定：seed==0 表示“不固定随机种子”（由逻辑层自行随机）。

    Args:
        seed_value: UI 输入的 seed 值。

    Returns:
        seed==0 时返回 None，否则返回 int seed。
    """

    return None if int(seed_value) == 0 else int(seed_value)


def _effective_drop_interval(*, settings, level: int) -> float:
    """根据设置与等级计算实际重力下落间隔（秒）。"""

    base = float(getattr(settings, "drop_interval", session.DEFAULT_DROP_INTERVAL))

    if not bool(getattr(settings, "use_level_speed", False)):
        return base

    min_interval = float(getattr(settings, "min_drop_interval", 0.05))
    min_interval = max(0.001, min_interval)

    lvl = max(1, int(level))

    curve = str(getattr(settings, "speed_curve", "MULTIPLIER") or "MULTIPLIER").upper()
    if curve == "GUIDELINE":
        # 常见 Guideline 重力曲线：pow(0.8 - (level-1)*0.007, level-1)
        factor_base = 0.8 - (float(lvl - 1) * 0.007)
        if factor_base <= 0.0:
            return min_interval
        return max(min_interval, base * (factor_base ** max(0, lvl - 1)))

    multiplier = float(getattr(settings, "level_speed_multiplier", 0.85))
    multiplier = max(0.01, min(multiplier, 0.99))

    return max(min_interval, base * (multiplier ** max(0, lvl - 1)))


def _piece_colors_from_settings(settings) -> dict[str, tuple[float, float, float, float]]:
    """从设置中读取每种方块的 RGBA 颜色。

    Args:
        settings: `TetrisSettings`（或具备同名字段的对象）。

    Returns:
        piece_key -> RGBA 的映射。
    """

    colors: dict[str, tuple[float, float, float, float]] = {}
    for key in TETROMINO_KEYS:
        value = getattr(settings, f"color_{key}")
        colors[str(key)] = (float(value[0]), float(value[1]), float(value[2]), float(value[3]))
    return colors


def _border_color_from_settings(settings) -> tuple[float, float, float, float]:
    """从设置中读取边框颜色（RGBA）。

    Args:
        settings: `TetrisSettings`（或具备同名字段的对象）。

    Returns:
        边框 RGBA。
    """

    value = settings.border_color
    return (float(value[0]), float(value[1]), float(value[2]), float(value[3]))


def _ai_weights_from_settings(settings) -> dict[str, float]:
    preset = str(getattr(settings, "ai_strategy", "STABLE") or "STABLE").upper()

    if preset == "CUSTOM":
        return {
            "aggregate_height": float(getattr(settings, "ai_w_aggregate_height", ai.WEIGHTS["aggregate_height"])),
            "lines": float(getattr(settings, "ai_w_lines", ai.WEIGHTS["lines"])),
            "holes": float(getattr(settings, "ai_w_holes", ai.WEIGHTS["holes"])),
            "bumpiness": float(getattr(settings, "ai_w_bumpiness", ai.WEIGHTS["bumpiness"])),
        }

    return dict(ai.WEIGHT_PRESETS.get(preset, ai.WEIGHTS))


def _ensure_visuals(context: bpy.types.Context, *, settings) -> bpy.types.Collection:
    """确保 Assets / Looks / GN 都已创建并可用。

    Args:
        context: Blender 上下文。
        settings: `TetrisSettings`（或具备同名字段的对象）。

    Returns:
        Looks collection（用于 GN pick instance）。

    Side effects:
        - 可能创建/更新 `TetrisAssets`、`TetrisLooks` 与相关 GN node group
    """

    assets.ensure_tetris_assets(cell_size=float(settings.cell_size))

    looks_collection = looks.ensure_looks(settings=settings)

    geo_nodes.setup_geometry_nodes_for_assets(
        looks_collection=looks_collection,
        block_scale=tuple(float(v) for v in settings.block_scale),
    )

    return looks_collection


def _get_or_create_session_from_settings(
    context: bpy.types.Context, *, force_new: bool = False
) -> session.TetrisSession:
    """根据当前 Scene 设置获取（或重建）`TetrisSession`。

    当棋盘尺寸变化、或显式要求 force_new 时，会创建新会话；否则复用现有会话并
    更新参数。

    Args:
        context: Blender 上下文。
        force_new: True 时强制创建新会话。

    Returns:
        可用于驱动游戏的 `TetrisSession`。
    """

    settings = properties.get_settings(context)
    seed = _seed_or_none(settings.seed)

    sess = session.get_session()
    if (
        force_new
        or sess is None
        or sess.game.width != int(settings.board_width)
        or sess.game.height != int(settings.board_height)
    ):
        sess = session.create_session(
            width=int(settings.board_width),
            height=int(settings.board_height),
            cell_size=float(settings.cell_size),
            seed=seed,
            drop_interval=float(settings.drop_interval),
            next_queue_size=int(getattr(settings, "next_queue_size", 5)),
            lines_per_level=int(getattr(settings, "lines_per_level", 10)),
            rotation_system=str(settings.rotation_system),
        )
    else:
        sess.cell_size = float(settings.cell_size)
        sess.game.set_next_queue_size(int(getattr(settings, "next_queue_size", 5)))
        sess.game.set_lines_per_level(int(getattr(settings, "lines_per_level", 10)))
        sess.game.rotation_system = str(settings.rotation_system).upper()

    sess.game.scoring_mode = str(getattr(settings, "scoring_mode", "GUIDELINE") or "GUIDELINE").upper()
    sess.game.combo_scoring_mode = str(getattr(settings, "combo_scoring_mode", "MULTIPLY") or "MULTIPLY").upper()
    sess.game.combo_multiplier_step = float(getattr(settings, "combo_multiplier_step", 0.25) or 0.25)

    sess.drop_interval = _effective_drop_interval(settings=settings, level=int(getattr(sess.game, "level", 1) or 1))

    # 关键：新会话创建后可能还没生成第一块，这里保证能立即开始/渲染。
    if sess.game.current is None and not sess.game.game_over:
        sess.game.spawn_piece()

    return sess


def _sync_scene_state(context: bpy.types.Context, sess: session.TetrisSession) -> None:
    """将会话状态写回到 Scene 的状态属性（用于 UI 显示/按钮可用性）。

    Args:
        context: Blender 上下文。
        sess: 当前会话。
    """

    state = properties.get_state(context)
    state.running = bool(sess.running)
    state.game_over = bool(sess.game.game_over)


def _sync_runtime_objects(context: bpy.types.Context, sess: session.TetrisSession) -> None:
    """确保运行时对象存在，并把逻辑层局面同步到 points mesh。

    Args:
        context: Blender 上下文。
        sess: 当前会话。

    Side effects:
        - 可能创建/更新运行时 points mesh 对象与 GN modifier
        - 会将 `sess.game` 的最新局面写入 mesh 属性
    """
    settings = properties.get_settings(context)

    looks_collection = bpy.data.collections.get(LOOKS_COLLECTION_NAME) or _ensure_visuals(context, settings=settings)

    block_scale = tuple(float(v) for v in settings.block_scale)

    board_obj, current_obj, border_obj, ghost_obj = runtime.ensure_runtime_objects(
        cell_size=sess.cell_size,
        looks_collection=looks_collection,
        block_scale=block_scale,
    )

    origin_corner = runtime.board_origin_corner(
        width=int(getattr(sess.game, "width", 0) or 0),
        height=int(getattr(sess.game, "height", 0) or 0),
        cell_size=sess.cell_size,
    )

    piece_colors = _piece_colors_from_settings(settings)
    border_color = _border_color_from_settings(settings)

    runtime.sync_from_game(
        game=sess.game,
        board_obj=board_obj,
        current_obj=current_obj,
        border_obj=border_obj,
        ghost_obj=ghost_obj,
        cell_size=sess.cell_size,
        origin_corner=origin_corner,
        piece_colors=piece_colors,
        border_color=border_color,
        show_ghost=bool(getattr(settings, "show_ghost", True)),
    )

    next_obj, hold_obj, stats_obj = runtime.ensure_hud_objects(
        looks_collection=looks_collection,
        block_scale=block_scale,
    )

    runtime.sync_hud_from_game(
        game=sess.game,
        next_obj=next_obj,
        hold_obj=hold_obj,
        stats_obj=stats_obj,
        cell_size=sess.cell_size,
        origin_corner=origin_corner,
        piece_colors=piece_colors,
        next_count=int(getattr(settings, "next_queue_size", 5)),
    )


class BLTETRIS_OT_setup_assets(bpy.types.Operator):
    """创建/修复 `TetrisAssets` 集合中的参考方块点云。"""

    bl_idname = "bltetris.setup_assets"
    bl_label = "Setup Assets"
    bl_options = {"REGISTER", "UNDO"}

    cell_size: bpy.props.FloatProperty(
        name="Cell Size",
        default=1.0,
        min=0.001,
    )

    def execute(self, context: bpy.types.Context):
        """生成 Assets，并把 cell_size 写回设置。

        Args:
            context: Blender 上下文。

        Returns:
            Operator 状态集合（成功时为 `{"FINISHED"}`）。
        """

        settings = properties.get_settings(context)
        settings.cell_size = float(self.cell_size)

        assets.ensure_tetris_assets(cell_size=float(self.cell_size))
        return {"FINISHED"}


class BLTETRIS_OT_setup_geometry_nodes(bpy.types.Operator):
    """创建/修复本项目需要的 Geometry Nodes 节点组。"""

    bl_idname = "bltetris.setup_geometry_nodes"
    bl_label = "Setup Geometry Nodes"
    bl_options = {"REGISTER", "UNDO"}

    cell_size: bpy.props.FloatProperty(
        name="Cell Size",
        default=1.0,
        min=0.001,
    )

    def execute(self, context: bpy.types.Context):
        """生成/修复 Looks + GN，并把 cell_size 写回设置。

        Args:
            context: Blender 上下文。

        Returns:
            Operator 状态集合（成功时为 `{"FINISHED"}`）。
        """

        settings = properties.get_settings(context)
        settings.cell_size = float(self.cell_size)

        _ensure_visuals(context, settings=settings)
        return {"FINISHED"}


class BLTETRIS_OT_apply_looks(bpy.types.Operator):
    """根据当前设置重建 Looks/GN，并刷新运行时显示。"""

    bl_idname = "bltetris.apply_looks"
    bl_label = "Apply Looks"

    def execute(self, context: bpy.types.Context):
        """重建视觉资源，并同步当前运行时状态到场景。

        Args:
            context: Blender 上下文。

        Returns:
            Operator 状态集合（成功时为 `{"FINISHED"}`）。
        """

        settings = properties.get_settings(context)
        _ensure_visuals(context, settings=settings)

        sess = session.get_session()
        if sess is not None:
            _sync_runtime_objects(context, sess)
            _sync_scene_state(context, sess)

        return {"FINISHED"}


class BLTETRIS_OT_record_start(bpy.types.Operator):
    """开始录制：之后每次状态变化会写入 `RecordedState`。"""

    bl_idname = "bltetris.record_start"
    bl_label = "Record"

    def execute(self, context: bpy.types.Context):
        """开启录制标记，并同步 UI 状态。

        Args:
            context: Blender 上下文。

        Returns:
            Operator 状态集合（成功时为 `{"FINISHED"}`）。
        """

        sess = _get_or_create_session_from_settings(context)
        session.start_recording(sess)
        _sync_scene_state(context, sess)
        return {"FINISHED"}


class BLTETRIS_OT_record_stop(bpy.types.Operator):
    """停止录制（保留已录制的状态序列）。"""

    bl_idname = "bltetris.record_stop"
    bl_label = "Stop"

    def execute(self, context: bpy.types.Context):
        """关闭录制标记，并同步 UI 状态。

        Args:
            context: Blender 上下文。

        Returns:
            Operator 状态集合（无会话时为 `{"CANCELLED"}`）。
        """

        sess = session.get_session()
        if sess is None:
            return {"CANCELLED"}
        session.stop_recording(sess)
        _sync_scene_state(context, sess)
        return {"FINISHED"}


class BLTETRIS_OT_record_clear(bpy.types.Operator):
    """清空已录制的状态序列。"""

    bl_idname = "bltetris.record_clear"
    bl_label = "Clear"

    def execute(self, context: bpy.types.Context):
        """删除当前会话中的录制缓存，并同步 UI 状态。

        Args:
            context: Blender 上下文。

        Returns:
            Operator 状态集合（无会话时为 `{"CANCELLED"}`）。
        """

        sess = session.get_session()
        if sess is None:
            return {"CANCELLED"}
        session.clear_recording(sess)
        _sync_scene_state(context, sess)
        return {"FINISHED"}


class BLTETRIS_OT_export_replay_json(bpy.types.Operator):
    """导出当前录制序列为 JSON 文件。"""

    bl_idname = "bltetris.export_replay_json"
    bl_label = "Export JSON"

    filepath: bpy.props.StringProperty(
        name="File Path",
        default="",
        subtype="FILE_PATH",
    )

    filter_glob: bpy.props.StringProperty(
        default="*.json",
        options={"HIDDEN"},
    )

    def execute(self, context: bpy.types.Context):
        sess = session.get_session()
        if sess is None or not sess.recording:
            self.report({"WARNING"}, "No recorded states")
            return {"CANCELLED"}

        filepath = str(getattr(self, "filepath", "") or "")
        filepath = bpy.path.ensure_ext(bpy.path.abspath(filepath), ".json")
        if not filepath:
            self.report({"WARNING"}, "Invalid filepath")
            return {"CANCELLED"}

        settings = properties.get_settings(context)
        meta: dict[str, object] = {
            "board_width": int(getattr(settings, "board_width", 0) or 0),
            "board_height": int(getattr(settings, "board_height", 0) or 0),
            "cell_size": float(getattr(settings, "cell_size", 1.0) or 1.0),
            "rotation_system": str(getattr(settings, "rotation_system", "SRS")),
            "exported_at": float(time.time()),
        }

        seed_value = int(getattr(settings, "seed", 0) or 0)
        if seed_value != 0:
            meta["seed"] = seed_value

        try:
            session.save_replay_json(filepath, recording=list(sess.recording), meta=meta)
        except Exception as exc:
            self.report({"ERROR"}, f"Export failed: {exc}")
            return {"CANCELLED"}

        self.report({"INFO"}, f"Exported {len(sess.recording)} steps")
        return {"FINISHED"}

    def invoke(self, context: bpy.types.Context, event: bpy.types.Event):
        if not str(getattr(self, "filepath", "") or ""):
            self.filepath = bpy.path.ensure_ext("//bltetris_replay", ".json")

        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}


class BLTETRIS_OT_import_replay_json(bpy.types.Operator):
    """从 JSON 文件导入录制序列到当前会话。"""

    bl_idname = "bltetris.import_replay_json"
    bl_label = "Import JSON"

    filepath: bpy.props.StringProperty(
        name="File Path",
        default="",
        subtype="FILE_PATH",
    )

    filter_glob: bpy.props.StringProperty(
        default="*.json",
        options={"HIDDEN"},
    )

    replace_existing: bpy.props.BoolProperty(
        name="Replace Existing Recording",
        default=True,
    )

    def execute(self, context: bpy.types.Context):
        filepath = str(getattr(self, "filepath", "") or "")
        filepath = bpy.path.ensure_ext(bpy.path.abspath(filepath), ".json")
        if not filepath:
            self.report({"WARNING"}, "Invalid filepath")
            return {"CANCELLED"}

        try:
            imported, meta, version = session.load_replay_json(filepath)
        except Exception as exc:
            self.report({"ERROR"}, f"Import failed: {exc}")
            return {"CANCELLED"}

        if not imported:
            self.report({"WARNING"}, "Replay contains no steps")
            return {"CANCELLED"}

        sess = _get_or_create_session_from_settings(context)
        session.stop_recording(sess)

        if bool(getattr(self, "replace_existing", True)):
            sess.recording.clear()

        sess.recording.extend(list(imported))
        sess.last_record_signature = None

        if int(version) > int(getattr(session, "REPLAY_JSON_VERSION", 1)):
            self.report({"WARNING"}, f"Replay version {version} is newer than supported")

        settings = properties.get_settings(context)
        mismatched = []

        bw = meta.get("board_width") if isinstance(meta, dict) else None
        bh = meta.get("board_height") if isinstance(meta, dict) else None
        cs = meta.get("cell_size") if isinstance(meta, dict) else None
        rs = meta.get("rotation_system") if isinstance(meta, dict) else None

        try:
            if bw is not None and int(bw) != int(getattr(settings, "board_width", 0) or 0):
                mismatched.append("board_width")
        except Exception:
            pass

        try:
            if bh is not None and int(bh) != int(getattr(settings, "board_height", 0) or 0):
                mismatched.append("board_height")
        except Exception:
            pass

        try:
            if cs is not None and float(cs) != float(getattr(settings, "cell_size", 1.0) or 1.0):
                mismatched.append("cell_size")
        except Exception:
            pass

        try:
            if rs is not None and str(rs).upper() != str(getattr(settings, "rotation_system", "SRS")).upper():
                mismatched.append("rotation_system")
        except Exception:
            pass

        if mismatched:
            self.report({"WARNING"}, "Imported replay meta differs from current settings")

        _sync_scene_state(context, sess)

        self.report({"INFO"}, f"Imported {len(imported)} steps")
        return {"FINISHED"}

    def invoke(self, context: bpy.types.Context, event: bpy.types.Event):
        if not str(getattr(self, "filepath", "") or ""):
            self.filepath = bpy.path.ensure_ext("//bltetris_replay", ".json")

        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}


class BLTETRIS_OT_bake_replay(bpy.types.Operator):
    """将录制序列烘焙为可 scrub 的时间线回放。"""

    bl_idname = "bltetris.bake_replay"
    bl_label = "Bake Replay"

    def execute(self, context: bpy.types.Context):
        """把 `session.recording` 变为 `TetrisReplay` collection。

        Args:
            context: Blender 上下文。

        Returns:
            Operator 状态集合（无录制数据时为 `{"CANCELLED"}`）。
        """

        sess = session.get_session()
        if sess is None or not sess.recording:
            self.report({"WARNING"}, "No recorded states")
            return {"CANCELLED"}

        settings = properties.get_settings(context)
        _ensure_visuals(context, settings=settings)

        replay.bake_replay(recorded=list(sess.recording), settings=settings)
        return {"FINISHED"}


class BLTETRIS_OT_restart(bpy.types.Operator):
    """重置游戏局面并同步运行时显示。"""

    bl_idname = "bltetris.restart"
    bl_label = "Restart"

    def execute(self, context: bpy.types.Context):
        """创建新会话并清理暂停标记，然后刷新点云对象。

        Args:
            context: Blender 上下文。

        Returns:
            Operator 状态集合（成功时为 `{"FINISHED"}`）。
        """

        settings = properties.get_settings(context)
        _ensure_visuals(context, settings=settings)

        seed = _seed_or_none(settings.seed)
        sess = _get_or_create_session_from_settings(context, force_new=True)
        session.restart_session(
            seed=seed,
            next_queue_size=int(getattr(settings, "next_queue_size", 5)),
            lines_per_level=int(getattr(settings, "lines_per_level", 10)),
            rotation_system=str(settings.rotation_system),
        )

        sess.running = False
        session.clear_pause_request()

        _sync_runtime_objects(context, sess)
        _sync_scene_state(context, sess)

        return {"FINISHED"}


class BLTETRIS_OT_step(bpy.types.Operator):
    """非 modal 模式下前进若干步（用于调试/验收）。"""

    bl_idname = "bltetris.step"
    bl_label = "Step"

    steps: bpy.props.IntProperty(
        name="Steps",
        default=1,
        min=1,
        max=1000,
    )

    def execute(self, context: bpy.types.Context):
        """重复调用 `tick_down()`，并在每步后做录制快照（若开启录制）。

        Args:
            context: Blender 上下文。

        Returns:
            Operator 状态集合（成功时为 `{"FINISHED"}`）。
        """

        settings = properties.get_settings(context)
        _ensure_visuals(context, settings=settings)

        sess = _get_or_create_session_from_settings(context)

        for _ in range(int(self.steps)):
            if sess.game.game_over:
                break
            if sess.game.current is None:
                sess.game.spawn_piece()
                session.record_snapshot(sess, reason="step_spawn")
            else:
                sess.game.tick_down()
                session.record_snapshot(sess, reason="step_tick")

        _sync_runtime_objects(context, sess)
        _sync_scene_state(context, sess)

        return {"FINISHED"}


class BLTETRIS_OT_pause(bpy.types.Operator):
    """请求暂停：modal 会在下一个事件循环退出。"""

    bl_idname = "bltetris.pause"
    bl_label = "Pause"

    def execute(self, context: bpy.types.Context):
        """设置全局 pause_request 标记。

        Args:
            context: Blender 上下文。

        Returns:
            Operator 状态集合（成功时为 `{"FINISHED"}`）。
        """

        session.request_pause()

        settings = properties.get_settings(context)
        audio.set_bgm_paused(paused=True, settings=settings)

        return {"FINISHED"}


class BLTETRIS_OT_capture_keybind(bpy.types.Operator):
    """点击按钮后捕获下一次按键，用于自定义快捷键。"""

    bl_idname = "bltetris.capture_keybind"
    bl_label = "Capture Keybind"

    prefix: bpy.props.StringProperty(
        name="Prefix",
        default="",
    )

    def invoke(self, context: bpy.types.Context, event: bpy.types.Event):
        prefix = str(getattr(self, "prefix", "") or "")
        if prefix not in _KEYBIND_PREFIXES:
            self.report({"WARNING"}, f"Invalid keybind prefix: {prefix}")
            return {"CANCELLED"}

        if context.area is not None:
            context.area.header_text_set("Press a key to bind (RMB to cancel)")

        context.window_manager.modal_handler_add(self)
        return {"RUNNING_MODAL"}

    def modal(self, context: bpy.types.Context, event: bpy.types.Event):
        if event.type == "RIGHTMOUSE" and event.value == "PRESS":
            if context.area is not None:
                context.area.header_text_set(None)
            return {"CANCELLED"}

        # 忽略鼠标/计时器噪声，只捕获键盘 PRESS。
        if event.type in {"LEFTMOUSE", "MIDDLEMOUSE", "MOUSEMOVE", "INBETWEEN_MOUSEMOVE", "TIMER"}:
            return {"RUNNING_MODAL"}

        if event.value != "PRESS":
            return {"RUNNING_MODAL"}

        prefix = str(getattr(self, "prefix", "") or "")
        if prefix not in _KEYBIND_PREFIXES:
            if context.area is not None:
                context.area.header_text_set(None)
            return {"CANCELLED"}

        settings = properties.get_settings(context)

        setattr(settings, f"{prefix}_key", str(event.type))
        setattr(settings, f"{prefix}_shift", bool(getattr(event, "shift", False)))
        setattr(settings, f"{prefix}_ctrl", bool(getattr(event, "ctrl", False)))
        setattr(settings, f"{prefix}_alt", bool(getattr(event, "alt", False)))
        setattr(settings, f"{prefix}_oskey", bool(getattr(event, "oskey", False)))

        if context.area is not None:
            context.area.header_text_set(None)
            context.area.tag_redraw()

        self.report({"INFO"}, f"Bound {prefix} to {event.type}")
        return {"FINISHED"}


class BLTETRIS_OT_clear_keybind(bpy.types.Operator):
    """清空指定动作的按键绑定。"""

    bl_idname = "bltetris.clear_keybind"
    bl_label = "Clear Keybind"

    prefix: bpy.props.StringProperty(
        name="Prefix",
        default="",
    )

    def execute(self, context: bpy.types.Context):
        prefix = str(getattr(self, "prefix", "") or "")
        if prefix not in _KEYBIND_PREFIXES:
            self.report({"WARNING"}, f"Invalid keybind prefix: {prefix}")
            return {"CANCELLED"}

        settings = properties.get_settings(context)

        setattr(settings, f"{prefix}_key", "")
        setattr(settings, f"{prefix}_shift", False)
        setattr(settings, f"{prefix}_ctrl", False)
        setattr(settings, f"{prefix}_alt", False)
        setattr(settings, f"{prefix}_oskey", False)

        return {"FINISHED"}


class BLTETRIS_OT_reset_keybinds(bpy.types.Operator):
    """将快捷键恢复为默认值。"""

    bl_idname = "bltetris.reset_keybinds"
    bl_label = "Reset Keybinds"

    def execute(self, context: bpy.types.Context):
        settings = properties.get_settings(context)

        settings.kb_move_left_key = "LEFT_ARROW"
        settings.kb_move_left_shift = False
        settings.kb_move_left_ctrl = False
        settings.kb_move_left_alt = False
        settings.kb_move_left_oskey = False

        settings.kb_move_right_key = "RIGHT_ARROW"
        settings.kb_move_right_shift = False
        settings.kb_move_right_ctrl = False
        settings.kb_move_right_alt = False
        settings.kb_move_right_oskey = False

        settings.kb_soft_drop_key = "DOWN_ARROW"
        settings.kb_soft_drop_shift = False
        settings.kb_soft_drop_ctrl = False
        settings.kb_soft_drop_alt = False
        settings.kb_soft_drop_oskey = False

        settings.kb_rotate_cw_key = "UP_ARROW"
        settings.kb_rotate_cw_shift = False
        settings.kb_rotate_cw_ctrl = False
        settings.kb_rotate_cw_alt = False
        settings.kb_rotate_cw_oskey = False

        settings.kb_rotate_cw_alt_key = "X"
        settings.kb_rotate_cw_alt_shift = False
        settings.kb_rotate_cw_alt_ctrl = False
        settings.kb_rotate_cw_alt_alt = False
        settings.kb_rotate_cw_alt_oskey = False

        settings.kb_rotate_ccw_key = "Z"
        settings.kb_rotate_ccw_shift = False
        settings.kb_rotate_ccw_ctrl = False
        settings.kb_rotate_ccw_alt = False
        settings.kb_rotate_ccw_oskey = False

        settings.kb_hold_key = "C"
        settings.kb_hold_shift = False
        settings.kb_hold_ctrl = False
        settings.kb_hold_alt = False
        settings.kb_hold_oskey = False

        settings.kb_hard_drop_key = "SPACE"
        settings.kb_hard_drop_shift = False
        settings.kb_hard_drop_ctrl = False
        settings.kb_hard_drop_alt = False
        settings.kb_hard_drop_oskey = False

        settings.kb_pause_key = "ESC"
        settings.kb_pause_shift = False
        settings.kb_pause_ctrl = False
        settings.kb_pause_alt = False
        settings.kb_pause_oskey = False

        return {"FINISHED"}


class BLTETRIS_OT_play(bpy.types.Operator):
    """核心 modal operator：处理输入、重力 tick、AI，以及录制/音效。"""

    bl_idname = "bltetris.play"
    bl_label = "Start / Resume"

    _timer = None

    def invoke(self, context: bpy.types.Context, event: bpy.types.Event):
        """启动 modal：初始化 session、计时器与 AI 规划状态。

        Args:
            context: Blender 上下文。
            event: 触发 invoke 的事件。

        Returns:
            Operator 状态集合（成功时为 `{"RUNNING_MODAL"}`）。
        """

        settings = properties.get_settings(context)
        _ensure_visuals(context, settings=settings)

        sess = _get_or_create_session_from_settings(context)
        if sess.running:
            self.report({"INFO"}, "Tetris is already running")
            return {"CANCELLED"}

        session.clear_pause_request()

        _sync_runtime_objects(context, sess)

        sess.running = True
        sess.cell_size = float(settings.cell_size)
        sess.drop_interval = _effective_drop_interval(settings=settings, level=int(getattr(sess.game, "level", 1) or 1))
        sess.last_drop_time = time.monotonic()

        self._ai_last_action_time = 0.0
        self._ai_last_drop_time = 0.0
        self._ai_drop_active = False
        self._ai_plan: list[str] = []
        self._ai_signature: tuple[str, int, int, int, str] | None = None

        # Key state for DAS/ARR / soft drop.
        self._key_left = False
        self._key_right = False
        self._key_down = False

        self._horiz_dir = 0
        self._horiz_dir_start_time = 0.0
        self._horiz_last_repeat_time = 0.0
        self._soft_drop_last_time = 0.0

        # Lock delay：当当前块触地后开始计时，超时后才 lock。
        self._lock_started_at: float | None = None

        audio.start_bgm(settings=settings, level=int(getattr(sess.game, "level", 1) or 1), paused=False)
        audio.play_sfx(event="start", settings=settings)

        _sync_scene_state(context, sess)

        wm = context.window_manager
        self._timer = wm.event_timer_add(session.TIMER_STEP_SECONDS, window=context.window)
        wm.modal_handler_add(self)
        return {"RUNNING_MODAL"}

    def modal(self, context: bpy.types.Context, event: bpy.types.Event):
        """主循环：按事件驱动推进游戏，并在需要时刷新点云。

        Args:
            context: Blender 上下文。
            event: 当前事件（键盘/计时器等）。

        Returns:
            Operator 状态集合：
            - `{"RUNNING_MODAL"}`：继续运行
            - `{"CANCELLED"}`：退出并保留状态（暂停）
            - `{"FINISHED"}`：游戏结束或正常结束
        """

        sess = session.get_session()
        if sess is None:
            audio.stop_bgm()
            self.cancel(context)
            return {"CANCELLED"}

        # 关键检查：如果外部（如 Restart）将 running 设为 False，则自动退出 modal。
        # 这防止了 Restart 后旧的 modal 继续运行导致多重 modal 冲突。
        if not sess.running:
            self.cancel(context)
            return {"CANCELLED"}

        settings = properties.get_settings(context)

        # 允许在运行中调整 Next/HUD 与等级参数（不重置棋盘）。
        sess.game.set_next_queue_size(int(getattr(settings, "next_queue_size", 5)))
        sess.game.set_lines_per_level(int(getattr(settings, "lines_per_level", 10)))

        sess.game.scoring_mode = str(getattr(settings, "scoring_mode", "GUIDELINE") or "GUIDELINE").upper()
        sess.game.combo_scoring_mode = str(getattr(settings, "combo_scoring_mode", "MULTIPLY") or "MULTIPLY").upper()
        sess.game.combo_multiplier_step = float(getattr(settings, "combo_multiplier_step", 0.25) or 0.25)

        sess.cell_size = float(settings.cell_size)
        sess.drop_interval = _effective_drop_interval(settings=settings, level=int(getattr(sess.game, "level", 1) or 1))

        # UI 点击 Pause / 其它 operator 请求暂停时，会设置全局 pause_request。
        if session.consume_pause_request():
            audio.play_sfx(event="pause", settings=settings)
            audio.set_bgm_paused(paused=True, settings=settings)
            self.cancel(context)
            return {"CANCELLED"}

        # Pause：退出 modal，但保留 session/game 状态。
        if event.value == "PRESS" and _event_matches_kb_prefix(event, settings=settings, prefix="kb_pause"):
            audio.play_sfx(event="pause", settings=settings)
            audio.set_bgm_paused(paused=True, settings=settings)
            self.cancel(context)
            return {"CANCELLED"}

        # ESC also triggers Pause (if not already bound to kb_pause, which defaults to ESC).
        # Even if kb_pause is re-bound, ESC is a standard "Exit/Pause" key for modals.
        if event.type == 'ESC' and event.value == 'PRESS':
            audio.play_sfx(event="pause", settings=settings)
            audio.set_bgm_paused(paused=True, settings=settings)
            self.cancel(context)
            return {"CANCELLED"}

        event_type = str(getattr(event, "type", "") or "")
        if event_type != "TIMER" and (
            "MOUSE" in event_type or event_type.startswith("TRACKPAD") or event_type.startswith("NDOF")
        ):
            return {"PASS_THROUGH"}

        updated = False
        now = time.monotonic()

        # 手动输入：在未开启 Auto Play 且未在播放动画时处理。
        if not settings.auto_play and not sess.game.clearing_rows:
            if _event_type_matches_kb_prefix(event, settings=settings, prefix="kb_move_left"):
                if event.value == "PRESS" and _event_matches_kb_prefix(event, settings=settings, prefix="kb_move_left"):
                    self._key_left = True
                    self._horiz_dir = -1
                    self._horiz_dir_start_time = now
                    self._horiz_last_repeat_time = now

                    ok = sess.game.try_move(dx=-1, dz=0)
                    if ok:
                        audio.play_sfx(event="move", settings=settings)
                        session.record_snapshot(sess, reason="manual_left")
                        updated = True

                        if sess.game.can_move(dx=0, dz=-1):
                            self._lock_started_at = None
                        else:
                            self._lock_started_at = now

                elif event.value == "RELEASE":
                    self._key_left = False
                    if getattr(self, "_horiz_dir", 0) == -1:
                        if getattr(self, "_key_right", False):
                            self._horiz_dir = 1
                            self._horiz_dir_start_time = now
                            self._horiz_last_repeat_time = now
                        else:
                            self._horiz_dir = 0

            elif _event_type_matches_kb_prefix(event, settings=settings, prefix="kb_move_right"):
                if event.value == "PRESS" and _event_matches_kb_prefix(event, settings=settings, prefix="kb_move_right"):
                    self._key_right = True
                    self._horiz_dir = 1
                    self._horiz_dir_start_time = now
                    self._horiz_last_repeat_time = now

                    ok = sess.game.try_move(dx=1, dz=0)
                    if ok:
                        audio.play_sfx(event="move", settings=settings)
                        session.record_snapshot(sess, reason="manual_right")
                        updated = True

                        if sess.game.can_move(dx=0, dz=-1):
                            self._lock_started_at = None
                        else:
                            self._lock_started_at = now

                elif event.value == "RELEASE":
                    self._key_right = False
                    if getattr(self, "_horiz_dir", 0) == 1:
                        if getattr(self, "_key_left", False):
                            self._horiz_dir = -1
                            self._horiz_dir_start_time = now
                            self._horiz_last_repeat_time = now
                        else:
                            self._horiz_dir = 0

            elif _event_type_matches_kb_prefix(event, settings=settings, prefix="kb_soft_drop"):
                if event.value == "PRESS" and _event_matches_kb_prefix(event, settings=settings, prefix="kb_soft_drop"):
                    self._key_down = True
                    self._soft_drop_last_time = now

                    ok = sess.game.try_move(dx=0, dz=-1)
                    if ok:
                        sess.last_drop_time = now
                        sess.game.score += 1
                        audio.play_sfx(event="soft_drop", settings=settings)
                        session.record_snapshot(sess, reason="manual_soft_drop")
                        updated = True
                        self._lock_started_at = None
                    else:
                        if self._lock_started_at is None:
                            self._lock_started_at = now

                elif event.value == "RELEASE":
                    self._key_down = False

            # One-shot actions
            if event.value == "PRESS":
                if _event_matches_kb_prefix(event, settings=settings, prefix="kb_rotate_cw") or _event_matches_kb_prefix(
                    event, settings=settings, prefix="kb_rotate_cw_alt"
                ):
                    ok = sess.game.try_rotate(cw=True)
                    if ok:
                        audio.play_sfx(event="rotate", settings=settings)
                        session.record_snapshot(sess, reason="manual_rot_cw")
                        updated = True

                        if sess.game.can_move(dx=0, dz=-1):
                            self._lock_started_at = None
                        else:
                            self._lock_started_at = now

                elif _event_matches_kb_prefix(event, settings=settings, prefix="kb_rotate_ccw"):
                    ok = sess.game.try_rotate(cw=False)
                    if ok:
                        audio.play_sfx(event="rotate", settings=settings)
                        session.record_snapshot(sess, reason="manual_rot_ccw")
                        updated = True

                        if sess.game.can_move(dx=0, dz=-1):
                            self._lock_started_at = None
                        else:
                            self._lock_started_at = now

                elif _event_matches_kb_prefix(event, settings=settings, prefix="kb_hold"):
                    ok = sess.game.try_hold()
                    if ok:
                        audio.play_sfx(event="hold", settings=settings)
                        session.record_snapshot(sess, reason="manual_hold")
                        updated = True
                        self._lock_started_at = None
                        sess.last_drop_time = now

                elif _event_matches_kb_prefix(event, settings=settings, prefix="kb_hard_drop"):
                    sess.game.hard_drop()
                    sess.last_drop_time = now
                    self._lock_started_at = None

                    audio.play_sfx(event="hard_drop", settings=settings)
                    if getattr(sess.game, "last_locked", False):
                        audio.play_sfx(event="lock", settings=settings)
                        if int(getattr(sess.game, "last_cleared_lines", 0) or 0) > 0:
                            audio.play_sfx(event="line_clear", settings=settings)
                        if sess.game.game_over:
                            audio.play_sfx(event="game_over", settings=settings)
                            audio.stop_bgm()

                    session.record_snapshot(sess, reason="manual_drop")
                    updated = True

        # TIMER 事件：驱动重力、AI 节奏，以及持续的 BGM 刷新。
        if event.type == "TIMER":
            if sess.game.game_over:
                audio.play_sfx(event="game_over", settings=settings)
                audio.stop_bgm()

                sess.running = False
                _sync_scene_state(context, sess)
                self.cancel(context)
                return {"FINISHED"}

            now = time.monotonic()
            audio.start_bgm(settings=settings, level=int(getattr(sess.game, "level", 1) or 1), paused=False)

            # 消行动画驱动
            if sess.game.clearing_rows:
                sess.game.clear_anim_progress += 0.2 
                updated = True
                
                if sess.game.clear_anim_progress >= 1.0:
                    sess.game.finalize_clear()
                    if not sess.game.game_over:
                        sess.game.spawn_piece()
                    sess.last_drop_time = now
                    self._lock_started_at = None
                    session.record_snapshot(sess, reason="clear_finish")
                
                # 动画进行中，跳过重力与输入
                if updated:
                    _sync_runtime_objects(context, sess)
                    _sync_scene_state(context, sess)
                return {"RUNNING_MODAL"}

            if settings.auto_play and sess.game.current is not None:
                if bool(getattr(self, "_ai_drop_active", False)):
                    drop_interval = float(getattr(settings, "ai_drop_interval", 0.02))
                    if now - float(getattr(self, "_ai_last_drop_time", 0.0)) >= drop_interval:
                        self._ai_last_drop_time = now

                        ok = sess.game.try_move(dx=0, dz=-1)
                        if ok:
                            sess.last_drop_time = now
                            sess.game.score += 1
                            audio.play_sfx(event="soft_drop", settings=settings)
                            session.record_snapshot(sess, reason="ai_soft_drop")
                            updated = True
                            self._lock_started_at = None
                        else:
                            sess.game.lock_current()
                            sess.last_drop_time = now
                            self._ai_drop_active = False
                            self._ai_plan = []
                            self._lock_started_at = None

                            if getattr(sess.game, "last_locked", False):
                                audio.play_sfx(event="lock", settings=settings)
                                if int(getattr(sess.game, "last_cleared_lines", 0) or 0) > 0:
                                    audio.play_sfx(event="line_clear", settings=settings)
                                if sess.game.game_over:
                                    audio.play_sfx(event="game_over", settings=settings)
                                    audio.stop_bgm()

                            session.record_snapshot(sess, reason="ai_lock")
                            updated = True

                        self._ai_last_action_time = now

                elif now - float(getattr(self, "_ai_last_action_time", 0.0)) >= float(settings.ai_action_interval):
                    piece = sess.game.current
                    ai_ply = str(getattr(settings, "ai_ply", "PLY1") or "PLY1")
                    signature = (str(piece.key), int(piece.rotation), int(piece.x), int(piece.z), ai_ply)

                    plan = list(getattr(self, "_ai_plan", []))
                    if not plan or getattr(self, "_ai_signature", None) != signature:
                        weights = _ai_weights_from_settings(settings)

                        if ai_ply == "PLY2":
                            next_weight = float(
                                getattr(settings, "ai_ply2_next_weight", ai.PLY2_NEXT_WEIGHT) or ai.PLY2_NEXT_WEIGHT
                            )
                            best = ai.find_best_placement_2ply(
                                sess.game,
                                weights=weights,
                                next_weight=next_weight,
                            )
                        else:
                            best = ai.find_best_placement(
                                sess.game,
                                weights=weights,
                            )

                        if best is None:
                            plan = ["DROP"]
                        else:
                            plan = ai.plan_actions(piece=piece, target_rotation=best.rotation, target_x=best.x)
                        self._ai_signature = signature

                        if bool(getattr(settings, "ai_show_debug", True)):
                            looks_collection = bpy.data.collections.get(LOOKS_COLLECTION_NAME) or _ensure_visuals(
                                context, settings=settings
                            )
                            target_obj, path_obj = runtime.ensure_ai_debug_objects(
                                looks_collection=looks_collection,
                                block_scale=tuple(float(v) for v in settings.block_scale),
                            )

                            if best is None:
                                runtime.set_points(obj=target_obj, points_world=[], piece_ids=[], colors=[])
                                runtime.set_points(obj=path_obj, points_world=[], piece_ids=[], colors=[])
                            else:
                                origin_corner = runtime.board_origin_corner(
                                    width=int(getattr(sess.game, "width", 0) or 0),
                                    height=int(getattr(sess.game, "height", 0) or 0),
                                    cell_size=float(sess.cell_size),
                                )
                                runtime.sync_ai_debug(
                                    game=sess.game,
                                    target_obj=target_obj,
                                    path_obj=path_obj,
                                    target_rotation=int(best.rotation),
                                    target_x=int(best.x),
                                    target_z=int(best.z),
                                    cell_size=float(sess.cell_size),
                                    origin_corner=origin_corner,
                                    piece_colors=_piece_colors_from_settings(settings),
                                    show_path=bool(getattr(settings, "ai_show_path", True)),
                                )

                    if plan:
                        action = plan.pop(0)
                        self._ai_plan = plan

                        if action == "LEFT":
                            ok = sess.game.try_move(dx=-1, dz=0)
                            updated = ok or updated
                            if ok:
                                audio.play_sfx(event="move", settings=settings)
                                session.record_snapshot(sess, reason="ai_left")

                                if sess.game.can_move(dx=0, dz=-1):
                                    self._lock_started_at = None
                                else:
                                    self._lock_started_at = now
                            else:
                                self._ai_plan = []
                        elif action == "RIGHT":
                            ok = sess.game.try_move(dx=1, dz=0)
                            updated = ok or updated
                            if ok:
                                audio.play_sfx(event="move", settings=settings)
                                session.record_snapshot(sess, reason="ai_right")

                                if sess.game.can_move(dx=0, dz=-1):
                                    self._lock_started_at = None
                                else:
                                    self._lock_started_at = now
                            else:
                                self._ai_plan = []
                        elif action == "ROT_CW":
                            ok = sess.game.try_rotate(cw=True)
                            updated = ok or updated
                            if ok:
                                audio.play_sfx(event="rotate", settings=settings)
                                session.record_snapshot(sess, reason="ai_rot_cw")

                                if sess.game.can_move(dx=0, dz=-1):
                                    self._lock_started_at = None
                                else:
                                    self._lock_started_at = now
                            else:
                                self._ai_plan = []
                        elif action == "ROT_CCW":
                            ok = sess.game.try_rotate(cw=False)
                            updated = ok or updated
                            if ok:
                                audio.play_sfx(event="rotate", settings=settings)
                                session.record_snapshot(sess, reason="ai_rot_ccw")

                                if sess.game.can_move(dx=0, dz=-1):
                                    self._lock_started_at = None
                                else:
                                    self._lock_started_at = now
                            else:
                                self._ai_plan = []
                        elif action == "DROP":
                            drop_mode = str(getattr(settings, "ai_drop_mode", "HARD") or "HARD").upper()
                            if drop_mode == "SOFT":
                                self._ai_drop_active = True
                                self._ai_last_drop_time = now
                                self._ai_plan = []
                                self._lock_started_at = None
                            else:
                                sess.game.hard_drop()
                                sess.last_drop_time = now
                                self._ai_plan = []
                                self._lock_started_at = None

                                audio.play_sfx(event="hard_drop", settings=settings)
                                if getattr(sess.game, "last_locked", False):
                                    audio.play_sfx(event="lock", settings=settings)
                                    if int(getattr(sess.game, "last_cleared_lines", 0) or 0) > 0:
                                        audio.play_sfx(event="line_clear", settings=settings)
                                    if sess.game.game_over:
                                        audio.play_sfx(event="game_over", settings=settings)
                                        audio.stop_bgm()

                                session.record_snapshot(sess, reason="ai_drop")
                                updated = True

                        self._ai_last_action_time = now

            # -------------------- Manual Hold (DAS/ARR / Soft Drop) --------------------

            if not settings.auto_play and sess.game.current is not None:
                # DAS/ARR：按住左右连续移动。
                horiz_dir = int(getattr(self, "_horiz_dir", 0) or 0)
                if horiz_dir != 0:
                    das = float(getattr(settings, "das_delay", 0.15))
                    arr = float(getattr(settings, "arr_interval", 0.05))

                    if now - float(getattr(self, "_horiz_dir_start_time", 0.0)) >= das:
                        if arr <= 0.0:
                            # ARR=0：尽可能滑到头（每次 TIMER 最多走 width 次，避免死循环）。
                            moved_any = False
                            max_steps = max(1, int(getattr(sess.game, "width", 10) or 10))
                            for _ in range(max_steps):
                                ok = sess.game.try_move(dx=horiz_dir, dz=0)
                                if not ok:
                                    break
                                moved_any = True
                                audio.play_sfx(event="move", settings=settings)
                                session.record_snapshot(sess, reason="das_arr_move")

                            if moved_any:
                                updated = True
                                if sess.game.can_move(dx=0, dz=-1):
                                    self._lock_started_at = None
                                else:
                                    self._lock_started_at = now
                        else:
                            if now - float(getattr(self, "_horiz_last_repeat_time", 0.0)) >= arr:
                                ok = sess.game.try_move(dx=horiz_dir, dz=0)
                                if ok:
                                    self._horiz_last_repeat_time = now
                                    audio.play_sfx(event="move", settings=settings)
                                    session.record_snapshot(sess, reason="das_arr_move")
                                    updated = True

                                    if sess.game.can_move(dx=0, dz=-1):
                                        self._lock_started_at = None
                                    else:
                                        self._lock_started_at = now

                # Soft drop：按住下键连续下落（不立即 lock）。
                if bool(getattr(self, "_key_down", False)):
                    interval = float(getattr(settings, "soft_drop_interval", 0.05))
                    if now - float(getattr(self, "_soft_drop_last_time", 0.0)) >= interval:
                        self._soft_drop_last_time = now
                        ok = sess.game.try_move(dx=0, dz=-1)
                        if ok:
                            sess.last_drop_time = now
                            sess.game.score += 1
                            audio.play_sfx(event="soft_drop", settings=settings)
                            session.record_snapshot(sess, reason="soft_drop_hold")
                            updated = True
                            self._lock_started_at = None
                        else:
                            if self._lock_started_at is None:
                                self._lock_started_at = now

            # -------------------- Gravity --------------------

            if sess.game.current is not None and now - sess.last_drop_time >= sess.drop_interval:
                ok = sess.game.try_move(dx=0, dz=-1)
                if ok:
                    sess.last_drop_time = now
                    session.record_snapshot(sess, reason="gravity_move")
                    updated = True
                    self._lock_started_at = None
                else:
                    if self._lock_started_at is None:
                        self._lock_started_at = now

            # -------------------- Lock Delay --------------------

            lock_delay = float(getattr(settings, "lock_delay", 0.0))
            if sess.game.current is None:
                self._lock_started_at = None
            elif sess.game.can_move(dx=0, dz=-1):
                # 还能下落：不在 lock delay 状态。
                self._lock_started_at = None
            else:
                # 触地：开始/继续 lock delay 倒计时。
                if self._lock_started_at is None:
                    self._lock_started_at = now

                if now - float(self._lock_started_at) >= lock_delay:
                    sess.game.lock_current()
                    sess.last_drop_time = now
                    self._lock_started_at = None

                    if getattr(sess.game, "last_locked", False):
                        audio.play_sfx(event="lock", settings=settings)
                        if int(getattr(sess.game, "last_cleared_lines", 0) or 0) > 0:
                            audio.play_sfx(event="line_clear", settings=settings)

                    if sess.game.game_over:
                        audio.play_sfx(event="game_over", settings=settings)
                        audio.stop_bgm()

                        sess.running = False
                        _sync_scene_state(context, sess)
                        self.cancel(context)
                        return {"FINISHED"}

                    session.record_snapshot(sess, reason="lock_delay")
                    updated = True

        if updated:
            _sync_runtime_objects(context, sess)
            _sync_scene_state(context, sess)

        return {"RUNNING_MODAL"}

    def cancel(self, context: bpy.types.Context):
        """退出 modal：停止计时器、更新状态。

        Args:
            context: Blender 上下文。

        Returns:
            None
        """

        # 停止所有持续性音频（BGM），避免退出后仍在播放。
        audio.stop_bgm()

        sess = session.get_session()
        if sess is not None:
            sess.running = False
            _sync_scene_state(context, sess)

        wm = context.window_manager
        if self._timer is not None:
            wm.event_timer_remove(self._timer)
            self._timer = None


class BLTETRIS_OT_cleanup(bpy.types.Operator):
    """清理运行时对象与会话（不删除 Looks/Assets）。"""

    bl_idname = "bltetris.cleanup"
    bl_label = "Cleanup Runtime"

    def execute(self, context: bpy.types.Context):
        """如果正在运行则拒绝清理，否则释放对象/mesh 并重置 UI 状态。

        Args:
            context: Blender 上下文。

        Returns:
            Operator 状态集合（运行中拒绝清理时为 `{"CANCELLED"}`）。
        """

        sess = session.get_session()
        if sess is not None and sess.running:
            self.report({"WARNING"}, "Pause before cleanup")
            return {"CANCELLED"}

        audio.stop_all()

        runtime.cleanup_runtime()
        session.clear_session()

        state = properties.get_state(context)
        state.running = False
        state.game_over = False

        return {"FINISHED"}


_classes = (
    BLTETRIS_OT_setup_assets,
    BLTETRIS_OT_setup_geometry_nodes,
    BLTETRIS_OT_apply_looks,
    BLTETRIS_OT_record_start,
    BLTETRIS_OT_record_stop,
    BLTETRIS_OT_record_clear,
    BLTETRIS_OT_export_replay_json,
    BLTETRIS_OT_import_replay_json,
    BLTETRIS_OT_bake_replay,
    BLTETRIS_OT_restart,
    BLTETRIS_OT_step,
    BLTETRIS_OT_pause,
    BLTETRIS_OT_capture_keybind,
    BLTETRIS_OT_clear_keybind,
    BLTETRIS_OT_reset_keybinds,
    BLTETRIS_OT_play,
    BLTETRIS_OT_cleanup,
)


def register() -> None:
    """向 Blender 注册本模块的所有 Operator 类。"""

    for cls in _classes:
        bpy.utils.register_class(cls)


def unregister() -> None:
    """从 Blender 反注册本模块的所有 Operator 类。"""

    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
