"""OneBot v11 API 动作数据包构建器；每个函数返回 ``{action, params, echo}`` 字典。"""

from __future__ import annotations

from typing import Any


def _pack(action: str, params: dict[str, Any], echo: str | None = None) -> dict[str, Any]:
    # 浅拷贝 params：调用方在异步等待回执期间改写原字典会污染已入队待发的包
    pack: dict[str, Any] = {"action": action, "params": dict(params)}
    if echo is not None:
        pack["echo"] = echo
    return pack


def build(action: str, params: dict[str, Any] | None = None, echo: str | None = None) -> dict[str, Any]:
    """通用 OneBot action 包构建器（透传任意动作，支持未来新增的扩展 API）"""
    return _pack(action, params or {}, echo)


def group_message(group_id: int, message: Any, echo: str | None = None) -> dict[str, Any]:
    return _pack("send_group_msg", {"group_id": group_id, "message": message}, echo)


def private_message(user_id: int, message: Any, echo: str | None = None) -> dict[str, Any]:
    return _pack("send_private_msg", {"user_id": user_id, "message": message}, echo)


def private_forward_message(user_id: int, messages: Any, echo: str | None = None) -> dict[str, Any]:
    return _pack("send_private_forward_msg", {"user_id": user_id, "messages": messages}, echo)


def forward_message(message_id: str, echo: str | None = None) -> dict[str, Any]:
    """获取合并转发消息内容"""
    return _pack("get_forward_msg", {"message_id": message_id}, echo)


def mark_msg_as_read(message_id: int) -> dict[str, Any]:
    """标记消息已读（NapCat 扩展）"""
    return _pack("mark_msg_as_read", {"message_id": message_id})


def group_forward_message(group_id: int, messages: Any, echo: str | None = None) -> dict[str, Any]:
    return _pack("send_group_forward_msg", {"group_id": group_id, "messages": messages}, echo)


def delete_message(message_id: int) -> dict[str, Any]:
    return _pack("delete_msg", {"message_id": message_id})


def get_message(message_id: int, echo: str | None = None) -> dict[str, Any]:
    return _pack("get_msg", {"message_id": message_id}, echo)


def group_ban(group_id: int, user_id: int, duration: int) -> dict[str, Any]:
    return _pack(
        "set_group_ban",
        {"group_id": group_id, "user_id": user_id, "duration": duration},
    )


def group_whole_ban(group_id: int, enable: bool) -> dict[str, Any]:
    return _pack("set_group_whole_ban", {"group_id": group_id, "enable": enable})


def group_kick(group_id: int, user_id: int, reject_add_request: bool = False) -> dict[str, Any]:
    return _pack(
        "set_group_kick",
        {"group_id": group_id, "user_id": user_id, "reject_add_request": reject_add_request},
    )


def group_leave(group_id: int, is_dismiss: bool = False) -> dict[str, Any]:
    return _pack("set_group_leave", {"group_id": group_id, "is_dismiss": is_dismiss})


def group_name(group_id: int, name: str) -> dict[str, Any]:
    return _pack("set_group_name", {"group_id": group_id, "group_name": name})


def group_card(group_id: int, user_id: int, card: str) -> dict[str, Any]:
    return _pack("set_group_card", {"group_id": group_id, "user_id": user_id, "card": card})


def group_admin(group_id: int, user_id: int, enable: bool) -> dict[str, Any]:
    return _pack("set_group_admin", {"group_id": group_id, "user_id": user_id, "enable": enable})


def group_special_title(group_id: int, user_id: int, title: str, duration: int = -1) -> dict[str, Any]:
    return _pack(
        "set_group_special_title",
        {"group_id": group_id, "user_id": user_id, "special_title": title, "duration": duration},
    )


def group_anonymous_ban(group_id: int, anonymous_flag: str, duration: int) -> dict[str, Any]:
    return _pack(
        "set_group_anonymous_ban",
        {"group_id": group_id, "anonymous_flag": anonymous_flag, "duration": duration},
    )


def group_sign(group_id: int) -> dict[str, Any]:
    """群打卡（NapCat / go-cqhttp 扩展）"""
    return _pack("send_group_sign", {"group_id": group_id})


def group_poke(group_id: int, user_id: int) -> dict[str, Any]:
    """群内戳一戳（NapCat 扩展）"""
    return _pack("group_poke", {"group_id": group_id, "user_id": user_id})


def friend_poke(user_id: int) -> dict[str, Any]:
    """好友戳一戳（NapCat 扩展）"""
    return _pack("friend_poke", {"user_id": user_id})


def essence_msg(message_id: int) -> dict[str, Any]:
    """设精华消息"""
    return _pack("set_essence_msg", {"message_id": message_id})


def delete_essence_msg(message_id: int) -> dict[str, Any]:
    return _pack("delete_essence_msg", {"message_id": message_id})


def group_msg_emoji_like(message_id: int, emoji_id: str) -> dict[str, Any]:
    """群消息贴表情回应（NapCat 扩展）"""
    return _pack("set_msg_emoji_like", {"message_id": message_id, "emoji_id": emoji_id})


