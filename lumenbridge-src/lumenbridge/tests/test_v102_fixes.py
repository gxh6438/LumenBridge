"""v1.0.2 实测反馈修复验证。

覆盖三组修复：
1. /lumen pip 子命令贪心参数展开（BDS [message: message] 单串到达）；
2. webui.password 校验按 enable 区分 + 坏 JSON 备份与内存现值保留；
3. _reload_webui 关停 / 补启路径与 group_allowed 未就绪拒绝。

真实 endstone 环境优先（conftest 仅在缺失时注入 stub）。
"""

from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from endstone_lumenbridge.config import (  # noqa: E402
    DEFAULT_CONFIG,
    ConfigManager,
    ConfigValidationError,
    _validate_effective_config,
)


class _FakeLogger:
    def info(self, msg):
        pass

    def warning(self, msg):
        pass

    def error(self, msg):
        pass

    def debug(self, msg):
        pass

    def exception(self, msg):
        pass


class _FakeSender:
    def __init__(self):
        self.messages = []

    def send_message(self, msg):
        self.messages.append(str(msg))


class _StubPipManager:
    enable = True

    def __init__(self):
        self.installed = []
        self.uninstalled = []

    def install(self, packages, on_log=None):
        self.installed.append(list(packages))
        return True, "install ok"

    def uninstall(self, package):
        self.uninstalled.append(package)
        return True, "uninstall ok"

    def list_packages(self):
        return []


class _SyncThread:
    """threading.Thread 替身：start() 同步执行 target，测试免轮询。"""

    def __init__(self, target=None, name=None, daemon=None):
        self._target = target

    def start(self):
        self._target()


class PipGreedyArgTests(unittest.TestCase):
    """/lumen pip install|uninstall 子命令贪心参数展开（v1.0.2 修复）。"""

    @classmethod
    def setUpClass(cls):
        from endstone_lumenbridge.plugin import LumenBridgePlugin

        cls.PluginCls = LumenBridgePlugin

    def _plugin(self, mgr):
        import threading

        plugin = self.PluginCls.__new__(self.PluginCls)
        plugin._pip_manager = mgr
        plugin.subplugin_manager = None
        # __new__ 绕过初始化：线程体访问 self._pip_serial_lock，
        # pybind 实例缺该属性会落入 C++ 侧查找导致段错误，必须补齐
        plugin._pip_serial_lock = threading.Lock()
        # logger 是 Endstone 基类只读 property；__new__ 实例 C++ 侧未初始化，
        # _cmd_log() 回退到 .logger 会段错误，必须优先提供 _tee_logger
        plugin._tee_logger = _FakeLogger()
        # 后台线程结果回传：同步执行便于断言
        plugin.run_on_main = lambda fn: fn()
        return plugin

    def _run(self, args, plugin):
        sender = _FakeSender()
        plugin._handle_pip_command(sender, args)
        return sender

    def test_install_greedy_single_string(self):
        """BDS 贪心形式：'install pkg-a pkg-b' 作为单个字符串到达。"""
        mgr = _StubPipManager()
        sender = self._run(["install pkg-a pkg-b"], self._plugin(mgr))
        self.assertEqual(mgr.installed, [["pkg-a", "pkg-b"]])
        self.assertFalse(any("用法" in m for m in sender.messages))

    def test_install_separate_tokens(self):
        """分离参数形式：['install', 'pkg-a', 'pkg-b']。"""
        mgr = _StubPipManager()
        sender = self._run(["install", "pkg-a", "pkg-b"], self._plugin(mgr))
        self.assertEqual(mgr.installed, [["pkg-a", "pkg-b"]])
        self.assertFalse(any("用法" in m for m in sender.messages))

    def test_uninstall_greedy_single_string(self):
        mgr = _StubPipManager()
        sender = self._run(["uninstall some-package"], self._plugin(mgr))
        self.assertEqual(mgr.uninstalled, ["some-package"])
        self.assertFalse(any("用法" in m for m in sender.messages))

    def test_list_without_args(self):
        mgr = _StubPipManager()
        sender = self._run([], self._plugin(mgr))
        # 空列表 → 回复一条「暂无已安装的包」类提示
        self.assertEqual(len(sender.messages), 1)

    def test_unknown_sub_reports_usage_not_crash(self):
        mgr = _StubPipManager()
        sender = self._run(["frobnicate"], self._plugin(mgr))
        self.assertFalse(any("用法: /lumen pip install" in m for m in sender.messages) and not sender.messages)
        self.assertTrue(any("未知" in m or "unknown" in m.lower() for m in sender.messages))


class WebUIPasswordValidationTests(unittest.TestCase):
    """webui.password 校验按 enable 区分（v1.0.2 修复）。"""

    def _config(self, **webui):
        cfg = copy.deepcopy(DEFAULT_CONFIG)
        cfg["webui"].update(webui)
        return cfg

    def test_disabled_with_empty_password_passes(self):
        """关闭 webui + 空密码 → 校验通过（旧实现此处回退默认并覆写用户文件）。"""
        _validate_effective_config(self._config(enable=False, password=""))

    def test_enabled_with_password_passes(self):
        _validate_effective_config(self._config(enable=True, password="*"))

    def test_enabled_with_empty_password_rejected(self):
        with self.assertRaises(ConfigValidationError):
            _validate_effective_config(self._config(enable=True, password=""))


