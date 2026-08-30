from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from test_qqofficial import FakeLogger  # noqa: E402
from endstone_lumenbridge.connections import ConnectionManager  # noqa: E402

SCRIPT = ROOT / "scripts" / "migrate_storage.py"

WS_ONLY_FIELDS = ("ws_type", "target", "listen_host", "listen_port", "access_token")


def write_json(path: Path, payload) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


class StorageLayoutTests(unittest.TestCase):
    """v1.1.0 目录化存储：connections/ 按类型分文件 + data/ 运行数据目录。"""

    def test_fresh_install_creates_per_type_files(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            ConnectionManager(Path(td), FakeLogger())
            conn_dir = Path(td) / "connections"
            self.assertEqual(
                sorted(p.name for p in conn_dir.iterdir()),
                ["astrbot.json", "qqofficial.json", "websocket.json"],
            )
            # 全新安装不再生成旧版单文件 connections.json
            self.assertFalse((Path(td) / "connections.json").exists())

    def test_qqofficial_card_has_no_ws_fields_and_silent_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cm = ConnectionManager(Path(td), FakeLogger())
            qo = [a for a in cm.adapters if a["type"] == "qqofficial"][0]
            self.assertEqual([k for k in WS_ONLY_FIELDS if k in qo], [])
            self.assertTrue(qo["suppress_connection_log"])

    def test_crud_partitions_by_type(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cm = ConnectionManager(Path(td), FakeLogger())
            cm.create({"type": "astrbot", "name": "AB", "target": "ws://127.0.0.1:6200"})
            conn_dir = Path(td) / "connections"
            astrbot = json.loads((conn_dir / "astrbot.json").read_text(encoding="utf-8"))["adapters"]
            websocket = json.loads((conn_dir / "websocket.json").read_text(encoding="utf-8"))["adapters"]
            self.assertEqual([a["name"] for a in astrbot], ["AB"])
            # astrbot 卡片绝不混入 websocket.json（默认 WebSocket 卡片仍在），
            # qqofficial.json 也只保留官方默认卡片
            self.assertEqual(
                [a["type"] for a in websocket],
                ["websocket"],
            )
            # 重新加载：分文件合并还原全量卡片
            cm2 = ConnectionManager(Path(td), FakeLogger())
            self.assertEqual(len([a for a in cm2.adapters if a["type"] == "astrbot"]), 1)

    def test_legacy_single_file_fallback_then_switch(self) -> None:
        """旧版单文件：回退读取（含 ws 字段清理）→ 写盘自动切新结构并改名旧文件。"""
        with tempfile.TemporaryDirectory() as td:
            old = Path(td) / "connections.json"
            write_json(old, {
                "version": 1,
                "adapters": [
                    {"id": "ws1", "type": "websocket", "name": "WS", "enabled": True,
                     "target": "ws://1.2.3.4:3001", "sync": {}},
                    {"id": "qo1", "type": "qqofficial", "name": "官", "enabled": True,
                     "app_id": "102000001", "app_secret": "s", "ws_type": 0,
                     "listen_host": "0.0.0.0", "listen_port": 3002, "access_token": "",
                     "suppress_connection_log": False, "sync": {}},
                ],
            })
            cm = ConnectionManager(Path(td), FakeLogger())
            # 回退读取成功：两张卡片均加载，官方卡片剔除 ws 字段（值 false 保留用户显式选择）
            self.assertEqual(len(cm.adapters), 2)
            qo = [a for a in cm.adapters if a["type"] == "qqofficial"][0]
            self.assertEqual([k for k in WS_ONLY_FIELDS if k in qo], [])
            self.assertFalse(qo["suppress_connection_log"])
            # 写盘后：新结构生成、旧文件改名 .migrated
            self.assertTrue((Path(td) / "connections.json.migrated").exists())
            self.assertFalse(old.exists())
            websocket = json.loads(
                (Path(td) / "connections" / "websocket.json").read_text(encoding="utf-8")
            )["adapters"]
            self.assertEqual(websocket[0]["id"], "ws1")
            # 二次加载走新结构（不再触发旧文件回退提示分支）
            cm2 = ConnectionManager(Path(td), FakeLogger())
            self.assertEqual(len(cm2.adapters), 2)

    def test_corrupt_partition_file_backup(self) -> None:
        """单个分文件损坏：仅备份该文件并跳过，其余分文件正常加载。"""
        with tempfile.TemporaryDirectory() as td:
            ConnectionManager(Path(td), FakeLogger())
            bad = Path(td) / "connections" / "qqofficial.json"
            bad.write_text("{ 不是合法 JSON", encoding="utf-8")
            cm = ConnectionManager(Path(td), FakeLogger())
            # websocket 卡片仍加载成功，损坏文件备份为 .bak
            self.assertTrue(any(a["type"] == "websocket" for a in cm.adapters))
            self.assertTrue((Path(td) / "connections" / "qqofficial.json.bak").exists())


class SenderSuppressTests(unittest.TestCase):
    """后台静默日志：凭据降级 / 重试 / 补发提示纳入 suppress 管辖，默认开启。"""

    def test_adapter_default_suppress_on(self) -> None:
        from test_qqofficial import make_adapter

        ad = make_adapter()
        self.assertTrue(ad.suppress_connection_log)
        ad2 = make_adapter(suppress_connection_log=False)
        self.assertFalse(ad2.suppress_connection_log)

    def test_hub_reads_missing_key_as_true(self) -> None:
        """存量卡片缺 suppress_connection_log 键时按开启（新默认）处理。"""
        from endstone_lumenbridge.onebot.hub import AdapterHub
        from endstone_lumenbridge.event_bus import EventBus
        from endstone_lumenbridge.connections import ConnectionManager

        with tempfile.TemporaryDirectory() as td:
            conn_dir = Path(td) / "connections"
            conn_dir.mkdir(parents=True)
            write_json(conn_dir / "qqofficial.json", {
                "version": 1,
                "adapters": [{
                    "id": "qo_x", "type": "qqofficial", "name": "X", "enabled": True,
                    "app_id": "102000009", "app_secret": "sec", "sync": {},
                }],
            })
            cm = ConnectionManager(Path(td), FakeLogger())
            hub = AdapterHub(FakeLogger(), EventBus(), cm)
            hub.sync_from_manager()
            qo = [a for a in hub.all() if getattr(a, "adapter_type", "") == "qqofficial"]
            self.assertTrue(qo, "官方适配器应已创建")
            # 适配器实例的 suppress 开关按新默认开启
            self.assertTrue(qo[0].suppress_connection_log)


class MigrationScriptTests(unittest.TestCase):
    """独立迁移脚本 migrate_storage.py：目录布局迁移 + 字段清理 + 数据搬移。"""

    def _run(self, td: str, *args: str) -> str:
        # 模拟真实用法：把脚本复制到插件数据目录（= 临时目录）后运行，
        # 脚本以自身所在目录定位 BASE
        script_copy = Path(td) / "migrate_storage.py"
        shutil.copy2(SCRIPT, script_copy)
        result = subprocess.run(
            [sys.executable, str(script_copy), *args],
            cwd=td, capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_full_migration(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            write_json(base / "connections.json", {
                "version": 1,
                "adapters": [
                    {"id": "ws1", "type": "websocket", "name": "WS", "enabled": True,
                     "target": "ws://1.2.3.4:3001", "sync": {}},
                    {"id": "qo1", "type": "qqofficial", "name": "官", "enabled": True,
                     "app_id": "102000001", "app_secret": "s", "ws_type": 0, "target": "",
                     "listen_host": "0.0.0.0", "listen_port": 3002, "access_token": "",
                     "suppress_connection_log": False, "sync": {}},
                    {"id": "ab1", "type": "astrbot", "name": "AB", "enabled": True,
                     "target": "ws://127.0.0.1:6200", "sync": {}},
                ],
            })
            write_json(base / "rules.json", [{"id": "r1"}])
            write_json(base / "whitelist.json", [{"qid": "10001", "xbox": "Alpha"}])
            write_json(base / "command_palette.json", {})

            out = self._run(td)
            self.assertIn("[完成]", out)

            # connections：分文件 + 官方卡片清理 + suppress 升级
            conn = base / "connections"
            qo = json.loads((conn / "qqofficial.json").read_text(encoding="utf-8"))["adapters"][0]
            self.assertEqual([k for k in WS_ONLY_FIELDS if k in qo], [])
            self.assertTrue(qo["suppress_connection_log"])
            self.assertEqual(len(json.loads(
                (conn / "websocket.json").read_text(encoding="utf-8"))["adapters"]), 1)
            self.assertEqual(len(json.loads(
                (conn / "astrbot.json").read_text(encoding="utf-8"))["adapters"]), 1)

            # 数据文件移入 data/，旧文件移除，备份保留
            self.assertTrue((base / "data" / "rules.json").is_file())
            self.assertEqual(
                json.loads((base / "data" / "whitelist.json").read_text(encoding="utf-8")),
                [{"qid": "10001", "xbox": "Alpha"}],
            )
            self.assertFalse((base / "rules.json").exists())
            self.assertTrue((base / "legacy_backup" / "connections.json").is_file())

            # 迁移后插件可直接从新结构加载
            cm = ConnectionManager(base, FakeLogger())
            self.assertEqual(len(cm.adapters), 3)

            # 幂等：再次运行无副作用
            out2 = self._run(td)
            self.assertIn("[跳过]", out2)
            cm2 = ConnectionManager(base, FakeLogger())
            self.assertEqual(len(cm2.adapters), 3)

    def test_whitelist_merge_keeps_both_sides(self) -> None:
        """data/ 下已有新绑定：旧白名单合并不丢任何一侧。"""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            (base / "data").mkdir()
            write_json(base / "whitelist.json", [{"qid": "10001", "xbox": "Alpha"}])
            write_json(base / "data" / "whitelist.json", [{"qid": "10002", "xbox": "Beta"}])
            self._run(td)
            merged = json.loads((base / "data" / "whitelist.json").read_text(encoding="utf-8"))
            self.assertEqual(
                sorted((b["qid"], b["xbox"]) for b in merged),
                [("10001", "Alpha"), ("10002", "Beta")],
            )

    def test_dry_run_changes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            write_json(base / "connections.json", {
                "version": 1,
                "adapters": [{"id": "qo1", "type": "qqofficial", "name": "官",
                              "enabled": True, "app_id": "10", "app_secret": "s", "sync": {}}],
            })
            out = self._run(td, "--dry")
            self.assertIn("[预览]", out)
            self.assertTrue((base / "connections.json").is_file())
            self.assertFalse((base / "connections").exists())


def main() -> int:
    suite = unittest.TestLoader().loadTestsFromModule(
        sys.modules[__name__]
    )
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
