"""OneBot v11 WebSocket 适配器（正向 / 反向双模式）。

网络 IO 运行在独立 asyncio 线程，回调游戏 API 必须经由 plugin.run_on_main。
"""

from __future__ import annotations

import asyncio
import json
import queue
import random
import threading
import uuid
from typing import Any, Callable

from .. import __version__
from ..i18n import t as _t
from ..vendor import import_websockets
from . import packets
from .message import format_message

websockets = import_websockets()

# WebSocket 握手身份标识：QQ 开放平台等网关据此显示客户端名称
# （未设置时会显示“未知 WebSocket”）
USER_AGENT = f"LumenBridge/{__version__} (Endstone)"

API_TIMEOUT = 10.0
SEND_QUEUE_SIZE = 100
# 入站事件派发队列容量：超出时丢最旧保内存（与发送队列同策略）
DISPATCH_QUEUE_SIZE = 2000
# websockets 连接状态枚举值 OPEN（IntEnum，值为 1）
_WS_STATE_OPEN = getattr(getattr(websockets, "State", None), "OPEN", 1)


class OneBotAdapter:
    """OneBot v11 WebSocket 适配器（正向 / 反向双模式）"""

    def __init__(
        self,
        logger: Any,
        event_bus: Any,
        *,
        ws_type: int = 0,
        target: str = "ws://127.0.0.1:3001",
        listen_host: str = "0.0.0.0",
        listen_port: int = 3002,
        access_token: str = "",
        bot_qq: int = 0,
        adapter_id: str = "",
        adapter_name: str = "",
        adapter_type: str = "websocket",
        groups: list[int] | None = None,
    ) -> None:
        self.logger = logger
        self.bus = event_bus
        self.ws_type = ws_type
        self.target = target
        self.listen_host = listen_host
        self.listen_port = listen_port
        self.access_token = access_token
        self.bot_qq = bot_qq
        # 多适配器元数据：id 对应 connections.json 卡片；type 为 websocket
        #（直连协议端）或 astrbot（AstrBot 插件端，协议同为 OneBot v11）
        self.adapter_id = adapter_id
        self.adapter_name = adapter_name
        self.adapter_type = adapter_type
        self.groups: list[int] = list(groups or [])
        # 由 AdapterHub 维护的配置快照，用于热重载 diff 判断是否需要重建连接
        self.config_snapshot: dict[str, Any] | None = None

        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._running = False

        self._ws: Any = None
        self._server: Any = None
        self._clients: set[Any] = set()
        # 反向模式下是否已广播 bot.online：实例级标志保证多客户端
        # 场景 online/offline 对称（最后一个客户端断开时才广播下线）
        self._announced = False
        self._connected_event: asyncio.Event | None = None
        self._send_queue: asyncio.Queue | None = None
        self._pending: dict[str, asyncio.Future] = {}
        self._sender_task: asyncio.Task | None = None
        self._main_future: Any = None
        # 入站事件派发队列 + 专用工作线程：下游处理器（正则命令执行等）
        # 可能阻塞数十秒，在 WS 事件循环内同步派发会停跳心跳（20s/10s）
        # 导致对端主动断连、发送队列积压丢包
        self._dispatch_queue: "queue.Queue[dict[str, Any] | None] | None" = None
        self._dispatch_thread: threading.Thread | None = None

    @property
    def is_connected(self) -> bool:
        if self.ws_type == 0:
            ws = self._ws
            if ws is None:
                return False
            # websockets>=13 新 asyncio API 用 state（State.OPEN==1）；
            # 旧 legacy API 没有 state 只有 closed，取不到 state 时回退
            # closed 判定，避免 legacy 实现下恒为 False
            state = getattr(ws, "state", None)
            if state is not None:
                return state == _WS_STATE_OPEN
            return not getattr(ws, "closed", True)
        return len(self._clients) > 0

    @property
    def mode_name(self) -> str:
        return _t("adapter.mode_forward") if self.ws_type == 0 else _t("adapter.mode_reverse")

    @property
    def display_name(self) -> str:
        """卡片展示名：适配器名称 + 连接模式。"""
        name = self.adapter_name or ("AstrBot" if self.adapter_type == "astrbot" else "WebSocket")
        return f"{name} ({self.mode_name})"

    def start(self) -> None:
        """启动独立事件循环线程并提交连接任务"""
        if self._running:
            return
        self._running = True
        self._dispatch_queue = queue.Queue(maxsize=DISPATCH_QUEUE_SIZE)
        self._dispatch_thread = threading.Thread(
            target=self._dispatch_worker,
            name=f"LumenBridge-Dispatch-{self.adapter_id or 'default'}",
            daemon=True,
        )
        self._dispatch_thread.start()
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run_loop,
            name=f"LumenBridge-WS-{self.adapter_id or 'default'}",
            daemon=True,
        )
        self._thread.start()
        self._main_future = asyncio.run_coroutine_threadsafe(self._main(), self._loop)

    def _dispatch_worker(self) -> None:
        """串行消费入站事件派发队列（FIFO 保序）。

        事件总线处理器链（正则命令执行最长可阻塞 command_timeout，默认
        5s 上限 60s）绝不能在 WS 事件循环线程内同步执行：心跳会停跳，
        对端按 ping_timeout 断开健康连接并触发无谓重连。

        退出条件双保险：收到哨兵包 或 _running 已置 False（stop() 中
        哨兵在队满时可能被 put_nowait 丢弃，靠 _running 轮询兜底退出）。
        """
        q = self._dispatch_queue
        if q is None:
            return
        while True:
            try:
                data = q.get(timeout=1.0)
            except queue.Empty:
                if not self._running:
                    return
                continue
            if data is None:
                return
            if not self._running:
                return
            try:
                self.bus.emit("onebot.pack", data)
            except Exception:
                self.logger.exception(_t("adapter.forward_msg_error"))

    def stop(self) -> None:
        """优雅关闭连接、取消后台任务并回收事件循环线程。"""
        self._running = False
        loop = self._loop
        if loop is None or loop.is_closed():
            return

        async def _shutdown() -> None:
            for fut in list(self._pending.values()):
                if not fut.done():
                    fut.cancel()
            self._pending.clear()
            if self._connected_event is not None:
                self._connected_event.set()
            if self._ws is not None:
                try:
                    await self._ws.close()
                except Exception:
                    pass
            for client in list(self._clients):
                try:
                    await client.close()
                except Exception:
                    pass
            self._clients.clear()
            if self._server is not None:
                try:
                    self._server.close()
                    await self._server.wait_closed()
                except Exception:
                    pass

            current = asyncio.current_task()
            tasks = [task for task in asyncio.all_tasks() if task is not current]
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

        try:
            asyncio.run_coroutine_threadsafe(_shutdown(), loop).result(timeout=5)
        except Exception:
            pass
        if loop.is_running():
            loop.call_soon_threadsafe(loop.stop())
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=5)
        # 停止派发工作线程：哨兵入队（队满时让位丢弃，正在关停无所谓）
        if self._dispatch_queue is not None:
            try:
                self._dispatch_queue.put_nowait(None)
            except queue.Full:
                pass
        if self._dispatch_thread and self._dispatch_thread is not threading.current_thread():
            self._dispatch_thread.join(timeout=2)
        self._dispatch_queue = None
        self._dispatch_thread = None
        # 先取消主协程 Future 再丢弃引用，确保任务能收到取消信号
        if self._main_future is not None:
            try:
                self._main_future.cancel()
            except Exception:
                pass
        self._loop = None
        self._thread = None
        self._main_future = None
        self._sender_task = None
        self._ws = None
        self._server = None
        # 兜底清理残留状态，防止 stop()+start() 复用同实例时旧 Future 残留
        self._pending.clear()
        self._clients.clear()
        self._announced = False
        self._connected_event = None
        self._send_queue = None
        self.logger.info(_t("adapter.stopped"))

    def _run_loop(self) -> None:
        loop = self._loop
        if loop is None:
            return
        asyncio.set_event_loop(loop)
        try:
            loop.run_forever()
        finally:
            try:
                pending = list(asyncio.all_tasks(loop))
            except RuntimeError:
                pending = []
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.close()

    async def _main(self) -> None:
        self._connected_event = asyncio.Event()
        self._send_queue = asyncio.Queue(maxsize=SEND_QUEUE_SIZE)
        self._sender_task = asyncio.create_task(
            self._sender_loop(),
            name=f"LumenBridge-OneBot-Sender-{self.adapter_id or 'default'}",
        )
        try:
            if self.ws_type == 0:
                await self._forward_loop()
            else:
                await self._reverse_serve()
        finally:
            if self._sender_task is not None and not self._sender_task.done():
                self._sender_task.cancel()
            if self._sender_task is not None:
                await asyncio.gather(self._sender_task, return_exceptions=True)
            self._sender_task = None

    async def _forward_loop(self) -> None:
        headers = {"User-Agent": USER_AGENT}
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"
        attempt = 0
        while self._running:
            try:
                self.logger.info(_t("adapter.connecting", target=self.target))
                async with websockets.connect(
                    self.target,
                    additional_headers=headers,
                    ping_interval=20,
                    ping_timeout=10,
                    close_timeout=10,
                    max_size=16 * 1024 * 1024,
                ) as ws:
                    self._ws = ws
                    self._connected_event.set()
                    attempt = 0
                    self.logger.info(_t("adapter.connected", name=self.display_name))
                    self.bus.emit("bot.online", self)
                    async for raw in ws:
                        try:
                            self._dispatch_raw(raw)
                        except Exception:
                            self.logger.exception(_t("adapter.forward_msg_error"))
            except asyncio.CancelledError:
                break
            except Exception as e:
                if self._running:
                    self.logger.warning(_t("adapter.connection_error", error=e))
            finally:
                was_online = self._ws is not None
                self._ws = None
                self._connected_event.clear()
                # 断线时通知资料卡片下线（插件侧把 connected 置回 False）；
                # 连接从未建立成功的尝试不触发，避免重复下线通知
                if was_online:
                    try:
                        self.bus.emit("bot.offline", self)
                    except Exception:
                        pass

            if not self._running:
                break
            attempt += 1
            delay = min(60.0, 2.0 ** min(attempt, 6) + random.uniform(0, 2))
            self.logger.warning(_t("adapter.disconnected", delay=delay, attempt=attempt))
            await asyncio.sleep(delay)

    async def _reverse_serve(self) -> None:
        async def handler(ws: Any) -> None:
            headers = getattr(ws, "request", None)
            headers = getattr(headers, "headers", {}) or {}
            auth = headers.get("Authorization", "")
            self_id = headers.get("X-Self-ID", "")

            if self.access_token and auth != f"Bearer {self.access_token}":
                self.logger.warning(_t("adapter.reverse_auth_failed"))
                await ws.close(code=4001, reason="unauthorized")
                return

            self._clients.add(ws)
            self._connected_event.set()
            self.logger.info(_t("adapter.reverse_client_connected", self_id=self_id))
            announced = not self.bot_qq or str(self_id) == str(self.bot_qq)
            if announced and not self._announced:
                self._announced = True
                self.bus.emit("bot.online", self)
            try:
                async for raw in ws:
                    try:
                        self._dispatch_raw(raw)
                    except Exception:
                        self.logger.exception(_t("adapter.reverse_msg_error"))
            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger.exception(_t("adapter.reverse_recv_error"))
            finally:
                self._clients.discard(ws)
                if not self._clients:
                    self._connected_event.clear()
                    # 最后一个客户端断开：通知资料卡片下线
                    # （仅此前广播过上线，保持 online/offline 对称）
                    if self._announced:
                        self._announced = False
                        try:
                            self.bus.emit("bot.offline", self)
                        except Exception:
                            pass
                self.logger.info(_t("adapter.reverse_client_disconnected"))

        while self._running:
            try:
                self._server = await websockets.serve(
                    handler, self.listen_host, self.listen_port,
                    max_size=16 * 1024 * 1024,
                )
                self.logger.info(
                    _t("adapter.reverse_started", host=self.listen_host, port=self.listen_port)
                )
                await self._server.wait_closed()
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(_t("adapter.reverse_server_error", error=e))
                await asyncio.sleep(10)
            if not self._running:
                break

    def _dispatch_raw(self, raw: Any) -> None:
        """解析收到的数据包并派发到事件总线"""
        try:
            data = json.loads(raw if isinstance(raw, str) else raw.decode())
        except (json.JSONDecodeError, UnicodeDecodeError):
            self.logger.error(_t("adapter.json_parse_error"))
            return

        # 防御：OneBot 偶尔发送非对象 JSON（数字 / 字符串 / 数组）
        if not isinstance(data, dict):
            self.logger.warning(_t("adapter.non_dict_ignored", data=data))
            return

        echo = data.get("echo")
        if echo is not None:
            echo_key = str(echo)  # 统一字符串键，与 call_api 写入侧一致
            fut = self._pending.pop(echo_key, None)
            # 仅当该 echo 仍对应等待中的 Future 才派发；fut 为 None 说明
            # 已超时/取消被 pop 或未知 echo，迟到的回执不再 emit，避免误触发
            if fut is not None:
                retcode = data.get("retcode")
                if data.get("status") == "failed" or (retcode is not None and retcode != 0):
                    # 失败回执与超时回执对调用方同为 None，这里留下
                    # retcode/wording 日志作为排障依据
                    self.logger.warning(
                        _t(
                            "adapter.api_failed",
                            retcode=retcode,
                            status=data.get("status"),
                            wording=data.get("wording") or data.get("message") or "",
                        )
                    )
                if not fut.done():
                    fut.set_result(data.get("data"))
                self.bus.emit(f"packid_{echo}", data.get("data"))

        # 注入来源适配器 id（下划线前缀 = LumenBridge 内部字段），分发器据此回查
        # 适配器实例；onebot.pack 保持单参 (pack) 以兼容子插件监听约定
        if self.adapter_id and "_lumen_adapter_id" not in data:
            data["_lumen_adapter_id"] = self.adapter_id
        self._dispatch_pack(data)

    def _dispatch_pack(self, data: dict[str, Any]) -> None:
        """把事件包交给派发工作线程（保序）；未启动时退化为同步派发。

        未 start() 的实例（测试桩直接调 _dispatch_raw）没有派发队列，
        同步派发保持旧行为。
        """
        q = self._dispatch_queue
        if q is None:
            self.bus.emit("onebot.pack", data)
            return
        try:
            q.put_nowait(data)
        except queue.Full:
            # 派发积压超限：丢最旧保最新（与发送队列同策略），留日志排查
            try:
                q.get_nowait()
                q.put_nowait(data)
            except (queue.Empty, queue.Full):
                pass
            self.logger.warning(_t("adapter.dispatch_queue_dropped"))

    def _drop_pack(self, dropped: Any) -> None:
        """丢弃一个待发包：带 echo 的请求立即以 None 完成 Future（避免调用方等到超时），
        并记录被丢包的 action 与关键目标字段，便于排查消息丢失。"""
        echo = dropped.get("echo") if isinstance(dropped, dict) else None
        if echo:
            fut = self._pending.pop(str(echo), None)
            if fut is not None and not fut.done():
                fut.set_result(None)
        params = dropped.get("params") if isinstance(dropped, dict) else None
        action = str(dropped.get("action") or "?") if isinstance(dropped, dict) else "?"
        target = "-"
        if isinstance(params, dict):
            if params.get("group_id") is not None:
                target = f"group_id={params.get('group_id')}"
            elif params.get("user_id") is not None:
                target = f"user_id={params.get('user_id')}"
        self.logger.warning(
            _t("adapter.send_queue_dropped_detail", action=action, target=target)
        )

    def _evict_oldest(self) -> None:
        """丢弃队首最旧的待发包以腾出空间。"""
        q = self._send_queue
        if q is None or q.empty():
            return
        try:
            dropped = q.get_nowait()
        except asyncio.QueueEmpty:
            return
        self._drop_pack(dropped)

    def _requeue_head(self, pack: dict[str, Any]) -> None:
        """将发送失败的包重新压回队首，保持消息顺序。

        在同一队列对象上 drain → 重灌：本方法为同步代码块（无 await 点），
        在事件循环线程内执行时具有原子性。**不替换 self._send_queue 引用**——
        替换队列对象会与 send_pack / call_api 中已捕获队列引用的入队协程
        并发交错，导致新包被写入孤儿队列而静默丢失。
        """
        q = self._send_queue
        if q is None:
            return
        try:
            items: list[Any] = []
            while True:
                try:
                    items.append(q.get_nowait())
                except asyncio.QueueEmpty:
                    break
            maxsize = q.maxsize or 0
            if maxsize and items and len(items) + 1 > maxsize:
                # 重灌后超容量：丢弃最旧包并完成其 echo Future
                self._drop_pack(items.pop(0))
            q.put_nowait(pack)
            for it in items:
                q.put_nowait(it)
        except Exception:
            # 重灌失败会丢失 pack 与 drain 出的 items，必须留日志供排查
            self.logger.exception("OneBot send queue requeue failed")

    async def _sender_loop(self) -> None:
        """发送队列常驻协程：断线时挂起，恢复后补发"""
        while self._running:
            try:
                await self._connected_event.wait()
                pack = await asyncio.wait_for(self._send_queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            except (asyncio.CancelledError, RuntimeError, GeneratorExit):
                return
            # 序列化失败（不可序列化对象 / 循环引用）时丢弃该包并记录：
            # 此类毒包若重入队会永久阻塞发送队列，且异常外泄会终止常驻发送协程
            try:
                text_pack = json.dumps(pack, ensure_ascii=False)
            except (TypeError, ValueError) as exc:
                self.logger.error(_t("adapter.send_serialize_failed", error=exc))
                continue
            try:
                if self.ws_type == 0 and self._ws is not None:
                    await self._ws.send(text_pack)
                elif self.ws_type == 1 and self._clients:
                    # 对每个反向客户端独立 try：单客户端故障不影响其他客户端；
                    # 跟踪 sent_any，全部客户端失败时抛错触发外层重入队，
                    # 避免消息静默丢失
                    sent_any = False
                    for client in list(self._clients):
                        try:
                            await client.send(text_pack)
                            sent_any = True
                        except Exception:
                            self.logger.warning(_t("adapter.reverse_send_failed"))
                    if not sent_any:
                        raise ConnectionError(_t("adapter.connection_unavailable"))
                else:
                    raise ConnectionError(_t("adapter.connection_unavailable"))
            except asyncio.CancelledError:
                # 取消前重新入队，避免未发送消息丢失
                self._requeue_head(pack)
                raise
            except Exception:
                self._requeue_head(pack)
                await asyncio.sleep(2.0)

    def send_pack(self, pack: dict[str, Any]) -> None:
        """线程安全的数据包发送入口（外部线程可直接调用）"""
        loop = self._loop
        if not self._running or loop is None or loop.is_closed():
            return

        async def _enqueue() -> None:
            # 绑定局部队列引用：full 检查与 put 之间即使 _requeue_head 重排队列，
            # 也始终作用于同一实例（重排不替换对象），不会把包写入孤儿队列
            q = self._send_queue
            if q is None:
                return
            if q.full():
                self._evict_oldest()
            await q.put(pack)

        try:
            asyncio.run_coroutine_threadsafe(_enqueue(), loop)
        except RuntimeError:
            pass

    def call_api(
        self,
        pack: dict[str, Any],
        callback: Callable[[Any], None] | None = None,
        timeout: float = API_TIMEOUT,
    ) -> None:
        """发送带 echo 的 API 请求；回执到达后在 WS 线程回调 callback(data)"""
        loop = self._loop
        if not self._running or loop is None or loop.is_closed():
            if callback:
                callback(None)
            return
        # 无 callback 时不生成 echo，避免 Future 残留在 _pending 中造成内存泄漏
        if callback is None:
            self.send_pack(pack)
            return
        # 始终生成新 echo 并浅拷贝 pack：复用外部传入的 echo 会覆盖 _pending
        # 中已有的同 echo Future（echo 冲突），且不原地改写调用方字典
        echo = uuid.uuid4().hex
        pack = {**pack, "echo": echo}

        async def _request() -> None:
            q = self._send_queue
            if q is None:
                callback(None)
                return
            fut: asyncio.Future = loop.create_future()
            self._pending[echo] = fut
            if q.full():
                self._evict_oldest()
            await q.put(pack)
            try:
                data = await asyncio.wait_for(fut, timeout=timeout)
                try:
                    callback(data)
                except Exception:
                    # 回调异常不能逃逸到事件循环：记录后吞掉，避免
                    # "exception was never retrieved" 且中断发送协程
                    self.logger.exception("OneBot API callback error")
            except asyncio.TimeoutError:
                self._pending.pop(echo, None)
                self.logger.warning(_t("adapter.api_timeout", action=pack.get('action')))
                try:
                    callback(None)
                except Exception:
                    self.logger.exception("OneBot API callback error")
            except asyncio.CancelledError:
                self._pending.pop(echo, None)
                # stop() 取消 pending Future 时回调 None，让调用方立即感知失败
                try:
                    callback(None)
                except Exception:
                    pass
                raise

        try:
            asyncio.run_coroutine_threadsafe(_request(), loop)
        except RuntimeError:
            if callback:
                callback(None)

    def send_group_msg(self, group_id: int, message: Any) -> None:
        self.send_pack(packets.group_message(group_id, format_message(message)))

    def send_private_msg(self, user_id: int, message: Any) -> None:
        self.send_pack(packets.private_message(user_id, format_message(message)))

    def send_group_forward_msg(self, group_id: int, messages: Any) -> None:
        self.send_pack(packets.group_forward_message(group_id, messages))

    def delete_msg(self, message_id: int) -> None:
        self.send_pack(packets.delete_message(message_id))

    def set_group_ban(self, group_id: int, user_id: int, duration: int) -> None:
        self.send_pack(packets.group_ban(group_id, user_id, duration))

    def set_group_whole_ban(self, group_id: int, enable: bool) -> None:
        self.send_pack(packets.group_whole_ban(group_id, enable))

    def set_group_kick(self, group_id: int, user_id: int, reject: bool = False) -> None:
        self.send_pack(packets.group_kick(group_id, user_id, reject))

    def set_group_leave(self, group_id: int, dismiss: bool = False) -> None:
        self.send_pack(packets.group_leave(group_id, dismiss))

    def set_group_name(self, group_id: int, name: str) -> None:
        self.send_pack(packets.group_name(group_id, name))

    def set_group_card(self, group_id: int, user_id: int, card: str) -> None:
        self.send_pack(packets.group_card(group_id, user_id, card))

    def set_group_add_request(self, flag: str, sub_type: str, approve: bool, reason: str = "") -> None:
        self.send_pack(packets.group_add_request(flag, sub_type, approve, reason))

    def set_friend_add_request(self, flag: str, approve: bool) -> None:
        self.send_pack(packets.friend_add_request(flag, approve))

    def send_like(self, user_id: int, times: int = 1) -> None:
        self.send_pack(packets.send_like(user_id, times))

    def get_group_member_list(self, group_id: int, callback: Callable[[Any], None]) -> None:
        self.call_api(packets.group_member_list(group_id), callback)

    def get_group_member_info(self, group_id: int, user_id: int, callback: Callable[[Any], None]) -> None:
        self.call_api(packets.group_member_info(group_id, user_id), callback)

    def get_stranger_info(self, user_id: int, callback: Callable[[Any], None]) -> None:
        self.call_api(packets.stranger_info(user_id), callback)

    def get_msg(self, message_id: int, callback: Callable[[Any], None]) -> None:
        self.call_api(packets.get_message(message_id), callback)

    def get_login_info(self, callback: Callable[[Any], None]) -> None:
        self.call_api(packets.login_info(), callback)

    def upload_group_file(self, group_id: int, file: str, name: str, folder_id: str | None = None) -> None:
        self.send_pack(packets.upload_group_file(group_id, file, name, folder_id))

    def get_group_root_files(self, group_id: int, callback: Callable[[Any], None]) -> None:
        self.call_api(packets.group_root_files(group_id), callback)

    def send_private_forward_msg(self, user_id: int, messages: Any) -> None:
        self.send_pack(packets.private_forward_message(user_id, messages))

    def get_forward_msg(self, message_id: str, callback: Callable[[Any], None]) -> None:
        self.call_api(packets.forward_message(message_id), callback)

    def mark_msg_as_read(self, message_id: int) -> None:
        self.send_pack(packets.mark_msg_as_read(message_id))

    def set_group_admin(self, group_id: int, user_id: int, enable: bool) -> None:
        self.send_pack(packets.group_admin(group_id, user_id, enable))

    def set_group_special_title(self, group_id: int, user_id: int, title: str, duration: int = -1) -> None:
        self.send_pack(packets.group_special_title(group_id, user_id, title, duration))

    def set_group_anonymous_ban(self, group_id: int, anonymous_flag: str, duration: int) -> None:
        self.send_pack(packets.group_anonymous_ban(group_id, anonymous_flag, duration))

    def send_group_sign(self, group_id: int) -> None:
        self.send_pack(packets.group_sign(group_id))

    def group_poke(self, group_id: int, user_id: int) -> None:
        self.send_pack(packets.group_poke(group_id, user_id))

    def friend_poke(self, user_id: int) -> None:
        self.send_pack(packets.friend_poke(user_id))

    def set_essence_msg(self, message_id: int) -> None:
        self.send_pack(packets.essence_msg(message_id))

    def delete_essence_msg(self, message_id: int) -> None:
        self.send_pack(packets.delete_essence_msg(message_id))

    def set_msg_emoji_like(self, message_id: int, emoji_id: str) -> None:
        self.send_pack(packets.set_msg_emoji_like(message_id, emoji_id))

    def get_group_info(self, group_id: int, callback: Callable[[Any], None], no_cache: bool = False) -> None:
        self.call_api(packets.group_info(group_id, no_cache), callback)

    def get_group_list(self, callback: Callable[[Any], None]) -> None:
        self.call_api(packets.group_list(), callback)

    def get_friend_list(self, callback: Callable[[Any], None]) -> None:
        self.call_api(packets.friend_list(), callback)

    def get_group_honor_info(self, group_id: int, callback: Callable[[Any], None], honor_type: str = "all") -> None:
        self.call_api(packets.group_honor_info(group_id, honor_type), callback)

    def get_version_info(self, callback: Callable[[Any], None]) -> None:
        self.call_api(packets.version_info(), callback)

    def get_status(self, callback: Callable[[Any], None]) -> None:
        self.call_api(packets.status_info(), callback)

    def get_image(self, file: str, callback: Callable[[Any], None]) -> None:
        self.call_api(packets.image_info(file), callback)

    def get_record(self, file: str, callback: Callable[[Any], None], out_format: str = "mp3") -> None:
        self.call_api(packets.record_info(file, out_format), callback)

    def get_group_files_by_folder(
        self, group_id: int, folder_id: str, callback: Callable[[Any], None]
    ) -> None:
        self.call_api(packets.group_files_by_folder(group_id, folder_id), callback)

    def get_group_file_url(self, group_id: int, file_id: str, callback: Callable[[Any], None]) -> None:
        self.call_api(packets.group_file_url(group_id, file_id), callback)

    def upload_private_file(self, user_id: int, file: str, name: str) -> None:
        self.send_pack(packets.upload_private_file(user_id, file, name))

    def delete_group_file(self, group_id: int, file_id: str) -> None:
        self.send_pack(packets.delete_group_file(group_id, file_id))

    def call_action(
        self,
        action: str,
        params: dict[str, Any] | None = None,
        callback: Callable[[Any], None] | None = None,
        timeout: float = API_TIMEOUT,
    ) -> None:
        """通用 OneBot action 调用入口：任意动作名 + 参数字典，便于直接调用扩展 API。"""
        pack = packets.build(action, params)
        if callback is not None:
            self.call_api(pack, callback, timeout)
        else:
            self.send_pack(pack)
