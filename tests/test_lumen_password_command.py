"""/lumen password 控制台密码重置命令验证。

覆盖：
1. 命令层（on_command 路由 + _handle_password_command）：
   - 仅限控制台：非控制台 sender 一律拒绝；硬门先于 allow_in_game/OP
     权限检查（check_command_permission 不被触及），玩家即使配置
     allow_in_game=True 也不能重置密码；
   - 带密码：哈希落盘（配置文件与命令回显均不出现明文）、委托 WebUI
     实时生效、旧 token 失效；
   - 留空：清除密码 → 配置写 "*" 哨兵（下次启动随机生成）、当前运行期
     立即生成随机密码并打印控制台 stdout（不经 sender 回显）、token 失效；
   - 纯空白输入：提示无效（与"留空清除"区分），配置不变；
   - WebUI 未启用（webui=None）：仅写入配置（下次启动生效）；
   - config_manager 缺失：安全降级拒绝；
   - 设置失败（配置写入异常）：错误回显。
2. 服务器层（WebUIServer.set_webui_password / clear_webui_password）：
   - set：存储 pbkdf2 哈希且 verify_password 可验证、旧密码失效、已签发
     token 失效、空白/超长（>128）抛 ValueError；
   - clear：返回 12 位随机密码且可用于登录校验、配置持久化 "*"、
     token 失效。
3. 集成（命令层 → 真实 WebUIServer → ConfigManager 落盘）：
   - 设置/清除/超长拒绝三条路径的端到端行为与落盘校验。

运行：python3 tests/test_lumen_password_command.py
"""

from __future__ import annotations

import io
import sys
import tempfile
import threading
import types
import unittest
from contextlib import contextmanager, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

# 离线沙箱兜底：真实 endstone 缺失时注入最小 stub（与 conftest 等价），
# 使 plugin.py 可被导入；真实环境存在时不覆盖任何内容。
try:
    import endstone  # noqa: F401
except ImportError:
    def _stub(name: str) -> types.ModuleType:
        mod = sys.modules.get(name)
        if mod is None:
            mod = types.ModuleType(name)
            sys.modules[name] = mod
        return mod

    class _ColorStub:
        def __getattr__(self, _attr: str) -> str:
            return ""

    _endstone = _stub("endstone")
    _endstone.ColorFormat = _ColorStub()
    _endstone.Player = type("Player", (), {})
    _endstone.Server = type("Server", (), {})
    _cmd = _stub("endstone.command")
    for _attr in ("Command", "CommandSender", "ConsoleCommandSender", "CommandSenderWrapper"):
        if not hasattr(_cmd, _attr):
            setattr(_cmd, _attr, type(_attr, (), {}))
    _evt = _stub("endstone.event")
    for _attr in (
        "PlayerChatEvent", "PlayerDeathEvent", "PlayerJoinEvent",
        "PlayerQuitEvent", "PlayerCommandEvent", "BroadcastMessageEvent",
    ):
        if not hasattr(_evt, _attr):
            setattr(_evt, _attr, type(_attr, (), {}))
    if not hasattr(_evt, "EventPriority"):
        _prio = type("EventPriority", (), {})
        for _lvl in ("HIGHEST", "HIGH", "NORMAL", "LOW", "LOWEST", "MONITOR"):
            setattr(_prio, _lvl, _lvl.lower())
        _evt.EventPriority = _prio
    if not hasattr(_evt, "event_handler"):
        def _event_handler(func=None, *, priority=None, ignore_cancelled=False):  # noqa: ANN001
            if priority is None:
                priority = _evt.EventPriority.NORMAL

            def deco(f):  # noqa: ANN001
                f._is_event_handler = True
                f._priority = priority
                f._ignore_cancelled = ignore_cancelled
                return f

            return deco(func) if func else deco
        _evt.event_handler = _event_handler
    _plg = _stub("endstone.plugin")
    if not hasattr(_plg, "Plugin"):
        _plg.Plugin = type("Plugin", (), {})

from endstone_lumenbridge.config import ConfigManager  # noqa: E402
from endstone_lumenbridge.plugin import LumenBridgePlugin  # noqa: E402
from endstone_lumenbridge.webui import auth as auth_util  # noqa: E402
from endstone_lumenbridge.webui.logbuffer import LogBuffer  # noqa: E402
from endstone_lumenbridge.webui.server import WebUIServer  # noqa: E402


class _FakeLogger:
    def info(self, _msg):
        pass

    def warning(self, _msg):
        pass

    def error(self, _msg):
        pass

    def debug(self, _msg):
        pass

    def exception(self, _msg):
        pass


class _RecordingConsoleSender:
    """ConsoleCommandSender 替身：记录 send_message 回显。"""

    def __init__(self):
        self.messages: list[str] = []

    def send_message(self, msg):
        self.messages.append(str(msg))


