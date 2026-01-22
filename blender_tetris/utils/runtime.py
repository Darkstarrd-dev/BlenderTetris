"""运行时数据同步：把 game state 写进 Points Mesh。

实时渲染路径：
- `runtime.sync_from_game()` 把棋盘/当前块/外框转换为一堆顶点坐标（格子中心点）
- 同时写入点属性：
  - `bltetris_piece`：用于 GN pick instance
  - `bltetris_color`：用于材质读取
- 这些 Points Mesh 上挂了 GN，GN 再把点实例化成真正可渲染的方块。

注意：如果用户在 Edit Mode，下游 `mesh.from_pydata()` 会报错，因此这里会尝试切回 Object Mode。
"""

from __future__ import annotations

import math

import bpy

from . import geo_nodes
from . import looks
from ..data.constants import (
    AI_PATH_POINTS_OBJECT_NAME,
    AI_TARGET_POINTS_OBJECT_NAME,
    ATTR_COLOR_NAME,
    ATTR_LEVEL_NAME,
    ATTR_PIECE_NAME,
    ATTR_SCORE_NAME,
    BORDER_PIECE_INDEX,
    CELL_SIZE_DEFAULT,
    GN_GROUP_NAME,
    GHOST_POINTS_OBJECT_NAME,
    HOLD_POINTS_OBJECT_NAME,
    HUD_COLLECTION_NAME,
    NEXT_POINTS_OBJECT_NAME,
    STATS_POINTS_OBJECT_NAME,
    STATS_TEXT_GN_GROUP_NAME,
    STATS_TEXT_GN_MODIFIER_NAME,
)
from ..core.tetrominoes import cells_bbox, piece_index, rotated_cells


# 运行时对象与集合的名称定义
GAME_COLLECTION_NAME = "TetrisGame"
BOARD_POINTS_OBJECT_NAME = "BLTETRIS_Board_Points"
CURRENT_POINTS_OBJECT_NAME = "BLTETRIS_Current_Points"
BORDER_POINTS_OBJECT_NAME = "BLTETRIS_Border_Points"


def ensure_collection(name: str, *, parent: bpy.types.Collection) -> bpy.types.Collection:
    """确保指定名称的 collection 存在，并链接到父级。

    Args:
        name: collection 名。
        parent: 父级 collection。

    Returns:
        `bpy.data.collections[name]`。
    """

    collection = bpy.data.collections.get(name)
    if collection is None:
        collection = bpy.data.collections.new(name)

    if parent.children.get(collection.name) is None:
        parent.children.link(collection)

    return collection


def ensure_points_object(*, name: str, collection: bpy.types.Collection) -> bpy.types.Object:
    """确保运行时点云对象（Points Mesh）存在。

    Args:
        name: 对象名称。
        collection: 所属 collection。

    Returns:
        对应的 `bpy.types.Object`。

    Raises:
        TypeError: 同名对象存在但不是 MESH。
    """

    obj = bpy.data.objects.get(name)
    if obj is None:
        # 创建新的 mesh 和 object
        mesh = bpy.data.meshes.new(f"{name}_mesh")
        obj = bpy.data.objects.new(name, mesh)

    if obj.type != "MESH":
        raise TypeError(f"Object '{name}' exists but is not a MESH")

    # 确保链接到指定集合
    if collection.objects.get(obj.name) is None:
        collection.objects.link(obj)

    return obj


def _ensure_mesh_attribute(
    mesh: bpy.types.Mesh,
    *,
    name: str,
    data_type: str,
    domain: str,
) -> bpy.types.Attribute:
    """内部工具：确保 mesh 上存在指定类型/域的属性。

    Args:
        mesh: 目标 mesh 数据块。
        name: 属性名。
        data_type: 属性数据类型（如 INT / FLOAT_COLOR）。
        domain: 属性域（POINT 等）。

    Returns:
        确保存在后的 `bpy.types.Attribute`。
    """

    attr = mesh.attributes.get(name)
    if attr is not None and (attr.data_type != data_type or attr.domain != domain):
        mesh.attributes.remove(attr)
        attr = None

    if attr is None:
        attr = mesh.attributes.new(name=name, type=data_type, domain=domain)

    return attr