class CorruptConfigBackupTests(unittest.TestCase):
    """坏 JSON：备份 .json.corrupt + reload 保留内存现值（v1.0.2 修复）。"""

    def setUp(self):

        self.dir = Path(tempfile.mkdtemp())
        self.cm = ConfigManager(self.dir, _FakeLogger())

    def tearDown(self):
        import shutil

        shutil.rmtree(self.dir, ignore_errors=True)

    def test_corrupt_json_backup_and_memory_preserved(self):
        # 手改关闭 webui 后正常加载
        cfg_path = self.dir / "config.json"
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        cfg["webui"]["enable"] = False
        cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=4), encoding="utf-8")
        self.cm.load()
        self.assertFalse(self.cm.data["webui"]["enable"])

        # 引入尾逗号笔误 → reload 保留内存现值并备份坏文件
        cfg_path.write_text('{"debug": false,}', encoding="utf-8")
        self.cm.load()
        self.assertFalse(self.cm.data["webui"]["enable"])
        corrupt = self.dir / "config.json.corrupt"
        self.assertTrue(corrupt.is_file())
        self.assertIn('"debug": false,}', corrupt.read_text(encoding="utf-8"))

    def test_first_load_corrupt_generates_default_with_backup(self):
        (self.dir / "config.json").write_text('{"broken": tru}', encoding="utf-8")
        cm = ConfigManager(self.dir, _FakeLogger())
        self.assertTrue(cm.data["webui"]["enable"])
        self.assertTrue((self.dir / "config.json.corrupt").is_file())


class _StubWebUI:
    def __init__(self, running=True):
        self._running = running
        self.started = 0
        self.stopped = 0

    @property
    def is_running(self):
        return self._running

    def start(self):
        self.started += 1
        self._running = True

    def stop(self):
        self.stopped += 1
        self._running = False


class ReloadWebUITests(unittest.TestCase):
    """_reload_webui 关停 / 补启路径（v1.0.2 修复）。"""

    @classmethod
    def setUpClass(cls):
        from endstone_lumenbridge.plugin import LumenBridgePlugin

        cls.PluginCls = LumenBridgePlugin

    def _plugin(self, webui, webui_conf):
        plugin = self.PluginCls.__new__(self.PluginCls)
        plugin.webui = webui
        plugin.subplugin_manager = None

        class _CM:
            data = {"webui": webui_conf}

        plugin.config_manager = _CM()
        return plugin

    def test_disable_stops_running_instance(self):
        webui = _StubWebUI(running=True)
        plugin = self._plugin(webui, {"enable": False})
        plugin._reload_webui()
        self.assertEqual(webui.stopped, 1)
        self.assertFalse(webui.is_running)

    def test_reenable_restarts_stopped_instance(self):
        """曾被 disable 停掉的实例，重新 enable 后必须显式补启（旧实现漏启）。"""
        webui = _StubWebUI(running=False)
        plugin = self._plugin(webui, {"enable": True, "host": "127.0.0.1", "port": 8300})
        plugin._reload_webui()
        self.assertEqual(webui.started, 1)
        self.assertTrue(webui.is_running)


class GroupAllowedNotReadyTests(unittest.TestCase):
    """connections 未就绪 → 拒绝处理（v1.0.2 死逻辑修复）。"""

    @classmethod
    def setUpClass(cls):
        from endstone_lumenbridge.plugin import LumenBridgePlugin

        cls.PluginCls = LumenBridgePlugin

    def test_connections_none_rejects_all(self):
        plugin = self.PluginCls.__new__(self.PluginCls)
        plugin.connections = None
        self.assertFalse(plugin.group_allowed({"group_id": "G1"}))


class OfficialDocAlignmentTests(unittest.TestCase):
    """QQ 官方文档对齐：被动回复窗口与次数（bot.q.qq.com 发送消息页）。

    - 群聊被动消息有效期 5 分钟、每条消息最多回复 5 次；
    - 单聊被动消息有效期 60 分钟、最多回复 4 次（2026/01/10 由 5 次调整为 4 次）；
    - msg_seq 不填默认 1：序号从 1 起、相同 (msg_id, msg_seq) 重复发送失败。
    """

    def test_passive_limits_match_official(self):
        from endstone_lumenbridge.onebot.qqofficial.constants import (
            PASSIVE_MAX_SEQ_C2C,
            PASSIVE_MAX_SEQ_GROUP,
            PASSIVE_WINDOW_C2C,
            PASSIVE_WINDOW_GROUP,
        )

        self.assertEqual(PASSIVE_MAX_SEQ_GROUP, 5)
        self.assertEqual(PASSIVE_MAX_SEQ_C2C, 4)
        # 本地窗口略短于官方上限，留安全余量（4.5min / 55min）
        self.assertLess(PASSIVE_WINDOW_GROUP, 5 * 60)
        self.assertLess(PASSIVE_WINDOW_C2C, 60 * 60)

    def test_msg_seq_starts_at_one_and_exhausts(self):
        from endstone_lumenbridge.onebot.qqofficial.credentials import CredentialsStore

        cs = CredentialsStore()
        cs.cache_passive("g", "mid", window=60, max_seq=5)
        seqs = [cs.take_passive("g") for _ in range(5)]
        self.assertEqual([s[1] for s in seqs], [1, 2, 3, 4, 5])
        self.assertIsNone(cs.take_passive("g"))


if __name__ == "__main__":
    unittest.main()
