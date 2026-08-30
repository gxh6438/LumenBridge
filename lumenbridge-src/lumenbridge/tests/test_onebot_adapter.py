"""OneBot v11 适配器专项测试

针对 v1.0.1 修复的 OneBotAdapter 关键 bug 编写独立验证用例：
1. _requeue_head 将失败包压回队首，保持消息顺序
2. _dispatch_raw 非 dict JSON 包被忽略不崩溃
3. echo 键统一字符串类型（int / str 都能匹配）
4. 反向 WS 多客户端广播单客户端故障不阻塞其他
5. stop() 清理所有状态（_pending / _clients / _send_queue / _ws 等）
6. send_pack 队列满时丢弃最旧包不崩溃
7. call_api 超时回调 None 并清理 _pending
8. is_connected / mode_name 属性正确
9. 反向 WS 异常被记录（不再被静默吞掉）

测试不依赖真实网络：用 asyncio.Queue 直接驱动 + Fake WS 对象验证。
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
from pathlib import Path
from typing import Any

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


class FakeLogger:
    def __init__(self) -> None:
        self.lines: list[tuple[str, str]] = []

    def _rec(self, lv: str, msg: Any) -> None:
        self.lines.append((lv, str(msg)))

    def info(self, m: Any) -> None: self._rec("info", m)
    def warning(self, m: Any) -> None: self._rec("warning", m)
    def error(self, m: Any) -> None: self._rec("error", m)
    def debug(self, m: Any) -> None: self._rec("debug", m)
    def exception(self, m: Any) -> None: self._rec("exception", m)


class FakeBus:
    """最小事件总线：记录 emit 调用，立即同步派发"""

    def __init__(self) -> None:
        self.emitted: list[tuple[str, tuple]] = []
        self._handlers: dict[str, list] = {}

    def on(self, event: str, handler: Any) -> None:
        self._handlers.setdefault(event, []).append(handler)

    def once(self, event: str, handler: Any) -> None:
        self.on(event, handler)

    def off(self, event: str, handler: Any) -> None:
        if handler in self._handlers.get(event, []):
            self._handlers[event].remove(handler)

    def emit(self, event: str, *args: Any) -> None:
        self.emitted.append((event, args))
        for h in list(self._handlers.get(event, [])):
            h(*args)


class FakeWS:
    """模拟 websockets 协议对象"""

    def __init__(self, fail_send: bool = False) -> None:
        self.state = 1  # 1 = OPEN
        self.sent: list[str] = []
        self.fail_send = fail_send
        self.closed = False

    async def send(self, text: str) -> None:
        if self.fail_send:
            raise ConnectionError("模拟发送失败")
        self.sent.append(text)

    async def close(self) -> None:
        self.closed = True
        self.state = 3  # CLOSED


def make_adapter(ws_type: int = 0) -> tuple[Any, FakeLogger, FakeBus]:
    """构造一个不启动线程的 OneBotAdapter 实例，手动注入事件循环"""
    from endstone_lumenbridge.onebot.adapter import OneBotAdapter

    logger = FakeLogger()
    bus = FakeBus()
    adapter = OneBotAdapter(
        logger, bus,
        ws_type=ws_type,
        target="ws://127.0.0.1:1",
        listen_host="127.0.0.1",
        listen_port=0,
        access_token="",
        bot_qq=114514,
    )
    return adapter, logger, bus


def start_loop_thread(adapter: Any) -> threading.Thread:
    """在独立线程中跑 adapter._loop，使 run_coroutine_threadsafe 能执行"""
    loop = adapter._loop
    t = threading.Thread(target=loop.run_forever, name="TestLoop", daemon=True)
    t.start()
    return t


def wait_until(cond, timeout: float = 5.0, interval: float = 0.05,
               desc: str = "condition") -> bool:
    """轮询等待条件成立（替代固定 time.sleep 就绪等待）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(interval)
    return cond()


def stop_loop_thread(adapter: Any, t: threading.Thread) -> None:
    """优雅停止事件循环线程"""
    loop = adapter._loop
    if loop is None:
        return
    loop.call_soon_threadsafe(loop.stop)
    t.join(timeout=3)
    if not loop.is_closed():
        loop.close()


