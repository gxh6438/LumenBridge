"""QQ 官方事件 → OneBot v11 事件包翻译。

接收网关 DISPATCH（op=0）载荷翻译为 OneBot v11 事件包派发：有对应语义的
映射标准事件；无对应语义的以官方事件名小写作扩展 notice 转发（raw=原始
载荷，仅官方适配器产生）；频道事件经 domain="guild" 与 QQ 群域隔离。
依赖以 adapter 引用注入，避免循环导入。
"""

from __future__ import annotations

import time
from typing import Any

from ...i18n import t as _t
from .constants import (
    CT_MEDIA_API,
    CT_SEGMENT_EXACT,
    CT_SEGMENT_PREFIX,
    OFFICIAL_RAW_EVENTS,
    PASSIVE_MAX_SEQ_C2C,
    PASSIVE_MAX_SEQ_GROUP,
    PASSIVE_WINDOW_C2C,
    PASSIVE_WINDOW_GROUP,
)
from .utils import content_segments, mention_segments, plain_content


def _scene_ext_value(data: dict[str, Any], key: str) -> str:
    """提取 message_scene.ext 中 key=value 条目的值（如 ref_msg_idx 引用索引）。

    官方 2026-09 载荷：ext 为 ["msg_idx=...", "ref_msg_idx=...", "auth_token=..."]。
    """
    scene = data.get("message_scene")
    ext = scene.get("ext") if isinstance(scene, dict) else None
    if isinstance(ext, list):
        prefix = f"{key}="
        for item in ext:
            text = str(item or "")
            if text.startswith(prefix):
                return text[len(prefix):]
    return ""


def _mention_nickmap(data: dict[str, Any]) -> dict[str, str]:
    """官方 mentions 数组 → {openid: 昵称} 映射（2026-09 载荷新增）。

    官方已把 @ 提及从 content 文本中剥离、经 mentions 字段单独下发
    （不含 @机器人自身）；映射用于给 at 段补昵称、并补齐 content 中
    已不存在的 @ 标记，避免提及信息整体丢失。
    """
    mentions = data.get("mentions")
    if not isinstance(mentions, list):
        return {}
    result: dict[str, str] = {}
    for user in mentions:
        if not isinstance(user, dict):
            continue
        uid = str(user.get("member_openid") or user.get("user_openid") or user.get("id") or "")
        nick = str(user.get("username") or "").strip()
        if uid and nick:
            result[uid] = nick
    return result


