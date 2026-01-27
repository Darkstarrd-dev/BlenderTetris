"""侧边栏 UI 面板（View3D > Sidebar > Tetrominode）。

面板目标：
- 提供最小可用的控制入口：Start/Resume、Pause、Restart、Cleanup
- 暴露可调参数：棋盘、速度、AI、音频、录制/烘焙、外观（颜色/材质/倒角）

约定：
- UI 只读 session 的运行状态与录制状态
- 大部分“行为”交由 operators 执行（UI 只画按钮与 prop）
"""

from __future__ import annotations

import bpy

from ..data import properties
from ..data import session
from ..core.tetrominoes import TETROMINO_KEYS


def _format_keybind(
    *, key: str, shift: bool, ctrl: bool, alt: bool, oskey: bool
) -> str:
    if not key:
        return "(Unbound)"

    parts: list[str] = []
    if ctrl:
        parts.append("Ctrl")
    if shift:
        parts.append("Shift")
    if alt:
        parts.append("Alt")
    if oskey:
        parts.append("OSKey")

    parts.append(str(key))
    return "+".join(parts)


def _draw_keybind_row(
    *, layout: bpy.types.UILayout, settings, label: str, prefix: str
) -> None:
    key = str(getattr(settings, f"{prefix}_key", "") or "")
    shift = bool(getattr(settings, f"{prefix}_shift", False))
    ctrl = bool(getattr(settings, f"{prefix}_ctrl", False))
    alt = bool(getattr(settings, f"{prefix}_alt", False))
    oskey = bool(getattr(settings, f"{prefix}_oskey", False))

    row = layout.row(align=True)
    row.label(text=label)
    row.label(
        text=_format_keybind(key=key, shift=shift, ctrl=ctrl, alt=alt, oskey=oskey)
    )

    op = row.operator("bltetris.capture_keybind", text="Set")
    op.prefix = prefix

    op = row.operator("bltetris.clear_keybind", text="Clear")
    op.prefix = prefix