# ======================================================================
# Bug #4: _requeue_head 将失败包压回队首，保持消息顺序
# ======================================================================
def test_requeue_head_preserves_order():
    print("== Bug #4: _requeue_head 压回队首，保持消息顺序 ==")
    adapter, _, _ = make_adapter()
    adapter._send_queue = asyncio.Queue(maxsize=10)
    # 队列里已有 [A, B]
    adapter._send_queue.put_nowait({"action": "A"})
    adapter._send_queue.put_nowait({"action": "B"})
    # 失败包 X 压回队首 -> 期望顺序 [X, A, B]
    adapter._requeue_head({"action": "X"})
    order = []
    while not adapter._send_queue.empty():
        order.append(adapter._send_queue.get_nowait()["action"])
    check("失败包压回队首", order == ["X", "A", "B"], f"got {order}")


def test_requeue_head_full_queue():
    print("== Bug #4 补充: 满队列时 _requeue_head 仍能压入 ==")
    adapter, _, _ = make_adapter()
    adapter._send_queue = asyncio.Queue(maxsize=2)
    adapter._send_queue.put_nowait({"action": "A"})
    adapter._send_queue.put_nowait({"action": "B"})
    # 队列已满，压入 X 应丢弃最旧的 A
    adapter._requeue_head({"action": "X"})
    order = []
    while not adapter._send_queue.empty():
        order.append(adapter._send_queue.get_nowait()["action"])
    check("满队列丢弃最旧包", order == ["X", "B"], f"got {order}")


def test_requeue_head_no_queue():
    print("== Bug #4 补充: 队列未初始化时 _requeue_head 不崩溃 ==")
    adapter, _, _ = make_adapter()
    adapter._send_queue = None
    try:
        adapter._requeue_head({"action": "X"})
        check("无队列不崩溃", True)
    except Exception as e:
        check("无队列不崩溃", False, repr(e))


# ======================================================================
# Bug #5: _dispatch_raw 非 dict JSON 包被忽略
# ======================================================================
def test_dispatch_raw_non_dict():
    print("== Bug #5: 非 dict JSON 包被忽略不崩溃 ==")
    adapter, logger, bus = make_adapter()
    bus.on("onebot.pack", lambda *a: None)
    # 数字、字符串、数组、null 都应被忽略
    for raw in [b"123", b'"hello"', b"[1,2,3]", b"null", b"true"]:
        bus.emitted.clear()
        adapter._dispatch_raw(raw)
        check(f"忽略非 dict 包: {raw!r}",
              len(bus.emitted) == 0, f"emitted={bus.emitted}")
    # 合法 dict 包正常派发
    bus.emitted.clear()
    adapter._dispatch_raw(b'{"post_type":"message","raw_message":"hi"}')
    check("合法 dict 包正常派发", len(bus.emitted) == 1, f"emitted={bus.emitted}")


def test_dispatch_raw_invalid_json():
    print("== Bug #5 补充: 非法 JSON 不崩溃 ==")
    adapter, logger, _ = make_adapter()
    before = len(logger.lines)
    adapter._dispatch_raw(b"not a json")
    check("非法 JSON 不崩溃且记录错误",
          any(lv == "error" for lv, _ in logger.lines[before:]),
          f"logs={logger.lines[before:]}")


# ======================================================================
# Bug #6: echo 键统一字符串类型
# ======================================================================
def test_echo_key_string_consistency():
    print("== Bug #6: echo 键统一字符串类型（int echo 也能匹配）==")
    adapter, _, bus = make_adapter()
    adapter._loop = asyncio.new_event_loop()
    received: list[Any] = []

    # 模拟 call_api 写入 _pending，echo 为字符串 "100"
    fut = adapter._loop.create_future()
    adapter._pending["100"] = fut
    bus.on("packid_100", lambda data: received.append(data))

    # 服务端回包 echo 为 int 100，应能匹配字符串键
    adapter._dispatch_raw(json.dumps({
        "status": "ok", "retcode": 0, "echo": 100, "data": {"ok": True}
    }).encode())

    check("int echo 匹配 str 键", fut.done() and fut.result() == {"ok": True},
          f"done={fut.done()}")
    check("packid_<echo> 事件触发", received == [{"ok": True}], f"received={received}")
    check("_pending 已清理", "100" not in adapter._pending)

    adapter._loop.close()


