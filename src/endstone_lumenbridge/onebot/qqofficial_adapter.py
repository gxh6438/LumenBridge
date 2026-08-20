"""QQ 官方机器人适配器（WebSocket 网关模式）—— 组合入口。

对接 QQ 开放平台机器人 API（api.bot.qq.com）。本文件只保留适配器本体：
网关会话（Hello/Identify/Resume/心跳）、鉴权（access_token）、REST 通道、
生命周期与 OneBot 兼容接口；其余职责拆分至 ``qqofficial`` 子包：

- ``qqofficial/constants.py``   协议常量（OP 码 / Intents / 窗口 / 错误码）
- ``qqofficial/utils.py``       HTTP 错误封装、业务码提取、消息内容解析
- ``qqofficial/credentials.py`` 被动凭据池 / 入群 event_id / 主动补发栈
- ``qqofficial/translate.py``   官方事件 → OneBot v11 事件包翻译
- ``qqofficial/sender.py``      发送队列 / 富媒体上传 / 重试矩阵 / 补发

消息范围：群消息（group_message_create 全量 / group_at_message_create @）、
C2C 私聊（c2c_message_create）、频道（at_message_create / direct_message_create）
及全部 notice / 扩展事件（OneBot 无语义的以官方事件名小写转发）。
群标识为 group_openid（字符串），与 OneBot 数字群号不同，经 parse_groups_loose
解析；事件包携带 ``domain="official"``（频道为 "guild"）与个人号域区分。

实现为零外部依赖：REST 走 urllib（asyncio.to_thread），WS 走内嵌 websockets。
事件包转换为 OneBot v11 格式后经 ``bus.emit("onebot.pack", ...)`` 派发，
因此 dispatcher / chat_sync / 正则引擎 / 子插件全部复用。
"""

from __future__ import annotations

import asyncio
import json
import random
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Callable

