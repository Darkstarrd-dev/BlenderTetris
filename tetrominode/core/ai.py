"""自动游戏（Auto Play）AI：1-ply 穷举落点 + 启发式打分。

核心思路：
- 对当前方块枚举所有旋转(0..3)与所有可落地的 X
- 对每个候选位置模拟硬降落地、消行
- 用简单特征（holes/aggregate height/bumpiness/lines）打分选最好

这里的 AI 不直接“按键模拟”，而是输出目标 (rotation, x)，再由上层规划成
ROT/MOVE/DROP 的动作序列。

注意：这是最基础的 1-ply 版本（只看当前块），后续可以扩展 2-ply/beam/expectimax。
"""

from __future__ import annotations

from dataclasses import dataclass

from .tetrominoes import rotated_cells


@dataclass(frozen=True)
class Placement:
    """一个候选落点（落地后的位置）及其评分结果。

    Attributes:
        rotation: 目标旋转（0..3）。
        x: 目标落点的左上角 x（棋盘坐标）。
        z: 目标落点的 z（棋盘坐标，落地后的锚点 z）。
        score: 综合评分（越大越好）。
        lines_cleared: 模拟落地后消行数。
        holes: 模拟落地后的“洞”数量。
        aggregate_height: 模拟落地后的总高度。
        bumpiness: 模拟落地后的凹凸度。
    """

    rotation: int
    x: int
    z: int
    score: float
    lines_cleared: int
    holes: int
    aggregate_height: int
    bumpiness: int


# 一组常见的基线权重（来源于许多公开的 1-ply tetris bot/实验）。
# Higher score is better.
WEIGHTS = {
    "aggregate_height": -0.510066,
    "lines": 0.760666,
    "holes": -0.35663,
    "bumpiness": -0.184483,
}

# 策略预设：用于 UI 一键切换。
WEIGHT_PRESETS: dict[str, dict[str, float]] = {
    # 更稳：强烈惩罚 holes，适度惩罚高度与凹凸。
    "STABLE": {
        "aggregate_height": -0.55,
        "lines": 0.65,
        "holes": -0.9,
        "bumpiness": -0.35,
    },
    # 更高分：更偏好消行（更容易追求 Tetris），但仍避免 holes。
    "HIGH_SCORE": {
        "aggregate_height": -0.42,
        "lines": 1.15,
        "holes": -0.45,
        "bumpiness": -0.18,
    },
    # 更观赏：更偏好平整（低 bumpiness）与较低高度。
    "SHOW": {
        "aggregate_height": -0.5,
        "lines": 0.6,
        "holes": -0.6,
        "bumpiness": -0.55,
    },
}


def _collides(
    *,
    cells: tuple[tuple[int, int], ...],
    occupied: set[tuple[int, int]],
    width: int,
    height: int,
) -> bool:
    """检测一组 cell 是否与边界或已占用格子冲突。

    Args:
        cells: 待检查的格子坐标列表（棋盘坐标系）。
        occupied: 已落地占用集合。
        width: 棋盘宽度。
        height: 棋盘高度。

    Returns:
        如果发生碰撞（越界或与占用重叠）返回 True，否则 False。
    """

    for (x, z) in cells:
        # 左右边界
        if x < 0 or x >= width:
            return True
        # 底部边界
        if z < 0:
            return True
        # 与占用碰撞（只检查棋盘内）
        if z < height and (x, z) in occupied:
            return True

    return False


def _drop_z(
    *,
    local: tuple[tuple[int, int], ...],
    x: int,
    start_z: int,
    occupied: set[tuple[int, int]],
    width: int,
    height: int,
) -> int | None:
    """给定一组 local cells 与 x，从 start_z 开始向下模拟“硬降”到最底。

    Args:
        local: 当前旋转下的本地 cell 坐标集合。
        x: 目标 x。
        start_z: 起始 z（通常从当前块的 z 开始，减少无意义搜索）。
        occupied: 已落地占用集合。
        width: 棋盘宽。
        height: 棋盘高。

    Returns:
        落地后的 z；如果起始位置本身就碰撞，则返回 None。
    """

    z = int(start_z)
    cells = tuple((x + cx, z + cz) for (cx, cz) in local)

    # 起始位置直接碰撞：该 x/rotation 不可行
    if _collides(cells=cells, occupied=occupied, width=width, height=height):
        return None

    # 一直尝试下移，直到下一步会碰撞
    while True:
        next_z = z - 1
        next_cells = tuple((x + cx, next_z + cz) for (cx, cz) in local)
        if _collides(cells=next_cells, occupied=occupied, width=width, height=height):
            return z
        z = next_z