class EventTranslator:
    """官方网关事件翻译器（组合于 QQOfficialAdapter）。"""

    def __init__(self, adapter: Any) -> None:
        self.ad = adapter

    # ------------------------------------------------------------ 分发入口
    async def on_dispatch(self, msg: dict[str, Any]) -> None:
        event = str(msg.get("t") or "")
        data = msg.get("d") or {}
        if not isinstance(data, dict):
            return

        if event == "READY":
            self.ad.on_ready(data)
            return
        if event == "RESUMED":
            # 会话恢复属运行提示类日志：静默模式下不打印（防刷屏）
            if not self.ad.suppress_connection_log:
                self.ad.logger.info(_t("qqofficial.resumed"))
            self.ad.bus.emit("bot.online", self.ad)
            return
        if event in ("GROUP_MESSAGE_CREATE", "GROUP_AT_MESSAGE_CREATE"):
            # @ 消息事件类型即「@机器人」信号：官方会把 @bot 前缀从 content 中剥离，扫描正文恒漏
            await self._emit_group_message(data, at_bot=event == "GROUP_AT_MESSAGE_CREATE")
            return
        if event == "C2C_MESSAGE_CREATE":
            await self._emit_c2c_message(data)
            return
        if event == "GROUP_ADD_ROBOT":
            self._emit_robot_added(data)
            return
        if event == "GROUP_DEL_ROBOT":
            self._emit_robot_removed(data)
            return
        if event == "FRIEND_ADD":
            self._emit_friend_change(data, "friend_add")
            return
        if event == "FRIEND_DEL":
            self._emit_friend_change(data, "friend_del")
            return
        if event in ("GROUP_MSG_REJECT", "GROUP_MSG_RECEIVE"):
            # 群管理员开关「接收主动消息」：关闭后无法主动发言，静默丢弃无从排查
            self._emit_group_msg_switch(data, rejected=event == "GROUP_MSG_REJECT")
            return
        if event in ("C2C_MSG_REJECT", "C2C_MSG_RECEIVE"):
            # C2C 版：用户开关「主动消息」推送；关闭后无法主动私聊该用户
            self._emit_c2c_msg_switch(data, rejected=event == "C2C_MSG_REJECT")
            return
        if event in ("AT_MESSAGE_CREATE", "MESSAGE_CREATE"):
            # 频道 @（1<<30）与私域全量消息（1<<9）载荷相同：映射群消息，domain="guild" 隔离
            await self._emit_guild_message(data)
            return
        if event == "DIRECT_MESSAGE_CREATE":
            # 频道私信（1<<12）：映射为私聊消息，domain="guild"
            await self._emit_guild_direct_message(data)
            return
        if event in ("PUBLIC_MESSAGE_DELETE", "DIRECT_MESSAGE_DELETE", "MESSAGE_DELETE"):
            # 频道撤回：PUBLIC_*/MESSAGE_DELETE → group_recall，DIRECT_* → friend_recall
            self._emit_guild_recall(data, friend=event == "DIRECT_MESSAGE_DELETE")
            return
        if event in ("GROUP_MEMBER_ADD", "GROUP_MEMBER_REMOVE"):
            # 群成员进退群：映射 OneBot group_increase/group_decrease，附 raw
            self._emit_group_member_event(data, joined=event == "GROUP_MEMBER_ADD")
            return
        if event == "GROUP_JOIN_REQUEST":
            # 申请加群：OneBot request.group.add，flag=join_request_id 供审批回传
            self._emit_group_join_request(data)
            return
        if event in ("GUILD_MEMBER_ADD", "GUILD_MEMBER_UPDATE", "GUILD_MEMBER_REMOVE"):
            # 频道成员进退/资料变更（1<<1 特权 intent）：进退映射 OneBot，变更走扩展转发
            if event == "GUILD_MEMBER_ADD":
                self._emit_guild_member_change(data, joined=True)
                return
            if event == "GUILD_MEMBER_REMOVE":
                self._emit_guild_member_change(data, joined=False)
                return
            self._emit_official_raw(event, data)
            return
        if event in OFFICIAL_RAW_EVENTS:
            # 无 OneBot 对应语义：官方事件名小写作 notice_type 原样转发
            self._emit_official_raw(event, data)
            return
        # 兜底：官方新增/未枚举事件同样转发，保证子插件永远能收到全量事件
        self._emit_official_raw(event, data, unknown=True)

    # ------------------------------------------------------------ 基础包装
    def _base_pack(self, **extra: Any) -> dict[str, Any]:
        """OneBot v11 事件包公共字段。"""
        pack: dict[str, Any] = {
            "self_id": self.ad.app_id,
            "time": int(time.time()),
            "domain": "official",
            "_lumen_adapter_id": self.ad.adapter_id,
        }
        pack.update(extra)
        return pack

    def _notice_pack(self, notice_type: str, sub_type: str, data: dict[str, Any]) -> None:
        """机器人自身入群通用 notice 包：user_id=机器人（app_id），operator_id=op_member_openid。"""
        pack = self._base_pack(
            post_type="notice",
            notice_type=notice_type,
            sub_type=sub_type,
            group_id=str(data.get("group_openid") or ""),
            user_id=self.ad.app_id,  # 加入者是机器人自身
            operator_id=str(data.get("op_member_openid") or ""),
        )
        self.ad._emit_pack(pack)

    # ------------------------------------------------------------ notice 翻译
    def _emit_robot_added(self, data: dict[str, Any]) -> None:
        """机器人被添加到群：打印 group_openid 供管理员抄录配置。"""
        group_openid = str(data.get("group_openid") or "")
        if group_openid:
            self.ad.remember_group(group_openid)
            self.ad.logger.info(
                _t("qqofficial.robot_added", group=group_openid, name=self.ad.display_name)
            )
            # event_id 可作被动回复凭据：入群欢迎消息借此发送，不受主动频次限制
            self.ad.credentials.cache_event_id(group_openid, data.get("event_id"))
        self._notice_pack("group_increase", "approve", data)

    def _emit_robot_removed(self, data: dict[str, Any]) -> None:
        """机器人被移出群：sub_type=kick_me（user_id=self_id 语义）。"""
        group_openid = str(data.get("group_openid") or "")
        if group_openid:
            self.ad.logger.warning(
                _t("qqofficial.robot_removed", group=group_openid, name=self.ad.display_name)
            )
            # 移群清理：发现记录 + 全部凭据，防死群继续参与广播与必败重试
            self.ad.forget_group(group_openid)
        pack = self._base_pack(
            post_type="notice",
            notice_type="group_decrease",
            sub_type="kick_me",
            group_id=group_openid,
            user_id=self.ad.app_id,  # 被移出的是机器人自身
            operator_id=str(data.get("op_member_openid") or ""),
        )
        self.ad._emit_pack(pack)

    def _emit_group_msg_switch(self, data: dict[str, Any], rejected: bool) -> None:
        """群管理员 关闭/开启 机器人主动消息（GROUP_MSG_REJECT / GROUP_MSG_RECEIVE）。

        OneBot v11 无对应事件（group_ban 是"禁言"，语义不符），故用自定义
        notice_type="group_msg_switch"（sub_type=reject/receive），与 C2C 的
        friend_msg_switch 成对；不识别该类型的插件会安全忽略。
        """
        group_openid = str(data.get("group_openid") or "")
        if not group_openid:
            return
        if rejected:
            self.ad.logger.warning(
                _t("qqofficial.group_msg_rejected", group=group_openid, name=self.ad.display_name)
            )
        else:
            self.ad.logger.info(
                _t("qqofficial.group_msg_received", group=group_openid, name=self.ad.display_name)
            )
        pack = self._base_pack(
            post_type="notice",
            notice_type="group_msg_switch",
            sub_type="reject" if rejected else "receive",
            group_id=group_openid,
            user_id=self.ad.app_id,  # 被操作者是机器人自身
            operator_id=str(data.get("op_member_openid") or ""),
        )
        self.ad._emit_pack(pack)

    def _emit_c2c_msg_switch(self, data: dict[str, Any], rejected: bool) -> None:
        """用户 关闭/开启 机器人主动消息推送（C2C_MSG_REJECT / C2C_MSG_RECEIVE）。

        OneBot v11 无对应标准通知，使用自定义 notice_type="friend_msg_switch"
        （sub_type=reject/receive）；主要价值在日志可排查。
        """
        openid = str(data.get("openid") or "")
        if not openid:
            return
        if rejected:
            self.ad.logger.warning(
                _t("qqofficial.c2c_msg_rejected", user=openid, name=self.ad.display_name)
            )
        else:
            self.ad.logger.info(
                _t("qqofficial.c2c_msg_received", user=openid, name=self.ad.display_name)
            )
        pack = self._base_pack(
            post_type="notice",
            notice_type="friend_msg_switch",
            sub_type="reject" if rejected else "receive",
            user_id=openid,
            operator_id=openid,
        )
        self.ad._emit_pack(pack)

    def _emit_official_raw(self, event: str, data: dict[str, Any], unknown: bool = False) -> None:
        """OneBot v11 无对应语义的官方事件 → 扩展 notice 转发。

        notice_type 使用官方事件名小写（如 interaction_create），raw 保留官方
        原始载荷。仅官方适配器会产生这些事件，个人号适配器永远不触发同名事件，
        子插件按需 ctx.on("notice.interaction_create", ...) 订阅，未订阅者安全忽略。
        """
        if not event:
            return
        if unknown:
            # 官方新增/未枚举事件：打 DEBUG 便于发现可补充的映射
            self.ad.logger.debug(_t("qqofficial.unknown_event", event=event))
        author = data.get("author")
        pack = self._base_pack(
            post_type="notice",
            notice_type=event.lower(),
            sub_type="",
            user_id=str(
                (data.get("openid") or "")
                or (data.get("user_openid") or "")
                or (data.get("group_member_openid") or "")
                or ((author or {}).get("id") if isinstance(author, dict) else "")
                or ""
            ),
            official_event=event,  # 官方原始事件名（大写），便于子插件精确分发
            raw=data,  # 官方原始载荷
        )
        group_key = str(
            data.get("group_openid")
            or data.get("group_id")
            or data.get("guild_id")
            # GUILD_CREATE/UPDATE/DELETE 载荷以 id 字段标识频道（官方文档）
            or (data.get("id") if event in ("GUILD_CREATE", "GUILD_UPDATE", "GUILD_DELETE") else "")
            or ""
        )
        if group_key:
            pack["group_id"] = group_key
        self.ad._emit_pack(pack)

    # ------------------------------------------------------------ 消息翻译
    # msg_elements 携带正文的消息类型（官方 2026-09 载荷）：
    # 101=并行消息 102=聊天记录（合并转发）103=引用消息（content 可为空）
    _ELEMENT_TEXT_TYPES = frozenset({101, 102, 103})

    def _reference_segments(self, data: dict[str, Any]) -> tuple[list[dict[str, Any]], str]:
        """官方 msg_elements 载荷 → (前置消息段列表, raw 兜底文本)。

        - 引用消息（message_type=103 / ref_msg_idx）：被引用内容转 OneBot
          reply 段（go-cqhttp 惯例附 text/sender/user_id，子插件可直接读）；
        - 聊天记录（102）：转 OneBot forward 段（[合并转发]）；
        - content 为空（纯引用 / 聊天记录转发）：元素正文兜底为 text 段，
          游戏侧不再显示为空消息。
        """
        elements = data.get("msg_elements")
        if not isinstance(elements, list) or not elements:
            return [], ""
        try:
            mtype = int(data.get("message_type") or 0)
        except (TypeError, ValueError):
            mtype = 0
        quoted = None
        for elem in elements:  # 取首个携带内容的元素为被引用消息
            if isinstance(elem, dict) and (
                str(elem.get("content") or "").strip() or elem.get("msg_idx")
            ):
                quoted = elem
                break
        if quoted is None:
            return [], ""
        ref_idx = _scene_ext_value(data, "ref_msg_idx")
        author = quoted.get("author") if isinstance(quoted.get("author"), dict) else {}
        q_uid = str(
            author.get("member_openid") or author.get("user_openid") or author.get("id") or ""
        )
        q_nick = str(author.get("username") or "").strip() or (q_uid[:8] if q_uid else "")
        q_text = str(quoted.get("content") or "").strip()
        segments: list[dict[str, Any]] = []
        if mtype == 102:
            # 聊天记录（合并转发）：OneBot forward 段
            segments.append(
                {"type": "forward", "data": {"id": ref_idx or str(quoted.get("msg_idx") or "")}}
            )
        elif mtype == 103 or ref_idx:
            # 引用回复：reply 段（id 优先官方引用索引，缺省回落元素索引）
            reply_data: dict[str, Any] = {"id": ref_idx or str(quoted.get("msg_idx") or "")}
            if q_text:
                reply_data["text"] = q_text
            if q_nick:
                reply_data["sender"] = q_nick
            if q_uid:
                reply_data["user_id"] = q_uid
            segments.append({"type": "reply", "data": reply_data})
        # 正文为空且元素携带正文：兜底 text 段（同时作为 raw_message 兜底）
        if not str(data.get("content") or "").strip() and q_text and mtype in self._ELEMENT_TEXT_TYPES:
            segments.append({"type": "text", "data": {"text": q_text}})
            return segments, q_text
        return segments, ""

    @staticmethod
    def _enrich_mentions(message: list[dict[str, Any]], data: dict[str, Any]) -> None:
        """用官方 mentions 数组增强 at 段：补昵称 + 补齐缺失的 @。

        content 内联标记解析出的 at 段只有 openid；官方新载荷把 @ 剥离到
        mentions 字段（带昵称），就地补 nickname，未覆盖的追加到尾部，
        保证提及信息不因载荷改版而丢失。
        """
        nickmap = _mention_nickmap(data)
        if not nickmap:
            return
        covered: set[str] = set()
        for seg in message:
            if isinstance(seg, dict) and seg.get("type") == "at":
                uid = str((seg.get("data") or {}).get("qq") or "")
                if not uid:
                    continue
                covered.add(uid)
                if uid in nickmap:
                    seg["data"]["nickname"] = nickmap[uid]
        for uid, nick in nickmap.items():
            if uid not in covered:
                message.append({"type": "at", "data": {"qq": uid, "nickname": nick}})

    async def _fetch_media_url(self, scope: str, owner: str, msg_id: str, seg_type: str) -> str:
        """经官方 media 接口换取附件下载 URL；失败返回空串。

        file_info 字段即下载 URL（带时效）；群与 C2C 路径不同。
        """
        media_type = CT_MEDIA_API.get(seg_type, "file")
        path = (
            f"/v2/groups/{owner}/messages/{msg_id}/media"
            if scope == "group"
            else f"/v2/users/{owner}/messages/{msg_id}/media"
        )
        try:
            data = await self.ad._api_request("POST", path, {"media_type": media_type})
            return str((data or {}).get("file_info") or "")
        except Exception as e:
            self.ad.logger.debug(_t("qqofficial.media_url_failed", msg_id=msg_id, error=e))
            return ""

    async def _attachment_segments(
        self, scope: str, owner: str, msg_id: str, data: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """官方 attachments → OneBot image/video/record 消息段。

        附件自带 url 直接用；缺失时经 media 接口补取（带时效下载地址）。
        无法识别的附件类型跳过。
        """
        segments: list[dict[str, Any]] = []
        attachments = data.get("attachments")
        if not isinstance(attachments, list):
            return segments
        for att in attachments:
            if not isinstance(att, dict):
                continue
            content_type = str(att.get("content_type") or "")
            seg_type = next(
                (seg for prefix, seg in CT_SEGMENT_PREFIX if content_type.startswith(prefix)), ""
            )
            if not seg_type:
                # 官方语音附件 content_type 为 "voice"（无斜杠前缀）：
                # 只做前缀匹配会把它当未知类型静默丢弃
                seg_type = CT_SEGMENT_EXACT.get(content_type, "")
            if not seg_type:
                continue
            url = str(att.get("url") or "").strip()
            filename = str(att.get("filename") or "")
            if not url and msg_id:
                url = await self._fetch_media_url(scope, owner, msg_id, seg_type)
            seg_data: dict[str, Any] = {"url": url, "file": filename, "path": url}
            asr_text = ""
            if seg_type == "record":
                # 官方 2026-09 载荷：语音附件附带 WAV 直链与 ASR 识别文本；
                # SILK 原始链多数播放器不可播，WAV 作为补充字段下发
                wav_url = str(att.get("voice_wav_url") or "").strip()
                if wav_url:
                    seg_data["wav_url"] = wav_url
                    if not url:
                        seg_data["url"] = seg_data["path"] = wav_url
                asr_text = str(att.get("asr_refer_text") or "").strip()
            segments.append({"type": seg_type, "data": seg_data})
            if asr_text:
                # 语音识别文本随段附送：游戏聊天无语音通道，文本是唯一可读形态
                segments.append({"type": "text", "data": {"text": f"[语音] {asr_text}"}})
        return segments

    async def _emit_group_message(self, data: dict[str, Any], at_bot: bool = False) -> None:
        group_openid = str(data.get("group_openid") or "")
        msg_id = str(data.get("id") or "")
        if not group_openid or not msg_id:
            return
        # 动态学习群 openid：官方无群列表 API，靠流入事件发现
        self.ad.remember_group(group_openid)
        author = data.get("author") or {}
        member_openid = str(author.get("member_openid") or "")
        nickname = str(author.get("username") or "").strip() or member_openid[:8]
        # 按原始顺序解析：@其他成员 就地转 at 段，@机器人自身 剥离触发标记
        message, content = content_segments(data.get("content"), self_id=self.ad.app_id)
        # 官方新载荷：引用消息 / 聊天记录前置段（reply / forward）与空正文兜底
        ref_segments, ref_fallback = self._reference_segments(data)
        message = ref_segments + message
        # mentions 数组增强：at 段补昵称、补齐 content 已剥离的 @
        self._enrich_mentions(message, data)
        if not content:
            content = ref_fallback
        # @机器人 留痕：at_bot 即 @ 信号（正文恒漏），全量模式兜底扫描标记
        mention_self = at_bot or any(
            str((seg.get("data") or {}).get("qq") or "") == str(self.ad.app_id)
            for seg in mention_segments(data.get("content"))
        )
        # 记录被动回复凭据（入池：同一 msg_id 可配递增 msg_seq 回复 5 次）
        self.ad.credentials.cache_passive(
            group_openid, msg_id, PASSIVE_WINDOW_GROUP, PASSIVE_MAX_SEQ_GROUP
        )
        # 记录消息归属（delete_msg 撤回管理员场景需按 id 反查群）
        self.ad.remember_msg_scope(msg_id, "group", group_openid)

        message += await self._attachment_segments("group", group_openid, msg_id, data)

        pack = self._base_pack(
            post_type="message",
            message_type="group",
            sub_type="normal",
            message_id=msg_id,
            group_id=group_openid,
            user_id=member_openid,
            anonymous=None,  # OneBot v11 标准字段：官方消息无匿名概念，恒为 null
            message=message,
            raw_message=content,
            font=0,
            mention_self=mention_self,
            sender={"user_id": member_openid, "nickname": nickname, "card": ""},
        )
        self.ad._emit_pack(pack)

    async def _emit_c2c_message(self, data: dict[str, Any]) -> None:
        msg_id = str(data.get("id") or "")
        author = data.get("author") or {}
        user_openid = str(author.get("user_openid") or "")
        if not msg_id or not user_openid:
            return
        nickname = str(author.get("username") or "").strip() or user_openid[:8]
        content = plain_content(data.get("content"))
        # 官方新载荷：引用消息（message_type=103）被引用内容在 msg_elements
        ref_segments, ref_fallback = self._reference_segments(data)
        message: list[dict[str, Any]] = list(ref_segments)
        if content or not ref_segments:
            message.append({"type": "text", "data": {"text": content}})
        if not content:
            content = ref_fallback
        self.ad.credentials.cache_passive(
            user_openid, msg_id, PASSIVE_WINDOW_C2C, PASSIVE_MAX_SEQ_C2C
        )
        # 记录消息归属（C2C 撤回接口 /v2/users/{u}/messages/{id} 按此反查目标）
        self.ad.remember_msg_scope(msg_id, "private", user_openid)

        message += await self._attachment_segments("private", user_openid, msg_id, data)

        pack = self._base_pack(
            post_type="message",
            message_type="private",
            sub_type="friend",
            message_id=msg_id,
            user_id=user_openid,
            message=message,
            raw_message=content,
            font=0,  # OneBot v11 标准字段：官方事件不提供字体信息
            sender={"user_id": user_openid, "nickname": nickname, "card": ""},
        )
        self.ad._emit_pack(pack)

    # ------------------------------------------------------------ 频道事件
    async def _emit_guild_message(self, data: dict[str, Any]) -> None:
        """频道消息（AT_MESSAGE_CREATE 1<<30 / 私域 MESSAGE_CREATE 1<<9）→ OneBot 群消息。

        两者载荷相同；channel_id 作 group_id，domain="guild" 标记频道域，
        下游按群列表隔离。
        """
        msg_id = str(data.get("id") or "")
        channel_id = str(data.get("channel_id") or "")
        if not msg_id or not channel_id:
            return
        author = data.get("author") or {}
        user_id = str(author.get("id") or "")
        nickname = str(author.get("username") or "").strip() or user_id[:8]
        content = plain_content(data.get("content"))
        message: list[dict[str, Any]] = [{"type": "text", "data": {"text": content}}]
        message += await self._attachment_segments("group", channel_id, msg_id, data)
        pack = self._base_pack(
            post_type="message",
            message_type="group",
            sub_type="normal",
            message_id=msg_id,
            group_id=channel_id,
            user_id=user_id,
            anonymous=None,
            message=message,
            raw_message=content,
            font=0,
            sender={"user_id": user_id, "nickname": nickname, "card": ""},
            domain="guild",  # 频道域：区别于 QQ 群（official/qq）
            guild_id=str(data.get("guild_id") or ""),
        )
        self.ad._emit_pack(pack)

    async def _emit_guild_direct_message(self, data: dict[str, Any]) -> None:
        """频道私信（DIRECT_MESSAGE_CREATE，1<<12）→ OneBot 私聊消息。"""
        msg_id = str(data.get("id") or "")
        author = data.get("author") or {}
        user_id = str(author.get("id") or "")
        if not msg_id or not user_id:
            return
        nickname = str(author.get("username") or "").strip() or user_id[:8]
        content = plain_content(data.get("content"))
        message: list[dict[str, Any]] = [{"type": "text", "data": {"text": content}}]
        message += await self._attachment_segments("user", user_id, msg_id, data)
        pack = self._base_pack(
            post_type="message",
            message_type="private",
            sub_type="friend",
            message_id=msg_id,
            user_id=user_id,
            message=message,
            raw_message=content,
            font=0,
            sender={"user_id": user_id, "nickname": nickname, "card": ""},
            domain="guild",
            guild_id=str(data.get("guild_id") or ""),
            src_guild_id=str(data.get("src_guild_id") or ""),
        )
        self.ad._emit_pack(pack)

    def _emit_guild_recall(self, data: dict[str, Any], friend: bool) -> None:
        """频道消息撤回（PUBLIC_MESSAGE_DELETE / MESSAGE_DELETE / DIRECT_MESSAGE_DELETE）。

        官方载荷含 message.id / message.author（发送者）、operator（操作者）。
        OneBot v11 规范：group_recall.user_id=消息发送者、operator_id=操作者；
        friend_recall.user_id=好友（即消息对方，发送者缺失时回退操作者）。
        """
        message = data.get("message") or {}
        if not isinstance(message, dict):
            message = {}
        msg_id = str(message.get("id") or "")
        author = message.get("author") or {}
        if not isinstance(author, dict):
            author = {}
        author_id = str(author.get("id") or "")
        operator = data.get("operator") or {}
        if not isinstance(operator, dict):
            operator = {}
        op_id = str(operator.get("id") or "")
        pack = self._base_pack(
            post_type="notice",
            notice_type="friend_recall" if friend else "group_recall",
            user_id=author_id or op_id,  # OneBot 语义：消息发送者
            message_id=msg_id,
            domain="guild",
            raw=data,
        )
        if friend:
            # 好友撤回规范无 operator_id；发送者缺失（自撤回等）回退操作者
            if not author_id:
                pack["user_id"] = op_id
        else:
            pack["operator_id"] = op_id  # OneBot 语义：撤回操作者
            pack["group_id"] = str(data.get("channel_id") or message.get("channel_id") or "")
        self.ad._emit_pack(pack)

    def _emit_group_member_event(self, data: dict[str, Any], joined: bool) -> None:
        """QQ 群成员进退群（GROUP_MEMBER_ADD / GROUP_MEMBER_REMOVE，属 1<<25）。

        OneBot v11 语义 group_increase（approve）/ group_decrease（leave），
        user_id=进/退群成员，operator_id 官方未提供时留空，raw 保留原文。
        """
        group_openid = str(data.get("group_openid") or "")
        member = str(data.get("member_openid") or data.get("openid") or "")
        if not group_openid or not member:
            return
        # 进退群事件同样用于发现群（冷群可能长期无消息，仅靠消息学习不完整）
        self.ad.remember_group(group_openid)
        pack = self._base_pack(
            post_type="notice",
            notice_type="group_increase" if joined else "group_decrease",
            sub_type="approve" if joined else "leave",
            group_id=group_openid,
            user_id=member,
            operator_id=str(data.get("op_member_openid") or ""),
            raw=data,
        )
        self.ad._emit_pack(pack)

    def _emit_group_join_request(self, data: dict[str, Any]) -> None:
        """用户申请加群（GROUP_JOIN_REQUEST）→ OneBot v11 request.group.add。

        flag=join_request_id（审批时经 set_group_add_request 回传官方接口）；
        comment 拼装昵称/来源/验证信息供人读；raw 保留官方原文。
        同时缓存 flag → (group_openid, member_openid) 供审批接口定位目标。
        """
        group_openid = str(data.get("group_openid") or "")
        member = str(data.get("member_openid") or "")
        flag = str(data.get("join_request_id") or "")
        if not group_openid or not member or not flag:
            return
        self.ad.remember_join_request(flag, group_openid, member)
        verify = data.get("verify_info") or {}
        if not isinstance(verify, dict):
            verify = {}
        parts: list[str] = [str(data.get("username") or member)]
        source = str(data.get("apply_source") or "")
        if source == "invited":
            parts.append(_t("qqofficial.join_request_invited", inviter=str(data.get("invited_by") or "")))
        message = str(verify.get("verify_message") or "")
        if message:
            parts.append(message)
        qa = verify.get("review_qa_list") or []
        if isinstance(qa, list):
            for item in qa:
                if isinstance(item, dict) and item.get("question"):
                    parts.append(f"Q:{item.get('question')} A:{item.get('answer') or ''}")
        pack = self._base_pack(
            post_type="request",
            request_type="group",
            sub_type="add",
            group_id=group_openid,
            user_id=member,
            comment=" | ".join(parts),
            flag=flag,
            raw=data,
        )
        self.ad._emit_pack(pack)

    def _emit_guild_member_change(self, data: dict[str, Any], joined: bool) -> None:
        """频道成员进退（GUILD_MEMBER_ADD / GUILD_MEMBER_REMOVE，1<<1 特权）。

        频道两级结构：group_id 取 guild_id，channel 信息在 raw 中保留。
        """
        guild_id = str(data.get("guild_id") or "")
        user = data.get("user") or {}
        if not isinstance(user, dict):
            user = {}
        user_id = str(user.get("id") or "")
        if not guild_id or not user_id:
            return
        pack = self._base_pack(
            post_type="notice",
            notice_type="group_increase" if joined else "group_decrease",
            sub_type="approve" if joined else "leave",
            group_id=guild_id,
            user_id=user_id,
            operator_id=str(data.get("op_user_id") or ""),
            domain="guild",
            raw=data,
        )
        self.ad._emit_pack(pack)

    def _emit_friend_change(self, data: dict[str, Any], notice_type: str) -> None:
        """C2C 好友添加 / 删除（openid 维度）。"""
        openid = str(data.get("openid") or "")
        if not openid:
            return
        pack = self._base_pack(
            post_type="notice",
            notice_type=notice_type,
            user_id=openid,
        )
        self.ad._emit_pack(pack)
