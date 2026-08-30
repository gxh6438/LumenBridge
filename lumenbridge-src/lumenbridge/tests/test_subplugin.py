"""子插件体系端到端测试

在临时数据目录写入 example_greeter 子插件与一个动态生成的测试子插件，
用 Mock Endstone 环境 + 模拟 OneBot 服务端验证：

1. 子插件发现（含清单自动生成）与 priority 排序加载
2. lumen.on 订阅 QQ 群消息并快捷回复
3. lumen.mc.listen("onJoin") 收到游戏事件
4. lumen.storage 私有 JSON 读写
5. lumen.env 共享变量池（含 main_group 动态映射）
6. register_regex_action 自定义动作可被正则引擎调用
7. reload_all 热重载与 unload_all 事件清理
8. 加载失败的子插件不影响其他子插件
"""

import asyncio
import json
import shutil
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

SRC = Path(__file__).parent.parent / "src"
sys.path.insert(0, str(SRC))

from endstone_lumenbridge.vendor import import_websockets  # noqa: E402
from endstone_lumenbridge.config import ConfigManager  # noqa: E402
from endstone_lumenbridge.event_bus import EventBus  # noqa: E402
from endstone_lumenbridge.onebot import EventDispatcher, OneBotAdapter  # noqa: E402
from endstone_lumenbridge.modules.regex_engine import RegexEngineModule  # noqa: E402
from endstone_lumenbridge.subplugin import SubPluginManager  # noqa: E402
from endstone_lumenbridge.subplugin.context import EnvPool  # noqa: E402

# 测试替身的 command_sender 是普通 Python 对象，真实 endstone 的
# CommandSenderWrapper（pybind 类型校验）无法包装。生产环境中 sender 恒为
# 真实 ConsoleCommandSender，不受影响；此处替换为记录回调的纯 Python 替身。
import endstone.command as _endstone_command  # noqa: E402


class _FakeCommandSenderWrapper:
    def __init__(self, sender, on_message=None, on_error=None):
        self._sender = sender
        self._on_message = on_message
        self._on_error = on_error


_endstone_command.CommandSenderWrapper = _FakeCommandSenderWrapper

websockets = import_websockets()

MAIN_GROUP = 987654321
PORT = 18766

received_packs: list[dict] = []
server_loop = None
connected_ws = None


async def onebot_handler(ws):
    global connected_ws
    connected_ws = ws
    async for raw in ws:
        pack = json.loads(raw)
        received_packs.append(pack)
        if "echo" in pack and pack.get("action") == "send_group_msg":
            await ws.send(json.dumps({
                "status": "ok", "retcode": 0, "echo": pack["echo"],
                "data": {"message_id": 1},
            }))


def push_event(event: dict) -> None:
    asyncio.run_coroutine_threadsafe(
        connected_ws.send(json.dumps(event)), server_loop
    ).result(timeout=5)


def start_mock_server():
    global server_loop
    server_loop = asyncio.new_event_loop()

    def run():
        asyncio.set_event_loop(server_loop)

        async def main():
            async with websockets.serve(onebot_handler, "127.0.0.1", PORT):
                await asyncio.Future()

        server_loop.run_until_complete(main())

    threading.Thread(target=run, daemon=True).start()
    # 轮询等待模拟服务端端口就绪（替代固定 sleep，M42）
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", PORT), timeout=0.5):
                break
        except OSError:
            time.sleep(0.05)
    else:
        raise RuntimeError(f"模拟 OneBot 服务端启动超时（端口 {PORT}）")


class FakeLogger:
    def _log(self, lv, msg): print(f"[{lv}] {msg}")
    def info(self, msg): self._log("INFO", msg)
    def warning(self, msg): self._log("WARN", msg)
    def error(self, msg): self._log("ERROR", msg)


class FakeScheduler:
    def run_task(self, plugin, func, delay=0, period=0):
        threading.Timer(0.05, func).start()


class FakeServer:
    def __init__(self):
        self.scheduler = FakeScheduler()
        self.command_sender = object()
        self.broadcasts = []
        self.dispatched = []
        self.online_players = []

    def broadcast_message(self, msg):
        self.broadcasts.append(str(msg))

    def dispatch_command(self, sender, cmd):
        self.dispatched.append(cmd)
        return True


