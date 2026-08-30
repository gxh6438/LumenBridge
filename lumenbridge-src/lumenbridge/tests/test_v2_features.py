"""LumenBridge v2 新功能验证脚本：
1. 新增 OneBot / NapCat API 包构建器（packets）与通用透传 build
2. 适配器高层 API（群管理 / 查询 / NapCat 扩展 / call_action 透传）
3. 子插件 ZIP 安装 / 升级 / 卸载（loader.install_from_zip / uninstall）
4. 子插件完整 Endstone API 透传（lumen.plugin / lumen.server / lumen.endstone / mc 桥接）
5. ConfigFormBuilder 中文标签 label
6. WebUI 新接口：/api/config/labels、ZIP 上传安装、文件浏览与编辑、子插件删除
"""

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

failures: list[str] = []


def check(name: str, fn) -> None:
    try:
        fn()
        print(f"[PASS] {name}")
    except Exception as e:
        import traceback
        traceback.print_exc()
        failures.append(f"{name}: {e!r}")
        print(f"[FAIL] {name}: {e!r}")
        # 失败必须抛错：pytest 收集 test_ 函数时得到真实 FAIL（结构性假绿修复）
        raise AssertionError(f"{name}: {e!r}") from e


# ----------------------------------------------------------------------
# 1. 新增 OneBot 包构建器
# ----------------------------------------------------------------------

def t_new_packets():
    from endstone_lumenbridge.onebot import packets as pk
    assert pk.group_card(1, 2, "新名片")["action"] == "set_group_card"
    assert pk.group_kick(1, 2)["params"]["reject_add_request"] is False
    assert pk.group_whole_ban(1, True)["action"] == "set_group_whole_ban"
    assert pk.group_member_info(1, 2)["action"] == "get_group_member_info"
    assert pk.group_member_list(1)["action"] == "get_group_member_list"
    assert pk.stranger_info(2)["action"] == "get_stranger_info"
    assert pk.group_info(1)["action"] == "get_group_info"
    assert pk.group_list()["action"] == "get_group_list"
    assert pk.group_name(1, "新群名")["params"]["group_name"] == "新群名"
    assert pk.group_admin(1, 2, True)["action"] == "set_group_admin"
    assert pk.group_leave(1)["action"] == "set_group_leave"
    assert pk.friend_add_request("f1", True)["action"] == "set_friend_add_request"
    assert pk.group_add_request("f2", "add", False, "拒绝")["params"]["approve"] is False
    assert pk.get_message(123)["action"] == "get_msg"
    assert pk.send_like(2, 10)["params"]["times"] == 10
    assert pk.login_info()["action"] == "get_login_info"
    assert pk.version_info()["action"] == "get_version_info"
    assert pk.group_msg_emoji_like(5, "128077")["action"] == "set_msg_emoji_like"
    assert pk.group_poke(1, 2)["action"] == "group_poke"
    assert pk.friend_poke(2)["action"] == "friend_poke"
    assert pk.group_sign(1)["action"] == "send_group_sign"
    assert pk.essence_msg(5)["action"] == "set_essence_msg"
    assert pk.group_special_title(1, 2, "头衔")["params"]["special_title"] == "头衔"
    assert pk.upload_group_file(1, "/tmp/a.txt", "a.txt")["action"] == "upload_group_file"
    assert pk.group_root_files(1)["action"] == "get_group_root_files"
    assert pk.group_file_url(1, "fid")["action"] == "get_group_file_url"
    assert pk.status_info()["action"] == "get_status"
    assert pk.image_info("abc.image")["action"] == "get_image"
    # 通用透传
    p = pk.build("custom_action", {"key": "val"}, echo="e9")
    assert p == {"action": "custom_action", "params": {"key": "val"}, "echo": "e9"}


# ----------------------------------------------------------------------
# 2. 适配器高层 API（不联网，拦截 send_pack 检查包体投递）
# ----------------------------------------------------------------------

class _Log:
    def info(self, *a): pass
    def warning(self, *a): pass
    def error(self, *a): pass
    def debug(self, *a): pass