def _clear_lines(
    *,
    occupied: dict[tuple[int, int], str],
    width: int,
    height: int,
) -> tuple[dict[tuple[int, int], str], int]:
    """对模拟占用执行消行，并返回新占用 + 消行数。

    Args:
        occupied: 模拟占用（cell -> piece_key）。
        width: 棋盘宽度。
        height: 棋盘高度。

    Returns:
        (new_occupied, lines_cleared)

        - `new_occupied`：执行消行与下落后的新占用。
        - `lines_cleared`：本次消除的行数。
    """

    counts = [0] * height
    for (x, z) in occupied:
        if 0 <= z < height:
            counts[z] += 1

    full_rows = [z for z, c in enumerate(counts) if c == width]
    if not full_rows:
        return occupied, 0

    full_set = set(full_rows)

    def rows_below(z: int) -> int:
        """统计 z 下方被消除的行数（用于计算下移）。

        Args:
            z: 当前行号。

        Returns:
            小于 z 的被消除行的数量。
        """

        return sum(1 for r in full_rows if r < z)

    new_occupied: dict[tuple[int, int], str] = {}
    for (x, z), k in occupied.items():
        if z in full_set:
            continue
        shift = rows_below(z)
        new_occupied[(x, z - shift)] = k

    return new_occupied, len(full_rows)


def _column_heights(*, occupied: set[tuple[int, int]], width: int) -> list[int]:
    """计算每列的高度（最高占用 z + 1）。

    Args:
        occupied: 已占用格子集合（cell 坐标）。
        width: 棋盘宽度。

    Returns:
        长度为 width 的高度列表。
    """

    heights = [0] * width
    for x in range(width):
        max_z = -1
        for (ox, oz) in occupied:
            if ox == x and oz > max_z:
                max_z = oz
        heights[x] = max_z + 1
    return heights


def _count_holes(*, occupied: set[tuple[int, int]], heights: list[int]) -> int:
    """统计“洞”的数量。

    定义：某列中，从 0 到该列最高高度之间的空格，都算洞。

    Args:
        occupied: 已占用格子集合。
        heights: 每列高度（来自 `_column_heights`）。

    Returns:
        洞的数量。
    """

    holes = 0
    width = len(heights)

    for x in range(width):
        h = heights[x]
        for z in range(h):
            if (x, z) not in occupied:
                holes += 1

    return holes


def _bumpiness(heights: list[int]) -> int:
    """凹凸度：相邻列高度差的绝对值之和。

    Args:
        heights: 每列高度。

    Returns:
        凹凸度（相邻差绝对值之和）。
    """

    return sum(abs(heights[i] - heights[i + 1]) for i in range(len(heights) - 1))


def evaluate_position(
    *,
    occupied: dict[tuple[int, int], str],
    width: int,
    height: int,
    lines_cleared: int,
    weights: dict[str, float] | None = None,
) -> tuple[float, int, int, int]:
    """对一个棋盘局面打分。

    Args:
        occupied: 局面占用。
        width: 宽。
        height: 高（这里用于一致性，不直接参与计算）。
        lines_cleared: 这一步落地后消的行数。

    Returns:
        (score, holes, aggregate_height, bumpiness)
    """

    occ_set = set(occupied.keys())
    heights = _column_heights(occupied=occ_set, width=width)
    agg_height = sum(heights)
    holes = _count_holes(occupied=occ_set, heights=heights)
    bump = _bumpiness(heights)

    w = WEIGHTS if weights is None else weights

    # 线性组合：最简单、最可解释的启发式
    score = (
        float(w.get("aggregate_height", WEIGHTS["aggregate_height"])) * float(agg_height)
        + float(w.get("lines", WEIGHTS["lines"])) * float(lines_cleared)
        + float(w.get("holes", WEIGHTS["holes"])) * float(holes)
        + float(w.get("bumpiness", WEIGHTS["bumpiness"])) * float(bump)
    )

    return score, holes, agg_height, bump


