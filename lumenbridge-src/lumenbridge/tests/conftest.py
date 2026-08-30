"""pytest 兼容层：
1. 为脚本风格测试提供 tmp fixture（映射到 tmp_path）；
2. 收集前统一注入最小 endstone stub（仅当真实 endstone 不可导入时），
   使离线沙箱中引用 endstone 符号（plugin.py / whitelist.py / regex_engine.py /
   subplugin/context.py 等）的模块也能被正常导入与收集。
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest


def _ensure_module(name: str) -> types.ModuleType:
    mod = sys.modules.get(name)
    if mod is None:
        mod = types.ModuleType(name)
        sys.modules[name] = mod
    return mod


class _ColorFormatStub:
    """ColorFormat 哑对象：任意颜色属性返回空串，不污染拼接文本。"""

    def __getattr__(self, attr: str) -> str:
        return ""


class _CommandSenderWrapperStub:
    """endstone 0.11 CommandSenderWrapper 哑类。

    与真实构造签名一致：CommandSenderWrapper(sender, on_message=..., on_error=...)。
    （test_integration / test_subplugin 会在导入后整体替换为自己的记录型替身。）
    """

    def __init__(self, sender, on_message=None, on_error=None):
        self._sender = sender
        self._on_message = on_message
        self._on_error = on_error


def _install_endstone_stubs() -> None:
    """真实 endstone 可导入时不做任何事；仅在缺失时注入最小哑类。"""
    try:
        import endstone  # noqa: F401
        return
    except ImportError:
        pass

    endstone = _ensure_module("endstone")
    if not hasattr(endstone, "ColorFormat"):
        endstone.ColorFormat = _ColorFormatStub()
    if not hasattr(endstone, "Player"):
        endstone.Player = type("Player", (), {})
    if not hasattr(endstone, "Server"):
        endstone.Server = type("Server", (), {})

    # endstone.command：plugin.py 顶部 / config.py / whitelist.py /
    # regex_engine.py / subplugin/context.py 的延迟导入引用
    command = _ensure_module("endstone.command")
    for attr in ("Command", "CommandSender", "ConsoleCommandSender"):
        if not hasattr(command, attr):
            setattr(command, attr, type(attr, (), {}))
    if not hasattr(command, "CommandSenderWrapper"):
        command.CommandSenderWrapper = _CommandSenderWrapperStub

    # endstone.event：plugin.py 顶部 import 的事件类 + 动态反射监听
    event = _ensure_module("endstone.event")
    for attr in (
        "PlayerChatEvent", "PlayerDeathEvent", "PlayerJoinEvent",
        "PlayerQuitEvent", "PlayerCommandEvent",
    ):
        if not hasattr(event, attr):
            setattr(event, attr, type(attr, (), {}))
    if not hasattr(event, "EventPriority"):
        priority = type("EventPriority", (), {})
        for level in ("HIGHEST", "HIGH", "NORMAL", "LOW", "LOWEST", "MONITOR"):
            setattr(priority, level, level.lower())
        event.EventPriority = priority
    if not hasattr(event, "event_handler"):
        event.event_handler = lambda *a, **k: (lambda f: f)

    # endstone.plugin：plugin.py 的 Plugin 基类
    plugin = _ensure_module("endstone.plugin")
    if not hasattr(plugin, "Plugin"):
        plugin.Plugin = type("Plugin", (), {})

    # endstone.logger：Logger 哑类（部分子插件 / 探测脚本引用）
    logger_mod = _ensure_module("endstone.logger")
    if not hasattr(logger_mod, "Logger"):
        logger_mod.Logger = type("Logger", (), {})


# 收集任何测试模块前完成注入（conftest 早于所有 test_* 模块被导入）
_install_endstone_stubs()


@pytest.fixture
def tmp(tmp_path: Path) -> Path:
    return tmp_path
