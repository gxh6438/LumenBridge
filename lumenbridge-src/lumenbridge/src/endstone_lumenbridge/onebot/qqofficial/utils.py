"""QQ 官方机器人工具函数：HTTP 错误封装、业务错误码提取、消息内容解析。

供 translate / sender / 主适配器共用，无外部依赖。
"""

from __future__ import annotations

import base64
import re
from pathlib import Path
from typing import Any

from .constants import LOCAL_MEDIA_MAX, MEDIA_FILE_TYPE

# 入站：官方推送的 @ 内联标记（旧协议 <@!openid> / <@openid>）
MENTION_RE = re.compile(r"<@!?[A-Za-z0-9_=-]+>\s*")
# 入站：官方新文本链 <qqbot-at-user id="openid"/>。官方文档（文本交互）已公告
# 旧协议 <@userid> 即将弃用；腾讯若迁移推送格式，入站解析按此兼容
QQBOT_AT_RE = re.compile(r'<qqbot-at-user\s+id="([^"]*)"\s*/?>\s*')
# 出站 @ 标记：markdown 载体（msg_type=2）+ 官方新格式文本链
# <qqbot-at-user id="openid" /> 是经 Gensokyo-ForSpark 实测可渲染真实 @ 的
# 组合（at_markdown 功能，2026-08-19 实测：纯文本 + 文本链、markdown +
# 频道模板 <at id=""> 均显示原文；「官方开发者实测 markdown 消息可渲染
# 真 at」）。用官方最新格式，旧协议 <@userid> 弃用不受影响。markdown 被
# 拒（能力未开通）时 sender 自动降级纯文本发送（该场景 @ 不渲染，保正文）。
# 格式转换器用（不吞尾随空白，与剥离用的 MENTION_RE / QQBOT_AT_RE 区分）：
# 旧协议 <@!id> / <@id>（已弃用）、旧社区写法 <at id="id"></at> 统一归一
# 为官方文本链标准格式（自闭合、斜杠前带空格）
_QQBOT_AT_CONV_RE = re.compile(r'<qqbot-at-user\s+id="([^"]*)"\s*/?>')
_AT_MD_CONV_RE = re.compile(r'<at\s+id="([^"]*)"\s*>\s*</at>')
_AT_LEGACY_CONV_RE = re.compile(r"<@!?([A-Za-z0-9_=-]+)>")
# 出站 @ 标记检测：sender 据此切换 markdown 载体；三种写法均检测，
# 防转换器遗漏的旁路标记
OUT_MENTION_RE = re.compile(
    r'<qqbot-at-user\s+id="[^"]+"\s*/?>'
    r'|<at\s+id="[^"]+"\s*>\s*</at>'
    r'|<@!?[A-Za-z0-9_=-]+>'
)
# markdown 载体下需转义的特殊字符（@ 标记先提取占位再转义回填）
_MD_ESCAPE_RE = re.compile(r"([\\`*_{}#\[\]<>~])")
_MD_UNESCAPE_RE = re.compile(r"\\([\\`*_{}#\[\]<>~])")
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
    """清理官方消息 content：去掉 @ 标记（新旧格式）与表情标记，压缩空白。"""
    text = str(content or "")
    text = MENTION_RE.sub("", text)
    text = QQBOT_AT_RE.sub("", text)
    text = FACE_RE.sub("[表情]", text)
    return text.strip()


# content 内联标记：@提及（含其尾随空白，供剥离 bot 触发标记用）或表情标记。
# @提及兼容两种推送格式：旧协议 <@!openid> 与新官方文本链 <qqbot-at-user id=""/>
#（组 1/2 为旧协议 id 与尾空白，组 3/4 为新格式 id 与尾空白）
_INLINE_TOKEN_RE = re.compile(
    r"<@!?([A-Za-z0-9_=-]+)>"
    r"(\s*)"
    r'|<qqbot-at-user\s+id="([^"]*)"\s*/?>'
    r"(\s*)"
    r"|<faceType=\d+[^>]*>"
)


