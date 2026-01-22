"""音频系统：基于 Blender 内置 `aud` 的轻量播放。

需求：
- 默认提供“程序化生成”的 SFX/BGM（方便开箱即用，不需要额外资源文件）
- 面板提供两个用户可选文件：
  - `BGM File`：循环播放作为背景音乐
  - `SFX File`：作为统一音效（当前为“单文件复用所有事件”）

设计注意：
- `aud.Sound` 的很多方法参数不是单参（例如 `fadeout(start, length)`），需要按文档调用。
- 这里不做复杂混音/节奏同步，优先保证稳定性与可控性。

事件命名约定（在 operators.py 触发）：
- start/pause
- move/rotate
- soft_drop/hard_drop
- lock/line_clear/game_over
"""

from __future__ import annotations

from dataclasses import dataclass, field
import time

import bpy

try:
    import aud  # type: ignore
except Exception:  # pragma: no cover
    aud = None  # type: ignore


@dataclass
class _AudioContext:
    """全局音频运行时状态。

    这个结构体避免在多个 operator 调用中重复创建 `aud.Device`，同时缓存
    程序化/自定义音频资源，减少频繁加载文件的开销。
    """

    device: object | None = None

    bgm_handle: object | None = None
    bgm_source: str | None = None

    bgm_paused: bool = False
    bgm_last_volume: float = 0.0

    bgm_fade_in: float = 0.4
    bgm_fade_out: float = 0.25

    bgm_anim_from: float = 0.0
    bgm_anim_to: float = 0.0
    bgm_anim_started_at: float = 0.0
    bgm_anim_duration: float = 0.0
    bgm_anim_stop_on_end: bool = False
    bgm_timer_running: bool = False

    custom_bgm_path: str | None = None
    custom_bgm_sound: object | None = None

    custom_sfx_sounds: dict[str, object] = field(default_factory=dict)

    sfx_cache: dict[str, object] = field(default_factory=dict)

    sfx_last_handle: object | None = None
    sfx_last_play_time: float = 0.0
    sfx_last_priority: int = 0
    sfx_last_event_time: dict[str, float] = field(default_factory=dict)


_ctx = _AudioContext()


_SFX_EVENT_PRIORITY: dict[str, int] = {
    "game_over": 100,
    "line_clear": 80,
    "lock": 60,
    "hard_drop": 50,
    "rotate": 40,
    "hold": 35,
    "start": 30,
    "pause": 30,
    "move": 10,
    "soft_drop": 10,
}

_SFX_EVENT_COOLDOWN_SECONDS: dict[str, float] = {
    # High-frequency events
    "move": 0.03,
    "soft_drop": 0.03,
    "rotate": 0.06,
    # Medium
    "hold": 0.08,
    "hard_drop": 0.12,
    "lock": 0.12,
    # Low
    "line_clear": 0.22,
    "start": 0.25,
    "pause": 0.25,
    "game_over": 0.6,
}


def _sfx_priority(event: str) -> int:
    return int(_SFX_EVENT_PRIORITY.get(event, 0))


def _sfx_cooldown(event: str) -> float:
    return float(_SFX_EVENT_COOLDOWN_SECONDS.get(event, 0.0))


def available() -> bool:
    """当前环境是否可用 `aud` 模块。

    Returns:
        True 表示可用，否则 False。
    """

    return aud is not None


def _ensure_device():
    """懒初始化 `aud.Device`。

    Returns:
        创建成功时返回 `aud.Device`；返回 None 表示当前无法创建音频设备（例如系统无音频后端/权限问题）。
    """

    if aud is None:
        return None

    if _ctx.device is None:
        try:
            _ctx.device = aud.Device()
        except Exception:
            _ctx.device = None
            return None

    return _ctx.device


def _abspath(path: str) -> str:
    """将 Blender 路径（支持 // 相对路径）转成绝对路径。

    Args:
        path: Blender 路径（可能包含 `//` 前缀）。

    Returns:
        绝对路径字符串。
    """

    return bpy.path.abspath(path)


