"""群内管理员命令：@ 批量添加管理员。

命令：``/添加管理员 @甲 @乙 @丙``（繁体 / 英文变体同样接受，不区分
大小写）。仅现有管理员可用；被 @ 的用户写入**来源适配器**的管理员列表
（自动去重）：个人号域写入 QQ 号，官方域写入成员 openid（@ 提及由官方
翻译层统一转成 OneBot at 段）。

权限边界：发送者必须已是该适配器管理员——普通群员无法自我提权；
上限与合法性由 ConnectionManager 校验。写入即时生效（update 自动失效
管理员键缓存），无需重载连接。
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from ..i18n import t as _t

if TYPE_CHECKING:
    from ..plugin import LumenBridgePlugin

# 添加管理员命令前缀：全语言变体常驻接受；匹配不区分大小写
_ADD_ADMIN_COMMANDS = ("/添加管理员", "/添加管理員", "/addadmin")

# raw_message 中的 @ CQ 码（兜底解析：协议端只给字符串消息时用）
_CQ_AT_QQ_RE = re.compile(r"\[CQ:at,qq=([^\],\s&#]+)\]")

# OneBot at 段的“全体成员”特殊值，不作为可添加的标识
_AT_ALL = "all"


def _message_text(pack: dict[str, Any]) -> str:
    """提取消息可比对纯文本（text 段拼接；@ 段天然分离不干扰命令识别）。"""
    segments = pack.get("message")
    if isinstance(segments, list):
        parts = [
            str((seg.get("data") or {}).get("text") or "")
            for seg in segments
            if isinstance(seg, dict) and seg.get("type") == "text"
        ]
        return "".join(parts).strip()
    return str(pack.get("raw_message", "") or "").strip()


def extract_at_ids(pack: dict[str, Any]) -> list[str]:
    """提取消息中被 @ 的用户标识（QQ 号 / openid），保序去重。

    优先走 message 段列表，回退解析 @ CQ 码；排除“全体成员”与机器人自身。
    """
    self_id = str(pack.get("self_id") or "")
    found: list[str] = []
    segments = pack.get("message")
    if isinstance(segments, list):
        for seg in segments:
            if isinstance(seg, dict) and seg.get("type") == "at":
                qq = str((seg.get("data") or {}).get("qq") or "").strip()
                if qq and qq != _AT_ALL and qq != self_id:
                    found.append(qq)
    if not found:
        raw = str(pack.get("raw_message", "") or "")
        for match in _CQ_AT_QQ_RE.finditer(raw):
            qq = match.group(1).strip()
            if qq and qq != _AT_ALL and qq != self_id:
                found.append(qq)
    return list(dict.fromkeys(found))


class AdminCommandModule:
    """群内管理员命令处理（/添加管理员 @…）。"""

    def __init__(self, plugin: "LumenBridgePlugin") -> None:
        self.plugin = plugin
        self.logger = getattr(plugin, "_tee_logger", None) or plugin.logger
        self.bus = plugin.bus
        # 不叠加群过滤：group_allowed 对未配置群列表的适配器放行任意群，
        # 权限由发送者是否为该适配器管理员兜底
        self.bus.on("message.group.normal", self._on_group_message)

    def _on_group_message(self, pack: dict[str, Any], reply: Any) -> None:
        text = _message_text(pack)
        # 快速预筛：命令必以 / 开头，绝大多数群消息零成本跳过
        if not text or not text.startswith("/"):
            return
        lowered = text.casefold()
        for cmd in _ADD_ADMIN_COMMANDS:
            if lowered.startswith(cmd.casefold()):
                self._handle_add_admin(pack, reply)
                return

    # ---------------------------------------------------------------- 添加
    def _handle_add_admin(self, pack: dict[str, Any], reply: Any) -> None:
        adapter_id = str(pack.get("_lumen_adapter_id", "") or "")
        if not adapter_id:
            return
        connections = getattr(self.plugin, "connections", None)
        adapter = connections.get(adapter_id) if connections is not None else None
        if adapter is None:
            return

        # 权限：发送者必须已是该适配器的管理员（普通群员无法提权）
        sender = pack.get("sender")
        if not isinstance(sender, dict):
            sender = {}
        user_key = str(sender.get("user_id") or pack.get("user_id") or "").strip()
        admins = connections.parse_groups_loose(adapter.get("admin_qq"))
        if not user_key or user_key not in admins:
            self.logger.warning(
                _t(
                    "admincmd.log.denied",
                    user=user_key or "?",
                    name=adapter.get("name") or adapter_id,
                )
            )
            reply(_t("admincmd.reply.permission_denied"), True)
            return

        targets = extract_at_ids(pack)
        if not targets:
            reply(_t("admincmd.reply.format_hint", command=_t("admincmd.command_word")), True)
            return

        added = [qq for qq in targets if qq not in admins]
        if not added:
            reply(_t("admincmd.reply.already_admin"), True)
            return
        try:
            connections.update(adapter_id, {"admin_qq": admins + added})
        except Exception as exc:  # noqa: BLE001 - 校验失败（如超上限）原样回执
            reply(_t("admincmd.reply.add_failed", error=exc), True)
            return
        self.logger.info(
            _t(
                "admincmd.log.added",
                name=adapter.get("name") or adapter_id,
                users=", ".join(added),
            )
        )
        reply(_t("admincmd.reply.add_success", count=len(added), users=", ".join(added)), True)