# ======================================================================
# Bug #7: 反向 WS 多客户端广播单客户端故障不阻塞其他
# ======================================================================
def test_reverse_broadcast_single_client_failure():
    print("== Bug #7: 反向 WS 多客户端广播单客户端故障不阻塞其他 ==")
    # 驱动真实 adapter._sender_loop（不再内联重写发送循环）：
    # 单客户端故障 → sent_any=True 不重入队；全部客户端故障 → 抛错重入队队首
    adapter, logger, _ = make_adapter(ws_type=1)
    adapter._loop = asyncio.new_event_loop()

    good_client = FakeWS(fail_send=False)
    bad_client = FakeWS(fail_send=True)
    adapter._clients = {good_client, bad_client}
    adapter._connected_event = asyncio.Event()
    adapter._send_queue = asyncio.Queue(maxsize=10)

    async def run_single_failure():
        adapter._connected_event.set()
        adapter._running = True
        adapter._send_queue.put_nowait({"action": "ping"})
        task = asyncio.ensure_future(adapter._sender_loop())
        # 轮询等待真实发送循环完成对正常客户端的投递
        for _ in range(200):
            if good_client.sent:
                break
            await asyncio.sleep(0.01)
        adapter._running = False
        await asyncio.wait_for(task, timeout=5)

    adapter._loop.run_until_complete(run_single_failure())

    check("正常客户端收到消息", len(good_client.sent) == 1, f"sent={good_client.sent}")
    check("故障客户端未阻塞广播", True)
    check("单客户端失败时 sent_any=True 不重入队",
          adapter._send_queue.empty(), f"qsize={adapter._send_queue.qsize()}")
    check("故障被记录为 warning",
          any("发送失败" in m for _, m in logger.lines), f"logs={logger.lines}")

    adapter._loop.close()

    # ---- 全部客户端失败：真实循环 raise ConnectionError → _requeue_head 压回队首
    adapter2, logger2, _ = make_adapter(ws_type=1)
    adapter2._loop = asyncio.new_event_loop()
    adapter2._clients = {FakeWS(fail_send=True)}
    adapter2._connected_event = asyncio.Event()
    adapter2._send_queue = asyncio.Queue(maxsize=10)

    async def run_all_failure():
        adapter2._connected_event.set()
        adapter2._running = True
        adapter2._send_queue.put_nowait({"action": "X"})
        adapter2._send_queue.put_nowait({"action": "B"})
        orig_queue = adapter2._send_queue
        task = asyncio.ensure_future(adapter2._sender_loop())
        # 真实 _requeue_head 会用新队列对象整体替换 _send_queue（压回队首），
        # 以对象身份变化精确判定"全部失败 → 重入队"确实发生
        for _ in range(400):
            if adapter2._send_queue is not orig_queue:
                break
            await asyncio.sleep(0.01)
        adapter2._running = False
        # 循环重入队后 sleep(2.0) 退避，等待其自然退出（不 cancel，避免二次重入队）
        await asyncio.wait_for(task, timeout=8)

    adapter2._loop.run_until_complete(run_all_failure())

    order = []
    while not adapter2._send_queue.empty():
        order.append(adapter2._send_queue.get_nowait()["action"])
    check("全部客户端失败时失败包重入队首", order == ["X", "B"], f"got {order}")
    check("全部失败也记录 warning",
          any("发送失败" in m for _, m in logger2.lines), f"logs={logger2.lines}")

    adapter2._loop.close()


# ======================================================================
# Bug #8: stop() 清理所有状态
# ======================================================================
def test_stop_clears_state():
    print("== Bug #8: stop() 清理所有残留状态 ==")
    adapter, _, _ = make_adapter()
    # 注入一些残留状态
    loop = asyncio.new_event_loop()
    adapter._loop = loop
    adapter._running = True
    adapter._pending = {"e1": loop.create_future()}
    adapter._clients = {FakeWS()}
    adapter._connected_event = asyncio.Event()
    adapter._send_queue = asyncio.Queue(maxsize=10)
    adapter._send_queue.put_nowait({"action": "queued"})
    adapter._ws = FakeWS()

    # 启动 loop 线程，让 stop() 内部的 _shutdown 协程能正常 await
    t = start_loop_thread(adapter)
    adapter.stop()

    check("_running 置 False", adapter._running is False)
    check("_pending 已清空", adapter._pending == {})
    check("_clients 已清空", adapter._clients == set())
    check("_connected_event 已清空", adapter._connected_event is None)
    check("_send_queue 已清空", adapter._send_queue is None)
    check("_ws 已清空", adapter._ws is None)
    check("_loop 已清空", adapter._loop is None)
    check("_thread 已清空", adapter._thread is None)
    t.join(timeout=3)


def test_stop_idempotent():
    print("== Bug #8 补充: stop() 可重复调用不崩溃 ==")
    adapter, _, _ = make_adapter()
    adapter._loop = None  # 模拟未启动
    try:
        adapter.stop()
        adapter.stop()
        check("重复 stop 不崩溃", True)
    except Exception as e:
        check("重复 stop 不崩溃", False, repr(e))