class _RecordingPlayerSender:
    """非控制台 sender 替身（玩家 / 命令方块等，任意权限）。"""

    def __init__(self, is_op: bool = False):
        self.messages: list[str] = []
        self.is_op = is_op

    def send_message(self, msg):
        self.messages.append(str(msg))


@contextmanager
def _console_sender_module():
    """临时把 sys.modules["endstone.command"] 换成携带替身类的模块。

    _handle_password_command / check_command_permission 都在函数体内
    from endstone.command import ConsoleCommandSender —— 调用期从
    sys.modules 解析，替换后 isinstance 判定对替身类生效。真实
    endstone 环境同样适用（模块级已绑定的名字不受影响，退出即还原）。
    """
    module = types.ModuleType("endstone.command")
    module.ConsoleCommandSender = _RecordingConsoleSender
    with patch.dict(sys.modules, {"endstone.command": module}):
        yield


class _FakeWebUI:
    """WebUI 替身：记录 set/clear 调用，可选模拟设置失败。"""

    def __init__(self, fail_set: bool = False):
        self.set_calls: list[str] = []
        self.clear_calls = 0
        self.fail_set = fail_set

    def set_webui_password(self, pw):
        self.set_calls.append(pw)
        if self.fail_set:
            raise RuntimeError("disk full")

    def clear_webui_password(self):
        self.clear_calls += 1
        return "Rnd0mPassw0rd"