def t_adapter_api():
    from endstone_lumenbridge.event_bus import EventBus
    from endstone_lumenbridge.onebot.adapter import OneBotAdapter

    adapter = OneBotAdapter(_Log(), EventBus(), ws_type=0, target="ws://127.0.0.1:1")
    sent: list[dict] = []
    adapter.send_pack = lambda pkt: sent.append(pkt)  # 拦截发送
    adapter.call_api = lambda pack, callback=None, timeout=15: sent.append(pack)

    for name, args in [
        ("set_group_card", (1, 2, "x")), ("set_group_kick", (1, 2)),
        ("set_group_whole_ban", (1, True)), ("set_group_admin", (1, 2, True)),
        ("set_group_name", (1, "n")), ("set_group_leave", (1,)),
        ("group_poke", (1, 2)), ("friend_poke", (2,)), ("send_like", (2,)),
        ("set_msg_emoji_like", (5, "128077")), ("delete_msg", (5,)),
        ("set_group_ban", (1, 2, 60)), ("set_group_special_title", (1, 2, "t")),
        ("send_group_sign", (1,)), ("set_essence_msg", (5,)),
        ("mark_msg_as_read", (5,)),
    ]:
        assert hasattr(adapter, name), f"缺少方法 {name}"
        getattr(adapter, name)(*args)

    # 异步查询类（回调式）
    cb = lambda d: None  # noqa: E731
    for name, args in [
        ("get_group_member_info", (1, 2, cb)), ("get_group_member_list", (1, cb)),
        ("get_stranger_info", (2, cb)), ("get_group_info", (1, cb)),
        ("get_group_list", (cb,)), ("get_msg", (123, cb)),
        ("get_login_info", (cb,)), ("get_version_info", (cb,)),
        ("get_friend_list", (cb,)), ("get_status", (cb,)),
        ("get_group_honor_info", (1, cb)),
    ]:
        assert hasattr(adapter, name), f"缺少查询方法 {name}"
        getattr(adapter, name)(*args)

    # 通用透传
    adapter.call_action("some_new_api", {"a": 1})
    adapter.call_action("some_query_api", {"b": 2}, callback=cb)
    assert len(sent) >= 29, f"仅投递 {len(sent)} 个包"
    assert all("action" in p for p in sent)
    assert any(p["action"] == "some_new_api" for p in sent)


# ----------------------------------------------------------------------
# 伪造插件环境（供 loader / context / webui 测试）
# ----------------------------------------------------------------------

class _FakeLogger:
    def __init__(self): self.lines = []
    def info(self, m): self.lines.append(("info", m))
    def warning(self, m): self.lines.append(("warn", m))
    def error(self, m): self.lines.append(("error", m))
    def debug(self, m): self.lines.append(("debug", m))


class _FakeScheduler:
    def run_task(self, plugin, fn, delay=0, period=0):
        fn()
        class _T: task_id = 1
        return _T()


class _FakeServer:
    def __init__(self):
        self.online_players = []
        self.scheduler = _FakeScheduler()
        self.dispatched = []
        self.command_sender = object()
    def dispatch_command(self, sender, cmd):
        self.dispatched.append(cmd); return True
    def broadcast_message(self, msg): pass


class _FakePlugin:
    VERSION = "2.0.0"
    def __init__(self, tmp: Path):
        from endstone_lumenbridge.event_bus import EventBus
        from endstone_lumenbridge.subplugin.context import EnvPool
        self.logger = _FakeLogger()
        self._tee_logger = None
        self.server = _FakeServer()
        self.bus = EventBus()
        self.adapter = None
        self.config_manager = type("CM", (), {"data": {}, "main_group": 123, "debug": False})()
        self.data_folder = str(tmp)
        self.webui_server = None
        self.regex_module = None
        self.whitelist_module = None
        self.env_pool = EnvPool(self)
        # 与 plugin.__init__ 保持一致：WebUI 与 marketplace 共享此串行锁
        self._pip_serial_lock = __import__("threading").Lock()
    def run_on_main(self, fn, delay=0): fn()


def _make_plugin_zip(name: str, version: str = "1.0.0", top_folder: bool = True) -> str:
    buf = io.BytesIO()
    prefix = f"{name}/" if top_folder else ""
    manifest = {"name": name, "version": version, "desc": "测试插件", "load": True}
    code = 'def on_load(lumen):\n    lumen.logger.info("hi from ' + name + '")\n'
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(prefix + "lumen.json", json.dumps(manifest))
        zf.writestr(prefix + "main.py", code)
        zf.writestr(prefix + "data.txt", "some data")
    tmp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
    tmp.write(buf.getvalue())
    tmp.close()
    return tmp.name


# ----------------------------------------------------------------------
# 3. 子插件 ZIP 安装 / 升级 / 卸载
# ----------------------------------------------------------------------

