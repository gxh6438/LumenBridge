"""v1.0.8 回归：/lumen webui-host 指令（仅控制台、立即生效）、
外部手改 config.json 热重载（GET /api/config 指纹检测）、0.0.0.0 启动回显如实显示。
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import types
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.request import Request, urlopen
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from endstone_lumenbridge.config import ConfigManager
from endstone_lumenbridge.connections import ConnectionManager
from endstone_lumenbridge.event_bus import EventBus
from endstone_lumenbridge.i18n import get_i18n
from endstone_lumenbridge.plugin import LumenBridgePlugin
from endstone_lumenbridge.webui.logbuffer import LogBuffer
from endstone_lumenbridge.webui.server import WebUIServer


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


class _RecordingLogger:
    def __init__(self):
        self.infos: list[str] = []

    def info(self, msg):
        self.infos.append(str(msg))

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
    """非控制台 sender 替身（玩家，任意权限）。"""

    def __init__(self, is_op: bool = False):
        self.messages: list[str] = []
        self.is_op = is_op

    def send_message(self, msg):
        self.messages.append(str(msg))


@contextmanager
def _console_sender_module():
    """临时把 sys.modules["endstone.command"] 换成携带替身类的模块。

    _handle_webui_host_command 在函数体内 from endstone.command import
    ConsoleCommandSender —— 调用期从 sys.modules 解析，替换后 isinstance
    判定对替身类生效（与 test_lumen_password_command 同款手法）。
    """
    module = types.ModuleType("endstone.command")
    module.ConsoleCommandSender = _RecordingConsoleSender
    with patch.dict(sys.modules, {"endstone.command": module}):
        yield


class _FakeWebUI:
    """WebUI 替身：记录 refresh_config 调用，暴露 host/port 供断言。"""

    def __init__(self, host: str = "127.0.0.1", port: int = 8300):
        self.host = host
        self.port = port
        self.refresh_calls = 0

    def refresh_config(self):
        self.refresh_calls += 1


class WebUiHostCommandTests(unittest.TestCase):
    """命令层语义：控制台硬门 + local/public/IP 分支 + 立即生效回显。"""

    def _plugin(self, webui, initial_host="127.0.0.1"):
        plugin = LumenBridgePlugin.__new__(LumenBridgePlugin)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cm = ConfigManager(Path(tmp.name), _FakeLogger())
        cm.apply_patch({"webui": {"host": initial_host}})
        plugin.config_manager = cm
        plugin.webui = webui
        return plugin

    def _host(self, plugin) -> str:
        assert plugin.config_manager is not None
        return str(plugin.config_manager.data["webui"]["host"])

    def test_non_console_sender_rejected(self):
        """玩家即使 OP 也被拒：监听地址是运维决策，硬门先于权限检查。"""
        plugin = self._plugin(_FakeWebUI())
        player = _RecordingPlayerSender(is_op=True)
        with patch.object(
            ConfigManager,
            "check_command_permission",
            side_effect=AssertionError("webui-host 硬门应先于权限检查"),
        ):
            with _console_sender_module():
                self.assertTrue(
                    plugin.on_command(player, SimpleNamespace(name="lumen"), ["webui-host", "public"])
                )
        self.assertEqual(len(player.messages), 1)
        self.assertIn("控制台", player.messages[0])

    def test_switch_to_public_applies_immediately(self):
        webui = _FakeWebUI(host="127.0.0.1")
        plugin = self._plugin(webui)
        console = _RecordingConsoleSender()
        with _console_sender_module():
            self.assertTrue(plugin.on_command(console, SimpleNamespace(name="lumen"), ["webui-host", "public"]))
        self.assertEqual(self._host(plugin), "0.0.0.0")
        # 配置落盘
        assert plugin.config_manager is not None
        disk = json.loads(plugin.config_manager.path.read_text(encoding="utf-8"))
        self.assertEqual(disk["webui"]["host"], "0.0.0.0")
        # 立即生效：refresh_config 被调用
        self.assertEqual(webui.refresh_calls, 1)
        # 回显包含切换成功与所有接口提示
        self.assertTrue(any("0.0.0.0" in m for m in console.messages))
        self.assertTrue(any("立即生效" in m for m in console.messages))

    def test_switch_to_local(self):
        webui = _FakeWebUI(host="0.0.0.0")
        plugin = self._plugin(webui, initial_host="0.0.0.0")
        console = _RecordingConsoleSender()
        with _console_sender_module():
            self.assertTrue(plugin.on_command(console, SimpleNamespace(name="lumen"), ["webui-host", "local"]))
        self.assertEqual(self._host(plugin), "127.0.0.1")
        self.assertEqual(webui.refresh_calls, 1)

    def test_specific_ip(self):
        webui = _FakeWebUI()
        plugin = self._plugin(webui)
        console = _RecordingConsoleSender()
        with _console_sender_module():
            self.assertTrue(plugin.on_command(console, SimpleNamespace(name="lumen"), ["webui-host", "192.168.1.10"]))
        self.assertEqual(self._host(plugin), "192.168.1.10")
        self.assertEqual(webui.refresh_calls, 1)

    def test_greedy_message_single_string(self):
        """BDS 贪心 message 参数：'public extra' 作为单字符串到达也能解析。"""
        webui = _FakeWebUI()
        plugin = self._plugin(webui)
        console = _RecordingConsoleSender()
        with _console_sender_module():
            self.assertTrue(plugin.on_command(console, SimpleNamespace(name="lumen"), ["webui-host", "public extra"]))
        self.assertEqual(self._host(plugin), "0.0.0.0")

    def test_invalid_ip_rejected(self):
        webui = _FakeWebUI()
        plugin = self._plugin(webui)
        console = _RecordingConsoleSender()
        with _console_sender_module():
            self.assertTrue(plugin.on_command(console, SimpleNamespace(name="lumen"), ["webui-host", "not-an-ip"]))
        # 配置未变、未触碰 WebUI
        self.assertEqual(self._host(plugin), "127.0.0.1")
        self.assertEqual(webui.refresh_calls, 0)
        self.assertTrue(any("无效" in m for m in console.messages))

    def test_same_host_noop(self):
        webui = _FakeWebUI(host="0.0.0.0")
        plugin = self._plugin(webui, initial_host="0.0.0.0")
        console = _RecordingConsoleSender()
        with _console_sender_module():
            self.assertTrue(plugin.on_command(console, SimpleNamespace(name="lumen"), ["webui-host", "public"]))
        self.assertEqual(webui.refresh_calls, 0)
        self.assertTrue(any("已是" in m for m in console.messages))

    def test_usage_without_args(self):
        webui = _FakeWebUI()
        plugin = self._plugin(webui)
        console = _RecordingConsoleSender()
        with _console_sender_module():
            self.assertTrue(plugin.on_command(console, SimpleNamespace(name="lumen"), ["webui-host"]))
        self.assertEqual(webui.refresh_calls, 0)
        self.assertTrue(any("用法" in m and "127.0.0.1" in m for m in console.messages))

    def test_webui_none_stages_to_config(self):
        """WebUI 未运行：仅写配置，下次启动生效。"""
        plugin = self._plugin(None)
        console = _RecordingConsoleSender()
        with _console_sender_module():
            self.assertTrue(plugin.on_command(console, SimpleNamespace(name="lumen"), ["webui-host", "public"]))
        self.assertEqual(self._host(plugin), "0.0.0.0")
        self.assertTrue(any("下次启动" in m for m in console.messages))

    def test_case_insensitive_keyword(self):
        webui = _FakeWebUI()
        plugin = self._plugin(webui)
        console = _RecordingConsoleSender()
        with _console_sender_module():
            self.assertTrue(plugin.on_command(console, SimpleNamespace(name="lumen"), ["webui-host", "PUBLIC"]))
        self.assertEqual(self._host(plugin), "0.0.0.0")


class DummyAdapter:
    ws_type = 0
    mode_name = "Forward WS"
    is_connected = True


class DummyServer:
    online_players: list[object] = []


class DummyPlugin:
    """最小插件桩：_init_i18n 复刻真实语义，run_on_main 内联执行。"""

    VERSION = "test"

    def __init__(self, data_folder: Path, host: str = "127.0.0.1", port: int = 10252,
                 logger=None) -> None:
        self.logger = logger or _FakeLogger()
        self._tee_logger = self.logger
        self.data_folder = data_folder
        self.config_manager = ConfigManager(data_folder, self.logger)
        self.config_manager.apply_patch({
            "webui": {"password": "test-password", "secret": "s", "host": host, "port": port},
        })
        self.connections = ConnectionManager(data_folder, self.logger)
        self.config_manager.attach_connections(self.connections)
        self.log_buffer = LogBuffer()
        self.adapter = DummyAdapter()
        self.server = DummyServer()
        self.bus = EventBus(self.logger)
        self.whitelist_module = None
        self.regex_module = None
        self.subplugin_manager = None
        self._language = "en"
        self._pip_manager_lock = threading.RLock()
        self._pip_serial_lock = threading.Lock()
        self._pip_manager = None

    @property
    def language(self) -> str:
        return self._language

    def _init_i18n(self) -> None:
        configured = self.config_manager.language
        self._language = get_i18n().set_language(configured)

    def run_on_main(self, callback, delay: int = 1) -> None:
        callback()

    def bot_profile_snapshot(self) -> dict[object, object]:
        return {"qq": 1, "nickname": "Bot", "avatar_url": ""}

    def reload_onebot_connection(self) -> None:
        return None


class ExternalConfigAndHostIntegrationTests(unittest.TestCase):
    """HTTP 集成：外部手改热重载 + webui-host 真实重启监听 + 启动回显。"""

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.plugin = DummyPlugin(Path(self.tempdir.name))
        self.webui = WebUIServer(self.plugin)
        self.addCleanup(self.webui.stop)
        self.plugin.webui = self.webui
        self.webui.start()
        assert self.webui._httpd is not None
        self.port = int(self.webui._httpd.server_address[1])
        self.base = f"http://127.0.0.1:{self.port}"
        status, data = self.request("POST", "/api/auth/login", {"password": "test-password"})
        self.assertEqual(status, 200)
        self.token = str(data["data"]["token"])

    def request(self, method: str, path: str, body: object | None = None, token: str = ""):
        headers = {}
        payload = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            payload = json.dumps(body).encode("utf-8")
        if token:
            headers["Authorization"] = "Bearer " + token
        req = Request(self.base + path, data=payload, headers=headers, method=method)
        try:
            with urlopen(req, timeout=3) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_external_config_edit_hot_reloads_via_get(self):
        """手改 config.json 后 GET /api/config：返回新值 + 后端语言热生效。"""
        cfg_path = Path(self.tempdir.name) / "config.json"
        disk = json.loads(cfg_path.read_text(encoding="utf-8"))
        disk["language"] = "zh_CN"
        cfg_path.write_text(json.dumps(disk, ensure_ascii=False, indent=4), encoding="utf-8")

        status, data = self.request("GET", "/api/config", token=self.token)
        self.assertEqual(status, 200)
        # 响应即为手改后的最新值（无需刷新浏览器重新进入）
        self.assertEqual(data["data"]["language"], "zh_CN")
        # 后端热生效：插件语言已按新配置切换
        self.assertEqual(self.plugin.language, "zh_CN")

        # 无再次外部修改时，后续 GET 不再触发重载（幂等）
        status2, data2 = self.request("GET", "/api/config", token=self.token)
        self.assertEqual(status2, 200)
        self.assertEqual(data2["data"]["language"], "zh_CN")

    def test_external_config_corrupt_keeps_memory(self):
        """手改写坏 config.json：GET 不崩溃，内存配置保持现值不被破坏文件污染。"""
        cfg_path = Path(self.tempdir.name) / "config.json"
        original = cfg_path.read_text(encoding="utf-8")
        cfg_path.write_text("{ broken json !!!", encoding="utf-8")
        try:
            status, data = self.request("GET", "/api/config", token=self.token)
            self.assertEqual(status, 200)
            self.assertEqual(data["data"]["webui"]["port"], self.port)
        finally:
            cfg_path.write_text(original, encoding="utf-8")
            self.plugin.config_manager.load()

    def test_file_stamp_detects_external_change_only(self):
        """自身 save/apply_patch 不算外部修改；外部写入才报变化。"""
        cm = self.plugin.config_manager
        self.assertFalse(cm.file_changed_on_disk())
        cm.apply_patch({"pip": {"timeout": 123}})
        self.assertFalse(cm.file_changed_on_disk())
        # 外部直接写盘（模拟手改）
        cfg_path = Path(self.tempdir.name) / "config.json"
        disk = json.loads(cfg_path.read_text(encoding="utf-8"))
        disk["language"] = "zh_CN"
        cfg_path.write_text(json.dumps(disk, ensure_ascii=False, indent=4), encoding="utf-8")
        self.assertTrue(cm.file_changed_on_disk())
        cm.load()
        self.assertFalse(cm.file_changed_on_disk())

    def test_webui_host_command_restarts_real_listener(self):
        """运行中的监听 socket 按 /lumen webui-host public 立即切换到 0.0.0.0。"""
        shell = LumenBridgePlugin.__new__(LumenBridgePlugin)
        shell.config_manager = self.plugin.config_manager
        shell.webui = self.webui
        console = _RecordingConsoleSender()
        with _console_sender_module():
            self.assertTrue(shell.on_command(console, SimpleNamespace(name="lumen"), ["webui-host", "public"]))
        # 监听 socket 已重绑到 0.0.0.0（同端口）
        assert self.webui._httpd is not None
        self.assertEqual(self.webui._httpd.server_address[0], "0.0.0.0")
        self.assertEqual(self.webui._httpd.server_address[1], self.port)
        self.assertEqual(str(self.plugin.config_manager.data["webui"]["host"]), "0.0.0.0")
        # url 属性如实显示 0.0.0.0（/lumen status 使用）
        self.assertTrue(self.webui.url.startswith("http://0.0.0.0:"))

        # 切回 local：监听地址恢复仅本机
        with _console_sender_module():
            self.assertTrue(shell.on_command(console, SimpleNamespace(name="lumen"), ["webui-host", "local"]))
        assert self.webui._httpd is not None
        self.assertEqual(self.webui._httpd.server_address[0], "127.0.0.1")

    def test_startup_log_shows_real_host(self):
        """host=0.0.0.0 时启动回显如实打印 0.0.0.0 并附局域网访问提示。"""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        recorder = _RecordingLogger()
        plugin = DummyPlugin(Path(tmp.name), host="0.0.0.0", port=10253, logger=recorder)
        webui = WebUIServer(plugin)
        self.addCleanup(webui.stop)
        plugin.webui = webui
        webui.start()
        assert webui._httpd is not None
        # 启动 URL 如实显示 0.0.0.0，且补了监听所有接口的提示（两条都含 0.0.0.0）
        hits = [m for m in recorder.infos if "0.0.0.0" in m]
        self.assertGreaterEqual(len(hits), 2)
        self.assertTrue(any("http://0.0.0.0" in m for m in hits))


class StartupBannerTests(unittest.TestCase):
    """启动横幅：与 figlet standard 基准逐字符校验（曾因手改错乱：丢反斜杠/缺管道/列偏移）。"""

    # pyfiglet.figlet_format("Lumen", font="standard") 的 6 行（各 33 列）
    LUMEN = [
        " _                               ",
        "| |   _   _ _ __ ___   ___ _ __  ",
        "| |  | | | | '_ ` _ \\ / _ \\ '_ \\ ",
        "| |__| |_| | | | | | |  __/ | | |",
        "|_____\\__,_|_| |_| |_|\\___|_| |_|",
        "                                 ",
    ]
    # pyfiglet.figlet_format("Bridge", font="standard") 的 6 行（各 31 列）
    BRIDGE = [
        " ____       _     _            ",
        "| __ ) _ __(_) __| | __ _  ___ ",
        "|  _ \\| '__| |/ _` |/ _` |/ _ \\",
        "| |_) | |  | | (_| | (_| |  __/",
        "|____/|_|  |_|\\__,_|\\__, |\\___|",
        "                    |___/      ",
    ]

    def test_banner_matches_figlet_standard(self):
        """整幅横幅 = Lumen 行 + Bridge 行 的逐字符拼接。"""
        from endstone_lumenbridge.plugin import _BANNER_LINES
        expected = [l + r for l, r in zip(self.LUMEN, self.BRIDGE)]
        self.assertEqual(list(_BANNER_LINES), expected)

    def test_banner_split_alignment(self):
        """拆分点必须落在 Lumen/Bridge 边界上，左右行宽与基准一致。"""
        from endstone_lumenbridge.plugin import _BANNER_LINES, _BANNER_SPLIT
        self.assertEqual(_BANNER_SPLIT, 33)
        # 等宽 64（历史 bug：行列宽 52~64 不等导致渲染错乱）
        self.assertEqual({len(line) for line in _BANNER_LINES}, {64})
        for line, lum, bri in zip(_BANNER_LINES, self.LUMEN, self.BRIDGE):
            self.assertEqual(line[:_BANNER_SPLIT], lum)
            self.assertEqual(line[_BANNER_SPLIT:], bri)


if __name__ == "__main__":
    unittest.main()
