"""聊天吸收探针回归：聊天美化插件（取消原生聊天事件 + 重发）兼容。

背景：u_beautiful_chat 等聊天美化插件会接管 PlayerChatEvent：取消事件
后以格式化文本重发（broadcast_message / 逐玩家 send_message），或直接
修改 event.format。LumenBridge 以 MONITOR 优先级观察聊天，事件到达时
已被取消，若直接按“消息被抑制”处理，聊天将不再转发到 QQ 群。

修复机制 v1.0.7（见 endstone_lumenbridge/modules/chat_probe.py）：
时间戳回溯——BroadcastMessageEvent 无条件记录 (消息, 时间戳)；
PlayerChatEvent（MONITOR）到达时回看最近 N 毫秒内是否出现过包含原始
聊天内容的广播，作为“消息已实际展示”的证据。

v1.0.6 的 threading.local 开窗方案存在两个缺陷（已弃用）：
1. 嵌入式 Python 跨事件回调更换 PyThreadState，同 OS 线程也会丢状态；
2. 美化插件先注册时，其 LOWEST 阶段广播早于开窗，漏记。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from endstone_lumenbridge.modules.chat_probe import ChatAbsorptionProbe
from endstone_lumenbridge.plugin import LumenBridgePlugin


# ----------------------------------------------------------------------
# 探针单元测试（时间戳回溯）
# ----------------------------------------------------------------------

def test_evidence_found_when_broadcast_contains_message():
    probe = ChatAbsorptionProbe()
    probe.record_broadcast("§a[主世界] §fSteve >> hello")
    assert probe.absorb_evidence("Steve", "hello") == "§a[主世界] §fSteve >> hello"


def test_no_broadcast_means_no_evidence():
    probe = ChatAbsorptionProbe()
    assert probe.absorb_evidence("Steve", "hello") is None


def test_broadcast_without_message_text_is_not_evidence():
    # join/quit 广播（[+] Steve）不含消息文本，不构成吸收证据
    probe = ChatAbsorptionProbe()
    probe.record_broadcast("§a[+] §fSteve")
    assert probe.absorb_evidence("Steve", "hello") is None


def test_expired_broadcast_is_not_evidence():
    # 窗口外的广播（过早，与本次聊天无关）不参与匹配
    probe = ChatAbsorptionProbe(window_ms=100)
    probe.record_broadcast("Steve >> hello")
    time.sleep(0.2)
    assert probe.absorb_evidence("Steve", "hello") is None


def test_recent_broadcast_is_evidence():
    # 窗口内的广播（含消息文本）构成吸收证据
    probe = ChatAbsorptionProbe(window_ms=1000)
    probe.record_broadcast("[格式化] Steve >> hello")
    time.sleep(0.05)
    assert probe.absorb_evidence("Steve", "hello") is not None


def test_empty_message_never_matches():
    probe = ChatAbsorptionProbe()
    probe.record_broadcast("任意广播")
    assert probe.absorb_evidence("Steve", "") is None
    assert probe.absorb_evidence("Steve", "   ") is None


def test_latest_match_wins():
    probe = ChatAbsorptionProbe()
    probe.record_broadcast("旧: Steve >> hello")
    probe.record_broadcast("新: Steve >> hello")
    assert probe.absorb_evidence("Steve", "hello") == "新: Steve >> hello"


def test_evidence_is_consumed_on_match():
    # 消费制：同一条广播不会被两轮聊天重复用作证据
    probe = ChatAbsorptionProbe()
    probe.record_broadcast("Steve >> hello")
    assert probe.absorb_evidence("Steve", "hello") is not None
    assert probe.absorb_evidence("Steve", "hello") is None


def test_strong_match_preferred_over_weak():
    # 强匹配（玩家名+消息）优先于弱匹配（仅消息），
    # 他人广播同文本消息不会被误认为本玩家的证据
    probe = ChatAbsorptionProbe()
    probe.record_broadcast("Alex: hello")          # 他人同文本
    probe.record_broadcast("Steve >> hello")       # 本玩家
    assert probe.absorb_evidence("Steve", "hello") == "Steve >> hello"
    # Alex 的广播未被消费，仍可作为 Alex 的证据
    assert probe.absorb_evidence("Alex", "hello") == "Alex: hello"


def test_weak_match_when_broadcast_lacks_player_name():
    # 弱匹配兜底：美化广播显示昵称（不含真实玩家名）时仍可判定
    probe = ChatAbsorptionProbe()
    probe.record_broadcast("§b[大魔王]§r: hello")
    assert probe.absorb_evidence("Steve", "hello") == "§b[大魔王]§r: hello"


def test_capacity_limit_evicts_oldest():
    probe = ChatAbsorptionProbe()
    for i in range(64):
        probe.record_broadcast(f"noise-{i}")
    probe.record_broadcast("Steve >> hello")
    # 最旧的 noise-0 已被逐出，但 hello 仍在
    assert probe.absorb_evidence("Steve", "hello") is not None


def test_v106_compat_open_close_are_noops():
    # v1.0.6 的 open/close 保留为空操作，外部调用不报错
    probe = ChatAbsorptionProbe()
    probe.open()
    probe.record_broadcast("x")
    assert probe.close() is False


def test_dump_debug_shape():
    probe = ChatAbsorptionProbe()
    probe.record_broadcast("Steve >> hello")
    snap = probe.dump_debug()
    assert snap["window_ms"] == probe._window_ms
    assert len(snap["recent"]) == 1
    assert "hello" in snap["recent"][0]["msg"]


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


def _fake_plugin(**overrides):
    import types as _types

    ns = SimpleNamespace(
        _chat_probe=ChatAbsorptionProbe(window_ms=1000),
        _chat_debug=False,
        chat_sync_module=_ChatSyncRecorder(),
        regex_module=_RegexRecorder(),
        bus=_BusRecorder(),
        logger=SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None),
    )
    # 绑定真实的策略读取方法（getattr(self, "config_manager", None) 兜底 None）
    ns._forward_cancelled_mode = _types.MethodType(
        LumenBridgePlugin._forward_cancelled_mode, ns
    )
    for key, value in overrides.items():
        setattr(ns, key, value)
    return ns


# ----------------------------------------------------------------------
# chat.forward_cancelled 策略（auto/always/never）
# ----------------------------------------------------------------------

def _make_plugin_with_mode(mode):
    return _fake_plugin(
        config_manager=SimpleNamespace(data={"chat": {"forward_cancelled": mode}})
    )


def test_forward_cancelled_always_forwards_suppressed_chat():
    """always 兜底：取消且无广播证据（如范围聊天逐玩家重发）也转发。"""
    plugin = _make_plugin_with_mode("always")
    _dispatch_chat(plugin, cancelled=True, broadcasts=())
    _assert_forwarded(plugin)


def test_forward_cancelled_never_blocks_absorbed_chat():
    """never 严格模式：取消即使有吸收证据也不转发。"""
    plugin = _make_plugin_with_mode("never")
    _dispatch_chat(
        plugin, cancelled=True, broadcasts=["§b[美化] Steve >> hello"]
    )
    _assert_not_forwarded(plugin)


def test_forward_cancelled_invalid_falls_back_to_auto():
    """非法配置值回退 auto（按吸收证据判定）。"""
    plugin = _fake_plugin(
        config_manager=SimpleNamespace(data={"chat": {"forward_cancelled": "yes??"}})
    )
    _dispatch_chat(plugin, cancelled=True, broadcasts=())
    _assert_not_forwarded(plugin)
    plugin2 = _fake_plugin(
        config_manager=SimpleNamespace(data={"chat": {"forward_cancelled": "yes??"}})
    )
    _dispatch_chat(plugin2, cancelled=True, broadcasts=["§b[美化] Steve >> hello"])
    _assert_forwarded(plugin2)


def test_forward_cancelled_missing_config_defaults_to_auto():
    """无配置/无 config_manager 均按 auto。"""
    plugin = _fake_plugin(config_manager=SimpleNamespace(data={}))
    _dispatch_chat(plugin, cancelled=True, broadcasts=())
    _assert_not_forwarded(plugin)


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
    # NORMAL（模拟聊天美化插件）：取消事件并 broadcast_message；
    # 注册顺序无关——广播先于/后于 LumenBridge 的处理器均记录在案
    for text in broadcasts:
        LumenBridgePlugin.on_broadcast_message(
            plugin, SimpleNamespace(message=text, is_cancelled=False)
        )
    # MONITOR：LumenBridge 时间戳回溯判定
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
    """美化插件场景：事件被取消但消息经广播重发 → 照常转发原文。"""
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


def test_cancel_send_mode_does_not_forward_by_default():
    """范围聊天类插件（取消 + 逐玩家 send_message，无广播信号）：
    无展示证据 → 按抑制处理不转发（文档化边界，兜底走
    chat.forward_cancelled = "always"）。"""
    plugin = _fake_plugin()
    _dispatch_chat(plugin, cancelled=True, broadcasts=())
    _assert_not_forwarded(plugin)


def test_normal_uncancelled_chat_still_forwards():
    """无聊天插件场景（含 UBC 仅改 format：分发期未取消）→ 照常转发。"""
    plugin = _fake_plugin()
    _dispatch_chat(plugin, cancelled=False, broadcasts=())
    _assert_forwarded(plugin)


def test_cancelled_broadcast_is_not_absorption_evidence():
    """广播本身被其他插件取消（未真正送达玩家）→ 不构成吸收证据。"""
    plugin = _fake_plugin()
    LumenBridgePlugin.on_broadcast_message(
        plugin, SimpleNamespace(message="Steve >> hello", is_cancelled=True)
    )
    event = SimpleNamespace(
        player=SimpleNamespace(name="Steve"), message="hello", is_cancelled=True
    )
    LumenBridgePlugin.on_player_chat(plugin, event)
    _assert_not_forwarded(plugin)


def test_broadcast_before_lumen_handler_registration_order_independent():
    """v1.0.6 缺陷回归：美化插件先注册（其广播早于 LumenBridge 一切
    处理器触发）时，v1.0.6 的 LOWEST 开窗会漏记；v1.0.7 时间戳回溯
    无条件记录，不受注册顺序影响。"""
    plugin = _fake_plugin()
    # 模拟：美化插件的广播发生在 LumenBridge 任何处理器之前
    LumenBridgePlugin.on_broadcast_message(
        plugin, SimpleNamespace(message="§b[美化] Steve >> hello", is_cancelled=False)
    )
    event = SimpleNamespace(
        player=SimpleNamespace(name="Steve"), message="hello", is_cancelled=True
    )
    LumenBridgePlugin.on_player_chat(plugin, event)
    _assert_forwarded(plugin)


def test_absorbed_then_muted_dispatch_no_state_leak():
    """连续两轮分发：吸收轮之后的禁言轮不受上一轮残留影响。"""
    plugin = _fake_plugin()
    _dispatch_chat(plugin, cancelled=True, broadcasts=["格式化文本 Steve >> hello"])
    _assert_forwarded(plugin)
    # 第二轮：禁言（无广播）——上一轮广播已消费，不构成本轮证据
    plugin.chat_sync_module.calls.clear()
    plugin.regex_module.calls.clear()
    plugin.bus.emitted.clear()
    _dispatch_chat(plugin, cancelled=True, broadcasts=())
    _assert_not_forwarded(plugin)


def test_stale_broadcast_does_not_leak_across_window():
    """窗口外的历史广播不构成新聊天的证据（时间戳淘汰）。"""
    plugin = _fake_plugin()
    plugin._chat_probe.set_window(100)  # 100ms 最小窗口
    _dispatch_chat(plugin, cancelled=True, broadcasts=["很久以前 Steve >> hello"])
    _assert_forwarded(plugin)
    time.sleep(0.2)  # 越过窗口
    plugin.chat_sync_module.calls.clear()
    plugin.regex_module.calls.clear()
    plugin.bus.emitted.clear()
    event = SimpleNamespace(
        player=SimpleNamespace(name="Steve"), message="hello", is_cancelled=True
    )
    LumenBridgePlugin.on_player_chat(plugin, event)
    _assert_not_forwarded(plugin)


# ----------------------------------------------------------------------
# 装饰器契约：优先级与注册标记
# ----------------------------------------------------------------------

def test_handler_priority_contract():
    """两个处理器必须以 MONITOR 优先级注册（只观察不修改，且晚于一切
    NORMAL 处理器，能看到取消结果与全部广播）。"""
    from endstone.event import EventPriority

    assert getattr(LumenBridgePlugin.on_player_chat, "_is_event_handler") is True
    assert LumenBridgePlugin.on_player_chat._priority == EventPriority.MONITOR

    assert getattr(LumenBridgePlugin.on_broadcast_message, "_is_event_handler") is True
    assert LumenBridgePlugin.on_broadcast_message._priority == EventPriority.MONITOR


def test_all_chat_handlers_discoverable_by_register_events():
    """register_events 扫描 dir(listener)：处理器必须都能被发现，
    且监听的事件类与处理器签名一一对应（Endstone 注册按参数注解路由）。"""
    import inspect

    handlers = {
        name: getattr(LumenBridgePlugin, name)
        for name in dir(LumenBridgePlugin)
        if getattr(getattr(LumenBridgePlugin, name, None), "_is_event_handler", False)
    }
    assert "on_player_chat" in handlers
    assert "on_broadcast_message" in handlers

    sig = inspect.signature(LumenBridgePlugin.on_broadcast_message)
    # 经类访问为未绑定函数：params = (self, event)；Endstone 注册时
    # 经实例访问（self 已剥离），要求事件参数注解可路由到正确事件
    event_cls = list(sig.parameters.values())[1].annotation
    assert event_cls.__name__ == "BroadcastMessageEvent"
