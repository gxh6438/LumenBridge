from __future__ import annotations

import sys
import tempfile
import unittest
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


if __name__ == "__main__":
    unittest.main()
