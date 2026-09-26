"""v1.0.7 WebUI 修复回归：总览主群含官方域 openid、语言热生效、开关单字段持久化。"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from endstone_lumenbridge.config import ConfigManager
from endstone_lumenbridge.connections import ConnectionManager
from endstone_lumenbridge.event_bus import EventBus
from endstone_lumenbridge.i18n import get_i18n
from endstone_lumenbridge.webui.logbuffer import LogBuffer
from endstone_lumenbridge.webui.server import WebUIServer


class DummyLogger:
    def info(self, _message: object) -> None:
        pass

    def warning(self, _message: object) -> None:
        pass

    def error(self, _message: object) -> None:
        pass

    def debug(self, _message: object) -> None:
        pass

    def exception(self, _message: object) -> None:
        pass


class DummyAdapter:
    ws_type = 0
    mode_name = "Forward WS"
    is_connected = True


class DummyServer:
    online_players: list[object] = []


class DummyPlugin:
    """最小插件桩：_init_i18n 复刻真实插件语义（读配置并更新 self.language）。"""

    VERSION = "test"

    def __init__(self, data_folder: Path) -> None:
        self.logger = DummyLogger()
        self._tee_logger = self.logger
        self.data_folder = data_folder
        self.config_manager = ConfigManager(data_folder, self.logger)
        self.config_manager.apply_patch({
            "webui": {"password": "test-password", "secret": "s", "port": 10250},
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

    def bot_profile_snapshot(self) -> dict[str, object]:
        return {"qq": 1, "nickname": "Bot", "avatar_url": ""}

    def reload_onebot_connection(self) -> None:
        return None


class V107WebUiFixTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        # addCleanup：setUp 中途失败时 tearDown 不会执行，端口会泄漏拖垮后续用例
        self.addCleanup(self.tempdir.cleanup)
        self.plugin = DummyPlugin(Path(self.tempdir.name))
        self.webui = WebUIServer(self.plugin)
        self.addCleanup(self.webui.stop)
        self.plugin.webui = self.webui
        self.webui.start()
        assert self.webui._httpd is not None
        self.base = f"http://127.0.0.1:{self.webui._httpd.server_address[1]}"
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

    def test_overview_main_groups_include_qqofficial_openid(self) -> None:
        """官方机器人 group_openid 必须出现在总览主群里，否则永远显示"未设置"。"""
        ws_id = next(a["id"] for a in self.plugin.connections.adapters if a["type"] == "websocket")
        qo_id = next(a["id"] for a in self.plugin.connections.adapters if a["type"] == "qqofficial")
        self.plugin.connections.update(ws_id, {"main_group": [123456]})
        self.plugin.connections.update(qo_id, {
            "app_id": "102345678",
            "app_secret": "secret-value",
            "main_group": ["R2FwaU9wZW5pZA"],
        })
        status, data = self.request("GET", "/api/overview", token=self.token)
        self.assertEqual(status, 200)
        groups = data["data"]["main_groups"]
        # 宽松并集统一返回字符串 token（数字群号与 openid 同等展示）
        self.assertIn("123456", groups)
        self.assertIn("R2FwaU9wZW5pZA", groups)

    def test_language_switch_persists_and_takes_effect_immediately(self) -> None:
        """WebUI 保存 language 后：config.json 落盘 + 插件语言热生效（无需重载）。"""
        self.assertEqual(self.plugin.language, "en")
        status, data = self.request("POST", "/api/config", {"language": "zh_CN"}, token=self.token)
        self.assertEqual(status, 200)
        # 配置落盘
        disk = json.loads((Path(self.tempdir.name) / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(disk.get("language"), "zh_CN")
        # 插件语言热更新（_init_i18n 已在保存后由主线程路径执行）
        self.assertEqual(self.plugin.language, "zh_CN")
        self.assertEqual(self.plugin.config_manager.language, "zh_CN")

    def test_enabled_single_field_toggle_persists_to_disk(self) -> None:
        """前端开关即时保存路径：单字段 {enabled} PUT 必须落盘并可重载还原。"""
        qo_id = next(a["id"] for a in self.plugin.connections.adapters if a["type"] == "qqofficial")
        status, data = self.request("PUT", f"/api/connections/{qo_id}", {"enabled": False}, token=self.token)
        self.assertEqual(status, 200)
        self.assertFalse(data["data"]["enabled"])
        # 重载后仍为关闭（reload_onebot_connection 会走 connections.load() 重读磁盘）
        self.plugin.connections.load()
        adapter = self.plugin.connections.get(qo_id)
        self.assertIsNotNone(adapter)
        assert adapter is not None
        self.assertFalse(adapter["enabled"])
        # 磁盘文件同样为关闭
        disk = json.loads((Path(self.tempdir.name) / "connections" / "qqofficial.json").read_text(encoding="utf-8"))
        stored = next(a for a in disk["adapters"] if a["id"] == qo_id)
        self.assertFalse(stored["enabled"])


if __name__ == "__main__":
    unittest.main()
