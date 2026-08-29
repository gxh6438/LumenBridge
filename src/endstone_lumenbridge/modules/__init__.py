"""LumenBridge 业务功能模块。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .chat_sync import ChatSyncModule
    from .regex_engine import RegexEngineModule
    from .whitelist import WhitelistModule


def __getattr__(name: str) -> Any:
    """按需载入模块，避免无关功能在工具/测试导入时强制依赖 Endstone。"""
    if name == "ChatSyncModule":
        from .chat_sync import ChatSyncModule
        return ChatSyncModule
    if name == "WhitelistModule":
        from .whitelist import WhitelistModule
        return WhitelistModule
    if name == "RegexEngineModule":
        from .regex_engine import RegexEngineModule
        return RegexEngineModule
    if name == "ChatFilterModule":
        from .chat_filter import ChatFilterModule
        return ChatFilterModule
    raise AttributeError(name)


__all__ = ["ChatSyncModule", "WhitelistModule", "RegexEngineModule", "ChatFilterModule"]
