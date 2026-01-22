"""资产生成：在场景里创建 `TetrisAssets`。

`TetrisAssets` 用于存放每种 tetromino 的“形状参考” points mesh：
- 子 collection：`tetrimino_I/O/T/S/Z/J/L`
- 每个子 collection 一个 mesh，对应 4 个顶点
- 顶点坐标表示格子中心（`+0.5*cell_size` 对齐）

这些对象在“游戏运行时”不会直接复制；实时运行使用 `runtime.py` 生成 board/current/border 的点云。
但保留 `TetrisAssets` 有利于：
- 直观看每种块的点位定义是否正确
- 作为 GN/材质/外观调试参考
"""

from __future__ import annotations

import bpy

from ..data.constants import (
    ASSETS_COLLECTION_NAME,
    ASSETS_TETROMINO_PREFIX,
    ATTR_COLOR_NAME,
    ATTR_PIECE_NAME,
    WORLD_Y_LEVEL,
)
from ..core.tetrominoes import TETROMINO_DEFS, piece_index


def ensure_collection(name: str, *, parent: bpy.types.Collection | None = None) -> bpy.types.Collection:
    """确保指定 collection 存在，并在需要时链接到父 collection。

    Args:
        name: collection 名称。
        parent: 如果提供，则确保该 collection 是 parent 的 child。

    Returns:
        `bpy.data.collections[name]`。

    Note:
        Blender 的 collection 是数据块（data-block），同名只会存在一个。
        这里的行为是幂等的：重复调用不会重复创建/重复链接。
    """

    collection = bpy.data.collections.get(name)
    if collection is None:
        collection = bpy.data.collections.new(name)

    if parent is not None:
        # `children.get()` 比 `name in children` 更稳（children 是集合-like）。
        if parent.children.get(collection.name) is None:
            parent.children.link(collection)

    return collection


def _ensure_mesh_attribute(
    mesh: bpy.types.Mesh,
    *,
    name: str,
    data_type: str,
    domain: str,
) -> bpy.types.Attribute:
    """确保 mesh 上存在指定的几何属性（Geometry Attribute）。

    如果同名属性存在但 data_type/domain 不一致，会先删除再创建。

    Args:
        mesh: 目标 mesh。
        name: 属性名。
        data_type: Blender attribute 类型（如 "INT" / "FLOAT_COLOR"）。
        domain: domain（如 "POINT" / "CORNER"）。

    Returns:
        创建/复用后的 `bpy.types.Attribute`。
    """

    attr = mesh.attributes.get(name)
    if attr is not None and (attr.data_type != data_type or attr.domain != domain):
        mesh.attributes.remove(attr)
        attr = None

    if attr is None:
        attr = mesh.attributes.new(name=name, type=data_type, domain=domain)

    return attr


def ensure_points_mesh_object(
    *,
    name: str,
    collection: bpy.types.Collection,
    points_world: list[tuple[float, float, float]],
    shape_key: str,
    cell_size: float,
) -> bpy.types.Object:
    """创建/更新一个只包含顶点的 points mesh 对象。

    该对象的顶点坐标用来表示格子中心点；后续 GN 会将这些点实例化为 cube。

    同时写入点属性：
    - `bltetris_piece`：实例索引
    - `bltetris_color`：默认白色（实际颜色在运行时/回放中会覆盖）

    Args:
        name: 对象名。
        collection: 需要链接到的 collection。
        points_world: 顶点坐标列表（会作为 mesh local 坐标写入）。
        shape_key: I/O/T/S/Z/J/L。
        cell_size: 当前格子大小（用于写入 object 自定义属性）。

    Returns:
        对应的 `bpy.types.Object`。

    Raises:
        TypeError: 同名对象存在但不是 MESH。
    """

    existing_obj = bpy.data.objects.get(name)
    if existing_obj is None:
        mesh = bpy.data.meshes.new(f"{name}_mesh")
        obj = bpy.data.objects.new(name, mesh)
    else:
        obj = existing_obj
        if obj.type != "MESH":
            raise TypeError(f"Object '{name}' exists but is not a MESH")
        mesh = obj.data

    # points mesh：只写顶点，不写边/面。
    mesh.clear_geometry()
    mesh.from_pydata(points_world, [], [])
    mesh.update()

    # piece index 作为点属性写入（每个点一样）。
    pid = piece_index(shape_key)

    piece_attr = _ensure_mesh_attribute(mesh, name=ATTR_PIECE_NAME, data_type="INT", domain="POINT")
    color_attr = _ensure_mesh_attribute(mesh, name=ATTR_COLOR_NAME, data_type="FLOAT_COLOR", domain="POINT")

    for idx in range(len(points_world)):
        piece_attr.data[idx].value = int(pid)
        color_attr.data[idx].color = (1.0, 1.0, 1.0, 1.0)

    # 资产对象一般保持在原点（方便检查）；实际游戏会用 runtime 的点云对象。
    obj.location = (0.0, 0.0, 0.0)
    obj.rotation_euler = (0.0, 0.0, 0.0)
    obj.scale = (1.0, 1.0, 1.0)

    # 写入少量自定义属性，便于调试/升级迁移。
    obj["bltetris_shape"] = shape_key
    obj["bltetris_cell_size"] = float(cell_size)

    if collection.objects.get(obj.name) is None:
        collection.objects.link(obj)

    # 默认隐藏资产对象，避免干扰视口
    # 但由于它在 TetrisAssets 集合中，我们通常隐藏整个集合。
    # 这里保持对象可见，由集合层级控制。
    # 用户反馈: "TetrisAssets 排除这个的显示，避免占据游戏画面"
    # 所以我们在创建集合时直接 exclude。

    return obj


def ensure_tetris_assets(*, cell_size: float) -> bpy.types.Collection:
    """确保 `TetrisAssets` 与所有 tetromino 子 collection / points mesh 都存在。

    Args:
        cell_size: 格子尺寸（用于把 cell 坐标转成实际点位）。

    Returns:
        `TetrisAssets` 根 collection。
    """

    scene_root = bpy.context.scene.collection
    assets_collection = ensure_collection(ASSETS_COLLECTION_NAME, parent=scene_root)

    # 尝试在 View Layer 中排除该集合（Exclude from View Layer）
    # 对应 UI 操作：勾选掉集合前面的复选框
    try:
        layer_collection = bpy.context.view_layer.layer_collection.children.get(assets_collection.name)
        if layer_collection:
            layer_collection.exclude = True
    except Exception:
        pass

    for tet in TETROMINO_DEFS:
        child_name = f"{ASSETS_TETROMINO_PREFIX}{tet.key}"
        tet_collection = ensure_collection(child_name, parent=assets_collection)

        # 对齐到格子中心：x/z 方向各 +0.5 格。
        half = float(cell_size) / 2.0
        points_world = [(x * cell_size + half, WORLD_Y_LEVEL, z * cell_size + half) for (x, z) in tet.cells]

        ensure_points_mesh_object(
            name=f"BLTETRIS_{tet.key}_points",
            collection=tet_collection,
            points_world=points_world,
            shape_key=tet.key,
            cell_size=cell_size,
        )

    return assets_collection
