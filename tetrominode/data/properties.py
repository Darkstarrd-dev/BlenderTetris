"""插件设置/状态的 Scene 属性。

我们把可调参数都挂在 `bpy.types.Scene` 上：
- `bltetris_settings`：用户可配置的参数（棋盘大小、速度、外观、AI、音频、回放烘焙参数）
- `bltetris_state`：运行态（running/game_over），并标记为 `SKIP_SAVE`

重要坑点：
- Blender 的 PropertyGroup 动态属性，必须写进 `Class.__annotations__`，不要用 `setattr`。
  否则 UI 会显示 `_PropertyDeferred` 并导致注册/序列化异常。

约定：
- 这里的 Settings 只负责“参数”，不负责运行时对象/逻辑。
- 运行时对象/逻辑由 operators/session/runtime 等模块控制。
"""

from __future__ import annotations

import bpy

from .constants import BOARD_HEIGHT_DEFAULT, BOARD_WIDTH_DEFAULT, CELL_SIZE_DEFAULT
from .session import DEFAULT_DROP_INTERVAL
from ..core.tetrominoes import TETROMINO_KEYS


# 默认配色（RGBA）。材质默认会读取 `bltetris_color`。
DEFAULT_PIECE_COLORS: dict[str, tuple[float, float, float, float]] = {
    "I": (0.0, 0.9, 0.9, 1.0),
    "O": (0.95, 0.9, 0.0, 1.0),
    "T": (0.6, 0.2, 0.9, 1.0),
    "S": (0.0, 0.85, 0.25, 1.0),
    "Z": (0.9, 0.1, 0.1, 1.0),
    "J": (0.1, 0.35, 0.9, 1.0),
    "L": (0.95, 0.5, 0.05, 1.0),
}

DEFAULT_BORDER_COLOR: tuple[float, float, float, float] = (0.2, 0.2, 0.2, 1.0)