class FakePlugin:
    def __init__(self, data_folder):
        self.logger = FakeLogger()
        self.server = FakeServer()
        self.data_folder = str(data_folder)
        self.config_manager = None
        self.connections = None
        self.bus = None
        self.adapter = None
        self.regex_module = None
        self.env_pool = None
        self.subplugin_manager = None

    def run_on_main(self, func, delay=1):
        self.server.scheduler.run_task(self, func, delay=delay)

    def group_allowed(self, pack):
        # 与 LumenBridgePlugin.group_allowed 的 connections=None 分支一致
        gid = pack.get("group_id")
        return gid is not None and gid in self.config_manager.main_groups


def wait_for(cond, timeout=8.0, desc=""):
    start = time.time()
    while time.time() - start < timeout:
        if cond():
            return True
        time.sleep(0.1)
    raise TimeoutError(f"等待超时: {desc}")


def group_msgs():
    out = []
    for p in received_packs:
        if p.get("action") == "send_group_msg":
            segs = p["params"]["message"]
            out.append("".join(
                s["data"].get("text", "") for s in segs if s.get("type") == "text"
            ))
    return out


def make_group_msg(user_id, raw):
    return {
        "post_type": "message", "message_type": "group", "sub_type": "normal",
        "group_id": MAIN_GROUP, "user_id": user_id, "message_id": 100,
        "raw_message": raw, "message": [{"type": "text", "data": {"text": raw}}],
        "sender": {"user_id": user_id, "nickname": "测试员", "card": "", "role": "member"},
        "self_id": 114514,
    }


BROKEN_PLUGIN = "raise RuntimeError('故意坏掉')\n"

PRIORITY_PLUGIN = """
def on_load(lumen):
    order = lumen.env.get("load_order") or []
    order.append(lumen.pluginName)
    lumen.env.set("load_order", order)
"""


def main():
    """运行子插件 e2e 流程并返回结果列表 [(name, ok), ...]（pytest 入口复用）。"""
    data_dir = Path(tempfile.mkdtemp(prefix="lumen_sub_test_"))
    try:
        return _main_flow(data_dir)
    finally:
        shutil.rmtree(data_dir, ignore_errors=True)