def find_best_placement(game, *, weights: dict[str, float] | None = None) -> Placement | None:
    """对当前块做 1-ply 搜索，返回最佳落点。

    Args:
        game: `TetrisGame`（或拥有相同字段接口的对象）。

    Returns:
        最佳 Placement；如果当前没有块则返回 None。

    Note:
        这里故意只看“当前块”，因为它足够快，适合 modal 实时。
    """

    piece = getattr(game, "current", None)
    if piece is None:
        return None

    width = int(getattr(game, "width"))
    height = int(getattr(game, "height"))

    occupied_map = getattr(game, "occupied", {})
    occupied_set = set(occupied_map.keys()) if isinstance(occupied_map, dict) else set(occupied_map)

    best: Placement | None = None

    # Search from current vertical position to keep placements feasible.
    start_z = int(getattr(piece, "z"))
    key = str(getattr(piece, "key"))
    system = str(getattr(piece, "system", getattr(game, "rotation_system", "SRS")) or "SRS").upper()

    for rotation in range(4):
        local = rotated_cells(key, rotation, system=system)
        max_x = max(x for (x, _) in local)

        # x 的枚举范围：保证该旋转的最大 x 不越过右边界
        for x in range(0, width - max_x):
            z = _drop_z(
                local=local,
                x=x,
                start_z=start_z,
                occupied=occupied_set,
                width=width,
                height=height,
            )
            if z is None:
                continue

            placed_cells = tuple((x + cx, z + cz) for (cx, cz) in local)

            # 复制占用并模拟落地
            simulated = dict(occupied_map)

            for cell in placed_cells:
                # 关键分支：如果锁定后仍超过棋盘顶部，直接判为无效（否则评分会失真）
                if cell[1] >= height:
                    simulated = {}
                    break
                simulated[cell] = key

            if not simulated:
                continue

            simulated, lines = _clear_lines(occupied=simulated, width=width, height=height)

            score, holes, agg_height, bump = evaluate_position(
                occupied=simulated,
                width=width,
                height=height,
                lines_cleared=lines,
                weights=weights,
            )

            candidate = Placement(
                rotation=rotation,
                x=x,
                z=z,
                score=score,
                lines_cleared=lines,
                holes=holes,
                aggregate_height=agg_height,
                bumpiness=bump,
            )

            if best is None or candidate.score > best.score:
                best = candidate

    return best


PLY2_NEXT_WEIGHT = 0.8


def _spawn_start_z(*, key: str, height: int, system: str) -> int:
    local = rotated_cells(str(key), 0, system=str(system).upper())
    max_z = max(z for (_, z) in local)
    return (int(height) - 1) - int(max_z)


