"""外观系统（Looks）。

为了解耦“游戏逻辑/点云”与“渲染外观”，这里维护一个 `TetrisLooks` collection：
- 每种方块（I/O/T/S/Z/J/L）各一个 look object
- 外框（BORDER）也作为一个 look object

GN 会使用：
- `Collection Info(TetrisLooks)` + `Pick Instance` + `Instance Index(bltetris_piece)`
来选择对应的实例对象。

关键点：
- `Pick Instance` 的索引依赖 collection 内对象顺序，因此每次 ensure 都会
  先 unlink 再按固定顺序 link，保证索引稳定。
- look object 自身通常隐藏（viewport/render），只作为实例源。
"""

from __future__ import annotations

import bpy

from . import geo_nodes
from ..data.constants import ATTR_COLOR_NAME, LOOKS_BORDER_KEY, LOOKS_COLLECTION_NAME
from ..core.tetrominoes import TETROMINO_KEYS


# 默认材质：读取几何属性 `bltetris_color` 作为 Base Color。
MATERIAL_ATTR_COLOR_NAME = "BLTETRIS_AttrColor"

# bevel modifier 名称（每个 look object 上各一个）。
BEVEL_MODIFIER_NAME = "BLTETRIS_Bevel"

# look object 的命名前缀。
LOOK_OBJECT_PREFIX = "BLTETRIS_Look_"

# Ghost 材质名称
MATERIAL_GHOST_NAME = "BLTETRIS_GhostMat"


def ensure_collection(name: str, *, parent: bpy.types.Collection) -> bpy.types.Collection:
    """确保 `name` 对应的 collection 存在，并挂到 parent 下。

    Args:
        name: collection 名。
        parent: 需要作为 parent.children 的 collection。

    Returns:
        对应的 `bpy.types.Collection`。

    Note:
        这里幂等：重复调用不会重复链接。
    """

    collection = bpy.data.collections.get(name)
    if collection is None:
        collection = bpy.data.collections.new(name)

    if parent.children.get(collection.name) is None:
        parent.children.link(collection)

    return collection


def ensure_attr_color_material() -> bpy.types.Material:
    """创建/获取默认材质 `BLTETRIS_AttrColor`。

    材质节点图：
    - Attribute 节点读取 `bltetris_color`
    - 连接到 Principled BSDF 的 Base Color
    """

    mat = bpy.data.materials.get(MATERIAL_ATTR_COLOR_NAME)
    if mat is None:
        mat = bpy.data.materials.new(MATERIAL_ATTR_COLOR_NAME)
    
    mat.use_nodes = True
    # 强制设回默认混合模式，防止被误设为 Ghost 的透明模式
    mat.blend_method = 'OPAQUE'

    nt = mat.node_tree
    nodes = nt.nodes
    links = nt.links

    # 统一重建节点图（避免用户手动改动后带来不确定性）。
    nodes.clear()

    out = nodes.new("ShaderNodeOutputMaterial")
    out.location = (400, 0)

    bsdf = nodes.new("ShaderNodeBsdfPrincipled")
    bsdf.location = (200, 0)

    attr = nodes.new("ShaderNodeAttribute")
    attr.location = (0, 0)
    attr.attribute_name = ATTR_COLOR_NAME

    links.new(attr.outputs.get("Color"), bsdf.inputs.get("Base Color"))
    links.new(bsdf.outputs.get("BSDF"), out.inputs.get("Surface"))

    return mat


def _ensure_look_object(
    *,
    name: str,
    collection: bpy.types.Collection,
    cell_size: float,
) -> bpy.types.Object:
    """确保 look object 存在且 mesh 与 cell_size/mesh_version 匹配。

    这里的 look object 是“实例源”，由 GN `Collection Info` 提供。

    Args:
        name: 对象名。
        collection: 所属 collection（TetrisLooks）。
        cell_size: 当前格子大小（用于重建方块网格）。

    Returns:
        对应的 `bpy.types.Object`。

    Raises:
        TypeError: 同名对象存在但不是 MESH。
    """

    obj = bpy.data.objects.get(name)
    if obj is not None:
        if obj.type != "MESH":
            raise TypeError(f"Object '{name}' exists but is not a MESH")

        existing = float(obj.get("bltetris_cell_size", 0.0) or 0.0)
        mesh_version = int(obj.data.get("bltetris_mesh_version", 0) or 0)

        # cell_size 或 mesh schema 发生变化时重建（避免 bevel 法线问题等）。
        if abs(existing - float(cell_size)) > 1e-6 or mesh_version != geo_nodes.BLOCK_MESH_VERSION:
            geo_nodes._ensure_block_mesh(obj.data, cell_size=float(cell_size))
            obj["bltetris_cell_size"] = float(cell_size)

        if collection.objects.get(obj.name) is None:
            collection.objects.link(obj)

        # 作为实例源通常不需要直接显示。
        obj.hide_viewport = True
        obj.hide_render = True
        return obj

    mesh = bpy.data.meshes.new(f"{name}_mesh")
    geo_nodes._ensure_block_mesh(mesh, cell_size=float(cell_size))

    obj = bpy.data.objects.new(name, mesh)
    obj["bltetris_cell_size"] = float(cell_size)

    collection.objects.link(obj)

    obj.hide_viewport = True
    obj.hide_render = True

    return obj


