"""/lumen update 命令与市场批量更新（update_all）验证。

覆盖：
1. MarketplaceClient.update_all：无待更新、混合成功/失败（单点失败不阻断）、仅更新
   available 且有 market_id 的条目并按名称排序、进度最终到 100；
2. MarketplaceClient.update：目标版本不高于当前版本时拒绝降级；
3. /lumen update 子命令：用法提示、市场未启用、未知插件、非市场来源、
   -A/--all 一键更新与单插件更新的结果回发。
"""

from __future__ import annotations

import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from endstone_lumenbridge.marketplace import MarketplaceClient, MarketplaceError  # noqa: E402


class _FakeLogger:
    def info(self, msg):
        pass

    def warning(self, msg):
        pass

    def error(self, msg):
        pass

    def exception(self, msg):
        pass


class _FakeSender:
    def __init__(self):
        self.messages = []

    def send_message(self, msg):
        self.messages.append(str(msg))


class _SyncThread:
    """threading.Thread 替身：start() 同步执行 target，测试免轮询。"""

    def __init__(self, target=None, name=None, daemon=None):
        self._target = target

    def start(self):
        self._target()


def _make_client() -> MarketplaceClient:
    tempdir = tempfile.TemporaryDirectory()
    root = Path(tempdir.name)
    data_dir = root / "plugins" / "LumenBridge"
    data_dir.mkdir(parents=True)
    config = {
        "marketplace": {"enable": True, "api_url": "http://market.test", "allow_http": True},
        "updates": {"api_url": ""},
    }
    plugin = SimpleNamespace(
        data_folder=data_dir,
        VERSION="1.0.6",
        config_manager=SimpleNamespace(data=config),
        logger=_FakeLogger(),
        _tee_logger=_FakeLogger(),
    )
    client = MarketplaceClient(plugin)
    client._tempdir = tempdir  # type: ignore[attr-defined]
    return client


class UpdateAllTests(unittest.TestCase):
    def test_no_pending_updates(self):
        client = _make_client()
        self.addCleanup(client._tempdir.cleanup)
        snapshots = {
            "aaa": {"market_id": "aaa", "local_version": "1.0.0", "latest_version": "1.0.0", "available": False},
            "bbb": {"market_id": "", "local_version": "1.0.0", "latest_version": "2.0.0", "available": True},
        }
        with patch.object(client, "check_subplugin_updates", return_value=snapshots) as check:
            result = client.update_all()
        check.assert_called_once_with(force=True)
        self.assertEqual(result["total"], 0)
        self.assertEqual(result["updated"], [])
        self.assertEqual(result["failed"], [])

    def test_single_failure_does_not_block_others(self):
        client = _make_client()
        self.addCleanup(client._tempdir.cleanup)
        snapshots = {
            "bbb": {"market_id": "bbb", "local_version": "1.0.0", "latest_version": "1.2.0", "available": True},
            "aaa": {"market_id": "aaa", "local_version": "0.9.0", "latest_version": "1.1.0", "available": True},
            "ccc": {"market_id": "ccc", "local_version": "1.0.0", "latest_version": "1.0.0", "available": False},
        }
        calls: list[str] = []
        progresses: list[int] = []

        def _fake_update(name, version="", *, update_dependencies=True, log=None, progress=None):
            calls.append((name, version))
            if name == "bbb":
                raise MarketplaceError("boom")
            return {"name": name, "version": "9.9.9"}

        with patch.object(client, "check_subplugin_updates", return_value=snapshots), \
             patch.object(client, "update", side_effect=_fake_update):
            result = client.update_all(progress=lambda pct, label="": progresses.append(pct))

        # 仅更新 available 且带 market_id 的条目，按名称排序
        self.assertEqual(calls, [("aaa", ""), ("bbb", "")])
        self.assertEqual(result["total"], 2)
        self.assertEqual([r["name"] for r in result["updated"]], ["aaa"])
        self.assertEqual(result["updated"][0]["to_version"], "9.9.9")
        self.assertEqual([r["name"] for r in result["failed"]], ["bbb"])
        self.assertIn("boom", result["failed"][0]["error"])
        self.assertEqual(progresses[-1], 100)

    def test_all_success_summary(self):
        client = _make_client()
        self.addCleanup(client._tempdir.cleanup)
        snapshots = {
            "one": {"market_id": "one", "local_version": "1.0.0", "latest_version": "1.1.0", "available": True},
            "two": {"market_id": "two", "local_version": "2.0.0", "latest_version": "2.1.0", "available": True},
        }
        with patch.object(client, "check_subplugin_updates", return_value=snapshots), \
             patch.object(client, "update", side_effect=lambda name, version="", **_kw: {"version": "latest"}):
            result = client.update_all()
        self.assertEqual(result["total"], 2)
        self.assertEqual(len(result["updated"]), 2)
        self.assertEqual(result["failed"], [])
        self.assertIn("成功 2", result["message"])
        self.assertIn("失败 0", result["message"])