def set_points(
    *,
    obj: bpy.types.Object,
    points_world: list[tuple[float, float, float]],
    piece_ids: list[int] | None = None,
    colors: list[tuple[float, float, float, float]] | None = None,
    scales: list[float] | None = None,
) -> None:
    """核心入口：把一组 3D 坐标和属性写入 MESH 对象（仅顶点）。

    Args:
        obj: 目标 MESH 对象。
        points_world: 顶点列表（格子中心坐标）。
        piece_ids: 形状索引列表（可选）。
        colors: 颜色列表（RGBA）（可选）。
        scales: 缩放列表（可选）。

    Raises:
        RuntimeError: 如果用户处于 Edit Mode 且无法切回。
    """

    if obj.type != "MESH":
        raise TypeError(f"Object '{obj.name}' is not a MESH")

    # 校验属性长度是否对齐
    if piece_ids is not None and len(piece_ids) != len(points_world):
        raise ValueError("piece_ids length must match points_world length")
    if colors is not None and len(colors) != len(points_world):
        raise ValueError("colors length must match points_world length")

    mesh = obj.data

    # 重要：mesh.from_pydata 在 Edit Mode 会失败。
    if bpy.context.mode != "OBJECT" or getattr(mesh, "is_editmode", False):
        try:
            bpy.ops.object.mode_set(mode="OBJECT")
        except Exception:
            # 关键分支：如果处于特殊模式（如 Sculpt）可能无法简单切回。
            if getattr(mesh, "is_editmode", False):
                raise RuntimeError("BLTETRIS: cannot update game meshes while in Edit Mode")

    # 写入顶点位置
    mesh.clear_geometry()
    mesh.from_pydata(points_world, [], [])
    mesh.update()

    # 写入点属性：piece 索引（用于 GN 实例化选择）
    if piece_ids is not None:
        attr = _ensure_mesh_attribute(mesh, name=ATTR_PIECE_NAME, data_type="INT", domain="POINT")
        for idx, value in enumerate(piece_ids):
            attr.data[idx].value = int(value)

    # 写入点属性：RGBA 颜色（供材质 Attribute 节点读取）
    if colors is not None:
        attr = _ensure_mesh_attribute(mesh, name=ATTR_COLOR_NAME, data_type="FLOAT_COLOR", domain="POINT")
        for idx, rgba in enumerate(colors):
            attr.data[idx].color = tuple(float(v) for v in rgba)

    # 写入点属性：缩放系数
    if scales is not None:
        from ..data.constants import ATTR_SCALE_NAME
        attr = _ensure_mesh_attribute(mesh, name=ATTR_SCALE_NAME, data_type="FLOAT", domain="POINT")
        for idx, val in enumerate(scales):
            attr.data[idx].value = float(val)
    else:
        # 默认 1.0
        from ..data.constants import ATTR_SCALE_NAME
        attr = _ensure_mesh_attribute(mesh, name=ATTR_SCALE_NAME, data_type="FLOAT", domain="POINT")
        for i in range(len(mesh.vertices)):
            attr.data[i].value = 1.0


def board_origin_corner(
    *,
    width: int,
    height: int,
    cell_size: float = CELL_SIZE_DEFAULT,
    y: float = 0.0,
) -> tuple[float, float, float]:
    """计算棋盘左下角在世界坐标中的原点。

    当前逻辑：让棋盘中心对齐到世界 (0, 0, 0)。
    角点计算公式：-(width * cell_size) / 2

    Returns:
        (ox, oy, oz) 三元组。
    """

    return (
        -(float(width) * float(cell_size)) / 2.0,
        float(y),
        -(float(height) * float(cell_size)) / 2.0,
    )


def cells_to_world_points(
    *,
    cells: list[tuple[int, int]],
    cell_size: float = CELL_SIZE_DEFAULT,
    origin: tuple[float, float, float] = (0.0, 0.0, 0.0),
) -> list[tuple[float, float, float]]:
    """把格子坐标 `(x, z)` 转换为 Blender 世界坐标 `(x, y, z)`。

    对齐：点落在格子的正中心（`+0.5 * cell_size`）。

    Returns:
        与输入 cells 等长的世界坐标点列表。
    """

    ox, oy, oz = origin
    half = float(cell_size) / 2.0
    return [(ox + (x * cell_size) + half, oy, oz + (z * cell_size) + half) for (x, z) in cells]


