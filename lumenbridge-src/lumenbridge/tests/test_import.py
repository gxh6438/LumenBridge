"""LumenBridge 静态验证：
1. 校验所有模块可正常导入
2. 校验 Endstone API 引用是否存在（CommandSenderWrapper、事件类等）
3. 校验事件总线、消息构建器、配置合并、正则引擎核心逻辑

pytest 下由 test_ 函数驱动（断言失败即 FAIL）；python 直接运行时输出汇总。
endstone 符号由 tests/conftest.py 在真实 endstone 缺失时统一注入 stub。
"""

import sys
from pathlib import Path

SRC = Path(__file__).parent.parent / "src"
sys.path.insert(0, str(SRC))


# 1. Endstone API 存在性
def t_endstone_api():
    from endstone import ColorFormat  # noqa
    from endstone.command import Command, CommandSender, CommandSenderWrapper  # noqa
    from endstone.event import (  # noqa
        EventPriority, PlayerChatEvent, PlayerDeathEvent,
        PlayerJoinEvent, PlayerQuitEvent, event_handler,
    )
    from endstone.plugin import Plugin  # noqa


# 2. 插件包导入
def t_import_plugin():
    from endstone_lumenbridge.plugin import LumenBridgePlugin  # noqa
    assert LumenBridgePlugin.api_version == "0.11"
    assert "lumen" in LumenBridgePlugin.commands


# 3. 事件总线
def t_event_bus():
    from endstone_lumenbridge.event_bus import EventBus
    bus = EventBus()
    got = []
    bus.on("a", lambda x: got.append(x))
    bus.once("a", lambda x: got.append(x * 10))
    bus.emit("a", 1)
    bus.emit("a", 2)
    assert got == [1, 10, 2], got


# 4. 消息构建
def t_message_builder():
    from endstone_lumenbridge.onebot.message import at, decode_cq_entities, format_message, text
    segs = format_message(["hello", at(123)])
    assert segs[0] == text("hello") and segs[1]["type"] == "at"
    assert decode_cq_entities("&#91;x&#93;&amp;") == "[x]&"


# 5. 数据包构建
def t_packets():
    from endstone_lumenbridge.onebot import packets
    p = packets.group_message(123, [{"type": "text", "data": {"text": "hi"}}], echo="e1")
    assert p["action"] == "send_group_msg" and p["echo"] == "e1"
    assert packets.group_ban(1, 2, 60)["params"]["duration"] == 60


# 6. 配置合并（v1.2.0 起 connection/sync 迁移至 connections.json，config.json 仅基础配置）
def t_config_merge():
    from endstone_lumenbridge.config import DEFAULT_CONFIG, deep_merge
    merged, patched = deep_merge(DEFAULT_CONFIG, {"whitelist": {"enable": False}})
    assert patched and merged["whitelist"]["enable"] is False
    assert merged["whitelist"]["bind_keyword"] == "绑定白名单"
    assert "connection" not in merged and "sync" not in merged
    from endstone_lumenbridge.connections import ConnectionManager
    assert ConnectionManager.is_configured({"ws_type": 0, "target": "ws://x"}) is True
    assert ConnectionManager.is_configured({"ws_type": 1, "listen_port": 0}) is False


# 7. 正则引擎纯逻辑（变量替换 / flags / 占位符）
def t_regex_logic():
    from endstone_lumenbridge.modules.regex_engine import compile_pattern
    from endstone_lumenbridge.modules.chat_sync import replace_placeholders
    m = compile_pattern("^执行(.+)", "i").search("执行list")
    assert m and m.group(1) == "list"
    assert replace_placeholders("[玩家] %s: %s", "Steve", "hi") == "[玩家] Steve: hi"


# 8. 事件分发器（无网络，模拟包）
def t_dispatcher():
    from endstone_lumenbridge.event_bus import EventBus
    from endstone_lumenbridge.onebot.dispatcher import EventDispatcher

    class FakeAdapter:
        sent = []
        def send_group_msg(self, gid, msg): self.sent.append((gid, msg))
        def send_private_msg(self, uid, msg): self.sent.append((uid, msg))

    bus = EventBus()
    adapter = FakeAdapter()
    EventDispatcher(adapter, bus, None)
    received = []
    bus.on("message.group.normal", lambda pack, reply: (received.append(pack), reply("ok", True)))
    bus.emit("onebot.pack", {
        "post_type": "message", "message_type": "group", "sub_type": "normal",
        "group_id": 999, "user_id": 111, "message_id": 5,
        "raw_message": "&#91;test&#93;", "message": [],
        "sender": {"nickname": "n", "user_id": 111},
    })
    assert received and received[0]["raw_message"] == "[test]"
    assert adapter.sent and adapter.sent[0][0] == 999
    # 引用回复段在最前
    assert adapter.sent[0][1][0]["type"] == "reply"


# 9. websockets 内嵌库
def t_vendor():
    from endstone_lumenbridge.vendor import import_websockets
    ws = import_websockets()
    assert hasattr(ws, "connect") and hasattr(ws, "serve")


# ----------------------------------------------------------------------
# pytest 入口：每项检查一个用例，断言失败直接抛出（真实 FAIL）
# ----------------------------------------------------------------------
def test_endstone_api():
    t_endstone_api()


def test_plugin_package_import():
    t_import_plugin()


def test_event_bus():
    t_event_bus()


def test_message_builder():
    t_message_builder()


def test_packets():
    t_packets()


def test_config_merge():
    t_config_merge()


def test_regex_logic():
    t_regex_logic()


def test_dispatcher():
    t_dispatcher()


def test_vendor_websockets():
    t_vendor()


# ----------------------------------------------------------------------
# 手动运行入口（python tests/test_import.py）
# ----------------------------------------------------------------------
CHECKS = [
    ("Endstone API 引用", t_endstone_api),
    ("插件包导入", t_import_plugin),
    ("事件总线", t_event_bus),
    ("消息构建器", t_message_builder),
    ("数据包构建器", t_packets),
    ("配置合并", t_config_merge),
    ("正则/占位符逻辑", t_regex_logic),
    ("事件分发器", t_dispatcher),
    ("内嵌 websockets", t_vendor),
]


def _run_manual() -> int:
    failures: list[str] = []
    for name, fn in CHECKS:
        try:
            fn()
            print(f"[PASS] {name}")
        except Exception as e:
            failures.append(f"{name}: {e!r}")
            print(f"[FAIL] {name}: {e!r}")
    print("\n==== 结果 ====")
    if failures:
        print(f"{len(failures)} 项失败")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(_run_manual())
