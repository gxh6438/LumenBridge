"""规则测试页（WebUI 聊天沙盒）后端逻辑测试

覆盖 RegexEngineModule.test_message：
- 命中返回规则 / 捕获组 / 动作预览，且绝不真实执行（不发生消息、不执行命令）；
- block 阻断语义：命中阻断规则后停止评估并标记 blocked_after；
- 条件判断与身份模拟（群管理员 / 后台管理员忠实复用 _check_conditions）。
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


class DummyLogger:
    def info(self, msg): pass
    def warning(self, msg): pass
    def error(self, msg): pass
    def debug(self, msg): pass


class DummyConfig:
    def __init__(self, admin_keys=()):
        self.regex_engine = {"enable": True, "only_on_main": False, "command_timeout": 1.0}
        self.main_group = 100
        self.main_groups = [100]
        self.admin_keys = frozenset(str(k) for k in admin_keys)


class DummyAdapter:
    def __init__(self):
        self.group_messages = []
        self.commands = []

    def send_group_msg(self, group_id, message):
        self.group_messages.append((group_id, message))


class DummyPlugin:
    def __init__(self, data_folder: Path, admin_keys=()):
        self.data_folder = data_folder
        self.logger = DummyLogger()
        self.config_manager = DummyConfig(admin_keys)
        self.bus = EventBus(self.logger)
        self.adapter = DummyAdapter()
        self.whitelist_module = None

    def group_allowed(self, pack):
        return True

    def run_on_main(self, fn):
        # 测试模式不应走到这里；走到即视为真实执行，记录以便断言
        self.adapter.commands.append(fn)
        fn()


class RuleTesterTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.plugin = DummyPlugin(Path(self.tempdir.name), admin_keys=("88888",))
        self.engine = RegexEngineModule(self.plugin)
        self.plugin.regex_module = self.engine

    def tearDown(self):
        self.tempdir.cleanup()

    def _set_rules(self, rules):
        self.engine.save_rules(rules)

    def test_hit_returns_preview_without_executing(self):
        self._set_rules([{
            "id": "r1", "name": "查服", "enabled": True, "triggerType": "message",
            "pattern": "^查服$", "flags": "i", "eventType": "", "conditions": [],
            "actions": [
                {"type": "executeCommand", "params": "list"},
                {"type": "replyText", "params": "$result"},
            ],
            "block": True,
        }])
        data = self.engine.test_message("查服", nickname="测试玩家")
        self.assertTrue(data["matched"])
        self.assertEqual(len(data["results"]), 1)
        r = data["results"][0]
        self.assertEqual(r["name"], "查服")
        self.assertEqual(r["groups"], ["查服"])
        # 动作预览：$result 原样保留（命令未执行、无输出可代入，原样展示更直观）
        previews = {a["type"]: a for a in r["actions"]}
        self.assertEqual(previews["executeCommand"]["preview"], "list")
        self.assertEqual(previews["replyText"]["preview"], "$result")
        # 关键：绝不真实执行 —— 无消息发送、无命令派发
        self.assertEqual(self.plugin.adapter.group_messages, [])
        self.assertEqual(self.plugin.adapter.commands, [])

    def test_capture_groups_and_reply_at_preview(self):
        self._set_rules([{
            "id": "r2", "name": "执行", "enabled": True, "triggerType": "message",
            "pattern": "^执行 (.+)$", "flags": "", "eventType": "", "conditions": [],
            "actions": [{"type": "replyAtText", "params": "已执行：$1"}],
            "block": False,
        }])
        data = self.engine.test_message("执行 say hi", nickname="小明")
        self.assertTrue(data["matched"])
        r = data["results"][0]
        self.assertEqual(r["groups"], ["执行 say hi", "say hi"])
        act = r["actions"][0]
        self.assertEqual(act["type"], "replyAtText")
        self.assertIn("@小明", act["preview"])
        self.assertIn("say hi", act["preview"])

    def test_no_match_returns_empty(self):
        self._set_rules([{
            "id": "r3", "name": "查服", "enabled": True, "triggerType": "message",
            "pattern": "^查服$", "flags": "", "eventType": "", "conditions": [],
            "actions": [{"type": "replyText", "params": "x"}], "block": False,
        }])
        data = self.engine.test_message("你好")
        self.assertFalse(data["matched"])
        self.assertEqual(data["results"], [])

    def test_block_stops_evaluation_and_flags_blocked_after(self):
        self._set_rules([
            {"id": "b1", "name": "A", "enabled": True, "triggerType": "message",
             "pattern": "^a", "flags": "", "eventType": "", "conditions": [],
             "actions": [], "block": True},
            {"id": "b2", "name": "B", "enabled": True, "triggerType": "message",
             "pattern": "^ab", "flags": "", "eventType": "", "conditions": [],
             "actions": [], "block": False},
        ])
        data = self.engine.test_message("abc")
        self.assertEqual([r["id"] for r in data["results"]], ["b1"])
        self.assertTrue(data["blocked_after"])

    def test_lumenadmin_condition_uses_real_admin_key(self):
        self._set_rules([{
            "id": "adm", "name": "管理执行", "enabled": True, "triggerType": "message",
            "pattern": "^执行", "flags": "", "eventType": "",
            "conditions": [{"field": "userRole", "operator": "==", "value": "lumenadmin"}],
            "actions": [], "block": False,
        }])
        # 普通成员身份不命中
        self.assertFalse(self.engine.test_message("执行 list")["matched"])
        # 后台管理员身份命中，且模拟 user_id 取真实 admin_keys
        data = self.engine.test_message("执行 list", role="lumenadmin")
        self.assertTrue(data["matched"])
        self.assertEqual(data["simulated_user_id"], "88888")
        self.assertEqual(data["admin_note"], "lumenadmin")

    def test_lumenadmin_without_configured_admin_reports_note(self):
        self.plugin.config_manager.admin_keys = frozenset()
        data = self.engine.test_message("执行 list", role="lumenadmin")
        self.assertEqual(data["admin_note"], "no_lumenadmin")
        self.assertFalse(data["matched"])

    def test_group_admin_role_simulation(self):
        self._set_rules([{
            "id": "ga", "name": "管理专属", "enabled": True, "triggerType": "message",
            "pattern": "^封", "flags": "", "eventType": "",
            "conditions": [{"field": "userRole", "operator": "==", "value": "admin"}],
            "actions": [], "block": False,
        }])
        self.assertFalse(self.engine.test_message("封人", role="member")["matched"])
        self.assertTrue(self.engine.test_message("封人", role="admin")["matched"])
        # 引擎按精确字符串比较身份：owner ≠ admin，群主需单写 owner 条件
        self.assertFalse(self.engine.test_message("封人", role="owner")["matched"])


if __name__ == "__main__":
    unittest.main()
