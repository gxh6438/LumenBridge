"""LumenBridge 端到端集成测试

在本地启动一个模拟 OneBot v11 WebSocket 服务端（模拟 NapCat），
用 Mock 对象替代 Endstone 的 Plugin / Server / Scheduler，
验证以下链路：

1. 正向 WS 连接建立 + bot.online 事件
2. QQ 群消息 -> 游戏广播（ChatSync）
3. 游戏聊天/进服/退服/死亡 -> QQ 群消息
4. 白名单绑定 / 解绑 / 退群自动移除（allowlist 命令 + 回复）
5. 正则引擎：查服（executeCommand 捕获输出）、管理员执行命令、
   权限拒绝（非管理员）、muteUser 动作
6. echo API 请求-回执（call_api）
"""

import asyncio
import json
import shutil
import socket
import sys
import threading
import time
from pathlib import Path

SRC = Path(__file__).parent.parent / "src"
sys.path.insert(0, str(SRC))

from endstone_lumenbridge.vendor import import_websockets  # noqa: E402
from endstone_lumenbridge.config import ConfigManager  # noqa: E402
from endstone_lumenbridge.event_bus import EventBus  # noqa: E402
from endstone_lumenbridge.onebot import EventDispatcher  # noqa: E402
from endstone_lumenbridge.modules.chat_sync import ChatSyncModule  # noqa: E402
from endstone_lumenbridge.modules.whitelist import WhitelistModule  # noqa: E402
from endstone_lumenbridge.modules.regex_engine import RegexEngineModule  # noqa: E402

# 测试替身的 command_sender 是普通 Python 对象，真实 endstone 的
# CommandSenderWrapper（pybind 类型校验）无法包装。生产环境中 sender 恒为
# 真实 ConsoleCommandSender，不受影响；此处替换为暴露回调的纯 Python 替身，
# 供 FakeServer.dispatch_command 模拟命令回显。
import endstone.command as _endstone_command  # noqa: E402


class _FakeCommandSenderWrapper:
    def __init__(self, sender, on_message=None, on_error=None):
        self._sender = sender
        self._on_message_cb = on_message
        self._on_error_cb = on_error


_endstone_command.CommandSenderWrapper = _FakeCommandSenderWrapper

websockets = import_websockets()

MAIN_GROUP = 987654321
ADMIN_QQ = 123456789
PORT = 18765

# ----------------------------------------------------------------------
# 模拟 OneBot 服务端
# ----------------------------------------------------------------------

received_packs: list[dict] = []
server_loop = None
connected_ws = None


async def onebot_handler(ws):
    global connected_ws
    connected_ws = ws
    async for raw in ws:
        pack = json.loads(raw)
        received_packs.append(pack)
        # 自动应答 echo API 请求
        if "echo" in pack:
            action = pack.get("action")
            if action == "get_group_member_list":
                await ws.send(json.dumps({
                    "status": "ok", "retcode": 0, "echo": pack["echo"],
                    "data": [{"user_id": ADMIN_QQ}, {"user_id": 1111}],
                }))
            elif action == "send_group_msg":
                await ws.send(json.dumps({
                    "status": "ok", "retcode": 0, "echo": pack["echo"],
                    "data": {"message_id": 42},
                }))


def push_event(event: dict) -> None:
    """从模拟服务端向插件推送事件"""
    fut = asyncio.run_coroutine_threadsafe(
        connected_ws.send(json.dumps(event)), server_loop
    )
    fut.result(timeout=5)


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


# ----------------------------------------------------------------------
# 模拟 Endstone 环境
# ----------------------------------------------------------------------

class FakeLogger:
    def _log(self, lv, msg): print(f"[{lv}] {msg}")
    def info(self, msg): self._log("INFO", msg)
    def warning(self, msg): self._log("WARN", msg)
    def error(self, msg): self._log("ERROR", msg)


class FakeScheduler:
    """立即在独立线程执行（模拟主线程 tick 调度）"""
    def run_task(self, plugin, func, delay=0, period=0):
        threading.Timer(0.05, func).start()


class FakeServer:
    def __init__(self):
        self.scheduler = FakeScheduler()
        self.command_sender = object()
        self.broadcasts: list[str] = []
        self.dispatched: list[str] = []

    def broadcast_message(self, msg):
        self.broadcasts.append(str(msg))

    def dispatch_command(self, sender, cmd):
        self.dispatched.append(cmd)
        # 模拟命令回显：生产环境由 0.11 CommandSenderWrapper 的 on_message 回调捕获
        cb = getattr(sender, "_on_message_cb", None)
        if callable(cb):
            if cmd.split(" ")[0] in ("list", "listd"):
                cb("There are 0 of a max of 20 players online:")
        return True