# 别名：适配器层以动作名 set_msg_emoji_like 引用
set_msg_emoji_like = group_msg_emoji_like


def group_member_list(group_id: int, echo: str | None = None) -> dict[str, Any]:
    return _pack("get_group_member_list", {"group_id": group_id}, echo)


def group_member_info(
    group_id: int, user_id: int, no_cache: bool = False, echo: str | None = None
) -> dict[str, Any]:
    # no_cache 默认 False：与 stranger_info / group_info 及 OneBot v11 规范对齐，
    # 避免高频调用（如入群欢迎查名片）每次强制穿透协议端缓存
    return _pack(
        "get_group_member_info",
        {"group_id": group_id, "user_id": user_id, "no_cache": no_cache},
        echo,
    )


def stranger_info(user_id: int, no_cache: bool = False, echo: str | None = None) -> dict[str, Any]:
    return _pack("get_stranger_info", {"user_id": user_id, "no_cache": no_cache}, echo)


def friend_info(user_id: int, no_cache: bool = False, echo: str | None = None) -> dict[str, Any]:
    # NapCat / go-cqhttp 扩展接口
    return _pack("get_friend_info", {"user_id": user_id, "no_cache": no_cache}, echo)


def login_info(echo: str | None = None) -> dict[str, Any]:
    return _pack("get_login_info", {}, echo)


def group_info(group_id: int, no_cache: bool = False, echo: str | None = None) -> dict[str, Any]:
    return _pack("get_group_info", {"group_id": group_id, "no_cache": no_cache}, echo)


def group_list(echo: str | None = None) -> dict[str, Any]:
    return _pack("get_group_list", {}, echo)


def friend_list(echo: str | None = None) -> dict[str, Any]:
    return _pack("get_friend_list", {}, echo)


def group_honor_info(group_id: int, honor_type: str = "all", echo: str | None = None) -> dict[str, Any]:
    return _pack("get_group_honor_info", {"group_id": group_id, "type": honor_type}, echo)


def version_info(echo: str | None = None) -> dict[str, Any]:
    return _pack("get_version_info", {}, echo)


def status_info(echo: str | None = None) -> dict[str, Any]:
    return _pack("get_status", {}, echo)


def image_info(file: str, echo: str | None = None) -> dict[str, Any]:
    """获取图片信息/下载链接"""
    return _pack("get_image", {"file": file}, echo)


def record_info(file: str, out_format: str = "mp3", echo: str | None = None) -> dict[str, Any]:
    """获取语音文件（转码）"""
    return _pack("get_record", {"file": file, "out_format": out_format}, echo)


def group_add_request(flag: str, sub_type: str, approve: bool, reason: str = "") -> dict[str, Any]:
    return _pack(
        "set_group_add_request",
        {"flag": flag, "sub_type": sub_type, "approve": approve, "reason": reason},
    )


def friend_add_request(flag: str, approve: bool) -> dict[str, Any]:
    return _pack("set_friend_add_request", {"flag": flag, "approve": approve})


def send_like(user_id: int, times: int = 1) -> dict[str, Any]:
    return _pack("send_like", {"user_id": user_id, "times": times})


def group_root_files(group_id: int, file_count: int = 50, echo: str | None = None) -> dict[str, Any]:
    return _pack("get_group_root_files", {"group_id": group_id, "file_count": file_count}, echo)


def group_files_by_folder(
    group_id: int, folder_id: str, file_count: int = 50, echo: str | None = None
) -> dict[str, Any]:
    return _pack(
        "get_group_files_by_folder",
        {"group_id": group_id, "folder_id": folder_id, "file_count": file_count},
        echo,
    )


def group_file_url(group_id: int, file_id: str, echo: str | None = None) -> dict[str, Any]:
    return _pack("get_group_file_url", {"group_id": group_id, "file_id": file_id}, echo)


def group_file_system_info(group_id: int, echo: str | None = None) -> dict[str, Any]:
    return _pack("get_group_file_system_info", {"group_id": group_id}, echo)


def upload_group_file(
    group_id: int, file: str, name: str, folder_id: str | None = None
) -> dict[str, Any]:
    params: dict[str, Any] = {"group_id": group_id, "file": file, "name": name}
    if folder_id:
        params["folder_id"] = folder_id
    return _pack("upload_group_file", params)


def upload_private_file(user_id: int, file: str, name: str) -> dict[str, Any]:
    return _pack("upload_private_file", {"user_id": user_id, "file": file, "name": name})


def delete_group_file(group_id: int, file_id: str) -> dict[str, Any]:
    return _pack("delete_group_file", {"group_id": group_id, "file_id": file_id})


def create_group_file_folder(group_id: int, name: str) -> dict[str, Any]:
    return _pack("create_group_file_folder", {"group_id": group_id, "folder_name": name})


def delete_group_file_folder(group_id: int, folder_id: str) -> dict[str, Any]:
    return _pack("delete_group_folder", {"group_id": group_id, "folder_id": folder_id})
