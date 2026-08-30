from __future__ import annotations

import importlib.metadata
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from endstone_lumenbridge.config import ConfigManager, ConfigValidationError
from endstone_lumenbridge.pip_manager import PipManager


class DummyLogger:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    def info(self, message: object) -> None:
        self.messages.append(("info", str(message)))

    def warning(self, message: object) -> None:
        self.messages.append(("warning", str(message)))

    def error(self, message: object) -> None:
        self.messages.append(("error", str(message)))


class _FakePopen:
    """PipManager.install 流式执行的 Popen 替身。

    install 通过 ``subprocess.Popen`` 逐行读 stdout 实时回调 on_log；
    替身需提供可迭代的 stdout/stderr、wait() 与 kill()。管道类参数
    （stdout=subprocess.PIPE 等）由 **_kwargs 吞掉。
    """

    def __init__(self, cmd, stdout_lines=(), stderr_lines=(), returncode=0, **_kwargs):
        self.args = cmd
        self.stdout = iter([f"{line}\n" for line in stdout_lines])
        self.stderr = iter([f"{line}\n" for line in stderr_lines])
        self.returncode = returncode

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        pass


class ConfigManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tempdir.name)
        self.manager = ConfigManager(self.data_dir, DummyLogger())

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_valid_patch_is_persisted_without_losing_sibling_keys(self) -> None:
        # v1.2.0 起 connection/admin_qq/main_group/sync 迁移至 connections.json，
        # config.json 仅基础配置；连接配置的持久化由 ConnectionManager 负责。
        updated = self.manager.apply_patch({
            "whitelist": {"bind_keyword": "绑定我"},
            "webui": {"port": 18400},
        })
        self.assertEqual(updated["whitelist"]["bind_keyword"], "绑定我")
        self.assertEqual(updated["webui"]["port"], 18400)
        self.assertTrue(updated["whitelist"]["enable"])
        persisted = json.loads((self.data_dir / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(persisted["whitelist"]["bind_keyword"], "绑定我")
        self.assertEqual(persisted["webui"]["port"], 18400)

    def test_unknown_key_is_rejected_and_does_not_change_current_config(self) -> None:
        before = json.loads(json.dumps(self.manager.data))
        with self.assertRaises(ConfigValidationError):
            self.manager.apply_patch({"webui": {"unknown_switch": True}})
        self.assertEqual(self.manager.data, before)

    def test_invalid_network_and_numeric_values_are_rejected(self) -> None:
        with self.assertRaises(ConfigValidationError):
            self.manager.apply_patch({"webui": {"port": 70000}})
        with self.assertRaises(ConfigValidationError):
            self.manager.apply_patch({"background": {"api_url": "file:///etc/passwd"}})
        with self.assertRaises(ConfigValidationError):
            self.manager.apply_patch({"webui": {"password": ""}})
        # webui.secret 设计上允许留空（留空自动生成），不应被拒绝
        self.manager.apply_patch({"webui": {"secret": ""}})

    def test_sensitive_values_can_be_kept_by_server_side_unmasking(self) -> None:
        # ConfigManager accepts actual values; masking itself is tested through the WebUI route.
        self.manager.apply_patch({"webui": {"secret": "token-value"}})
        self.assertEqual(self.manager.data["webui"]["secret"], "token-value")

    def test_deprecated_pip_allow_all_is_migrated_on_load_and_persisted_without_it(self) -> None:
        legacy = {
            "pip": {"enable": True, "index_url": "", "timeout": 300, "allow_all": True, "allow_list": ["hello_pip"]}
        }
        (self.data_dir / "config.json").write_text(json.dumps(legacy), encoding="utf-8")
        manager = ConfigManager(self.data_dir, DummyLogger())
        self.assertNotIn("allow_all", manager.pip)
        self.assertNotIn("allow_list", manager.pip)
        saved = json.loads((self.data_dir / "config.json").read_text(encoding="utf-8"))
        self.assertNotIn("allow_all", saved["pip"])
        self.assertNotIn("allow_list", saved["pip"])

    def test_stale_webui_allow_all_is_ignored_but_other_unknown_keys_remain_rejected(self) -> None:
        updated = self.manager.apply_patch({"pip": {"allow_all": True, "timeout": 301}})
        self.assertEqual(updated["pip"]["timeout"], 301)
        self.assertNotIn("allow_all", updated["pip"])


class PipManagerTests(unittest.TestCase):
    def manager(self, **pip: object) -> PipManager:
        config = {"pip": {"enable": True, "timeout": 30, **pip}}
        return PipManager(config, DummyLogger())

    def test_hello_pip_distribution_is_detected_through_its_hello_import_package(self) -> None:
        with patch("endstone_lumenbridge.pip_manager.importlib.metadata.distribution", side_effect=importlib.metadata.PackageNotFoundError), \
             patch("endstone_lumenbridge.pip_manager.importlib.metadata.packages_distributions", return_value={}), \
             patch("endstone_lumenbridge.pip_manager.importlib.machinery.PathFinder.find_spec", side_effect=lambda name: object() if name == "hello" else None):
            self.assertTrue(PipManager.check_dependency("hello_pip>=1.0"))

    def test_uninstalled_but_cached_in_sys_modules_is_detected_as_missing(self) -> None:
        """曾 import 后被 pip uninstall 的依赖不应因 sys.modules 残留被判为已安装。"""
        # 模拟 fake_pkg 在 sys.modules 中有残留 __spec__（origin 指向已删除的路径），
        # 但磁盘上不存在——模拟 pip uninstall 后只清了磁盘没清 sys.modules 的场景
        fake_spec = SimpleNamespace(origin="/nonexistent/site-packages/fake_pkg/__init__.py")
        fake_mod = SimpleNamespace(__spec__=fake_spec)
        with patch.dict("sys.modules", {"fake_pkg": fake_mod}, clear=False), \
             patch("endstone_lumenbridge.pip_manager.importlib.metadata.version", side_effect=importlib.metadata.PackageNotFoundError), \
             patch("endstone_lumenbridge.pip_manager.importlib.metadata.distribution", side_effect=importlib.metadata.PackageNotFoundError), \
             patch("endstone_lumenbridge.pip_manager.importlib.metadata.packages_distributions", return_value={}), \
             patch("endstone_lumenbridge.pip_manager.importlib.machinery.PathFinder.find_spec", return_value=None):
            self.assertFalse(PipManager.check_dependency("fake_pkg>=1.0"))

    def test_legal_pypi_requirement_is_not_gated_by_a_whitelist(self) -> None:
        manager = self.manager()
        with patch.object(manager, "dry_run", return_value=(True, "", [])) as dry_run, \
             patch.object(manager, "missing_dependencies", return_value=[]), \
             patch("endstone_lumenbridge.pip_manager.subprocess.Popen", _FakePopen):
            ok, _message = manager.install(["any-package>=1.0"])
        self.assertTrue(ok)
        dry_run.assert_called_once_with(["any-package>=1.0"], upgrade=False)

    def test_invalid_dry_run_report_fails_closed(self) -> None:
        manager = self.manager()
        fake_result = SimpleNamespace(returncode=0, stdout="not-json", stderr="")
        with patch("endstone_lumenbridge.pip_manager.subprocess.run", return_value=fake_result):
            safe, reason, conflicts = manager.dry_run(["requests>=2"])
        self.assertFalse(safe)
        self.assertTrue(reason)
        self.assertEqual(conflicts, [])

    def test_valid_dry_run_report_detects_protected_package(self) -> None:
        manager = self.manager()
        report = {"install": [{"metadata": {"name": "endstone"}}]}
        fake_result = SimpleNamespace(returncode=0, stdout=json.dumps(report), stderr="")
        with patch("endstone_lumenbridge.pip_manager.subprocess.run", return_value=fake_result):
            safe, _reason, conflicts = manager.dry_run(["endstone"])
        self.assertFalse(safe)
        self.assertEqual(conflicts, ["endstone"])

    def test_pip_commands_include_pep668_override(self) -> None:
        # v1.2.x 起 install/uninstall 一律走 sys.executable -m pip + --user
        # （uv 不尊重 PYTHONUSERBASE，会把包装到系统 Python），并注入 PEP 668 覆写。
        install_cmd = PipManager._pip_cmd(["install", "--", "hello-pip"])
        uninstall_cmd = PipManager._pip_cmd(["uninstall", "-y", "--", "hello-pip"])
        list_cmd = PipManager._pip_cmd(["list", "--format=json"])
        self.assertEqual(install_cmd[:3], [sys.executable, "-m", "pip"])
        self.assertIn("--break-system-packages", install_cmd)
        self.assertIn("--break-system-packages", uninstall_cmd)
        self.assertIn("-y", uninstall_cmd)
        # 只读命令无需 PEP 668 覆写
        self.assertNotIn("--break-system-packages", list_cmd)

    def test_dry_run_uses_standard_pip_json_report_not_uv(self) -> None:
        manager = self.manager()
        report = {"install": [{"metadata": {"name": "hello-pip"}}]}
        fake_result = SimpleNamespace(returncode=0, stdout=json.dumps(report), stderr="")
        captured: dict[str, object] = {}

        def fake_run(cmd, **_kwargs):
            captured["cmd"] = cmd
            return fake_result

        with patch("endstone_lumenbridge.pip_manager.subprocess.run", side_effect=fake_run):
            safe, _reason, conflicts = manager.dry_run(["hello-pip"])
        cmd = captured["cmd"]
        self.assertEqual(cmd[:4], [sys.executable, "-m", "pip", "install"])
        self.assertIn("--break-system-packages", cmd)
        self.assertIn("--report", cmd)
        self.assertNotIn("uv", cmd)
        self.assertTrue(safe)
        self.assertEqual(conflicts, [])

    def test_upgrade_preflight_and_install_commands_include_upgrade(self) -> None:
        manager = self.manager()
        report = {"install": [{"metadata": {"name": "hello-pip"}}]}
        captured: dict[str, object] = {}

        def fake_run(cmd, **_kwargs):
            captured.setdefault("commands", []).append(cmd)
            return SimpleNamespace(returncode=0, stdout=json.dumps(report), stderr="")

        def fake_popen(cmd, **_kwargs):
            captured.setdefault("commands", []).append(cmd)
            return _FakePopen(cmd, **_kwargs)

        with patch.object(manager, "missing_dependencies", return_value=[]), \
             patch("endstone_lumenbridge.pip_manager.subprocess.run", side_effect=fake_run), \
             patch("endstone_lumenbridge.pip_manager.subprocess.Popen", side_effect=fake_popen):
            ok, _message = manager.install(["hello-pip>=1"], upgrade=True)
        self.assertTrue(ok)
        commands = captured["commands"]
        self.assertIn("--upgrade", commands[0])
        self.assertIn("--upgrade", commands[1])


if __name__ == "__main__":
    unittest.main()