def _tone(*, freq: float, dur: float, wave: str = "sine", fade: float = 0.02) -> object:
    """生成一个短音色 `aud.Sound`。

    这是程序化音效的最小构件：通过波形 + 截断 + 淡入淡出避免爆音。

    Args:
        freq: 频率（Hz）。
        dur: 时长（秒）。
        wave: 波形名称（对应 `aud.Sound.<wave>` 工厂方法）。
        fade: 淡入淡出长度（秒）。

    Returns:
        一个可直接传给 `aud.Device.play()` 的 `aud.Sound` 对象。

    Raises:
        RuntimeError: 当前环境不支持 `aud`。
    """
    if aud is None:
        raise RuntimeError("aud not available")

    factory = getattr(aud.Sound, wave, None)
    if factory is None:
        factory = aud.Sound.sine

    freq_v = float(freq)
    if freq_v <= 0.0:
        # Use a silent oscillator for rests.
        base = aud.Sound.sine(0.0)
    else:
        base = factory(freq_v)

    duration = max(0.01, float(dur))

    sound = base.limit(0.0, duration)

    fade_len = max(0.0, min(float(fade), duration / 2.0))
    if fade_len > 0.0:
        sound = sound.fadein(0.0, fade_len)
        sound = sound.fadeout(duration - fade_len, fade_len)

    return sound


_NOTE_NAMES_SHARP: tuple[str, ...] = (
    "C",
    "C#",
    "D",
    "D#",
    "E",
    "F",
    "F#",
    "G",
    "G#",
    "A",
    "A#",
    "B",
)


def _build_note_hz_table() -> dict[str, float]:
    """构建 C0-B8 的音名到频率映射表（Hz）。"""

    table: dict[str, float] = {}
    for octave in range(0, 9):
        for idx, name in enumerate(_NOTE_NAMES_SHARP):
            midi = 12 * (octave + 1) + idx  # C0==12, A4==69
            hz = 440.0 * (2.0 ** ((midi - 69) / 12.0))
            table[f"{name}{octave}"] = hz
    return table


_NOTE_HZ: dict[str, float] = _build_note_hz_table()


def _note_to_hz(note: str | None) -> float:
    """音名（如 C4/F#3）到 Hz；None/REST 表示休止。"""

    if note is None:
        return 0.0

    key = str(note).strip().upper()
    if not key or key in {"R", "REST", "-", "NONE"}:
        return 0.0

    return float(_NOTE_HZ.get(key, 0.0))


def _sequence(*, notes: list[str | None], step: float, wave: str) -> object:
    """将音符序列拼接成 `aud.Sound`（每个音符占用一个 step）。"""

    if aud is None:
        raise RuntimeError("aud not available")

    step_s = max(0.01, float(step))
    # Add a tiny gap between notes (80% duty cycle for the sound)
    # This gives a much better chiptune/staccato feel.
    dur_s = step_s * 0.8
    gap_s = step_s - dur_s
    fade = min(0.01, dur_s / 4.0)

    sound = None
    for note in notes:
        hz = _note_to_hz(note)
        if hz <= 0.0:
            seg = _tone(freq=0.0, dur=step_s, wave="sine", fade=0.0)
        else:
            seg = _tone(freq=hz, dur=dur_s, wave=wave, fade=fade)
            # Append silence gap
            seg = seg.join(_tone(freq=0.0, dur=gap_s, wave="sine", fade=0.0))
        
        sound = seg if sound is None else sound.join(seg)

    return sound if sound is not None else _tone(freq=0.0, dur=step_s, wave="sine", fade=0.0)


# Section A: Fast melody (Measures 1-8 in sheet)
_SECTION_A_MELODY: list[str | None] = [
    # Bar 1 & 5
    "E5", "E5", "B4", "C5", "D5", "D5", "C5", "B4",
    # Bar 2 & 6
    "A4", "A4", "A4", "C5", "E5", "E5", "D5", "C5",
    # Bar 3 & 7
    "B4", "B4", "B4", "C5", "D5", "D5", "E5", "E5",
    # Bar 4 & 8
    "C5", "C5", "A4", "A4", "A4", "A4", "-", "-",
]

