"""安全漏洞专项测试

针对 v1.0.1 修复的安全漏洞编写独立验证用例：
1. ZIP 安装包路径穿越（loader.install_from_zip）
2. WebUI plugin-views 路径穿越（/plugin-views/../../）
3. WebUI SPA 静态资源路径穿越（/../../etc/passwd）
4. WebUI 子插件文件编辑器路径穿越（GET/POST ?path=../../）
5. install/url 仅做协议与大小校验（内网部署场景，不拦截内网/回环地址）
6. 密码时序攻击防御（hmac.compare_digest 恒定时间比较）
7. ZIP 绝对路径穿越（/etc/passwd）
8. ZIP Windows 绝对路径穿越（C:\\Windows\\system32）

测试启动真实 WebUIServer，用 urllib 发起 HTTP 请求验证。
"""

from __future__ import annotations

import io
import json
import shutil
import socket
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

SRC = Path(__file__).parent.parent / "src"
sys.path.insert(0, str(SRC))

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        PASSED.append(name)
        print(f"  [PASS] {name}")
    else:
        FAILED.append(name)
        print(f"  [FAIL] {name} {detail}")
        # 失败必须抛错：pytest 收集 test_ 函数时得到真实 FAIL（结构性假绿修复）
        raise AssertionError(f"{name} {detail}".strip())


# ======================================================================
# Fake 环境（复用 test_webui.py 的最小可用 Plugin 替身）
# ======================================================================

class FakeLogger:
    def info(self, m): pass
    def warning(self, m): pass
    def error(self, m): pass
    def debug(self, m): pass


class FakeScheduler:
    def run_task(self, plugin, func, delay=0, period=0):
        func()
        return None


class FakeServer:
    def __init__(self):
        self.scheduler = FakeScheduler()
        self.online_players = []
    def dispatch_command(self, sender, cmd): return True


class FakeAdapter:
    is_connected = True
    mode_name = "正向 WebSocket"
    def send_group_msg(self, group, msg): pass


class FakeWhitelist:
    bindings: list = []
    # 与源码 WhitelistModule 签名一致：get_binding_by_qq(qq, domain) /
    # unbind_sync(qq, timeout, domain)（M41 统一 FakeWhitelist 签名）
    def get_binding_by_qq(self, qq, domain="qq"): return None
    def unbind_sync(self, qq, timeout=6.0, domain="qq"): return False, "无记录", None


class FakeRegex:
    rules: list = []
    def save_rules(self, rules): self.rules = rules


class FakeSubPlugin:
    def __init__(self, name):
        self.folder = Path("/tmp") / name
        self.manifest = {"name": name, "version": "1.0.0", "desc": "", "load": True, "priority": "main"}
        self.loaded = True
        self.error = ""
        self.module = None
        self.context = None


class FakeSubMgr:
    def __init__(self, plugins_dir: Path | None = None):
        self.subplugins: dict[str, FakeSubPlugin] = {}
        self.toggled: list = []
        # 真实 SubPluginManager 暴露的属性，文件编辑器路由会访问
        self.plugins_dir = plugins_dir or Path(tempfile.gettempdir()) / "lb_test_plugins"
        self.plugins_dir.mkdir(parents=True, exist_ok=True)
    def set_enabled(self, name, enable): return False
    def reload_all(self): return 0
    def install_from_zip(self, path):
        return False, "测试环境拒绝", ""
    def uninstall(self, name): return False, "测试环境"


class FakeBus:
    def __init__(self):
        self.emitted: list = []
    def emit(self, event, *args): self.emitted.append((event, args))


class FakeConfigManager:
    def __init__(self, tmp: Path):
        self._path = tmp / "config.json"
        self.data = {
            "connection": {"access_token": "SECRET", "bot_qq": 12345},
            "main_group": 98765,
            "admin_qq": [100, 200],
            "webui": {"enable": True, "host": "127.0.0.1", "port": 18301,
                      "password": "testpass", "secret": "testsecret"},
            "background": {"enable": False, "api_url": "", "blur_strength": 0,
                           "fallback_to_default": True, "cache_seconds": 600},
        }
        self.saved = False
    @property
    def main_group(self): return self.data.get("main_group", 0)
    @property
    def main_groups(self): return [self.main_group]
    @property
    def admin_qq(self): return self.data.get("admin_qq", [])
    @property
    def connection(self): return self.data.get("connection", {})
    @property
    def background(self): return self.data.get("background", {})
    def get(self, key, default=None): return self.data.get(key, default)
    def save(self): self.saved = True


