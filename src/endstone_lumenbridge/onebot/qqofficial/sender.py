"""QQ 官方机器人消息发送器。

消费发送队列，按凭据优先级（被动 msg_id → 入群 event_id → 主动）组装发送。
富媒体经 /v2/{groups|users}/{target}/files 上传；错误码驱动重试：超时/网络
错误按间隔重试并递增 msg_seq 规避官方 (msg_id, msg_seq) 去重，event_id
无效（40034025）清除后立即重发，主动被拒（22009）交补发栈，被动回复成功
后借剩余额度补发。依赖以 adapter 引用注入，避免循环导入。
"""

from __future__ import annotations

import asyncio
import base64
from typing import Any

from ...i18n import t as _t
from .constants import (
    ACTIVE_STACK_FLUSH,
    BIZ_ACTIVE_REJECTED,
    BIZ_EVENT_ID_INVALID,
    BIZ_MSG_ID_EXPIRED,
    MEDIA_FILE_TYPE,
)
from .utils import (
    OUT_MENTION_RE,
    ApiHTTPError,
    biz_code,
    escape_markdown_text,
    extract_payload,
    markdown_to_plain_text,
    normalize_target as _normalize_target,
)


def _payload_body(content: str, file_info: str) -> dict[str, Any]:
    """按内容选择官方消息载体。

    - 富媒体：msg_type=7，content 作配文；
    - 含出站 @ 标记（官方文本链）：切换 markdown 载体（msg_type=2），
      实测仅该组合可渲染真实 @；其余文本同步转义防误解析；
    - 其余：纯文本 msg_type=0。
    """
    if file_info:
        return {"msg_type": 7, "content": content}
    if OUT_MENTION_RE.search(content or ""):
        return {"msg_type": 2, "markdown": {"content": escape_markdown_text(content)}}
    return {"msg_type": 0, "content": content}


