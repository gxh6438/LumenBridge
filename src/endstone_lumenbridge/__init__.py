"""LumenBridge - Endstone 群服互通框架。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

__version__ = "1.0.0"

if TYPE_CHECKING:
    from endstone_lumenbridge.plugin import LumenBridgePlugin


def __getattr__(name: str) -> Any:
    """按需导入 Endstone 插件类，避免纯工具模块被宿主依赖提前阻塞。"""
    if name == "LumenBridgePlugin":
        from endstone_lumenbridge.plugin import LumenBridgePlugin

        return LumenBridgePlugin
    raise AttributeError(name)


__all__ = ["LumenBridgePlugin"]