def _apply_material(obj: bpy.types.Object, material: bpy.types.Material) -> None:
    """把材质赋给 look object（清空并只保留一个材质槽）。

    Args:
        obj: 目标 look 对象。
        material: 要应用的材质。

    Side effects:
        - 会清空并重建 `obj.data.materials` 列表
    """

    if obj.type != "MESH":
        return

    mats = obj.data.materials
    mats.clear()
    mats.append(material)


def _apply_bevel(obj: bpy.types.Object, *, width: float, segments: int) -> None:
    """在 look object 上设置 Bevel Modifier。

    Args:
        obj: 目标对象。
        width: 倒角宽度。
        segments: 倒角段数。

    Note:
        - `limit_method=NONE` 让倒角更“像圆角方块”。
        - profile 轻微偏圆（0.7）。
    """

    bevel = obj.modifiers.get(BEVEL_MODIFIER_NAME)
    if bevel is None:
        bevel = obj.modifiers.new(name=BEVEL_MODIFIER_NAME, type="BEVEL")

    bevel.width = float(width)
    bevel.segments = int(segments)

    # 属性在不同 Blender 版本可能略有差异，所以用 hasattr 防御。
    if hasattr(bevel, "limit_method"):
        bevel.limit_method = "NONE"
    if hasattr(bevel, "profile"):
        bevel.profile = 0.7


def ensure_ghost_material() -> bpy.types.Material:
    """创建/获取 Ghost 专用材质。

    样式：Blended, Alpha 0.01, Emission Strength 0.1。
    """

    mat = bpy.data.materials.get(MATERIAL_GHOST_NAME)
    if mat is None:
        mat = bpy.data.materials.new(MATERIAL_GHOST_NAME)
        
    mat.use_nodes = True
    mat.blend_method = 'BLEND'

    nt = mat.node_tree
    nodes = nt.nodes
    links = nt.links
    nodes.clear()

    out = nodes.new("ShaderNodeOutputMaterial")
    out.location = (400, 0)

    bsdf = nodes.new("ShaderNodeBsdfPrincipled")
    bsdf.location = (200, 0)
    
    # 设置基础参数
    if hasattr(bsdf, "inputs"):
        # Alpha 0.01
        alpha_in = bsdf.inputs.get("Alpha")
        if alpha_in: alpha_in.default_value = 0.01
        
        # Emission Strength 0.1
        em_strength_in = bsdf.inputs.get("Emission Strength")
        if em_strength_in: em_strength_in.default_value = 0.1

        # 设置默认颜色为白色
        base_color_in = bsdf.inputs.get("Base Color")
        if base_color_in: base_color_in.default_value = (1.0, 1.0, 1.0, 1.0)
        
        em_color_in = bsdf.inputs.get("Emission Color") or bsdf.inputs.get("Emission")
        if em_color_in:
            if em_color_in.type == 'RGBA':
                em_color_in.default_value = (1.0, 1.0, 1.0, 1.0)

    links.new(bsdf.outputs.get("BSDF"), out.inputs.get("Surface"))

    return mat


def ensure_looks(*, settings) -> bpy.types.Collection:
    """确保 `TetrisLooks` 外观库存在且与当前 settings 同步。

    settings 提供：
    - 每个 piece 的材质/倒角参数
    - border 的材质/倒角参数

    Returns:
        `TetrisLooks` collection。
    """

    scene_root = bpy.context.scene.collection
    looks_collection = ensure_collection(LOOKS_COLLECTION_NAME, parent=scene_root)

    default_material = ensure_attr_color_material()

    # 固定顺序：I/O/T/S/Z/J/L/BORDER
    ordered_keys = tuple(TETROMINO_KEYS) + (LOOKS_BORDER_KEY,)
    objects_in_order: list[bpy.types.Object] = []

    for key in ordered_keys:
        obj_name = f"{LOOK_OBJECT_PREFIX}{key}"
        obj = _ensure_look_object(
            name=obj_name,
            collection=looks_collection,
            cell_size=float(settings.cell_size),
        )

        if key == LOOKS_BORDER_KEY:
            # 边框：使用 border_* 参数
            material = getattr(settings, "border_material", None) or default_material
            _apply_material(obj, material)
            _apply_bevel(
                obj,
                width=float(getattr(settings, "border_bevel_width", 0.0)),
                segments=int(getattr(settings, "border_bevel_segments", 0)),
            )
        else:
            override = bool(getattr(settings, f"piece_override_style_{key}", False))

            if override:
                material = getattr(settings, f"material_{key}", None) or default_material
                bevel_width = float(getattr(settings, f"bevel_width_{key}", 0.0))
                bevel_segments = int(getattr(settings, f"bevel_segments_{key}", 0))
            else:
                material = getattr(settings, "pieces_material", None) or default_material
                bevel_width = float(getattr(settings, "pieces_bevel_width", 0.0))
                bevel_segments = int(getattr(settings, "pieces_bevel_segments", 0))

            _apply_material(obj, material)
            _apply_bevel(
                obj,
                width=bevel_width,
                segments=bevel_segments,
            )

        objects_in_order.append(obj)

    # Enforce stable order for Collection Info / Pick Instance.
    # 关键：Pick Instance 的索引完全依赖 collection.objects 的顺序。
    for obj in objects_in_order:
        if looks_collection.objects.get(obj.name) is not None:
            looks_collection.objects.unlink(obj)

    for obj in objects_in_order:
        looks_collection.objects.link(obj)

    return looks_collection
