"""聊天吸收探针回归：聊天美化插件（取消原生聊天事件 + broadcast_message 重发）兼容。

背景：u_beautiful_chat 等聊天美化插件会取消 PlayerChatEvent，再调用
server.broadcast_message 重发格式化文本。LumenBridge 以 MONITOR 优先级
观察聊天，修复前事件到达时已被取消，消息不再转发到 QQ 群。

修复机制（见 endstone_lumenbridge/modules/chat_probe.py）：
LOWEST 开窗 →（分发期间任何 broadcast_message 同步触发
BroadcastMessageEvent → 记录）→ MONITOR 关窗三态判定。
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from endstone_lumenbridge.modules.chat_probe import ChatAbsorptionProbe
from endstone_lumenbridge.plugin import LumenBridgePlugin


# ----------------------------------------------------------------------
# 探针单元测试
# ----------------------------------------------------------------------

def test_probe_records_broadcast_within_window():
    probe = ChatAbsorptionProbe()
    probe.open()
    probe.record_broadcast("[主世界] Steve >> hello")
    assert probe.close() is True


def test_probe_no_broadcast_means_not_absorbed():
    probe = ChatAbsorptionProbe()
    probe.open()
    assert probe.close() is False


def test_probe_record_without_window_is_ignored():
    # 窗口外（非聊天分发中）的广播与聊天无关，不应误计入
    probe = ChatAbsorptionProbe()
    probe.record_broadcast("某条普通广播")
    assert probe.close() is False


def test_probe_close_without_open_is_false():
    probe = ChatAbsorptionProbe()
    assert probe.close() is False


def test_probe_window_resets_between_dispatches():
    # 上一轮分发若因异常未关窗，残留广播不应泄漏到下一轮
    probe = ChatAbsorptionProbe()
    probe.open()
    probe.record_broadcast("上一轮的广播")
    # 模拟异常未关窗，新一次聊天分发开窗（open 内部重置）
    probe.open()
    assert probe.close() is False


def test_probe_thread_isolation():
    # 窗口按分发线程隔离：其他线程的广播（如 QQ→游戏广播经
    # run_on_main 在主线程执行）不污染当前线程的聊天窗口
    probe = ChatAbsorptionProbe()
    probe.open()  # 主线程（假定为聊天分发线程）开窗

    other = threading.Thread(target=probe.record_broadcast, args=("其他线程的广播",))
    other.start()
    other.join()

    assert probe.close() is False


# ----------------------------------------------------------------------
# 处理器集成测试（按 Endstone 真实分发顺序驱动）
# ----------------------------------------------------------------------

class _ChatSyncRecorder:
    def __init__(self):
        self.calls = []

    def on_player_chat(self, name, message):
        self.calls.append((name, message))


class _RegexRecorder:
    def __init__(self):
        self.calls = []

    def on_mc_player_chat(self, name, message):
        self.calls.append((name, message))


class _BusRecorder:
    def __init__(self):
        self.emitted = []

    def emit(self, *args):
        self.emitted.append(args)


def _fake_plugin():
    return SimpleNamespace(
        _chat_probe=ChatAbsorptionProbe(),
        chat_sync_module=_ChatSyncRecorder(),
        regex_module=_RegexRecorder(),
        bus=_BusRecorder(),
    )


def _dispatch_chat(plugin, *, cancelled, broadcasts=()):
    """按 Endstone 真实分发顺序模拟一次玩家聊天事件。

    :param cancelled: 事件最终是否被取消
    :param broadcasts: 分发期间（聊天插件处理阶段）发生的广播文本；
                       每个 broadcast_message 同步触发一次
                       BroadcastMessageEvent（即 LumenBridge 的
                       on_broadcast_message 处理器）
    """
    event = SimpleNamespace(
        player=SimpleNamespace(name="Steve"),
        message="hello",
        is_cancelled=cancelled,
    )
    # 1. LOWEST：LumenBridge 开窗（先于一切取消方）
    LumenBridgePlugin.on_player_chat_probe(plugin, event)
    # 2. NORMAL（模拟聊天美化插件）：取消事件并 broadcast_message
    for text in broadcasts:
        LumenBridgePlugin.on_broadcast_message(
            plugin, SimpleNamespace(message=text, is_cancelled=False)
        )
    # 3. MONITOR：LumenBridge 关窗判定
    LumenBridgePlugin.on_player_chat(plugin, event)


def _assert_forwarded(plugin):
    assert plugin.chat_sync_module.calls == [("Steve", "hello")]
    assert plugin.regex_module.calls == [("Steve", "hello")]
    assert plugin.bus.emitted == [("mc.player_chat", "Steve", "hello")]


def _assert_not_forwarded(plugin):
    assert plugin.chat_sync_module.calls == []
    assert plugin.regex_module.calls == []
    assert plugin.bus.emitted == []


def test_chat_plugin_absorption_forwards_raw_message():
    """u_beautiful_chat 场景：事件被取消但消息经广播重发 → 照常转发原文。"""
    plugin = _fake_plugin()
    _dispatch_chat(
        plugin,
        cancelled=True,
        broadcasts=["§a[主世界 | 生命：20] §fSteve >> hello"],
    )
    # 转发的是玩家原始发言（广播文本仅作“已展示”证据，不作为转发内容）
    _assert_forwarded(plugin)


def test_mute_plugin_suppression_does_not_forward():
    """禁言场景：事件被取消且无广播（消息未展示）→ 不转发。"""
    plugin = _fake_plugin()
    _dispatch_chat(plugin, cancelled=True, broadcasts=())
    _assert_not_forwarded(plugin)


def test_normal_uncancelled_chat_still_forwards():
    """无聊天插件场景：事件未被取消 → 照常转发（原行为不回归）。"""
    plugin = _fake_plugin()
    _dispatch_chat(plugin, cancelled=False, broadcasts=())
    _assert_forwarded(plugin)


def test_cancelled_broadcast_is_not_absorption_evidence():
    """广播本身被其他插件取消（未真正送达玩家）→ 不构成吸收证据。"""
    plugin = _fake_plugin()
    plugin._chat_probe.open()
    LumenBridgePlugin.on_broadcast_message(
        plugin, SimpleNamespace(message="被拦截的广播", is_cancelled=True)
    )
    event = SimpleNamespace(
        player=SimpleNamespace(name="Steve"), message="hello", is_cancelled=True
    )
    LumenBridgePlugin.on_player_chat(plugin, event)
    _assert_not_forwarded(plugin)


def test_absorbed_then_muted_dispatch_no_state_leak():
    """连续两轮分发：吸收轮之后的禁言轮不受上一轮残留影响。"""
    plugin = _fake_plugin()
    _dispatch_chat(plugin, cancelled=True, broadcasts=["格式化文本"])
    _assert_forwarded(plugin)
    # 第二轮：禁言（无广播）——探针状态必须已重置
    plugin.chat_sync_module.calls.clear()
    plugin.regex_module.calls.clear()
    plugin.bus.emitted.clear()
    _dispatch_chat(plugin, cancelled=True, broadcasts=())
    _assert_not_forwarded(plugin)


# ----------------------------------------------------------------------
# 装饰器契约：优先级与注册标记
# ----------------------------------------------------------------------

def test_handler_priority_contract():
    """三个处理器必须以正确的优先级注册（顺序是吸收识别机制的前提）。"""
    from endstone.event import EventPriority

    assert getattr(LumenBridgePlugin.on_player_chat_probe, "_is_event_handler") is True
    assert LumenBridgePlugin.on_player_chat_probe._priority == EventPriority.LOWEST

    assert getattr(LumenBridgePlugin.on_player_chat, "_is_event_handler") is True
    assert LumenBridgePlugin.on_player_chat._priority == EventPriority.MONITOR

    assert getattr(LumenBridgePlugin.on_broadcast_message, "_is_event_handler") is True
    assert LumenBridgePlugin.on_broadcast_message._priority == EventPriority.MONITOR


def test_all_chat_handlers_discoverable_by_register_events():
    """register_events 扫描 dir(listener)：三个处理器必须都能被发现，
    且监听的事件类与处理器签名一一对应（Endstone 注册按参数注解路由）。"""
    import inspect

    handlers = {
        name: getattr(LumenBridgePlugin, name)
        for name in dir(LumenBridgePlugin)
        if getattr(getattr(LumenBridgePlugin, name, None), "_is_event_handler", False)
    }
    # LumenBridgePlugin 全部原生事件监听器都在此列
    assert "on_player_chat_probe" in handlers
    assert "on_player_chat" in handlers
    assert "on_broadcast_message" in handlers

    sig = inspect.signature(LumenBridgePlugin.on_broadcast_message)
    # 经类访问为未绑定函数：params = (self, event)；Endstone 注册时
    # 经实例访问（self 已剥离），要求事件参数注解可路由到正确事件
    event_cls = list(sig.parameters.values())[1].annotation
    assert event_cls.__name__ == "BroadcastMessageEvent"
