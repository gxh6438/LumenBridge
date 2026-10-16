"""WebUI 子插件管理接口测试（真实 HTTP）：

1. GET /api/config/labels 配置中文标签
2. multipart ZIP 上传安装
3. 子插件文件列表 / 读取 / 保存（含非法 JSON 与目录穿越拦截）
4. 子插件卸载与未登录拒绝
5. ConfigFormBuilder 中文标签 label
6. 依赖补全全链路：deps-sources 探测 / Endstone 依赖落盘（上传与链接）/
   全服热重载任务 / install-requirements 结果如实透传
"""

from __future__ import annotations

import io
import json
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from unittest.mock import patch

SRC = Path(__file__).parent.parent / "src"
sys.path.insert(0, str(SRC))

# 本文件专属端口（每文件唯一端口，避免与其他 WebUI 测试串用/残留占用）
WEBUI_PORT = 18391
BASE = f"http://127.0.0.1:{WEBUI_PORT}"


def _wait_port_ready(timeout: float = 5.0, interval: float = 0.05) -> bool:
    """轮询等待 WebUI 端口可连。"""
    import socket

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", WEBUI_PORT), timeout=0.5):
                return True
        except OSError:
            time.sleep(interval)
    return False


def _req(method, path, body=None, token=None, raw_body=None, ctype=None):
    headers = {}
    if token:
        headers["Authorization"] = "Bearer " + token
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    if raw_body is not None:
        data = raw_body
        headers["Content-Type"] = ctype
    r = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(r, timeout=8) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


class _Logger:
    def info(self, *a): pass
    def warning(self, *a): pass
    def error(self, *a): pass
    def debug(self, *a): pass


class _Scheduler:
    def run_task(self, plugin, fn, delay=0, period=0):
        fn()

        class _T:
            task_id = 1

        return _T()


class _Server:
    def __init__(self):
        self.online_players = []
        self.scheduler = _Scheduler()
        self.command_sender = object()

    def dispatch_command(self, sender, cmd):
        return True

    def broadcast_message(self, msg):
        pass


class _Plugin:
    VERSION = "test"

    def __init__(self, tmp: Path):
        from endstone_lumenbridge.event_bus import EventBus
        from endstone_lumenbridge.subplugin.context import EnvPool

        self.logger = _Logger()
        self._tee_logger = None
        self.server = _Server()
        self.bus = EventBus()
        self.adapter = None
        self.config_manager = type("CM", (), {
            "data": {"webui": {"host": "127.0.0.1", "port": WEBUI_PORT,
                               "password": "testpwd", "secret": "s3"}},
        })()
        self.data_folder = str(tmp)
        self.log_buffer = None
        self.whitelist_module = None
        self.regex_module = None
        self.env_pool = EnvPool(self)
        self._pip_serial_lock = __import__("threading").Lock()

    def run_on_main(self, fn, delay=0):
        fn()


def _make_plugin_zip(name: str, version: str = "1.0.0") -> str:
    buf = io.BytesIO()
    manifest = {"name": name, "version": version, "desc": "测试插件", "load": True}
    code = 'def on_load(lumen):\n    lumen.logger.info("hi")\n'
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(f"{name}/lumen.json", json.dumps(manifest))
        zf.writestr(f"{name}/main.py", code)
    tmp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
    tmp.write(buf.getvalue())
    tmp.close()
    return tmp.name


def test_configform_label():
    """ConfigFormBuilder 中文标签：显式 label 优先，缺省回退 key。"""
    from endstone_lumenbridge.webui.configform import ConfigFormBuilder

    captured = {}
    b = ConfigFormBuilder("demo", lambda builder: captured.update(builder.to_schema()))
    b.switch("enable", True, desc="说明文字", label="启用功能")
    b.text("prefix", "!", label="命令前缀")
    b.number("limit", 5)
    b.register()
    items = captured["items"]
    assert items[0]["label"] == "启用功能" and items[0]["desc"] == "说明文字"
    assert items[1]["label"] == "命令前缀"
    assert items[2].get("label", "limit") in ("limit", "")


