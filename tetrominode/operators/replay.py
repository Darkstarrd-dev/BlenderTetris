"""录制烘焙为可 scrub 的时间线回放。

目标：把一段游戏过程变成“无需运行 modal 逻辑也能播放/渲染”的动画。

策略（拆分 Board/Current/Border）：
- 录制阶段：`session.recording` 里保存每次状态变化的 `RecordedState`
- 烘焙阶段：把每一步的点集 append 到一个大 Points Mesh，并给每个点写属性：
  - `bltetris_frame`：这个点属于录制的第几步（0..N-1）
  - `bltetris_piece`：实例索引
  - `bltetris_color`：材质读取
- 回放阶段：GN 通过 `Replay Index` 过滤 `bltetris_frame==ReplayIndex`，再实例化 Looks。
- 时间线：对 GN 输入 `Replay Index` 打关键帧（CONSTANT 插值），实现逐步播放。

兼容性：
- Blender 5.x 使用 layered actions，`Action.fcurves` 可能不存在。
  需要 `action.fcurve_ensure_for_datablock(...)` 取到曲线后再改插值。
"""

from __future__ import annotations

import bpy

from ..utils import geo_nodes
from ..utils import looks
from ..utils import runtime
from ..data.constants import (
    ATTR_COLOR_NAME,
    ATTR_FRAME_NAME,
    ATTR_PIECE_NAME,
    BORDER_PIECE_INDEX,
    LOOKS_COLLECTION_NAME,
    REPLAY_BORDER_OBJECT_NAME,
    REPLAY_BOARD_OBJECT_NAME,
    REPLAY_COLLECTION_NAME,
    REPLAY_CURRENT_OBJECT_NAME,
    REPLAY_GN_MODIFIER_NAME,
)
from ..data.session import RecordedState
from ..core.tetrominoes import piece_index, rotated_cells


def _ensure_collection(name: str, *, parent: bpy.types.Collection) -> bpy.types.Collection:
    """确保一个 Collection 存在并链接到父级。

    Args:
        name: collection 名称。
        parent: 父级 collection（通常是 scene root）。

    Returns:
        创建/获取到的 `bpy.types.Collection`。
    """

    collection = bpy.data.collections.get(name)
    if collection is None:
        collection = bpy.data.collections.new(name)
    if parent.children.get(collection.name) is None:
        parent.children.link(collection)
    return collection


def _remove_collection(name: str) -> None:
    """删除指定 Collection 及其孤立的 mesh 数据块。

    Args:
        name: collection 名称。

    Side effects:
        - 会删除 collection 下的 objects
        - 可能会删除 users==0 的 mesh 数据块
    """

    collection = bpy.data.collections.get(name)
    if collection is None:
        return

    meshes = {obj.data for obj in collection.objects if obj.type == "MESH"}
    objects = list(collection.objects)

    for obj in objects:
        bpy.data.objects.remove(obj, do_unlink=True)

    for mesh in meshes:
        if mesh.users == 0:
            bpy.data.meshes.remove(mesh)

    bpy.data.collections.remove(collection, do_unlink=True)


def _ensure_points_object(*, name: str, collection: bpy.types.Collection) -> bpy.types.Object:
    """确保回放用的 points mesh 对象存在并在 collection 内。

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
        mesh = bpy.data.meshes.new(f"{name}_mesh")
        obj = bpy.data.objects.new(name, mesh)
        collection.objects.link(obj)
    else:
        if obj.type != "MESH":
            raise TypeError(f"Object '{name}' exists but is not a MESH")
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
    """确保 mesh 上存在指定类型/域的 Attribute。

    Args:
        mesh: 目标 mesh。
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


