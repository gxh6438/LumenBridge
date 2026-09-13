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
        # 首次登录后明文密码自动迁移为哈希存储（密码不变，登录态不受影响）
        stored_pw = self.plugin.config_manager.data["webui"]["password"]
        self.assertTrue(stored_pw.startswith("pbkdf2_sha256$"), stored_pw)
        # 迁移后仍可用原密码登录（哈希校验路径）
        status, _headers, data = self.request("POST", "/api/auth/login", {"password": "test-password"})
        self.assertEqual(status, 200, data)
        status, _headers, response = self.request("GET", "/api/config", token=token)
        self.assertEqual(status, 200)
        full_config = response["data"]
        full_config["webui"]["port"] = 8600
        status, _headers, response = self.request("POST", "/api/config", full_config, token)
        self.assertEqual(status, 200, response)
        self.assertEqual(response["code"], 200)
        # 掩码 ****** 回传时应被还原为真实密钥，而非把掩码写入配置
        self.assertEqual(self.plugin.config_manager.data["webui"]["secret"], "super-secret")
        # 掩码还原的是已迁移的哈希串，而非明文
        self.assertEqual(self.plugin.config_manager.data["webui"]["password"], stored_pw)
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

    # ─── 静态样式分层（lumen.css 共享层 / app.css 主面板层）───
    # 子插件页面在 iframe 中通过 <link href="/lumen.css"> 引用共享样式，
    # link 请求不携带鉴权 token，静态路由必须保持免鉴权可访问。

    def fetch_static(self, path: str, headers: dict[str, str] | None = None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        connection.request("GET", path, headers=headers or {})
        response = connection.getresponse()
        resp_headers = dict(response.getheaders())
        body = response.read()
        connection.close()
        return response.status, resp_headers, body

    def test_static_css_layers_served_with_css_mime_and_etag_revalidation(self) -> None:
        static_dir = ROOT / "src" / "endstone_lumenbridge" / "webui" / "static"
        for name in ("lumen.css", "app.css"):
            with self.subTest(asset=name):
                # 免鉴权访问（子插件 iframe 内 link 场景）
                status, headers, body = self.fetch_static(f"/{name}")
                self.assertEqual(status, 200)
                self.assertEqual(headers.get("Content-Type"), "text/css; charset=utf-8")
                # js/css 走 no-cache：每次重验证，ETag 命中即 304
                self.assertEqual(headers.get("Cache-Control"), "no-cache")
                etag = headers.get("ETag")
                self.assertTrue(etag)
                self.assertEqual(body, (static_dir / name).read_bytes())

                status, headers, _body = self.fetch_static(f"/{name}", {"If-None-Match": etag})
                self.assertEqual(status, 304)
                self.assertEqual(headers.get("ETag"), etag)

    def test_static_css_layers_negotiate_gzip(self) -> None:
        static_dir = ROOT / "src" / "endstone_lumenbridge" / "webui" / "static"
        import gzip as gzip_mod

        for name in ("lumen.css", "app.css"):
            with self.subTest(asset=name):
                status, headers, body = self.fetch_static(f"/{name}", {"Accept-Encoding": "gzip"})
                self.assertEqual(status, 200)
                self.assertEqual(headers.get("Content-Encoding"), "gzip")
                self.assertEqual(headers.get("Vary"), "Accept-Encoding")
                self.assertEqual(
                    gzip_mod.decompress(body),
                    (static_dir / name).read_bytes(),
                )

                # 不支持 gzip 的客户端拿到未压缩原文
                status, headers, body = self.fetch_static(f"/{name}")
                self.assertEqual(status, 200)
                self.assertNotIn("Content-Encoding", headers)
                self.assertEqual(body, (static_dir / name).read_bytes())


class PasswordHashTests(unittest.TestCase):
    """管理员密码哈希存储（pbkdf2_sha256$iters$salt$digest）。"""

    def test_hash_round_trip_and_rejects_wrong_password(self) -> None:
        from endstone_lumenbridge.webui import auth as auth_util
        stored = auth_util.hash_password("s3cret!")
        self.assertTrue(auth_util.is_hashed_password(stored))
        self.assertFalse(auth_util.is_hashed_password("s3cret!"))
        # 每次哈希带独立随机盐
        self.assertNotEqual(stored, auth_util.hash_password("s3cret!"))
        self.assertTrue(auth_util.verify_password("s3cret!", stored))
        self.assertFalse(auth_util.verify_password("wrong", stored))
        self.assertFalse(auth_util.verify_password("", stored))
        # 旧明文格式仍可直接比较（兼容）
        self.assertTrue(auth_util.verify_password("plain", "plain"))
        self.assertFalse(auth_util.verify_password("plain", "other"))

    def test_malformed_hash_and_iteration_cap_rejected(self) -> None:
        from endstone_lumenbridge.webui import auth as auth_util
        for bad in ("pbkdf2_sha256$abc$00$00", "pbkdf2_sha256$100$zz$00",
                    "pbkdf2_sha256$0$00$00", "pbkdf2_sha256$999999999$00$00"):
            with self.subTest(stored=bad):
                self.assertFalse(auth_util.verify_password("x", bad))


class SessionSecretTests(unittest.TestCase):
    """会话签名密钥进程化：热重载保会话、进程重启必失效。"""

    def _new_webui(self, tmp: Path, with_secret: bool):
        from endstone_lumenbridge.webui.server import _PROCESS_SECRET_ATTR
        plugin = DummyPlugin(tmp)
        # DummyPlugin.__init__ 把端口置 0 用于请求临时空闲端口；恢复合法端口，
        # 否则后续 apply_patch 的全量校验会因端口范围失败
        plugin.config_manager.data["webui"]["port"] = 10240
        if not with_secret:
            plugin.config_manager.apply_patch({"webui": {"secret": ""}})
            plugin.config_manager.data["webui"]["secret"] = ""
        webui = WebUIServer(plugin)
        plugin.webui = webui
        return webui, _PROCESS_SECRET_ATTR

    def test_hot_reload_reuses_process_secret_but_restart_invalidates(self) -> None:
        import sys as _sys
        tmp1 = Path(tempfile.mkdtemp())
        webui, attr = self._new_webui(tmp1, with_secret=False)
        token = webui.auth_provider.issue_token()
        # 同进程重建实例（/lumen reload 场景）：密钥复用，token 仍有效
        webui2, _ = self._new_webui(Path(tempfile.mkdtemp()), with_secret=False)
        self.assertEqual(webui.secret, webui2.secret)
        self.assertTrue(webui2.auth_provider.verify_token(token))
        # 模拟进程重启（新进程无 sys 属性）：旧 token 全部失效
        old_secret = getattr(_sys, attr)
        delattr(_sys, attr)
        try:
            webui3, _ = self._new_webui(Path(tempfile.mkdtemp()), with_secret=False)
            self.assertNotEqual(webui3.secret, old_secret)
            self.assertFalse(webui3.auth_provider.verify_token(token))
        finally:
            setattr(_sys, attr, old_secret)

    def test_legacy_disk_secret_file_is_cleaned_up(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        legacy = tmp / "data" / "webui_secret.txt"
        legacy.parent.mkdir(parents=True, exist_ok=True)
        legacy.write_text("stale-secret-from-old-version", encoding="utf-8")
        webui, _ = self._new_webui(tmp, with_secret=False)
        self.assertFalse(legacy.exists())
        self.assertNotEqual(webui.secret, "stale-secret-from-old-version")

    def test_configured_secret_still_wins(self) -> None:
        webui, _ = self._new_webui(Path(tempfile.mkdtemp()), with_secret=True)
        self.assertEqual(webui.secret, "super-secret")


class ConfigSavePasswordHashingTests(unittest.TestCase):
    """配置页修改密码：落盘前哈希、旧 token 立即失效。"""

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.plugin = DummyPlugin(Path(self.tempdir.name))
        self.plugin.config_manager.data["webui"]["port"] = 10240
        self.webui = WebUIServer(self.plugin)
        self.plugin.webui = self.webui
        self.webui.start()
        self.port = self.webui._httpd.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        self.token = self._login("test-password")
        # 登录触发明文→哈希迁移
        self.assertTrue(self.plugin.config_manager.data["webui"]["password"].startswith("pbkdf2_sha256$"))

    def tearDown(self) -> None:
        self.webui.stop()
        self.tempdir.cleanup()

    def _request(self, method: str, path: str, body: object | None = None, token: str = ""):
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
                return response.status, json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def _login(self, password: str) -> str:
        status, data = self._request("POST", "/api/auth/login", {"password": password})
        assert status == 200, data
        return data["data"]["token"]

    def test_new_password_saved_as_hash_and_takes_effect(self) -> None:
        status, data = self._request("POST", "/api/config", {"webui": {"password": "brand-new-pw"}}, self.token)
        self.assertEqual(status, 200, data)
        stored = self.plugin.config_manager.data["webui"]["password"]
        self.assertTrue(stored.startswith("pbkdf2_sha256$"), stored)
        self.assertNotIn("brand-new-pw", stored)
        # 改密后旧 token 失效，需用新密码重新登录
        status, _ = self._request("GET", "/api/overview", token=self.token)
        self.assertEqual(status, 401)
        self._login("brand-new-pw")
        status, _ = self._request("GET", "/api/overview", token=self._login("brand-new-pw"))
        self.assertEqual(status, 200)

    def test_masked_password_roundtrip_keeps_existing_hash(self) -> None:
        before = self.plugin.config_manager.data["webui"]["password"]
        status, data = self._request("POST", "/api/config", {"webui": {"password": "******"}}, self.token)
        self.assertEqual(status, 200, data)
        # 掩码回传被还原为原哈希，不会被二次哈希或清空
        self.assertEqual(self.plugin.config_manager.data["webui"]["password"], before)


if __name__ == "__main__":
    unittest.main()
