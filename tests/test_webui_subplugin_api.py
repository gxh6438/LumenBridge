"""WebUI 子插件管理接口测试（真实 HTTP）：

1. GET /api/config/labels 配置中文标签
2. multipart ZIP 上传安装
3. 子插件文件列表 / 读取 / 保存（含非法 JSON 与目录穿越拦截）
4. 子插件卸载与未登录拒绝
5. ConfigFormBuilder 中文标签 label
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

        # multipart 上传安装
        zip_path = _make_plugin_zip("web_sp", "1.0.0")
        zip_bytes = Path(zip_path).read_bytes()
        boundary = "----lumtestboundary"
        mp = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
              f"filename=\"web_sp.zip\"\r\nContent-Type: application/zip\r\n\r\n").encode() \
             + zip_bytes + f"\r\n--{boundary}--\r\n".encode()
        st, d = _req("POST", "/api/subplugins/install/upload", token=token,
                     raw_body=mp, ctype=f"multipart/form-data; boundary={boundary}")
        assert st == 200, d
        assert d["data"]["name"] == "web_sp"

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


if __name__ == "__main__":
    test_configform_label()
    test_webui_subplugin_api()
    print("全部通过")