def _set_points_with_attrs(
    *,
    obj: bpy.types.Object,
    points_world: list[tuple[float, float, float]],
    piece_ids: list[int],
    colors: list[tuple[float, float, float, float]],
    frame_ids: list[int],
) -> None:
    """把点坐标与回放属性批量写入 points mesh。

    这里把一次回放的全部步骤“打包”进同一个 mesh：通过 `bltetris_frame` 让 GN
    在任意时间点只显示某一步的点集。

    Args:
        obj: 目标 points mesh 对象。
        points_world: 顶点坐标（世界坐标）。
        piece_ids: 每个点对应的 piece 索引。
        colors: 每个点对应的 RGBA。
        frame_ids: 每个点属于录制序列中的哪一步（0..N-1）。

    Raises:
        TypeError: obj 不是 MESH。
        ValueError: 输入列表长度不一致。
    """

    if obj.type != "MESH":
        raise TypeError(f"Object '{obj.name}' is not a MESH")

    if len(points_world) != len(piece_ids) or len(points_world) != len(colors) or len(points_world) != len(frame_ids):
        raise ValueError("Replay point attribute lengths must match points")

    mesh = obj.data

    if bpy.context.mode != "OBJECT" or getattr(mesh, "is_editmode", False):
        bpy.ops.object.mode_set(mode="OBJECT")

    mesh.clear_geometry()
    mesh.from_pydata(points_world, [], [])
    mesh.update()

    piece_attr = _ensure_mesh_attribute(mesh, name=ATTR_PIECE_NAME, data_type="INT", domain="POINT")
    color_attr = _ensure_mesh_attribute(mesh, name=ATTR_COLOR_NAME, data_type="FLOAT_COLOR", domain="POINT")
    frame_attr = _ensure_mesh_attribute(mesh, name=ATTR_FRAME_NAME, data_type="INT", domain="POINT")

    for i in range(len(points_world)):
        piece_attr.data[i].value = int(piece_ids[i])
        color_attr.data[i].color = tuple(float(v) for v in colors[i])
        frame_attr.data[i].value = int(frame_ids[i])


def _ensure_replay_modifier(
    *,
    obj: bpy.types.Object,
    looks_collection: bpy.types.Collection,
    block_scale: tuple[float, float, float],
) -> bpy.types.NodesModifier:
    """确保回放对象有正确的 GN modifier，并注入输入参数。

    Args:
        obj: 目标回放 points mesh 对象。
        looks_collection: 外观实例来源。
        block_scale: 实例缩放。

    Returns:
        配置完成的 `bpy.types.NodesModifier`。

    Raises:
        TypeError: 目标对象上同名 modifier 存在但不是 NODES。
    """

    node_group = geo_nodes.ensure_replay_points_to_blocks_node_group()

    modifier = obj.modifiers.get(REPLAY_GN_MODIFIER_NAME)
    if modifier is None:
        modifier = obj.modifiers.new(name=REPLAY_GN_MODIFIER_NAME, type="NODES")

    if modifier.type != "NODES":
        raise TypeError(f"Modifier '{REPLAY_GN_MODIFIER_NAME}' exists on '{obj.name}' but is not a Geometry Nodes modifier")

    modifier.node_group = node_group

    looks_id = geo_nodes._get_input_identifier(node_group, "Looks Collection")
    if looks_id is not None:
        modifier[looks_id] = looks_collection

    scale_id = geo_nodes._get_input_identifier(node_group, "Block Scale")
    if scale_id is not None:
        modifier[scale_id] = tuple(float(v) for v in block_scale)

    replay_id = geo_nodes._get_input_identifier(node_group, "Replay Index")
    if replay_id is not None:
        modifier[replay_id] = 0

    return modifier