class BLTETRIS_PG_settings(bpy.types.PropertyGroup):
    """用户可配置的插件参数（挂在 Scene 上）。

    包含：
    - 游戏参数：棋盘尺寸、cell_size、下落速度、随机 seed
    - AI 参数：Auto Play 开关、动作间隔
    - 音频参数：SFX/BGM 开关、音量、自定义文件路径
    - 回放参数：烘焙起始帧、帧步长、是否覆盖旧回放
    - 外观参数：block scale、边框颜色/材质/倒角、每个 piece 的颜色/材质/倒角
    """

    board_width: bpy.props.IntProperty(
        name="Board Width",
        default=BOARD_WIDTH_DEFAULT,
        min=4,
        max=40,
    )

    board_height: bpy.props.IntProperty(
        name="Board Height",
        default=BOARD_HEIGHT_DEFAULT,
        min=4,
        max=80,
    )

    cell_size: bpy.props.FloatProperty(
        name="Cell Size",
        default=CELL_SIZE_DEFAULT,
        min=0.001,
    )

    drop_interval: bpy.props.FloatProperty(
        name="Drop Interval",
        default=DEFAULT_DROP_INTERVAL,
        min=0.05,
        max=5.0,
        subtype="TIME",
    )

    seed: bpy.props.IntProperty(
        name="Seed",
        default=0,
    )

    next_queue_size: bpy.props.IntProperty(
        name="Next Count",
        default=5,
        min=1,
        max=10,
    )

    # -------------------- Feel / Input --------------------

    das_delay: bpy.props.FloatProperty(
        name="DAS Delay",
        default=0.15,
        min=0.0,
        max=1.0,
        subtype="TIME",
    )

    arr_interval: bpy.props.FloatProperty(
        name="ARR Interval",
        default=0.05,
        min=0.0,
        max=1.0,
        subtype="TIME",
    )

    soft_drop_interval: bpy.props.FloatProperty(
        name="Soft Drop Interval",
        default=0.05,
        min=0.01,
        max=1.0,
        subtype="TIME",
    )

    lock_delay: bpy.props.FloatProperty(
        name="Lock Delay",
        default=0.5,
        min=0.0,
        max=5.0,
        subtype="TIME",
    )

    # -------------------- Keybinds (saved in .blend) --------------------

    kb_move_left_key: bpy.props.StringProperty(
        name="Move Left",
        description="Keybind for moving left (press/hold for DAS/ARR)",
        default="LEFT_ARROW",
    )
    kb_move_left_shift: bpy.props.BoolProperty(name="Move Left Shift", default=False)
    kb_move_left_ctrl: bpy.props.BoolProperty(name="Move Left Ctrl", default=False)
    kb_move_left_alt: bpy.props.BoolProperty(name="Move Left Alt", default=False)
    kb_move_left_oskey: bpy.props.BoolProperty(name="Move Left OSKey", default=False)

    kb_move_right_key: bpy.props.StringProperty(
        name="Move Right",
        description="Keybind for moving right (press/hold for DAS/ARR)",
        default="RIGHT_ARROW",
    )
    kb_move_right_shift: bpy.props.BoolProperty(name="Move Right Shift", default=False)
    kb_move_right_ctrl: bpy.props.BoolProperty(name="Move Right Ctrl", default=False)
    kb_move_right_alt: bpy.props.BoolProperty(name="Move Right Alt", default=False)
    kb_move_right_oskey: bpy.props.BoolProperty(name="Move Right OSKey", default=False)

    kb_soft_drop_key: bpy.props.StringProperty(
        name="Soft Drop",
        description="Keybind for soft drop (press/hold)",
        default="DOWN_ARROW",
    )
    kb_soft_drop_shift: bpy.props.BoolProperty(name="Soft Drop Shift", default=False)
    kb_soft_drop_ctrl: bpy.props.BoolProperty(name="Soft Drop Ctrl", default=False)
    kb_soft_drop_alt: bpy.props.BoolProperty(name="Soft Drop Alt", default=False)
    kb_soft_drop_oskey: bpy.props.BoolProperty(name="Soft Drop OSKey", default=False)

    kb_rotate_cw_key: bpy.props.StringProperty(
        name="Rotate CW",
        description="Keybind for rotate clockwise",
        default="UP_ARROW",
    )
    kb_rotate_cw_shift: bpy.props.BoolProperty(name="Rotate CW Shift", default=False)
    kb_rotate_cw_ctrl: bpy.props.BoolProperty(name="Rotate CW Ctrl", default=False)
    kb_rotate_cw_alt: bpy.props.BoolProperty(name="Rotate CW Alt", default=False)
    kb_rotate_cw_oskey: bpy.props.BoolProperty(name="Rotate CW OSKey", default=False)

    kb_rotate_cw_alt_key: bpy.props.StringProperty(
        name="Rotate CW (Alt)",
        description="Alternate keybind for rotate clockwise",
        default="X",
    )
    kb_rotate_cw_alt_shift: bpy.props.BoolProperty(name="Rotate CW (Alt) Shift", default=False)
    kb_rotate_cw_alt_ctrl: bpy.props.BoolProperty(name="Rotate CW (Alt) Ctrl", default=False)
    kb_rotate_cw_alt_alt: bpy.props.BoolProperty(name="Rotate CW (Alt) Alt", default=False)
    kb_rotate_cw_alt_oskey: bpy.props.BoolProperty(name="Rotate CW (Alt) OSKey", default=False)

    kb_rotate_ccw_key: bpy.props.StringProperty(
        name="Rotate CCW",
        description="Keybind for rotate counter-clockwise",
        default="Z",
    )
    kb_rotate_ccw_shift: bpy.props.BoolProperty(name="Rotate CCW Shift", default=False)
    kb_rotate_ccw_ctrl: bpy.props.BoolProperty(name="Rotate CCW Ctrl", default=False)
    kb_rotate_ccw_alt: bpy.props.BoolProperty(name="Rotate CCW Alt", default=False)
    kb_rotate_ccw_oskey: bpy.props.BoolProperty(name="Rotate CCW OSKey", default=False)

    kb_hold_key: bpy.props.StringProperty(
        name="Hold",
        description="Keybind for hold",
        default="C",
    )
    kb_hold_shift: bpy.props.BoolProperty(name="Hold Shift", default=False)
    kb_hold_ctrl: bpy.props.BoolProperty(name="Hold Ctrl", default=False)
    kb_hold_alt: bpy.props.BoolProperty(name="Hold Alt", default=False)
    kb_hold_oskey: bpy.props.BoolProperty(name="Hold OSKey", default=False)

    kb_hard_drop_key: bpy.props.StringProperty(
        name="Hard Drop",
        description="Keybind for hard drop",
        default="SPACE",
    )
    kb_hard_drop_shift: bpy.props.BoolProperty(name="Hard Drop Shift", default=False)
    kb_hard_drop_ctrl: bpy.props.BoolProperty(name="Hard Drop Ctrl", default=False)
    kb_hard_drop_alt: bpy.props.BoolProperty(name="Hard Drop Alt", default=False)
    kb_hard_drop_oskey: bpy.props.BoolProperty(name="Hard Drop OSKey", default=False)

    kb_pause_key: bpy.props.StringProperty(
        name="Pause",
        description="Keybind for pause (exit modal and keep state)",
        default="ESC",
    )
    kb_pause_shift: bpy.props.BoolProperty(name="Pause Shift", default=False)
    kb_pause_ctrl: bpy.props.BoolProperty(name="Pause Ctrl", default=False)
    kb_pause_alt: bpy.props.BoolProperty(name="Pause Alt", default=False)
    kb_pause_oskey: bpy.props.BoolProperty(name="Pause OSKey", default=False)

    rotation_system: bpy.props.EnumProperty(
        name="Rotation System",
        description="选择旋转算法与踢墙规则",
        items=[
            ("SRS", "SRS (Guideline)", "标准 Guideline SRS 旋转系统"),
            ("SIMPLE", "Simple (Classic)", "经典简化旋转（旋转+归一化+简单踢墙）"),
        ],
        default="SRS",
    )

    # -------------------- Scoring / Speed --------------------

    lines_per_level: bpy.props.IntProperty(
        name="Lines Per Level",
        default=10,
        min=1,
        max=200,
    )

    use_level_speed: bpy.props.BoolProperty(
        name="Use Level Speed",
        default=True,
    )

    level_speed_multiplier: bpy.props.FloatProperty(
        name="Speed Multiplier",
        default=0.85,
        min=0.5,
        max=0.99,
        subtype="FACTOR",
    )

    min_drop_interval: bpy.props.FloatProperty(
        name="Min Drop Interval",
        default=0.05,
        min=0.01,
        max=5.0,
        subtype="TIME",
    )

    speed_curve: bpy.props.EnumProperty(
        name="Speed Curve",
        description="速度曲线（在启用 Use Level Speed 时生效）",
        items=[
            ("MULTIPLIER", "Multiplier", "DropInterval = base * multiplier^(level-1)"),
            ("GUIDELINE", "Guideline", "DropInterval = base * (0.8-(level-1)*0.007)^(level-1)"),
        ],
        default="MULTIPLIER",
    )

    scoring_mode: bpy.props.EnumProperty(
        name="Scoring Mode",
        description="计分模式（影响消行/连击/B2B 等）",
        items=[
            ("GUIDELINE", "Guideline", "Guideline-ish scoring (supports B2B, combo, T-Spin)"),
            ("EXPONENTIAL", "Exponential", "Line clear base scores use an exponential curve"),
        ],
        default="GUIDELINE",
    )

    combo_scoring_mode: bpy.props.EnumProperty(
        name="Combo Mode",
        description="连击加成模式",
        items=[
            ("MULTIPLY", "Multiply", "Multiply points by (1 + combo * step)"),
            ("ADD", "Add", "Add +50*combo*level (legacy)"),
        ],
        default="MULTIPLY",
    )

    combo_multiplier_step: bpy.props.FloatProperty(
        name="Combo Step",
        default=0.25,
        min=0.0,
        max=2.0,
        subtype="FACTOR",
    )

    auto_play: bpy.props.BoolProperty(
        name="Auto Play",
        default=False,
    )

    ai_ply: bpy.props.EnumProperty(
        name="AI Depth",
        description="选择 AI 搜索深度（1-ply 更快，2-ply 更强）",
        items=[
            ("PLY1", "1-ply", "只评估当前块（更快）"),
            ("PLY2", "2-ply", "考虑下一块（更强）"),
        ],
        default="PLY1",
    )

    ai_strategy: bpy.props.EnumProperty(
        name="AI Strategy",
        description="AI 策略预设（Custom 时使用自定义权重）",
        items=[
            ("STABLE", "Stable", "更稳：强烈避免 holes"),
            ("HIGH_SCORE", "High Score", "更高分：更偏好消行"),
            ("SHOW", "Show", "更观赏：更偏好平整"),
            ("CUSTOM", "Custom", "使用自定义权重"),
        ],
        default="STABLE",
    )

    ai_ply2_next_weight: bpy.props.FloatProperty(
        name="2-ply Next Weight",
        default=0.8,
        min=0.0,
        max=2.0,
        subtype="FACTOR",
    )

    ai_w_aggregate_height: bpy.props.FloatProperty(
        name="W: Height",
        default=-0.510066,
        min=-5.0,
        max=5.0,
    )

    ai_w_lines: bpy.props.FloatProperty(
        name="W: Lines",
        default=0.760666,
        min=-5.0,
        max=5.0,
    )

    ai_w_holes: bpy.props.FloatProperty(
        name="W: Holes",
        default=-0.35663,
        min=-5.0,
        max=5.0,
    )

    ai_w_bumpiness: bpy.props.FloatProperty(
        name="W: Bumpiness",
        default=-0.184483,
        min=-5.0,
        max=5.0,
    )

    ai_action_interval: bpy.props.FloatProperty(
        name="AI Action Interval",
        default=0.06,
        min=0.01,
        max=1.0,
        subtype="TIME",
    )

    ai_drop_mode: bpy.props.EnumProperty(
        name="AI Drop Mode",
        description="AI 的下落方式：瞬间落地或逐步下落",
        items=[
            ("HARD", "Instant", "Hard drop (instant lock)"),
            ("SOFT", "Step", "Soft-drop step-by-step then lock"),
        ],
        default="HARD",
    )

    ai_drop_interval: bpy.props.FloatProperty(
        name="AI Drop Interval",
        default=0.02,
        min=0.005,
        max=1.0,
        subtype="TIME",
    )

    ai_show_debug: bpy.props.BoolProperty(
        name="AI Visualize",
        default=True,
    )

    ai_show_path: bpy.props.BoolProperty(
        name="AI Show Path",
        default=True,
    )

    ui_show_advanced: bpy.props.BoolProperty(
        name="Advanced Settings",
        default=False,
        options={"SKIP_SAVE"},
    )

    # -------------------- Audio --------------------

    audio_enabled: bpy.props.BoolProperty(
        name="Audio Enabled",
        default=True,
    )

    sfx_enabled: bpy.props.BoolProperty(
        name="SFX Enabled",
        default=True,
    )

    bgm_enabled: bpy.props.BoolProperty(
        name="BGM Enabled",
        default=True,
    )

    sfx_volume: bpy.props.FloatProperty(
        name="SFX Volume",
        default=0.35,
        min=0.0,
        max=1.0,
        subtype="FACTOR",
    )

    bgm_volume: bpy.props.FloatProperty(
        name="BGM Volume",
        default=0.2,
        min=0.0,
        max=1.0,
        subtype="FACTOR",
    )

    bgm_fade_in: bpy.props.FloatProperty(
        name="BGM Fade In",
        description="BGM 开始/恢复时的淡入时间",
        default=0.4,
        min=0.0,
        max=5.0,
        subtype="TIME",
    )

    bgm_fade_out: bpy.props.FloatProperty(
        name="BGM Fade Out",
        description="BGM 停止时的淡出时间",
        default=0.25,
        min=0.0,
        max=5.0,
        subtype="TIME",
    )

    bgm_pause_duck: bpy.props.FloatProperty(
        name="BGM Pause Duck",
        description="暂停时的 BGM 音量倍率（0=静音）",
        default=0.25,
        min=0.0,
        max=1.0,
        subtype="FACTOR",
    )

    bgm_sync_to_level: bpy.props.BoolProperty(
        name="Sync Tempo To Level",
        description="程序化 BGM 的节奏随等级变化",
        default=True,
    )

    bgm_use_custom: bpy.props.BoolProperty(
        name="Use Custom BGM",
        default=False,
    )

    bgm_filepath: bpy.props.StringProperty(
        name="BGM File",
        default="",
        subtype="FILE_PATH",
    )

    sfx_use_custom: bpy.props.BoolProperty(
        name="Use Custom SFX",
        default=False,
    )

    sfx_filepath: bpy.props.StringProperty(
        name="SFX File",
        default="",
        subtype="FILE_PATH",
    )

    sfx_use_per_event: bpy.props.BoolProperty(
        name="Per-Event SFX",
        description="为每个事件单独指定音效文件（未指定则回退到全局 SFX/程序化音效）",
        default=False,
    )

    sfx_priority_window: bpy.props.FloatProperty(
        name="SFX Priority Window",
        description="在该时间窗内，低优先级音效可能被跳过，以避免同一帧内过多叠加",
        default=0.06,
        min=0.0,
        max=0.5,
        subtype="TIME",
    )

    sfx_throttle_enabled: bpy.props.BoolProperty(
        name="SFX Throttle",
        description="限制高频音效的触发频率，避免叠加过密",
        default=True,
    )

    sfx_throttle_scale: bpy.props.FloatProperty(
        name="SFX Throttle Scale",
        description="节流强度倍率（0=关闭节流，>1 更严格）",
        default=1.0,
        min=0.0,
        max=4.0,
        subtype="FACTOR",
    )

    # -------------------- Replay Bake --------------------

    bake_start_frame: bpy.props.IntProperty(
        name="Bake Start Frame",
        default=1,
        min=1,
    )

    bake_frame_step: bpy.props.IntProperty(
        name="Bake Frame Step",
        default=1,
        min=1,
        max=120,
    )

    bake_replace_existing: bpy.props.BoolProperty(
        name="Replace Existing Replay",
        default=True,
    )

    # -------------------- Looks --------------------

    show_ghost: bpy.props.BoolProperty(
        name="Show Ghost",
        description="显示当前方块的落点阴影",
        default=True,
    )

    block_scale: bpy.props.FloatVectorProperty(
        name="Block Scale",
        size=3,
        default=(1.0, 1.0, 1.0),
        min=0.001,
        max=10.0,
        subtype="XYZ",
    )

    pieces_material: bpy.props.PointerProperty(
        name="Pieces Material",
        type=bpy.types.Material,
    )

    pieces_bevel_width: bpy.props.FloatProperty(
        name="Pieces Bevel Width",
        default=0.06,
        min=0.0,
        max=0.5,
        subtype="DISTANCE",
    )

    pieces_bevel_segments: bpy.props.IntProperty(
        name="Pieces Bevel Segments",
        default=2,
        min=0,
        max=16,
    )

    border_color: bpy.props.FloatVectorProperty(
        name="Border Color",
        size=4,
        subtype="COLOR",
        default=DEFAULT_BORDER_COLOR,
        min=0.0,
        max=1.0,
    )

    border_material: bpy.props.PointerProperty(
        name="Border Material",
        type=bpy.types.Material,
    )

    border_bevel_width: bpy.props.FloatProperty(
        name="Border Bevel Width",
        default=0.06,
        min=0.0,
        max=0.5,
        subtype="DISTANCE",
    )

    border_bevel_segments: bpy.props.IntProperty(
        name="Border Bevel Segments",
        default=2,
        min=0,
        max=16,
    )


