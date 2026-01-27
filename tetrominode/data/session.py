"""全局会话与录制缓冲。

- `TetrisSession`：当前正在运行的游戏会话（单例）
- `RecordedState`：一次“状态变化”的快照（用于录制与后续烘焙回放）

录制策略：
- 只在 `recording_active=True` 时写入
- 对快照做签名去重，避免重复记录造成数据膨胀

注意：
- 这里使用模块级 `_session` 作为单例存储，属于“开发态/快速迭代”方案。
- 如果未来要支持多窗口/多场景并行，需要把 session 迁移到 WindowManager/Scene 自定义属性中。
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import time
from typing import Any

from .constants import BOARD_HEIGHT_DEFAULT, BOARD_WIDTH_DEFAULT, CELL_SIZE_DEFAULT
from ..core.game import TetrisGame


# 默认下落间隔（秒），越小越快。
DEFAULT_DROP_INTERVAL = 0.6

# modal 的 timer 步进间隔（秒）。这个值越小，响应越细；但会更频繁触发 TIMER。
TIMER_STEP_SECONDS = 0.05

# Replay JSON（导出/导入）格式版本。
REPLAY_JSON_SCHEMA = "tetrominode.replay"
REPLAY_JSON_VERSION = 2


@dataclass(frozen=True)
class RecordedState:
    """一次录制快照（用于回放烘焙）。

    Attributes:
        occupied: 已落地棋盘占用。key 是 `(x,z)`，value 是 piece key（I/O/T/...）。
        current: 当前块状态，格式为 `(key, rotation, x, z)`；如果没有当前块则为 None。
        game_over: 该步是否已 game over。
        reason: 记录原因（用于调试：manual_move / ai_drop / gravity_tick 等）。
    """

    occupied: dict[tuple[int, int], str]
    current: tuple[str, int, int, int] | None
    game_over: bool
    reason: str


@dataclass
class TetrisSession:
    """运行会话（单例）。

    Fields:
        game: 纯逻辑 `TetrisGame` 实例。
        cell_size: 当前格子大小（用于把 cell 转换成点云坐标）。
        drop_interval: 重力下落间隔（秒）。
        last_drop_time: 上次重力 tick 的时间戳（monotonic）。
        running: modal 是否正在运行。
        pause_requested: 是否请求暂停（由 operator 设置，由 modal 消费）。

        recording_active: 是否开启录制。
        recording: RecordedState 列表（去重后的状态变化序列）。
        last_record_signature: 上一次录制快照的签名，用于去重。
    """

    game: TetrisGame
    cell_size: float
    drop_interval: float
    last_drop_time: float
    running: bool
    pause_requested: bool

    recording_active: bool
    recording: list[RecordedState]
    last_record_signature: tuple | None


_session: TetrisSession | None = None


def get_session() -> TetrisSession | None:
    """获取当前 session。

    Returns:
        如果尚未创建 session，返回 None；否则返回当前的 `TetrisSession`。
    """

    return _session


def create_session(
    *,
    width: int = BOARD_WIDTH_DEFAULT,
    height: int = BOARD_HEIGHT_DEFAULT,
    cell_size: float = CELL_SIZE_DEFAULT,
    seed: int | None = None,
    drop_interval: float = DEFAULT_DROP_INTERVAL,
    next_queue_size: int = 5,
    lines_per_level: int = 10,
    rotation_system: str = "SRS",
) -> TetrisSession:
    """创建一个新的 session（覆盖旧 session）。

    Args:
        width: 棋盘宽。
        height: 棋盘高。
        cell_size: 格子大小。
        seed: 随机种子（None 表示随机）。
        drop_interval: 下落间隔。
        next_queue_size: Next 预览队列长度。
        lines_per_level: 每升一级需要的累计消行数。
        rotation_system: 旋转系统（SRS 或 SIMPLE）。

    Returns:
        新创建的 `TetrisSession`。
    """

    global _session

    game = TetrisGame(
        width=int(width),
        height=int(height),
        seed=seed,
        next_queue_size=int(next_queue_size),
        lines_per_level=int(lines_per_level),
        rotation_system=rotation_system,
    )

    # 创建 session 时立刻生成一个当前块，保证 UI/运行时有内容。
    game.spawn_piece()

    _session = TetrisSession(
        game=game,
        cell_size=float(cell_size),
        drop_interval=float(drop_interval),
        last_drop_time=time.monotonic(),
        running=False,
        pause_requested=False,
        recording_active=False,
        recording=[],
        last_record_signature=None,
    )

    return _session


def ensure_session(
    *,
    width: int = BOARD_WIDTH_DEFAULT,
    height: int = BOARD_HEIGHT_DEFAULT,
    cell_size: float = CELL_SIZE_DEFAULT,
    seed: int | None = None,
    drop_interval: float = DEFAULT_DROP_INTERVAL,
    next_queue_size: int = 5,
    lines_per_level: int = 10,
    rotation_system: str = "SRS",
) -> TetrisSession:
    """确保 session 存在；若不存在则创建。

    与 `create_session` 的区别：
    - 已存在时不会重置棋盘，只会更新参数。

    Returns:
        当前 session。
    """

    global _session

    if _session is None:
        return create_session(
            width=width,
            height=height,
            cell_size=cell_size,
            seed=seed,
            drop_interval=drop_interval,
            next_queue_size=next_queue_size,
            lines_per_level=lines_per_level,
            rotation_system=rotation_system,
        )

    # 动态更新可变参数（不会重置棋盘）。
    _session.cell_size = float(cell_size)
    _session.drop_interval = float(drop_interval)
    _session.game.set_next_queue_size(int(next_queue_size))
    _session.game.set_lines_per_level(int(lines_per_level))
    _session.game.rotation_system = str(rotation_system).upper()

    return _session


def restart_session(
    *,
    seed: int | None = None,
    next_queue_size: int = 5,
    lines_per_level: int = 10,
    rotation_system: str = "SRS",
) -> TetrisSession:
    """重开：清空棋盘并重新生成一个当前块。

    Args:
        seed: 可选的随机种子（None 表示随机）。
        next_queue_size: Next 预览队列长度。
        lines_per_level: 每升一级需要的累计消行数。
        rotation_system: 旋转系统（SRS 或 SIMPLE）。

    Returns:
        当前 session。
    """

    sess = ensure_session(
        seed=seed,
        next_queue_size=next_queue_size,
        lines_per_level=lines_per_level,
        rotation_system=rotation_system,
    )

    sess.game.reset()
    sess.game.rng.seed(seed)
    sess.game.spawn_piece()

    sess.last_drop_time = time.monotonic()
    sess.pause_requested = False

    # 若正在录制，把“重开”也写入回放序列。
    if sess.recording_active:
        record_snapshot(sess, reason="restart")

    return sess


def capture_snapshot(game: TetrisGame, *, reason: str) -> RecordedState:
    """从 `TetrisGame` 抽取一个可序列化的快照。

    Args:
        game: 当前 game。
        reason: 记录原因（用于调试）。

    Returns:
        `RecordedState`。
    """

    occupied_raw = getattr(game, "occupied", {})
    occupied: dict[tuple[int, int], str] = {}

    # 理论上 occupied 是 dict[(x,z)] -> key。
    # 这里做兼容：若未来结构变更/历史数据使用 set，也不会直接崩。
    if isinstance(occupied_raw, dict):
        for (x, z), key in occupied_raw.items():
            occupied[(int(x), int(z))] = str(key)
    else:
        for x, z in list(occupied_raw):
            occupied[(int(x), int(z))] = "I"  # legacy fallback

    current_piece = getattr(game, "current", None)
    current: tuple[str, int, int, int] | None = None
    if current_piece is not None:
        current = (
            str(getattr(current_piece, "key")),
            int(getattr(current_piece, "rotation")),
            int(getattr(current_piece, "x")),
            int(getattr(current_piece, "z")),
        )

    game_over = bool(getattr(game, "game_over", False))

    return RecordedState(
        occupied=occupied,
        current=current,
        game_over=game_over,
        reason=str(reason),
    )


def start_recording(sess: TetrisSession) -> None:
    """开始录制（清空旧录制并立刻记录一个起始快照）。

    Args:
        sess: 目标会话。

    Side effects:
        - 会将 `sess.recording_active` 设为 True
        - 会清空 `sess.recording` 并写入首帧快照
    """

    sess.recording_active = True
    sess.recording.clear()
    sess.last_record_signature = None
    record_snapshot(sess, reason="start")


def stop_recording(sess: TetrisSession) -> None:
    """停止录制（保留已录制数据）。

    Args:
        sess: 目标会话。

    Side effects:
        - 会将 `sess.recording_active` 设为 False
    """

    sess.recording_active = False


def clear_recording(sess: TetrisSession) -> None:
    """清空录制缓存。

    Args:
        sess: 目标会话。

    Side effects:
        - 会清空 `sess.recording` 并重置去重签名
    """

    sess.recording.clear()
    sess.last_record_signature = None


def record_snapshot(sess: TetrisSession, *, reason: str) -> None:
    """根据当前 game 状态追加一个录制快照（带去重）。

    去重策略：
    - 生成 signature：occupied + current + game_over
    - 如果与上一次签名一致，则跳过写入

    Args:
        sess: 当前 session。
        reason: 记录原因（会写进 RecordedState）。
    """

    if not sess.recording_active:
        return

    state = capture_snapshot(sess.game, reason=reason)

    signature = (
        # occupied items 做排序，保证 dict 顺序不影响签名。
        tuple(sorted(state.occupied.items())),
        state.current,
        state.game_over,
    )

    # 连续重复状态不写入，避免“按住键”造成爆炸增长。
    if sess.last_record_signature == signature:
        return

    sess.recording.append(state)
    sess.last_record_signature = signature


def request_pause() -> None:
    """请求暂停：modal 会在下一次循环中消费并退出。"""

    sess = get_session()
    if sess is None or not sess.running:
        return
    sess.pause_requested = True


def clear_pause_request() -> None:
    """清除暂停请求（通常在开始/恢复时调用）。"""

    sess = get_session()
    if sess is None:
        return
    sess.pause_requested = False


def consume_pause_request() -> bool:
    """消费一次暂停请求。

    Returns:
        如果之前有 pause 请求则返回 True，并清除该请求；否则 False。
    """

    sess = get_session()
    if sess is None or not sess.pause_requested:
        return False
    sess.pause_requested = False
    return True


def clear_session() -> None:
    """清空全局 session（用于 Cleanup）。"""

    global _session
    _session = None


def recording_to_replay_json(
    recording: list[RecordedState],
    *,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """将录制序列转换为可写入 JSON 的 dict。

    JSON 结构（v2 - 差分压缩）：
    {
        "schema": "tetrominode.replay",
        "version": 2,
        "meta": {...},
        "steps": [
            {
                "add": [[x,z,key], ...],
                "rem": [[x,z], ...],
                "cur": [key,rot,x,z] | null,
                "over": bool,
                "reason": str
            },
            ...
        ],
    }

    Args:
        recording: `RecordedState` 序列。
        meta: 可选元信息。

    Returns:
        可直接 `json.dump` 的 dict。
    """

    meta_out: dict[str, Any] = dict(meta or {})
    steps: list[dict[str, Any]] = []

    prev_occupied: dict[tuple[int, int], str] = {}

    for state in recording:
        current_occupied = state.occupied

        # 计算新增/修改的格子
        added = []
        for coords, key in current_occupied.items():
            if coords not in prev_occupied or prev_occupied[coords] != key:
                added.append([int(coords[0]), int(coords[1]), str(key)])

        # 计算删除的格子
        removed = []
        for coords in prev_occupied:
            if coords not in current_occupied:
                removed.append([int(coords[0]), int(coords[1])])

        step_data: dict[str, Any] = {
            "cur": list(state.current) if state.current is not None else None,
            "over": bool(state.game_over),
            "reason": str(state.reason),
        }

        if added:
            step_data["add"] = added
        if removed:
            step_data["rem"] = removed

        steps.append(step_data)
        prev_occupied = current_occupied

    return {
        "schema": REPLAY_JSON_SCHEMA,
        "version": REPLAY_JSON_VERSION,
        "meta": meta_out,
        "steps": steps,
    }


def _parse_xy_key(key: str) -> tuple[int, int] | None:
    text = str(key).strip()
    if text.startswith("(") and text.endswith(")"):
        text = text[1:-1].strip()

    for sep in (",", "|", " "):
        if sep not in text:
            continue
        parts = [p.strip() for p in text.split(sep) if p.strip()]
        if len(parts) != 2:
            continue
        try:
            return int(parts[0]), int(parts[1])
        except Exception:
            return None

    return None


def _parse_occupied(raw: Any) -> dict[tuple[int, int], str]:
    occupied: dict[tuple[int, int], str] = {}

    if raw is None:
        return occupied

    # v1: list[[x,z,key], ...]
    if isinstance(raw, list):
        for item in raw:
            x = z = None
            key = None

            if isinstance(item, dict):
                x = item.get("x")
                z = item.get("z")
                key = item.get("key") if "key" in item else item.get("piece")
            elif isinstance(item, (list, tuple)):
                if len(item) >= 3:
                    x, z, key = item[0], item[1], item[2]
                elif (
                    len(item) == 2
                    and isinstance(item[0], (list, tuple))
                    and len(item[0]) == 2
                ):
                    (x, z), key = item

            if x is None or z is None or key is None:
                continue

            try:
                occupied[(int(x), int(z))] = str(key)
            except Exception as exc:
                raise ValueError(
                    "Replay JSON occupied contains invalid coordinates"
                ) from exc

        return occupied

    # Legacy: dict{"x,z": key}
    if isinstance(raw, dict):
        for k, v in raw.items():
            if not isinstance(k, str):
                continue
            coords = _parse_xy_key(k)
            if coords is None:
                continue
            occupied[(int(coords[0]), int(coords[1]))] = str(v)
        return occupied

    raise ValueError("Replay JSON occupied must be a list or dict")


def _parse_current(raw: Any) -> tuple[str, int, int, int] | None:
    if raw is None:
        return None

    if isinstance(raw, (list, tuple)) and len(raw) >= 4:
        key, rotation, x, z = raw[0], raw[1], raw[2], raw[3]
        return (str(key), int(rotation), int(x), int(z))

    if isinstance(raw, dict):
        key = (
            raw.get("key")
            if "key" in raw
            else (raw.get("piece") if "piece" in raw else raw.get("type"))
        )
        rotation = raw.get("rotation") if "rotation" in raw else raw.get("rot")
        x = raw.get("x")
        z = raw.get("z") if "z" in raw else raw.get("y")

        if key is None or x is None or z is None:
            raise ValueError("Replay JSON current dict missing fields")

        return (str(key), int(rotation or 0), int(x), int(z))

    raise ValueError("Replay JSON current must be null, list, or dict")


def _parse_step(raw: Any) -> RecordedState:
    # v1: dict
    if isinstance(raw, dict):
        occupied = _parse_occupied(raw.get("occupied"))
        current = _parse_current(raw.get("current"))
        game_over = bool(raw.get("game_over", False))
        reason = str(raw.get("reason", "imported"))
        return RecordedState(
            occupied=occupied, current=current, game_over=game_over, reason=reason
        )

    # Legacy: [occupied, current, game_over, reason?]
    if isinstance(raw, (list, tuple)) and len(raw) >= 3:
        occupied = _parse_occupied(raw[0])
        current = _parse_current(raw[1])
        game_over = bool(raw[2])
        reason = str(raw[3]) if len(raw) > 3 else "imported"
        return RecordedState(
            occupied=occupied, current=current, game_over=game_over, reason=reason
        )

    raise ValueError("Replay JSON step must be an object or list")


def parse_replay_json(payload: Any) -> tuple[list[RecordedState], dict[str, Any], int]:
    """解析 replay JSON payload。

    Args:
        payload: `json.load` 的返回值。

    Returns:
        (recording, meta, version)

    Raises:
        ValueError: payload 结构不合法。
    """

    doc: Any = payload
    if isinstance(payload, dict):
        if "tetrominode_replay" in payload:
            doc = payload.get("tetrominode_replay")
        elif "tetromino_replay" in payload:
            # backward compatible key
            doc = payload.get("tetromino_replay")
        elif "blender_tetris_replay" in payload:
            # backward compatible key
            doc = payload.get("blender_tetris_replay")

    meta: dict[str, Any] = {}
    version = 0
    steps_raw: Any = None

    if isinstance(doc, dict):
        version_raw = doc.get("version")
        if version_raw is not None:
            version = int(version_raw)

        meta_raw = doc.get("meta")
        if isinstance(meta_raw, dict):
            meta = dict(meta_raw)

        steps_raw = doc.get("steps")
        if steps_raw is None:
            steps_raw = doc.get("states")
        if steps_raw is None:
            steps_raw = doc.get("recording")

    elif isinstance(doc, list):
        # legacy root: just a list of steps
        steps_raw = doc
        version = 0

    else:
        raise ValueError("Replay JSON root must be a dict or list")

    if not isinstance(steps_raw, list):
        raise ValueError("Replay JSON missing steps list")

    recording: list[RecordedState] = []

    if version >= 2:
        # v2: 差分还原
        prev_occupied: dict[tuple[int, int], str] = {}
        for step_raw in steps_raw:
            if not isinstance(step_raw, dict):
                continue

            # 还原 occupied
            occupied = dict(prev_occupied)
            for item in step_raw.get("add", []):
                occupied[(int(item[0]), int(item[1]))] = str(item[2])
            for item in step_raw.get("rem", []):
                occupied.pop((int(item[0]), int(item[1])), None)

            current = _parse_current(step_raw.get("cur"))
            # 兼容 v2 的缩写 key 或 v1 的全称 key
            game_over = bool(step_raw.get("over", step_raw.get("game_over", False)))
            reason = str(step_raw.get("reason", "imported"))

            state = RecordedState(
                occupied=occupied, current=current, game_over=game_over, reason=reason
            )
            recording.append(state)
            prev_occupied = occupied
    else:
        # v0/v1: 全量解析
        for step in steps_raw:
            recording.append(_parse_step(step))

    return recording, meta, version


def save_replay_json(
    path: str,
    *,
    recording: list[RecordedState],
    meta: dict[str, Any] | None = None,
    indent: int = 2,
) -> None:
    """将录制序列导出为 JSON 文件（UTF-8）。"""

    payload = recording_to_replay_json(recording, meta=meta)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=int(indent))
        f.write("\n")


def load_replay_json(path: str) -> tuple[list[RecordedState], dict[str, Any], int]:
    """从 JSON 文件导入录制序列（UTF-8）。"""

    with open(path, "r", encoding="utf-8") as f:
        payload = json.load(f)

    return parse_replay_json(payload)