class PasswordCommandTests(unittest.TestCase):
    """命令层：路由硬门 + 参数分支 + 回显语义。"""

    @classmethod
    def setUpClass(cls):
        cls.PluginCls = LumenBridgePlugin

    def _plugin(self, webui, config=True, allow_in_game=False):
        plugin = self.PluginCls.__new__(self.PluginCls)
        if config:
            tmp = tempfile.TemporaryDirectory()
            self.addCleanup(tmp.cleanup)
            cm = ConfigManager(Path(tmp.name), _FakeLogger())
            if allow_in_game:
                cm.apply_patch({"commands": {"allow_in_game": True}})
            plugin.config_manager = cm
        else:
            plugin.config_manager = None
        plugin.webui = webui
        return plugin

    def _config_password(self, plugin) -> str:
        assert plugin.config_manager is not None
        return str(plugin.config_manager.data["webui"]["password"])

    # ------------------------------------------------------------ 控制台硬门

    def test_non_console_sender_rejected(self):
        webui = _FakeWebUI()
        plugin = self._plugin(webui)
        player = _RecordingPlayerSender(is_op=True)
        with _console_sender_module():
            # on_command 路由：玩家即使 OP 也被拒，且不给 webui 触碰密码
            self.assertTrue(plugin.on_command(player, SimpleNamespace(name="lumen"), ["password", "abc"]))
        self.assertEqual(len(player.messages), 1)
        self.assertIn("控制台", player.messages[0])
        self.assertEqual(webui.set_calls, [])
        self.assertEqual(webui.clear_calls, 0)

    def test_hard_gate_precedes_permission_and_allows_in_game(self):
        webui = _FakeWebUI()
        # 即使配置放行所有游戏内命令，password 硬门仍先于权限检查生效
        plugin = self._plugin(webui, allow_in_game=True)
        player = _RecordingPlayerSender(is_op=True)
        with _console_sender_module():
            with patch.object(
                ConfigManager,
                "check_command_permission",
                side_effect=AssertionError("password 硬门应先于权限检查"),
            ):
                self.assertTrue(
                    plugin.on_command(player, SimpleNamespace(name="lumen"), ["password", "abc"])
                )
        self.assertEqual(len(player.messages), 1)
        self.assertIn("控制台", player.messages[0])
        self.assertEqual(webui.set_calls, [])

    def test_config_manager_missing_degrades_safe(self):
        plugin = self._plugin(None, config=False)
        console = _RecordingConsoleSender()
        with _console_sender_module():
            self.assertTrue(plugin.on_command(console, SimpleNamespace(name="lumen"), ["password", "abc"]))
        self.assertEqual(len(console.messages), 1)
        self.assertIn("配置", console.messages[0])

    # ------------------------------------------------------------ 设置密码

    def test_console_sets_password(self):
        webui = _FakeWebUI()
        plugin = self._plugin(webui)
        console = _RecordingConsoleSender()
        with _console_sender_module():
            self.assertTrue(plugin.on_command(console, SimpleNamespace(name="lumen"), ["password", "NewPass123"]))
        self.assertEqual(webui.set_calls, ["NewPass123"])
        self.assertEqual(webui.clear_calls, 0)
        self.assertEqual(len(console.messages), 1)
        self.assertIn("已重置", console.messages[0])
        # 回显不含明文密码
        self.assertNotIn("NewPass123", console.messages[0])
        # 未走清除/配置直写路径：配置仍为默认哨兵
        self.assertEqual(self._config_password(plugin), "*")

    def test_console_password_keeps_internal_spaces(self):
        """贪心 message 参数：密码原样取首个参数，内部空格保留。"""
        webui = _FakeWebUI()
        plugin = self._plugin(webui)
        console = _RecordingConsoleSender()
        with _console_sender_module():
            self.assertTrue(
                plugin._handle_password_command(console, ["pass word 42"])
            )
        self.assertEqual(webui.set_calls, ["pass word 42"])

    # ------------------------------------------------------------ 清除密码

    def test_console_clears_password_and_prints_random_to_stdout(self):
        webui = _FakeWebUI()
        plugin = self._plugin(webui)
        console = _RecordingConsoleSender()
        captured = io.StringIO()
        with _console_sender_module():
            with redirect_stdout(captured):
                self.assertTrue(
                    plugin.on_command(console, SimpleNamespace(name="lumen"), ["password"])
                )
        self.assertEqual(webui.clear_calls, 1)
        self.assertEqual(webui.set_calls, [])
        self.assertEqual(len(console.messages), 1)
        self.assertIn("已清除", console.messages[0])
        # 随机密码只走控制台 stdout，不进 sender 回显
        self.assertIn("Rnd0mPassw0rd", captured.getvalue())
        self.assertNotIn("Rnd0mPassw0rd", console.messages[0])

    # ------------------------------------------------------------ 无效输入

    def test_whitespace_only_password_rejected(self):
        webui = _FakeWebUI()
        plugin = self._plugin(webui)
        console = _RecordingConsoleSender()
        with _console_sender_module():
            self.assertTrue(plugin._handle_password_command(console, ["   "]))
        self.assertEqual(webui.set_calls, [])
        self.assertEqual(webui.clear_calls, 0)
        self.assertEqual(len(console.messages), 1)
        self.assertIn("不能为空白", console.messages[0])
        self.assertEqual(self._config_password(plugin), "*")

    def test_set_failure_reported(self):
        webui = _FakeWebUI(fail_set=True)
        plugin = self._plugin(webui)
        console = _RecordingConsoleSender()
        with _console_sender_module():
            self.assertTrue(plugin._handle_password_command(console, ["boom-pass"]))
        self.assertEqual(webui.set_calls, ["boom-pass"])
        self.assertEqual(len(console.messages), 1)
        self.assertIn("失败", console.messages[0])

    # ------------------------------------------------------------ WebUI 未启用

    def test_webui_disabled_stages_password_to_config(self):
        plugin = self._plugin(None)
        console = _RecordingConsoleSender()
        with _console_sender_module():
            self.assertTrue(plugin._handle_password_command(console, ["Staged123"]))
        self.assertEqual(len(console.messages), 1)
        self.assertIn("下次启动", console.messages[0])
        stored = self._config_password(plugin)
        self.assertTrue(auth_util.is_hashed_password(stored))
        self.assertNotIn("Staged123", stored)

    def test_webui_disabled_stages_clear_sentinel(self):
        plugin = self._plugin(None)
        console = _RecordingConsoleSender()
        with _console_sender_module():
            self.assertTrue(plugin._handle_password_command(console, []))
        self.assertEqual(len(console.messages), 1)
        self.assertIn("下次启动", console.messages[0])
        # 随机密码在下次启动由 WebUI 生成；此处不应打印本运行期密码
        self.assertNotIn("已生成", console.messages[0])
        self.assertEqual(self._config_password(plugin), "*")


class _ServerDummyPlugin:
    """WebUIServer 构造所需的最小插件替身（真实 ConfigManager + LogBuffer）。"""

    VERSION = "test"

    def __init__(self, data_folder: Path, password: str = "old-password") -> None:
        self.logger = _FakeLogger()
        self._tee_logger = self.logger
        self.config_manager = ConfigManager(data_folder, self.logger)
        self.config_manager.apply_patch(
            {"webui": {"password": password, "secret": "unit-test-secret"}}
        )
        self.log_buffer = LogBuffer()
        self._pip_serial_lock = threading.Lock()