def _wait_market_task(token: str, task_id: str, timeout: float = 10.0) -> dict:
    """轮询市场任务直到完成，返回任务快照。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        st, d = _req("GET", f"/api/market/task/{task_id}", token=token)
        assert st == 200, d
        task = d["data"]
        if task.get("done"):
            return task
        time.sleep(0.05)
    raise AssertionError("market task timeout")


def test_webui_subplugin_api():
    """WebUI 子插件管理全链路（真实 HTTP）。"""
    from endstone_lumenbridge.subplugin.loader import SubPluginManager
    from endstone_lumenbridge.webui import LogBuffer
    from endstone_lumenbridge.webui import WebUIServer

    tmp = Path(tempfile.mkdtemp())
    srv = None
    try:
        plugin = _Plugin(tmp)
        plugin.log_buffer = LogBuffer()
        plugin.subplugin_manager = SubPluginManager(plugin)
        srv = WebUIServer(plugin)
        srv.start()
        assert _wait_port_ready(), "WebUIServer 启动超时"

        # 登录
        st, d = _req("POST", "/api/auth/login", {"password": "testpwd"})
        assert st == 200, d
        token = d["data"]["token"]

        # 中文标签接口
        st, d = _req("GET", "/api/config/labels", token=token)
        assert st == 200 and d["data"]["whitelist.enable"]["label"] == "启用白名单绑定"

        # multipart 上传安装（任务模式：受理后轮询任务结果）
        zip_path = _make_plugin_zip("web_sp", "1.0.0")
        zip_bytes = Path(zip_path).read_bytes()
        boundary = "----lumtestboundary"
        mp = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
              f"filename=\"web_sp.zip\"\r\nContent-Type: application/zip\r\n\r\n").encode() \
             + zip_bytes + f"\r\n--{boundary}--\r\n".encode()
        st, d = _req("POST", "/api/subplugins/install/upload", token=token,
                     raw_body=mp, ctype=f"multipart/form-data; boundary={boundary}")
        assert st == 200, d
        task = _wait_market_task(token, d["data"]["task_id"])
        assert task["success"], task
        assert task["result"]["name"] == "web_sp"

        # 文件列表
        st, d = _req("GET", "/api/subplugins/web_sp/files", token=token)
        assert st == 200 and any(f["path"] == "main.py" for f in d["data"])

        # 读取与保存文件
        st, d = _req("GET", "/api/subplugins/web_sp/file?path=lumen.json", token=token)
        assert st == 200 and "web_sp" in d["data"]["content"]
        st, d = _req("POST", "/api/subplugins/web_sp/file?path=lumen.json", token=token,
                     body={"content": json.dumps({"name": "web_sp", "version": "1.0.1"})})
        assert st == 200, d
        # 非法 JSON 被拒绝
        st, d = _req("POST", "/api/subplugins/web_sp/file?path=lumen.json", token=token,
                     body={"content": "{bad json"})
        assert st == 400
        # 目录穿越被拦截
        st, d = _req("GET", "/api/subplugins/web_sp/file?path=../../config.json", token=token)
        assert st == 403

        # 市场已装映射：_market 来源子插件 → 市场 ID 映射（供市场卡片徽章/更新按钮）
        with plugin.subplugin_manager._lock:
            sp = plugin.subplugin_manager.subplugins["web_sp"]
            sp.manifest["_market"] = {"source": "marketplace", "id": "web-sp-id"}
        st, d = _req("GET", "/api/market/installed", token=token)
        assert st == 200, d
        entry = d["data"]["web-sp-id"]
        assert entry["name"] == "web_sp" and entry["version"] == "1.0.0"
        assert entry["update_available"] is False  # 无检查缓存时无更新态

        # 卸载
        st, d = _req("DELETE", "/api/subplugins/web_sp", token=token)
        assert st == 200, d
        st, d = _req("GET", "/api/subplugins/web_sp/files", token=token)
        assert st == 404

        # 未登录被拒
        st, _ = _req("GET", "/api/config/labels")
        assert st == 401
    finally:
        if srv is not None:
            srv.stop()
        shutil.rmtree(tmp, ignore_errors=True)


def _multipart(filename: str, content: bytes, field: str = "file") -> tuple[bytes, str]:
    boundary = "----lumtestboundary"
    mp = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{field}\"; "
          f"filename=\"{filename}\"\r\nContent-Type: application/octet-stream\r\n\r\n").encode() \
          + content + f"\r\n--{boundary}--\r\n".encode()
    return mp, f"multipart/form-data; boundary={boundary}"


class _DepsMarket:
    """市场替身：helper_sp 在售；依赖补装如实返回部分缺失；全服重载成功。"""

    enabled = True

    def _find_market_plugin_by_manifest_name(self, name):
        return {"name": str(name)} if str(name).lower() == "helper_sp" else None

    def install_plugin_requirements(self, name, log=None, progress=None):
        return {"ok": False, "message": "Endstone 插件依赖无法自动安装: HomeAPI",
                "endstone_missing": ["HomeAPI"]}

    def safe_server_reload(self, *, log=None):
        log("reload called")
        return {"ok": True, "version": "1.0.6"}


def _wait_task(token, task_id, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        st, d = _req("GET", f"/api/market/task/{task_id}", token=token)
        if st == 200 and d["data"].get("done"):
            return d["data"]
        time.sleep(0.05)
    raise AssertionError(f"market task {task_id} not done in {timeout}s")


def test_deps_resolve_endpoints():
    """依赖补全四端点：方式探测 / Endstone 落盘（上传+链接）/ 全服热重载。"""
    from endstone_lumenbridge.subplugin.loader import SubPluginManager
    from endstone_lumenbridge.webui import LogBuffer
    from endstone_lumenbridge.webui import WebUIServer

    tmp = Path(tempfile.mkdtemp())
    srv = None
    try:
        # 目录结构：server_root/plugins/lumenbridge = data_folder
        # Endstone 依赖必须落盘到 server_root/plugins（BDS 根，非子插件目录）
        data_folder = tmp / "server_root" / "plugins" / "lumenbridge"
        data_folder.mkdir(parents=True)
        bds_plugins = data_folder.parent

        plugin = _Plugin(data_folder)
        plugin.log_buffer = LogBuffer()
        plugin.marketplace = _DepsMarket()
        plugin.server.plugin_manager = type("PM", (), {"plugins": []})()
        plugin.subplugin_manager = SubPluginManager(plugin)
        srv = WebUIServer(plugin)
        srv.start()
        assert _wait_port_ready(), "WebUIServer 启动超时"

        st, d = _req("POST", "/api/auth/login", {"password": "testpwd"})
        assert st == 200, d
        token = d["data"]["token"]

        # 装入声明两类依赖的子插件（disabled，只需清单可读）
        mgr = plugin.subplugin_manager
        sp_dir = mgr.plugins_dir / "deps_demo"
        sp_dir.mkdir(parents=True)
        (sp_dir / "lumen.json").write_text(json.dumps({
            "name": "deps_demo", "version": "1.0.0", "load": False,
            "requires": {"subplugins": ["helper_sp>=1.0.0"], "endstone": ["HomeAPI>=1.0"]},
        }), encoding="utf-8")
        (sp_dir / "main.py").write_text("def on_load(lumen):\n    pass\n", encoding="utf-8")
        (sp_dir / "requirements.txt").write_text(
            "[subplugins]\nHelper_SP = https://cdn.test/helper.zip\n"
            "[endstone]\nHomeAPI = https://cdn.test/homeapi.whl\n",
            encoding="utf-8",
        )
        mgr.load_all()

        # --- deps-sources：市场存在性 / 本地状态 / requirements 直链 ---
        st, d = _req("GET", "/api/subplugins/deps_demo/deps-sources", token=token)
        assert st == 200, d
        data = d["data"]
        assert data["market_enabled"] is True
        assert data["unverified"] is False
        sp_dep = next(x for x in data["subplugins"] if x["name"] == "helper_sp")
        assert sp_dep["in_market"] is True
        assert sp_dep["installed"] is False
        assert sp_dep["url"] == "https://cdn.test/helper.zip"
        es_dep = next(x for x in data["endstone"] if x["name"] == "HomeAPI")
        assert es_dep["url"] == "https://cdn.test/homeapi.whl"
        # 未知子插件 → 404
        st, _ = _req("GET", "/api/subplugins/no_such/deps-sources", token=token)
        assert st == 404

        # --- install-requirements：部分缺失时 result.ok=false 如实透传（假成功修复） ---
        st, d = _req("POST", "/api/subplugins/deps_demo/install-requirements", token=token, body={})
        assert st == 200, d
        task = _wait_task(token, d["data"]["task_id"])
        assert task["success"] is True  # 任务本身执行成功
        assert task["result"]["ok"] is False  # 但依赖未补齐，如实告知
        assert "HomeAPI" in task["result"]["endstone_missing"]

        # --- endstone-dep/upload：落盘服务器 plugins 目录 ---
        body, ctype = _multipart("HomeAPI-1.0.0.whl", b"fake-wheel")
        st, d = _req("POST", "/api/subplugins/deps_demo/endstone-dep/upload", token=token,
                     raw_body=body, ctype=ctype)
        assert st == 200, d
        assert (bds_plugins / "HomeAPI-1.0.0.whl").read_bytes() == b"fake-wheel"
        assert not (mgr.plugins_dir / "HomeAPI-1.0.0.whl").exists()  # 不落子插件目录
        # 路径穿越与非法扩展被拒
        body, ctype = _multipart("../evil.whl", b"x")
        st, _ = _req("POST", "/api/subplugins/deps_demo/endstone-dep/upload", token=token,
                     raw_body=body, ctype=ctype)
        assert st == 400
        body, ctype = _multipart("HomeAPI.tar.gz", b"x")
        st, _ = _req("POST", "/api/subplugins/deps_demo/endstone-dep/upload", token=token,
                     raw_body=body, ctype=ctype)
        assert st == 400

        # --- endstone-dep/url：链接下载落盘 + 参数校验 ---
        with patch("endstone_lumenbridge.webui.server._fetch_url_bytes", return_value=b"dl-wheel"):
            st, d = _req("POST", "/api/subplugins/deps_demo/endstone-dep/url", token=token,
                         body={"url": "https://cdn.test/HomeAPI-2.0.0.whl"})
            assert st == 200, d
            assert (bds_plugins / "HomeAPI-2.0.0.whl").read_bytes() == b"dl-wheel"
            # sha256 不匹配拒绝写入
            st, _ = _req("POST", "/api/subplugins/deps_demo/endstone-dep/url", token=token,
                         body={"url": "https://cdn.test/HomeAPI-2.0.0.whl", "sha256": "0" * 64})
            assert st == 400
        st, _ = _req("POST", "/api/subplugins/deps_demo/endstone-dep/url", token=token,
                     body={"url": "ftp://cdn.test/HomeAPI.whl"})
        assert st == 400
        st, _ = _req("POST", "/api/subplugins/deps_demo/endstone-dep/url", token=token,
                     body={"url": "https://cdn.test/no-extension"})
        assert st == 400

        # --- server/reload：全服热重载任务 ---
        st, d = _req("POST", "/api/server/reload", token=token, body={})
        assert st == 200, d
        task = _wait_task(token, d["data"]["task_id"])
        assert task["success"] is True
        assert task["result"] == {"ok": True, "version": "1.0.6"}
    finally:
        if srv is not None:
            srv.stop()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    test_configform_label()
    test_webui_subplugin_api()
    test_deps_resolve_endpoints()
    print("全部通过")