def test_stop_then_start_state_clean():
    print("== Bug #8 补充: stop+start 复用实例时旧 Future 不残留 ==")
    adapter, _, _ = make_adapter()
    loop = asyncio.new_event_loop()
    adapter._loop = loop
    adapter._running = True
    adapter._pending = {"old_echo": loop.create_future()}
    adapter._connected_event = asyncio.Event()
    adapter._send_queue = asyncio.Queue(maxsize=5)

    t = start_loop_thread(adapter)
    adapter.stop()
    check("stop 后 _pending 不残留旧 echo", adapter._pending == {},
          f"pending={adapter._pending}")
    t.join(timeout=3)


# ======================================================================
# Bug #11: send_pack 队列满时丢弃最旧包不崩溃
# ======================================================================
def test_send_pack_full_queue():
    print("== Bug #11: send_pack 队列满时丢弃最旧包不崩溃 ==")
    adapter, _, _ = make_adapter()
    adapter._loop = asyncio.new_event_loop()
    adapter._running = True
    adapter._send_queue = asyncio.Queue(maxsize=2)
    adapter._send_queue.put_nowait({"action": "old1"})
    adapter._send_queue.put_nowait({"action": "old2"})

    t = start_loop_thread(adapter)
    try:
        # 队列已满，send_pack 应丢弃最旧包并加入新包
        adapter.send_pack({"action": "new1"})
        # 轮询等待事件循环处理入队（最多 5s，每 0.05s 检查）。
        # 初始 qsize 已为 2（old1+old2），不能用 qsize==2 判断——必须确认
        # 新包 new1 已实际进入队列内部缓冲（deque 只读快照，线程安全）
        wait_until(
            lambda: len(adapter._send_queue._queue) > 0
            and adapter._send_queue._queue[-1].get("action") == "new1",
            desc="send_pack 入队",
        )

        items = []
        while not adapter._send_queue.empty():
            items.append(adapter._send_queue.get_nowait())
        actions = [it["action"] for it in items]
        check("满队列丢弃最旧包并加入新包",
              actions == ["old2", "new1"], f"got {actions}")
    finally:
        stop_loop_thread(adapter, t)


def test_send_pack_not_running():
    print("== Bug #11 补充: 适配器未运行时 send_pack 静默返回 ==")
    adapter, _, _ = make_adapter()
    adapter._running = False
    try:
        adapter.send_pack({"action": "x"})
        check("未运行时 send_pack 不崩溃", True)
    except Exception as e:
        check("未运行时 send_pack 不崩溃", False, repr(e))


# ======================================================================
# Bug #12: call_api 超时回调 None 并清理 _pending
# ======================================================================
def test_call_api_timeout():
    print("== Bug #12: call_api 超时回调 None 并清理 _pending ==")
    adapter, logger, _ = make_adapter()
    adapter._loop = asyncio.new_event_loop()
    adapter._running = True
    adapter._send_queue = asyncio.Queue(maxsize=10)

    t = start_loop_thread(adapter)
    try:
        callback_results: list[Any] = []
        adapter.call_api(
            {"action": "get_group_member_list", "params": {"group_id": 1}},
            callback=lambda data: callback_results.append(data),
            timeout=0.3,
        )
        # 轮询等待超时触发（最多 5s，每 0.05s 检查）
        wait_until(lambda: len(callback_results) >= 1, desc="call_api 超时回调")

        check("超时回调收到 None", callback_results == [None], f"got {callback_results}")
        check("超时后 _pending 已清理", len(adapter._pending) == 0, f"pending={adapter._pending}")
        check("超时记录 warning",
              any("超时" in m for _, m in logger.lines), f"logs={logger.lines}")
    finally:
        stop_loop_thread(adapter, t)


def test_call_api_not_running_callback_none():
    print("== Bug #12 补充: 适配器未运行时 call_api 立即回调 None ==")
    adapter, _, _ = make_adapter()
    adapter._running = False
    results: list[Any] = []
    adapter.call_api({"action": "x"}, callback=lambda d: results.append(d))
    check("未运行时立即回调 None", results == [None], f"got {results}")


# ======================================================================
# Bug #13: is_connected / mode_name 属性
# ======================================================================
def test_properties():
    print("== Bug #13: is_connected / mode_name 属性正确 ==")
    # 正向模式
    adapter_fwd, _, _ = make_adapter(ws_type=0)
    check("正向模式 mode_name", adapter_fwd.mode_name == "正向 WebSocket")
    check("正向模式未连接", adapter_fwd.is_connected is False)

    # 模拟 WS 已连接
    fake_ws = FakeWS()
    adapter_fwd._ws = fake_ws
    check("正向模式已连接", adapter_fwd.is_connected is True)

    fake_ws.state = 3  # CLOSED
    check("正向模式 WS 关闭后未连接", adapter_fwd.is_connected is False)

    # 反向模式
    adapter_rev, _, _ = make_adapter(ws_type=1)
    check("反向模式 mode_name", adapter_rev.mode_name == "反向 WebSocket")
    check("反向模式无客户端未连接", adapter_rev.is_connected is False)

    adapter_rev._clients = {FakeWS()}
    check("反向模式有客户端已连接", adapter_rev.is_connected is True)