class PasswordServerTests(unittest.TestCase):
    """服务器层：set_webui_password / clear_webui_password。"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.plugin = _ServerDummyPlugin(Path(tmp.name))
        self.webui = WebUIServer(self.plugin)

    def _config_password(self) -> str:
        return str(self.plugin.config_manager.data["webui"]["password"])

    def _file_text(self) -> str:
        assert self.plugin.config_manager is not None
        return self.plugin.config_manager.path.read_text(encoding="utf-8")

    def test_set_password_hashes_and_invalidates_tokens(self):
        old_token = self.webui.auth_provider.issue_token()
        self.assertTrue(self.webui.auth_provider.verify_token(old_token))

        self.webui.set_webui_password("NewPass123")

        # 运行时立即生效：新密码可验证、旧密码失效
        self.assertTrue(auth_util.verify_password("NewPass123", self.webui.password))
        self.assertFalse(auth_util.verify_password("old-password", self.webui.password))
        self.assertTrue(self.webui._password_from_config)
        # 所有已签发 token 失效；新签发的 token 有效
        self.assertFalse(self.webui.auth_provider.verify_token(old_token))
        self.assertTrue(self.webui.auth_provider.verify_token(self.webui.auth_provider.issue_token()))
        # 哈希落盘：配置内存与文件均无明文
        stored = self._config_password()
        self.assertTrue(auth_util.is_hashed_password(stored))
        self.assertNotIn("NewPass123", self._file_text())

    def test_set_password_rejects_invalid_inputs(self):
        for bad in ("", "   ", "\t", "x" * 129):
            with self.assertRaises(ValueError):
                self.webui.set_webui_password(bad)
        self.assertEqual(self._config_password(), "old-password")

    def test_set_password_accepts_boundary_length(self):
        boundary = "x" * WebUIServer.MAX_CONSOLE_PASSWORD_LENGTH
        self.webui.set_webui_password(boundary)
        self.assertTrue(auth_util.verify_password(boundary, self.webui.password))

    def test_clear_password_returns_random_and_persists_sentinel(self):
        old_token = self.webui.auth_provider.issue_token()
        self.assertTrue(self.webui.auth_provider.verify_token(old_token))

        new_pw = self.webui.clear_webui_password()

        # 随机密码 12 位、当前运行期可用其登录（明文恒时比较路径）
        self.assertEqual(len(new_pw), 12)
        self.assertTrue(auth_util.verify_password(new_pw, self.webui.password))
        self.assertFalse(self.webui._password_from_config)
        # 配置持久化 "*" 哨兵：下次启动重新随机
        self.assertEqual(self._config_password(), "*")
        self.assertIn('"*"', self._file_text())
        self.assertNotIn(new_pw, self._file_text())
        # 旧 token 失效
        self.assertFalse(self.webui.auth_provider.verify_token(old_token))


class PasswordIntegrationTests(unittest.TestCase):
    """端到端：命令层 → 真实 WebUIServer → ConfigManager 落盘。"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dummy = _ServerDummyPlugin(Path(tmp.name))
        self.webui = WebUIServer(self.dummy)
        self.plugin = LumenBridgePlugin.__new__(LumenBridgePlugin)
        self.plugin.config_manager = self.dummy.config_manager
        self.plugin.webui = self.webui

    def _run(self, args: list[str]) -> _RecordingConsoleSender:
        console = _RecordingConsoleSender()
        with _console_sender_module():
            self.assertTrue(self.plugin._handle_password_command(console, args))
        return console

    def _config_password(self) -> str:
        assert self.plugin.config_manager is not None
        return str(self.plugin.config_manager.data["webui"]["password"])

    def test_set_password_end_to_end(self):
        old_token = self.webui.auth_provider.issue_token()
        console = self._run(["NewPass123"])
        self.assertIn("已重置", console.messages[0])
        self.assertNotIn("NewPass123", console.messages[0])
        self.assertTrue(auth_util.verify_password("NewPass123", self.webui.password))
        self.assertFalse(self.webui.auth_provider.verify_token(old_token))
        self.assertTrue(auth_util.is_hashed_password(self._config_password()))

    def test_clear_password_end_to_end(self):
        old_token = self.webui.auth_provider.issue_token()
        captured = io.StringIO()
        console = _RecordingConsoleSender()
        with _console_sender_module():
            with redirect_stdout(captured):
                self.assertTrue(self.plugin._handle_password_command(console, []))
        self.assertIn("已清除", console.messages[0])
        # stdout 打印的随机密码可用于登录，且不出现在回显/配置里
        printed = self.webui.password
        self.assertIn(printed, captured.getvalue())
        self.assertTrue(auth_util.verify_password(printed, self.webui.password))
        self.assertNotIn(printed, console.messages[0])
        self.assertEqual(self._config_password(), "*")
        self.assertFalse(self.webui.auth_provider.verify_token(old_token))

    def test_too_long_password_rejected_end_to_end(self):
        console = self._run(["x" * (WebUIServer.MAX_CONSOLE_PASSWORD_LENGTH + 1)])
        self.assertIn("不能为空白", console.messages[0])
        self.assertEqual(self._config_password(), "old-password")
        self.assertEqual(self.webui.password, "old-password")


if __name__ == "__main__":
    unittest.main(verbosity=2)