def content_segments(content: Any, self_id: str = "") -> tuple[list[dict[str, Any]], str]:
    """把官方 content 按原始顺序解析为 (text/at 交错的消息段列表, 纯文本)。

    - @机器人自身 的触发标记连同其后空白一并剥离（不应进入转发内容）；
    - @其他成员 **就地** 转 at 段，保持原始位置（"你好@xxx你好呀" 不被
      重排成 "你好你好呀@xxx"）；
    - 表情标记转 [表情] 文本。纯文本不含 @ 标记，供 raw_message 使用。
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
        if m.group(1) is not None or m.group(3) is not None:
            # @提及（旧协议 / 新官方文本链）：先收口前文再插入 at 段（顺序保持的关键）
            _flush()
            uid = m.group(1) or m.group(3) or ""
            if uid and uid != str(self_id or ""):
                segments.append({"type": "at", "data": {"qq": uid}})
                ws = m.group(2) or m.group(4) or ""
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
    """提取官方消息中的 @ 提及为 at 消息段（id 即 openid/AppID）。

    兼容旧协议 <@!xxx> / <@xxx> 与新官方文本链 <qqbot-at-user id="xxx"/>，
    复用 _INLINE_TOKEN_RE 按出现顺序统一提取（分别扫描两种格式会乱序）。
    """
    text = str(content or "")
    segments: list[dict[str, Any]] = []
    for m in _INLINE_TOKEN_RE.finditer(text):
        uid = (m.group(1) or m.group(3) or "").strip()
        if uid:
            segments.append({"type": "at", "data": {"qq": uid}})
    return segments


# 合法 openid / unionid 字符集（32 位 hex 为主，防御性放宽到 20-64 位）
_OPENID_RE = re.compile(r"[A-Fa-f0-9_-]{20,64}")


def normalize_target(target: Any) -> str:
    """规范化出站目标（群 / 用户 openid）。

    用户在 WebUI 群列表里可能填带备注的值（如 "BBC E1B33508D3DAA..."），
    带空格 / 前缀的 openid 会让 REST 路径异常（/v2/groups/BBC E1B3.../
    messages）。提取首个合法 openid 片段；无匹配时仅去空白原样返回
    （保持旧行为，交给官方侧报错暴露配置问题）。
    """
    text = str(target or "").strip()
    if not text:
        return text
    if _OPENID_RE.fullmatch(text):
        return text
    match = _OPENID_RE.search(text)
    return match.group(0) if match else text.replace(" ", "")


def normalize_at_markers(content: Any) -> str:
    """出站格式转换器：任意风格的 @ 标记统一转为官方文本链标准格式。

    <qqbot-at-user id="id" />（自闭合、斜杠前带空格）配合 markdown 载体
    是实测可渲染真实 @ 的组合（详见 OUT_MENTION_RE 注释）；旧协议与旧
    社区写法一并归一化。id 原样保留（openid / unionid 均可，由官方侧
    解析）；已是标准格式的标记不受影响（幂等）；空 id 占位标记丢弃。
    """

    def _tag(uid: str) -> str:
        return f'<qqbot-at-user id="{uid}" />' if uid else ""

    text = _QQBOT_AT_CONV_RE.sub(lambda m: _tag(m.group(1)), str(content or ""))
    text = _AT_MD_CONV_RE.sub(lambda m: _tag(m.group(1)), text)
    return _AT_LEGACY_CONV_RE.sub(lambda m: _tag(m.group(1)), text)


def escape_markdown_text(content: Any) -> str:
    """markdown 载体下转义文本的格式特殊字符，@ 标记除外。

    切到 markdown 载体后，普通文本里的 * # > _ < 等会被客户端解析成
    格式（如「绑定白名单<你的游戏ID>」的尖括号会被当 HTML 标签吞掉）。
    先把 @ 标记（官方文本链）提取为占位符，再转义其余文本，最后回填。
    """
    text = str(content or "")
    tokens: list[str] = []

    def _stash(m: "re.Match[str]") -> str:
        tokens.append(m.group(0))
        return f"\x00{len(tokens) - 1}\x00"

    stashed = _QQBOT_AT_CONV_RE.sub(_stash, text)
    escaped = _MD_ESCAPE_RE.sub(r"\\\1", stashed)
    for i, tok in enumerate(tokens):
        escaped = escaped.replace(f"\x00{i}\x00", tok)
    return escaped


def markdown_to_plain_text(content: Any) -> str:
    """markdown 载体降级纯文本通路：反转义 + @ 标记还原为可读文本。

    纯文本通道 @ 不渲染（Gensokyo 实测显示原文），官方文本链标记还原为
    @Openid前8位 可读文本（避免整段标记原样刷在群里），并反转义转义字符。
    """

    def _to_nick(m: "re.Match[str]") -> str:
        uid = m.group(1)
        return f"@Openid{uid[:8]}" if len(uid) >= 8 else ""

    text = _QQBOT_AT_CONV_RE.sub(_to_nick, str(content or ""))
    return _MD_UNESCAPE_RE.sub(r"\1", text)


def read_local_media(path: str) -> bytes | None:
    """读取本地媒体文件（file:/// 前缀 / 绝对路径 / base64:// 内联）；超限返回 None。

    base64:// 是 OneBot v11 标准内联格式（msgbuilder.image(bytes) 即生成
    它），个人号协议端原生支持；官方域须本地解码后走 files 接口的
    file_data 通道上传，否则图片会被静默丢弃（无日志无报错）。
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

    富媒体描述：{"type": "image"|"video"|"record", "url": str|None, "data":
    bytes|None}，首个富媒体段生效（官方单条消息仅支持一个媒体）。
    出站 @ 统一经 normalize_at_markers 归一为官方文本链标准格式，sender
    检测后切换 markdown 载体发送（实测可渲染组合）。
    """
    # 单个消息段 dict（如 {"type": "text", ...}）等价于单元素列表，
    # 否则会走 str(dict) 分支把整段序列化成 repr 字符串发给用户
    if isinstance(message, dict):
        message = [message]
    if isinstance(message, str) or not isinstance(message, (list, tuple)):
        return normalize_at_markers(str(message or "")), None
    parts: list[str] = []
    media: dict[str, Any] | None = None
    for seg in message:
        if isinstance(seg, str):
            parts.append(normalize_at_markers(seg))
            continue
        if not isinstance(seg, dict):
            continue
        stype = str(seg.get("type") or "")
        data = seg.get("data") or {}
        if stype == "text":
            parts.append(normalize_at_markers(str(data.get("text", ""))))
        elif stype == "at":
            # at 段 → 官方文本链 <qqbot-at-user id="openid" />（见
            # normalize_at_markers 说明，配合 markdown 载体渲染）；
            # "all"（@全体）官方不支持，丢弃
            at_id = str(data.get("qq") or "").strip()
            if at_id and at_id != "all":
                parts.append(f'<qqbot-at-user id="{at_id}" />')
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