def border_cells(*, width: int, height: int) -> list[tuple[int, int]]:
    """生成棋盘外围一圈的格子坐标集合（外扩 1 格）。

    Returns:
        外扩 1 格的边框 cell 列表（已排序，保证幂等）。
    """

    cells: set[tuple[int, int]] = set()

    # 左右立柱
    for z in range(-1, height + 1):
        cells.add((-1, z))
        cells.add((width, z))

    # 上下横梁
    for x in range(-1, width + 1):
        cells.add((x, -1))
        cells.add((x, height))

    # 返回排序后的列表（保持结果幂等）
    return sorted(cells, key=lambda p: (p[1], p[0]))


def ensure_runtime_objects(
    *,
    cell_size: float = CELL_SIZE_DEFAULT,
    looks_collection: bpy.types.Collection,
    block_scale: tuple[float, float, float] = (1.0, 1.0, 1.0),
) -> tuple[bpy.types.Object, bpy.types.Object, bpy.types.Object, bpy.types.Object]:
    """确保所有运行时点云对象已创建并挂载了正确的 GN Modifier。

    Returns:
        (board_obj, current_obj, border_obj, ghost_obj)
    """

    scene_root = bpy.context.scene.collection
    game_collection = ensure_collection(GAME_COLLECTION_NAME, parent=scene_root)

    board_obj = ensure_points_object(name=BOARD_POINTS_OBJECT_NAME, collection=game_collection)
    current_obj = ensure_points_object(name=CURRENT_POINTS_OBJECT_NAME, collection=game_collection)
    border_obj = ensure_points_object(name=BORDER_POINTS_OBJECT_NAME, collection=game_collection)
    ghost_obj = ensure_points_object(name=GHOST_POINTS_OBJECT_NAME, collection=game_collection)

    node_group = bpy.data.node_groups.get(GN_GROUP_NAME) or geo_nodes.ensure_points_to_blocks_node_group()

    # 基础材质
    default_mat = looks.ensure_attr_color_material()

    # 为常规对象更新 GN modifier 并确保材质为 BLTETRIS_AttrColor
    for obj in (board_obj, current_obj, border_obj):
        geo_nodes.ensure_geometry_nodes_modifier(
            obj=obj,
            node_group=node_group,
            looks_collection=looks_collection,
            block_scale=block_scale,
        )
        if default_mat not in list(obj.data.materials):
            obj.data.materials.clear()
            obj.data.materials.append(default_mat)

    # Ghost 处理
    from ..data.constants import GHOST_GN_MODIFIER_NAME

    # 1. 实例化方块
    geo_nodes.ensure_geometry_nodes_modifier(
        obj=ghost_obj,
        node_group=node_group,
        looks_collection=looks_collection,
        block_scale=block_scale,
    )

    # 2. 强制设置 Ghost 材质（通过第二个 GN 组）
    ghost_mat_group = geo_nodes.ensure_ghost_material_node_group()
    ghost_mat_mod = ghost_obj.modifiers.get(GHOST_GN_MODIFIER_NAME)
    if ghost_mat_mod is None:
        ghost_mat_mod = ghost_obj.modifiers.new(name=GHOST_GN_MODIFIER_NAME, type="NODES")
    ghost_mat_mod.node_group = ghost_mat_group

    return board_obj, current_obj, border_obj, ghost_obj