# Section B: Slower melody (Measures 9-16 in sheet)
_SECTION_B_MELODY: list[str | None] = [
    # Bar 9
    "D5", "D5", "D5", "D5", "F5", "F5", "A5", "A5",
    # Bar 10
    "G5", "G5", "G5", "F5", "E5", "E5", "E5", "E5",
    # Bar 11
    "C5", "C5", "C5", "C5", "E5", "E5", "D5", "D5",
    # Bar 12
    "C5", "C5", "C5", "B4", "A4", "A4", "A4", "A4",
    # Bar 13
    "D5", "D5", "D5", "D5", "F5", "F5", "A5", "A5",
    # Bar 14
    "G5", "G5", "G5", "F5", "E5", "E5", "E5", "E5",
    # Bar 15
    "E5", "E5", "C5", "C5", "E5", "E5", "D5", "D5",
    # Bar 16
    "C5", "C5", "B4", "B4", "A4", "A4", "A4", "A4",
]

# Bass Section A (Am - E7 - Am - E7 - Dm - Am - E7 - Am)
_SECTION_A_BASS: list[str | None] = [
    "A2", "E3", "A2", "E3", "E2", "B2", "E2", "B2",
    "A2", "E3", "A2", "E3", "E2", "B2", "E2", "B2",
    "D2", "A2", "D3", "A2", "A2", "E3", "A2", "E3",
    "E2", "B2", "E2", "B2", "A2", "E3", "A2", "E3",
]

# Bass Section B (Dm - Dm - Am - Am - E7 - E7 - Am - Am)
_SECTION_B_BASS: list[str | None] = [
    "D2", "A2", "D3", "A2", "D2", "A2", "D3", "A2",
    "A2", "E3", "A2", "E3", "A2", "E3", "A2", "E3",
    "E2", "B2", "E2", "B2", "E2", "B2", "E2", "B2",
    "A2", "E3", "A2", "E3", "A2", "E3", "A2", "E3",
]

# Combined BGM: (A*2) -> (B*2) -> Repeat
_TYPE_A_MELODY: list[str | None] = (_SECTION_A_MELODY * 2) + (_SECTION_B_MELODY * 2)
_TYPE_A_BASS: list[str | None] = (_SECTION_A_BASS * 2) + (_SECTION_B_BASS * 2)


def _procedural_sfx(event: str) -> object:
    """为事件生成（并缓存）程序化音效。

    Args:
        event: 事件名（如 move/rotate/lock/line_clear 等）。

    Returns:
        对应事件的 `aud.Sound`。

    Raises:
        RuntimeError: 当前环境不支持 `aud`。
    """

    if aud is None:
        raise RuntimeError("aud not available")

    key = f"proc:{event}"
    cached = _ctx.sfx_cache.get(key)
    if cached is not None:
        return cached

    if event == "move":
        sound = _tone(freq=880.0, dur=0.045, wave="square")
    elif event == "rotate":
        sound = _tone(freq=660.0, dur=0.06, wave="triangle")
    elif event == "hold":
        sound = _tone(freq=523.25, dur=0.07, wave="triangle")
    elif event == "soft_drop":
        sound = _tone(freq=440.0, dur=0.05, wave="sine")
    elif event == "hard_drop":
        sound = _tone(freq=110.0, dur=0.09, wave="square")
    elif event == "lock":
        sound = _tone(freq=165.0, dur=0.08, wave="square")
    elif event == "line_clear":
        a = _tone(freq=880.0, dur=0.12, wave="sine")
        b = _tone(freq=660.0, dur=0.12, wave="sine")
        sound = a.mix(b)
    elif event == "game_over":
        a = _tone(freq=220.0, dur=0.14, wave="square")
        b = _tone(freq=165.0, dur=0.14, wave="square")
        c = _tone(freq=110.0, dur=0.22, wave="square")
        sound = a.join(b).join(c)
    elif event == "start":
        a = _tone(freq=660.0, dur=0.06, wave="triangle")
        b = _tone(freq=990.0, dur=0.08, wave="triangle")
        sound = a.join(b)
    elif event == "pause":
        sound = _tone(freq=330.0, dur=0.08, wave="triangle")
    else:
        sound = _tone(freq=600.0, dur=0.05, wave="sine")

    _ctx.sfx_cache[key] = sound
    return sound


