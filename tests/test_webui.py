"""WebUI 端到端测试

启动真实 WebUIServer（随机端口），用 urllib 发起 HTTP 请求，覆盖：
登录/错误密码/未授权、总览、配置读写（含敏感键掩码）、规则保存热载、
白名单查询与解绑、子插件列表/开关/重载、日志缓存与 SSE、
子插件 lumen.web.createConfig / registerApi / registerPage。
"""

import json
import shutil
import socket
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

PASSED = []
FAILED = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        PASSED.append(name)
        print(f"  [PASS] {name}")
    else:
        FAILED.append(name)
        print(f"  [FAIL] {name} {detail}")
        # 失败必须抛错：pytest 收集 test_ 函数时得到真实 FAIL（结构性假绿修复）
        raise AssertionError(f"{name} {detail}".strip())


# ----------------------------------------------------------------------
# Fake 环境
# ----------------------------------------------------------------------

class FakeLogger:
    def info(self, msg): print(f"    [log] {msg}")
    def warning(self, msg): print(f"    [warn] {msg}")
    def error(self, msg): print(f"    [err] {msg}")
    def debug(self, msg): pass


class FakeScheduler:
    def __init__(self):
        self.tasks = []
    def run_task(self, plugin, func, delay=0, period=0):
        # 立即执行以简化测试
        func()
        return None


class FakePlayer:
    def __init__(self, name): self.name = name


class FakeServer:
    def __init__(self):
        self.scheduler = FakeScheduler()
        self.online_players = [FakePlayer("Steve"), FakePlayer("Alex")]
        self.executed = []
    def dispatch_command(self, sender, cmd):
        self.executed.append(cmd)
        return True


class FakeAdapter:
    is_connected = True
    mode_name = "正向 WebSocket"
    ws_type = 0
    def send_group_msg(self, group, msg): pass


class FakeWhitelist:
    def __init__(self):
        self.bindings = [{"qid": "10001", "xbox": "SteveXbox"}]
        self.removed_cmds = []
        self.fail_unbind = False
    def snapshot(self, domain=None):
        return self.bindings
    def get_binding_by_qq(self, qq, domain="qq"):
        entry = next((b for b in self.bindings if b["qid"] == str(qq)), None)
        return dict(entry) if entry else None
    def unbind_sync(self, qq, timeout=6.0, domain="qq"):
        entry = self.get_binding_by_qq(qq, domain)
        if not entry:
            return False, "记录不存在", None
        self.removed_cmds.append(("remove", entry["xbox"]))
        if self.fail_unbind:
            return False, "服务器拒绝了白名单命令", entry
        self.bindings.remove(next(b for b in self.bindings if b["qid"] == str(qq)))
        return True, f"已解绑并移除白名单：{entry['xbox']}", entry


class FakeRegex:
    def __init__(self):
        self.rules = [{"name": "r1"}]
        self.saved = None
    def save_rules(self, rules):
        self.saved = rules
        self.rules = rules


class FakeSubPlugin:
    def __init__(self, name):
        self.folder = Path("/tmp") / name
        self.manifest = {"name": name, "version": "1.0.0", "desc": "测试", "load": True, "priority": "main"}
        self.loaded = True
        self.error = ""
        self.missing_deps = []
        self.missing_modules = []
        self.missing_requirements = []


class FakeSubMgr:
    def __init__(self):
        self.subplugins = {"demo": FakeSubPlugin("demo")}
        self.toggled = []
        self._lock = __import__("threading").RLock()
    def set_enabled(self, name, enable):
        if name not in self.subplugins:
            return False
        self.toggled.append((name, enable))
        self.subplugins[name].manifest["load"] = enable
        return True
    def reload_all(self):
        return len(self.subplugins)


class FakeBus:
    def __init__(self):
        self.emitted = []
    def emit(self, event, *args):
        self.emitted.append((event, args))


