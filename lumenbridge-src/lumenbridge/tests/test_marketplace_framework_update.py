from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
import urllib.error
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from endstone_lumenbridge.marketplace import MarketplaceClient, MarketplaceError


class _Headers(dict):
    def get_all(self, name: str):
        return self.get(name, [])


class _FakeResponse:
    def __init__(self, *, body: bytes = b"", headers: dict | None = None, status: int = 200):
        self._body = body
        self.headers = _Headers(headers or {})
        self.status = status

    def read(self, n: int = -1) -> bytes:
        return self._body if n is None or n < 0 else self._body[:n]

    def geturl(self) -> str:
        return "http://market.test/"

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _http_error(code: int, payload: dict) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "http://market.test/api", code, "err", _Headers({}), io.BytesIO(json.dumps(payload).encode("utf-8"))
    )


class FrameworkUpdateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.plugins_dir = root / "plugins"
        self.data_dir = self.plugins_dir / "LumenBridge"
        self.plugins_dir.mkdir()
        self.data_dir.mkdir()
        config = {
            "marketplace": {"enable": True, "api_url": "http://market.test", "allow_http": True},
            "updates": {"api_url": "http://market.test/api/v1/updates/lumenbridge"},
        }
        logger = SimpleNamespace(info=lambda _m: None, warning=lambda _m: None, error=lambda _m: None)
        self.plugin = SimpleNamespace(data_folder=self.data_dir, VERSION="1.0.6", config_manager=SimpleNamespace(data=config), logger=logger, _tee_logger=logger)
        self.client = MarketplaceClient(self.plugin)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def wheel(self, version: str, name: str = "endstone-lumenbridge") -> Path:
        path = Path(self.tempdir.name) / f"release-{version}.whl"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr(f"{name.replace('-', '_')}-{version}.dist-info/METADATA", f"Name: {name}\nVersion: {version}\n")
            archive.writestr(f"{name.replace('-', '_')}/__init__.py", "")
        return path

    def test_stage_framework_update_replaces_only_after_metadata_validation_and_backs_up_old_wheel(self) -> None:
        old = self.plugins_dir / "endstone_lumenbridge-1.0.6-py3-none-any.whl"
        old.write_bytes(b"old-wheel")
        release = self.wheel("1.1.0")
        info = {"configured": True, "available": True, "current_version": "1.0.6", "latest": {"version": "1.1.0", "download_url": "http://market.test/download", "sha256": "a" * 64}}
        with patch.object(self.client, "framework_update_info", return_value=info), \
             patch.object(self.client, "_download_verified", return_value=str(release)) as dl:
            receipt = self.client.stage_framework_update()
            # 幂等复用：receipt/wheel 与发布记录一致（哈希也一致）时不重复下载
            self.client._file_sha256 = lambda _p: "a" * 64  # type: ignore[method-assign]
            second = self.client.stage_framework_update()
        dl.assert_called_once()
        self.assertEqual(second["to_version"], receipt["to_version"])
        target = self.plugins_dir / "endstone_lumenbridge-1.1.0-py3-none-any.whl"
        self.assertTrue(target.is_file())
        self.assertEqual(receipt["to_version"], "1.1.0")
        self.assertFalse(receipt["restart_required"])  # 已支持热重载，无需重启
        self.assertTrue(Path(receipt["backup_directory"]).is_dir())
        self.assertTrue((Path(receipt["backup_directory"]) / old.name).is_file())
        persisted = json.loads((self.data_dir / "data" / "framework_update.json").read_text(encoding="utf-8"))
        self.assertEqual(persisted["wheel"], target.name)

    def test_stage_rejects_wheel_with_mismatched_metadata_before_touching_existing_wheel(self) -> None:
        old = self.plugins_dir / "endstone_lumenbridge-1.0.6-py3-none-any.whl"
        old.write_bytes(b"old-wheel")
        invalid = self.wheel("1.1.0", name="other-package")
        info = {"configured": True, "available": True, "current_version": "1.0.6", "latest": {"version": "1.1.0", "download_url": "http://market.test/download", "sha256": "b" * 64}}
        with patch.object(self.client, "framework_update_info", return_value=info), \
             patch.object(self.client, "_download_verified", return_value=str(invalid)):
            with self.assertRaises(MarketplaceError):
                self.client.stage_framework_update()
        self.assertEqual(old.read_bytes(), b"old-wheel")
        self.assertFalse((self.plugins_dir / "endstone_lumenbridge-1.1.0-py3-none-any.whl").exists())

    def test_apply_framework_update_schedules_hot_swap(self) -> None:
        release = self.wheel("1.1.0")
        info = {"configured": True, "available": True, "current_version": "1.0.6", "latest": {"version": "1.1.0", "download_url": "http://market.test/download", "sha256": "c" * 64}}
        calls: list[str] = []
        new_plugin = SimpleNamespace(logger=SimpleNamespace(info=lambda _m: None), version="1.1.0")
        manager = SimpleNamespace(
            get_plugin=lambda name: new_plugin,
            is_plugin_enabled=lambda p: True,
        )
        server = SimpleNamespace(plugin_manager=manager, reload=lambda: calls.append("reload"))
        scheduled: list = []

        def run_on_main(fn, delay: int = 1) -> None:
            scheduled.append(delay)
            fn()

        self.plugin.run_on_main = run_on_main
        self.plugin.server = server
        with patch.object(self.client, "framework_update_info", return_value=info), \
             patch.object(self.client, "_download_verified", return_value=str(release)), \
             patch.dict(sys.modules):
            result = self.client.apply_framework_update()
        self.assertTrue(result["scheduled"])
        # 热重载走官方 Server.reload（disable+load+enable 手工替换已被弃用）
        self.assertEqual(calls, ["reload"])
        self.assertEqual(scheduled, [20])
        self.assertFalse(result["restart_required"])

    def test_apply_hot_swap_restores_backup_when_new_wheel_fails(self) -> None:
        release = self.wheel("1.1.0")
        info = {"configured": True, "available": True, "current_version": "1.0.6", "latest": {"version": "1.1.0", "download_url": "http://market.test/download", "sha256": "d" * 64}}
        old_wheel = self.plugins_dir / "endstone_lumenbridge-1.0.6-py3-none-any.whl"
        old_wheel.write_bytes(b"old-wheel")
        reload_calls: list[int] = []
        recovered_plugin = SimpleNamespace(version="1.0.6")
        current: dict = {"plugin": None}

        def do_reload() -> None:
            reload_calls.append(1)
            # 第一次 reload：新 wheel 加载失败（插件不存在）
            # 第二次 reload：回滚旧 wheel 后恢复
            current["plugin"] = None if len(reload_calls) == 1 else recovered_plugin

        manager = SimpleNamespace(
            get_plugin=lambda name: current["plugin"],
            is_plugin_enabled=lambda p: True,
        )
        server = SimpleNamespace(plugin_manager=manager, reload=do_reload)
        self.plugin.run_on_main = lambda fn, delay=1: fn()
        self.plugin.server = server
        with patch.object(self.client, "framework_update_info", return_value=info), \
             patch.object(self.client, "_download_verified", return_value=str(release)), \
             patch.dict(sys.modules):
            result = self.client.apply_framework_update()
        self.assertTrue(result["scheduled"])
        # 失败回滚：备份目录中的旧 wheel 复制回 plugins/，并再次热重载恢复
        restored = self.plugins_dir / "endstone_lumenbridge-1.0.6-py3-none-any.whl"
        self.assertTrue(restored.is_file())
        self.assertEqual(restored.read_bytes(), b"old-wheel")
        self.assertEqual(len(reload_calls), 2)