def _keyframe_replay_index(
    *,
    obj: bpy.types.Object,
    modifier: bpy.types.NodesModifier,
    frame_start: int,
    frame_step: int,
    steps: int,
) -> None:
    """为回放用的 `Replay Index` 输入打关键帧，并将曲线设为常量插值。

    常量插值能避免 Blender 在两帧之间做浮点插值，导致显示“半步”的回放。

    Args:
        obj: 持有 modifier 的对象。
        modifier: 回放用 GN modifier。
        frame_start: 起始帧。
        frame_step: 步进帧间隔（>=1）。
        steps: 录制步数。

    Raises:
        RuntimeError: GN 节点组缺少 `Replay Index` 输入。
    """

    node_group = modifier.node_group
    if node_group is None:
        return

    replay_id = geo_nodes._get_input_identifier(node_group, "Replay Index")
    if replay_id is None:
        raise RuntimeError("Replay node group missing 'Replay Index' input")

    for i in range(steps):
        frame = int(frame_start + i * frame_step)
        modifier[replay_id] = int(i)
        modifier.keyframe_insert(data_path=f'["{replay_id}"]', frame=frame)

    anim = obj.animation_data
    if not anim or not anim.action:
        return

    action = anim.action
    data_path = f'modifiers["{REPLAY_GN_MODIFIER_NAME}"]["{replay_id}"]'

    # Blender 5.x uses layered actions without .fcurves; use fcurve_ensure_for_datablock.
    fcurve = None
    if hasattr(action, "fcurve_ensure_for_datablock"):
        try:
            fcurve = action.fcurve_ensure_for_datablock(obj, data_path, index=0)
        except Exception:
            fcurve = None

    if fcurve is None and hasattr(action, "fcurves"):
        for fc in action.fcurves:
            if fc.data_path == data_path:
                fcurve = fc
                break

    if fcurve is not None:
        for kp in fcurve.keyframe_points:
            kp.interpolation = "CONSTANT"