def _sequence_with_durations(*, notes: list[tuple[str | None, int]], step: float, wave: str) -> object:
    """将 (音符, 时值) 序列拼接成 `aud.Sound`。"""

    if aud is None:
        raise RuntimeError("aud not available")

    step_s = max(0.01, float(step))
    
    sound = None
    for note, beats in notes:
        total_dur = step_s * beats
        # 保持断奏感：实际发声占 85%
        active_dur = total_dur * 0.85
        gap_dur = total_dur - active_dur
        
        fade = min(0.01, active_dur / 4.0)
        hz = _note_to_hz(note)
        
        if hz <= 0.0:
            seg = _tone(freq=0.0, dur=total_dur, wave="sine", fade=0.0)
        else:
            seg = _tone(freq=hz, dur=active_dur, wave=wave, fade=fade)
            seg = seg.join(_tone(freq=0.0, dur=gap_dur, wave="sine", fade=0.0))
        
        sound = seg if sound is None else sound.join(seg)

    return sound if sound is not None else _tone(freq=0.0, dur=step_s, wave="sine", fade=0.0)


def _procedural_bgm(*, bpm: float = 150.0) -> object:
    """生成符合 Retro 规格的 BGM (Type-A Korobeiniki)。
    
    结构：(Section A * 2) + (Section B * 2)
    """

    if aud is None:
        raise RuntimeError("aud not available")

    bpm_i = int(round(float(bpm)))
    bpm_i = max(60, min(240, bpm_i))

    key = f"proc:bgm:retro_v4:{bpm_i}"
    cached = _ctx.sfx_cache.get(key)
    if cached is not None:
        return cached

    step = 60.0 / float(bpm_i) * 0.5  # 八分音符基础单位 (1 beat)

    # Section A: 32 beats
    melody_a = [
        ('E5', 2), ('B4', 1), ('C5', 1), ('D5', 2), ('C5', 1), ('B4', 1),
        ('A4', 2), ('A4', 1), ('C5', 1), ('E5', 2), ('D5', 1), ('C5', 1),
        ('B4', 3), ('C5', 1), ('D5', 2), ('E5', 2),
        ('C5', 2), ('A4', 2), ('A4', 2), ('REST', 2),
    ]
    # Bass A: 28 beats -> pad to 32
    bass_a = [
        ('E4', 1), ('E4', 1), ('E4', 1), ('E4', 1), ('A3', 1), ('A3', 1), ('A3', 1), ('A3', 1),
        ('G#3', 1), ('G#3', 1), ('E4', 1), ('E4', 1), ('A3', 1), ('A3', 1), ('A3', 1), ('B3', 1), 
        ('C4', 1), ('C4', 1), ('D4', 1), ('D4', 1), ('E4', 1), ('E4', 1), ('C4', 1), ('A3', 1), 
        ('A3', 2), ('G#3', 2), ('REST', 4)
    ]

    # Section B: 32 beats
    melody_b = [
        ('D5', 2), ('F5', 1), ('A5', 2), ('G5', 1), ('F5', 1),
        ('E5', 3), ('C5', 1), ('E5', 2), ('D5', 1), ('C5', 1),
        ('B4', 2), ('B4', 1), ('C5', 1), ('D5', 2), ('E5', 2),
        ('C5', 2), ('A4', 2), ('A4', 2), ('REST', 2),
    ]
    # Bass B: 20 beats -> pad to 32
    bass_b = [
        ('D4', 1), ('D4', 1), ('D4', 1), ('D4', 1), ('C4', 1), ('C4', 1), ('C4', 1), ('C4', 1),
        ('G3', 1), ('G3', 1), ('B3', 1), ('B3', 1), ('E4', 1), ('E4', 1), ('C4', 1), ('A3', 1),
        ('A3', 2), ('REST', 2), ('REST', 12)
    ]

    # Combine to (A*2) + (B*2)
    melody_score = (melody_a * 2) + (melody_b * 2)
    bass_score = (bass_a * 2) + (bass_b * 2)

    melody = _sequence_with_durations(notes=melody_score, step=step, wave="square")
    bass = _sequence_with_durations(notes=bass_score, step=step, wave="triangle")

    # Triangle is naturally quieter, mix them
    sound = melody.mix(bass)
    sound = sound.loop(-1)

    _ctx.sfx_cache[key] = sound
    return sound