class UpdateDowngradeGuardTests(unittest.TestCase):
    def test_update_rejects_downgrade_version(self):
        client = _make_client()
        self.addCleanup(client._tempdir.cleanup)
        lock = threading.Lock()
        subplugin = SimpleNamespace(
            name="demo",
            folder=Path(client.plugin.data_folder) / "demo",
            manifest={
                "version": "1.2.0",
                "_market": {"source": "marketplace", "id": "demo-plugin"},
            },
        )
        manager = SimpleNamespace(_lock=lock, subplugins={"demo": subplugin})
        client.plugin.subplugin_manager = manager  # type: ignore[attr-defined]
        with patch.object(client, "install") as install:
            with self.assertRaises(MarketplaceError):
                client.update("demo", "1.0.0")
            install.assert_not_called()

    def test_update_rejects_invalid_version_format(self):
        client = _make_client()
        self.addCleanup(client._tempdir.cleanup)
        lock = threading.Lock()
        subplugin = SimpleNamespace(
            name="demo",
            folder=Path(client.plugin.data_folder) / "demo",
            manifest={
                "version": "1.2.0",
                "_market": {"source": "marketplace", "id": "demo-plugin"},
            },
        )
        manager = SimpleNamespace(_lock=lock, subplugins={"demo": subplugin})
        client.plugin.subplugin_manager = manager  # type: ignore[attr-defined]
        with patch.object(client, "install") as install:
            with self.assertRaises(MarketplaceError):
                client.update("demo", "1.3.0; rm -rf /")
            install.assert_not_called()


class UpdateCommandTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from endstone_lumenbridge import plugin as plugin_module

        cls.plugin_module = plugin_module
        cls.PluginCls = plugin_module.LumenBridgePlugin

    def _plugin(self, marketplace, subplugin_manager):
        plugin = self.PluginCls.__new__(self.PluginCls)
        plugin.marketplace = marketplace
        plugin.subplugin_manager = subplugin_manager
        # logger 是 Endstone 基类只读 property；_cmd_log() 优先取 _tee_logger
        plugin._tee_logger = _FakeLogger()
        plugin.run_on_main = lambda fn, delay=1: fn()
        return plugin

    def _run(self, plugin, args):
        sender = _FakeSender()
        with patch.object(self.plugin_module.threading, "Thread", _SyncThread):
            plugin._handle_update_command(sender, args)
        return sender

    @staticmethod
    def _market(update_result=None, update_all_result=None, enabled=True):
        market = SimpleNamespace(enabled=enabled)
        if update_result is not None:
            market.update = lambda name, version="", **_kw: update_result
        if update_all_result is not None:
            market.update_all = lambda **_kw: update_all_result
        return market

    def test_no_args_shows_usage(self):
        sender = self._run(self._plugin(self._market(), None), [])
        self.assertEqual(len(sender.messages), 1)
        self.assertIn("用法", sender.messages[0])

    def test_market_disabled_rejected(self):
        sender = self._run(self._plugin(self._market(enabled=False), None), ["-A"])
        self.assertEqual(len(sender.messages), 1)

    def test_unknown_plugin_rejected(self):
        lock = threading.Lock()
        manager = SimpleNamespace(_lock=lock, subplugins={})
        sender = self._run(self._plugin(self._market(), manager), ["ghost"])
        self.assertEqual(len(sender.messages), 1)

    def test_non_market_plugin_rejected(self):
        lock = threading.Lock()
        subplugin = SimpleNamespace(name="demo", manifest={"version": "1.0.0", "_market": {"source": "local"}})
        manager = SimpleNamespace(_lock=lock, subplugins={"demo": subplugin})
        sender = self._run(self._plugin(self._market(), manager), ["demo"])
        self.assertEqual(len(sender.messages), 1)

    def test_update_all_flag_a(self):
        result = {
            "total": 2,
            "updated": [{"name": "a", "to_version": "1.1.0"}, {"name": "b", "to_version": "2.1.0"}],
            "failed": [],
            "message": "ok",
        }
        market = self._market(update_all_result=result)
        market.check_subplugin_updates = lambda **_kw: {}
        sender = self._run(self._plugin(market, None), ["-A"])
        self.assertTrue(any("批量更新" in m or "成功" in m for m in sender.messages))

    def test_update_all_flag_long(self):
        result = {"total": 0, "updated": [], "failed": [], "message": "none"}
        market = self._market(update_all_result=result)
        market.check_subplugin_updates = lambda **_kw: {}
        sender = self._run(self._plugin(market, None), ["--all"])
        self.assertTrue(any("没有需要更新" in m for m in sender.messages))

    def test_update_all_with_failure_reports_each(self):
        result = {
            "total": 2,
            "updated": [{"name": "a", "to_version": "1.1.0"}],
            "failed": [{"name": "b", "error": "boom"}],
            "message": "partial",
        }
        market = self._market(update_all_result=result)
        market.check_subplugin_updates = lambda **_kw: {}
        sender = self._run(self._plugin(market, None), ["-A"])
        joined = "\n".join(sender.messages)
        self.assertIn("成功 1", joined)
        self.assertIn("失败 1", joined)
        self.assertIn("b", joined)
        self.assertIn("boom", joined)

    def test_single_plugin_update_success(self):
        lock = threading.Lock()
        subplugin = SimpleNamespace(
            name="demo",
            manifest={"version": "1.0.0", "_market": {"source": "marketplace", "id": "demo-plugin"}},
        )
        manager = SimpleNamespace(_lock=lock, subplugins={"demo": subplugin})
        market = self._market(update_result={"name": "demo", "version": "1.1.0"})
        market.check_subplugin_updates = lambda **_kw: {}
        sender = self._run(self._plugin(market, manager), ["demo"])
        joined = "\n".join(sender.messages)
        self.assertIn("demo", joined)
        self.assertIn("1.1.0", joined)

    def test_single_plugin_update_failure(self):
        lock = threading.Lock()
        subplugin = SimpleNamespace(
            name="demo",
            manifest={"version": "1.0.0", "_market": {"source": "marketplace", "id": "demo-plugin"}},
        )
        manager = SimpleNamespace(_lock=lock, subplugins={"demo": subplugin})

        def _boom(name, version="", **_kw):
            raise MarketplaceError("download failed")

        market = SimpleNamespace(enabled=True, update=_boom)
        market.check_subplugin_updates = lambda **_kw: {}
        sender = self._run(self._plugin(market, manager), ["demo"])
        joined = "\n".join(sender.messages)
        self.assertIn("download failed", joined)


if __name__ == "__main__":
    unittest.main()