def sync_from_game(
    *,
    game,
    board_obj: bpy.types.Object,
    current_obj: bpy.types.Object,
    border_obj: bpy.types.Object,
    ghost_obj: bpy.types.Object | None = None,
    cell_size: float = CELL_SIZE_DEFAULT,
    origin_corner: tuple[float, float, float] | None = None,
    piece_colors: dict[str, tuple[float, float, float, float]] | None = None,
    border_color: tuple[float, float, float, float] = (0.2, 0.2, 0.2, 1.0),
    show_ghost: bool = False,
) -> None:
    """将 TetrisGame 逻辑层的局面同步到 Blender 表现层（Mesh）。

    核心流程：
    1. 读取 game.occupied (落地块) -> 转换坐标与属性 -> set_points 到 board_obj
    2. 读取 game.current (下落块) -> 转换坐标与属性 -> set_points 到 current_obj
    3. 生成棋盘外框 -> set_points 到 border_obj
    4. (可选) 计算 Ghost -> set_points 到 ghost_obj
    """

    # 1. 落地块处理
    occupied = getattr(game, "occupied", {})
    if isinstance(occupied, dict):
        occupied_items = list(occupied.items())
    else:
        occupied_items = [((x, z), "I") for (x, z) in list(occupied)]

    occupied_items.sort(key=lambda item: (item[0][1], item[0][0]))

    board_cells: list[tuple[int, int]] = []
    board_piece_ids: list[int] = []
    board_colors: list[tuple[float, float, float, float]] = []
    board_scales: list[float] = []

    if piece_colors is None:
        piece_colors = {}

    clearing_rows = set(getattr(game, "clearing_rows", []))
    anim_progress = float(getattr(game, "clear_anim_progress", 0.0))

    for (x, z), key in occupied_items:
        board_cells.append((int(x), int(z)))
        board_piece_ids.append(piece_index(str(key)))
        board_colors.append(piece_colors.get(str(key), (1.0, 1.0, 1.0, 1.0)))
        
        # 消行中的方块缩小
        if int(z) in clearing_rows:
            board_scales.append(max(0.0, 1.0 - anim_progress))
        else:
            board_scales.append(1.0)

    # 2. 当前块处理
    current_piece = getattr(game, "current", None)
    current_cells: list[tuple[int, int]] = []
    current_piece_ids: list[int] = []
    current_colors: list[tuple[float, float, float, float]] = []

    if current_piece is not None:
        key = str(getattr(current_piece, "key"))
        pid = piece_index(key)
        col = piece_colors.get(key, (1.0, 1.0, 1.0, 1.0))
        for (cx, cz) in current_piece.cells_global():
            current_cells.append((int(cx), int(cz)))
            current_piece_ids.append(pid)
            current_colors.append(col)

    # 3. Ghost 处理
    ghost_cells: list[tuple[int, int]] = []
    ghost_ids: list[int] = []
    ghost_colors: list[tuple[float, float, float, float]] = []

    if show_ghost and ghost_obj is not None and current_piece is not None:
        ghost = getattr(game, "get_ghost", lambda: None)()
        if ghost:
            key = str(ghost.key)
            pid = piece_index(key)
            base_col = piece_colors.get(key, (1.0, 1.0, 1.0, 1.0))
            # 0.25 alpha for ghost
            g_col = (base_col[0], base_col[1], base_col[2], 0.25)
            for (gx, gz) in ghost.cells_global():
                ghost_cells.append((int(gx), int(gz)))
                ghost_ids.append(pid)
                ghost_colors.append(g_col)

    # 4. 坐标对齐与应用
    width = int(getattr(game, "width", 0) or 0)
    height = int(getattr(game, "height", 0) or 0)

    if origin_corner is None:
        if width > 0 and height > 0:
            origin_corner = board_origin_corner(width=width, height=height, cell_size=cell_size)
        else:
            origin_corner = (0.0, 0.0, 0.0)

    board_points = cells_to_world_points(cells=board_cells, cell_size=cell_size, origin=origin_corner)
    current_points = cells_to_world_points(cells=current_cells, cell_size=cell_size, origin=origin_corner)

    set_points(obj=board_obj, points_world=board_points, piece_ids=board_piece_ids, colors=board_colors, scales=board_scales)
    set_points(obj=current_obj, points_world=current_points, piece_ids=current_piece_ids, colors=current_colors)

    if ghost_obj is not None:
        ghost_points = cells_to_world_points(cells=ghost_cells, cell_size=cell_size, origin=origin_corner)
        set_points(obj=ghost_obj, points_world=ghost_points, piece_ids=ghost_ids, colors=ghost_colors)

    # 5. 外框处理
    if width > 0 and height > 0:
        bcells = border_cells(width=width, height=height)
        bpoints = cells_to_world_points(cells=bcells, cell_size=cell_size, origin=origin_corner)
        bpiece_ids = [BORDER_PIECE_INDEX] * len(bcells)
        bcolors = [border_color] * len(bcells)
        set_points(obj=border_obj, points_world=bpoints, piece_ids=bpiece_ids, colors=bcolors)


