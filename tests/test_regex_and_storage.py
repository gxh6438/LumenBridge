from __future__ import annotations

import sys
import tempfile
import threading
import unittest
from collections import OrderedDict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from endstone_lumenbridge.event_bus import EventBus
from endstone_lumenbridge.modules.regex_engine import RegexEngineModule
from endstone_lumenbridge.subplugin.context import Storage


class DummyLogger:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    def info(self, message: object) -> None:
        self.messages.append(("info", str(message)))

    def warning(self, message: object) -> None:
        self.messages.append(("warning", str(message)))

    def error(self, message: object) -> None:
        self.messages.append(("error", str(message)))

    def debug(self, message: object) -> None:
        self.messages.append(("debug", str(message)))


class DummyConfig:
    def __init__(self) -> None:
        self.regex_engine = {"enable": True, "only_on_main": True, "command_timeout": 1.0}
        self.main_group = 100
        self.main_groups = [100]
        self.admin_qq: list[int] = []


class DummyAdapter:
    def __init__(self) -> None:
        self.group_messages: list[tuple[object, object]] = []

    def send_group_msg(self, group_id: object, message: object) -> None:
        self.group_messages.append((group_id, message))


class DummyPlugin:
    def __init__(self, data_folder: Path) -> None:
        self.data_folder = data_folder
        self.logger = DummyLogger()
        self.config_manager = DummyConfig()
        self.bus = EventBus(self.logger)
        self.adapter = DummyAdapter()
        self.whitelist_module = None

    def run_on_main(self, callback, delay: int = 1) -> None:
        callback()

    def group_allowed(self, pack) -> bool:
        return True


class RegexEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.plugin = DummyPlugin(Path(self.tempdir.name))
        self.engine = RegexEngineModule(self.plugin)
        self.engine.rules = [{
            "id": "welcome",
            "name": "welcome",
            "enabled": True,
            "triggerType": "event",
            "pattern": "",
            "flags": "",
            "eventType": "group.member_join",
            "conditions": [],
            "actions": [{"type": "replyText", "params": "欢迎 $userId / $0"}],
            "block": True,
        }]

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_empty_pattern_event_rule_matches_the_event(self) -> None:
        self.engine.handle_event("group.member_join", {"user_id": 42, "group_id": 100})
        self.assertEqual(self.plugin.adapter.group_messages, [(100, "欢迎 42 / 42")])

    def test_event_rule_pattern_still_filters_non_matching_target(self) -> None:
        self.engine.rules[0]["pattern"] = "^Alice$"
        self.engine.handle_event("group.member_join", {"user_id": "Bob", "group_id": 100})
        self.assertEqual(self.plugin.adapter.group_messages, [])

    def test_message_rule_with_empty_pattern_remains_disabled(self) -> None:
        self.engine.rules[0].update({"triggerType": "message", "eventType": "", "pattern": ""})
        self.engine._on_group_message({"user_id": 42, "group_id": 100, "raw_message": "anything"}, lambda *_: None)
        self.assertEqual(self.plugin.adapter.group_messages, [])

    def test_builtin_variables_in_parse(self) -> None:
        pack = {
            "user_id": 10086,
            "group_id": 123,
            "message_id": 456,
            "self_id": 999,
            "raw_message": "查白名单 @someone",
            "sender": {"nickname": "小明", "card": "群名片", "role": "admin"},
        }
        text = "$userId/$groupId/$nickname/$card/$role/$messageId/$selfId/$domain/$rawMessage"
        self.assertEqual(
            self.engine._parse_variables(text, pack, {}),
            "10086/123/小明/群名片/admin/456/999/qq/查白名单 @someone",
        )

    def test_card_falls_back_to_nickname_and_role_defaults_member(self) -> None:
        pack = {"user_id": "openid-x", "sender": {"nickname": "官方用户", "card": ""}}
        self.assertEqual(
            self.engine._parse_variables("$card/$role/$domain", pack, {}),
            "官方用户/member/qq",
        )
        pack["domain"] = "official"
        self.assertEqual(self.engine._parse_variables("$domain", pack, {}), "official")

    def test_unknown_variable_kept_verbatim_and_context_wins(self) -> None:
        pack = {"user_id": 1}
        self.assertEqual(
            self.engine._parse_variables("$nope/$result", pack, {"result": "ok"}),
            "$nope/ok",
        )


class StorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.base = Path(self.tempdir.name) / "plugin-data"
        self.storage = Storage(self.base)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_nested_relative_file_is_stored_inside_plugin_directory(self) -> None:
        self.storage.write("state/session.json", {"ok": True})
        self.assertEqual(self.storage.read("state/session.json"), {"ok": True})
        self.assertTrue((self.base / "state" / "session.json").is_file())

    def test_path_traversal_and_absolute_paths_are_rejected(self) -> None:
        for unsafe in ("../outside.json", "/tmp/outside.json"):
            with self.assertRaises(ValueError):
                self.storage.write(unsafe, {"blocked": True})
            with self.assertRaises(ValueError):
                self.storage.read(unsafe)
            with self.assertRaises(ValueError):
                self.storage.path(unsafe)


class GetOpenidCommandTests(unittest.TestCase):
    """/get openid 命令：双域 ID 查询（官方域 openid / 个人号域 QQ 号）。"""

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.plugin = DummyPlugin(Path(self.tempdir.name))
        self.engine = RegexEngineModule(self.plugin)
        self.base_pack = {
            "group_id": "G100",
            "user_id": "U777",
            "self_id": "APP1",
            "domain": "official",
            "message": [],
        }

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def _run(self, text: str, pack: dict | None = None) -> list[str]:
        self.plugin.adapter.group_messages.clear()
        self.engine._handle_get_openid(pack or self.base_pack, text)
        return [str(m) for _, m in self.plugin.adapter.group_messages]

    def test_no_args_returns_group_and_self_id(self) -> None:
        body = "\n".join(self._run("/get openid"))
        self.assertIn("G100", body)
        self.assertIn("U777", body)

    def test_at_member_official_domain_returns_openid(self) -> None:
        pack = {**self.base_pack, "message": [
            {"type": "at", "data": {"qq": "APP1"}},
            {"type": "at", "data": {"qq": "OPENID_ABC"}},
        ]}
        self.assertTrue(any("OPENID_ABC" in m for m in self._run("/get openid", pack)))

    def test_at_member_qq_domain_returns_qq_number(self) -> None:
        pack = {**self.base_pack, "domain": "qq", "message": [
            {"type": "at", "data": {"qq": "123456"}},
        ]}
        self.assertTrue(any("123456" in m for m in self._run("/get openid", pack)))

    def test_qq_number_arg_domain_aware(self) -> None:
        # 官方域：QQ 号查询提示不支持
        self.assertTrue(
            any("不支持" in m or "无法" in m or "不能" in m for m in self._run("/get openid 123456"))
        )
        # 个人号域：QQ 号即用户 ID
        pack = {**self.base_pack, "domain": "qq"}
        self.assertTrue(any("123456" in m for m in self._run("/get openid 123456", pack)))

    def test_case_insensitive_and_extra_spaces(self) -> None:
        messages = self._run("/GET   OpenID")
        self.assertEqual(len(messages), 1)

    def test_other_messages_not_intercepted(self) -> None:
        self.assertFalse(self.engine._handle_get_openid(self.base_pack, "/get ip"))


class RegexInvalidActionTests(unittest.TestCase):
    """规则动作列表混入非 dict 元素：全部跳过，不得中断后续合法动作。"""

    def test_non_dict_action_elements_skipped(self) -> None:
        class _Plugin:
            logger = DummyLogger()
            data_folder = tempfile.mkdtemp(prefix="lb_regex_audit_")
            whitelist_module = None
            config_manager = DummyConfig()

            def group_allowed(self, pack):
                return True

        engine = RegexEngineModule.__new__(RegexEngineModule)
        engine.plugin = _Plugin()
        engine.logger = DummyLogger()
        engine.rules = [{
            "name": "mixed",
            "enabled": True,
            "triggerType": "event",
            "eventType": "server.player_chat",
            "pattern": "^触发$",
            "actions": [
                "bad-string",
                None,
                123,
                {"type": "replyText", "params": "第一个"},
                {"type": "replyText", "params": "第二个"},
            ],
        }]
        engine.custom_actions = {}
        engine.adapter = DummyAdapter()
        engine._action_dedup = OrderedDict()
        engine._action_dedup_lock = threading.Lock()
        engine.on_mc_player_chat("Steve", "触发")
        texts = [str(m) for _, m in engine.adapter.group_messages]
        self.assertIn("第一个", texts)
        self.assertIn("第二个", texts)


if __name__ == "__main__":
    unittest.main()