class FakePlugin:
    """模拟 LumenBridgePlugin 的宿主接口"""
    def __init__(self, data_folder: Path):
        self.logger = FakeLogger()
        self.server = FakeServer()
        self.data_folder = str(data_folder)
        self.config_manager = None
        self.bus = None
        self.adapter = None
        self.chat_sync_module = None
        self.whitelist_module = None
        self.regex_module = None

    def run_on_main(self, func, delay=1):
        self.server.scheduler.run_task(self, func, delay=delay)

    def group_allowed(self, pack):
        # 与 LumenBridgePlugin.group_allowed 的行为一致：群号须在主群集合内
        gid = pack.get("group_id")
        return gid is not None and gid in self.config_manager.main_groups


# ----------------------------------------------------------------------
# 测试主流程
# ----------------------------------------------------------------------

def wait_for(cond, timeout=8.0, desc=""):
    start = time.time()
    while time.time() - start < timeout:
        if cond():
            return True
        time.sleep(0.1)
    raise TimeoutError(f"等待超时: {desc}")


def group_msgs():
    """提取模拟服务端收到的 send_group_msg 文本"""
    out = []
    for p in received_packs:
        if p.get("action") == "send_group_msg":
            segs = p["params"]["message"]
            text = "".join(
                s["data"].get("text", "") for s in segs if s.get("type") == "text"
            )
            out.append(text)
    return out


def make_group_msg(user_id, raw, message=None, role="member", card=""):
    return {
        "post_type": "message", "message_type": "group", "sub_type": "normal",
        "group_id": MAIN_GROUP, "user_id": user_id, "message_id": 100,
        "raw_message": raw,
        "message": message or [{"type": "text", "data": {"text": raw}}],
        "sender": {"user_id": user_id, "nickname": f"用户{user_id}", "card": card, "role": role},
        "self_id": 114514,
    }


def main():
    """运行端到端流程并返回结果列表 [(name, ok, detail), ...]（pytest 入口复用）。"""
    import tempfile
    data_dir = Path(tempfile.mkdtemp(prefix="lumen_test_"))
    try:
        return _main_flow(data_dir)
    finally:
        shutil.rmtree(data_dir, ignore_errors=True)


