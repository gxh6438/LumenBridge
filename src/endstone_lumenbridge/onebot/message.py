"""OneBot v11 消息段构建器，提供 format() 统一格式化入口与合并转发构建器。"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any, Iterable

Segment = dict[str, Any]

# 允许读取本地图片的根目录白名单（默认为空 = 禁止读本地文件）。
# 防止消息变量（如 $1 来自用户输入）拼出任意路径，把服务器本地文件
# base64 后外发到 QQ 群造成信息泄露。插件启用时注册数据目录。
_local_image_roots: list[Path] = []


def set_local_image_roots(roots: Iterable[Path | str]) -> None:
    """设置允许 image() 读取本地文件的根目录列表。"""
    _local_image_roots[:] = [Path(r) for r in roots]


def _resolve_under_roots(file: str) -> Path | None:
    """路径存在且位于任一白名单根目录内时返回 Path，否则 None。"""
    try:
        p = Path(file)
        if not p.is_file():
            return None
        resolved = p.resolve()
        for root in _local_image_roots:
            try:
                resolved.relative_to(root.resolve())
                return resolved
            except (ValueError, OSError):
                continue
    except (ValueError, OSError):
        # 非法路径（空字节 / 过长文件名等）
        return None
    return None


def text(raw: str) -> Segment:
    return {"type": "text", "data": {"text": str(raw)}}


def at(qq: int | str) -> Segment:
    return {"type": "at", "data": {"qq": str(qq)}}


def face(face_id: int | str) -> Segment:
    return {"type": "face", "data": {"id": str(face_id)}}


def image(file: str | bytes, sub_type: int = 0) -> Segment:
    """支持本地路径 / URL / base64 / bytes 的图片消息段

    本地路径仅在 :func:`set_local_image_roots` 注册的白名单目录内才会被读取，
    白名单外一律按原始字符串（URL/标识符）透传给协议端，防止任意文件读取。
    sub_type 可选（0 普通图 / 1 表情图，OneBot v11 扩展），默认 0 保持兼容。
    """
    if isinstance(file, bytes):
        file = "base64://" + base64.b64encode(file).decode()
    elif isinstance(file, str):
        p = _resolve_under_roots(file)
        if p is not None:
            try:
                # 限制本地图片文件大小，避免大文件导致内存耗尽
                if p.stat().st_size > 10 * 1024 * 1024:  # 10MB
                    file = str(file)  # 过大时回退为原始字符串（URL/标识符），避免 OOM
                else:
                    file = "base64://" + base64.b64encode(p.read_bytes()).decode()
            except OSError:
                # 读文件瞬间被删除 / 权限变化，按原始字符串处理
                pass
    return {"type": "image", "data": {"file": file, "subType": sub_type}}


def reply(message_id: int | str) -> Segment:
    return {"type": "reply", "data": {"id": str(message_id)}}


def poke(qq: int | str) -> Segment:
    return {"type": "poke", "data": {"qq": str(qq)}}


def video(file: str) -> Segment:
    return {"type": "video", "data": {"file": str(file)}}


def record(file: str) -> Segment:
    return {"type": "record", "data": {"file": str(file)}}


def format_message(msg: Any) -> list[Segment]:
    """统一格式化入口：字符串自动转 text 段，单段自动包装为列表"""
    if not isinstance(msg, list):
        msg = [msg]
    result: list[Segment] = []
    for seg in msg:
        if isinstance(seg, str):
            result.append(text(seg))
        else:
            result.append(seg)
    return result


class ForwardMessageBuilder:
    """合并转发消息构建器"""

    def __init__(self) -> None:
        self._nodes: list[Segment] = []

    def add_message_by_id(self, message_id: int | str) -> "ForwardMessageBuilder":
        self._nodes.append({"type": "node", "data": {"id": str(message_id)}})
        return self

    def add_custom_message(
        self, name: str, uin: int | str, content: Any
    ) -> "ForwardMessageBuilder":
        self._nodes.append(
            {
                "type": "node",
                "data": {"name": name, "uin": str(uin), "content": format_message(content)},
            }
        )
        return self

    def build(self) -> list[Segment]:
        return list(self._nodes)


def decode_cq_entities(raw: str) -> str:
    """解码 OneBot raw_message 中的 HTML 实体"""
    return (
        raw.replace("&#91;", "[")
        .replace("&#93;", "]")
        .replace("&#44;", ",")
        .replace("&amp;", "&")
    )
