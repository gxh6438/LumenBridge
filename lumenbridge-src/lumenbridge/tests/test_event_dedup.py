"""事件 / 规则动作去抖测试

覆盖 v1.2.4 两层修复：
1. EventDispatcher：notice / request 在窗口内按指纹去重（多链路重复上报 / 协议端重发）；
2. RegexEngineModule：同一规则对同一事件指纹窗口内只执行一次动作（覆盖消息与事件两类规则）。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from endstone_lumenbridge.event_bus import EventBus
from endstone_lumenbridge.modules.regex_engine import RegexEngineModule
from endstone_lumenbridge.onebot.dispatcher import EventDispatcher


class DummyLogger:
    def info(self, msg): pass
    def warning(self, msg): pass
    def error(self, msg): pass
    def debug(self, msg): pass


class DummyConfig:
    def __init__(self):
        self.regex_engine = {"enable": True, "only_on_main": False, "command_timeout": 1.0}
        self.main_group = 100
        self.main_groups = [100]
        self.admin_qq: list[int] = []


class DummyAdapter:
    def __init__(self):
        self.group_messages = []

    def send_group_msg(self, group_id, message):
        self.group_messages.append((group_id, message))


class DummyPlugin:
    def __init__(self, data_folder: Path):
        self.data_folder = data_folder
        self.logger = DummyLogger()
        self.config_manager = DummyConfig()
        self.bus = EventBus(self.logger)
        self.adapter = DummyAdapter()
        self.whitelist_module = None

    def group_allowed(self, pack):
        return True


def notice_pack(user_id=10001, group_id=100, t=1700000000, adapter_id="a1"):
    return {
        "post_type": "notice",
        "notice_type": "group_increase",
        "sub_type": "approve",
        "group_id": group_id,
        "user_id": user_id,
        "operator_id": 20002,
        "self_id": 30003,
        "time": t,
        "_lumen_adapter_id": adapter_id,
    }


class DispatcherDedupTests(unittest.TestCase):
    def setUp(self):
        self.bus = EventBus(DummyLogger())
        self.dispatcher = EventDispatcher(None, self.bus, DummyLogger())
        self.hits = []
        self.bus.on("notice.group_increase", lambda pack: self.hits.append(pack))

    def test_same_notice_from_two_adapter_links_fires_once(self):
        # 同一 NapCat 事件经两个适配器链路重复上报 → 只触发一次
        self.bus.emit("onebot.pack", notice_pack(adapter_id="a1"))
        self.bus.emit("onebot.pack", notice_pack(adapter_id="a2"))
        self.assertEqual(len(self.hits), 1)

    def test_replayed_notice_within_window_fires_once(self):
        self.bus.emit("onebot.pack", notice_pack())
        self.bus.emit("onebot.pack", notice_pack())
        self.assertEqual(len(self.hits), 1)

    def test_different_time_or_user_not_suppressed(self):
        self.bus.emit("onebot.pack", notice_pack(t=1700000001))
        self.bus.emit("onebot.pack", notice_pack(user_id=10002))
        self.assertEqual(len(self.hits), 2)

    def test_message_events_are_not_deduplicated_at_dispatcher(self):
        hits = []
        self.bus.on("message.group.normal", lambda pack, reply: hits.append(pack))
        pack = {
            "post_type": "message", "message_type": "group", "sub_type": "normal",
            "group_id": 100, "user_id": 10001, "message_id": 1,
            "raw_message": "hi", "time": 1700000000,
        }
        self.bus.emit("onebot.pack", dict(pack))
        self.bus.emit("onebot.pack", dict(pack))
        self.assertEqual(len(hits), 2)


class RegexActionDedupTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.plugin = DummyPlugin(Path(self.tempdir.name))
        self.engine = RegexEngineModule(self.plugin)

    def tearDown(self):
        self.tempdir.cleanup()

    def test_welcome_rule_fires_once_for_duplicate_group_increase(self):
        self.engine.rules = [{
            "id": "rule_welcome", "name": "入群欢迎", "enabled": True,
            "triggerType": "event", "pattern": "", "eventType": "group.member_join",
            "conditions": [], "actions": [{"type": "replyText", "params": "欢迎"}], "block": False,
        }]
        pack = notice_pack()
        self.engine.handle_event("group.member_join", dict(pack))
        self.engine.handle_event("group.member_join", dict(pack))
        self.engine.handle_event("group.member_join", dict(pack))
        self.assertEqual(len(self.plugin.adapter.group_messages), 1)

    def test_different_members_both_welcomed(self):
        self.engine.rules = [{
            "id": "rule_welcome", "name": "入群欢迎", "enabled": True,
            "triggerType": "event", "pattern": "", "eventType": "group.member_join",
            "conditions": [], "actions": [{"type": "replyText", "params": "欢迎"}], "block": False,
        }]
        self.engine.handle_event("group.member_join", notice_pack(user_id=1))
        self.engine.handle_event("group.member_join", notice_pack(user_id=2))
        self.assertEqual(len(self.plugin.adapter.group_messages), 2)

    def test_message_rule_dedups_by_message_id(self):
        self.engine.rules = [{
            "id": "rule_hi", "name": "打招呼", "enabled": True,
            "triggerType": "message", "pattern": "^你好", "flags": "",
            "conditions": [], "actions": [{"type": "replyText", "params": "你好呀"}], "block": False,
        }]
        base = {
            "post_type": "message", "message_type": "group", "sub_type": "normal",
            "group_id": 100, "user_id": 10001, "message_id": 42,
            "message": [{"type": "text", "data": {"text": "你好"}}],
            "raw_message": "你好", "time": 1700000000, "self_id": 30003,
        }
        self.engine._on_group_message(dict(base), None)
        self.engine._on_group_message(dict(base), None)
        self.assertEqual(len(self.plugin.adapter.group_messages), 1)
        # 不同消息（不同 message_id）正常触发
        other = dict(base, message_id=43)
        self.engine._on_group_message(other, None)
        self.assertEqual(len(self.plugin.adapter.group_messages), 2)

    def test_new_custom_event_rules_are_covered(self):
        # 用户新建的事件规则同样受去抖保护
        self.engine.rules = [{
            "id": "rule_custom", "name": "自定义", "enabled": True,
            "triggerType": "event", "pattern": "", "eventType": "server.player_join",
            "conditions": [], "actions": [{"type": "replyText", "params": "$userId 进服"}], "block": False,
        }]
        self.engine.handle_event("server.player_join", {"user_id": "Steve"})
        self.engine.handle_event("server.player_join", {"user_id": "Steve"})
        self.assertEqual(len(self.plugin.adapter.group_messages), 1)


if __name__ == "__main__":
    unittest.main()
