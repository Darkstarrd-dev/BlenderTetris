"""Geometry Nodes 生成与修复。

这里用 Python API 动态创建 GN node group（避免手工搭节点）：
- `BLTETRIS_PointsToBlocks`：实时实例化（board/current/border）
- `BLTETRIS_ReplayPointsToBlocks`：回放实例化（带 `Replay Index` 过滤）

兼容性注意：
- Blender 5.1 的 `Collection Info` 输出口是 `Instances`（不是 `Geometry`）。
- 节点 socket 名可能漂移，所以经常要 `inputs.get(name) or inputs[index]`。
- 本文件有 schema version，自愈重建“被破坏的 node group”。
"""

from __future__ import annotations

import bpy

from ..data.constants import (
    ASSETS_COLLECTION_NAME,
    ATTR_COLOR_NAME,
    ATTR_FRAME_NAME,
    ATTR_LEVEL_NAME,
    ATTR_PIECE_NAME,
    ATTR_SCALE_NAME,
    ATTR_SCORE_NAME,
    BLOCK_OBJECT_NAME,
    GN_GROUP_NAME,
    GN_MODIFIER_NAME,
    GHOST_GN_GROUP_NAME,
    REPLAY_GN_GROUP_NAME,
    REPLAY_GN_MODIFIER_NAME,
    STATS_TEXT_GN_GROUP_NAME,
    STATS_TEXT_GN_MODIFIER_NAME,
)



# GN 节点图版本，若发生结构变更则增加此值以触发自动重建
GN_SCHEMA_VERSION = 5

# 基础方块网格版本，用于法向修复等 mesh 变更
BLOCK_MESH_VERSION = 2


def _ensure_block_mesh(mesh: bpy.types.Mesh, *, cell_size: float) -> None:
    """内部工具：为指定的 Mesh 数据块生成标准的 1x1x1 立方体顶点和面。

    Args:
        mesh: 目标 Mesh 块。
        cell_size: 格子大小。
    """

    half = float(cell_size) / 2.0

    # 顶点定义（以局部原点为中心）
    verts = [
        (-half, -half, -half),
        (half, -half, -half),
        (half, half, -half),
        (-half, half, -half),
        (-half, -half, half),
        (half, -half, half),
        (half, half, half),
        (-half, half, half),
    ]

    # 面定义：注意 Winding Order，顺时针/逆时针决定法向
    faces = [
        # 底面（修正后的顺序，确保法向朝外）
        (0, 3, 2, 1),
        # 顶面
        (4, 5, 6, 7),
        # 前面
        (0, 1, 5, 4),
        # 右面
        (1, 2, 6, 5),
        # 后面
        (2, 3, 7, 6),
        # 左面
        (3, 0, 4, 7),
    ]

    mesh.clear_geometry()
    mesh.from_pydata(verts, [], faces)
    mesh.update()

    # 写入版本号，便于后续自愈
    mesh["bltetris_mesh_version"] = BLOCK_MESH_VERSION


def ensure_default_block_object(*, cell_size: float) -> bpy.types.Object:
    """确保全局通用的基础方块对象存在（用于实例源）。

    Args:
        cell_size: 格子尺寸。

    Returns:
        `BLTETRIS_Block` 对象。

    Raises:
        TypeError: 同名对象存在但不是 MESH。
    """

    obj = bpy.data.objects.get(BLOCK_OBJECT_NAME)
    if obj is not None:
        if obj.type != "MESH":
            raise TypeError(f"Object '{BLOCK_OBJECT_NAME}' exists but is not a MESH")

        # 检查是否需要根据 cell_size 或版本变化重新生成 mesh
        existing = float(obj.get("bltetris_cell_size", 0.0) or 0.0)
        mesh_version = int(obj.data.get("bltetris_mesh_version", 0) or 0)

        if abs(existing - float(cell_size)) > 1e-6 or mesh_version != BLOCK_MESH_VERSION:
            _ensure_block_mesh(obj.data, cell_size=float(cell_size))
            obj["bltetris_cell_size"] = float(cell_size)

        # 始终设置为隐藏
        obj.hide_viewport = True
        obj.hide_render = True
        return obj

    # 创建全新对象
    mesh = bpy.data.meshes.new(f"{BLOCK_OBJECT_NAME}_mesh")
    _ensure_block_mesh(mesh, cell_size=float(cell_size))

    obj = bpy.data.objects.new(BLOCK_OBJECT_NAME, mesh)
    obj["bltetris_cell_size"] = float(cell_size)

    # 默认放入 Assets 集合
    target_collection = bpy.data.collections.get(ASSETS_COLLECTION_NAME)
    if target_collection is None:
        target_collection = bpy.context.scene.collection

    if target_collection.objects.get(obj.name) is None:
        target_collection.objects.link(obj)

    obj.hide_viewport = True
    obj.hide_render = True

    return obj


def _has_group_socket(node_group: bpy.types.NodeTree, *, name: str, in_out: str) -> bool:
    """内部工具：检查节点组接口是否存在指定名称的 socket。

    Blender 4.0+ 使用 interface 系统，旧版直接访问 inputs/outputs。

    Args:
        node_group: 目标节点组。
        name: socket 名称。
        in_out: "INPUT" 或 "OUTPUT"。

    Returns:
        如果存在该 socket 返回 True，否则 False。
    """

    if hasattr(node_group, "interface"):
        for item in node_group.interface.items_tree:
            if getattr(item, "item_type", None) != "SOCKET":
                continue
            if getattr(item, "in_out", None) != in_out:
                continue
            if item.name == name:
                return True
        return False

    if in_out == "INPUT":
        return node_group.inputs.get(name) is not None
    if in_out == "OUTPUT":
        return node_group.outputs.get(name) is not None
    return False