class MessageSender:
    """官方消息发送器（组合于 QQOfficialAdapter）。"""

    def __init__(self, adapter: Any) -> None:
        self.ad = adapter
        self.queue: asyncio.Queue | None = None
        self.task: asyncio.Task | None = None

    # ------------------------------------------------------------ 生命周期
    def start(self) -> None:
        """在适配器事件循环内创建发送队列与消费任务（_main 开头调用）。"""
        self.queue = asyncio.Queue(maxsize=100)
        self.task = asyncio.create_task(
            self._sender_loop(),
            name=f"LumenBridge-QQOfficial-Sender-{self.ad.adapter_id or 'default'}",
        )

    # ------------------------------------------------------------ 富媒体上传
    async def upload_media(self, kind: str, target: str, media: dict[str, Any]) -> str:
        """上传富媒体到官方 files 接口，返回 file_info（用于 media 字段）。

        url 优先；本地文件走 base64 file_data 通道。失败抛异常由调用方记录。
        """
        file_type = MEDIA_FILE_TYPE.get(str(media.get("type")), 0)
        if not file_type:
            raise ValueError(f"unsupported media type: {media.get('type')}")
        body: dict[str, Any] = {"file_type": file_type, "srv_send_msg": False}
        url = str(media.get("url") or "").strip()
        if url:
            body["url"] = url
        else:
            blob = media.get("data")
            if not isinstance(blob, (bytes, bytearray)) or not blob:
                raise ValueError("media has neither url nor local data")
            body["file_data"] = base64.b64encode(bytes(blob)).decode("ascii")
        path = (
            f"/v2/groups/{target}/files" if kind == "group" else f"/v2/users/{target}/files"
        )
        data = await self.ad._api_request("POST", path, body)
        file_info = str((data or {}).get("file_info") or (data or {}).get("file_uuid") or "")
        if not file_info:
            raise RuntimeError("files api returned no file_info")
        return file_info

    # ------------------------------------------------------------ 单条发送
    async def post_message(
        self, kind: str, target: str, body: dict[str, Any], has_media: bool
    ) -> str:
        """发送单条消息，含错误码驱动的重试矩阵。

        返回 "ok" / "rejected"（主动被拒 22009，可入补发栈）/ "failed"。
        超时/网络错误按间隔重试（文本 1s / 富媒体 3s，递增 msg_seq 规避
        官方 (msg_id, msg_seq) 去重）；event_id 无效（40034025）清除后立即
        重发一次；markdown 载体被拒（未开通能力）时降级纯文本重发一次。
        参数经 ad._retry_params() 运行时读取，支持测试 monkeypatch。
        """
        path = (
            f"/v2/groups/{target}/messages" if kind == "group" else f"/v2/users/{target}/messages"
        )
        retry_max, delay_text, delay_media = self.ad._retry_params()
        event_id_retried = False
        msg_id_retried = False
        md_fallback_retried = False
        for attempt in range(1, retry_max + 1):
            try:
                data = await self.ad._api_request("POST", path, body)
                # 重试期间 msg_seq 已递增：回写凭据池，否则下次 seq 重复被去重
                if "msg_id" in body and "msg_seq" in body:
                    self.ad.credentials.sync_passive_seq(
                        target, str(body["msg_id"]), int(body["msg_seq"])
                    )
                # 记录发送回执 id（撤回自己发的消息需按 id 反查，2 分钟时限内有效）
                sent_id = str((data or {}).get("id") or "")
                if sent_id:
                    self.ad.remember_msg_scope(sent_id, kind, target)
                return "ok"
            except ApiHTTPError as e:
                biz = biz_code(e)
                if biz == BIZ_ACTIVE_REJECTED:
                    # 主动消息被拒：无需重试，交调用方入栈
                    return "rejected"
                if (
                    not md_fallback_retried
                    and body.get("msg_type") == 2
                    and "markdown" in str(e.detail or e).lower()
                ):
                    # markdown 能力未开通：降级纯文本（@ 不渲染，还原为可读文本）
                    md_fallback_retried = True
                    body["content"] = markdown_to_plain_text(
                        str((body.get("markdown") or {}).get("content") or "")
                    )
                    body.pop("markdown", None)
                    body["msg_type"] = 0
                    continue
                if biz == BIZ_EVENT_ID_INVALID and "event_id" in body and not event_id_retried:
                    # event_id 无效：移除缓存，否则后续消息空耗重试轮次再降级
                    self.ad.credentials.purge_event_id(target)
                    body.pop("event_id", None)
                    event_id_retried = True
                    continue
                if biz == BIZ_MSG_ID_EXPIRED and "msg_id" in body and not msg_id_retried:
                    # msg_id 官方判定过期：移除凭据改走主动通道重发一次
                    self.ad.credentials.purge_passive(target, str(body.get("msg_id") or ""))
                    body.pop("msg_id", None)
                    body.pop("msg_seq", None)
                    msg_id_retried = True
                    continue
                if attempt < retry_max and not self.ad.suppress_connection_log:
                    self.ad.logger.warning(
                        _t(
                            "qqofficial.send_retry",
                            attempt=attempt,
                            max=retry_max,
                            target=target,
                            error=e,
                        )
                    )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if attempt < retry_max and not self.ad.suppress_connection_log:
                    self.ad.logger.warning(
                        _t(
                            "qqofficial.send_retry",
                            attempt=attempt,
                            max=retry_max,
                            target=target,
                            error=e,
                        )
                    )
            # 重试前递增 msg_seq：官方按 (msg_id, msg_seq) 去重，沿用会被判定重复
            if "msg_seq" in body:
                body["msg_seq"] = int(body["msg_seq"]) + 1
            await asyncio.sleep(delay_media if has_media else delay_text)
        return "failed"

    # ------------------------------------------------------------ 补发栈
    async def flush_active_stack(self, target: str) -> None:
        """借被动凭据补发该目标栈内的主动消息（AtoP 机制）。

        仅在被动回复发送成功后调用：复用凭据池剩余额度，每次至多
        ACTIVE_STACK_FLUSH 条，凭据耗尽即止。
        """
        for _ in range(ACTIVE_STACK_FLUSH):
            item = self.ad.credentials.pop_active(target)
            if item is None:
                return
            passive = self.ad.credentials.take_passive(target)
            if passive is None:
                # 凭据耗尽：条目回栈首（unshift 保序），留待下次机会
                self.ad.credentials.unshift_active(item)
                return
            kind, tgt, content, media = item
            try:
                file_info = ""
                if media is not None:
                    try:
                        file_info = await self.upload_media(kind, target, media)
                    except Exception as e:
                        self.ad.logger.warning(
                            _t("qqofficial.media_fallback", target=target, error=e)
                        )
                body = _payload_body(content, file_info)
                body["msg_id"], body["msg_seq"] = passive
                if file_info:
                    body["media"] = {"file_info": file_info}
                result = await self.post_message(kind, target, body, media is not None)
                if result == "ok":
                    # 补发成功属运行提示类日志：静默模式下不打印（防刷屏）
                    if not self.ad.suppress_connection_log:
                        self.ad.logger.info(_t("qqofficial.active_flushed", target=target))
                else:
                    if result == "rejected" and "msg_id" not in body:
                        # 过期降级后被拒：回栈首等待下次，否则静默丢失
                        self.ad.credentials.unshift_active(item)
                    self.ad.logger.warning(
                        _t("qqofficial.active_flush_failed", target=target, error=result)
                    )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.ad.logger.warning(_t("qqofficial.active_flush_failed", target=target, error=e))
            await asyncio.sleep(0.2)

    # ------------------------------------------------------------ 发送队列
    async def _sender_loop(self) -> None:
        """发送队列消费：逐条调用官方 REST 接口，保持顺序。"""
        while self.ad._running:
            try:
                item = await asyncio.wait_for(self.queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            except (asyncio.CancelledError, RuntimeError, GeneratorExit):
                return
            kind, target, content, media = item
            try:
                # 富媒体先上传：失败降级纯文本，不消耗被动凭据（msg_seq）
                file_info = ""
                if media is not None:
                    try:
                        file_info = await self.upload_media(kind, str(target), media)
                    except Exception as e:
                        self.ad.logger.warning(
                            _t("qqofficial.media_fallback", target=target, error=e)
                        )
                        file_info = ""
                passive = self.ad.credentials.take_passive(str(target))
                body = _payload_body(content, file_info)
                if file_info:
                    body["media"] = {"file_info": file_info}
                if passive:
                    # 被动凭据优先（不受主动消息频次限制）
                    body["msg_id"], body["msg_seq"] = passive
                else:
                    # 无被动凭据时尝试入群 event_id（不消耗主动额度）
                    event_id = self.ad.credentials.take_event_id(str(target))
                    if event_id:
                        body["event_id"] = event_id
                        self.ad.logger.debug(_t("qqofficial.event_id_reply", target=target))
                    else:
                        # 群聊主动消息几乎必被拒（22009）即"命令生效但群里无返回"
                        if not self.ad.suppress_connection_log:
                            self.ad.logger.warning(
                                _t("qqofficial.send_no_credential", target=target, head=content[:40])
                            )
                result = await self.post_message(kind, str(target), body, media is not None)
                if result == "rejected":
                    # 过期降级后被拒（body 已无 msg_id）同样入栈，否则静默丢失
                    if passive is None or "msg_id" not in body:
                        # 主动被拒（22009）：入栈等下次被动回复借道补发
                        self.ad.credentials.push_active((kind, str(target), content, media))
                        if not self.ad.suppress_connection_log:
                            self.ad.logger.info(
                                _t(
                                    "qqofficial.active_queued",
                                    target=target,
                                    size=self.ad.credentials.active_size(str(target)),
                                )
                            )
                elif result == "failed":
                    self.ad.logger.warning(
                        _t("qqofficial.send_failed", target=target, error="retries exhausted")
                    )
                if passive is not None and result == "ok":
                    # 被动回复发送成功：借剩余额度补发该目标栈内的主动消息
                    await self.flush_active_stack(str(target))
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.ad.logger.warning(_t("qqofficial.send_failed", target=target, error=e))
            finally:
                # 轻微限速，避免触发平台 QPS 限制
                await asyncio.sleep(0.2)

    # ------------------------------------------------------------ 入队
    def enqueue(self, kind: str, target: Any, message: Any) -> None:
        """消息入队（任意线程可调）：提取文本/富媒体并投递到事件循环。"""
        loop = self.ad._loop
        if not self.ad._running or loop is None or loop.is_closed():
            return
        target = _normalize_target(target)
        content, media = extract_payload(message)
        if not content and media is None:
            # 空载荷丢弃须留痕：base64 未解析被无声丢弃时用户无感知
            self.ad.logger.warning(
                _t("qqofficial.send_dropped_empty", target=target)
            )
            return
        content = content[:2000]

        async def _put() -> None:
            if self.queue is None:
                return
            if self.queue.full():
                # 队满丢弃队首，并记录被丢消息的目标（群 openid / 用户 openid）
                dropped_target = "-"
                try:
                    dropped = self.queue.get_nowait()
                    if isinstance(dropped, tuple) and len(dropped) >= 2:
                        dropped_target = f"{dropped[0]}:{dropped[1]}"
                except asyncio.QueueEmpty:
                    pass
                self.ad.logger.warning(
                    _t("qqofficial.send_queue_dropped", target=dropped_target)
                )
            await self.queue.put((kind, str(target), content, media))

        try:
            asyncio.run_coroutine_threadsafe(_put(), loop)
        except RuntimeError:
            pass
