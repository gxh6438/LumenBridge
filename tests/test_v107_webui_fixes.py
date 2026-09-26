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

    def test_qqofficial_sync_patch_persists_to_disk(self) -> None:
        """v1.0.8：QQ 官方适配器群服互通（sync）保存必须落盘且深合并不清空其他键。

        前端 bug 曾表现为官方卡片 sync 表单值从未被提交（采集逻辑位于
        提前 return 的类型分支之后），重开弹窗永远是旧值。
        """
        qo_id = next(a["id"] for a in self.plugin.connections.adapters if a["type"] == "qqofficial")
        status, data = self.request("PUT", f"/api/connections/{qo_id}", {
            "sync": {"chat_to_group_enable": False, "max_message_length": 512},
        }, token=self.token)
        self.assertEqual(status, 200)
        # 返回值：指定的 sync 键已更新，未提及的键保留（深合并语义）
        sync = data["data"]["sync"]
        self.assertFalse(sync["chat_to_group_enable"])
        self.assertEqual(sync["max_message_length"], 512)
        self.assertTrue(sync["chat_to_server_enable"])
        # 落盘可重载还原
        disk = json.loads((Path(self.tempdir.name) / "connections" / "qqofficial.json").read_text(encoding="utf-8"))
        stored = next(a for a in disk["adapters"] if a["id"] == qo_id)
        self.assertFalse(stored["sync"]["chat_to_group_enable"])
        self.assertEqual(stored["sync"]["max_message_length"], 512)
        self.plugin.connections.load()
        reloaded = self.plugin.connections.get(qo_id)
        assert reloaded is not None
        self.assertFalse(reloaded["sync"]["chat_to_group_enable"])
        self.assertEqual(reloaded["sync"]["max_message_length"], 512)


class V108WebUiWordingAndFormTests(unittest.TestCase):
    """v1.0.8 文案与表单采集回归：OneBot 措辞、适配器重建提示。"""

    LOCALES = ROOT / "src" / "endstone_lumenbridge" / "locales"

    @staticmethod
    def _locale(key: str) -> dict:
        return json.loads((V108WebUiWordingAndFormTests.LOCALES / f"{key}.json").read_text(encoding="utf-8"))

    def test_connection_reloaded_wording_is_adapter_not_onebot(self) -> None:
        """重建提示改为"适配器连接"：QQ 官方等非 OneBot 适配器不再误报 OneBot。"""
        zh_cn = self._locale("zh_CN")
        text = zh_cn["plugin"]["connection_reloaded"]
        self.assertIn("适配器", text)
        self.assertNotIn("OneBot", text)
        en = self._locale("en")
        self.assertIn("Adapter", en["plugin"]["connection_reloaded"])
        self.assertNotIn("OneBot", en["plugin"]["connection_reloaded"])

    def test_websocket_type_label_renamed_onebot(self) -> None:
        """连接管理中 WebSocket 类型卡片显示名统一改为 OneBot。"""
        for lang in ("zh_CN", "zh_TW", "en"):
            self.assertEqual(self._locale(lang)["connections"]["type_websocket"], "OneBot", lang)
        # 新建适配器的默认卡片名同步（后端三处默认名来源）
        from endstone_lumenbridge.connections import DEFAULT_ADAPTERS
        ws_default = next(a for a in DEFAULT_ADAPTERS if a["type"] == "websocket")
        self.assertEqual(ws_default["name"], "OneBot")

    def test_collect_adapter_form_gathers_sync_before_qqofficial_return(self) -> None:
        """app.js 的 sync 采集必须位于 QQ 官方类型分支（提前 return）之前。

        前端 bug 根因：官方分支 return 后才采集 ae-sync-* 表单，
        导致 QQ 官方卡片的群服互通设置永远不随保存提交。
        """
        source = (ROOT / "src" / "endstone_lumenbridge" / "webui" / "static" / "app.js").read_text(encoding="utf-8")
        start = source.index("function collectAdapterForm()")
        end = source.index("function adapterToggleEnabled")
        body = source[start:end]
        sync_pos = body.index('getElementById("ae-sync-" + key)')
        branch_pos = body.index("if (isQQOfficial)")
        self.assertLess(
            sync_pos, branch_pos,
            "sync 表单采集必须在 isQQOfficial 分支之前执行，否则官方域保存丢失群服互通设置",
        )


if __name__ == "__main__":
    unittest.main()