def _load_sound_file(path: str) -> object | None:
    """尝试从文件加载音频资源。

    Args:
        path: 音频文件路径（支持 Blender 的 `//` 相对路径）。

    Returns:
        加载成功返回 `aud.Sound`，失败返回 None。
    """

    if aud is None:
        return None

    try:
        return aud.Sound.file(_abspath(path))
    except Exception:
        return None


def _load_sfx_file_cached(path: str) -> object | None:
    """从文件加载 SFX（带全局缓存）。"""

    if aud is None:
        return None

    abs_path = _abspath(path)
    cached = _ctx.custom_sfx_sounds.get(abs_path)
    if cached is not None:
        return cached

    sound = _load_sound_file(path)
    if sound is None:
        return None

    _ctx.custom_sfx_sounds[abs_path] = sound
    return sound


_BGM_TIMER_INTERVAL = 0.02


def _set_handle_volume(handle: object, volume: float) -> None:
    try:
        handle.volume = float(volume)
    except Exception:
        pass


def _bgm_volume_timer() -> float | None:
    handle = _ctx.bgm_handle
    if handle is None:
        _ctx.bgm_timer_running = False
        return None

    now = time.monotonic()
    duration = float(_ctx.bgm_anim_duration)

    if duration <= 0.0:
        target = float(_ctx.bgm_anim_to)
        _set_handle_volume(handle, target)
        _ctx.bgm_last_volume = target

        if _ctx.bgm_anim_stop_on_end and target <= 0.0:
            try:
                handle.stop()
            except Exception:
                pass
            _ctx.bgm_handle = None
            _ctx.bgm_source = None
            _ctx.bgm_last_volume = 0.0

        _ctx.bgm_anim_stop_on_end = False
        _ctx.bgm_anim_duration = 0.0
        _ctx.bgm_timer_running = False
        return None

    t = (now - float(_ctx.bgm_anim_started_at)) / duration
    if t >= 1.0:
        target = float(_ctx.bgm_anim_to)
        _set_handle_volume(handle, target)
        _ctx.bgm_last_volume = target

        if _ctx.bgm_anim_stop_on_end and target <= 0.0:
            try:
                handle.stop()
            except Exception:
                pass
            _ctx.bgm_handle = None
            _ctx.bgm_source = None
            _ctx.bgm_last_volume = 0.0

        _ctx.bgm_anim_stop_on_end = False
        _ctx.bgm_anim_duration = 0.0
        _ctx.bgm_timer_running = False
        return None

    from_v = float(_ctx.bgm_anim_from)
    to_v = float(_ctx.bgm_anim_to)
    v = from_v + (to_v - from_v) * max(0.0, min(1.0, t))

    _set_handle_volume(handle, v)
    _ctx.bgm_last_volume = v
    return _BGM_TIMER_INTERVAL


def _start_bgm_volume_animation(*, to: float, duration: float, stop_on_end: bool = False) -> None:
    handle = _ctx.bgm_handle
    if handle is None:
        return

    _ctx.bgm_anim_from = float(_ctx.bgm_last_volume)
    _ctx.bgm_anim_to = float(to)
    _ctx.bgm_anim_started_at = time.monotonic()
    _ctx.bgm_anim_duration = max(0.0, float(duration))
    _ctx.bgm_anim_stop_on_end = bool(stop_on_end)

    if not _ctx.bgm_timer_running:
        try:
            bpy.app.timers.register(_bgm_volume_timer, first_interval=0.0)
            _ctx.bgm_timer_running = True
        except Exception:
            _ctx.bgm_timer_running = False