def ensure_hud_objects(
    *,
    looks_collection: bpy.types.Collection,
    block_scale: tuple[float, float, float] = (1.0, 1.0, 1.0),
) -> tuple[bpy.types.Object, bpy.types.Object, bpy.types.Object]:
    """确保 HUD（Next/Hold/Stats）用的对象存在并挂载 GN。

    Returns:
        (next_obj, hold_obj, stats_obj)
    """

    scene_root = bpy.context.scene.collection
    hud_collection = ensure_collection(HUD_COLLECTION_NAME, parent=scene_root)

    next_obj = ensure_points_object(name=NEXT_POINTS_OBJECT_NAME, collection=hud_collection)
    hold_obj = ensure_points_object(name=HOLD_POINTS_OBJECT_NAME, collection=hud_collection)
    stats_obj = ensure_points_object(name=STATS_POINTS_OBJECT_NAME, collection=hud_collection)

    node_group = bpy.data.node_groups.get(GN_GROUP_NAME) or geo_nodes.ensure_points_to_blocks_node_group()
    default_mat = looks.ensure_attr_color_material()

    for obj in (next_obj, hold_obj):
        geo_nodes.ensure_geometry_nodes_modifier(
            obj=obj,
            node_group=node_group,
            looks_collection=looks_collection,
            block_scale=block_scale,
        )
        if default_mat not in list(obj.data.materials):
            obj.data.materials.clear()
            obj.data.materials.append(default_mat)

    stats_group = bpy.data.node_groups.get(STATS_TEXT_GN_GROUP_NAME) or geo_nodes.ensure_stats_text_node_group()

    stats_mod = stats_obj.modifiers.get(STATS_TEXT_GN_MODIFIER_NAME)
    if stats_mod is None:
        stats_mod = stats_obj.modifiers.new(name=STATS_TEXT_GN_MODIFIER_NAME, type="NODES")

    if stats_mod.type != "NODES":
        raise TypeError(
            f"Modifier '{STATS_TEXT_GN_MODIFIER_NAME}' exists on '{stats_obj.name}' but is not a Geometry Nodes modifier"
        )

    stats_mod.node_group = stats_group

    # 移除 Solidify 修改器（已移入 GN 内部以避免 Crash）
    solidify_name = "BLTETRIS_Stats_Solidify"
    if stats_obj.modifiers.get(solidify_name):
        stats_obj.modifiers.remove(stats_obj.modifiers[solidify_name])

    return next_obj, hold_obj, stats_obj


def ensure_ai_debug_objects(
    *,
    looks_collection: bpy.types.Collection,
    block_scale: tuple[float, float, float] = (1.0, 1.0, 1.0),
) -> tuple[bpy.types.Object, bpy.types.Object]:
    """确保 AI 可视化（目标落点/路径）用的对象存在并挂载 GN。"""

    scene_root = bpy.context.scene.collection
    hud_collection = ensure_collection(HUD_COLLECTION_NAME, parent=scene_root)

    target_obj = ensure_points_object(name=AI_TARGET_POINTS_OBJECT_NAME, collection=hud_collection)
    path_obj = ensure_points_object(name=AI_PATH_POINTS_OBJECT_NAME, collection=hud_collection)

    node_group = bpy.data.node_groups.get(GN_GROUP_NAME) or geo_nodes.ensure_points_to_blocks_node_group()
    default_mat = looks.ensure_attr_color_material()

    # 用户要求 AI 相关 scale 均为 0.1
    ai_scale = (0.1, 0.1, 0.1)

    for obj in (target_obj, path_obj):
        geo_nodes.ensure_geometry_nodes_modifier(
            obj=obj,
            node_group=node_group,
            looks_collection=looks_collection,
            block_scale=ai_scale,
        )
        if default_mat not in list(obj.data.materials):
            obj.data.materials.clear()
            obj.data.materials.append(default_mat)

    return target_obj, path_obj