def t_install_zip():
    from endstone_lumenbridge.subplugin.loader import SubPluginManager
    tmp = Path(tempfile.mkdtemp())
    try:
        plugin = _FakePlugin(tmp)
        mgr = SubPluginManager(plugin)
        zip1 = _make_plugin_zip("test_sp", "1.0.0")
        ok, msg, name = mgr.install_from_zip(zip1)
        assert ok, msg
        assert name == "test_sp"
        assert (mgr.plugins_dir / "test_sp" / "main.py").is_file()
        # 低版本覆盖应被拒绝
        zip_old = _make_plugin_zip("test_sp", "0.9.0")
        ok2, msg2, _ = mgr.install_from_zip(zip_old)
        assert not ok2, "低版本覆盖应被拒绝: " + msg2
        # 高版本升级
        zip2 = _make_plugin_zip("test_sp", "2.0.0")
        ok3, msg3, _ = mgr.install_from_zip(zip2)
        assert ok3, msg3
        m = json.loads((mgr.plugins_dir / "test_sp" / "lumen.json").read_text("utf-8"))
        assert m["version"] == "2.0.0"
        # 无顶层文件夹（平铺 zip）
        zip3 = _make_plugin_zip("flat_sp", "1.0.0", top_folder=False)
        ok4, msg4, name4 = mgr.install_from_zip(zip3)
        assert ok4, msg4
        assert (mgr.plugins_dir / name4 / "main.py").is_file()
        # 卸载
        ok5, msg5 = mgr.uninstall("test_sp")
        assert ok5, msg5
        assert not (mgr.plugins_dir / "test_sp").exists()
        ok6, _ = mgr.uninstall("not_exists")
        assert not ok6
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ----------------------------------------------------------------------
# 4. 子插件完整 Endstone API 透传
# ----------------------------------------------------------------------

def t_context_full_api():
    from endstone_lumenbridge.subplugin.context import LumenContext
    tmp = Path(tempfile.mkdtemp())
    try:
        plugin = _FakePlugin(tmp)
        data_dir = tmp / "plugins" / "demo"
        data_dir.mkdir(parents=True)
        ctx = LumenContext(plugin, "demo", data_dir)
        # 完整 Endstone API 透传
        assert ctx.plugin is plugin
        assert ctx.server is plugin.server
        # H14：scheduler 为任务跟踪代理（run_task* 返回的 task 记入
        # _scheduled_tasks，_cleanup 时统一 cancel，防热重载残留）；
        # 底层对象与调用行为完整透传——验证委托而非裸身份
        sched = ctx.scheduler
        raw = plugin.server.scheduler
        assert sched is raw or getattr(sched, "_scheduler", None) is raw
        ran = []
        task = sched.run_task(plugin, lambda: ran.append(1))
        assert ran == [1] and getattr(task, "task_id", None) == 1
        assert task in ctx._scheduled_tasks
        import endstone
        assert ctx.endstone is endstone
        mod = ctx.import_module("endstone.event")
        assert hasattr(mod, "PlayerJoinEvent")
        # mc 桥接
        assert hasattr(ctx.mc, "runcmd") or hasattr(ctx.mc, "run_command")
        assert hasattr(ctx.mc, "listen")
        run = getattr(ctx.mc, "runcmd", None) or getattr(ctx.mc, "run_command")
        run("say test")
        assert "say test" in plugin.server.dispatched
        # QQ 客户端桥接（adapter 为 None 时属性仍可访问）
        assert hasattr(ctx, "QClient")
        # 消息与包构建器透传
        assert hasattr(ctx.msgbuilder, "at") or hasattr(ctx.msgbuilder, "text")
        assert hasattr(ctx.packbuilder, "build")
        # run_on_main / 存储
        ran = []
        ctx.run_on_main(lambda: ran.append(1))
        assert ran == [1]
        assert hasattr(ctx, "storage")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ----------------------------------------------------------------------
# 5. ConfigFormBuilder 中文标签
# ----------------------------------------------------------------------

def t_configform_label():
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
    # 未给 label 时回退 key
    assert items[2].get("label", "limit") in ("limit", "")


# ----------------------------------------------------------------------
# 6. WebUI 新接口（真实 HTTP 请求）
# ----------------------------------------------------------------------