class BLTETRIS_PT_main(bpy.types.Panel):
    """主面板：把核心操作集中到一个 Sidebar Tab。"""

    bl_idname = "BLTETRIS_PT_main"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Tetrominode"
    bl_label = "Tetrominode"

    def draw(self, context: bpy.types.Context):
        """绘制 UI。"""

        layout = self.layout
        settings = properties.get_settings(context)
        state = properties.get_state(context)
        sess = session.get_session()

        # -------------------- Status (Top Info) --------------------
        col = layout.column(align=True)

        row = col.row(align=True)
        row.label(text="Status:")
        status_key = "PAUSED"
        if state.game_over:
            status_key = "GAME OVER"
        elif state.running:
            status_key = "RUNNING"
        row.label(text=status_key)

        if sess is not None:
            score = int(getattr(sess.game, "score", 0))
            level = int(getattr(sess.game, "level", 1) or 1)
            lines = int(getattr(sess.game, "lines_cleared_total", 0))
            speed = float(
                getattr(
                    sess,
                    "drop_interval",
                    float(getattr(settings, "drop_interval", 0.0)),
                )
            )

            row = col.row(align=True)
            row.label(text="Score:")
            row.label(text=str(score))

            row = col.row(align=True)
            row.label(text="Level:")
            row.label(text=str(level))

            row = col.row(align=True)
            row.label(text="Lines:")
            row.label(text=str(lines))

            row = col.row(align=True)
            row.label(text="Speed:")
            row.label(text=f"{speed:.2f}s")

            ai_ply = str(getattr(settings, "ai_ply", "PLY1") or "PLY1")
            ai_status = ai_ply if bool(settings.auto_play) else "OFF"

            row = col.row(align=True)
            row.label(text="AI:")
            row.label(text=ai_status)

        # 仅用于调试展示：当前块位置与旋转。
        if sess is not None and sess.game.current is not None:
            row = layout.row(align=True)
            row.label(text="Piece:")
            row.label(text=str(sess.game.current.key))
            row.label(text=f"r:{sess.game.current.rotation}")
            row.label(text=f"p:({sess.game.current.x}, {sess.game.current.z})")

        # Next/Hold 状态
        if sess is not None:
            row = layout.row(align=True)
            row.label(text="Hold:", translate=True)
            hold_key = getattr(sess.game, "hold_key", None)
            row.label(text=hold_key if hold_key else "-", translate=False)

            row = layout.row(align=True)
            row.label(text="Next:", translate=True)
            peek_next = getattr(sess.game, "peek_next", None)
            if callable(peek_next):
                next_keys = peek_next(
                    count=int(getattr(settings, "next_queue_size", 5))
                )
                row.label(
                    text=" ".join(next_keys) if next_keys else "-", translate=False
                )

        layout.separator()

        # -------------------- Controls --------------------
        col = layout.column(align=True)
        col.enabled = not state.running
        col.operator("bltetris.play")

        col = layout.column(align=True)
        col.enabled = state.running
        col.operator("bltetris.pause")

        layout.operator("bltetris.restart")

        layout.separator()

        # -------------------- Panels --------------------
        ai_box = layout.box()
        ai_box.label(text="Auto Play")
        ai_box.prop(settings, "auto_play")

        audio_box = layout.box()
        audio_box.label(text="Audio Settings")
        audio_box.prop(settings, "audio_enabled")

        audio_col = audio_box.column()
        audio_col.enabled = bool(settings.audio_enabled)

        sfx_row = audio_col.row(align=True)
        sfx_row.prop(settings, "sfx_enabled")
        sfx_row.prop(settings, "sfx_volume")

        bgm_row = audio_col.row(align=True)
        bgm_row.prop(settings, "bgm_enabled")
        bgm_row.prop(settings, "bgm_volume")

        layout.separator()

        # -------------------- Advanced --------------------
        adv_box = layout.box()
        adv_row = adv_box.row()
        adv_row.prop(
            settings,
            "ui_show_advanced",
            text="Advanced Settings",
            emboss=False,
            icon="TRIA_DOWN" if bool(settings.ui_show_advanced) else "TRIA_RIGHT",
        )

        if bool(settings.ui_show_advanced):
            adv = adv_box.column()

            # AI
            ai_settings = adv.box()
            ai_settings.label(text="AI", translate=True)
            ai_settings.prop(settings, "ai_ply")

            ply2_col = ai_settings.column()
            ply2_col.enabled = (
                str(getattr(settings, "ai_ply", "PLY1") or "PLY1") == "PLY2"
            )
            ply2_col.prop(settings, "ai_ply2_next_weight")

            ai_settings.prop(settings, "ai_strategy")

            weights_box = ai_settings.box()
            weights_box.enabled = (
                str(getattr(settings, "ai_strategy", "STABLE") or "STABLE") == "CUSTOM"
            )
            weights_box.label(text="Weights (Custom)", translate=True)
            weights_box.prop(settings, "ai_w_lines")
            weights_box.prop(settings, "ai_w_holes")
            weights_box.prop(settings, "ai_w_aggregate_height")
            weights_box.prop(settings, "ai_w_bumpiness")

            ai_settings.separator()
            ai_settings.prop(settings, "ai_action_interval")
            ai_settings.prop(settings, "ai_drop_mode")

            drop_col = ai_settings.column()
            drop_col.enabled = (
                str(getattr(settings, "ai_drop_mode", "HARD") or "HARD") == "SOFT"
            )
            drop_col.prop(settings, "ai_drop_interval")

            ai_settings.separator()
            ai_settings.prop(settings, "ai_show_debug")
            path_row = ai_settings.row()
            path_row.enabled = bool(getattr(settings, "ai_show_debug", True))
            path_row.prop(settings, "ai_show_path")

            # Game Settings
            game_box = adv.box()
            game_box.label(text="Game Settings", translate=True)
            game_box.prop(settings, "board_width")
            game_box.prop(settings, "board_height")
            game_box.prop(settings, "cell_size")
            game_box.prop(settings, "drop_interval")
            game_box.prop(settings, "rotation_system")
            game_box.prop(settings, "seed")
            game_box.prop(settings, "next_queue_size")

            # Input
            feel_box = adv.box()
            feel_box.label(text="Feel / Input", translate=True)
            feel_box.prop(settings, "das_delay")
            feel_box.prop(settings, "arr_interval")
            feel_box.prop(settings, "soft_drop_interval")
            feel_box.prop(settings, "lock_delay")

            # Keybinds
            keybind_box = adv.box()
            keybind_box.label(text="Keybinds", translate=True)
            keybind_box.label(
                text="Click Set, then press a key (RMB to cancel)", translate=True
            )
            keybind_box.enabled = not state.running

            _draw_keybind_row(
                layout=keybind_box,
                settings=settings,
                label="Move Left",
                prefix="kb_move_left",
            )
            _draw_keybind_row(
                layout=keybind_box,
                settings=settings,
                label="Move Right",
                prefix="kb_move_right",
            )
            _draw_keybind_row(
                layout=keybind_box,
                settings=settings,
                label="Soft Drop",
                prefix="kb_soft_drop",
            )

            keybind_box.separator()
            _draw_keybind_row(
                layout=keybind_box,
                settings=settings,
                label="Rotate CW",
                prefix="kb_rotate_cw",
            )
            _draw_keybind_row(
                layout=keybind_box,
                settings=settings,
                label="Rotate CW (Alt)",
                prefix="kb_rotate_cw_alt",
            )
            _draw_keybind_row(
                layout=keybind_box,
                settings=settings,
                label="Rotate CCW",
                prefix="kb_rotate_ccw",
            )
            _draw_keybind_row(
                layout=keybind_box, settings=settings, label="Hold", prefix="kb_hold"
            )
            _draw_keybind_row(
                layout=keybind_box,
                settings=settings,
                label="Hard Drop",
                prefix="kb_hard_drop",
            )
            _draw_keybind_row(
                layout=keybind_box, settings=settings, label="Pause", prefix="kb_pause"
            )

            keybind_box.separator()
            keybind_box.operator(
                "bltetris.reset_keybinds", text="Reset Keybinds", translate=True
            )

            # Scoring
            scoring_box = adv.box()
            scoring_box.label(text="Scoring / Speed", translate=True)
            scoring_box.prop(settings, "lines_per_level")
            scoring_box.prop(settings, "use_level_speed")

            speed_col = scoring_box.column()
            speed_col.enabled = bool(settings.use_level_speed)
            speed_col.prop(settings, "speed_curve")

            if (
                str(getattr(settings, "speed_curve", "MULTIPLIER") or "MULTIPLIER")
                == "MULTIPLIER"
            ):
                speed_col.prop(settings, "level_speed_multiplier")

            speed_col.prop(settings, "min_drop_interval")

            scoring_box.separator()
            scoring_box.prop(settings, "scoring_mode")
            scoring_box.prop(settings, "combo_scoring_mode")

            combo_col = scoring_box.column()
            combo_col.enabled = (
                str(getattr(settings, "combo_scoring_mode", "MULTIPLY") or "MULTIPLY")
                == "MULTIPLY"
            )
            combo_col.prop(settings, "combo_multiplier_step")

            # Audio Files
            audio_files = adv.box()
            audio_files.label(text="Audio Files", translate=True)
            audio_files.enabled = bool(settings.audio_enabled)

            audio_files.label(text="Sound Effects", translate=True)
            audio_files.prop(settings, "sfx_use_custom")
            sfx_file = audio_files.row()
            sfx_file.enabled = bool(settings.sfx_use_custom)
            sfx_file.prop(settings, "sfx_filepath")

            audio_files.prop(settings, "sfx_use_per_event")
            audio_files.prop(settings, "sfx_priority_window")

            audio_files.prop(settings, "sfx_throttle_enabled")
            throttle = audio_files.row()
            throttle.enabled = bool(settings.sfx_throttle_enabled)
            throttle.prop(settings, "sfx_throttle_scale")

            sfx_events = audio_files.box()
            sfx_events.enabled = bool(settings.sfx_use_per_event)
            sfx_events.label(text="Per-Event SFX (optional)", translate=True)
            for event_key in properties._SFX_EVENT_FILE_LABELS.keys():
                sfx_events.prop(settings, f"sfx_event_filepath_{event_key}")

            audio_files.separator()
            audio_files.label(text="Background Music", translate=True)
            audio_files.prop(settings, "bgm_use_custom")
            bgm_file = audio_files.row()
            bgm_file.enabled = bool(settings.bgm_use_custom)
            bgm_file.prop(settings, "bgm_filepath")

            bgm_adv = audio_files.box()
            bgm_adv.enabled = bool(settings.bgm_enabled)
            bgm_adv.label(text="BGM Advanced", translate=True)
            bgm_adv.prop(settings, "bgm_fade_in")
            bgm_adv.prop(settings, "bgm_fade_out")
            bgm_adv.prop(settings, "bgm_pause_duck")
            bgm_adv.prop(settings, "bgm_sync_to_level")

            # Replay
            replay_box = adv.box()
            replay_box.label(text="Recording / Replay")

            recording_on = bool(
                sess is not None and getattr(sess, "recording_active", False)
            )
            recorded_steps = int(
                len(getattr(sess, "recording", [])) if sess is not None else 0
            )

            # 拆分动态字符串以实现汉化
            row = replay_box.row(align=True)
            row.label(text="Recording:")
            row.label(text="ON" if recording_on else "OFF")
            row.label(text="Steps:")
            row.label(text=str(recorded_steps))

            row = replay_box.row(align=True)
            row.operator("bltetris.record_start")
            row.operator("bltetris.record_stop")
            row.operator("bltetris.record_clear")

            replay_box.separator()
            replay_box.prop(settings, "bake_start_frame")
            replay_box.prop(settings, "bake_frame_step")
            replay_box.prop(settings, "bake_replace_existing")

            row = replay_box.row()
            row.enabled = recorded_steps > 0
            row.operator("bltetris.bake_replay")

            replay_box.separator()
            row = replay_box.row(align=True)
            export_col = row.column(align=True)
            export_col.enabled = recorded_steps > 0
            export_col.operator("bltetris.export_replay_json")
            row.column(align=True).operator("bltetris.import_replay_json")

            # Looks
            looks_box = adv.box()
            looks_box.label(text="Looks")
            looks_box.prop(settings, "show_ghost")
            looks_box.prop(settings, "block_scale")

            looks_box.separator()
            border_box = looks_box.box()
            border_box.label(text="Border")
            border_box.prop(settings, "border_color")
            border_box.prop(settings, "border_material")
            border_box.prop(settings, "border_bevel_width")
            border_box.prop(settings, "border_bevel_segments")

            looks_box.separator()
            shared_box = looks_box.box()
            shared_box.label(text="Pieces (Shared)")
            shared_box.prop(settings, "pieces_material")
            shared_box.prop(settings, "pieces_bevel_width")
            shared_box.prop(settings, "pieces_bevel_segments")

            looks_box.separator()
            pieces_box = looks_box.box()
            pieces_box.label(text="Pieces")

            for piece_key in TETROMINO_KEYS:
                box = pieces_box.box()
                override_prop = f"piece_override_style_{piece_key}"
                override = bool(getattr(settings, override_prop, False))

                header = box.row(align=True)
                header.prop(
                    settings,
                    override_prop,
                    text="",
                    emboss=False,
                    icon="TRIA_DOWN" if override else "TRIA_RIGHT",
                )
                header.label(text=f"Piece {piece_key}")
                header.prop(settings, f"color_{piece_key}", text="")

                if override:
                    box.prop(settings, f"material_{piece_key}")
                    box.prop(settings, f"bevel_width_{piece_key}")
                    box.prop(settings, f"bevel_segments_{piece_key}")

            looks_box.separator()
            looks_box.operator("bltetris.apply_looks")

            # Debug
            setup_box = adv.box()
            setup_box.label(text="Setup / Debug")

            op = setup_box.operator("bltetris.setup_assets")
            op.cell_size = float(settings.cell_size)

            op = setup_box.operator("bltetris.setup_geometry_nodes")
            op.cell_size = float(settings.cell_size)

            setup_box.separator()
            setup_box.operator("bltetris.step")

            row = setup_box.row()
            row.enabled = not state.running
            row.operator("bltetris.cleanup")


_classes = (BLTETRIS_PT_main,)


def register() -> None:
    """注册面板。"""

    for cls in _classes:
        bpy.utils.register_class(cls)


def unregister() -> None:
    """卸载面板。"""

    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