def stop_bgm(*, immediate: bool = False) -> None:
    """停止正在播放的 BGM（支持淡出）。"""

    handle = _ctx.bgm_handle
    if handle is None:
        _ctx.bgm_handle = None
        _ctx.bgm_source = None
        _ctx.bgm_last_volume = 0.0
        return

    if immediate or float(_ctx.bgm_fade_out) <= 0.0:
        try:
            handle.stop()
        except Exception:
            pass
        _ctx.bgm_handle = None
        _ctx.bgm_source = None
        _ctx.bgm_last_volume = 0.0
        return

    _start_bgm_volume_animation(to=0.0, duration=float(_ctx.bgm_fade_out), stop_on_end=True)


def stop_all() -> None:
    """停止所有持续音频（BGM + 最近一条 SFX）。"""

    stop_bgm(immediate=True)

    handle = _ctx.sfx_last_handle
    if handle is not None:
        try:
            handle.stop()
        except Exception:
            pass
    _ctx.sfx_last_handle = None


def start_bgm(*, settings, level: int | None = None, paused: bool | None = None) -> None:
    """确保 BGM 在播放，并根据设置更新音量/来源（支持淡入淡出）。

    Args:
        settings: `TetrisSettings`（或具备同名字段的对象）。
        level: 当前等级（用于后续节奏同步；自定义文件 BGM 不受影响）。
        paused: 是否处于暂停态（暂停时会应用 bgm_pause_duck 音量倍率）。
    """

    if aud is None:
        return

    _ctx.bgm_fade_in = float(getattr(settings, "bgm_fade_in", 0.4) or 0.4)
    _ctx.bgm_fade_out = float(getattr(settings, "bgm_fade_out", 0.25) or 0.25)

    paused_before = bool(_ctx.bgm_paused)
    if paused is not None:
        _ctx.bgm_paused = bool(paused)
    paused_changed = paused_before != bool(_ctx.bgm_paused)

    if not bool(getattr(settings, "audio_enabled", True)):
        stop_bgm()
        return

    if not bool(getattr(settings, "bgm_enabled", True)):
        stop_bgm()
        return

    device = _ensure_device()
    if device is None:
        return

    use_custom = bool(getattr(settings, "bgm_use_custom", False))
    path = str(getattr(settings, "bgm_filepath", "") or "").strip()

    source = "proc"
    sound = None

    if use_custom and path:
        abs_path = _abspath(path)
        if _ctx.custom_bgm_sound is None or _ctx.custom_bgm_path != abs_path:
            loaded = _load_sound_file(path)
            _ctx.custom_bgm_path = abs_path
            _ctx.custom_bgm_sound = loaded.loop(-1) if loaded is not None else None

        sound = _ctx.custom_bgm_sound
        if sound is not None:
            source = f"file:{abs_path}"

    if sound is None:
        bpm = 160.0
        if bool(getattr(settings, "bgm_sync_to_level", True)) and level is not None:
            lvl = max(1, int(level))
            bpm = 160.0 + float(min(lvl, 20) - 1) * 4.0

        bpm_key = int(round(bpm / 5.0)) * 5
        source = f"proc:type_a:{bpm_key}"
        sound = _procedural_bgm(bpm=float(bpm_key))

    target_volume = float(getattr(settings, "bgm_volume", 0.2) or 0.2)
    if bool(_ctx.bgm_paused):
        target_volume *= float(getattr(settings, "bgm_pause_duck", 0.25) or 0.25)

    # Same source: keep playing and update volume (paused/volume slider changes).
    if _ctx.bgm_handle is not None and _ctx.bgm_source == source:
        if paused_changed:
            duration = float(_ctx.bgm_fade_in if target_volume >= float(_ctx.bgm_last_volume) else _ctx.bgm_fade_out)
            _start_bgm_volume_animation(to=target_volume, duration=duration, stop_on_end=False)
            return

        if float(_ctx.bgm_anim_duration) > 0.0:
            return

        _set_handle_volume(_ctx.bgm_handle, target_volume)
        _ctx.bgm_last_volume = target_volume
        return

    # Switching sources: stop immediately to avoid overlap.
    stop_bgm(immediate=True)

    try:
        handle = device.play(sound)
    except Exception:
        return

    _ctx.bgm_handle = handle
    _ctx.bgm_source = source
    _ctx.bgm_last_volume = 0.0
    _set_handle_volume(handle, 0.0)

    if float(_ctx.bgm_fade_in) > 0.0:
        _start_bgm_volume_animation(to=target_volume, duration=float(_ctx.bgm_fade_in), stop_on_end=False)
    else:
        _set_handle_volume(handle, target_volume)
        _ctx.bgm_last_volume = target_volume