def _register_piece_style_properties() -> None:
    """为每个 tetromino 注册独立的外观参数。

    动态属性：
    - `color_<K>`：该块的颜色（RGBA）
    - `material_<K>`：该块的材质（可选，默认走 AttrColor）
    - `bevel_width_<K>` / `bevel_segments_<K>`：该块的倒角

    重要：必须写进 `BLTETRIS_PG_settings.__annotations__`。
    """

    annotations = BLTETRIS_PG_settings.__annotations__

    for key in TETROMINO_KEYS:
        # color：RGBA
        annotations[f"color_{key}"] = bpy.props.FloatVectorProperty(
            name=f"{key} Color",
            size=4,
            subtype="COLOR",
            default=DEFAULT_PIECE_COLORS.get(key, (1.0, 1.0, 1.0, 1.0)),
            min=0.0,
            max=1.0,
        )

        annotations[f"piece_override_style_{key}"] = bpy.props.BoolProperty(
            name=f"{key} Override Style",
            default=False,
        )

        # material：可选（不指定则使用默认 AttrColor 材质）
        annotations[f"material_{key}"] = bpy.props.PointerProperty(
            name=f"{key} Material",
            type=bpy.types.Material,
        )

        # bevel：每个块单独可调
        annotations[f"bevel_width_{key}"] = bpy.props.FloatProperty(
            name=f"{key} Bevel",
            default=0.06,
            min=0.0,
            max=0.5,
            subtype="DISTANCE",
        )

        annotations[f"bevel_segments_{key}"] = bpy.props.IntProperty(
            name=f"{key} Bevel Segments",
            default=2,
            min=0,
            max=16,
        )