def _preview_cells_centered(key: str) -> list[tuple[int, int]]:
    """将某个 tetromino 的 spawn 形状归一化并居中到 4x4 预览框。"""

    local = rotated_cells(key, 0)
    min_x, min_z, max_x, max_z = cells_bbox(local)

    norm = [(x - min_x, z - min_z) for (x, z) in local]

    bbox_w = (max_x - min_x) + 1
    bbox_h = (max_z - min_z) + 1

    # 预览框固定为 4x4（兼容 I）。
    off_x = max(0, (4 - int(bbox_w)) // 2)
    off_z = max(0, (4 - int(bbox_h)) // 2)

    return [(int(x) + off_x, int(z) + off_z) for (x, z) in norm]


def sync_hud_from_game(
    *,
    game,
    next_obj: bpy.types.Object,
    hold_obj: bpy.types.Object,
    stats_obj: bpy.types.Object | None = None,
    cell_size: float = CELL_SIZE_DEFAULT,
    origin_corner: tuple[float, float, float] | None = None,
    piece_colors: dict[str, tuple[float, float, float, float]] | None = None,
    next_count: int = 5,
) -> None:
    """将 Hold/Next/Stats 状态写入 HUD 对象。

    Args:
        game: 逻辑层 `TetrisGame`。
        next_obj: Next 预览 points mesh。
        hold_obj: Hold 预览 points mesh。
        stats_obj: Stats 文本 HUD 的承载对象（可选）。
        cell_size: 格子尺寸。
        origin_corner: 棋盘左下角角点原点；None 时自动按棋盘尺寸计算。
        piece_colors: piece_key -> RGBA。
        next_count: Next 预览数量。
    """

    if piece_colors is None:
        piece_colors = {}

    width = int(getattr(game, "width", 0) or 0)
    height = int(getattr(game, "height", 0) or 0)

    if origin_corner is None:
        if width > 0 and height > 0:
            origin_corner = board_origin_corner(width=width, height=height, cell_size=cell_size)
        else:
            origin_corner = (0.0, 0.0, 0.0)

    ox, oy, oz = origin_corner

    # 将 HUD 垂直居中到棋盘中心。
    board_center_z = oz + (float(height) * float(cell_size)) / 2.0

    side_gap = float(cell_size) * 2.0
    box_world_w = float(cell_size) * 4.0

    next_origin_x = ox + float(width) * float(cell_size) + side_gap
    hold_origin_x = ox - box_world_w - side_gap

    hold_origin_z = board_center_z - (4.0 * float(cell_size)) / 2.0

    # Next 竖向堆叠：每块占 4 行 + 1 行间隔。
    n = max(0, int(next_count))
    slot_h = 4 + 1
    stack_h_cells = n * 4 + max(0, n - 1) * 1
    next_origin_z = board_center_z - (float(stack_h_cells) * float(cell_size)) / 2.0

    # -------------------- Hold --------------------

    hold_key = getattr(game, "hold_key", None)
    if hold_key:
        cells = _preview_cells_centered(str(hold_key))
        pts = cells_to_world_points(cells=cells, cell_size=cell_size, origin=(hold_origin_x, oy, hold_origin_z))

        pid = piece_index(str(hold_key))
        col = piece_colors.get(str(hold_key), (1.0, 1.0, 1.0, 1.0))

        set_points(
            obj=hold_obj,
            points_world=pts,
            piece_ids=[pid] * len(pts),
            colors=[col] * len(pts),
        )
    else:
        set_points(obj=hold_obj, points_world=[], piece_ids=[], colors=[])

    # -------------------- Next --------------------

    next_keys: list[str] = []
    peek_next = getattr(game, "peek_next", None)
    if callable(peek_next):
        next_keys = list(peek_next(count=n))

    next_cells: list[tuple[int, int]] = []
    next_piece_ids: list[int] = []
    next_colors: list[tuple[float, float, float, float]] = []

    top_slot_z = max(0, stack_h_cells - 4)

    for i, key in enumerate(next_keys[:n]):
        base_z = top_slot_z - (i * slot_h)
        cells = _preview_cells_centered(str(key))
        pid = piece_index(str(key))
        col = piece_colors.get(str(key), (1.0, 1.0, 1.0, 1.0))

        for (cx, cz) in cells:
            next_cells.append((int(cx), int(cz) + int(base_z)))
            next_piece_ids.append(pid)
            next_colors.append(col)

    pts = cells_to_world_points(cells=next_cells, cell_size=cell_size, origin=(next_origin_x, oy, next_origin_z))
    set_points(obj=next_obj, points_world=pts, piece_ids=next_piece_ids, colors=next_colors)

    if stats_obj is not None:
        level = int(getattr(game, "level", 1) or 1)
        score = int(getattr(game, "score", 0) or 0)

        # 调整 Stats 面板的默认位置与变换（根据用户需求）
        # Target: X=-6m, Y=-2m, Z=12.5m, Rot X=90, Y=0, Z=0, Scale=2.680
        stats_obj.location = (-6.0, 0.0, 12.5)
        # Rot X=90 deg = pi/2
        stats_obj.rotation_euler = (math.pi / 2.0, 0.0, 0.0)
        stats_obj.scale = (2.68, 2.68, 2.68)

        set_points(obj=stats_obj, points_world=[(0.0, 0.0, 0.0)])
        mesh = stats_obj.data
        level_attr = _ensure_mesh_attribute(mesh, name=ATTR_LEVEL_NAME, data_type="INT", domain="POINT")
        score_attr = _ensure_mesh_attribute(mesh, name=ATTR_SCORE_NAME, data_type="INT", domain="POINT")
        for i in range(len(mesh.vertices)):
            level_attr.data[i].value = int(level)
            score_attr.data[i].value = int(score)


def sync_ai_debug(
    *,
    game,
    target_obj: bpy.types.Object,
    path_obj: bpy.types.Object,
    target_rotation: int,
    target_x: int,
    target_z: int,
    cell_size: float = CELL_SIZE_DEFAULT,
    origin_corner: tuple[float, float, float] | None = None,
    piece_colors: dict[str, tuple[float, float, float, float]] | None = None,
    show_path: bool = True,
) -> None:
    """将 AI 目标落点/路径写入 HUD 调试对象。"""

    if piece_colors is None:
        piece_colors = {}

    width = int(getattr(game, "width", 0) or 0)
    height = int(getattr(game, "height", 0) or 0)

    if origin_corner is None:
        if width > 0 and height > 0:
            origin_corner = board_origin_corner(width=width, height=height, cell_size=cell_size)
        else:
            origin_corner = (0.0, 0.0, 0.0)

    current_piece = getattr(game, "current", None)
    if current_piece is None:
        set_points(obj=target_obj, points_world=[], piece_ids=[], colors=[])
        set_points(obj=path_obj, points_world=[], piece_ids=[], colors=[])
        return

    key = str(getattr(current_piece, "key", ""))
    pid = piece_index(key) if key else BORDER_PIECE_INDEX

    base_col = piece_colors.get(key, (1.0, 1.0, 1.0, 1.0))
    ghost_col = (
        min(1.0, float(base_col[0]) * 0.35 + 0.65),
        min(1.0, float(base_col[1]) * 0.35 + 0.65),
        min(1.0, float(base_col[2]) * 0.35 + 0.65),
        1.0,
    )

    system = str(getattr(current_piece, "system", getattr(game, "rotation_system", "SRS")) or "SRS").upper()

    local = rotated_cells(key, int(target_rotation), system=system)
    cells = [(int(target_x) + cx, int(target_z) + cz) for (cx, cz) in local]

    pts = cells_to_world_points(cells=cells, cell_size=cell_size, origin=origin_corner)
    y_off = float(cell_size) * 0.5
    pts = [(x, y + y_off, z) for (x, y, z) in pts]

    set_points(
        obj=target_obj,
        points_world=pts,
        piece_ids=[pid] * len(pts),
        colors=[ghost_col] * len(pts),
    )

    if not show_path:
        set_points(obj=path_obj, points_world=[], piece_ids=[], colors=[])
        return

    cx = int(getattr(current_piece, "x", 0) or 0)
    tx = int(target_x)

    if tx == cx:
        xs = [tx]
    else:
        step = 1 if tx > cx else -1
        xs = list(range(cx, tx + step, step))

    path_cells = [(x, int(target_z)) for x in xs]
    path_pts = cells_to_world_points(cells=path_cells, cell_size=cell_size, origin=origin_corner)
    y_off_path = float(cell_size) * 1.2
    path_pts = [(x, y + y_off_path, z) for (x, y, z) in path_pts]

    set_points(
        obj=path_obj,
        points_world=path_pts,
        piece_ids=[pid] * len(path_pts),
        colors=[ghost_col] * len(path_pts),
    )


def cleanup_runtime() -> None:
    """彻底清理运行时集合（Game + HUD）及其孤立 mesh 数据块。"""

    for coll_name in (GAME_COLLECTION_NAME, HUD_COLLECTION_NAME):
        coll = bpy.data.collections.get(coll_name)
        if coll is None:
            continue

        meshes = {obj.data for obj in coll.objects if obj.type == "MESH"}
        objects = list(coll.objects)

        for obj in objects:
            bpy.data.objects.remove(obj, do_unlink=True)

        # 关键：手动清理孤立的数据块，避免内存泄漏/垃圾堆积。
        for mesh in meshes:
            if mesh.users == 0:
                bpy.data.meshes.remove(mesh)

        bpy.data.collections.remove(coll, do_unlink=True)