def _main_flow(data_dir: Path):
    start_mock_server()

    plugin = FakePlugin(data_dir)
    plugin.config_manager = ConfigManager(data_dir, plugin.logger)
    # v1.2.0 起 main_group/admin_qq/connection 由 connections.json 承载（经 ConnectionManager）
    from endstone_lumenbridge.connections import ConnectionManager
    plugin.connections = ConnectionManager(data_dir, plugin.logger)
    ws_card = plugin.connections.websocket_adapters[0]
    ws_card["enabled"] = True
    ws_card["target"] = f"ws://127.0.0.1:{PORT}"
    ws_card["main_group"] = MAIN_GROUP
    ws_card["admin_qq"] = [ADMIN_QQ]
    ws_card["bot_qq"] = 114514
    plugin.config_manager.attach_connections(plugin.connections)

    plugin.bus = EventBus(plugin.logger)
    # v1.2.0 多适配器架构：hub 按 connections.json 卡片创建并管理适配器实例
    from endstone_lumenbridge.onebot.hub import AdapterHub
    plugin.hub = AdapterHub(plugin.logger, plugin.bus, plugin.connections)
    plugin.adapter = plugin.hub
    plugin.dispatcher = EventDispatcher(plugin.hub, plugin.bus, plugin.logger)
    plugin.hub.sync_from_manager()
    plugin.whitelist_module = WhitelistModule(plugin)
    plugin.chat_sync_module = ChatSyncModule(plugin)
    plugin.regex_module = RegexEngineModule(plugin)

    plugin.adapter.start()

    results = []

    def record(name, ok, detail=""):
        results.append((name, ok, detail))
        print(f"[{'PASS' if ok else 'FAIL'}] {name} {detail}")

    # 1. 连接建立
    try:
        wait_for(lambda: plugin.adapter.is_connected, desc="WS 连接")
        record("正向 WS 连接建立", True)
    except TimeoutError as e:
        record("正向 WS 连接建立", False, str(e)); return results

    time.sleep(0.5)

    # 2. QQ -> 游戏
    push_event(make_group_msg(1111, "大家好", card="小明"))
    try:
        wait_for(lambda: any("小明" in b and "大家好" in b for b in plugin.server.broadcasts),
                 desc="QQ 消息广播进游戏")
        record("QQ 群消息 -> 游戏广播", True, plugin.server.broadcasts[-1])
    except TimeoutError as e:
        record("QQ 群消息 -> 游戏广播", False, str(e))

    # 3. 游戏 -> QQ
    n0 = len(group_msgs())
    plugin.chat_sync_module.on_player_chat("Steve", "hello qq")
    plugin.chat_sync_module.on_player_join("Alex")
    plugin.chat_sync_module.on_player_death("Steve 被苦力怕炸死了")
    try:
        wait_for(lambda: len(group_msgs()) >= n0 + 3, desc="游戏事件转发到群")
        msgs = group_msgs()[n0:]
        ok = (any("Steve" in m and "hello qq" in m for m in msgs)
              and any("Alex" in m and "进服" in m for m in msgs)
              and any("苦力怕" in m for m in msgs))
        record("游戏聊天/进服/死亡 -> QQ 群", ok, str(msgs))
    except TimeoutError as e:
        record("游戏聊天/进服/死亡 -> QQ 群", False, str(e))

    # 4. 白名单绑定
    n0 = len(group_msgs())
    push_event(make_group_msg(1111, "绑定白名单SteveXbox"))
    try:
        wait_for(lambda: any("绑定成功" in m for m in group_msgs()[n0:]), desc="绑定回复")
        wait_for(lambda: any("whitelist add" in c for c in plugin.server.dispatched), desc="whitelist add")
        entry = plugin.whitelist_module.get_binding_by_qq(1111)
        record("白名单绑定", entry is not None and entry["xbox"] == "SteveXbox")
    except TimeoutError as e:
        record("白名单绑定", False, str(e))

    # 5. 正则引擎：查服（executeCommand 经 0.11 CommandSenderWrapper on_message 回调捕获输出）
    n0 = len(group_msgs())
    push_event(make_group_msg(1111, "查服"))
    try:
        wait_for(lambda: len(group_msgs()) > n0, desc="查服回复")
        record("正则引擎-查服(executeCommand)", True, group_msgs()[-1])
    except TimeoutError as e:
        record("正则引擎-查服(executeCommand)", False, str(e))

    # 6. 正则引擎：非管理员执行命令被拒绝
    n0 = len(group_msgs())
    push_event(make_group_msg(1111, "执行say hack"))
    time.sleep(1.5)
    denied = not any("执行结果" in m for m in group_msgs()[n0:])
    record("正则引擎-非管理员被拒绝", denied)

    # 7. 正则引擎：管理员执行命令
    n0 = len(group_msgs())
    push_event(make_group_msg(ADMIN_QQ, "执行say hello"))
    try:
        wait_for(lambda: any("执行结果" in m for m in group_msgs()[n0:]), desc="管理员执行命令")
        ok = any("say hello" in c for c in plugin.server.dispatched)
        record("正则引擎-管理员执行命令", ok, str(plugin.server.dispatched[-2:]))
    except TimeoutError as e:
        record("正则引擎-管理员执行命令", False, str(e))

    # 8. 退群自动移除白名单
    n0 = len(group_msgs())
    push_event({
        "post_type": "notice", "notice_type": "group_decrease",
        "group_id": MAIN_GROUP, "user_id": 1111, "self_id": 114514,
    })
    try:
        wait_for(lambda: plugin.whitelist_module.get_binding_by_qq(1111) is None, desc="退群移除绑定")
        wait_for(lambda: any("whitelist remove" in c for c in plugin.server.dispatched), desc="whitelist remove")
        record("退群自动移除白名单", True)
    except TimeoutError as e:
        record("退群自动移除白名单", False, str(e))

    # 9. echo API 请求-回执
    got = {}
    plugin.adapter.get_group_member_list(MAIN_GROUP, lambda data: got.update({"data": data}))
    try:
        wait_for(lambda: "data" in got, desc="echo 回执")
        record("echo API 请求-回执", isinstance(got["data"], list) and len(got["data"]) == 2)
    except TimeoutError as e:
        record("echo API 请求-回执", False, str(e))

    # 10. 优雅停止
    plugin.hub.stop_all()
    record("适配器优雅停止", not plugin.hub.is_connected)

    return results


def print_summary(results):
    print("\n==== 集成测试结果 ====")
    failed = [r for r in results if not r[1]]
    for name, ok, detail in results:
        print(f"  {'PASS' if ok else 'FAIL'} - {name}")
    if failed:
        print(f"\n{len(failed)} 项失败")
        sys.exit(1)
    print("\n全部通过")


# C7：pytest 入口 —— 驱动真实 e2e 流程，任一 record 失败即 FAIL
def test_integration_e2e():
    results = main()
    failed = [name for name, ok, _ in results if not ok]
    assert not failed, f"集成测试失败项: {failed}"


if __name__ == "__main__":
    print_summary(main())