class FakeConfigManager:
    def __init__(self, tmp: Path):
        self._path = tmp / "config.json"
        self.data = {
            "connection": {"access_token": "SECRET-TOKEN", "bot_qq": 12345},
            "main_group": 98765,
            "admin_qq": [100, 200],
            "webui": {"enable": True, "host": "127.0.0.1", "port": WEBUI_PORT,
                      "password": "testpass", "secret": "testsecret"},
            "background": {"enable": True, "api_url": "https://t.alcy.cc/fj",
                           "blur_strength": 0, "fallback_to_default": True,
                           "cache_seconds": 600},
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
    def save(self):
        self.saved = True
    def apply_patch(self, patch):
        # 测试桩：模拟 ConfigManager 的深合并保存（真实校验由 test_config_and_pip 覆盖）
        def merge(dst, src):
            for k, v in src.items():
                if isinstance(v, dict) and isinstance(dst.get(k), dict):
                    merge(dst[k], v)
                else:
                    dst[k] = v
        merge(self.data, patch)
        self.save()


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
        self.subplugin_manager = FakeSubMgr()
        self.log_buffer = LogBuffer()
        self.webui = None
        self._pip_serial_lock = __import__("threading").RLock()
    def run_on_main(self, func, delay=1):
        func()
    def bot_profile_snapshot(self):
        return {
            "qq": 12345,
            "nickname": "测试机器人",
            "avatar_url": "https://q1.qlogo.cn/g?b=qq&nk=12345&s=100",
            "source": "onebot",
        }


# ----------------------------------------------------------------------
# HTTP 工具
# ----------------------------------------------------------------------

# 本文件专属 WebUI 端口（M42：每文件唯一端口，避免与其他文件串用/残留占用）
WEBUI_PORT = 18310
BASE = f"http://127.0.0.1:{WEBUI_PORT}"
TOKEN = ""


def _wait_port_ready(timeout: float = 5.0, interval: float = 0.05) -> bool:
    """轮询等待 WebUI 端口可连（替代固定 sleep 就绪等待，M42）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", WEBUI_PORT), timeout=0.5):
                return True
        except OSError:
            time.sleep(interval)
    return False


def _wait_until(cond, timeout: float = 5.0, interval: float = 0.05) -> bool:
    """轮询等待条件成立（M42：替代固定 time.sleep 就绪等待）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(interval)
    return cond()


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


def test_webui_e2e():
    """WebUI 端到端（C7：原 main() 流程整段迁入，pytest 可收集/真实 FAIL）。"""
    tmp = Path(tempfile.mkdtemp())
    plugin = webui = None

    from endstone_lumenbridge.webui import WebUIServer

    global TOKEN

    try:
        plugin = FakePlugin(tmp)
        webui = WebUIServer(plugin)
        plugin.webui = webui
        webui.start()
        if not _wait_port_ready():
            raise RuntimeError(f"WebUIServer 启动超时（端口 {WEBUI_PORT} 未就绪）")

        print("== 1. 鉴权 ==")
        code, res = req("POST", "/api/auth/login", {"password": "wrong"}, token="")
        check("错误密码返回 401", code == 401)
        code, res = req("POST", "/api/auth/login", {"password": "testpass"}, token="")
        check("正确密码登录成功", code == 200 and res["data"]["token"])
        TOKEN = res["data"]["token"]
        code, res = req("GET", "/api/overview", token="")
        check("无 token 访问返回 401", code == 401)
        code, res = req("GET", "/api/overview", token="invalid.token")
        check("伪造 token 返回 401", code == 401)

        print("== 2. 总览 ==")
        code, res = req("GET", "/api/overview")
        d = res["data"]
        check("总览返回 200", code == 200)
        check("总览: 版本正确", d["version"] == "1.0.0")
        check("总览: 连接状态", d["connected"] is True and d["mode"] == 0 and d["mode_name"] == "正向 WebSocket")
        check("总览: 机器人昵称与头像", d["bot_profile"]["nickname"] == "测试机器人"
              and d["bot_profile"]["qq"] == 12345
              and d["bot_profile"]["avatar_url"].startswith("https://q1.qlogo.cn/"))
        check("总览: 在线玩家", d["online_players"] == ["Steve", "Alex"])
        check("总览: 统计数字", d["whitelist_count"] == 1 and d["rules_count"] == 1 and d["subplugin_count"] == 1)

        print("== 3. 配置读写 ==")
        code, res = req("GET", "/api/config")
        check("读取配置", code == 200 and res["data"]["main_group"] == 98765)
        check("敏感键已掩码", res["data"]["connection"]["access_token"] == "******")
        new_conf = res["data"]
        new_conf["main_group"] = 55555
        code, res = req("POST", "/api/config", new_conf)
        check("保存配置成功", code == 200)
        check("掩码值未覆盖原 token",
              plugin.config_manager.data["connection"]["access_token"] == "SECRET-TOKEN")
        check("配置已持久化", plugin.config_manager.saved and plugin.config_manager.data["main_group"] == 55555)
        check("触发 config.update.core 事件",
              any(e[0] == "config.update.core" for e in plugin.bus.emitted))

        print("== 4. 正则规则 ==")
        code, res = req("GET", "/api/rules")
        check("读取规则", code == 200 and res["data"] == [{"name": "r1"}])
        code, res = req("POST", "/api/rules", [{"name": "new_rule", "pattern": "^hi$"}])
        check("保存规则并热载", code == 200 and plugin.regex_module.saved[0]["name"] == "new_rule")
        code, res = req("POST", "/api/rules", {"not": "array"})
        check("非数组规则被拒绝", code == 400)

        print("== 5. 白名单 ==")
        code, res = req("GET", "/api/whitelist")
        check("白名单列表", code == 200 and res["data"][0]["qid"] == "10001")
        code, res = req("DELETE", "/api/whitelist/10001")
        check("解绑成功", code == 200)
        check("触发 allowlist remove", plugin.whitelist_module.removed_cmds == [("remove", "SteveXbox")])
        code, res = req("DELETE", "/api/whitelist/10001")
        check("重复解绑返回 404", code == 404)
        plugin.whitelist_module.bindings.append({"qid": "10002", "xbox": "Player With Space"})
        plugin.whitelist_module.fail_unbind = True
        code, res = req("DELETE", "/api/whitelist/10002")
        check("游戏侧解绑失败返回 409", code == 409 and "绑定记录已保留" in res["msg"])
        check("解绑失败不删除本地绑定",
              plugin.whitelist_module.get_binding_by_qq("10002") is not None)
        plugin.whitelist_module.fail_unbind = False

        print("== 6. 子插件 ==")
        code, res = req("GET", "/api/subplugins")
        check("子插件列表", code == 200 and res["data"][0]["name"] == "demo" and res["data"][0]["description"] == "测试")
        code, res = req("POST", "/api/subplugins/demo/toggle", {"enable": False})
        check("子插件禁用", code == 200 and plugin.subplugin_manager.toggled == [("demo", False)])
        code, res = req("POST", "/api/subplugins/nonexist/toggle", {"enable": True})
        check("不存在的子插件返回 404", code == 404)
        code, res = req("POST", "/api/subplugins/reload")
        check("子插件热重载", code == 200 and "1" in res["msg"])

        print("== 7. 日志缓存与 SSE ==")
        plugin.log_buffer.push("info", "Test", "hello log")
        code, res = req("GET", "/api/logs")
        check("日志缓存查询", code == 200 and any(l["msg"] == "hello log" for l in res["data"]))

        sse_lines = []
        def read_sse():
            r = urllib.request.Request(BASE + "/api/logs/stream?token=" + TOKEN)
            try:
                with urllib.request.urlopen(r, timeout=5) as resp:
                    for _ in range(4):
                        line = resp.readline().decode()
                        if line.startswith("data:"):
                            sse_lines.append(json.loads(line[5:]))
                        if len(sse_lines) >= 2:
                            break
            except Exception:
                pass
        t = threading.Thread(target=read_sse, daemon=True)
        t.start()
        # 轮询等待 SSE 订阅建立后再推送（替代固定 sleep，M42）
        _wait_until(lambda: plugin.log_buffer.subscriber_count > 0, timeout=5.0)
        plugin.log_buffer.push("warn", "Test", "sse realtime")
        t.join(timeout=5)
        check("SSE 实时日志推送", any(l.get("msg") == "sse realtime" for l in sse_lines),
              f"got {sse_lines}")
        for _ in range(60):
            if plugin.log_buffer.subscriber_count == 0:
                break
            time.sleep(0.05)
        check("SSE 断开后释放订阅", plugin.log_buffer.subscriber_count == 0,
              f"remaining {plugin.log_buffer.subscriber_count}")

        print("== 8. 子插件 Web 扩展 ==")
        from endstone_lumenbridge.subplugin.context import WebBridge
        web = WebBridge(plugin, "demo", "demo")
        web.createConfig().text("api_key", "abc", "密钥").number("interval", 5, "间隔").switch("on", True, "开关").register()
        code, res = req("GET", "/api/plugins/configs")
        check("配置表单注册", code == 200 and "demo" in res["data"])
        code, res = req("GET", "/api/plugins/config/demo")
        check("配置表单 schema", code == 200 and len(res["data"]["items"]) == 3)
        code, res = req("POST", "/api/plugins/config/demo", {"api_key": "xyz", "interval": 10})
        check("配置表单保存触发事件", code == 200 and
              any(e[0] == "config.update.demo" and e[1] == ("api_key", "xyz") for e in plugin.bus.emitted))
        code, res = req("GET", "/api/plugins/config/demo")
        check("schema 值已更新", res["data"]["items"][0]["val"] == "xyz")

        web.registerApi("GET", "/demo/stat", lambda r: {"count": 42})
        code, res = req("GET", "/api/plugin/demo/stat")
        check("自定义 API 调用", code == 200 and res["data"]["count"] == 42)
        code, res = req("GET", "/api/plugin/demo/stat", token="")
        check("自定义 API 默认需鉴权", code == 401)

        web.registerPage("演示页", "web/index.html")
        code, res = req("GET", "/api/custom_pages")
        check("自定义页面注册", code == 200 and res["data"][0]["title"] == "演示页"
              and res["data"][0]["url"] == "/plugin-views/demo/web/index.html")

        print("== 9. 静态资源 ==")
        code, body = req("GET", "/", raw=True, token="")
        check("SPA 首页可访问", code == 200 and "LumenBridge" in body)
        code, body = req("GET", "/app.js", raw=True, token="")
        check("app.js 可访问", code == 200 and "loadDashboard" in body)
        code, body = req("GET", "/nonexistent-page", raw=True, token="")
        check("未知路径 fallback 到 SPA", code == 200 and "LumenBridge" in body)

        print("== 10. 路径安全 ==")
        code, res = req("GET", "/plugin-views/../../config.json")
        check("目录穿越被拦截", code in (403, 404, 401))

        print(f"\n===== 结果: {len(PASSED)} 通过, {len(FAILED)} 失败 =====")
        # 任一 check 失败即 FAIL（check 已抛 AssertionError 到此说明全过）
    finally:
        if webui is not None:
            webui.stop()
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    """手动运行入口：跑 e2e 并按汇总退出。"""
    try:
        test_webui_e2e()
    except AssertionError:
        print(f"\n===== 结果: {len(PASSED)} 通过, {len(FAILED)} 失败 =====")
        for f in FAILED:
            print(f"  FAILED: {f}")
        sys.exit(1)


if __name__ == "__main__":
    main()