_SFX_EVENT_FILE_LABELS: dict[str, str] = {
    "start": "Start SFX",
    "pause": "Pause SFX",
    "move": "Move SFX",
    "rotate": "Rotate SFX",
    "soft_drop": "Soft Drop SFX",
    "hard_drop": "Hard Drop SFX",
    "hold": "Hold SFX",
    "lock": "Lock SFX",
    "line_clear": "Line Clear SFX",
    "game_over": "Game Over SFX",
}


def _register_sfx_event_audio_properties() -> None:
    """为每个音频事件注册独立的 SFX 文件路径。"""

    annotations = BLTETRIS_PG_settings.__annotations__

    for event_key, label in _SFX_EVENT_FILE_LABELS.items():
        annotations[f"sfx_event_filepath_{event_key}"] = bpy.props.StringProperty(
            name=label,
            default="",
            subtype="FILE_PATH",
        )


# 动态属性需要在 register 之前构建。
_register_sfx_event_audio_properties()
_register_piece_style_properties()


class BLTETRIS_PG_state(bpy.types.PropertyGroup):
    """运行态标记（挂在 Scene 上，且 SKIP_SAVE）。

    目的：
    - UI 显示当前状态
    - 防止用户重复启动多个 modal

    注意：真实运行状态仍以 `session.get_session()` 为准。
    """

    running: bpy.props.BoolProperty(
        name="Running",
        default=False,
        options={"SKIP_SAVE"},
    )

    game_over: bpy.props.BoolProperty(
        name="Game Over",
        default=False,
        options={"SKIP_SAVE"},
    )