from ..i18n import t as _t
from ..vendor import import_websockets
from .adapter import USER_AGENT, _WS_STATE_OPEN
from .qqofficial.constants import (
    ACTIVE_STACK_FLUSH,
    ACTIVE_STACK_MAX,
    API_DOMAIN,
    AUTH_FAIL_CODES,
    BIZ_ACTIVE_REJECTED,
    BIZ_EVENT_ID_INVALID,
    BIZ_MSG_ID_EXPIRED,
    CT_MEDIA_API,
    CT_SEGMENT_PREFIX,
    DEFAULT_CONNECT_INTERVAL,
    DEFAULT_INTENTS,
    EVENT_ID_WINDOW,
    HTTP_TIMEOUT,
    INTENT_FALLBACK_THRESHOLD,
    INTENT_GROUP_MEMBER,
    LOCAL_MEDIA_MAX,
    MEDIA_FILE_TYPE,
    OFFICIAL_RAW_EVENTS,
    OP_DISPATCH,
    OP_HEARTBEAT,
    OP_HEARTBEAT_ACK,
    OP_HELLO,
    OP_IDENTIFY,
    OP_INVALID_SESSION,
    OP_RECONNECT,
    OP_RESUME,
    PASSIVE_MAX_SEQ_GROUP,
    PASSIVE_POOL_MAX,
    PASSIVE_WINDOW_C2C,
    PASSIVE_WINDOW_GROUP,
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
from .qqofficial.utils import (
    ApiHTTPError,
    biz_code,
    content_of,
    extract_payload,
    mention_segments,
    plain_content,
)

websockets = import_websockets()

# ---------------------------------------------------------------- 兼容别名
# 历史版本常量 / 函数自本模块导入（tests/test_qqofficial.py 与文档引用），
# 模块分离后统一 re-export；下划线名保持不变以兼容 monkeypatch。
_API_DOMAIN = API_DOMAIN
_SANDBOX_DOMAIN = SANDBOX_DOMAIN
_TOKEN_URL = TOKEN_URL
_HTTP_TIMEOUT = HTTP_TIMEOUT
_RECONNECT_BASE_DELAY = RECONNECT_BASE_DELAY
_RECONNECT_MAX_DELAY = RECONNECT_MAX_DELAY
_PASSIVE_WINDOW_GROUP = PASSIVE_WINDOW_GROUP
_PASSIVE_WINDOW_C2C = PASSIVE_WINDOW_C2C
_PASSIVE_MAX_SEQ = PASSIVE_MAX_SEQ_GROUP  # 群聊 5 次（C2C 为 4 次，见 credentials）
_PASSIVE_POOL_MAX = PASSIVE_POOL_MAX
_EVENT_ID_WINDOW = EVENT_ID_WINDOW
_ACTIVE_STACK_MAX = ACTIVE_STACK_MAX
_ACTIVE_STACK_FLUSH = ACTIVE_STACK_FLUSH
_SEND_RETRY_MAX = SEND_RETRY_MAX
_SEND_RETRY_DELAY_TEXT = SEND_RETRY_DELAY_TEXT
_SEND_RETRY_DELAY_MEDIA = SEND_RETRY_DELAY_MEDIA
_BIZ_ACTIVE_REJECTED = BIZ_ACTIVE_REJECTED
_BIZ_EVENT_ID_INVALID = BIZ_EVENT_ID_INVALID
_BIZ_MSG_ID_EXPIRED = BIZ_MSG_ID_EXPIRED
_DEFAULT_CONNECT_INTERVAL = DEFAULT_CONNECT_INTERVAL
_AUTH_FAIL_CODES = AUTH_FAIL_CODES
_SESSION_RESET_CODES = SESSION_RESET_CODES
_SESSION_ENDED = SESSION_ENDED
_SESSION_RECONNECT = SESSION_RECONNECT
_SESSION_INVALID = SESSION_INVALID
_DEFAULT_INTENTS = DEFAULT_INTENTS
_OFFICIAL_RAW_EVENTS = OFFICIAL_RAW_EVENTS
_MEDIA_FILE_TYPE = MEDIA_FILE_TYPE
_CT_SEGMENT = CT_SEGMENT_PREFIX
_CT_MEDIA_API = CT_MEDIA_API
_LOCAL_MEDIA_MAX = LOCAL_MEDIA_MAX
_biz_code = biz_code
_extract_payload = extract_payload
_content_of = content_of
_plain_content = plain_content
_mention_segments = mention_segments


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
        # 后台静默日志开关（默认开启）：True 时抑制连接/断连/重连、凭据降级
        # 与补发提示等运行类日志（防刷屏）；发送失败等异常日志不受影响
        self.suppress_connection_log = bool(suppress_connection_log)
        # 连接间隔（毫秒）：两次网关连接尝试之间的最小等待时间；0 表示按指数退避自动重连
        try:
            self.connect_interval = max(0, int(connect_interval))
        except (TypeError, ValueError):
            self.connect_interval = DEFAULT_CONNECT_INTERVAL
        # 附加事件订阅位（按需叠加，见 constants intent 表）；非法值回落 0
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
        # 动态学习的群 openid（官方无群列表 API，从收到的群事件中发现）：
        # openid → 最近活跃时间。配置 groups 为空时广播发往这些群
        #（「未填写群 openid → 对所有群生效」的全局转发语义）
        self._discovered_groups: dict[str, float] = {}
        self.config_snapshot: dict[str, Any] | None = None

        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._running = False
        self._main_future: Any = None

        self._ws: Any = None
        self._heartbeat_task: asyncio.Task | None = None

        # 网关会话状态（跨重连保留用于 Resume）
        self._session_id: str = ""
        self._last_seq: int = 0
        # 最近一次心跳 ACK（op 11）到达时间：心跳看门狗判定半开连接用
        self._last_ack_at: float = 0.0
        # access_token 缓存
        self._access_token: str = ""
        self._token_expires: float = 0.0
        # asyncio.Lock 需绑定运行中的事件循环：__init__ 早于事件循环创建，
        # 改为延迟创建（_main 开头与使用处 None 防御）
        self._token_lock: asyncio.Lock | None = None
        # READY 的机器人资料（供 get_login_info 使用）
        self._bot_user: dict[str, Any] = {}

        # 入群申请缓存：join_request_id(flag) → (group_openid, member_openid)。
        # 官方审批接口需三者齐备，OneBot set_group_add_request 只回传 flag，
        # 事件到达时先记下映射。上限防爆内存（超出丢最旧）。
        self._join_requests: dict[str, tuple[str, str]] = {}
        # 消息 id → (kind, target)：撤回接口需 group_openid 反查（OneBot delete_msg 只带 message_id）
        self._msg_scopes: dict[str, tuple[str, str]] = {}
        # GROUP_MEMBER intent（1<<24）自适应降级：官方文档双版本（总表 1<<24 /
        # 部分事件页 1<<25），默认双订阅；若机器人无 1<<24 权限被网关拒连，
        # 连续 Identify 失败达阈值后自动摘除该位重连（见 _main / _identify_or_resume）
        self._group_member_intent = True
        self._identify_failures = 0

        # 子模块组合（translate / sender 经 self.ad 引用回适配器）
        self.credentials = CredentialsStore()
        self.translator = EventTranslator(self)
        self.sender = MessageSender(self)

    # ------------------------------------------------------------ 属性视图
    @property
    def is_connected(self) -> bool:
        ws = self._ws
        if ws is None:
            return False
        # websockets>=13 新 asyncio API 用 state（State.OPEN==1）；
        # 旧 legacy API 才有 closed 属性，做双兼容。
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

    # ------------------------------------------------------------ 兼容委托
    # 历史内部接口（tests/test_qqofficial.py 及文档引用），转发到子模块。
    @property
    def _passive(self) -> dict[str, Any]:
        return self.credentials.passive

    @property
    def _event_ids(self) -> dict[str, Any]:
        return self.credentials.event_ids

    @property
    def _active_stack(self) -> dict[str, Any]:
        return self.credentials.active_stack

    def _cache_passive(self, target: str, msg_id: str, window: float) -> None:
        self.credentials.cache_passive(target, msg_id, window)

    def _take_passive(self, target: str) -> tuple[str, int] | None:
        return self.credentials.take_passive(target)

    def _cache_event_id(self, target: str, event_id: Any) -> None:
        self.credentials.cache_event_id(target, event_id)

    def _take_event_id(self, target: str) -> str:
        return self.credentials.take_event_id(target)

    def _push_active_stack(
        self, kind: str, target: str, content: str, media: dict[str, Any] | None
    ) -> None:
        self.credentials.push_active((kind, target, content, media))

    async def _flush_active_stack(self, target: str) -> None:
        await self.sender.flush_active_stack(target)

    async def _post_message(
        self, kind: str, target: str, body: dict[str, Any], has_media: bool
    ) -> str:
        return await self.sender.post_message(kind, target, body, has_media)

    async def _upload_media(self, kind: str, target: str, media: dict[str, Any]) -> str:
        return await self.sender.upload_media(kind, target, media)

    async def _on_dispatch(self, msg: dict[str, Any]) -> None:
        await self.translator.on_dispatch(msg)

    async def _emit_group_message(self, data: dict[str, Any]) -> None:
        await self.translator._emit_group_message(data)

    async def _emit_c2c_message(self, data: dict[str, Any]) -> None:
        await self.translator._emit_c2c_message(data)

    def _emit_robot_added(self, data: dict[str, Any]) -> None:
        self.translator._emit_robot_added(data)

    def _emit_robot_removed(self, data: dict[str, Any]) -> None:
        self.translator._emit_robot_removed(data)

    def _emit_friend_change(self, data: dict[str, Any], notice_type: str) -> None:
        self.translator._emit_friend_change(data, notice_type)

    def _retry_params(self) -> tuple[int, float, float]:
        """发送重试参数 (max, 文本间隔, 富媒体间隔)。

        运行时读模块级常量，测试 monkeypatch 本模块后立即生效。
        """
        return _SEND_RETRY_MAX, _SEND_RETRY_DELAY_TEXT, _SEND_RETRY_DELAY_MEDIA

    # ------------------------------------------------------------ 生命周期
    def start(self) -> None:
        if self._running:
            return
        self._running = True
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
        self.sender.queue = None
        self.sender.task = None
        # 锁可能绑定旧事件循环，重置以便下次 start() 在新循环内重建
        self._token_lock = None
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
        # 延迟创建 token 锁：确保绑定本适配器自有事件循环
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
            # GROUP_MEMBER（1<<24）自适应降级：全新 Identify 的会话在收到
            # READY 前即断开视为一次失败（权限不足被网关拒绝等），
            # 连续达阈值后摘除该位重连，避免死循环拒连
            if self._running and was_fresh_identify and not ready_before:
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
            # 会话健康存活超过 60s 后的断开视为独立新故障，重置退避计数，
            # 避免服务端例行重连（op=7）反复拉长重连间隔
            if time.monotonic() - session_started >= 60.0:
                attempt = 0
            attempt += 1
            backoff = min(
                RECONNECT_MAX_DELAY, RECONNECT_BASE_DELAY ** min(attempt, 5) + random.uniform(0, 2)
            )
            if self._session_id:
                # Resume 快速重连：官方要求断开后"短时间内"重连补发事件，
                # 会话 TTL 很短；此时不适用 connect_interval 下限，尽快恢复
                delay = min(backoff, 5.0)
            else:
                # 全新 Identify：连接间隔配置（毫秒）作为重连等待下限，
                # 防止鉴权失败/无会话时高频建连打爆网关；0 表示仅按指数退避
                configured = self.connect_interval / 1000.0
                delay = max(backoff, configured) if configured > 0 else backoff
            if not self.suppress_connection_log:
                self.logger.warning(_t("adapter.disconnected", delay=delay, attempt=attempt))
            await asyncio.sleep(delay)

    # ------------------------------------------------------------ 鉴权
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
        # None 防御：锁在 _main 开头创建，此处兜底保证早于 _main 调用时也可用
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

    # ------------------------------------------------------------ 网关会话
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
            # 会话无效：重置后由外层重连走全新 Identify
            if not self.suppress_connection_log:
                self.logger.warning(_t("qqofficial.invalid_session"))
            self._session_id = ""
            self._last_seq = 0
            await asyncio.sleep(3.0)
            return SESSION_INVALID
        if op == OP_DISPATCH:
            seq = msg.get("s")
            if isinstance(seq, int) and seq > 0:
                self._last_seq = seq
            await self._on_dispatch(msg)
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
                # websockets>=13 新 API 用 state 判断连接，旧 legacy API 才有
                # closed 属性；单用 getattr(..., "closed", True) 在新版本下恒为
                # True，心跳任务会立即退出导致网关因无心跳踢掉连接。
                if self._ws is None or not self.is_connected:
                    return
                # ACK 看门狗：超过 2 个心跳周期未收到 op 11，说明链路已死
                # （服务端不再 ACK 的半开连接），主动断开触发 Resume 快速重连
                if time.monotonic() - self._last_ack_at > interval * 2 + 10:
                    if not self.suppress_connection_log:
                        self.logger.warning(_t("qqofficial.heartbeat_stale"))
                    try:
                        await self._ws.close()
                    except Exception:
                        pass
                    return
                await self._ws.send(json.dumps(payload))
                # 按 80% 周期发送心跳：为事件循环偶发阻塞留余量，避免
                # 心跳迟到触发服务端断连
                await asyncio.sleep(interval * 0.8)
        except asyncio.CancelledError:
            return
        except Exception:
            return

    # ------------------------------------------------------------ READY
    def on_ready(self, data: dict[str, Any]) -> None:
        """网关 READY：记录会话 id 与机器人资料，通知上线（translator 回调）。"""
        self._session_id = str(data.get("session_id") or "")
        user = data.get("user") or {}
        if isinstance(user, dict):
            self._bot_user = {
                "qq": 0,
                "nickname": str(user.get("username") or ""),
            }
        # OneBot v11 元事件对齐：连接建立后上报 lifecycle.connect，
        # 子插件经 meta_event.connect 订阅在官方/个人号适配器间行为一致
        self.bus.emit(
            "onebot.pack",
            {
                "self_id": self.app_id,
                "time": int(time.time()),
                "post_type": "meta_event",
                "meta_event_type": "lifecycle",
                "sub_type": "connect",
                "domain": "official",
                "_lumen_adapter_id": self.adapter_id,
            },
        )
        self.bus.emit("bot.online", self)
        self.logger.info(
            _t("qqofficial.ready", name=self._bot_user.get("nickname") or self.app_id)
        )

    # ------------------------------------------------------------ 发送入口
    def send_group_msg(self, group_id: Any, message: Any) -> None:
        self.sender.enqueue("group", group_id, message)

    def send_private_msg(self, user_id: Any, message: Any) -> None:
        self.sender.enqueue("private", user_id, message)

    # ------------------------------------------------------------ 入群审批
    def remember_group(self, group_openid: Any) -> None:
        """记录动态发现的群 openid（收到该群的任意事件时调用）。

        官方无群列表 API，「未配置群 openid = 全局转发」语义下的广播目标
        只能从流入事件中学习。上限防爆内存；新群首次发现时提示管理员
        可将其抄录进连接配置以固定目标。
        """
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

    def broadcast_groups(self) -> list[str]:
        """广播目标群列表：配置的 groups 优先，未配置时用动态发现的群。

        chat_sync 出站广播调用；两者皆空返回空列表（调用方跳过发送，
        不再向不存在的虚拟群 0 发送）。
        """
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

    # ------------------------------------------------------------ 消息撤回
    def remember_msg_scope(self, message_id: Any, kind: str, target: str) -> None:
        """记录消息 id → (kind, target) 映射，供 delete_msg 反查群/私聊。

        kind: "group"（撤回官方支持，机器人消息 2 分钟内、管理员可撤成员消息）
              / "private"（官方无撤回接口，仅记录用于告警提示）。
        """
        key = str(message_id or "")
        if not key or not target:
            return
        if len(self._msg_scopes) >= 1024:
            for old in list(self._msg_scopes.keys())[:128]:
                self._msg_scopes.pop(old, None)
        self._msg_scopes[key] = (kind, str(target))

    def delete_msg(self, message_id: Any) -> None:
        """撤回消息：OneBot 语义 → 官方 DELETE /v2/groups/{g}/messages/{id}。

        - 仅群聊消息可撤回：机器人自己发的限 2 分钟内；
          机器人是群管理员时可撤普通群员的消息（message_id 取自收到的消息事件）；
        - C2C / 未知 id：官方无接口或无记录，告警并跳过。
        """
        key = str(message_id or "")
        scope = self._msg_scopes.get(key)
        if scope is None:
            self.logger.warning(_t("qqofficial.recall_unknown_id", id=key[:24]))
            return
        kind, target = scope
        if kind != "group":
            self.logger.warning(_t("qqofficial.recall_unsupported_scope", id=key[:24]))
            return
        loop = self._loop
        if loop is None or not loop.is_running():
            self.logger.warning(_t("qqofficial.recall_no_loop", id=key[:24]))
            return

        async def _recall() -> None:
            try:
                await self._api_request("DELETE", f"/v2/groups/{target}/messages/{key}")
                self._msg_scopes.pop(key, None)
                self.logger.info(_t("qqofficial.recall_ok", group=target))
            except Exception as e:
                self.logger.warning(_t("qqofficial.recall_failed", group=target, error=e))

        asyncio.run_coroutine_threadsafe(_recall(), loop)

    def set_group_add_request(
        self,
        flag: str,
        sub_type: str = "add",
        approve: bool = True,
        reason: str = "",
    ) -> None:
        """处理加群请求：OneBot 语义 → 官方审批接口。

        POST /v2/groups/{group_openid}/approval_join_request/{member_openid}
        body: {op: approve|decline, join_request_id, reject_reason?}
        （须机器人是群管理员；flag 为事件下发的 join_request_id）
        """
        key = str(flag or "")
        target = self._join_requests.get(key)
        if target is None:
            self.logger.warning(_t("qqofficial.join_request_unknown_flag", flag=key[:24]))
            return
        group_openid, member_openid = target
        loop = self._loop
        if loop is None or not loop.is_running():
            self.logger.warning(_t("qqofficial.join_request_no_loop"))
            return

        async def _approve() -> None:
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
                self._join_requests.pop(key, None)
                self.logger.info(
                    _t(
                        "qqofficial.join_request_handled",
                        group=group_openid,
                        member=member_openid,
                        result="approve" if approve else "decline",
                    )
                )
            except Exception as e:  # noqa: BLE001
                self.logger.warning(
                    _t("qqofficial.join_request_failed", group=group_openid, error=e)
                )

        asyncio.run_coroutine_threadsafe(_approve(), loop)

    # ------------------------------------------------------------ 兼容接口
    def get_login_info(self, callback: Callable[[Any], None]) -> None:
        """返回机器人资料：QQ 号取配置的 bot_qq（AppID 不是 QQ 号），昵称取 READY。"""
        info = {
            "user_id": self.bot_qq,
            "nickname": self._bot_user.get("nickname") or "",
            "app_id": self.app_id,
        }
        callback(info)

    def get_group_list(self, callback: Callable[[Any], None]) -> None:
        """官方无群列表接口：以配置 + 动态发现的 group_openid 本地兜底。"""
        seen = list(self.groups)
        seen += [g for g in self._discovered_groups if g not in self.groups]
        callback([{"group_id": g, "group_name": str(g)} for g in seen])

    def get_group_info(
        self, group_id: Any, callback: Callable[[Any], None] | None = None
    ) -> None:
        """群信息本地兜底：配置中的 openid 视为已互通群。

        未配置任何群 openid 时默认对所有群生效（与 group_allowed 规则一致）。
        """
        key = str(group_id)
        known = {str(g) for g in self.groups}
        info = (
            {"group_id": key, "group_name": key, "member_count": 0, "max_member_count": 0}
            if not known or key in known or key in self._discovered_groups
            else None
        )
        if callback:
            callback(info)

    def call_api(
        self,
        pack: dict[str, Any],
        callback: Callable[[Any], None] | None = None,
        timeout: float = 10.0,
    ) -> None:
        """QQ 官方适配器不支持 OneBot action 协议；直接回调 None 保持行为一致。"""
        if callback:
            callback(None)

    def call_action(
        self,
        action: str,
        params: dict[str, Any] | None = None,
        callback: Callable[[Any], None] | None = None,
        timeout: float = 10.0,
    ) -> None:
        if callback:
            callback(None)

    def send_pack(self, pack: dict[str, Any]) -> None:
        """OneBot 原始包与官方协议不兼容，忽略并提示。"""
        self.logger.debug(_t("qqofficial.raw_pack_ignored"))

    def __getattr__(self, name: str) -> Any:
        """未显式实现的 OneBot 方法统一降级：末参为回调时以 None 通知失败。

        hub.__getattr__ 会把任意 OneBot 方法代理到适配器，QQ 官方协议
        没有对应能力（禁言 / 撤回 / 群管理等），降级避免 AttributeError；
        写操作（set_/delete_ 等）按 warning 记录，避免静默失败无感知。
        """
        if name.startswith("_"):
            raise AttributeError(name)

        def _unsupported(*args: Any, **kwargs: Any) -> Any:
            self.logger.warning(_t("qqofficial.unsupported_action", action=name))
            # 末参为回调时回调 None 通知失败（查询与写操作一致）
            if args and callable(args[-1]):
                try:
                    args[-1](None)
                except Exception:
                    pass
            return None

        return _unsupported
