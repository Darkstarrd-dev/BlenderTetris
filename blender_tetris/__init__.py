"""Blender 插件入口（register/unregister）。

注意：这个项目处于“快速迭代/脚本注入”开发态，所以 `register()` 会对各模块 `importlib.reload()`。
这样通过 websocket 反复注入时，改动可以立即生效。
"""

bl_info = {
    "name": "Blender Tetris",
    "author": "Darkstarrd",
    "version": (0, 4, 6),
    "blender": (4, 2, 0),
    "location": "View3D > Sidebar > Tetris",
    "description": "Play Tetris in Blender using a modal operator and Geometry Nodes instancing.",
    "category": "3D View",
}

import importlib
import bpy

# 1. 数据层
from .data import constants, session, properties
# 2. 核心逻辑
from .core import tetrominoes, game, ai
# 3. 工具层
from .utils import audio, assets, geo_nodes, looks, runtime
# 4. 操作符
from .operators import game_ops, replay
# 5. UI
from .ui import panels, translations

_modules = [
    constants,
    session,
    properties,
    tetrominoes,
    game,
    ai,
    audio,
    assets,
    geo_nodes,
    looks,
    runtime,
    game_ops,
    replay,
    panels,
    translations,
]


def register() -> None:
    """注册插件。
    """

    # 热重载所有模块
    for module in _modules:
        importlib.reload(module)

    # 注册翻译
    try:
        bpy.app.translations.register(__name__, translations.translations)
    except Exception:
        try:
            bpy.app.translations.unregister(__name__)
        except Exception:
            pass
        bpy.app.translations.register(__name__, translations.translations)

    # 依次注册属性、操作符和 UI
    properties.register()
    game_ops.register()
    panels.register()


def unregister() -> None:
    """卸载插件。
    """

    # 停止音频
    audio.stop_all()

    panels.unregister()
    replay.unregister()
    game_ops.unregister()
    properties.unregister()

    # 注销翻译
    try:
        bpy.app.translations.unregister(__name__)
    except Exception:
        pass
