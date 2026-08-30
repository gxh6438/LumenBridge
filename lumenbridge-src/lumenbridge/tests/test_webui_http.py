from __future__ import annotations

import http.client
import json
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from endstone_lumenbridge.config import ConfigManager
from endstone_lumenbridge.event_bus import EventBus
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
    VERSION = "test"

    def __init__(self, data_folder: Path) -> None:
        self.logger = DummyLogger()
        self._tee_logger = self.logger
        self.data_folder = data_folder
        self.config_manager = ConfigManager(data_folder, self.logger)
        self.config_manager.apply_patch({
            "webui": {"password": "test-password", "secret": "super-secret", "port": 10240},
        })
        # v1.2.0 起 connection 由 connections.json（适配器卡片）承载
        from endstone_lumenbridge.connections import ConnectionManager
        self.connections = ConnectionManager(data_folder, self.logger)
        primary = self.connections.primary_websocket()
        if primary:
            # primary_websocket() 返回深拷贝快照（安全加固），写入须走 update 持久化
            self.connections.update(primary["id"], {"access_token": "onebot-secret"})
        self.config_manager.attach_connections(self.connections)
        self.config_manager.data["webui"]["port"] = 0
        self.log_buffer = LogBuffer()
        self.adapter = DummyAdapter()
        self.server = DummyServer()
        self.bus = EventBus(self.logger)
        self.whitelist_module = None
        self.regex_module = None
        self.subplugin_manager = None
        self.language = "en"
        self._pip_manager_lock = __import__("threading").RLock()
        self._pip_manager = None
        # 与 plugin.__init__ 保持一致：WebUI 与 marketplace 共享此串行锁
        self._pip_serial_lock = __import__("threading").Lock()

    def run_on_main(self, callback, delay: int = 1) -> None:
        callback()

    def bot_profile_snapshot(self) -> dict[str, object]:
        return {"qq": 12345, "nickname": "BridgeBot", "avatar_url": ""}

    def _init_i18n(self) -> None:
        pass

    def reload_onebot_connection(self) -> None:
        card = self.config_manager.connection
        mode = int(card.get("ws_type", 0) or 0)
        self.adapter = type("ReloadedAdapter", (), {
            "ws_type": mode,
            "mode_name": "Reverse WS" if mode == 1 else "Forward WS",
            "is_connected": False,
        })()


class WebUiHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.plugin = DummyPlugin(Path(self.tempdir.name))
        self.webui = WebUIServer(self.plugin)
        # WebUIServer 已捕获 port=0 用于请求临时空闲端口；恢复配置快照中的
        # 合法端口，确保后续 /api/config 全量校验不会因测试端口而失败。
        self.plugin.config_manager.data["webui"]["port"] = 10240
        self.plugin.webui = self.webui
        self.webui.start()
        assert self.webui._httpd is not None
        self.port = self.webui._httpd.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"

    def tearDown(self) -> None:
        self.webui.stop()
        self.tempdir.cleanup()

    def request(self, method: str, path: str, body: object | None = None, token: str = ""):
        headers = {}
        payload = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            payload = json.dumps(body).encode("utf-8")
        if token:
            headers["Authorization"] = "Bearer " + token
        request = Request(self.base + path, data=payload, headers=headers, method=method)
        try:
            with urlopen(request, timeout=3) as response:
                return response.status, dict(response.headers.items()), json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            return exc.code, dict(exc.headers.items()), json.loads(exc.read().decode("utf-8"))

    def login(self) -> str:
        status, _headers, data = self.request("POST", "/api/auth/login", {"password": "test-password"})
        self.assertEqual(status, 200)
        return data["data"]["token"]

    def test_login_and_config_masking_use_same_origin_security_headers(self) -> None:
        token = self.login()
        status, headers, data = self.request("GET", "/api/config", token=token)
        self.assertEqual(status, 200)
        self.assertNotIn("Access-Control-Allow-Origin", headers)
        self.assertEqual(headers.get("Referrer-Policy"), "no-referrer")
        # v1.2.0 起 connection 不再出现在 /api/config，敏感字段只保留 webui 口令/密钥
        self.assertNotIn("connection", data["data"])
        # 固定 6 个 * 掩码（不回显长度，防长度侧信道），回传时按纯星串自动还原
        self.assertEqual(data["data"]["webui"]["password"], "******")
        self.assertEqual(data["data"]["webui"]["secret"], "******")

    def test_config_route_rejects_invalid_patch_without_persistence(self) -> None:
        token = self.login()
        before = self.plugin.config_manager.data["webui"]["port"]
        status, _headers, _data = self.request("POST", "/api/config", {"webui": {"port": 0}}, token)
        self.assertEqual(status, 400)
        self.assertEqual(self.plugin.config_manager.data["webui"]["port"], before)
        status, _headers, data = self.request("POST", "/api/config", {"webui": {"port": 8500}}, token)
        self.assertEqual(status, 200)
        self.assertEqual(data["code"], 200)
        self.assertEqual(self.plugin.config_manager.data["webui"]["port"], 8500)

    def test_masked_full_config_round_trip_keeps_real_secrets(self) -> None:
        token = self.login()
        status, _headers, response = self.request("GET", "/api/config", token=token)
        self.assertEqual(status, 200)
        full_config = response["data"]
        full_config["webui"]["port"] = 8600
        status, _headers, response = self.request("POST", "/api/config", full_config, token)
        self.assertEqual(status, 200, response)
        self.assertEqual(response["code"], 200)
        # 掩码 ****** 回传时应被还原为真实密钥，而非把掩码写入配置
        self.assertEqual(self.plugin.config_manager.data["webui"]["secret"], "super-secret")
        self.assertEqual(self.plugin.config_manager.data["webui"]["password"], "test-password")
        primary = self.plugin.connections.primary_websocket()
        self.assertEqual(primary["access_token"], "onebot-secret")

    def test_framework_reload_applies_saved_connection_mode_to_overview(self) -> None:
        token = self.login()
        status, _headers, data = self.request("GET", "/api/connections", token=token)
        self.assertEqual(status, 200, data)
        primary = next(c for c in data["data"]["adapters"] if c.get("type") == "websocket")
        status, _headers, data = self.request(
            "PUT", f"/api/connections/{primary['id']}", {"ws_type": 1}, token
        )
        self.assertEqual(status, 200, data)
        status, _headers, data = self.request("GET", "/api/overview", token=token)
        self.assertEqual(status, 200)
        self.assertEqual(data["data"]["mode"], 1)
        self.assertEqual(data["data"]["mode_name"], "Reverse WS")

    def test_overview_has_machine_readable_and_display_mode_fields(self) -> None:
        token = self.login()
        status, _headers, data = self.request("GET", "/api/overview", token=token)
        self.assertEqual(status, 200)
        self.assertEqual(data["data"]["mode"], 0)
        self.assertEqual(data["data"]["mode_name"], "Forward WS")

    def test_options_does_not_grant_cross_origin_access(self) -> None:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        connection.request("OPTIONS", "/api/config", headers={"Origin": "https://attacker.invalid"})
        response = connection.getresponse()
        headers = dict(response.getheaders())
        response.read()
        connection.close()
        self.assertEqual(response.status, 405)
        self.assertNotIn("Access-Control-Allow-Origin", headers)
        self.assertEqual(headers.get("Allow"), "GET, POST, PUT, DELETE")


if __name__ == "__main__":
    unittest.main()
