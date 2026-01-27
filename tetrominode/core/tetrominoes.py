"""Tetromino 定义、旋转与 SRS wall-kick 数据。

约定：
- 使用 XZ 平面（x=列，z=行），z 越大越“高”，向下落是 `dz=-1`。
- 旋转状态使用 0/R/2/L 的约定，并映射为 rotation=0/1/2/3。

旋转实现：
- J/L/S/T/Z：使用固定 3x3 包围盒，本地坐标范围 x/z ∈ [0..2]。
  - 旋转中心为 (1, 1)（整数），纯旋转后仍是整数格。
- I/O：使用固定 4x4 包围盒，本地坐标范围 x/z ∈ [0..3]。
  - 旋转中心为 (1.5, 1.5)（格线交点），纯旋转后仍会落在整数格。

SRS wall-kick：
- J/L/S/T/Z 共用一套 kick 表。
- I 有独立 kick 表。
- O 不 kick（只测试 (0,0)）。

注：这里实现的是 Guideline SRS 的 kick translation 表（见 https://tetris.wiki/Super_Rotation_System）。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TetrominoDef:
    """单个 tetromino 的定义（用于生成、旋转与索引映射）。

    Attributes:
        key: 形状标识（I/O/T/S/Z/J/L）。
        cells: rotation=0（spawn state）时，在固定包围盒坐标系下的 4 个 cell 坐标。
    """

    key: str
    cells: tuple[tuple[int, int], ...]


# SRS spawn state（rotation=0）定义。
# 这里的坐标不是“最小包围盒”，而是 SRS 固定包围盒（3x3 或 4x4）。
TETROMINO_DEFS: tuple[TetrominoDef, ...] = (
    # I/O 使用 4x4。
    TetrominoDef("I", ((0, 2), (1, 2), (2, 2), (3, 2))),
    TetrominoDef("O", ((1, 1), (2, 1), (1, 2), (2, 2))),
    # J/L/S/T/Z 使用 3x3。
    TetrominoDef("T", ((0, 1), (1, 1), (2, 1), (1, 2))),
    TetrominoDef("S", ((0, 1), (1, 1), (1, 2), (2, 2))),
    TetrominoDef("Z", ((1, 1), (2, 1), (0, 2), (1, 2))),
    TetrominoDef("J", ((0, 1), (1, 1), (2, 1), (0, 2))),
    TetrominoDef("L", ((0, 1), (1, 1), (2, 1), (2, 2))),
)

TETROMINO_KEYS: tuple[str, ...] = tuple(t.key for t in TETROMINO_DEFS)
TETROMINO_BY_KEY: dict[str, TetrominoDef] = {t.key: t for t in TETROMINO_DEFS}

# 约定：piece index 与 Looks collection 的对象顺序一致。
PIECE_INDEX_BY_KEY: dict[str, int] = {key: idx for idx, key in enumerate(TETROMINO_KEYS)}


def piece_index(key: str) -> int:
    """把形状 key 映射为实例索引。

    该索引用于：
    - 点属性 `bltetris_piece`
    - GN 的 `Instance Index`（Pick Instance 时选择 `TetrisLooks` 内的 look object）

    Args:
        key: 形状标识。

    Returns:
        0..6 的整数，分别对应 I/O/T/S/Z/J/L。

    Raises:
        KeyError: 传入了未知的形状标识。
    """

    idx = PIECE_INDEX_BY_KEY.get(key)
    if idx is None:
        raise KeyError(f"Unknown tetromino key: {key}")
    return idx


# 每个 tetromino 使用的包围盒边长。
_BOX_SIZE_BY_KEY: dict[str, int] = {
    "I": 4,
    "O": 4,
    "T": 3,
    "S": 3,
    "Z": 3,
    "J": 3,
    "L": 3,
}

# 每个 tetromino 的旋转中心（包围盒局部坐标）。
_ROT_CENTER_BY_KEY: dict[str, tuple[float, float]] = {
    # 3x3 pieces rotate about the center cell.
    "T": (1.0, 1.0),
    "S": (1.0, 1.0),
    "Z": (1.0, 1.0),
    "J": (1.0, 1.0),
    "L": (1.0, 1.0),
    # I/O rotate about the center of the 4x4 box.
    "I": (1.5, 1.5),
    "O": (1.5, 1.5),
}


# -------------------- Rotation Logic --------------------

def _rotate_cell_cw_about(*, x: int, z: int, cx: float, cz: float) -> tuple[int, int]:
    """SRS 专用：将单个 cell 围绕中心 (cx,cz) 顺时针旋转 90°。

    旋转公式（z 向上）：
    - dx = x - cx
    - dz = z - cz
    - (dx, dz) -> (dz, -dx)

    Args:
        x, z: 原始坐标。
        cx, cz: 旋转中心。

    Returns:
        旋转后的整数格子坐标。
    """
    dx = float(x) - float(cx)
    dz = float(z) - float(cz)

    rx = float(cx) + dz
    rz = float(cz) - dx

    return int(round(rx)), int(round(rz))


def _rotate_cells_simple(cells: tuple[tuple[int, int], ...]) -> tuple[tuple[int, int], ...]:
    """Simple 模式专用：将 cell 集合围绕原点顺时针旋转 90°，并归一化到最小包围盒。

    旋转公式：(x, z) -> (z, -x)
    归一化：将旋转后坐标平移，使得 min_x=0, min_z=0。

    Args:
        cells: 原始坐标集合。

    Returns:
        旋转并归一化后的 cell 集合。
    """

    rotated = tuple((z, -x) for (x, z) in cells)

    # 归一化：把最小坐标平移到 (0,0)
    min_x = min(x for (x, _) in rotated)
    min_z = min(z for (_, z) in rotated)

    return tuple((x - min_x, z - min_z) for (x, z) in rotated)


def rotated_cells(key: str, rotation: int, system: str = "SRS") -> tuple[tuple[int, int], ...]:
    """获取指定 tetromino 在给定旋转下的 cell 集合。

    Args:
        key: 形状标识。
        rotation: 旋转次数（0..3）。
        system: 旋转系统标识，"SRS" (默认) 或 "SIMPLE" (经典简化版)。

    Returns:
        在本地坐标系下的 4 个 cell 坐标。

    Raises:
        KeyError: 未知形状。
    """

    tet = TETROMINO_BY_KEY.get(key)
    if tet is None:
        raise KeyError(f"Unknown tetromino key: {key}")

    turns = int(rotation) % 4
    cells = tet.cells

    if str(system).upper() == "SIMPLE":
        # Simple 模式：每次旋转执行简单的旋转+归一化
        for _ in range(turns):
            cells = _rotate_cells_simple(cells)
    else:
        # SRS 模式：围绕特定旋转中心旋转，保持包围盒对齐
        cx, cz = _ROT_CENTER_BY_KEY.get(key, (1.0, 1.0))
        for _ in range(turns):
            cells = tuple(_rotate_cell_cw_about(x=x, z=z, cx=cx, cz=cz) for (x, z) in cells)

    # 稳定排序，避免字典顺序影响下游
    return tuple(sorted(cells, key=lambda p: (p[1], p[0])))



def cells_bbox(cells: tuple[tuple[int, int], ...]) -> tuple[int, int, int, int]:
    """计算 cell 集合的包围盒。

    Args:
        cells: cell 集合。

    Returns:
        `(min_x, min_z, max_x, max_z)`。

    Note:
        这里使用 (x,z) 而不是 (x,y)，因为棋盘在 XZ 平面。
    """

    xs = [x for (x, _) in cells]
    zs = [z for (_, z) in cells]
    return min(xs), min(zs), max(xs), max(zs)


# -------------------- SRS Wall Kick Tables --------------------

# 旋转状态索引约定：0=spawn, 1=R, 2=2, 3=L。
# Kick translation 的坐标约定：(+x 向右，+z 向上)。

_SRS_KICKS_JLSTZ: dict[tuple[int, int], tuple[tuple[int, int], ...]] = {
    (0, 1): ((0, 0), (-1, 0), (-1, 1), (0, -2), (-1, -2)),
    (1, 0): ((0, 0), (1, 0), (1, -1), (0, 2), (1, 2)),
    (1, 2): ((0, 0), (1, 0), (1, -1), (0, 2), (1, 2)),
    (2, 1): ((0, 0), (-1, 0), (-1, 1), (0, -2), (-1, -2)),
    (2, 3): ((0, 0), (1, 0), (1, 1), (0, -2), (1, -2)),
    (3, 2): ((0, 0), (-1, 0), (-1, -1), (0, 2), (-1, 2)),
    (3, 0): ((0, 0), (-1, 0), (-1, -1), (0, 2), (-1, 2)),
    (0, 3): ((0, 0), (1, 0), (1, 1), (0, -2), (1, -2)),
}

_SRS_KICKS_I: dict[tuple[int, int], tuple[tuple[int, int], ...]] = {
    (0, 1): ((0, 0), (-2, 0), (1, 0), (-2, -1), (1, 2)),
    (1, 0): ((0, 0), (2, 0), (-1, 0), (2, 1), (-1, -2)),
    (1, 2): ((0, 0), (-1, 0), (2, 0), (-1, 2), (2, -1)),
    (2, 1): ((0, 0), (1, 0), (-2, 0), (1, -2), (-2, 1)),
    (2, 3): ((0, 0), (2, 0), (-1, 0), (2, 1), (-1, -2)),
    (3, 2): ((0, 0), (-2, 0), (1, 0), (-2, -1), (1, 2)),
    (3, 0): ((0, 0), (1, 0), (-2, 0), (1, -2), (-2, 1)),
    (0, 3): ((0, 0), (-1, 0), (2, 0), (-1, 2), (2, -1)),
}


def srs_kicks(*, key: str, from_rotation: int, to_rotation: int) -> tuple[tuple[int, int], ...]:
    """获取一次旋转（from->to）的 SRS kick 列表。

    Args:
        key: tetromino key。
        from_rotation: 起始旋转状态（0..3）。
        to_rotation: 目标旋转状态（0..3）。

    Returns:
        5 个 (dx, dz) 的元组（依次测试）。

        - 对于 O：返回 ((0,0),)（不 kick）
        - 对于未知/不支持组合：也返回 ((0,0),)
    """

    fr = int(from_rotation) % 4
    tr = int(to_rotation) % 4

    if fr == tr:
        return ((0, 0),)

    if key == "O":
        return ((0, 0),)

    if key == "I":
        return _SRS_KICKS_I.get((fr, tr), ((0, 0),))

    # J/L/S/T/Z 共用。
    return _SRS_KICKS_JLSTZ.get((fr, tr), ((0, 0),))