def bake_replay(
    *,
    recorded: list[RecordedState],
    settings,
) -> bpy.types.Collection:
    """将录制的状态序列烘焙为 `TetrisReplay` collection。

    产物包含 3 个 points mesh（Board/Current/Border），并在其 GN modifier 上
    为 `Replay Index` 打关键帧以映射到时间线。

    Args:
        recorded: 录制的状态序列（去重后的 `RecordedState` 列表）。
        settings: `TetrisSettings`（或具备同名字段的对象）。

    Returns:
        创建/更新后的 `TetrisReplay` collection。

    Raises:
        RuntimeError: recorded 为空。
        ValueError: bake_frame_step <= 0。
    """

    if not recorded:
        raise RuntimeError("No recorded states to bake")

    frame_start = int(getattr(settings, "bake_start_frame", 1) or 1)
    frame_step = int(getattr(settings, "bake_frame_step", 1) or 1)

    if frame_step <= 0:
        raise ValueError("Bake frame step must be >= 1")

    if bool(getattr(settings, "bake_replace_existing", True)):
        _remove_collection(REPLAY_COLLECTION_NAME)

    scene_root = bpy.context.scene.collection
    replay_collection = _ensure_collection(REPLAY_COLLECTION_NAME, parent=scene_root)

    board_obj = _ensure_points_object(name=REPLAY_BOARD_OBJECT_NAME, collection=replay_collection)
    current_obj = _ensure_points_object(name=REPLAY_CURRENT_OBJECT_NAME, collection=replay_collection)
    border_obj = _ensure_points_object(name=REPLAY_BORDER_OBJECT_NAME, collection=replay_collection)

    width = int(getattr(settings, "board_width"))
    height = int(getattr(settings, "board_height"))
    cell_size = float(getattr(settings, "cell_size"))
    origin_corner = runtime.board_origin_corner(width=width, height=height, cell_size=cell_size)

    # Bake colors from current settings.
    piece_colors: dict[str, tuple[float, float, float, float]] = {}
    for key in ("I", "O", "T", "S", "Z", "J", "L"):
        value = getattr(settings, f"color_{key}")
        piece_colors[key] = (float(value[0]), float(value[1]), float(value[2]), float(value[3]))

    border_color_value = getattr(settings, "border_color")
    border_color = (
        float(border_color_value[0]),
        float(border_color_value[1]),
        float(border_color_value[2]),
        float(border_color_value[3]),
    )

    board_points: list[tuple[float, float, float]] = []
    board_piece_ids: list[int] = []
    board_colors: list[tuple[float, float, float, float]] = []
    board_frames: list[int] = []

    current_points: list[tuple[float, float, float]] = []
    current_piece_ids: list[int] = []
    current_colors: list[tuple[float, float, float, float]] = []
    current_frames: list[int] = []

    border_points: list[tuple[float, float, float]] = []
    border_piece_ids: list[int] = []
    border_colors: list[tuple[float, float, float, float]] = []
    border_frames: list[int] = []

    border_cells = runtime.border_cells(width=width, height=height)

    rotation_system = str(getattr(settings, "rotation_system", "SRS"))

    # Border is baked ONLY ONCE with frame_id = -1 to always show.
    bpts = runtime.cells_to_world_points(cells=border_cells, cell_size=cell_size, origin=origin_corner)
    border_points.extend(bpts)
    border_piece_ids.extend([BORDER_PIECE_INDEX] * len(bpts))
    border_colors.extend([border_color] * len(bpts))
    border_frames.extend([-1] * len(bpts))

    for frame_index, state in enumerate(recorded):
        occupied_items = sorted(state.occupied.items(), key=lambda item: (item[0][1], item[0][0]))

        for (x, z), key in occupied_items:
            board_points.extend(runtime.cells_to_world_points(cells=[(x, z)], cell_size=cell_size, origin=origin_corner))
            board_piece_ids.append(piece_index(key))
            board_colors.append(piece_colors.get(key, (1.0, 1.0, 1.0, 1.0)))
            board_frames.append(frame_index)

        if state.current is not None:
            key, rotation, px, pz = state.current
            local = rotated_cells(key, rotation, system=rotation_system)
            cells = [(px + cx, pz + cz) for (cx, cz) in local]

            pts = runtime.cells_to_world_points(cells=cells, cell_size=cell_size, origin=origin_corner)
            current_points.extend(pts)
            current_piece_ids.extend([piece_index(key)] * len(pts))
            current_colors.extend([piece_colors.get(key, (1.0, 1.0, 1.0, 1.0))] * len(pts))
            current_frames.extend([frame_index] * len(pts))

    _set_points_with_attrs(
        obj=board_obj,
        points_world=board_points,
        piece_ids=board_piece_ids,
        colors=board_colors,
        frame_ids=board_frames,
    )
    _set_points_with_attrs(
        obj=current_obj,
        points_world=current_points,
        piece_ids=current_piece_ids,
        colors=current_colors,
        frame_ids=current_frames,
    )
    _set_points_with_attrs(
        obj=border_obj,
        points_world=border_points,
        piece_ids=border_piece_ids,
        colors=border_colors,
        frame_ids=border_frames,
    )

    looks_collection = bpy.data.collections.get(LOOKS_COLLECTION_NAME) or looks.ensure_looks(settings=settings)
    block_scale_value = getattr(settings, "block_scale")
    block_scale = (float(block_scale_value[0]), float(block_scale_value[1]), float(block_scale_value[2]))

    board_mod = _ensure_replay_modifier(obj=board_obj, looks_collection=looks_collection, block_scale=block_scale)
    current_mod = _ensure_replay_modifier(obj=current_obj, looks_collection=looks_collection, block_scale=block_scale)
    border_mod = _ensure_replay_modifier(obj=border_obj, looks_collection=looks_collection, block_scale=block_scale)

    steps = len(recorded)
    _keyframe_replay_index(
        obj=board_obj,
        modifier=board_mod,
        frame_start=frame_start,
        frame_step=frame_step,
        steps=steps,
    )
    _keyframe_replay_index(
        obj=current_obj,
        modifier=current_mod,
        frame_start=frame_start,
        frame_step=frame_step,
        steps=steps,
    )
    _keyframe_replay_index(
        obj=border_obj,
        modifier=border_mod,
        frame_start=frame_start,
        frame_step=frame_step,
        steps=steps,
    )

    scene = bpy.context.scene
    scene.frame_start = min(scene.frame_start, frame_start)
    scene.frame_end = max(scene.frame_end, frame_start + (steps - 1) * frame_step)

    return replay_collection