_classes = (
    BLTETRIS_PG_settings,
    BLTETRIS_PG_state,
)


def get_settings(context: bpy.types.Context) -> BLTETRIS_PG_settings:
    """快捷获取 Scene 上的 settings。

    Args:
        context: Blender 上下文。

    Returns:
        绑定在 `context.scene` 上的 `BLTETRIS_PG_settings`。
    """

    return context.scene.bltetris_settings


def get_state(context: bpy.types.Context) -> BLTETRIS_PG_state:
    """快捷获取 Scene 上的 state。

    Args:
        context: Blender 上下文。

    Returns:
        绑定在 `context.scene` 上的 `BLTETRIS_PG_state`。
    """

    return context.scene.bltetris_state


def register() -> None:
    """注册 PropertyGroup 并把指针属性挂到 Scene 上。"""

    for cls in _classes:
        bpy.utils.register_class(cls)

    # 通过 PointerProperty 把属性组挂到 Scene。
    bpy.types.Scene.bltetris_settings = bpy.props.PointerProperty(type=BLTETRIS_PG_settings)
    bpy.types.Scene.bltetris_state = bpy.props.PointerProperty(type=BLTETRIS_PG_state)


def unregister() -> None:
    """卸载 PropertyGroup 与 Scene 指针属性。"""

    if hasattr(bpy.types.Scene, "bltetris_state"):
        del bpy.types.Scene.bltetris_state
    if hasattr(bpy.types.Scene, "bltetris_settings"):
        del bpy.types.Scene.bltetris_settings

    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