def t_webui_v2_api():
    from endstone_lumenbridge.subplugin.loader import SubPluginManager
    from endstone_lumenbridge.webui import LogBuffer
    from endstone_lumenbridge.webui.server import WebUIServer

    tmp = Path(tempfile.mkdtemp())
    try:
        plugin = _FakePlugin(tmp)
        plugin.log_buffer = LogBuffer()
        plugin.config_manager.data = {
            "webui": {"host": "127.0.0.1", "port": 18391, "password": "testpwd", "secret": "s3"},
        }
        plugin.subplugin_manager = SubPluginManager(plugin)
        srv = WebUIServer(plugin)
        srv.start()
        # 轮询等待端口就绪（替代固定 sleep，M42）
        import socket as _socket
        _deadline = time.monotonic() + 5.0
        while time.monotonic() < _deadline:
            try:
                with _socket.create_connection(("127.0.0.1", 18391), timeout=0.5):
                    break
            except OSError:
                time.sleep(0.05)
        else:
            raise RuntimeError("WebUIServer 启动超时（端口 18391 未就绪）")
        base = "http://127.0.0.1:18391"

        def req(method, path, body=None, token=None, raw_body=None, ctype=None):
            headers = {}
            if token: headers["Authorization"] = "Bearer " + token
            data = None
            if body is not None:
                data = json.dumps(body).encode(); headers["Content-Type"] = "application/json"
            if raw_body is not None:
                data = raw_body; headers["Content-Type"] = ctype
            r = urllib.request.Request(base + path, data=data, headers=headers, method=method)
            try:
                with urllib.request.urlopen(r, timeout=8) as resp:
                    return resp.status, json.loads(resp.read().decode())
            except urllib.error.HTTPError as e:
                return e.code, json.loads(e.read().decode())

        # 登录
        st, d = req("POST", "/api/auth/login", {"password": "testpwd"})
        assert st == 200, d
        token = d["data"]["token"]

        # 中文标签接口
        st, d = req("GET", "/api/config/labels", token=token)
        # v1.2.0 起 main_group 迁移至连接卡片，此处校验仍存在的核心叶子中文标签
        assert st == 200 and d["data"]["whitelist.enable"]["label"] == "启用白名单绑定"

        # multipart 上传安装
        zip_path = _make_plugin_zip("web_sp", "1.0.0")
        zip_bytes = Path(zip_path).read_bytes()
        boundary = "----lumtestboundary"
        mp = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
              f"filename=\"web_sp.zip\"\r\nContent-Type: application/zip\r\n\r\n").encode() \
             + zip_bytes + f"\r\n--{boundary}--\r\n".encode()
        st, d = req("POST", "/api/subplugins/install/upload", token=token,
                    raw_body=mp, ctype=f"multipart/form-data; boundary={boundary}")
        assert st == 200, d
        assert d["data"]["name"] == "web_sp"

        # 文件列表
        st, d = req("GET", "/api/subplugins/web_sp/files", token=token)
        assert st == 200 and any(f["path"] == "main.py" for f in d["data"])

        # 读取与保存文件
        st, d = req("GET", "/api/subplugins/web_sp/file?path=lumen.json", token=token)
        assert st == 200 and "web_sp" in d["data"]["content"]
        st, d = req("POST", "/api/subplugins/web_sp/file?path=lumen.json", token=token,
                    body={"content": json.dumps({"name": "web_sp", "version": "1.0.1"})})
        assert st == 200, d
        # 非法 JSON 被拒绝
        st, d = req("POST", "/api/subplugins/web_sp/file?path=lumen.json", token=token,
                    body={"content": "{bad json"})
        assert st == 400
        # 目录穿越被拦截
        st, d = req("GET", "/api/subplugins/web_sp/file?path=../../config.json", token=token)
        assert st == 403

        # 卸载
        st, d = req("DELETE", "/api/subplugins/web_sp", token=token)
        assert st == 200, d
        st, d = req("GET", "/api/subplugins/web_sp/files", token=token)
        assert st == 404

        # 未登录被拒
        st, _ = req("GET", "/api/config/labels")
        assert st == 401

        srv.stop()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ----------------------------------------------------------------------
# C7：pytest 入口 —— 每个验证段一个用例，异常直接冒泡为真实 FAIL
# ----------------------------------------------------------------------

def test_new_packets():
    """新增 OneBot 包构建器。"""
    t_new_packets()


def test_adapter_api():
    """适配器高层 API 与通用透传。"""
    t_adapter_api()


def test_subplugin_zip_lifecycle():
    """子插件 ZIP 安装/升级/卸载。"""
    t_install_zip()


def test_context_full_api():
    """子插件完整 Endstone API 透传。"""
    t_context_full_api()


def test_configform_label():
    """配置表单中文标签。"""
    t_configform_label()


def test_webui_v2_api():
    """WebUI v2 新接口（真实 HTTP）。"""
    t_webui_v2_api()


_ALL_CHECKS = [
    ("新增 OneBot 包构建器", t_new_packets),
    ("适配器高层 API 与通用透传", t_adapter_api),
    ("子插件 ZIP 安装/升级/卸载", t_install_zip),
    ("子插件完整 Endstone API 透传", t_context_full_api),
    ("配置表单中文标签", t_configform_label),
    ("WebUI v2 新接口", t_webui_v2_api),
]


def main():
    """手动运行入口：汇总全部段落（原模块级顶层执行迁入，C7）。"""
    for name, fn in _ALL_CHECKS:
        try:
            check(name, fn)
        except AssertionError:
            # check 失败已打印详情；吞掉以继续跑完其余段落，最终汇总退出码
            pass
    print("\n==== 结果 ====")
    if failures:
        print(f"{len(failures)} 项失败")
        sys.exit(1)
    print("全部通过")


if __name__ == "__main__":
    main()
