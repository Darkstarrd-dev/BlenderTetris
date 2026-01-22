"""blender_tetris 常量定义（命名即约定）。

这个插件会在 Blender 场景里创建多个 collection/object/node group。
为了避免硬编码散落各处，统一把名字放在这里：

- `TetrisAssets`：tetromino 形状的 points mesh 资产（开发/参考用）
- `TetrisLooks`：外观库（每种块/边框一个 look object），供 GN Pick Instance 使用
- `TetrisGame`：实时运行时的 points mesh（board/current/border），由 operator 不断写入顶点
- `TetrisReplay`：烘焙回放（把每步的点 append 到一个大 mesh，并用 `bltetris_frame` 过滤）

属性命名：
- `bltetris_piece`：int，0..6 表示 I/O/T/S/Z/J/L，7 表示 BORDER
- `bltetris_color`：RGBA（FLOAT_COLOR），用于材质读取
- `bltetris_frame`：int，回放用的帧索引（不是 Blender frame）
"""

ASSETS_COLLECTION_NAME = "TetrisAssets"
ASSETS_TETROMINO_PREFIX = "tetrimino_"

LOOKS_COLLECTION_NAME = "TetrisLooks"
LOOKS_BORDER_KEY = "BORDER"

# Points Mesh 上的属性名（GN 会用 Named Attribute 读取）。
ATTR_PIECE_NAME = "bltetris_piece"
ATTR_COLOR_NAME = "bltetris_color"
ATTR_SCALE_NAME = "bltetris_scale"
ATTR_FRAME_NAME = "bltetris_frame"
ATTR_LEVEL_NAME = "bltetris_level"
ATTR_SCORE_NAME = "bltetris_score"

# Look collection 的对象顺序：I/O/T/S/Z/J/L/BORDER，其中 BORDER 的 piece index 固定为 7。
BORDER_PIECE_INDEX = 7

#（旧）基础方块对象名：目前外观使用 TetrisLooks，不再依赖单个 block object。
BLOCK_OBJECT_NAME = "BLTETRIS_Block"

# 实时实例化 GN。
GN_GROUP_NAME = "BLTETRIS_PointsToBlocks"
GN_MODIFIER_NAME = "BLTETRIS_GeometryNodes"

# HUD / 预览窗口（Next/Hold/Stats）。
HUD_COLLECTION_NAME = "TetrisHUD"
NEXT_POINTS_OBJECT_NAME = "BLTETRIS_Next_Points"
HOLD_POINTS_OBJECT_NAME = "BLTETRIS_Hold_Points"
STATS_POINTS_OBJECT_NAME = "BLTETRIS_Stats_Points"
GHOST_POINTS_OBJECT_NAME = "BLTETRIS_Ghost_Points"
AI_TARGET_POINTS_OBJECT_NAME = "BLTETRIS_AI_Target_Points"
AI_PATH_POINTS_OBJECT_NAME = "BLTETRIS_AI_Path_Points"

# HUD 文本 GN。
STATS_TEXT_GN_GROUP_NAME = "BLTETRIS_StatsText"
STATS_TEXT_GN_MODIFIER_NAME = "BLTETRIS_StatsText_GeometryNodes"

# 回放 collection/对象名。
REPLAY_COLLECTION_NAME = "TetrisReplay"
REPLAY_BOARD_OBJECT_NAME = "BLTETRIS_Replay_Board_Points"
REPLAY_CURRENT_OBJECT_NAME = "BLTETRIS_Replay_Current_Points"
REPLAY_BORDER_OBJECT_NAME = "BLTETRIS_Replay_Border_Points"

# 回放实例化 GN（包含 Replay Index 过滤）。
REPLAY_GN_GROUP_NAME = "BLTETRIS_ReplayPointsToBlocks"
REPLAY_GN_MODIFIER_NAME = "BLTETRIS_Replay_GeometryNodes"

# Ghost 专用材质设置 GN。
GHOST_GN_GROUP_NAME = "BLTETRIS_GhostPoints_Material"
GHOST_GN_MODIFIER_NAME = "BLTETRIS_GhostPoints_GeometryNodes"

# 默认棋盘与格子大小。
BOARD_WIDTH_DEFAULT = 10
BOARD_HEIGHT_DEFAULT = 20
CELL_SIZE_DEFAULT = 1.0

# 棋盘所在平面：XZ（Y 固定为 0）。
WORLD_Y_LEVEL = 0.0