def _find_group_node(node_group: bpy.types.NodeTree, *, node_type: str) -> bpy.types.Node | None:
    """内部工具：查找节点组中指定类型的节点。

    Args:
        node_group: 目标节点组。
        node_type: `node.type` 期望值（如 "GROUP_OUTPUT"）。

    Returns:
        找到则返回对应节点，否则返回 None。
    """

    for node in node_group.nodes:
        if getattr(node, "type", None) == node_type:
            return node
    return None


def _has_required_nodes(node_group: bpy.types.NodeTree) -> bool:
    """内部工具：验证节点组内部是否包含必要的节点类。

    Args:
        node_group: 目标节点组。

    Returns:
        满足要求返回 True，否则 False。
    """

    required = {
        "NodeGroupInput",
        "NodeGroupOutput",
        "GeometryNodeMeshToPoints",
        "GeometryNodeCollectionInfo",
        "GeometryNodeInstanceOnPoints",
    }

    present = {n.bl_idname for n in node_group.nodes}
    return required.issubset(present)


def _is_points_to_blocks_group_valid(node_group: bpy.types.NodeTree) -> bool:
    """内部逻辑：判断现有的“实时点云到方块”节点组是否可用。

    如果不可用（例如被用户手动删了节点或版本过旧），则返回 False 触发重建。

    Args:
        node_group: 目标节点组。

    Returns:
        节点组可用返回 True，否则 False。
    """

    version = int(node_group.get("bltetris_schema_version", 0) or 0)
    if version != GN_SCHEMA_VERSION:
        return False

    # 检查接口完整性
    has_looks = _has_group_socket(node_group, name="Looks Collection", in_out="INPUT")
    has_scale = _has_group_socket(node_group, name="Block Scale", in_out="INPUT")
    if not (has_looks and has_scale):
        return False

    # 检查核心节点存在性
    if not _has_required_nodes(node_group):
        return False

    # 检查输出端是否已连线
    output_node = _find_group_node(node_group, node_type="GROUP_OUTPUT")
    if output_node is None:
        return False

    if not output_node.inputs:
        return False

    geom_input = output_node.inputs.get("Geometry") or output_node.inputs[0]
    return bool(getattr(geom_input, "is_linked", False))


def _rename_to_legacy(node_group: bpy.types.NodeTree) -> None:
    """内部工具：将损坏/过旧的节点组重命名，腾出名称空间供重建。

    Args:
        node_group: 目标节点组。

    Side effects:
        - 会修改 `node_group.name`
    """

    legacy_base = f"{GN_GROUP_NAME}_legacy"
    legacy_name = legacy_base
    suffix = 2
    while bpy.data.node_groups.get(legacy_name) is not None:
        legacy_name = f"{legacy_base}_{suffix}"
        suffix += 1
    node_group.name = legacy_name