class AnonymousWriteTests(unittest.TestCase):
    """点赞/举报完全匿名：会话 Cookie + CSRF 头，不依赖任何密钥。"""

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.data_dir = root / "plugins" / "LumenBridge"
        self.data_dir.mkdir(parents=True)
        config = {"marketplace": {"enable": True, "api_url": "http://market.test", "allow_http": True}}
        logger = SimpleNamespace(info=lambda _m: None, warning=lambda _m: None, error=lambda _m: None)
        self.plugin = SimpleNamespace(data_folder=self.data_dir, VERSION="1.0.6", config_manager=SimpleNamespace(data=config), logger=logger, _tee_logger=logger)
        self.client = MarketplaceClient(self.plugin)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def page_response(self, session_id: str) -> _FakeResponse:
        html = f'<body class="community" data-csrf="{"e" * 32}">'
        return _FakeResponse(
            body=html.encode("utf-8"),
            headers={"Set-Cookie": [f"LBMARKETSESSID={session_id}; path=/; HttpOnly"]},
        )

    def ok_response(self, payload: dict) -> _FakeResponse:
        return _FakeResponse(body=json.dumps({"ok": True, "data": payload}).encode("utf-8"))

    def test_report_plugin_uses_anonymous_session_without_any_key(self) -> None:
        posts: list = []

        def fake_open(request):
            url = request.full_url
            if not "/market/plugins/" in url:
                return self.page_response("sess-aaa")
            posts.append(request)
            return self.ok_response({"id": "rep1", "status": "open"})

        with patch.object(self.client, "_open", side_effect=fake_open):
            result = self.client.report_plugin("demo-plugin", "恶意插件", "")
        self.assertEqual(result["id"], "rep1")
        self.assertEqual(len(posts), 1)
        headers = posts[0].headers
        self.assertEqual(headers["X-csrf-token"], "e" * 32)
        self.assertIn("LBMARKETSESSID=sess-aaa", headers["Cookie"])
        self.assertIn("LBMARKETVISITOR=", headers["Cookie"])
        self.assertNotIn("X-LumenBridge-Report-Key", headers)

    def test_like_plugin_refreshes_session_once_on_csrf_failure(self) -> None:
        posts: list = []
        pages: list[str] = []

        def fake_open(request):
            url = request.full_url
            if "/market/plugins/" not in url:
                session = f"sess-{len(pages) + 1}"
                pages.append(session)
                return self.page_response(session)
            posts.append(request)
            if len(posts) == 1:
                raise _http_error(403, {"ok": False, "error": {"code": "csrf_failed", "message": "请求验证失败"}})
            return self.ok_response({"liked": True, "like_count": 3})

        with patch.object(self.client, "_open", side_effect=fake_open):
            result = self.client.like_plugin("demo-plugin", True)
        self.assertTrue(result["liked"])
        # 会话刷新重建：第二次提交携带新会话 Cookie
        self.assertEqual(len(posts), 2)
        self.assertIn("LBMARKETSESSID=sess-2", posts[1].headers["Cookie"])

    def test_non_csrf_error_not_retried_and_code_propagated(self) -> None:
        attempts: list[int] = []

        def fake_open(request):
            if "/market/plugins/" not in request.full_url:
                return self.page_response("sess-x")
            attempts.append(1)
            raise _http_error(429, {"ok": False, "error": {"code": "rate_limited", "message": "请求过于频繁"}})

        with patch.object(self.client, "_open", side_effect=fake_open):
            with self.assertRaises(MarketplaceError) as ctx:
                self.client.like_plugin("demo-plugin", True)
        self.assertEqual(len(attempts), 1)
        self.assertEqual(ctx.exception.code, "rate_limited")

    def test_config_migration_strips_report_api_key(self) -> None:
        from endstone_lumenbridge.config import _strip_deprecated_pip_fields

        raw = {"marketplace": {"enable": True, "report_api_key": "secret"}, "pip": {"allow_all": True}}
        sanitized, changed = _strip_deprecated_pip_fields(raw)
        self.assertTrue(changed)
        self.assertNotIn("report_api_key", sanitized["marketplace"])
        self.assertNotIn("allow_all", sanitized["pip"])
        # 原对象不被修改
        self.assertEqual(raw["marketplace"]["report_api_key"], "secret")


if __name__ == "__main__":
    unittest.main()