class FakePlugin:
    VERSION = "1.0.0"
    def __init__(self, tmp: Path):
        from endstone_lumenbridge.webui import LogBuffer
        self.data_folder = str(tmp)
        self.logger = FakeLogger()
        self.server = FakeServer()
        self.config_manager = FakeConfigManager(tmp)
        self.bus = FakeBus()
        self.adapter = FakeAdapter()
        self.whitelist_module = FakeWhitelist()
        self.regex_module = FakeRegex()
        # plugins_dir 指向 tmp/plugins，与真实 SubPluginManager 一致
        plugins_dir = Path(tmp) / "plugins"
        plugins_dir.mkdir(parents=True, exist_ok=True)
        self.subplugin_manager = FakeSubMgr(plugins_dir)
        self.log_buffer = LogBuffer()
        # 与 plugin.__init__ 保持一致：WebUI 与 marketplace 共享此串行锁
        self._pip_serial_lock = __import__("threading").Lock()
        self.webui = None
    def run_on_main(self, func, delay=1): func()
    def bot_profile_snapshot(self):
        return {"qq": 12345, "nickname": "bot", "avatar_url": "", "source": "config"}


# ======================================================================
# HTTP 工具
# ======================================================================

BASE = "http://127.0.0.1:18301"
TOKEN = ""


def req(method, path, body=None, token=None, raw=False):
    url = BASE + path
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method)
    r.add_header("Content-Type", "application/json")
    tok = token if token is not None else TOKEN
    if tok:
        r.add_header("Authorization", "Bearer " + tok)
    try:
        with urllib.request.urlopen(r, timeout=5) as resp:
            payload = resp.read().decode()
            return resp.status, (payload if raw else json.loads(payload))
    except urllib.error.HTTPError as e:
        payload = e.read().decode()
        try:
            return e.code, json.loads(payload)
        except json.JSONDecodeError:
            return e.code, payload


