"""QQ 官方机器人工具函数：HTTP 错误封装、业务错误码提取、消息内容解析。

从适配器主文件拆出，供 translate / sender / 主适配器共用，无外部依赖。
"""

from __future__ import annotations

import base64
import re
from pathlib import Path
from typing import Any

from .constants import LOCAL_MEDIA_MAX, MEDIA_FILE_TYPE

# <@!xxx> / <@xxx> at 机器人的内联标记
MENTION_RE = re.compile(r"<@!?[A-Za-z0-9_=-]+>\s*")
# 出站 @ 标记（extract_payload 生成）：官方仅在 markdown（msg_type=2）中
# 解析为真实提及，sender 据此切换消息载体
OUT_MENTION_RE = re.compile(r"<@!?[A-Za-z0-9_=-]+>")
# QQ 官方表情标记 <faceType=1,id=...,ext="..."> → [表情]
FACE_RE = re.compile(r"<faceType=\d+[^>]*>")


class ApiHTTPError(RuntimeError):
    """官方 REST 接口 HTTP 错误；code 属性为 HTTP 状态码，供 401 等精确判断。"""

    def __init__(self, code: int, detail: str = "") -> None:
        super().__init__(f"HTTP {code} {detail}".strip())
        self.code = int(code)
        self.detail = str(detail)


_BIZ_CODE_RE = re.compile(r'"code"\s*:\s*(-?\d+)')


def biz_code(err: ApiHTTPError) -> int:
    """从 HTTP 错误响应体提取官方业务错误码（如 22009 主动消息被拒）。"""
    match = _BIZ_CODE_RE.search(err.detail or "")
    return int(match.group(1)) if match else 0


def plain_content(content: Any) -> str:
    """清理官方消息 content：去掉 @机器人 标记与表情标记，压缩空白。"""
    text = str(content or "")
    text = MENTION_RE.sub("", text)
    text = FACE_RE.sub("[表情]", text)
    return text.strip()


# content 内联标记：@提及（含其尾随空白，供剥离 bot 触发标记用）或表情标记
_INLINE_TOKEN_RE = re.compile(r"<@!?([A-Za-z0-9_=-]+)>"
                               r"(\s*)|<faceType=\d+[^>]*>")


def content_segments(content: Any, self_id: str = "") -> tuple[list[dict[str, Any]], str]:
    """把官方 content 按原始顺序解析为 (text/at 交错的消息段列表, 纯文本)。

    - @机器人自身 的触发标记连同其后空白一并剥离（不应进入转发内容）；
    - @其他成员 **就地** 转 at 段：此前实现先剥掉全部 @ 标记、再把 at 段
      统一追加到消息末尾，导致 "你好@xxx你好呀" 被转发成 "你好你好呀@xxx"；
    - 表情标记转 [表情] 文本。
    纯文本不含任何 @ 标记（与 plain_content 一致），供 raw_message 使用。
    """
    text = str(content or "")
    segments: list[dict[str, Any]] = []
    plain_parts: list[str] = []
    buf: list[str] = []

    def _flush() -> None:
        joined = "".join(buf)
        buf.clear()
        if joined:  # 空串不产生 text 段（如消息以 @bot 触发标记开头）
            segments.append({"type": "text", "data": {"text": joined}})
            plain_parts.append(joined)

    pos = 0
    for m in _INLINE_TOKEN_RE.finditer(text):
        buf.append(text[pos:m.start()])  # 标记之前的普通文本
        if m.group(1) is not None:  # @提及：先收口前文再插入 at 段（顺序保持的关键）
            _flush()
            uid = m.group(1) or ""
            if uid and uid != str(self_id or ""):
                segments.append({"type": "at", "data": {"qq": uid}})
                ws = m.group(2) or ""
                if ws:
                    buf.append(ws)  # 保持 @ 后原有间距
            # @机器人自身：token 与其后空白一并丢弃
        else:  # 表情标记：并入当前文本流（与旧 plain_content 行为一致）
            buf.append("[表情]")
        pos = m.end()
    buf.append(text[pos:])
    _flush()
    return segments, "".join(plain_parts).strip()


def mention_segments(content: Any) -> list[dict[str, Any]]:
    """提取官方消息中的 <@!xxx> / <@xxx> 提及为 at 消息段（id 即 openid/AppID）。"""
    text = str(content or "")
    segments: list[dict[str, Any]] = []
    for match in re.finditer(r"<@!?([A-Za-z0-9_=-]+)>", text):
        segments.append({"type": "at", "data": {"qq": match.group(1)}})
    return segments


def read_local_media(path: str) -> bytes | None:
    """读取本地媒体文件（file:/// 前缀 / 绝对路径 / base64:// 内联）；超限返回 None。

    base64:// 是 OneBot v11 标准内联格式（msgbuilder.image(bytes) 即生成它），
    个人号协议端原生支持；官方域必须在本地解码后走 files 接口的 file_data
    通道上传，此前未处理导致 base64 图片被静默丢弃（无日志无报错）。
    """
    if path.startswith("base64://"):
        try:
            blob = base64.b64decode(path[len("base64://"):], validate=False)
        except (ValueError, TypeError):
            return None
        return blob if 0 < len(blob) <= LOCAL_MEDIA_MAX else None
    if path.startswith("file://"):
        path = path[7:]
    if path.startswith(("http://", "https://")):
        return None
    try:
        data = Path(path).read_bytes()
    except OSError:
        return None
    return data if 0 < len(data) <= LOCAL_MEDIA_MAX else None


def extract_payload(message: Any) -> tuple[str, dict[str, Any] | None]:
    """把 OneBot 消息统一为 (纯文本, 富媒体描述)。

    富媒体描述：{"type": "image"|"video"|"record", "url": str|None, "data": bytes|None}
    首个富媒体段生效（官方单条消息仅支持一个媒体）。
    """
    # 单个消息段 dict（如 {"type": "text", ...}）等价于单元素列表，
    # 否则会走 str(dict) 分支把整段序列化成 repr 字符串发给用户
    if isinstance(message, dict):
        message = [message]
    if isinstance(message, str) or not isinstance(message, (list, tuple)):
        return str(message or ""), None
    parts: list[str] = []
    media: dict[str, Any] | None = None
    for seg in message:
        if isinstance(seg, str):
            parts.append(seg)
            continue
        if not isinstance(seg, dict):
            continue
        stype = str(seg.get("type") or "")
        data = seg.get("data") or {}
        if stype == "text":
            parts.append(str(data.get("text", "")))
        elif stype == "at":
            # 官方 <@!openid> 内联标记仅在 markdown 消息（msg_type=2）中
            # 渲染为真实 @（纯文本会原样显示标记）；sender 检测到该标记后
            # 自动切换 markdown 载体发送。"all"（@全体）官方不支持，丢弃
            at_id = str(data.get("qq") or "").strip()
            if at_id and at_id != "all":
                parts.append(f"<@!{at_id}>")
        elif stype in MEDIA_FILE_TYPE and media is None:
            url = str(data.get("url") or "").strip()
            file_ref = str(data.get("file") or data.get("path") or "").strip()
            if url.startswith(("http://", "https://")):
                media = {"type": stype, "url": url, "data": None}
            elif file_ref.startswith(("http://", "https://")):
                media = {"type": stype, "url": file_ref, "data": None}
            else:
                blob = read_local_media(file_ref or url)
                if blob is not None:
                    media = {"type": stype, "url": None, "data": blob}
    return "".join(parts), media


def content_of(message: Any) -> str:
    """仅取 OneBot 消息的纯文本部分（丢弃富媒体）。"""
    text, _media = extract_payload(message)
    return text
