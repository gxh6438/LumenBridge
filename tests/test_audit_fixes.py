"""本轮全面审计修复的专项回归测试。

覆盖：
1. QQ 官方：Resume 重放去重（seq 过滤）/ msg_id 过期清理（40034005）
   / 会话重置关闭码常量
2. 子插件加载器：同名冲突 / reload_one 校验先于卸载 / 升级递归保留数据
   / set_enabled 即时生效
3. 正则引擎：非法动作元素跳过
4. marketplace：清单检查时间原子写
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import threading
import unittest
import zipfile
from collections import OrderedDict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


class FakeLogger:
    def __init__(self) -> None:
        self.logs: list[tuple[str, str]] = []

    def _log(self, level: str, msg: object) -> None:
        self.logs.append((level, str(msg)))

    def info(self, msg: object, *a: object) -> None:
        self._log("info", msg)

    def warning(self, msg: object, *a: object) -> None:
        self._log("warning", msg)

    def error(self, msg: object, *a: object) -> None:
        self._log("error", msg)

    def debug(self, msg: object, *a: object) -> None:
        self._log("debug", msg)

    def exception(self, msg: object, *a: object) -> None:
        self._log("error", msg)


class FakeBus:
    def __init__(self) -> None:
        self.events: list[tuple[str, object]] = []

    def emit(self, event: str, payload: object = None) -> None:
        self.events.append((event, payload))


def run_async(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ======================================================================
# QQ 官方：Resume 重放去重
# ======================================================================
class ResumeReplayDedupTests(unittest.TestCase):
    def test_replayed_dispatch_filtered_by_seq(self) -> None:
        from endstone_lumenbridge.onebot.qqofficial_adapter import QQOfficialAdapter

        ad = QQOfficialAdapter(
            FakeLogger(), FakeBus(),
            app_id="102000001", app_secret="s",
            groups=["G1"], adapter_id="qo_dedup", adapter_name="dedup",
        )
        dispatched: list[int] = []

        async def fake_dispatch(msg: dict) -> None:
            dispatched.append(msg["s"])

        ad._on_dispatch = fake_dispatch  # type: ignore[method-assign]

        def feed(seq: int) -> None:
            raw = json.dumps({"op": 0, "s": seq, "t": "GROUP_MESSAGE_CREATE", "d": {}})
            run_async(ad._on_gateway_message(raw))

        feed(1)
        feed(2)
        feed(3)
        self.assertEqual(dispatched, [1, 2, 3])
        # 网关 Resume 补发重放已处理过的 seq：必须丢弃，防消息重复下发
        feed(2)
        feed(1)
        self.assertEqual(dispatched, [1, 2, 3])
        # 新事件正常放行
        feed(4)
        self.assertEqual(dispatched, [1, 2, 3, 4])

    def test_invalid_session_resets_seq(self) -> None:
        from endstone_lumenbridge.onebot.qqofficial_adapter import QQOfficialAdapter

        ad = QQOfficialAdapter(
            FakeLogger(), FakeBus(),
            app_id="102000001", app_secret="s",
            groups=["G1"], adapter_id="qo_reset", adapter_name="reset",
        )
        ad._last_seq = 42
        ad._session_id = "sess"

        async def noop() -> None:
            pass

        # op=OP_INVALID_SESSION(9)：会话失效，seq 归零后重连走全新 Identify
        with self.subTest(op="invalid_session"):
            ad._last_seq = 42
            raw = json.dumps({"op": 9, "d": {}})
            result = run_async(ad._on_gateway_message(raw))
            self.assertTrue(result)
            self.assertEqual(ad._last_seq, 0)
            self.assertEqual(ad._session_id, "")


# ======================================================================
# QQ 官方：msg_id 过期（40034005）
# ======================================================================
class MsgIdExpiredTests(unittest.TestCase):
    def test_purge_passive_removes_entry(self) -> None:
        from endstone_lumenbridge.onebot.qqofficial.credentials import CredentialsStore

        store = CredentialsStore()
        store.cache_passive("T1", "m1", window=60.0)
        store.cache_passive("T1", "m2", window=60.0)

        store.purge_passive("T1", "m1")
        ids = set()
        for _ in range(4):
            entry = store.take_passive("T1")
            if entry:
                ids.add(entry[0])
        self.assertEqual(ids, {"m2"})

        # 清空后池条目整体移除
        store.purge_passive("T1", "m2")
        self.assertIsNone(store.take_passive("T1"))
        self.assertNotIn("T1", store.passive)

    def test_sender_expired_msg_id_retries_without_credential(self) -> None:
        from endstone_lumenbridge.onebot.qqofficial.credentials import CredentialsStore
        from endstone_lumenbridge.onebot.qqofficial.sender import MessageSender
        from endstone_lumenbridge.onebot.qqofficial.utils import ApiHTTPError

        store = CredentialsStore()
        store.cache_passive("T1", "m1", window=60.0)

        requests: list[dict] = []

        class FakeAdapter:
            logger = FakeLogger()
            credentials = store
            suppress_connection_log = True

            def _retry_params(self):
                return (3, 0.0, 0.0)

            def remember_msg_scope(self, message_id, kind, target) -> None:
                pass

            async def _api_request(self, method: str, path: str, body: dict) -> dict:
                requests.append(dict(body))
                if len(requests) == 1:
                    # 首次：官方判定 msg_id 已过期
                    raise ApiHTTPError(400, '{"code":40034005}')
                return {"id": "OK"}

        sender = MessageSender(FakeAdapter())
        body = {"msg_type": 0, "content": "hi", "msg_id": "m1", "msg_seq": 1}
        result = run_async(sender.post_message("group", "T1", body, False))

        self.assertEqual(result, "ok")
        # 第二次请求必须已剥离过期凭据字段（改走主动通道）
        self.assertIn("msg_id", requests[0])
        self.assertNotIn("msg_id", requests[1])
        self.assertNotIn("msg_seq", requests[1])
        # 池中过期凭据必须被清理，后续消息不再反复取到
        self.assertIsNone(store.take_passive("T1"))

    def test_session_reset_codes_constants(self) -> None:
        from endstone_lumenbridge.onebot.qqofficial.constants import (
            AUTH_FAIL_CODES,
            SESSION_RESET_CODES,
        )

        # 会话失效（需全新 Identify）：4006 seq 无效 / 4009 会话过期 /
        # 9001 心跳超时 / 9005 服务端强制断开
        for code in (4006, 4009, 9001, 9005):
            self.assertIn(code, SESSION_RESET_CODES, f"missing reset code {code}")
        # 4004 鉴权失败：需重取 token，与会话重置集区分
        self.assertIn(4004, AUTH_FAIL_CODES)
        # 可 Resume 的常规重连码不得误入重置集（否则每次断线都丢会话）
        for code in (4000, 4001, 4003, 4007):
            self.assertNotIn(code, SESSION_RESET_CODES)


# ======================================================================
# 正则引擎：非法动作元素跳过
# ======================================================================
class RegexInvalidActionTests(unittest.TestCase):
    def _make_engine(self):
        from endstone_lumenbridge.modules.regex_engine import RegexEngineModule

        called: list[tuple] = []

        class FakePlugin:
            logger = FakeLogger()
            data_folder = tempfile.mkdtemp(prefix="lb_regex_audit_")
            whitelist_module = None

            class _CM:
                regex_engine = {"enable": True}
                main_group = 10000
                main_groups = [10000]
                admin_keys = []
                admin_qq = []

            config_manager = _CM()

            def group_allowed(self, pack):
                return True

        class FakeAdapter:
            def send_group_msg(self, gid, content):
                called.append(("send", gid, content))

        engine = RegexEngineModule.__new__(RegexEngineModule)
        engine.plugin = FakePlugin()
        engine.logger = FakeLogger()
        engine.rules = []
        engine.custom_actions = {}
        engine.adapter = FakeAdapter()
        engine._action_dedup = OrderedDict()
        engine._action_dedup_lock = threading.Lock()
        return engine, called

    def test_non_dict_action_elements_skipped(self) -> None:
        engine, called = self._make_engine()
        engine.rules = [
            {
                "name": "mixed",
                "enabled": True,
                "triggerType": "event",
                "eventType": "server.player_chat",
                "pattern": "^触发$",
                # 混入字符串 / None / 数字：全部跳过，不得中断后续动作
                "actions": [
                    "bad-string",
                    None,
                    123,
                    {"type": "replyText", "params": "第一个"},
                    {"type": "replyText", "params": "第二个"},
                ],
            }
        ]
        engine.on_mc_player_chat("Steve", "触发")
        texts = [c[2] for c in called if c[0] == "send"]
        self.assertIn("第一个", texts)
        self.assertIn("第二个", texts)


# ======================================================================
# 子插件加载器
# ======================================================================
class DummyPlugin:
    def __init__(self, data_folder: Path) -> None:
        self.data_folder = data_folder
        self.logger = FakeLogger()
        self._tee_logger = self.logger
        self.config_manager = None
        self.adapter = None
        self.env_pool = None
        self.webui = None
        self.regex_module = None
        self.subplugin_manager = None

    def _get_pip_manager(self):
        return None


class LoaderAuditTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.data = Path(self.tempdir.name)
        self.plugin = DummyPlugin(self.data)
        from endstone_lumenbridge.subplugin.loader import SubPluginManager

        self.manager = SubPluginManager(self.plugin)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def make_zip(self, name: str, version: str, main_code: str, extra: dict | None = None) -> Path:
        archive = self.data / f"{name}-{version}.zip"
        manifest = {"name": name, "version": version, "load": True, "dependencies": []}
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("lumen.json", json.dumps(manifest))
            zf.writestr("main.py", main_code)
            for path, content in (extra or {}).items():
                zf.writestr(path, content)
        return archive

    def install(self, name: str, version: str, main_code: str, extra: dict | None = None) -> tuple[bool, str, str]:
        return self.manager.install_from_zip(self.make_zip(name, version, main_code, extra))

    def test_name_conflict_second_folder_rejected(self) -> None:
        # 目录 a 与 b 的清单声明同一 name：仅先加载者生效，后者拒绝
        for folder in ("dup_a", "dup_b"):
            d = self.manager.plugins_dir / folder
            d.mkdir(parents=True)
            (d / "lumen.json").write_text(
                json.dumps({"name": "dup_plugin", "version": "1.0.0", "load": True}),
                encoding="utf-8",
            )
            (d / "main.py").write_text("def on_load(lumen):\n    pass\n", encoding="utf-8")
        self.manager.load_all()

        self.assertIn("dup_plugin", self.manager.subplugins)
        loaded = self.manager.subplugins["dup_plugin"]
        self.assertEqual(loaded.folder.name, "dup_a")
        self.assertTrue(loaded.loaded)

    def test_reload_one_corrupt_manifest_keeps_running(self) -> None:
        ok, _, name = self.install(
            "corrupt_reload", "1.0.0",
            "def on_load(lumen):\n    lumen.storage.write('state.json', {'v': 1})\n",
        )
        self.assertTrue(ok)
        # 模拟清单被写坏：reload_one 必须校验失败直接返回，不得卸载运行中实例
        (self.manager.plugins_dir / name / "lumen.json").write_text("{broken json", encoding="utf-8")
        self.assertFalse(self.manager.reload_one(name))
        self.assertTrue(self.manager.subplugins[name].loaded)

    def test_reload_one_disabled_manifest_keeps_running(self) -> None:
        ok, _, name = self.install(
            "disabled_reload", "1.0.0",
            "def on_load(lumen):\n    lumen.storage.write('state.json', {'v': 1})\n",
        )
        self.assertTrue(ok)
        manifest_path = self.manager.plugins_dir / name / "lumen.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["load"] = False
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        self.assertFalse(self.manager.reload_one(name))
        self.assertTrue(self.manager.subplugins[name].loaded)

    def test_upgrade_preserves_nested_user_data(self) -> None:
        ok, _, name = self.install(
            "nested_data", "1.0.0",
            "def on_load(lumen):\n"
            "    lumen.storage.write('data/nested/state.json', {'v': 1})\n"
            "    lumen.storage.write('config/settings.json', {'k': 'user'})\n",
        )
        self.assertTrue(ok)
        dest = self.manager.plugins_dir / name
        self.assertTrue((dest / "data" / "nested" / "state.json").is_file())

        # 升级到 2.0.0：嵌套目录的用户数据必须保留
        ok, _msg, name2 = self.install(
            "nested_data", "2.0.0",
            "def on_load(lumen):\n    pass\n",
            extra={"new_file.txt": "bundle data"},
        )
        self.assertEqual(name2, name)
        self.assertTrue(ok)
        self.assertTrue((dest / "data" / "nested" / "state.json").is_file())
        self.assertEqual(
            json.loads((dest / "config" / "settings.json").read_text(encoding="utf-8")),
            {"k": "user"},
        )
        # 新包自带文件到位
        self.assertTrue((dest / "new_file.txt").is_file())

    def test_set_enabled_takes_effect_immediately(self) -> None:
        ok, _, name = self.install(
            "toggle_me", "1.0.0",
            "def on_load(lumen):\n    lumen.storage.write('state.json', {'v': 1})\n",
        )
        self.assertTrue(ok)
        self.assertTrue(self.manager.subplugins[name].loaded)

        # 禁用：立即卸载运行实例
        self.assertTrue(self.manager.set_enabled(name, False))
        self.assertFalse(self.manager.subplugins[name].loaded)
        manifest = json.loads(
            (self.manager.plugins_dir / name / "lumen.json").read_text(encoding="utf-8")
        )
        self.assertFalse(manifest["load"])

        # 启用：立即尝试加载（与禁用对称）
        self.assertTrue(self.manager.set_enabled(name, True))
        self.assertTrue(self.manager.subplugins[name].loaded)


# ======================================================================
# marketplace：清单检查时间原子写
# ======================================================================
class MarketplaceAtomicWriteTests(unittest.TestCase):
    def test_update_manifest_check_time_leaves_no_tmp(self) -> None:
        from endstone_lumenbridge.marketplace import MarketplaceClient
        from endstone_lumenbridge.subplugin.loader import SubPluginManager

        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            plugin = DummyPlugin(data)
            manager = SubPluginManager(plugin)
            plugin.subplugin_manager = manager

            folder = manager.plugins_dir / "atomic_plugin"
            folder.mkdir(parents=True)
            manifest = {
                "name": "atomic_plugin", "version": "1.0.0", "load": True,
                "_market": {"source": "test", "last_checked_at": 0},
            }
            (folder / "lumen.json").write_text(json.dumps(manifest), encoding="utf-8")
            (folder / "main.py").write_text("def on_load(lumen):\n    pass\n", encoding="utf-8")
            from endstone_lumenbridge.subplugin.loader import SubPlugin

            manager.subplugins["atomic_plugin"] = SubPlugin(folder, manifest)

            client = MarketplaceClient(plugin)
            client._update_manifest_check_time("atomic_plugin", 12345)

            manifest_path = folder / "lumen.json"
            self.assertFalse((folder / "lumen.json.tmp").exists())
            data_on_disk = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(data_on_disk["_market"]["last_checked_at"], 12345)


if __name__ == "__main__":
    unittest.main()
