"""QQ 官方机器人适配器（WebSocket 网关模式）—— 组合入口。

对接 QQ 开放平台机器人 API（api.bot.qq.com）。本文件保留适配器本体：
网关会话（Hello/Identify/Resume/心跳）、鉴权（access_token）、REST 通道、
生命周期与 OneBot 兼容接口；其余职责拆分至 ``qqofficial`` 子包
（constants 协议常量 / utils 工具 / credentials 凭据池 / translate 事件
翻译 / sender 发送）。群标识为 group_openid（字符串），事件包携带
``domain="official"``（频道为 "guild"）与个人号域区分。零外部依赖：REST
走 urllib（asyncio.to_thread），WS 走内嵌 websockets；事件包转换为
OneBot v11 格式后经 ``bus.emit("onebot.pack", ...)`` 派发复用下游。
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import queue
import random
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from ..i18n import t as _t
from ..vendor import import_websockets
from .adapter import USER_AGENT, _WS_STATE_OPEN
from .qqofficial.constants import (
    API_DOMAIN,
    AUTH_FAIL_CODES,
    DEFAULT_CONNECT_INTERVAL,
    DEFAULT_INTENTS,
    HTTP_TIMEOUT,
    INTENT_FALLBACK_THRESHOLD,
    INTENT_GROUP_MEMBER,
    INTERACTION_ACK_DELAY,
    INTERACTION_ACK_MAX,
    OFFICIAL_BATCH_OP_LIMIT,
    OFFICIAL_BLACKLIST_PAGE_LIMIT,
    OFFICIAL_MEMBER_PAGE_LIMIT,
    OP_DISPATCH,
    OP_HEARTBEAT,
    OP_HEARTBEAT_ACK,
    OP_HELLO,
    OP_IDENTIFY,
    OP_INVALID_SESSION,
    OP_RECONNECT,
    OP_RESUME,
    RECONNECT_BASE_DELAY,
    RECONNECT_MAX_DELAY,
    SANDBOX_DOMAIN,
    SEND_RETRY_DELAY_MEDIA,
    SEND_RETRY_DELAY_TEXT,
    SEND_RETRY_MAX,
    SESSION_ENDED,
    SESSION_INVALID,
    SESSION_RECONNECT,
    SESSION_RESET_CODES,
    TOKEN_URL,
)
from .qqofficial.credentials import CredentialsStore
from .qqofficial.sender import MessageSender
from .qqofficial.translate import EventTranslator
from .qqofficial.utils import ApiHTTPError

websockets = import_websockets()


class QQOfficialAdapter:
    """QQ 官方机器人 WebSocket 适配器（群聊 + C2C + 频道）"""

    def __init__(
        self,
        logger: Any,
        event_bus: Any,
        *,
        app_id: str = "",
        app_secret: str = "",
        sandbox: bool = False,
        adapter_id: str = "",
        adapter_name: str = "",
        bot_qq: int = 0,
        groups: list[str] | None = None,
        connect_interval: int = DEFAULT_CONNECT_INTERVAL,
        extra_intents: int = 0,
        suppress_connection_log: bool = True,
    ) -> None:
        self.logger = logger
        self.bus = event_bus
        self.app_id = str(app_id or "").strip()
        self.app_secret = str(app_secret or "").strip()
        self.sandbox = bool(sandbox)
        # 后台静默日志开关：抑制连接/重连等运行类日志（防刷屏），异常日志不受影响
        self.suppress_connection_log = bool(suppress_connection_log)
        # 连接间隔（毫秒）：两次连接尝试的最小间隔；0 表示仅按指数退避
        try:
            self.connect_interval = max(0, int(connect_interval))
        except (TypeError, ValueError):
            self.connect_interval = DEFAULT_CONNECT_INTERVAL
        # 附加事件订阅位（叠加默认 Intents 之外的事件）；非法值回落 0
        try:
            self.extra_intents = max(0, int(extra_intents))
        except (TypeError, ValueError):
            self.extra_intents = 0
        # 机器人 QQ 号：官方域 AppID 不是 QQ 号，昵称 / 头像需单独配置 QQ 号
        self.bot_qq = int(bot_qq or 0)
        self.adapter_id = adapter_id
        self.adapter_name = adapter_name
        self.adapter_type = "qqofficial"
        # 群标识列表：QQ 官方为 group_openid 字符串
        self.groups: list[str] = [str(g) for g in (groups or [])]
        # 动态发现的群 openid（官方无群列表 API）；groups 为空时广播发往这些群
        self._discovered_groups: dict[str, float] = {}
        self.config_snapshot: dict[str, Any] | None = None

        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._running = False
        self._main_future: Any = None

        # 派发队列：总线处理器链可能长时间阻塞，不能在 WS 事件循环线程内
        # 同步执行（心跳停跳会触发重连风暴）
        self._dispatch_queue: queue.Queue | None = None
        self._dispatch_thread: threading.Thread | None = None

        self._ws: Any = None
        self._heartbeat_task: asyncio.Task | None = None

        # 网关会话状态（跨重连保留用于 Resume）
        self._session_id: str = ""
        self._last_seq: int = 0
        # 最近一次心跳 ACK（op 11）到达时间：心跳看门狗判定半开连接用
        self._last_ack_at: float = 0.0
        self._access_token: str = ""
        self._token_expires: float = 0.0
        # asyncio.Lock 需绑定运行中的事件循环，延迟创建
        self._token_lock: asyncio.Lock | None = None
        # READY 的机器人资料（供 get_login_info 使用）
        self._bot_user: dict[str, Any] = {}

        # 入群申请缓存 flag → (group_openid, member_openid)：OneBot 只回传 flag，
        # 官方审批接口需三者齐备；超出上限丢最旧
        self._join_requests: dict[str, tuple[str, str]] = {}
        # 已回应的互动事件 id（官方同一 interaction_id 仅可回应一次）；
        # 超上限丢最旧防无限增长
        self._acked_interactions: dict[str, float] = {}
        self._interaction_lock = threading.Lock()
        # 消息 id → (kind, target)：撤回接口需 group_openid 反查（OneBot delete_msg 只带 message_id）
        self._msg_scopes: dict[str, tuple[str, str]] = {}
        # GROUP_MEMBER intent 自适应降级：官方文档位号两版不一（1<<24 / 1<<25），
        # 无权限被拒连达阈值后自动摘除该位
        self._group_member_intent = True
        self._identify_failures = 0

        # 子模块组合（translate / sender 经 self.ad 引用回适配器）
        self.credentials = CredentialsStore()
        self.translator = EventTranslator(self)
        self.sender = MessageSender(self)

    @property
    def is_connected(self) -> bool:
        ws = self._ws
        if ws is None:
            return False
        # websockets 新旧 API 双兼容（详细原因见 _heartbeat_loop）
        state = getattr(ws, "state", None)
        if state is not None:
            return state == _WS_STATE_OPEN
        return not getattr(ws, "closed", True)

    @property
    def mode_name(self) -> str:
        # 模板 "[QQ官方] 适配器 {name}" 必须传入 name，否则占位符原样输出
        return _t("qqofficial.mode", name=self.adapter_name or "QQ 官方机器人")

    @property
    def display_name(self) -> str:
        # mode_name 已含适配器名，无需再按 "{name} ({mode})" 拼接
        return self.mode_name

    def _retry_params(self) -> tuple[int, float, float]:
        """发送重试参数 (max, 文本间隔, 富媒体间隔)；读本模块命名空间支持测试 monkeypatch。"""
        return SEND_RETRY_MAX, SEND_RETRY_DELAY_TEXT, SEND_RETRY_DELAY_MEDIA

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._dispatch_queue = queue.Queue(maxsize=2000)
        self._dispatch_thread = threading.Thread(
            target=self._dispatch_worker,
            name=f"LumenBridge-QQOfficial-Dispatch-{self.adapter_id or 'default'}",
            daemon=True,
        )
        self._dispatch_thread.start()
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run_loop,
            name=f"LumenBridge-QQOfficial-{self.adapter_id or 'default'}",
            daemon=True,
        )
        self._thread.start()
        self._main_future = asyncio.run_coroutine_threadsafe(self._main(), self._loop)

    def stop(self) -> None:
        self._running = False
        loop = self._loop
        if loop is None or loop.is_closed():
            return

        async def _shutdown() -> None:
            if self._ws is not None:
                try:
                    await self._ws.close()
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
            loop.call_soon_threadsafe(loop.stop)
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=5)
        self._loop = None
        self._thread = None
        # 先取消主协程 Future 再丢弃引用，确保任务能收到取消信号
        if self._main_future is not None:
            try:
                self._main_future.cancel()
            except Exception:
                pass
        self._main_future = None
        self._heartbeat_task = None
        self._ws = None
        # 停止派发工作线程：哨兵入队（队满时靠 worker 的 _running 轮询兜底退出）
        if self._dispatch_queue is not None:
            try:
                self._dispatch_queue.put_nowait(None)
            except queue.Full:
                pass
        if self._dispatch_thread and self._dispatch_thread is not threading.current_thread():
            self._dispatch_thread.join(timeout=2)
        self._dispatch_queue = None
        self._dispatch_thread = None
        self.sender.queue = None
        self.sender.task = None
        # 锁可能绑定旧事件循环，重置以便下次 start() 在新循环内重建
        self._token_lock = None
        self.logger.info(_t("adapter.stopped"))

    def _emit_pack(self, pack: dict[str, Any]) -> None:
        """入站事件包入派发队列（WS 线程调用必须非阻塞）；队满丢最旧防阻塞事件循环。"""
        q = self._dispatch_queue
        if q is None:
            # stop() 后残留调用直接透传，不丢事件
            try:
                self.bus.emit("onebot.pack", pack)
            except Exception:
                pass
            return
        try:
            q.put_nowait(pack)
        except queue.Full:
            try:
                q.get_nowait()
            except queue.Empty:
                pass
            try:
                q.put_nowait(pack)
            except queue.Full:
                pass

    def _dispatch_worker(self) -> None:
        """串行消费派发队列（FIFO 保序）；哨兵或 _running=False 退出（队满时哨兵可能被丢弃）。"""
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
        if self._token_lock is None:
            self._token_lock = asyncio.Lock()
        self.sender.start()
        attempt = 0
        while self._running:
            session_started = time.monotonic()
            was_fresh_identify = not self._session_id
            ready_before = bool(self._session_id)  # 会话已存在视为曾 READY
            try:
                await self._gateway_session()
            except asyncio.CancelledError:
                break
            except Exception as e:
                if self._running and not self.suppress_connection_log:
                    self.logger.warning(_t("qqofficial.gateway_error", error=e))
            finally:
                was_online = self._ws is not None
                self._ws = None
                if self._heartbeat_task is not None and not self._heartbeat_task.done():
                    self._heartbeat_task.cancel()
                self._heartbeat_task = None
                # 会话中断时通知资料卡片下线（插件侧把 connected 置回 False）
                if was_online and self.bus is not None:
                    try:
                        self.bus.emit("bot.offline", self)
                    except Exception:
                        pass
            # 全新 Identify 未 READY 即断（WS 已建立）计一次失败，达阈值摘除
            # GROUP_MEMBER 位；网关地址阶段的网络错误与权限无关，不计入
            if self._running and was_fresh_identify and not ready_before and was_online:
                ready_now = bool(self._session_id)
                if ready_now:
                    self._identify_failures = 0
                else:
                    self._identify_failures += 1
                    if (
                        self._group_member_intent
                        and self._identify_failures >= INTENT_FALLBACK_THRESHOLD
                    ):
                        self._group_member_intent = False
                        self.logger.warning(
                            _t("qqofficial.group_member_intent_fallback", bit=24)
                        )
            if not self._running:
                break
            # 会话存活超 60s 后断开视为新故障，重置退避（避免例行重连拉长间隔）
            if time.monotonic() - session_started >= 60.0:
                attempt = 0
            attempt += 1
            backoff = min(
                RECONNECT_MAX_DELAY, RECONNECT_BASE_DELAY ** min(attempt, 5) + random.uniform(0, 2)
            )
            if self._session_id:
                # Resume 快速重连：会话 TTL 很短，不受 connect_interval 下限约束
                delay = min(backoff, 5.0)
            else:
                # 全新 Identify：connect_interval 为重连下限，防高频建连
                configured = self.connect_interval / 1000.0
                delay = max(backoff, configured) if configured > 0 else backoff
            if not self.suppress_connection_log:
                self.logger.warning(_t("adapter.disconnected", delay=delay, attempt=attempt))
            await asyncio.sleep(delay)

    def _fetch_token_sync(self) -> tuple[str, int]:
        """同步获取 access_token（在线程池中执行）。"""
        body = json.dumps({"appId": self.app_id, "clientSecret": self.app_secret}).encode()
        req = urllib.request.Request(
            TOKEN_URL,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        token = str(data.get("access_token") or "")
        expires = int(data.get("expires_in") or 0)
        if not token or expires <= 0:
            raise RuntimeError(_t("qqofficial.token_invalid_resp"))
        return token, expires

    async def _access_token_async(self, *, force: bool = False) -> str:
        """获取（必要时刷新）access_token；force 用于 4004 后强制重取。"""
        if self._token_lock is None:
            self._token_lock = asyncio.Lock()
        async with self._token_lock:
            now = time.time()
            if not force and self._access_token and now < self._token_expires - 60:
                return self._access_token
            token, expires = await asyncio.to_thread(self._fetch_token_sync)
            self._access_token = token
            # 提前 2 分钟过期，避免边界失效
            self._token_expires = now + max(60, expires - 120)
            # token 例行刷新属运行类日志，静默模式下不打印
            if not self.suppress_connection_log:
                self.logger.debug(_t("qqofficial.token_refreshed", seconds=expires))
            return self._access_token

    def _api_domain(self) -> str:
        return SANDBOX_DOMAIN if self.sandbox else API_DOMAIN

    def _api_request_sync(self, method: str, path: str, body: dict[str, Any] | None, token: str) -> Any:
        """同步 REST 调用（在线程池中执行）。"""
        url = f"https://{self._api_domain()}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            url,
            data=data,
            headers={
                "Authorization": f"QQBot {token}",
                "X-Union-Appid": self.app_id,
                "Content-Type": "application/json",
                "User-Agent": USER_AGENT,
            },
            method=method,
        )
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "ignore")[:200]
            except Exception:
                pass
            raise ApiHTTPError(e.code, detail) from e

    async def _api_request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        token = await self._access_token_async()
        try:
            return await asyncio.to_thread(self._api_request_sync, method, path, body, token)
        except ApiHTTPError as e:
            # token 失效（HTTP 401）时强制刷新重试一次；按 code 精确判断
            if e.code == 401:
                token = await self._access_token_async(force=True)
                return await asyncio.to_thread(self._api_request_sync, method, path, body, token)
            raise

    async def _ws_url(self) -> str:
        data = await self._api_request("GET", "/gateway/bot")
        url = str((data or {}).get("url") or "")
        if not url:
            raise RuntimeError(_t("qqofficial.gateway_url_empty"))
        return url

    async def _gateway_session(self) -> str:
        """建立一次网关会话；返回会话结束原因（SESSION_* 常量）。"""
        url = await self._ws_url()
        if not self.suppress_connection_log:
            self.logger.info(_t("qqofficial.connecting", app_id=self.app_id))
        async with websockets.connect(
            url,
            additional_headers={"User-Agent": USER_AGENT},
            ping_interval=20,
            ping_timeout=10,
            close_timeout=10,
            max_size=16 * 1024 * 1024,
        ) as ws:
            self._ws = ws
            if not self.suppress_connection_log:
                self.logger.info(_t("qqofficial.connected", name=self.display_name))
            async for raw in ws:
                if not self._running:
                    break
                try:
                    reason = await self._on_gateway_message(raw)
                    if reason:
                        return reason
                except Exception:
                    self.logger.exception(_t("qqofficial.dispatch_error"))
            # 按关闭码判断会话失效：失效则重置状态走全新 Identify，避免无限 Resume
            if self._running:
                code = getattr(ws, "close_code", None)
                if isinstance(code, int) and (
                    code in SESSION_RESET_CODES or code in AUTH_FAIL_CODES
                ):
                    if not self.suppress_connection_log:
                        self.logger.warning(_t("qqofficial.session_reset_code", code=code))
                    self._session_id = ""
                    self._last_seq = 0
        return SESSION_ENDED

    async def _on_gateway_message(self, raw: Any) -> str | None:
        """处理单条网关消息；返回非 None 的原因表示需结束当前会话（触发外层重连）。"""
        try:
            msg = json.loads(raw if isinstance(raw, str) else raw.decode())
        except (json.JSONDecodeError, UnicodeDecodeError):
            self.logger.error(_t("adapter.json_parse_error"))
            return None
        if not isinstance(msg, dict):
            return None

        op = msg.get("op")
        if op == OP_HELLO:
            interval = float((msg.get("d") or {}).get("heartbeat_interval") or 30000) / 1000.0
            # 重复 HELLO 到达时先取消旧心跳任务，避免多个心跳并发发送
            if self._heartbeat_task is not None and not self._heartbeat_task.done():
                self._heartbeat_task.cancel()
            # 新连接的 ACK 计时基准：从 HELLO 起算
            self._last_ack_at = time.monotonic()
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop(max(10.0, interval)))
            await self._identify_or_resume()
            return None
        if op == OP_HEARTBEAT_ACK:
            self._last_ack_at = time.monotonic()
            return None
        if op == OP_RECONNECT:
            if not self.suppress_connection_log:
                self.logger.warning(_t("qqofficial.server_reconnect"))
            return SESSION_RECONNECT
        if op == OP_INVALID_SESSION:
            # 会话无效：重置后走全新 Identify
            if not self.suppress_connection_log:
                self.logger.warning(_t("qqofficial.invalid_session"))
            self._session_id = ""
            self._last_seq = 0
            await asyncio.sleep(3.0)
            return SESSION_INVALID
        if op == OP_DISPATCH:
            seq = msg.get("s")
            if isinstance(seq, int) and seq > 0 and seq <= self._last_seq:
                # Resume 重放去重：序号 <= 已处理最大序号的是断线前的重放，丢弃
                return None
            await self.translator.on_dispatch(msg)
            # 派发成功后才推进序号：先推进则派发异常时该事件被去重永久丢失
            if isinstance(seq, int) and seq > 0:
                self._last_seq = seq
            return None
        return None

    async def _identify_or_resume(self) -> None:
        token = await self._access_token_async()
        if self._session_id:
            payload = {
                "op": OP_RESUME,
                "d": {
                    "token": f"QQBot {token}",
                    "session_id": self._session_id,
                    "seq": self._last_seq,
                },
            }
        else:
            intents = DEFAULT_INTENTS | self.extra_intents
            if not self._group_member_intent:
                intents &= ~INTENT_GROUP_MEMBER
            payload = {
                "op": OP_IDENTIFY,
                "d": {
                    "token": f"QQBot {token}",
                    "intents": intents,
                    "shard": [0, 1],
                    "properties": {"$platform": "LumenBridge"},
                },
            }
        await self._ws.send(json.dumps(payload))

    async def _heartbeat_loop(self, interval: float) -> None:
        try:
            while True:
                # 官方文档：首次连接未收到事件前心跳 d 传 null，此后携带最新 s
                payload = {"op": OP_HEARTBEAT, "d": self._last_seq or None}
                # websockets>=13 用 state 判断连接，旧 API 才有 closed；单用
                # closed 在新版恒为 True，心跳会立即退出被网关踢掉
                if self._ws is None or not self.is_connected:
                    return
                # ACK 看门狗：超 2 个周期未收到 op 11 视为半开连接，主动断开触发 Resume
                if time.monotonic() - self._last_ack_at > interval * 2 + 10:
                    if not self.suppress_connection_log:
                        self.logger.warning(_t("qqofficial.heartbeat_stale"))
                    try:
                        await self._ws.close()
                    except Exception:
                        pass
                    return
                await self._ws.send(json.dumps(payload))
                # 按 80% 周期发送，为事件循环偶发阻塞留余量
                await asyncio.sleep(interval * 0.8)
        except asyncio.CancelledError:
            return
        except Exception:
            return

    def on_ready(self, data: dict[str, Any]) -> None:
        """网关 READY：记录会话 id 与机器人资料，通知上线（translator 回调）。"""
        self._session_id = str(data.get("session_id") or "")
        user = data.get("user") or {}
        if isinstance(user, dict):
            self._bot_user = {
                "qq": 0,
                "nickname": str(user.get("username") or ""),
            }
        # OneBot v11 对齐：连接建立后上报 lifecycle.connect 元事件
        self._emit_pack(
            {
                "self_id": self.app_id,
                "time": int(time.time()),
                "post_type": "meta_event",
                "meta_event_type": "lifecycle",
                "sub_type": "connect",
                "domain": "official",
                "_lumen_adapter_id": self.adapter_id,
            }
        )
        self.bus.emit("bot.online", self)
        # 登录成功属连接类运行日志，静默模式下不打印
        if not self.suppress_connection_log:
            self.logger.info(
                _t("qqofficial.ready", name=self._bot_user.get("nickname") or self.app_id)
            )

    def send_group_msg(self, group_id: Any, message: Any) -> None:
        self.sender.enqueue("group", group_id, message)

    def send_private_msg(self, user_id: Any, message: Any) -> None:
        self.sender.enqueue("private", user_id, message)

    def remember_group(self, group_openid: Any) -> None:
        """记录动态发现的群 openid：官方无群列表 API，广播目标从流入事件学习。"""
        key = str(group_openid or "").strip()
        if not key:
            return
        is_new = key not in self._discovered_groups
        self._discovered_groups[key] = time.time()
        if len(self._discovered_groups) > 64:
            # 淘汰最久未活跃的一半
            for old in sorted(self._discovered_groups, key=self._discovered_groups.get)[:32]:
                self._discovered_groups.pop(old, None)
        if is_new and key not in self.groups:
            self.logger.info(
                _t("qqofficial.group_discovered", group=key, name=self.display_name)
            )

    def forget_group(self, group_openid: Any) -> None:
        """机器人被移出群：清理动态发现记录与该群全部回复凭据。

        移群后被动凭据全部失效，不清理则窗口内每条消息都要先经历必败
        重试；配置的 groups 属用户配置不清理（重新入群时会重新学习）。
        """
        key = str(group_openid or "").strip()
        if not key:
            return
        self._discovered_groups.pop(key, None)
        self.credentials.purge_target(key)

    def broadcast_groups(self) -> list[str]:
        """广播目标群列表：配置的 groups 优先，未配置时用动态发现的群（皆空返回空列表）。"""
        if self.groups:
            return list(self.groups)
        return list(self._discovered_groups)

    def remember_join_request(self, flag: str, group_openid: str, member_openid: str) -> None:
        """记录入群申请映射（flag → group/member openid），供审批回传。"""
        if not flag or not group_openid or not member_openid:
            return
        if len(self._join_requests) >= 512:
            # 丢最旧的 64 条（dict 保插入序）
            for key in list(self._join_requests.keys())[:64]:
                self._join_requests.pop(key, None)
        self._join_requests[flag] = (group_openid, member_openid)

    def remember_msg_scope(self, message_id: Any, kind: str, target: str) -> None:
        """记录消息 id → (kind, target) 映射，供 delete_msg 反查群/私聊。"""
        key = str(message_id or "")
        if not key or not target:
            return
        if len(self._msg_scopes) >= 1024:
            for old in list(self._msg_scopes.keys())[:128]:
                self._msg_scopes.pop(old, None)
        self._msg_scopes[key] = (kind, str(target))

    def respond_interaction(
        self,
        interaction_id: Any,
        code: int = 0,
        callback: Callable[[Any], None] | None = None,
    ) -> bool:
        """回应互动事件（按钮/快捷菜单点击）：PUT /interactions/{id}。

        官方要求 type=11/12 的点击必须回应，否则客户端一直 loading 到超时；
        同一 interaction_id 仅可回应一次（重复返回 False）。code：0 成功 /
        1 操作失败 / 2 操作频繁 / 3 重复操作 / 4 没有权限 / 5 仅管理员操作。
        子插件经 call_action("respond_interaction", ...) 调用抢占兜底回应。
        """
        # 官方历史载荷 id 可能带 "INTERACTION_CREATE:" 前缀，PUT 接口要求纯 ID
        iid = str(interaction_id or "").strip().removeprefix("INTERACTION_CREATE:")
        if not iid:
            if callback:
                callback({"ok": False, "error": "empty interaction_id"})
            return False
        with self._interaction_lock:
            if iid in self._acked_interactions:
                if callback:
                    callback({"ok": False, "error": "interaction already responded"})
                return False
            self._acked_interactions[iid] = time.time()
            if len(self._acked_interactions) > INTERACTION_ACK_MAX:
                for old in list(self._acked_interactions.keys())[: INTERACTION_ACK_MAX // 4]:
                    self._acked_interactions.pop(old, None)

        async def _respond() -> Any:
            await self._api_request("PUT", f"/interactions/{iid}", {"code": int(code)})
            return {"ok": True}

        self._run_official_api(_respond, callback)
        return True

    def schedule_interaction_ack(
        self, interaction_id: str, delay: float = INTERACTION_ACK_DELAY
    ) -> None:
        """按钮点击兜底回应：子插件未在 delay 秒内自定义回应时自动补 code=0。

        保证客户端永不因未回应而 loading 到超时；子插件经 respond_interaction
        抢先回应后，此处因同一 id 去重而自动跳过。
        """
        iid = str(interaction_id or "").strip()
        loop = self._loop
        if not iid or loop is None or loop.is_closed():
            return

        async def _later() -> None:
            await asyncio.sleep(max(0.0, delay))
            self.respond_interaction(iid, 0)

        try:
            asyncio.run_coroutine_threadsafe(_later(), loop)
        except RuntimeError:
            pass

    def delete_msg(
        self, message_id: Any, callback: Callable[[Any], None] | None = None
    ) -> None:
        """撤回消息：OneBot 语义 → 官方 DELETE 接口（2026-07 起单聊亦可撤回）。

        - 群聊 /v2/groups/{g}/messages/{id}：机器人（管理员可撤群员的）
          发送超过 2 分钟的消息不可撤回；
        - 单聊 /v2/users/{u}/messages/{id}：仅可撤回机器人发给该用户
          自己的消息（同样 2 分钟时限）。
        结果经 callback 回传 {"ok": ...} 供调用方感知成败。
        """
        key = str(message_id or "")
        scope = self._msg_scopes.get(key)
        if scope is None:
            self.logger.warning(_t("qqofficial.recall_unknown_id", id=key[:24]))
            if callback:
                callback({"ok": False, "error": "未知消息 ID（消息可能已过期或未被机器人收到）"})
            return
        kind, target = scope
        if kind not in ("group", "private"):
            self.logger.warning(_t("qqofficial.recall_unsupported_scope", id=key[:24]))
            if callback:
                callback({"ok": False, "error": "未知消息归属，无法撤回"})
            return

        async def _recall() -> Any:
            path = (
                f"/v2/users/{target}/messages/{key}"
                if kind == "private"
                else f"/v2/groups/{target}/messages/{key}"
            )
            try:
                await self._api_request("DELETE", path)
            except Exception as e:  # noqa: BLE001
                self.logger.warning(_t("qqofficial.recall_failed", group=target, error=e))
                return {"ok": False, "error": f"撤回失败：{e}"}
            self._msg_scopes.pop(key, None)
            self.logger.info(_t("qqofficial.recall_ok", group=target))
            return {"ok": True}

        self._run_official_api(
            _recall, callback, fail_value={"ok": False, "error": "撤回请求执行失败"}
        )

    def set_group_add_request(
        self,
        flag: str,
        sub_type: str = "add",
        approve: bool = True,
        reason: str = "",
        callback: Callable[[Any], None] | None = None,
    ) -> None:
        """处理加群请求：OneBot 语义 → 官方审批接口。

        POST /v2/groups/{g}/approval_join_request/{m}，body {op, join_request_id,
        reject_reason?}；须群管理员，flag 即事件下发的 join_request_id。
        """
        key = str(flag or "")
        target = self._join_requests.get(key)
        if target is None:
            self.logger.warning(_t("qqofficial.join_request_unknown_flag", flag=key[:24]))
            if callback:
                callback({"ok": False, "error": "未知申请（flag 无记录或已被处理）"})
            return
        group_openid, member_openid = target

        async def _approve() -> Any:
            body: dict[str, Any] = {
                "op": "approve" if approve else "decline",
                "join_request_id": key,
            }
            if not approve and reason:
                body["reject_reason"] = str(reason)[:255]
            try:
                await self._api_request(
                    "POST",
                    f"/v2/groups/{group_openid}/approval_join_request/{member_openid}",
                    body,
                )
            except Exception as e:  # noqa: BLE001
                self.logger.warning(
                    _t("qqofficial.join_request_failed", group=group_openid, error=e)
                )
                return {"ok": False, "error": f"审批失败：{e}"}
            self._join_requests.pop(key, None)
            self.logger.info(
                _t(
                    "qqofficial.join_request_handled",
                    group=group_openid,
                    member=member_openid,
                    result="approve" if approve else "decline",
                )
            )
            return {"ok": True}

        self._run_official_api(
            _approve, callback, fail_value={"ok": False, "error": "审批请求执行失败"}
        )

    def get_login_info(self, callback: Callable[[Any], None]) -> None:
        """返回机器人资料：QQ 号取配置的 bot_qq（AppID 不是 QQ 号），昵称取 READY。"""
        info = {
            "user_id": self.bot_qq,
            "nickname": self._bot_user.get("nickname") or "",
            "app_id": self.app_id,
        }
        callback(info)

    def get_group_list(self, callback: Callable[[Any], None]) -> None:
        """官方无群列表接口：以配置 + 动态发现的 group_openid 并集兜底。"""
        known = list(self.groups)
        known += [g for g in self._discovered_groups if g not in self.groups]
        callback([{"group_id": g, "group_name": str(g)} for g in known])

    def get_group_info(
        self, group_id: Any, callback: Callable[[Any], None] | None = None
    ) -> None:
        """群信息本地兜底：配置中的 openid 视为已互通群；未配置时对所有群生效。"""
        if callback:
            callback(self._local_group_info(str(group_id)))

    def call_api(
        self,
        pack: dict[str, Any],
        callback: Callable[[Any], None] | None = None,
        timeout: float = 10.0,
    ) -> None:
        """QQ 官方适配器不支持 OneBot action 协议；直接回调 None 保持行为一致。"""
        if callback:
            callback(None)

    def _run_official_api(
        self,
        coro_factory: Callable[[], Any],
        callback: Callable[[Any], None] | None,
        *,
        fail_value: Any = None,
    ) -> None:
        """在适配器事件循环上执行官方 REST 请求并回调（失败回调 fail_value）。"""
        loop = self._loop
        if loop is None or not loop.is_running():
            self.logger.warning(_t("qqofficial.api_no_loop"))
            if callback:
                callback(fail_value)
            return

        async def _call() -> Any:
            try:
                return await coro_factory()
            except Exception as e:  # noqa: BLE001
                self.logger.warning(_t("qqofficial.api_failed", error=e))
                return fail_value

        future = asyncio.run_coroutine_threadsafe(_call(), loop)
        if callback is None:
            return

        def _done(fut: "concurrent.futures.Future[Any]") -> None:
            try:
                callback(fut.result())
            except Exception:  # noqa: BLE001
                callback(fail_value)

        future.add_done_callback(_done)

    @staticmethod
    def _ban_expire_str(seconds: int) -> str:
        """禁言到期时间（官方要求东八区 ISO 格式）。"""
        expire = datetime.fromtimestamp(
            time.time() + seconds, tz=timezone(timedelta(hours=8))
        )
        return expire.strftime("%Y-%m-%dT%H:%M:%S+08:00")

    def set_group_ban(
        self,
        group_id: Any,
        user_id: Any,
        duration: int,
        callback: Callable[[Any], None] | None = None,
    ) -> None:
        """群禁言 → 官方 POST /v2/groups/{g}/restrict_chat_setting。

        duration 秒（0=解除，>0=禁言至 now+duration）；须群管理员、最长
        30 天、仅能禁言普通成员，超限截断为 30 天。
        """
        group_openid = str(group_id or "")
        member_openid = str(user_id or "")
        if not group_openid or not member_openid:
            if callback:
                callback(None)
            return
        seconds = int(duration or 0)
        if seconds < 0:
            self.logger.warning(_t("qqofficial.ban_invalid_duration", duration=seconds))
            if callback:
                callback(None)
            return
        if seconds > 2592000:  # 官方上限 30 天
            self.logger.warning(_t("qqofficial.ban_too_long", duration=seconds))
            seconds = 2592000

        if seconds == 0:
            member: dict[str, Any] = {
                "op": "del",
                "member_openid": member_openid,
                "mute_expire_at": "",
            }
        else:
            member = {
                "op": "add",
                "member_openid": member_openid,
                "mute_expire_at": self._ban_expire_str(seconds),
            }
        body = {"members": [member]}

        async def _ban() -> Any:
            await self._api_request(
                "POST", f"/v2/groups/{group_openid}/restrict_chat_setting", body
            )
            self.logger.info(
                _t(
                    "qqofficial.ban_ok",
                    group=group_openid,
                    member=member_openid,
                    seconds=seconds,
                )
            )
            return {"ok": True}

        self._run_official_api(_ban, callback)

    def set_group_ban_batch(
        self,
        group_id: Any,
        user_ids: list[Any],
        duration: int,
        callback: Callable[[Any], None] | None = None,
    ) -> None:
        """批量群禁言（官方扩展动作）：单次最多 10 人，其余语义同 set_group_ban。"""
        group_openid = str(group_id or "")
        members = [str(u or "") for u in (user_ids or []) if str(u or "")]
        if not group_openid or not members:
            if callback:
                callback(None)
            return
        if len(members) > 10:
            self.logger.warning(_t("qqofficial.ban_batch_limit", count=len(members)))
            members = members[:10]
        seconds = int(duration or 0)
        if seconds <= 0 or seconds > 2592000:
            self.logger.warning(_t("qqofficial.ban_invalid_duration", duration=seconds))
            if callback:
                callback(None)
            return
        body = {
            "members": [
                {
                    "op": "add",
                    "member_openid": m,
                    "mute_expire_at": self._ban_expire_str(seconds),
                }
                for m in members
            ]
        }

        async def _ban() -> Any:
            await self._api_request(
                "POST", f"/v2/groups/{group_openid}/restrict_chat_setting", body
            )
            self.logger.info(
                _t("qqofficial.ban_batch_ok", group=group_openid, count=len(members))
            )
            return {"ok": True, "count": len(members)}

        self._run_official_api(_ban, callback)

    def get_group_restrict_chat_setting(
        self, group_id: Any, callback: Callable[[Any], None] | None = None
    ) -> None:
        """查询群禁言状态（官方扩展）：全员禁言规则 + 当前禁言成员列表。"""
        group_openid = str(group_id or "")
        if not group_openid:
            if callback:
                callback(None)
            return

        async def _query() -> Any:
            return await self._api_request(
                "GET", f"/v2/groups/{group_openid}/restrict_chat_setting"
            )

        self._run_official_api(_query, callback)

    def get_group_bot_state(
        self, group_id: Any, callback: Callable[[Any], None] | None = None
    ) -> None:
        """查询机器人群内状态（官方扩展）：角色/主动消息开关/收消息设置。"""
        group_openid = str(group_id or "")
        if not group_openid:
            if callback:
                callback(None)
            return

        async def _query() -> Any:
            return await self._api_request(
                "GET", f"/v2/groups/{group_openid}/bot_state"
            )

        self._run_official_api(_query, callback)

    def get_group_join_request_list(
        self,
        group_id: Any,
        cursor: str = "",
        limit: int = 20,
        callback: Callable[[Any], None] | None = None,
    ) -> None:
        """拉取入群申请列表（官方扩展）：需群管理员，支持分页。"""
        group_openid = str(group_id or "")
        if not group_openid:
            if callback:
                callback(None)
            return

        async def _query() -> Any:
            path = f"/v2/groups/{group_openid}/join_request_list"
            if cursor or limit:
                # 官方接口请求体承载分页参数（GET + JSON body）
                return await self._api_request(
                    "GET", path, {"cursor": str(cursor), "limit": int(limit)}
                )
            return await self._api_request("GET", path)

        self._run_official_api(_query, callback)

    # ---- 群成员 / 黑名单 / 踢人（官方内邀白名单接口组） ----
    # 以下接口官方标注「内邀接入中」，未获白名单的机器人调用返回错误码 11253；
    # _run_official_api 捕获后回调 None 并记录告警，子插件按 None 判定降级。

    def set_group_kick(
        self,
        group_id: Any,
        user_id: Any,
        reject_add_request: bool = False,
        callback: Callable[[Any], None] | None = None,
    ) -> None:
        """移出群成员（OneBot set_group_kick）。

        官方端点 batch_remove_members 为内邀白名单接口（错误码 11253 = 未获
        权限，回调 None）。reject_add_request=True 时同时加入群黑名单（对应
        官方 add_to_member_blacklist，语义即 OneBot 的「拒绝此人再次加群」）。
        """
        group_openid = str(group_id or "")
        member_openid = str(user_id or "")
        if not group_openid or not member_openid:
            if callback:
                callback(None)
            return

        async def _kick() -> Any:
            return await self._api_request(
                "POST",
                f"/v2/groups/{group_openid}/batch_remove_members",
                {
                    "member_openids": [member_openid],
                    "add_to_member_blacklist": bool(reject_add_request),
                },
            )

        self._run_official_api(_kick, callback)

    def get_group_member_list(
        self,
        group_id: Any,
        cursor: str = "",
        callback: Callable[[Any], None] | None = None,
    ) -> None:
        """群成员列表（OneBot get_group_member_list，官方内邀白名单接口）。

        传 cursor 时仅拉单页（回调官方原始结构 {members, next_cursor}，大群
        自行分页）；不传时循环翻页聚合为 OneBot 风格扁平列表，页数上限
        OFFICIAL_MEMBER_PAGE_LIMIT（每页 30 条）防失控。
        """
        group_openid = str(group_id or "")
        if not group_openid:
            if callback:
                callback(None)
            return

        async def _query() -> Any:
            path = f"/v2/groups/{group_openid}/members"
            if cursor:
                return await self._api_request(
                    "GET", path, {"cursor": str(cursor)}
                )
            collected: list[Any] = []
            cur = ""
            for _ in range(OFFICIAL_MEMBER_PAGE_LIMIT):
                r = await self._api_request(
                    "GET", path, {"cursor": cur} if cur else None
                )
                if not isinstance(r, dict):
                    return collected or r
                collected.extend(r.get("members") or [])
                cur = str(r.get("next_cursor") or "")
                if not cur:
                    break
            return collected

        self._run_official_api(_query, callback)

    def get_group_member_info(
        self,
        group_id: Any,
        user_id: Any,
        callback: Callable[[Any], None] | None = None,
    ) -> None:
        """群成员信息（OneBot get_group_member_info，官方内邀白名单接口）。

        回调 OneBot 风格 dict（user_id/nickname/role/joined_at/is_bot），
        官方原始载荷挂 raw 键；官方 member_role 与 OneBot role 取值一致
        （member/admin/owner），原样透传。
        """
        group_openid = str(group_id or "")
        member_openid = str(user_id or "")
        if not group_openid or not member_openid:
            if callback:
                callback(None)
            return

        async def _query() -> Any:
            r = await self._api_request(
                "GET", f"/v2/groups/{group_openid}/members/{member_openid}"
            )
            if not isinstance(r, dict):
                return r
            return {
                "user_id": str(r.get("member_openid") or member_openid),
                "nickname": str(r.get("username") or ""),
                "role": str(r.get("member_role") or "member"),
                "is_bot": bool(r.get("bot")),
                "joined_at": str(r.get("joined_at") or ""),
                "union_openid": str(r.get("union_openid") or ""),
                "raw": r,
            }

        self._run_official_api(_query, callback)

    def get_group_blacklist(
        self,
        group_id: Any,
        cursor: str = "",
        callback: Callable[[Any], None] | None = None,
    ) -> None:
        """群黑名单查询（官方扩展动作，官方内邀白名单接口）。

        传 cursor 时仅拉单页（回调官方原始结构 {users, next_cursor}）；
        不传时翻页聚合为扁平列表（每页 limit=100，上限
        OFFICIAL_BLACKLIST_PAGE_LIMIT）。
        """
        group_openid = str(group_id or "")
        if not group_openid:
            if callback:
                callback(None)
            return

        async def _query() -> Any:
            path = f"/v2/groups/{group_openid}/member_blacklist"
            if cursor:
                return await self._api_request(
                    "GET", path, {"cursor": str(cursor), "limit": 100}
                )
            collected: list[Any] = []
            cur = ""
            for _ in range(OFFICIAL_BLACKLIST_PAGE_LIMIT):
                r = await self._api_request(
                    "GET", path, {"cursor": cur, "limit": 100} if cur else None
                )
                if not isinstance(r, dict):
                    return collected or r
                collected.extend(r.get("users") or [])
                cur = str(r.get("next_cursor") or "")
                if not cur:
                    break
            return collected

        self._run_official_api(_query, callback)

    def set_group_blacklist(
        self,
        group_id: Any,
        user_ids: Any,
        add: bool = True,
        callback: Callable[[Any], None] | None = None,
    ) -> None:
        """群黑名单操作（官方扩展动作，官方内邀白名单接口）。

        add=True 加入黑名单（目标成员在群中时无法加入，官方语义）、
        False 移出。单次官方限 20 个 openid，超出自动分批；多批时回调
        {batches: [...各批官方响应...]}，单批回调官方原始响应。
        """
        group_openid = str(group_id or "")
        ids = [str(x) for x in (user_ids or []) if str(x or "").strip()]
        if not group_openid or not ids:
            if callback:
                callback(None)
            return

        async def _query() -> Any:
            path = f"/v2/groups/{group_openid}/member_blacklist"
            results: list[Any] = []
            for i in range(0, len(ids), OFFICIAL_BATCH_OP_LIMIT):
                results.append(
                    await self._api_request(
                        "POST",
                        path,
                        {
                            "op": "add" if add else "del",
                            "member_openids": ids[i : i + OFFICIAL_BATCH_OP_LIMIT],
                        },
                    )
                )
            return results[0] if len(results) == 1 else {"batches": results}

        self._run_official_api(_query, callback)

    def call_action(
        self,
        action: str,
        params: dict[str, Any] | None = None,
        callback: Callable[[Any], None] | None = None,
        timeout: float = 10.0,
    ) -> None:
        """OneBot action 分发：已映射的动作转发官方端点，其余告警降级回调 None。"""
        handler = _ACTION_HANDLERS.get(action)
        if handler is None:
            self.logger.warning(_t("qqofficial.unsupported_action", action=action))
            if callback:
                callback(None)
            return
        handler(self, params or {}, callback)

    def _get_group_info_async(
        self, group_id: Any, callback: Callable[[Any], None] | None
    ) -> None:
        """群信息：优先官方 info 端点（白名单接口），失败回退本地兜底。"""
        group_openid = str(group_id or "")
        if not group_openid:
            if callback:
                callback(None)
            return

        async def _query() -> Any:
            try:
                data = await self._api_request(
                    "GET", f"/v2/groups/{group_openid}/info"
                )
                if isinstance(data, dict) and data.get("group_openid"):
                    return {
                        "group_id": data.get("group_openid"),
                        "group_name": data.get("group_name", ""),
                        "member_count": data.get("group_member_num", 0),
                        "max_member_count": 0,
                        "group_memo": data.get("group_finger_memo", ""),
                    }
            except Exception as e:  # noqa: BLE001
                # 白名单接口无权限属常态：回退本地兜底
                self.logger.debug(_t("qqofficial.group_info_official_failed", error=e))
            return self._local_group_info(group_openid)

        self._run_official_api(_query, callback)

    def _local_group_info(self, group_openid: str) -> dict[str, Any] | None:
        """本地兜底群信息：配置/动态发现的 openid 视为已互通群。"""
        known = {str(g) for g in self.groups}
        if not known or group_openid in known or group_openid in self._discovered_groups:
            return {
                "group_id": group_openid,
                "group_name": group_openid,
                "member_count": 0,
                "max_member_count": 0,
            }
        return None

    def send_pack(self, pack: dict[str, Any]) -> None:
        """OneBot 原始包与官方协议不兼容，忽略并提示。"""
        self.logger.debug(_t("qqofficial.raw_pack_ignored"))

    def __getattr__(self, name: str) -> Any:
        """未显式实现的 OneBot 方法统一降级：末参为回调时以 None 通知失败。

        官方协议无对应能力，降级避免 AttributeError；写操作记 warning 防静默失败。
        """
        if name.startswith("_"):
            raise AttributeError(name)

        def _unsupported(*args: Any, **kwargs: Any) -> Any:
            self.logger.warning(_t("qqofficial.unsupported_action", action=name))
            if args and callable(args[-1]):
                try:
                    args[-1](None)
                except Exception:
                    pass
            return None

        return _unsupported


def _h_send_group_msg(ad: "QQOfficialAdapter", p: dict[str, Any], callback: Any) -> None:
    ad.send_group_msg(p.get("group_id"), p.get("message"))
    if callback:
        callback(None)


def _h_send_private_msg(ad: "QQOfficialAdapter", p: dict[str, Any], callback: Any) -> None:
    ad.send_private_msg(p.get("user_id"), p.get("message"))
    if callback:
        callback(None)


def _h_delete_msg(ad: "QQOfficialAdapter", p: dict[str, Any], callback: Any) -> None:
    ad.delete_msg(p.get("message_id"), callback)


def _h_set_group_add_request(ad: "QQOfficialAdapter", p: dict[str, Any], callback: Any) -> None:
    ad.set_group_add_request(
        str(p.get("flag") or ""),
        str(p.get("sub_type") or "add"),
        bool(p.get("approve", True)),
        str(p.get("reason") or ""),
        callback,
    )


def _h_set_group_ban(ad: "QQOfficialAdapter", p: dict[str, Any], callback: Any) -> None:
    ad.set_group_ban(
        p.get("group_id"), p.get("user_id"), int(p.get("duration") or 0), callback
    )


def _h_set_group_ban_batch(ad: "QQOfficialAdapter", p: dict[str, Any], callback: Any) -> None:
    ad.set_group_ban_batch(
        p.get("group_id"), p.get("user_ids") or [], int(p.get("duration") or 0), callback
    )


def _h_get_group_restrict_chat_setting(ad: "QQOfficialAdapter", p: dict[str, Any], callback: Any) -> None:
    ad.get_group_restrict_chat_setting(p.get("group_id"), callback)


def _h_get_group_bot_state(ad: "QQOfficialAdapter", p: dict[str, Any], callback: Any) -> None:
    ad.get_group_bot_state(p.get("group_id"), callback)


def _h_get_group_join_request_list(ad: "QQOfficialAdapter", p: dict[str, Any], callback: Any) -> None:
    ad.get_group_join_request_list(
        p.get("group_id"),
        str(p.get("cursor") or ""),
        int(p.get("limit") or 20),
        callback,
    )


def _h_respond_interaction(ad: "QQOfficialAdapter", p: dict[str, Any], callback: Any) -> None:
    # 互动事件回应（按钮/快捷菜单点击）：抢占兜底自动回应自定义 code
    ad.respond_interaction(
        p.get("interaction_id"), int(p.get("code") or 0), callback
    )


def _h_get_group_info(ad: "QQOfficialAdapter", p: dict[str, Any], callback: Any) -> None:
    ad._get_group_info_async(p.get("group_id"), callback)


def _h_get_group_list(ad: "QQOfficialAdapter", p: dict[str, Any], callback: Any) -> None:
    ad.get_group_list(callback or (lambda _: None))


def _h_get_login_info(ad: "QQOfficialAdapter", p: dict[str, Any], callback: Any) -> None:
    ad.get_login_info(callback or (lambda _: None))


def _h_set_group_kick(ad: "QQOfficialAdapter", p: dict[str, Any], callback: Any) -> None:
    # OneBot v11 set_group_kick：reject_add_request=True 对应官方
    # add_to_member_blacklist（拉黑即拒绝再次进群）
    ad.set_group_kick(
        p.get("group_id"),
        p.get("user_id"),
        bool(p.get("reject_add_request")),
        callback,
    )


def _h_get_group_member_list(ad: "QQOfficialAdapter", p: dict[str, Any], callback: Any) -> None:
    # cursor 可选：传则单页返回官方结构，不传则翻页聚合为 OneBot 风格列表
    ad.get_group_member_list(
        p.get("group_id"), str(p.get("cursor") or ""), callback
    )


def _h_get_group_member_info(ad: "QQOfficialAdapter", p: dict[str, Any], callback: Any) -> None:
    ad.get_group_member_info(p.get("group_id"), p.get("user_id"), callback)


def _h_get_group_blacklist(ad: "QQOfficialAdapter", p: dict[str, Any], callback: Any) -> None:
    ad.get_group_blacklist(
        p.get("group_id"), str(p.get("cursor") or ""), callback
    )


def _h_set_group_blacklist(ad: "QQOfficialAdapter", p: dict[str, Any], callback: Any) -> None:
    # add: True=拉黑（默认）、False=移出黑名单；user_ids 超官方单批 20 自动分批
    ad.set_group_blacklist(
        p.get("group_id"),
        p.get("user_ids") or [],
        bool(p.get("add", True)),
        callback,
    )


# OneBot action → 官方端点处理器（签名统一 (adapter, params, callback)）
_ACTION_HANDLERS = {
    "send_group_msg": _h_send_group_msg,
    "send_private_msg": _h_send_private_msg,
    "delete_msg": _h_delete_msg,
    "set_group_add_request": _h_set_group_add_request,
    "set_group_ban": _h_set_group_ban,
    "set_group_ban_batch": _h_set_group_ban_batch,
    "get_group_restrict_chat_setting": _h_get_group_restrict_chat_setting,
    "get_group_bot_state": _h_get_group_bot_state,
    "get_group_join_request_list": _h_get_group_join_request_list,
    "respond_interaction": _h_respond_interaction,
    "get_group_info": _h_get_group_info,
    "get_group_list": _h_get_group_list,
    "get_login_info": _h_get_login_info,
    # 群成员管理组（官方内邀白名单接口；未获权限调用回调 None + 告警日志）
    "set_group_kick": _h_set_group_kick,
    "get_group_member_list": _h_get_group_member_list,
    "get_group_member_info": _h_get_group_member_info,
    # 群黑名单（OneBot v11 无标准动作，官方扩展命名）
    "get_group_blacklist": _h_get_group_blacklist,
    "set_group_blacklist": _h_set_group_blacklist,
}