def set_bgm_paused(*, paused: bool, settings) -> None:
    """设置 BGM 暂停态（通过淡入/淡出做音量 Duck）。"""

    if aud is None:
        return

    _ctx.bgm_fade_in = float(getattr(settings, "bgm_fade_in", 0.4) or 0.4)
    _ctx.bgm_fade_out = float(getattr(settings, "bgm_fade_out", 0.25) or 0.25)

    _ctx.bgm_paused = bool(paused)

    handle = _ctx.bgm_handle
    if handle is None:
        return

    target_volume = float(getattr(settings, "bgm_volume", 0.2) or 0.2)
    if bool(_ctx.bgm_paused):
        target_volume *= float(getattr(settings, "bgm_pause_duck", 0.25) or 0.25)

    duration = float(_ctx.bgm_fade_in if target_volume >= float(_ctx.bgm_last_volume) else _ctx.bgm_fade_out)
    _start_bgm_volume_animation(to=target_volume, duration=duration, stop_on_end=False)


def play_sfx(*, event: str, settings) -> None:
    """播放一次性音效。

    优先级/来源规则：
    1) 若启用 Per-Event SFX 且该事件设置了文件，则优先使用该文件
    2) 否则若启用全局 Custom SFX，则使用全局文件
    3) 否则回退到程序化音效

    同时做一层“优先级节流”：在 `sfx_priority_window` 时间窗内，低优先级事件会被跳过，
    更高优先级事件会尝试停止上一条音效以避免叠加过密。

    Args:
        event: 事件名（如 move/rotate/lock/line_clear 等）。
        settings: `TetrisSettings`（或具备同名字段的对象）。
    """

    if aud is None:
        return

    if not bool(getattr(settings, "audio_enabled", True)):
        return

    if not bool(getattr(settings, "sfx_enabled", True)):
        return

    device = _ensure_device()
    if device is None:
        return

    volume = float(getattr(settings, "sfx_volume", 0.35))

    sound = None

    if bool(getattr(settings, "sfx_use_per_event", False)):
        event_path = str(getattr(settings, f"sfx_event_filepath_{event}", "") or "").strip()
        if event_path:
            sound = _load_sfx_file_cached(event_path)

    if sound is None:
        use_custom = bool(getattr(settings, "sfx_use_custom", False))
        path = str(getattr(settings, "sfx_filepath", "") or "").strip()
        if use_custom and path:
            sound = _load_sfx_file_cached(path)

    if sound is None:
        sound = _procedural_sfx(event)

    now = time.monotonic()

    if bool(getattr(settings, "sfx_throttle_enabled", True)):
        scale = float(getattr(settings, "sfx_throttle_scale", 1.0) or 1.0)
        min_interval = _sfx_cooldown(event) * max(0.0, scale)
        if min_interval > 0.0:
            last_event_time = float(_ctx.sfx_last_event_time.get(event, 0.0))
            if (now - last_event_time) < min_interval:
                return

    priority_window = float(getattr(settings, "sfx_priority_window", 0.06) or 0.06)
    priority = _sfx_priority(event)

    if priority_window > 0.0 and (now - float(_ctx.sfx_last_play_time)) < priority_window:
        if priority <= int(_ctx.sfx_last_priority):
            return

        prev = _ctx.sfx_last_handle
        if prev is not None:
            try:
                prev.stop()
            except Exception:
                pass

    try:
        handle = device.play(sound)
    except Exception:
        return

    try:
        handle.volume = volume
    except Exception:
        pass

    _ctx.sfx_last_handle = handle
    _ctx.sfx_last_play_time = now
    _ctx.sfx_last_priority = priority
    _ctx.sfx_last_event_time[event] = now
