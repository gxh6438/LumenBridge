"""OneBot v11 消息段构建器，提供 format() 统一格式化入口与合并转发构建器。"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any, Iterable

Segment = dict[str, Any]

# 本地图片根目录白名单（默认空 = 禁止读本地文件）：防止用户可控的消息
# 变量拼出任意路径，把本地文件 base64 外发造成信息泄露
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
    """支持本地路径 / URL / base64 / bytes 的图片消息段。

    本地路径仅在白名单内读取，白名单外按原始字符串透传协议端，防任意文件读取。
    sub_type：0 普通图 / 1 表情图（OneBot v11 扩展）。
    """
    if isinstance(file, bytes):
        file = "base64://" + base64.b64encode(file).decode()
    elif isinstance(file, str):
        p = _resolve_under_roots(file)
        if p is not None:
            try:
                # 限制本地图片大小防内存耗尽
                if p.stat().st_size > 10 * 1024 * 1024:  # 10MB
                    file = str(file)  # 过大时回退为原始字符串
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


def button(
    label: str,
    data: str = "",
    *,
    id: str = "",
    style: int = 1,
    visited_label: str = "",
    permission: int | dict | None = None,
    action_type: int = 1,
    url: str = "",
    reply: bool = False,
    enter: bool = False,
) -> dict:
    """官方域按钮节点便捷构造（配合 keyboard() 使用）。

    官方必填字段（visited_label / data / permission / unsupport_tips）缺省
    时自动补全：visited_label 回填 label，permission 默认 {"type": 2}（所有
    人可操作）。action_type：1 回调按钮（点击触发 notice.interaction_create，
    data 为回传数据）/ 2 指令按钮（点击在用户输入框插入 @机器人 data，
    enter=True 直接送出、reply=True 引用本消息）/ 0 跳转按钮（需 url）。
    style：0 灰色线框 / 1 蓝色线框。permission int 直接作为官方 type
    （0 指定用户 1 仅管理者 2 所有人 3 身份组），dict 原样透传。
    """
    node: dict = {
        "render_data": {
            "label": str(label),
            "visited_label": str(visited_label or label),
            "style": int(style),
        },
        "action": {
            "type": int(action_type),
            "permission": (
                {"type": int(permission)}
                if isinstance(permission, int)
                else (permission if permission is not None else {"type": 2})
            ),
            "data": str(data),
            "unsupport_tips": "当前客户端版本暂不支持此操作",
        },
    }
    if id:
        node["id"] = str(id)
    if url:
        node["action"]["url"] = str(url)
    if action_type == 2:
        # 指令按钮专属字段（客户端 8983+）：reply 引用本消息 / enter 直接发送
        if reply:
            node["action"]["reply"] = True
        if enter:
            node["action"]["enter"] = True
    return node


def keyboard(payload: dict | str | list) -> Segment:
    """按钮键盘消息段（LumenBridge 官方域扩展；个人号域协议端会忽略未知段）。

    payload 三种形态：
    - str：开放平台后台预配置的按钮模板 ID（官方 keyboard.id）；
    - list：便捷行列表 [[button(...), ...], ...]，自动包装为官方 rows 结构；
    - dict：完整官方 keyboard.content 结构（如 {"rows": [...]}）原样透传。
    """
    if isinstance(payload, str):
        data: dict = {"id": payload.strip()}
    elif isinstance(payload, list):
        rows = [{"buttons": [b if isinstance(b, dict) else dict(b) for b in row]} for row in payload]
        data = {"content": {"rows": rows}}
    elif isinstance(payload, dict):
        data = {"content": payload}
    else:
        data = {"content": {"rows": []}}
    return {"type": "keyboard", "data": data}


def format_message(msg: Any) -> list[Segment]:
    """统一格式化入口：字符串自动转 text 段，单段自动包装为列表。

    None / 非法类型的段直接丢弃，避免序列化出 null 段被协议端拒绝。
    """
    if not isinstance(msg, list):
        msg = [msg]
    result: list[Segment] = []
    for seg in msg:
        if isinstance(seg, str):
            result.append(text(seg))
        elif isinstance(seg, dict):
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