def ensure_points_to_blocks_node_group() -> bpy.types.NodeTree:
    """确保实时点云实例化用的 GeometryNodeTree 存在且连线正确。

    核心逻辑：
    Input -> MeshToPoints -> InstanceOnPoints(Pick Instance from LooksColl) -> Realize -> StoreNamedAttr(Color) -> Output

    Returns:
        可用的 `bpy.types.NodeTree`（名为 `BLTETRIS_PointsToBlocks`）。

    Raises:
        RuntimeError: 节点连接时发现关键 socket 缺失。
    """

    existing = bpy.data.node_groups.get(GN_GROUP_NAME)
    if existing is not None and _is_points_to_blocks_group_valid(existing):
        return existing

    # 自动重命名并重建
    if existing is not None:
        _rename_to_legacy(existing)

    node_group = bpy.data.node_groups.new(GN_GROUP_NAME, "GeometryNodeTree")
    node_group["bltetris_schema_version"] = GN_SCHEMA_VERSION

    # 1. 建立接口 (Socket)
    if hasattr(node_group, "interface"):
        interface = node_group.interface
        interface.new_socket(name="Geometry", in_out="INPUT", socket_type="NodeSocketGeometry")
        interface.new_socket(name="Looks Collection", in_out="INPUT", socket_type="NodeSocketCollection")
        scale_socket = interface.new_socket(name="Block Scale", in_out="INPUT", socket_type="NodeSocketVector")
        interface.new_socket(name="Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
        if hasattr(scale_socket, "default_value"):
            scale_socket.default_value = (1.0, 1.0, 1.0)
    else:
        node_group.inputs.new("NodeSocketGeometry", "Geometry")
        node_group.inputs.new("NodeSocketCollection", "Looks Collection")
        scale_socket = node_group.inputs.new("NodeSocketVector", "Block Scale")
        node_group.outputs.new("NodeSocketGeometry", "Geometry")
        scale_socket.default_value = (1.0, 1.0, 1.0)

    nodes = node_group.nodes
    links = node_group.links

    # 2. 创建内部节点
    input_node = nodes.new("NodeGroupInput")
    input_node.location = (-900, 0)

    output_node = nodes.new("NodeGroupOutput")
    output_node.location = (900, 0)

    mesh_to_points = nodes.new("GeometryNodeMeshToPoints")
    mesh_to_points.location = (-650, 0)
    if hasattr(mesh_to_points, "mode"):
        mesh_to_points.mode = "VERTICES"

    # 读取 Piece Index 属性
    named_piece = nodes.new("GeometryNodeInputNamedAttribute")
    named_piece.location = (-650, -220)
    if hasattr(named_piece, "data_type"):
        named_piece.data_type = "INT"
    name_sock = named_piece.inputs.get("Name") or named_piece.inputs[0]
    name_sock.default_value = ATTR_PIECE_NAME

    # 读取 Color 属性
    named_color = nodes.new("GeometryNodeInputNamedAttribute")
    named_color.location = (-650, -420)
    if hasattr(named_color, "data_type"):
        named_color.data_type = "FLOAT_COLOR"
    name_sock = named_color.inputs.get("Name") or named_color.inputs[0]
    name_sock.default_value = ATTR_COLOR_NAME

    # 新增：读取 Scale 属性
    named_scale = nodes.new("GeometryNodeInputNamedAttribute")
    named_scale.location = (-650, -620)
    if hasattr(named_scale, "data_type"):
        named_scale.data_type = "FLOAT"
    name_sock = named_scale.inputs.get("Name") or named_scale.inputs[0]
    name_sock.default_value = ATTR_SCALE_NAME

    # 新增：缩放混合 (Input Block Scale * Attribute Scale)
    math_scale = nodes.new("ShaderNodeVectorMath")
    math_scale.operation = "MULTIPLY"
    math_scale.location = (-300, -200)

    collection_info = nodes.new("GeometryNodeCollectionInfo")
    collection_info.location = (-400, 220)
    if hasattr(collection_info, "separate_children"):
        collection_info.separate_children = True
    if hasattr(collection_info, "reset_children"):
        collection_info.reset_children = True

    instance_on_points = nodes.new("GeometryNodeInstanceOnPoints")
    instance_on_points.location = (0, 0)
    if hasattr(instance_on_points, "pick_instance"):
        instance_on_points.pick_instance = True

    # Realize 是必须的，否则材质无法读取 Named Attribute。
    realize = nodes.new("GeometryNodeRealizeInstances")
    realize.location = (300, 0)

    store_color = nodes.new("GeometryNodeStoreNamedAttribute")
    store_color.location = (550, 0)
    if hasattr(store_color, "data_type"):
        store_color.data_type = "FLOAT_COLOR"
    if hasattr(store_color, "domain"):
        store_color.domain = "POINT"

    # 3. 定义辅助连线函数 (Socket Fallback)
    def _out(node: bpy.types.Node, name: str) -> bpy.types.NodeSocket:
        sock = node.outputs.get(name)
        if sock is None:
            raise RuntimeError(f"{node.bl_idname} missing output '{name}'; outputs={[s.name for s in node.outputs]}")
        return sock

    def _in(node: bpy.types.Node, name: str) -> bpy.types.NodeSocket:
        sock = node.inputs.get(name)
        if sock is None:
            raise RuntimeError(f"{node.bl_idname} missing input '{name}'; inputs={[s.name for s in node.inputs]}")
        return sock

    # 4. 执行连线
    links.new(_out(input_node, "Geometry"), _in(mesh_to_points, "Mesh"))

    # CHANGED: Direct connect MeshToPoints -> InstanceOnPoints (no capture)
    links.new(_out(mesh_to_points, "Points"), _in(instance_on_points, "Points"))
    
    # 兼容 Blender 5.1 的 Instances 输出
    coll_out = (collection_info.outputs.get("Instances") or 
                collection_info.outputs.get("Geometry") or 
                collection_info.outputs[0])
    links.new(coll_out, _in(instance_on_points, "Instance"))

    # 设置实例索引和缩放
    links.new(_out(named_piece, "Attribute"), _in(instance_on_points, "Instance Index"))
    
    # CHANGED: Multiply Block Scale by Attribute Scale
    links.new(_out(input_node, "Block Scale"), math_scale.inputs[0])
    links.new(_out(named_scale, "Attribute"), math_scale.inputs[1])
    links.new(math_scale.outputs[0], _in(instance_on_points, "Scale"))

    # 实例化后还原颜色属性
    links.new(_out(instance_on_points, "Instances"), _in(realize, "Geometry"))
    links.new(_out(realize, "Geometry"), _in(store_color, "Geometry"))

    store_name_in = store_color.inputs.get("Name") or store_color.inputs[1]
    store_name_in.default_value = ATTR_COLOR_NAME
    
    # CHANGED: Direct connect NamedAttr -> Store (no capture)
    store_val_in = store_color.inputs.get("Value") or store_color.inputs[2] if len(store_color.inputs)>2 else store_color.inputs[-1]
    links.new(_out(named_color, "Attribute"), store_val_in)

    # 最终输出
    links.new(_out(store_color, "Geometry"), _in(output_node, "Geometry"))
    links.new(_out(input_node, "Looks Collection"), _in(collection_info, "Collection"))

    return node_group


def _get_input_identifier(node_group: bpy.types.NodeTree, socket_name: str) -> str | None:
    """内部工具：获取指定名称的输入 Socket 的唯一标识符（Identifier）。

    Modifier 属性访问需要此 Identifier。

    Args:
        node_group: 目标节点组。
        socket_name: 输入 socket 的名称（UI 展示名）。

    Returns:
        identifier 字符串；如果找不到则返回 None。
    """

    if hasattr(node_group, "interface"):
        for item in node_group.interface.items_tree:
            if getattr(item, "item_type", None) != "SOCKET":
                continue
            if getattr(item, "in_out", None) != "INPUT":
                continue
            if item.name == socket_name:
                return item.identifier
        return None

    sock = node_group.inputs.get(socket_name)
    if sock is None:
        return None
    return sock.identifier


def ensure_geometry_nodes_modifier(
    *,
    obj: bpy.types.Object,
    node_group: bpy.types.NodeTree,
    looks_collection: bpy.types.Collection,
    block_scale: tuple[float, float, float] = (1.0, 1.0, 1.0),
) -> bpy.types.NodesModifier:
    """确保对象上挂载了 GN Modifier，并填入正确的输入参数。

    Args:
        obj: 目标对象。
        node_group: 使用的 GN NodeTree。
        looks_collection: 外观实例来源。
        block_scale: 统一缩放系数。

    Returns:
        挂载并配置好的 `bpy.types.NodesModifier`。

    Raises:
        TypeError: 同名 modifier 存在但不是 Geometry Nodes modifier。
    """

    modifier = obj.modifiers.get(GN_MODIFIER_NAME)
    if modifier is None:
        modifier = obj.modifiers.new(name=GN_MODIFIER_NAME, type="NODES")

    if modifier.type != "NODES":
        raise TypeError(
            f"Modifier '{GN_MODIFIER_NAME}' exists on '{obj.name}' but is not a Geometry Nodes modifier"
        )

    # 绑定节点组
    modifier.node_group = node_group

    # 填入 Input 参数（必须使用 identifier）
    looks_id = _get_input_identifier(node_group, "Looks Collection")
    if looks_id is not None:
        modifier[looks_id] = looks_collection

    scale_id = _get_input_identifier(node_group, "Block Scale")
    if scale_id is not None:
        modifier[scale_id] = tuple(float(v) for v in block_scale)

    return modifier


def setup_geometry_nodes_for_assets(
    *,
    looks_collection: bpy.types.Collection,
    block_scale: tuple[float, float, float] = (1.0, 1.0, 1.0),
) -> None:
    """为 `TetrisAssets` 里的所有参考块批量初始化 GN。

    主要用于开发调试，让资产集合里的预览点也能显示为方块。

    Args:
        looks_collection: 外观实例来源。
        block_scale: 实例缩放。

    Raises:
        RuntimeError: 资产集合不存在（需要先 Setup Assets）。
    """

    assets_collection = bpy.data.collections.get(ASSETS_COLLECTION_NAME)
    if assets_collection is None:
        raise RuntimeError(f"Missing collection '{ASSETS_COLLECTION_NAME}'. Run Setup Assets first.")

    node_group = ensure_points_to_blocks_node_group()

    points_objects: list[bpy.types.Object] = []
    for child in assets_collection.children:
        for obj in child.objects:
            if obj.get("bltetris_shape") is None:
                continue
            if obj.type != "MESH":
                continue
            points_objects.append(obj)

    for obj in points_objects:
        ensure_geometry_nodes_modifier(
            obj=obj,
            node_group=node_group,
            looks_collection=looks_collection,
            block_scale=block_scale,
        )


REPLAY_GN_SCHEMA_VERSION = 3


def _rename_group_to_legacy(node_group: bpy.types.NodeTree, *, base_name: str) -> None:
    """内部工具：重命名重名的节点组。

    Args:
        node_group: 目标节点组。
        base_name: 期望的基础名称（会添加 `_legacy` 后缀）。

    Side effects:
        - 会修改 `node_group.name`
    """

    legacy_base = f"{base_name}_legacy"
    legacy_name = legacy_base
    suffix = 2
    while bpy.data.node_groups.get(legacy_name) is not None:
        legacy_name = f"{legacy_base}_{suffix}"
        suffix += 1
    node_group.name = legacy_name


def _is_replay_group_valid(node_group: bpy.types.NodeTree) -> bool:
    """回放节点组合法性校验。

    Args:
        node_group: 目标节点组。

    Returns:
        节点组可用返回 True，否则 False。
    """

    version = int(node_group.get("bltetris_schema_version", 0) or 0)
    if version != REPLAY_GN_SCHEMA_VERSION:
        return False

    has_looks = _has_group_socket(node_group, name="Looks Collection", in_out="INPUT")
    has_scale = _has_group_socket(node_group, name="Block Scale", in_out="INPUT")
    has_index = _has_group_socket(node_group, name="Replay Index", in_out="INPUT")
    if not (has_looks and has_scale and has_index):
        return False

    output_node = _find_group_node(node_group, node_type="GROUP_OUTPUT")
    if output_node is None or not output_node.inputs:
        return False

    geom_input = output_node.inputs.get("Geometry") or output_node.inputs[0]
    return bool(getattr(geom_input, "is_linked", False))


def ensure_replay_points_to_blocks_node_group() -> bpy.types.NodeTree:
    """确保“回放专用”节点组存在。

    逻辑：
    Input(Geometry, Replay Index) -> Separate Geometry( (Frame == Index) OR (Frame == -1) ) ->
    MeshToPoints -> InstanceOnPoints ... (后续同实时实例化)

    Returns:
        可用的 `bpy.types.NodeTree`（名为 `BLTETRIS_ReplayPointsToBlocks`）。
    """

    existing = bpy.data.node_groups.get(REPLAY_GN_GROUP_NAME)
    if existing is not None and _is_replay_group_valid(existing):
        return existing

    if existing is not None:
        _rename_group_to_legacy(existing, base_name=REPLAY_GN_GROUP_NAME)

    node_group = bpy.data.node_groups.new(REPLAY_GN_GROUP_NAME, "GeometryNodeTree")
    node_group["bltetris_schema_version"] = REPLAY_GN_SCHEMA_VERSION

    # 1. 接口
    if hasattr(node_group, "interface"):
        interface = node_group.interface
        interface.new_socket(name="Geometry", in_out="INPUT", socket_type="NodeSocketGeometry")
        interface.new_socket(name="Looks Collection", in_out="INPUT", socket_type="NodeSocketCollection")
        scale_socket = interface.new_socket(name="Block Scale", in_out="INPUT", socket_type="NodeSocketVector")
        interface.new_socket(name="Replay Index", in_out="INPUT", socket_type="NodeSocketInt")
        interface.new_socket(name="Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
        if hasattr(scale_socket, "default_value"):
            scale_socket.default_value = (1.0, 1.0, 1.0)
    else:
        node_group.inputs.new("NodeSocketGeometry", "Geometry")
        node_group.inputs.new("NodeSocketCollection", "Looks Collection")
        scale_socket = node_group.inputs.new("NodeSocketVector", "Block Scale")
        node_group.inputs.new("NodeSocketInt", "Replay Index")
        node_group.outputs.new("NodeSocketGeometry", "Geometry")
        scale_socket.default_value = (1.0, 1.0, 1.0)

    nodes = node_group.nodes
    links = node_group.links

    # 2. 节点
    input_node = nodes.new("NodeGroupInput")
    input_node.location = (-1100, 0)

    output_node = nodes.new("NodeGroupOutput")
    output_node.location = (900, 0)

    # 读取点自带的 bltetris_frame 属性
    named_frame = nodes.new("GeometryNodeInputNamedAttribute")
    named_frame.location = (-1100, -200)
    if hasattr(named_frame, "data_type"):
        named_frame.data_type = "INT"
    name_sock = named_frame.inputs.get("Name") or named_frame.inputs[0]
    name_sock.default_value = ATTR_FRAME_NAME

    # 核心：帧过滤（当前录制步数 == Replay Index）
    compare_index = nodes.new("FunctionNodeCompare")
    compare_index.location = (-850, -200)
    if hasattr(compare_index, "data_type"):
        compare_index.data_type = "INT"
    if hasattr(compare_index, "operation"):
        compare_index.operation = "EQUAL"

    # 新增：始终显示过滤 (Frame == -1)
    compare_always = nodes.new("FunctionNodeCompare")
    compare_always.location = (-850, -400)
    if hasattr(compare_always, "data_type"):
        compare_always.data_type = "INT"
    if hasattr(compare_always, "operation"):
        compare_always.operation = "EQUAL"
    (compare_always.inputs.get("B") or compare_always.inputs[1]).default_value = -1

    # OR 逻辑
    boolean_math = nodes.new("FunctionNodeBooleanMath")
    boolean_math.location = (-600, -200)
    boolean_math.operation = "OR"

    separate = nodes.new("GeometryNodeSeparateGeometry")
    separate.location = (-400, 0)
    if hasattr(separate, "domain"):
        separate.domain = "POINT"

    # 后续实例化节点（逻辑同实时版）
    mesh_to_points = nodes.new("GeometryNodeMeshToPoints")
    mesh_to_points.location = (-150, 0)
    
    named_piece = nodes.new("GeometryNodeInputNamedAttribute")
    named_piece.location = (-150, -200)
    if hasattr(named_piece, "data_type"):
        named_piece.data_type = "INT"
    named_piece.inputs.get("Name", named_piece.inputs[0]).default_value = ATTR_PIECE_NAME

    named_color = nodes.new("GeometryNodeInputNamedAttribute")
    named_color.location = (-150, -400)
    if hasattr(named_color, "data_type"):
        named_color.data_type = "FLOAT_COLOR"
    named_color.inputs.get("Name", named_color.inputs[0]).default_value = ATTR_COLOR_NAME

    # 新增：读取 Scale 属性
    named_scale = nodes.new("GeometryNodeInputNamedAttribute")
    named_scale.location = (-150, -600)
    if hasattr(named_scale, "data_type"):
        named_scale.data_type = "FLOAT"
    named_scale.inputs.get("Name", named_scale.inputs[0]).default_value = ATTR_SCALE_NAME

    # 新增：缩放混合
    math_scale = nodes.new("ShaderNodeVectorMath")
    math_scale.operation = "MULTIPLY"
    math_scale.location = (100, -200)

    collection_info = nodes.new("GeometryNodeCollectionInfo")
    collection_info.location = (100, 220)
    if hasattr(collection_info, "separate_children"):
        collection_info.separate_children = True
    if hasattr(collection_info, "reset_children"):
        collection_info.reset_children = True

    instance_on_points = nodes.new("GeometryNodeInstanceOnPoints")
    instance_on_points.location = (350, 0)
    if hasattr(instance_on_points, "pick_instance"):
        instance_on_points.pick_instance = True

    realize = nodes.new("GeometryNodeRealizeInstances")
    realize.location = (600, 0)

    store_color = nodes.new("GeometryNodeStoreNamedAttribute")
    store_color.location = (750, 0)
    if hasattr(store_color, "data_type"):
        store_color.data_type = "FLOAT_COLOR"

    # 3. 定义辅助 Socket 工具
    def _out(node: bpy.types.Node, name: str) -> bpy.types.NodeSocket:
        """从 outputs 获取 socket，必要时回退到第 0 个。"""
        sock = node.outputs.get(name)
        return sock if sock is not None else node.outputs[0]

    def _in(node: bpy.types.Node, name: str, fallback_index: int) -> bpy.types.NodeSocket:
        """从 inputs 获取 socket，必要时回退到指定索引。"""
        sock = node.inputs.get(name)
        return sock if sock is not None else node.inputs[fallback_index]

    # 4. 连线：首先执行过滤逻辑
    links.new(_out(named_frame, "Attribute"), _in(compare_index, "A", 0))
    links.new(_out(input_node, "Replay Index"), _in(compare_index, "B", 1))

    links.new(_out(named_frame, "Attribute"), _in(compare_always, "A", 0))

    links.new(_out(compare_index, "Result"), boolean_math.inputs[0])
    links.new(_out(compare_always, "Result"), boolean_math.inputs[1])

    links.new(_out(input_node, "Geometry"), _in(separate, "Geometry", 0))
    links.new(_out(boolean_math, "Boolean"), _in(separate, "Selection", 1))

    # 过滤出的点再转成 Points Domain（确保鲁棒性）
    selection_out = separate.outputs.get("Selection") or separate.outputs[0]
    links.new(selection_out, _in(mesh_to_points, "Mesh", 0))

    # 后续实例化逻辑（同实时版）
    links.new(_out(mesh_to_points, "Points"), _in(instance_on_points, "Points", 0))

    coll_out = (collection_info.outputs.get("Instances") or 
                collection_info.outputs.get("Geometry") or 
                collection_info.outputs[0])
    links.new(coll_out, _in(instance_on_points, "Instance", 2))

    links.new(_out(named_piece, "Attribute"), _in(instance_on_points, "Instance Index", 4))
    
    # CHANGED: Multiply Block Scale by Attribute Scale
    links.new(_out(input_node, "Block Scale"), math_scale.inputs[0])
    links.new(_out(named_scale, "Attribute"), math_scale.inputs[1])
    links.new(math_scale.outputs[0], _in(instance_on_points, "Scale", 6))

    links.new(_out(instance_on_points, "Instances"), _in(realize, "Geometry", 0))
    links.new(_out(realize, "Geometry"), _in(store_color, "Geometry", 0))

    store_name_in = store_color.inputs.get("Name") or store_color.inputs[1]
    store_name_in.default_value = ATTR_COLOR_NAME

    store_val_in = store_color.inputs.get("Value") or store_color.inputs[2] if len(store_color.inputs)>2 else store_color.inputs[-1]
    links.new(_out(named_color, "Attribute"), store_val_in)

    links.new(_out(store_color, "Geometry"), _in(output_node, "Geometry", 0))
    links.new(_out(input_node, "Looks Collection"), _in(collection_info, "Collection", 0))

    return node_group


STATS_TEXT_GN_SCHEMA_VERSION = 4


def _is_stats_text_group_valid(node_group: bpy.types.NodeTree) -> bool:
    version = int(node_group.get("bltetris_schema_version", 0) or 0)
    if version != STATS_TEXT_GN_SCHEMA_VERSION:
        return False

    has_geom_in = _has_group_socket(node_group, name="Geometry", in_out="INPUT")
    has_geom_out = _has_group_socket(node_group, name="Geometry", in_out="OUTPUT")
    if not (has_geom_in and has_geom_out):
        return False

    output_node = _find_group_node(node_group, node_type="GROUP_OUTPUT")
    if output_node is None or not output_node.inputs:
        return False

    geom_input = output_node.inputs.get("Geometry") or output_node.inputs[0]
    return bool(getattr(geom_input, "is_linked", False))


def ensure_stats_text_node_group() -> bpy.types.NodeTree:
    """确保 HUD 文本（level/score）用的节点组存在。"""

    existing = bpy.data.node_groups.get(STATS_TEXT_GN_GROUP_NAME)
    if existing is not None and _is_stats_text_group_valid(existing):
        return existing

    if existing is not None:
        _rename_group_to_legacy(existing, base_name=STATS_TEXT_GN_GROUP_NAME)

    node_group = bpy.data.node_groups.new(STATS_TEXT_GN_GROUP_NAME, "GeometryNodeTree")
    node_group["bltetris_schema_version"] = STATS_TEXT_GN_SCHEMA_VERSION

    if hasattr(node_group, "interface"):
        interface = node_group.interface
        interface.new_socket(name="Geometry", in_out="INPUT", socket_type="NodeSocketGeometry")
        interface.new_socket(name="Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
    else:
        node_group.inputs.new("NodeSocketGeometry", "Geometry")
        node_group.outputs.new("NodeSocketGeometry", "Geometry")

    nodes = node_group.nodes
    links = node_group.links
    nodes.clear()

    def _new_node(*type_names: str) -> bpy.types.Node:
        last_exc: Exception | None = None
        for type_name in type_names:
            try:
                return nodes.new(type_name)
            except Exception as exc:
                last_exc = exc
        raise RuntimeError(f"Unsupported node types: {type_names}") from last_exc

    def _out_sock(node: bpy.types.Node, name: str) -> bpy.types.NodeSocket:
        sock = node.outputs.get(name)
        return sock if sock is not None else node.outputs[0]

    def _in_sock(node: bpy.types.Node, name: str, fallback_index: int) -> bpy.types.NodeSocket:
        sock = node.inputs.get(name)
        return sock if sock is not None else node.inputs[fallback_index]

    input_node = nodes.new("NodeGroupInput")
    input_node.location = (-900, 0)

    output_node = nodes.new("NodeGroupOutput")
    output_node.location = (900, 0)

    named_level = nodes.new("GeometryNodeInputNamedAttribute")
    named_level.location = (-700, 200)
    if hasattr(named_level, "data_type"):
        named_level.data_type = "INT"
    (named_level.inputs.get("Name") or named_level.inputs[0]).default_value = ATTR_LEVEL_NAME

    named_score = nodes.new("GeometryNodeInputNamedAttribute")
    named_score.location = (-700, 0)
    if hasattr(named_score, "data_type"):
        named_score.data_type = "INT"
    (named_score.inputs.get("Name") or named_score.inputs[0]).default_value = ATTR_SCORE_NAME

    def _create_sample_index(*, named_attr_node: bpy.types.Node, y: float):
        # 按照用户截图，使用 Sample Index 节点来提取点属性
        # Input: Geometry, Value (Attribute), Index (default 0)
        node = _new_node("GeometryNodeSampleIndex")
        node.location = (-450, y)
        
        # 设置为 INT 类型以匹配 Attribute 数据
        if hasattr(node, "data_type"):
            node.data_type = "INT"
        if hasattr(node, "domain"):
            node.domain = "POINT"

        # 1. Group Input Geometry -> Sample Index Geometry
        links.new(_out_sock(input_node, "Geometry"), _in_sock(node, "Geometry", 0))
        
        # 2. Named Attribute -> Sample Index Value
        val_sock = node.inputs.get("Value") or node.inputs[1]
        links.new(_out_sock(named_attr_node, "Attribute"), val_sock)
        
        # 3. Index 默认为 0 (无需连接)
        
        return node

    sample_level = _create_sample_index(named_attr_node=named_level, y=200)
    sample_score = _create_sample_index(named_attr_node=named_score, y=0)

    value_to_string_level = _new_node("FunctionNodeValueToString", "GeometryNodeValueToString")
    value_to_string_level.location = (-200, 200)

    value_to_string_score = _new_node("FunctionNodeValueToString", "GeometryNodeValueToString")
    value_to_string_score.location = (-200, 0)

    for node in (value_to_string_level, value_to_string_score):
        dec_sock = node.inputs.get("Decimals")
        if dec_sock is not None and hasattr(dec_sock, "default_value"):
            try:
                dec_sock.default_value = 0
            except Exception:
                pass

    def _link_value(source_node: bpy.types.Node, target_node: bpy.types.Node) -> None:
        out_sock = source_node.outputs.get("Value") or source_node.outputs.get("Result") or source_node.outputs[0]
        in_sock = target_node.inputs.get("Value") or target_node.inputs.get("Number") or target_node.inputs[0]
        links.new(out_sock, in_sock)

    _link_value(sample_level, value_to_string_level)
    _link_value(sample_score, value_to_string_score)

    level_label_curves = _new_node("GeometryNodeStringToCurves")
    level_label_curves.location = (50, 260)
    (level_label_curves.inputs.get("String") or level_label_curves.inputs[0]).default_value = "LEVEL: "

    level_value_curves = _new_node("GeometryNodeStringToCurves")
    level_value_curves.location = (50, 150)
    links.new(_out_sock(value_to_string_level, "String"), _in_sock(level_value_curves, "String", 0))

    score_label_curves = _new_node("GeometryNodeStringToCurves")
    score_label_curves.location = (50, 60)
    (score_label_curves.inputs.get("String") or score_label_curves.inputs[0]).default_value = "SCORE: "

    score_value_curves = _new_node("GeometryNodeStringToCurves")
    score_value_curves.location = (50, -50)
    links.new(_out_sock(value_to_string_score, "String"), _in_sock(score_value_curves, "String", 0))

    for node in (level_label_curves, level_value_curves, score_label_curves, score_value_curves):
        size_sock = node.inputs.get("Size")
        if size_sock is not None and hasattr(size_sock, "default_value"):
            size_sock.default_value = 0.35

    # -------------------------------------------------------------------------
    # Join Strings Logic (Append Number to Label)
    # Goal: "LEVEL: " + "1" -> "LEVEL: 1"
    # -------------------------------------------------------------------------

    # Helper to create Join Strings node
    def _join_strings(str1_node: bpy.types.Node, str2_node: bpy.types.Node) -> bpy.types.Node:
        join_str = _new_node("FunctionNodeStringJoin", "GeometryNodeStringJoin") # Try both names
        join_str.location = (str1_node.location.x - 200, str1_node.location.y)
        
        # Link label string (constant)
        # Note: String Join inputs can be variable. We usually use the first two.
        # But 'String' inputs in Join Strings are multi-input socket in newer Blender versions?
        # Or standard socket. Let's assume standard 2 inputs or check.
        # Actually FunctionNodeStringJoin usually has 'Delimiter' and 'Strings' (multi).
        
        # Let's try simple concatenation.
        # If Blender version is old, might not have Join Strings.
        # But user says "score、level 后面应该有数字".
        # Let's try to feed the Joined String into the Curves node.
        
        return join_str

    # Actually, simpler approach:
    # Just render the Label and Value as separate objects but positioned correctly like currently done.
    # The user says "score、level 后面应该有数字".
    # Currently `ensure_stats_text_node_group` creates 4 separate text objects:
    # 1. LEVEL (Label)
    # 2. <Level Value>
    # 3. SCORE (Label)
    # 4. <Score Value>
    # And transforms them:
    # _set_translation(level_label_xform, (0.0, 1.0, 0.0))
    # _set_translation(level_value_xform, (0.0, 0.6, 0.0))
    # This puts them on separate lines vertically.
    
    # User probably wants: "LEVEL 1" on one line.
    
    # Let's adjust translation to put them side-by-side.
    # Level Label at (0, 1.0)
    # Level Value at (1.2, 1.0) ? (Need to estimate width)
    # Score Label at (0, 0.5)
    # Score Value at (1.2, 0.5)
    
    # Or better: Use Join Strings node to feed a single String to Curves node.
    
    try:
        join_str_level = _new_node("FunctionNodeStringJoin")
        join_str_level.location = (-100, 260)
        
        # Input 1: Constant "LEVEL: "
        # We need a String Input node for constant? Or just typed value?
        # FunctionNodeStringJoin inputs are dynamic.
        # But usually you can just set the value of the socket if not linked.
        
        # Linking:
        # JoinStr.Strings[0] = "LEVEL: " (set default)
        # JoinStr.Strings[1] <--- ValueToStringLevel
        
        # The node usually has a "Strings" input that is a multi-socket.
        # Let's try to link to it.
        
        # However, programmatic access to multi-socket inputs is tricky in Python API without knowing index.
        # In 3.x/4.x, Join Strings has a 'Delimiter' and a 'Strings' virtual socket.
        
        # Safer bet for compatibility: Keep them separate but move them to be inline.
        # "Level" text width is roughly 0.35 * 5 chars ~ 1.75 unit? No, fonts are thinner.
        # Let's try aligning them horizontally.
        
    except Exception:
        pass

    def _curves_to_transformed_mesh(curves_node: bpy.types.Node, *, y: float):
        fill = _new_node("GeometryNodeFillCurve")
        fill.location = (250, y)

        transform = _new_node("GeometryNodeTransform")
        transform.location = (450, y)

        links.new(_out_sock(curves_node, "Curves"), _in_sock(fill, "Curve", 0))
        links.new(_out_sock(fill, "Mesh"), _in_sock(transform, "Geometry", 0))
        return transform

    level_label_xform = _curves_to_transformed_mesh(level_label_curves, y=260)
    level_value_xform = _curves_to_transformed_mesh(level_value_curves, y=150)
    score_label_xform = _curves_to_transformed_mesh(score_label_curves, y=60)
    score_value_xform = _curves_to_transformed_mesh(score_value_curves, y=-50)

    def _set_translation(node: bpy.types.Node, translation: tuple[float, float, float]) -> None:
        sock = node.inputs.get("Translation")
        if sock is None and len(node.inputs) > 1:
            sock = node.inputs[1]
        if sock is not None and hasattr(sock, "default_value"):
            sock.default_value = translation

    # Adjusted layout: Put Value next to Label
    # Level Label: (0.0, 1.0, 0.0)
    # Level Value: (1.8, 1.0, 0.0)
    # Score Label: (0.0, 0.0, 0.0)
    # Score Value: (1.8, 0.0, 0.0)

    _set_translation(level_label_xform, (0.0, 1.0, 0.0))
    _set_translation(level_value_xform, (1.8, 1.0, 0.0))
    _set_translation(score_label_xform, (0.0, 0.0, 0.0))
    _set_translation(score_value_xform, (1.8, 0.0, 0.0))

    # -------------------------------------------------------------------------
    # Extrude Logic (Replaces Solidify Modifier to avoid crashes)
    # Goal: Solid 3D Text, Thickness 1.0, Centered (Offset 0.0 -> -0.5 to +0.5)
    # -------------------------------------------------------------------------
    
    def _extrude_and_solidify(geometry_node: bpy.types.Node, thickness: float = 1.0) -> bpy.types.Node:
        # 1. Start at Z = -0.5 * thickness
        start_z = -0.5 * thickness
        
        transform_start = _new_node("GeometryNodeTransform")
        transform_start.location = (geometry_node.location.x + 200, geometry_node.location.y)
        _set_translation(transform_start, (0.0, 0.0, start_z))
        links.new(_out_sock(geometry_node, "Geometry"), _in_sock(transform_start, "Geometry", 0))
        
        # 2. Extrude Upwards (Z = +thickness)
        extrude = _new_node("GeometryNodeExtrudeMesh")
        extrude.location = (transform_start.location.x + 200, transform_start.location.y + 50)
        
        off_sock = extrude.inputs.get("Offset Scale") or extrude.inputs.get("Offset")
        if off_sock and hasattr(off_sock, "default_value"):
             # Extrude Mesh uses "Offset" vector or "Offset Scale" float with implicit normal?
             # Default is usually Normal. For flat text (XY), Normal is Z.
             # So Scale = thickness works.
             try:
                 if off_sock.type == "VECTOR":
                     # 用户要求: Offset Scale (偏移比例) = 0.040
                     # 这里的逻辑是：如果 Offset Scale 是 float，则直接设为 thickness
                     # 如果是 Vector (Blender 版本差异?)，则设为 Z。
                     # 在 Screenshot 1 中，看到的是 "偏移比例" (Offset Scale) 浮点值。
                     off_sock.default_value = (0.0, 0.0, thickness)
                 else:
                     off_sock.default_value = thickness
             except:
                 pass
                 
        links.new(_out_sock(transform_start, "Geometry"), _in_sock(extrude, "Mesh", 0))
        
        # 3. The Bottom Face (Original, Flipped)
        # Extrude Mesh moves the original faces to the top. It does NOT keep the bottom.
        # So we need to join with the original (transformed) geometry.
        # And Flip Faces of the bottom to point down.
        
        flip = _new_node("GeometryNodeFlipFaces")
        flip.location = (transform_start.location.x + 200, transform_start.location.y - 150)
        links.new(_out_sock(transform_start, "Geometry"), _in_sock(flip, "Mesh", 0))
        
        # 4. Join Top/Sides (Extrude Output) with Bottom (Flip Output)
        join_solid = _new_node("GeometryNodeJoinGeometry")
        join_solid.location = (extrude.location.x + 200, extrude.location.y)
        
        links.new(_out_sock(extrude, "Mesh"), _in_sock(join_solid, "Geometry", 0))
        links.new(_out_sock(flip, "Mesh"), _in_sock(join_solid, "Geometry", 0))
        
        return join_solid

    # We apply this to the final joined flat text
    
    # ... Previous Join of flat text ...
    join = _new_node("GeometryNodeJoinGeometry")
    join.location = (700, 80)
    join_in = join.inputs.get("Geometry") or join.inputs[0]
    for node in (level_label_xform, level_value_xform, score_label_xform, score_value_xform):
        links.new(_out_sock(node, "Geometry"), join_in)

    # Apply Solidify Logic
    # 用户要求 thickness = 0.04
    solid_text = _extrude_and_solidify(join, thickness=0.04)

    # Output
    links.new(_out_sock(solid_text, "Geometry"), _in_sock(output_node, "Geometry", 0))

    return node_group


def ensure_ghost_material_node_group() -> bpy.types.NodeTree:
    """确保 Ghost 专用材质设置节点组存在。

    逻辑：
    Input -> Set Material (BLTETRIS_GhostMat) -> Output
    """

    from . import looks

    existing = bpy.data.node_groups.get(GHOST_GN_GROUP_NAME)
    if existing is not None:
        return existing

    node_group = bpy.data.node_groups.new(GHOST_GN_GROUP_NAME, "GeometryNodeTree")

    if hasattr(node_group, "interface"):
        interface = node_group.interface
        interface.new_socket(name="Geometry", in_out="INPUT", socket_type="NodeSocketGeometry")
        interface.new_socket(name="Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
    else:
        node_group.inputs.new("NodeSocketGeometry", "Geometry")
        node_group.outputs.new("NodeSocketGeometry", "Geometry")

    nodes = node_group.nodes
    links = node_group.links
    nodes.clear()

    input_node = nodes.new("NodeGroupInput")
    input_node.location = (-300, 0)

    output_node = nodes.new("NodeGroupOutput")
    output_node.location = (300, 0)

    set_material = nodes.new("GeometryNodeSetMaterial")
    set_material.location = (0, 0)
    
    ghost_mat = looks.ensure_ghost_material()
    set_material.inputs.get("Material").default_value = ghost_mat

    links.new(input_node.outputs[0], set_material.inputs[0])
    links.new(set_material.outputs[0], output_node.inputs[0])

    return node_group
