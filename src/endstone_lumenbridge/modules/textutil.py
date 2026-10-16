"""群命令文本解析共享工具：OneBot 事件包 → 可比对纯文本 + 命令前缀匹配。"""

from __future__ import annotations

import re
from typing import Any

# raw_message 中的 @ CQ 码：剥离后「@机器人 指令 参数」与「指令 参数」等价
_CQ_AT_RE = re.compile(r"\[CQ:at,[^\]]*\]\s*")


def join_text_segments(segments: Any) -> str:
    """拼接消息段列表的 text 部分，畸形段跳过。"""
    if not isinstance(segments, list):
        return ""
    parts = [
        str((seg.get("data") or {}).get("text") or "")
        for seg in segments
        if isinstance(seg, dict) and seg.get("type") == "text"
    ]
    return "".join(parts)


def message_text(pack: dict[str, Any], *, strip_at: bool = False) -> str:
    """群消息可比对纯文本：优先 text 段拼接，回退 raw_message（可剥离 @ CQ 码）。"""
    segments = pack.get("message")
    if isinstance(segments, list):
        return join_text_segments(segments).strip()
    raw = str(pack.get("raw_message", "") or "")
    if strip_at:
        raw = _CQ_AT_RE.sub("", raw)
    return raw.strip()


def match_command_prefix(text: str, prefixes: tuple[str, ...]) -> str | None:
    """命令快速预筛：非 / 开头零成本跳过；命中返回对应前缀（不区分大小写）。"""
    if not text or not text.startswith("/"):
        return None
    lowered = text.casefold()
    for prefix in prefixes:
        if lowered.startswith(prefix.casefold()):
            return prefix
    return None
