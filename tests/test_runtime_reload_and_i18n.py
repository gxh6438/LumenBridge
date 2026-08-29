from __future__ import annotations

import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from endstone_lumenbridge.config import DEFAULT_CONFIG
from endstone_lumenbridge.i18n import get_i18n
from endstone_lumenbridge.plugin import LumenBridgePlugin
from endstone_lumenbridge.webui.server import build_config_labels


def leaf_paths(value, prefix: str = "") -> list[str]:
    if isinstance(value, dict):
        paths: list[str] = []
        for key, child in value.items():
            paths.extend(leaf_paths(child, f"{prefix}.{key}" if prefix else key))
        return paths
    return [prefix]


class DummyLogger:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def info(self, message: object) -> None:
        self.messages.append(str(message))


class FakeAdapter:
    def __init__(self, mode: str) -> None:
        self.mode_name = mode
        self.display_name = mode
        self.started = False
        self.stopped = False
        self.is_connected = True

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True


class RuntimeReloadAndI18nTests(unittest.TestCase):
    def test_all_default_config_leaves_have_complete_labels_in_three_languages(self) -> None:
        paths = leaf_paths(DEFAULT_CONFIG)
        i18n = get_i18n()
        for language in ("en", "zh_CN", "zh_TW"):
            i18n.set_language(language)
            labels = build_config_labels()
            for path in paths:
                self.assertIn(path, labels, f"{language} lacks backend label mapping for {path}")
                self.assertTrue(labels[path].get("label"), f"{language} lacks label for {path}")
                self.assertTrue(labels[path].get("desc"), f"{language} lacks description for {path}")
        # 恢复默认语言，避免 zh_TW 泄漏污染同进程后续测试（如 security 断言简体）
        i18n.set_language("zh_CN")

    def test_connection_reload_replaces_adapter_and_updates_cached_references(self) -> None:
        # v1.2.0：reload_onebot_connection 基于 connections.json + hub 差量重建，
        # 模块与子插件上下文统一改持 hub 门面。
        online_adapter = FakeAdapter("Forward WS")
        logger = DummyLogger()
        connections = SimpleNamespace(
            load_calls=[],
            # v1.3.0：reload 末尾按 connections.adapters 重建多适配器资料基线
            adapters=[{"id": "main", "enabled": True, "type": "onebot", "name": "Main", "bot_qq": 24680}],
        )
        connections.load = lambda: connections.load_calls.append(True)
        # _rebuild_bot_profiles_baseline 经 adapters_view() 读取适配器卡片
        connections.adapters_view = lambda include_disabled=True: connections.adapters
        hub = SimpleNamespace(
            sync_calls=[],
            online=[online_adapter],
        )
        hub.sync_from_manager = lambda: hub.sync_calls.append(True)
        hub.connected = lambda: hub.online
        # _rebuild_bot_profiles_baseline 经 hub.get() 查询适配器实时连接态
        hub.get = lambda adapter_id: online_adapter
        hub.all = lambda: hub.online
        chat = SimpleNamespace(adapter=object())
        whitelist = SimpleNamespace(adapter=object())
        regex = SimpleNamespace(adapter=object())
        sub_context = SimpleNamespace(QClient=object())
        sub_manager = SimpleNamespace(
            _lock=threading.RLock(),
            subplugins={"demo": SimpleNamespace(context=sub_context)},
        )
        fake_plugin = SimpleNamespace(
            connections=connections,
            hub=hub,
            chat_sync_module=chat,
            whitelist_module=whitelist,
            regex_module=regex,
            subplugin_manager=sub_manager,
            _bot_profile_lock=threading.RLock(),
            _bot_profiles={},
            _tee_logger=logger,
            logger=logger,
            _qq_avatar_url=lambda qq: f"avatar:{qq}",
            _connection_mode_summary=lambda: "Forward WS",
            _rebuild_bot_profiles_baseline=lambda: LumenBridgePlugin._rebuild_bot_profiles_baseline(fake_plugin),
        )

        LumenBridgePlugin.reload_onebot_connection(fake_plugin)

        self.assertTrue(connections.load_calls)
        self.assertTrue(hub.sync_calls)
        self.assertIs(chat.adapter, hub)
        self.assertIs(whitelist.adapter, hub)
        self.assertIs(regex.adapter, hub)
        self.assertIs(sub_context.QClient, hub)
        self.assertEqual(fake_plugin._bot_profiles["main"]["qq"], 24680)
        self.assertEqual(fake_plugin._bot_profiles["main"]["avatar_url"], "avatar:24680")
        self.assertTrue(logger.messages)


if __name__ == "__main__":
    unittest.main()