def find_best_placement_2ply(
    game,
    *,
    weights: dict[str, float] | None = None,
    next_weight: float = PLY2_NEXT_WEIGHT,
) -> Placement | None:
    """对当前块做 2-ply 搜索（考虑下一块），返回最佳落点。

    这里的 2-ply 指：
    1) 枚举当前块的所有可落点
    2) 对每个落点，继续枚举“下一块”的最佳落点

    Note:
        - 为了保持 modal 实时性，不做 beam/expectimax。
        - 默认使用 `PLY2_NEXT_WEIGHT` 对第二层评分做折扣。
    """

    piece = getattr(game, "current", None)
    if piece is None:
        return None

    next_key: str | None = None

    peek_next = getattr(game, "peek_next", None)
    if callable(peek_next):
        nxt = peek_next(count=1)
        if nxt:
            next_key = str(nxt[0])
    else:
        queue = getattr(game, "next_queue", None)
        if queue:
            next_key = str(queue[0])

    if not next_key:
        return find_best_placement(game, weights=weights)

    width = int(getattr(game, "width"))
    height = int(getattr(game, "height"))

    occupied_raw = getattr(game, "occupied", {})
    if isinstance(occupied_raw, dict):
        occupied_map: dict[tuple[int, int], str] = dict(occupied_raw)
    else:
        occupied_map = {tuple(cell): "X" for cell in occupied_raw}

    occupied_set = set(occupied_map.keys())

    start_z = int(getattr(piece, "z"))
    key = str(getattr(piece, "key"))
    system = str(getattr(piece, "system", getattr(game, "rotation_system", "SRS")) or "SRS").upper()

    next_start_z = _spawn_start_z(key=next_key, height=height, system=system)

    # 预计算下一块在 4 个旋转下的 local cells，提高 2-ply 性能。
    next_locals = [rotated_cells(next_key, r, system=system) for r in range(4)]
    next_max_x = [max(x for (x, _) in local) for local in next_locals]

    best: Placement | None = None

    for rotation in range(4):
        local = rotated_cells(key, rotation, system=system)
        max_x = max(x for (x, _) in local)

        for x in range(0, width - max_x):
            z = _drop_z(
                local=local,
                x=x,
                start_z=start_z,
                occupied=occupied_set,
                width=width,
                height=height,
            )
            if z is None:
                continue

            placed_cells = tuple((x + cx, z + cz) for (cx, cz) in local)

            simulated_1 = dict(occupied_map)
            valid_1 = True
            for cell in placed_cells:
                if cell[1] >= height:
                    valid_1 = False
                    break
                simulated_1[cell] = key

            if not valid_1:
                continue

            simulated_1, lines_1 = _clear_lines(occupied=simulated_1, width=width, height=height)

            score_1, holes_1, agg_height_1, bump_1 = evaluate_position(
                occupied=simulated_1,
                width=width,
                height=height,
                lines_cleared=lines_1,
                weights=weights,
            )

            occ1_set = set(simulated_1.keys())

            best_next_score: float | None = None

            for next_rot in range(4):
                local_2 = next_locals[next_rot]
                max_x_2 = next_max_x[next_rot]

                for x_2 in range(0, width - max_x_2):
                    z_2 = _drop_z(
                        local=local_2,
                        x=x_2,
                        start_z=next_start_z,
                        occupied=occ1_set,
                        width=width,
                        height=height,
                    )
                    if z_2 is None:
                        continue

                    placed_2 = tuple((x_2 + cx, z_2 + cz) for (cx, cz) in local_2)

                    simulated_2 = dict(simulated_1)
                    valid_2 = True
                    for cell in placed_2:
                        if cell[1] >= height:
                            valid_2 = False
                            break
                        simulated_2[cell] = next_key

                    if not valid_2:
                        continue

                    simulated_2, lines_2 = _clear_lines(occupied=simulated_2, width=width, height=height)

                    score_2, _, _, _ = evaluate_position(
                        occupied=simulated_2,
                        width=width,
                        height=height,
                        lines_cleared=lines_2,
                        weights=weights,
                    )

                    if best_next_score is None or score_2 > best_next_score:
                        best_next_score = score_2

            combined = score_1
            if best_next_score is None:
                combined -= 1.0e9
            else:
                combined += float(best_next_score) * float(next_weight)

            candidate = Placement(
                rotation=rotation,
                x=x,
                z=z,
                score=combined,
                lines_cleared=lines_1,
                holes=holes_1,
                aggregate_height=agg_height_1,
                bumpiness=bump_1,
            )

            if best is None or candidate.score > best.score:
                best = candidate

    return best


def plan_actions(*, piece, target_rotation: int, target_x: int) -> list[str]:
    """把目标落点转换为离散动作序列（旋转/移动/硬降）。

    Args:
        piece: 当前块对象（只要求有 rotation/x 属性）。
        target_rotation: 目标旋转（0..3）。
        target_x: 目标 x。

    Returns:
        动作列表，元素是字符串：
        - ROT_CW / ROT_CCW
        - LEFT / RIGHT
        - DROP

    Note:
        这里为了简化：
        - delta==2 时用两次 ROT_CW。
        - delta==3 用一次 ROT_CCW。
    """

    actions: list[str] = []

    current_rotation = int(getattr(piece, "rotation"))
    current_x = int(getattr(piece, "x"))

    delta = (int(target_rotation) - current_rotation) % 4
    if delta == 3:
        actions.append("ROT_CCW")
    elif delta == 2:
        actions.extend(["ROT_CW", "ROT_CW"])
    else:
        actions.extend(["ROT_CW"] * delta)

    dx = int(target_x) - current_x
    if dx > 0:
        actions.extend(["RIGHT"] * dx)
    elif dx < 0:
        actions.extend(["LEFT"] * (-dx))

    # 最后统一用硬降锁定
    actions.append("DROP")
    return actions