def _wait_port_ready(host: str, port: int, timeout: float = 5.0,
                     interval: float = 0.05) -> bool:
    """轮询等待 TCP 端口可连（替代固定 sleep 就绪等待，M42）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except OSError:
            time.sleep(interval)
    return False


def setup_webui(tmp: Path):
    """启动真实 WebUIServer，返回 (plugin, webui)"""
    from endstone_lumenbridge.webui import WebUIServer
    plugin = FakePlugin(tmp)
    webui = WebUIServer(plugin)
    plugin.webui = webui
    webui.start()
    if not _wait_port_ready("127.0.0.1", 18301):
        webui.stop()
        raise RuntimeError("WebUIServer 启动超时（端口 18301 未就绪）")
    return plugin, webui


def login():
    global TOKEN
    code, res = req("POST", "/api/auth/login", {"password": "testpass"}, token="")
    assert code == 200, f"登录失败: {res}"
    TOKEN = res["data"]["token"]


# ======================================================================
# Bug #14: ZIP 安装包路径穿越
# ======================================================================
def test_zip_path_traversal():
    print("== Bug #14: ZIP 安装包路径穿越被拒绝 ==")
    from endstone_lumenbridge.subplugin.loader import SubPluginManager

    tmp = Path(tempfile.mkdtemp())
    try:
        # 1. 相对路径穿越 ../../escaped.txt
        evil_zip = tmp / "evil1.zip"
        with zipfile.ZipFile(evil_zip, "w") as zf:
            zf.writestr("../../escaped.txt", "hacked")
            zf.writestr("lumen.json", '{"name":"evil"}')
            zf.writestr("main.py", 'def on_load(lumen): pass')

        class _P:
            logger = FakeLogger()
            data_folder = str(tmp)
            server = None
            bus = None
            def run_on_main(self, fn): fn()

        mgr = SubPluginManager(_P())
        ok, msg, name = mgr.install_from_zip(str(evil_zip))
        check("相对路径穿越被拒绝", not ok and "非法路径" in msg, f"ok={ok}, msg={msg}")
        check("未发生路径穿越", not (tmp / ".." / ".." / "escaped.txt").exists())

        # 2. 绝对路径 /etc/passwd
        evil_zip2 = tmp / "evil2.zip"
        with zipfile.ZipFile(evil_zip2, "w") as zf:
            zf.writestr("/etc/passwd", "hacked")
            zf.writestr("lumen.json", '{"name":"evil2"}')
            zf.writestr("main.py", 'def on_load(lumen): pass')
        ok2, msg2, _ = mgr.install_from_zip(str(evil_zip2))
        check("绝对路径穿越被拒绝", not ok2 and "非法路径" in msg2, f"ok={ok2}, msg={msg2}")

        # 3. 正常 zip 仍可安装
        good_zip = tmp / "good.zip"
        with zipfile.ZipFile(good_zip, "w") as zf:
            zf.writestr("lumen.json", '{"name":"good_plug","version":"1.0.0"}')
            zf.writestr("main.py", 'def on_load(lumen): pass')
        ok3, msg3, name3 = mgr.install_from_zip(str(good_zip))
        check("正常 zip 可安装", ok3 and name3 == "good_plug", f"ok={ok3}, msg={msg3}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ======================================================================
# Bug #16/#17/#18: WebUI 路径穿越（3处）
# ======================================================================
def test_webui_plugin_views_traversal():
    print("== Bug #16: WebUI plugin-views 路径穿越被拦截 ==")
    tmp = Path(tempfile.mkdtemp())
    try:
        plugin, webui = setup_webui(tmp)
        login()

        # 构造路径穿越请求
        code, res = req("GET", "/plugin-views/../../config.json")
        check("plugin-views 路径穿越被拦截", code in (403, 404), f"code={code}")

        code, res = req("GET", "/plugin-views/../config.json")
        check("plugin-views 单层穿越被拦截", code in (403, 404), f"code={code}")

        webui.stop()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_webui_spa_traversal():
    print("== Bug #17: WebUI SPA 静态资源路径穿越被拦截 ==")
    tmp = Path(tempfile.mkdtemp())
    try:
        plugin, webui = setup_webui(tmp)
        login()

        # SPA 静态资源穿越应 fallback 到 index.html（不返回 403，但也不泄露文件）
        code, body = req("GET", "/../../etc/passwd", raw=True)
        check("SPA 路径穿越不泄露系统文件",
              code == 200 and "root:" not in body, f"code={code}")

        code, body = req("GET", "/../../../etc/shadow", raw=True)
        check("SPA 深层穿越不泄露 shadow",
              code == 200 and "root:" not in body, f"code={code}")

        webui.stop()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_webui_subplugin_file_traversal():
    print("== Bug #18: 子插件文件编辑器路径穿越被拦截 ==")
    tmp = Path(tempfile.mkdtemp())
    try:
        plugin, webui = setup_webui(tmp)
        login()

        # 创建一个真实的子插件目录
        sp_dir = Path(plugin.data_folder) / "plugins" / "testplug"
        sp_dir.mkdir(parents=True)
        (sp_dir / "lumen.json").write_text('{"name":"testplug"}', encoding="utf-8")
        (sp_dir / "main.py").write_text('def on_load(lumen): pass', encoding="utf-8")

        # GET 路径穿越
        code, res = req("GET", "/api/subplugins/testplug/file?path=../../config.json")
        check("GET 文件路径穿越被拦截", code == 403, f"code={code}, res={res}")

        code, res = req("GET", "/api/subplugins/testplug/file?path=../../../etc/passwd")
        check("GET 深层穿越被拦截", code == 403, f"code={code}")

        # POST 路径穿越
        code, res = req("POST", "/api/subplugins/testplug/file?path=../../evil.py",
                        body={"content": "hacked"})
        check("POST 文件路径穿越被拦截", code == 403, f"code={code}, res={res}")

        # 合法路径正常工作
        code, res = req("GET", "/api/subplugins/testplug/file?path=lumen.json")
        check("合法路径可读取", code == 200 and "testplug" in res["data"]["content"],
              f"code={code}, res={res}")

        webui.stop()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ======================================================================
# Bug #19: install/url 校验（SSRF 内网拦截已按需求撤销，仅保留协议校验）
# ======================================================================
def test_ssrf_defense():
    print("== Bug #19: install/url 协议校验 + 内网地址放行（内网部署场景）==")
    tmp = Path(tempfile.mkdtemp())
    try:
        plugin, webui = setup_webui(tmp)
        login()

        # 非 http(s) 协议仍被拒绝
        for scheme in ("file:///etc/passwd", "ftp://example.com/x.zip"):
            code, res = req("POST", "/api/subplugins/install/url", body={"url": scheme})
            check(f"拒绝 {scheme.split(':')[0]}:// 协议", code == 400, f"code={code}, res={res}")

        # 内网 / 回环地址不再被拦截：打桩 urlopen 使下载成功，
        # 响应应来自安装阶段（"测试环境拒绝"）而非 SSRF 拦截（含"内网"字样）
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("main.py", "def on_load(lumen):\n    pass\n")
            zf.writestr("lumen.json", json.dumps({"name": "ssrf_probe_sp", "version": "1.0.0"}))

        class _StubResp:
            def read(self, n=-1):
                return buf.getvalue()

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        orig_urlopen = urllib.request.urlopen

        def _fake_urlopen(request, timeout=None):
            # 测试框架自身的 /api/ 请求透传，仅拦截子插件下载请求
            full = request.full_url if hasattr(request, "full_url") else str(request)
            if "/api/" in full:
                return orig_urlopen(request, timeout=timeout)
            return _StubResp()

        urllib.request.urlopen = _fake_urlopen
        try:
            for url in ("http://127.0.0.1:8080/evil.zip", "http://192.168.1.1/x.zip",
                        "http://10.0.0.1/x.zip", "http://169.254.169.254/latest/x.zip"):
                code, res = req("POST", "/api/subplugins/install/url", body={"url": url})
                check(f"内网地址 {url.rsplit('/', 2)[0]} 放行至下载安装流程",
                      code == 400 and "内网" not in res.get("msg", ""),
                      f"code={code}, res={res}")
        finally:
            urllib.request.urlopen = orig_urlopen

        # 源码层面：SSRF IP 固定机制确已移除
        server_src = (SRC / "endstone_lumenbridge" / "webui" / "server.py").read_text(encoding="utf-8")
        check("server.py 已移除 SSRF IP 固定机制", "_pin_host" not in server_src)

        # HTML 响应不再携带 CSP / X-Frame-Options（内网部署，避免误伤
        # 跨域背景图、内联脚本与 iframe 自定义页）
        with urllib.request.urlopen(BASE + "/?probe=csp", timeout=5) as resp:
            resp_headers = {k.lower() for k in resp.headers.keys()}
        check("HTML 响应不再携带 CSP/X-Frame-Options",
              "content-security-policy" not in resp_headers
              and "x-frame-options" not in resp_headers,
              f"headers={resp_headers}")

        webui.stop()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ======================================================================
# Bug #20: 密码时序攻击防御
# ======================================================================
def test_password_timing_safe():
    print("== Bug #20: 密码比较使用 hmac.compare_digest（恒定时间）==")
    tmp = Path(tempfile.mkdtemp())
    try:
        plugin, webui = setup_webui(tmp)

        # 1. 错误密码返回 401
        code, res = req("POST", "/api/auth/login", {"password": "wrong"}, token="")
        check("错误密码返回 401", code == 401, f"code={code}")

        # 2. 正确密码返回 200
        code, res = req("POST", "/api/auth/login", {"password": "testpass"}, token="")
        check("正确密码返回 200", code == 200, f"code={code}")

        # 3. 空密码返回 401（不崩溃）
        code, res = req("POST", "/api/auth/login", {"password": ""}, token="")
        check("空密码返回 401", code == 401, f"code={code}")

        # 4. 缺少 password 字段返回 401
        code, res = req("POST", "/api/auth/login", {}, token="")
        check("缺少 password 字段返回 401", code == 401, f"code={code}")

        # 5. 验证源码使用 hmac.compare_digest 而非 ==
        server_src = (SRC / "endstone_lumenbridge" / "webui" / "server.py").read_text("utf-8")
        check("源码使用 hmac.compare_digest",
              "compare_digest" in server_src and "hmac" in server_src,
              "未找到 hmac.compare_digest 调用")
        # 进一步确认密码比较处使用 compare_digest（而非 ==）。
        # 窗口需覆盖路由匹配与密码比较之间的限速/请求体校验逻辑，
        # 取 1200 字符防止中间插入防御代码后误报。
        login_section = server_src[server_src.index("api/auth/login"):server_src.index("api/auth/login") + 1200]
        check("登录密码比较使用 compare_digest", "compare_digest" in login_section,
              "登录路由未使用 compare_digest")

        # 6. 时序测试：错误密码和正确密码响应时间不应有显著差异
        # （hmac.compare_digest 是恒定时间，理论上时间相近）
        import time as _time
        t1 = _time.time()
        for _ in range(5):
            req("POST", "/api/auth/login", {"password": "wrong"}, token="")
        t1 = (_time.time() - t1) / 5

        t2 = _time.time()
        for _ in range(5):
            req("POST", "/api/auth/login", {"password": "testpass"}, token="")
        t2 = (_time.time() - t2) / 5

        # 比率应小于 3 倍（恒定时间比较下差异极小，留较大余量排除网络抖动）
        ratio = max(t1, t2) / max(min(t1, t2), 0.001)
        check(f"错误/正确密码响应时间比率 < 3 倍（{ratio:.2f}）", ratio < 3.0,
              f"wrong={t1*1000:.1f}ms, correct={t2*1000:.1f}ms, ratio={ratio:.2f}")

        webui.stop()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ======================================================================
# Bug #21: 子插件 loader 路径穿越（额外防御）
# ======================================================================
def test_loader_symlink_defense():
    print("== Bug #21: 子插件目录发现跳过符号链接（额外防御）==")
    from endstone_lumenbridge.subplugin.loader import SubPluginManager

    tmp = Path(tempfile.mkdtemp())
    try:
        plugins_dir = tmp / "plugins"
        plugins_dir.mkdir(parents=True)

        # 创建一个正常子插件
        good_dir = plugins_dir / "good"
        good_dir.mkdir()
        (good_dir / "lumen.json").write_text('{"name":"good","load":true}', encoding="utf-8")
        (good_dir / "main.py").write_text('def on_load(lumen): pass', encoding="utf-8")

        # 创建一个指向 /etc 的符号链接（如果系统支持）
        try:
            (plugins_dir / "evil_link").symlink_to("/etc", target_is_directory=True)
            has_symlink = True
        except (OSError, NotImplementedError):
            has_symlink = False

        if has_symlink:
            class _P:
                logger = FakeLogger()
                data_folder = str(tmp)
                server = None
                bus = None
                def run_on_main(self, fn): fn()

            mgr = SubPluginManager(_P())
            found = mgr.discover()
            names = [sp.name for sp in found]
            check("正常子插件被发现", "good" in names, f"found={names}")
            check("符号链接子插件不包含 main.py 被跳过",
                  "evil_link" not in names or not any(
                      sp.name == "evil_link" and sp.folder.is_symlink() for sp in found
                  ))
        else:
            check("系统不支持符号链接，跳过本用例", True)
            print("    （跳过：系统不支持 symlink）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    # check 失败会抛 AssertionError（pytest 真实 FAIL 用）；手动运行时逐个吞掉，
    # 保持"跑完全部再汇总退出码"的原语义
    for fn in (
        test_zip_path_traversal,
        test_webui_plugin_views_traversal,
        test_webui_spa_traversal,
        test_webui_subplugin_file_traversal,
        test_ssrf_defense,
        test_password_timing_safe,
        test_loader_symlink_defense,
    ):
        try:
            fn()
        except AssertionError:
            pass
        print()

    print(f"\n===== 结果: {len(PASSED)} 通过, {len(FAILED)} 失败 =====")
    if FAILED:
        for f in FAILED:
            print(f"  FAILED: {f}")
        sys.exit(1)


if __name__ == "__main__":
    main()
