from __future__ import annotations

import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from endstone_lumenbridge.subplugin.loader import SubPluginManager
from endstone_lumenbridge.webui.server import WebUIServer


class ImmediatePlugin:
    def __init__(self, reload_result: bool) -> None:
        self.subplugin_manager = ReloadManager(reload_result)
        self.calls: list[tuple[object, int]] = []

    def run_on_main(self, fn, delay: int = 1) -> None:
        self.calls.append((fn, delay))
        fn()


class ReloadManager:
    def __init__(self, result: bool) -> None:
        self.result = result
        self._lock = threading.RLock()
        self.subplugins = {}
        self.names: list[str] = []

    def reload_one(self, name: str) -> bool:
        self.names.append(name)
        return self.result


class SuccessfulPip:
    def install(self, packages, on_log=None):
        if on_log:
            on_log("installed " + " ".join(packages))
        return True, "dependencies installed"


class ReloadWorkflowTests(unittest.TestCase):
    def worker(self, reload_result: bool) -> WebUIServer:
        worker = object.__new__(WebUIServer)
        worker.plugin = ImmediatePlugin(reload_result)
        worker._pip_tasks = {}
        worker._pip_tasks_lock = threading.RLock()
        # 与 WebUIServer.__init__ 保持一致：_start_pip_task 内部会持此锁串行化 pip 调用
        worker._pip_serial_lock = threading.Lock()
        # 任务完成时失效 pip list 缓存需要（否则后台线程 AttributeError）
        worker._pip_list_lock = threading.Lock()
        worker._pip_list_cache = None
        return worker

    @staticmethod
    def wait_task(worker: WebUIServer, task_id: str) -> dict:
        deadline = time.time() + 3
        while time.time() < deadline:
            with worker._pip_tasks_lock:
                task = dict(worker._pip_tasks[task_id])
            if task.get("done"):
                return task
            time.sleep(0.01)
        raise AssertionError("pip task did not finish")

    def test_dependency_install_waits_for_user_confirmation_before_reloading(self) -> None:
        worker = self.worker(True)
        task_id = worker._start_pip_task(SuccessfulPip(), ["hello-pip"], "install_deps", "demo")
        task = self.wait_task(worker, task_id)
        self.assertTrue(task["installation_success"])
        self.assertIsNone(task["reload_success"])
        self.assertTrue(task["reload_required"])
        self.assertTrue(task["success"])
        self.assertEqual(worker.plugin.subplugin_manager.names, [])
        self.assertEqual(worker.plugin.calls, [])
        self.assertIn("确认后重载", task["msg"])

    def test_explicit_internal_reload_reports_reload_failure_without_claiming_success(self) -> None:
        worker = self.worker(False)
        task_id = worker._start_pip_task(
            SuccessfulPip(), ["hello-pip"], "install_deps", "demo", reload_after_install=True
        )
        task = self.wait_task(worker, task_id)
        self.assertTrue(task["installation_success"])
        self.assertFalse(task["reload_success"])
        self.assertFalse(task["success"])
        self.assertFalse(task["reload_required"])
        self.assertIn("依赖已安装", task["msg"])
        self.assertIn("重载失败", task["msg"])

    def test_loader_invalidates_import_cache_before_loading_plugin(self) -> None:
        # _load_one 会在实际依赖 find_spec 前调用 invalidate_caches。最小夹具随后因
        # 缺少 main.py 正常进入加载失败分支，不需要真实 pip 安装。
        manager = object.__new__(SubPluginManager)
        manager.plugin = object()
        manager.logger = SimpleNamespace(info=lambda _m: None, warning=lambda _m: None, error=lambda _m: None)
        manager.subplugins = {}
        with tempfile.TemporaryDirectory() as temporary:
            plugin = SimpleNamespace(
                folder=Path(temporary) / "cache_test", name="cache_test",
                manifest={"dependencies": []}, loaded=False, error="", module=None,
                context=None, missing_deps=[], missing_modules=[],
            )
            plugin.folder.mkdir()
            with patch("endstone_lumenbridge.subplugin.loader.importlib.invalidate_caches") as invalidate:
                self.assertFalse(manager._load_one(plugin))
        invalidate.assert_called_once()

    def test_webui_contains_single_reload_and_correct_manual_install_contract(self) -> None:
        app = (ROOT / "src" / "endstone_lumenbridge" / "webui" / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn("reloadSingleSubplugin", app)
        self.assertIn("/reload`", app)
        self.assertIn("uv pip install --system --break-system-packages --", app)
        self.assertIn("manual_command_copied", app)
        self.assertIn("reloadRequired", app)
        self.assertIn("dependencies_installed", app)
        self.assertNotIn('toast(t("subplugins.error_copied"));\n}\n\nfunction copySubpluginError', app)


if __name__ == "__main__":
    unittest.main()