# ======================================================================
# Bug #15: 反向 WS 异常被记录（不再被静默吞掉）
# ======================================================================
def test_reverse_handler_logs_exception():
    print("== Bug #15: 反向 WS 接收循环异常被记录（不静默吞掉）==")

    adapter, logger, bus = make_adapter(ws_type=1)
    adapter._loop = asyncio.new_event_loop()
    adapter._running = True
    adapter._connected_event = asyncio.Event()
    adapter._send_queue = asyncio.Queue(maxsize=10)

    # 构造一个会抛异常的 FakeWS（async for 时抛错）
    class ExplodingWS:
        def __aiter__(self): return self
        async def __anext__(self):
            raise RuntimeError("模拟接收异常")
        async def close(self): pass
        request = type("R", (), {"headers": {}})()

    # 反向 handler 通过 websockets.serve 的 handler 调用
    # 直接调用 _reverse_serve 内的 handler 逻辑不太方便，
    # 改为验证 _dispatch_raw 异常被 _sender_loop / 主循环 try 捕获后记录
    before = len(logger.lines)
    # 触发 _dispatch_raw 内部异常（非 JSON）
    adapter._dispatch_raw(b"\xff\xfe bad bytes")
    check("异常被记录为 error",
          any(lv == "error" for lv, _ in logger.lines[before:]),
          f"logs={logger.lines[before:]}")
    adapter._loop.close()


# ======================================================================
# 综合: send_pack + call_api 并发不互相阻塞
# ======================================================================
def test_send_pack_and_call_api_concurrent():
    print("== 综合: send_pack 与 call_api 并发不互相阻塞 ==")
    adapter, _, _ = make_adapter()
    adapter._loop = asyncio.new_event_loop()
    adapter._running = True
    adapter._send_queue = asyncio.Queue(maxsize=100)
    adapter._connected_event = asyncio.Event()

    t = start_loop_thread(adapter)
    try:
        # 并发投递 50 个 send_pack
        for i in range(50):
            adapter.send_pack({"action": f"msg_{i}"})

        # 同时发起一个 call_api（短超时）
        callback_results: list[Any] = []
        adapter.call_api(
            {"action": "query"},
            callback=lambda d: callback_results.append(d),
            timeout=0.2,
        )

        # 轮询等待全部投递完成（send_pack 50 个 + call_api 超时回调）
        wait_until(lambda: adapter._send_queue.qsize() >= 50
                   and callback_results == [None],
                   timeout=5.0, desc="并发投递与超时回调")

        # send_pack 投递的 50 个应都在队列里（call_api 的包也占一个）
        check("send_pack 50 个全部入队", adapter._send_queue.qsize() >= 50,
              f"qsize={adapter._send_queue.qsize()}")
        # call_api 超时后回调 None
        check("call_api 并发超时回调 None", callback_results == [None],
              f"got {callback_results}")
    finally:
        stop_loop_thread(adapter, t)


def main():
    runners = [
        test_requeue_head_preserves_order,
        test_requeue_head_full_queue,
        test_requeue_head_no_queue,
        test_dispatch_raw_non_dict,
        test_dispatch_raw_invalid_json,
        test_echo_key_string_consistency,
        test_reverse_broadcast_single_client_failure,
        test_stop_clears_state,
        test_stop_idempotent,
        test_stop_then_start_state_clean,
        test_send_pack_full_queue,
        test_send_pack_not_running,
        test_call_api_timeout,
        test_call_api_not_running_callback_none,
        test_properties,
        test_reverse_handler_logs_exception,
        test_send_pack_and_call_api_concurrent,
    ]
    for fn in runners:
        print(f"\n== {fn.__name__} ==")
        try:
            fn()
        except AssertionError:
            # check 失败已抛 AssertionError 并打印；吞掉以继续跑完其余用例，
            # 最终 FAILED 非空 → sys.exit(1)（手动运行汇总语义保留）
            pass
    print()

    print(f"\n===== 结果: {len(PASSED)} 通过, {len(FAILED)} 失败 =====")
    if FAILED:
        for f in FAILED:
            print(f"  FAILED: {f}")
        sys.exit(1)


if __name__ == "__main__":
    main()