def _main_flow(data_dir: Path):
    plugins_dir = data_dir / "plugins"
    plugins_dir.mkdir(parents=True)

    # 1. 复制示例子插件
    example_src = Path(__file__).parent.parent / "examples_plugins" / "subplugins" / "example_greeter"
    shutil.copytree(example_src, plugins_dir / "example_greeter")

    # 2. 坏插件（验证隔离）
    (plugins_dir / "broken_one").mkdir()
    (plugins_dir / "broken_one" / "main.py").write_text(BROKEN_PLUGIN, encoding="utf-8")

    # 3. priority 排序验证插件（pre 与 post）
    for name, prio in [("z_pre_plugin", "pre"), ("a_post_plugin", "post")]:
        d = plugins_dir / name
        d.mkdir()
        (d / "main.py").write_text(PRIORITY_PLUGIN, encoding="utf-8")
        (d / "lumen.json").write_text(json.dumps({
            "name": name, "version": "1.0.0", "load": True, "priority": prio,
        }), encoding="utf-8")

    start_mock_server()

    plugin = FakePlugin(data_dir)
    plugin.config_manager = ConfigManager(data_dir, plugin.logger)
    # v1.2.0 起 main_group 由 connections.json 承载（经 ConnectionManager）
    from endstone_lumenbridge.connections import ConnectionManager
    plugin.connections = ConnectionManager(data_dir, plugin.logger)
    ws_card = plugin.connections.websocket_adapters[0]
    ws_card["main_group"] = MAIN_GROUP
    plugin.config_manager.attach_connections(plugin.connections)
    plugin.bus = EventBus(plugin.logger)
    plugin.adapter = OneBotAdapter(
        plugin.logger, plugin.bus, ws_type=0,
        target=f"ws://127.0.0.1:{PORT}", access_token="", bot_qq=114514,
    )
    EventDispatcher(plugin.adapter, plugin.bus, plugin.logger)
    plugin.regex_module = RegexEngineModule(plugin)
    plugin.env_pool = EnvPool(plugin)
    plugin.subplugin_manager = SubPluginManager(plugin)
    plugin.subplugin_manager.load_all()
    plugin.adapter.start()

    results = []

    def record(name, ok, detail=""):
        results.append((name, ok))
        print(f"[{'PASS' if ok else 'FAIL'}] {name} {detail}")

    mgr = plugin.subplugin_manager

    # T1: 发现与隔离
    record("子插件发现与坏插件隔离",
           mgr.subplugins.get("example_greeter") and mgr.subplugins["example_greeter"].loaded
           and mgr.subplugins.get("broken_one") is not None
           and not mgr.subplugins["broken_one"].error == "")

    # T2: 清单自动生成
    record("缺失清单自动生成",
           (plugins_dir / "broken_one" / "lumen.json").is_file())

    # T3: priority 排序（pre 先于 post）
    order = plugin.env_pool.get("load_order")
    record("priority 三段加载顺序", order == ["z_pre_plugin", "a_post_plugin"], str(order))

    # T4: storage 自动生成私有配置
    record("storage 私有配置生成",
           (plugins_dir / "example_greeter" / "config.json").is_file())

    # T5: env 动态映射
    record("env 共享池 main_group 映射",
           plugin.env_pool.get("main_group") == MAIN_GROUP)

    wait_for(lambda: plugin.adapter.is_connected, desc="WS 连接")
    time.sleep(0.5)

    # T6: 子插件订阅群消息并回复
    n0 = len(group_msgs())
    push_event(make_group_msg(1111, "你好"))
    try:
        wait_for(lambda: any("你好呀，测试员" in m for m in group_msgs()[n0:]), desc="问候回复")
        record("子插件订阅群消息并回复", True)
    except TimeoutError as e:
        record("子插件订阅群消息并回复", False, str(e))

    # T7: 子插件 runcmdEx（"在线人数" -> list 命令）
    n0 = len(group_msgs())
    push_event(make_group_msg(1111, "在线人数"))
    try:
        wait_for(lambda: any("list" in c for c in plugin.server.dispatched), desc="list 命令")
        record("子插件 runcmdEx 执行命令", True)
    except TimeoutError as e:
        record("子插件 runcmdEx 执行命令", False, str(e))

    # T8: mc.listen onJoin -> QClient 群通知
    n0 = len(group_msgs())
    plugin.bus.emit("mc.player_join", "Steve")
    try:
        wait_for(lambda: any("欢迎 Steve" in m for m in group_msgs()[n0:]), desc="进服欢迎")
        record("mc.listen 游戏事件桥接", True)
    except TimeoutError as e:
        record("mc.listen 游戏事件桥接", False, str(e))

    # T9: 正则引擎自定义动作已注册
    record("register_regex_action 注册",
           "greet" in getattr(plugin.regex_module, "custom_actions", {}))

    # T10: 热重载
    count = mgr.reload_all()
    record("reload_all 热重载", count >= 3, f"加载 {count} 个")

    # T11: 卸载后事件清理（不再回复）
    mgr.unload_all()
    n0 = len(group_msgs())
    push_event(make_group_msg(1111, "你好"))
    time.sleep(1.5)
    record("unload_all 事件清理", len(group_msgs()) == n0)

    plugin.adapter.stop()

    return results


def print_summary(results):
    print("\n==== 子插件测试结果 ====")
    failed = [r for r in results if not r[1]]
    for name, ok in results:
        print(f"  {'PASS' if ok else 'FAIL'} - {name}")
    if failed:
        print(f"\n{len(failed)} 项失败")
        sys.exit(1)
    print("\n全部通过")


# C7：pytest 入口 —— 驱动真实 e2e 流程，任一 record 失败即 FAIL
def test_subplugin_e2e():
    results = main()
    failed = [name for name, ok in results if not ok]
    assert not failed, f"子插件测试失败项: {failed}"


if __name__ == "__main__":
    print_summary(main())
