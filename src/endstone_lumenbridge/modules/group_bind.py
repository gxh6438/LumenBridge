"""群绑定密钥模块：免手填的群聊 / 管理员一键绑定。

流程：WebUI 为适配器签发一次性密钥（仅显示一次、5 分钟过期、新钥即焚旧钥）
→ 管理员把「指令 密钥」发到目标群（@ 与不@ 机器人均可）→ 验证后把当前群
与发送者写入来源适配器的身份配置（群列表 / 管理员列表自动去重）。

安全边界：密钥只存内存不落盘，进程重启自然清空；一次性消费，绑定成功
即销毁；5 分钟窗口限制泄露后的滥用时限。
"""

from __future__ import annotations

import hmac
import re
import secrets
import threading
import time
from typing import TYPE_CHECKING, Any

from ..i18n import t as _t

if TYPE_CHECKING:
    from ..plugin import LumenBridgePlugin

# 密钥有效期（秒）：到期自动销毁
KEY_TTL = 300.0
# 密钥熵：12 位十六进制（token_hex(6)），无易混淆字符，输入友好
_KEY_BYTES = 6

# 绑定指令前缀：全语言变体常驻接受，匹配不区分大小写
_BIND_COMMANDS = ("/lumen绑定", "/lumen綁定", "/lumen bind")

# raw_message 中的 @ CQ 码：剥离后「@机器人 指令 密钥」与「指令 密钥」等价
_CQ_AT_RE = re.compile(r"\[CQ:at,[^\]]*\]\s*")


def extract_bind_text(pack: dict[str, Any]) -> str:
    """提取群消息的可比对纯文本（剥离 @ 段）：优先拼 text 段，回退剥离 @ CQ 码。"""
    segments = pack.get("message")
    if isinstance(segments, list):
        parts = [
            str((seg.get("data") or {}).get("text") or "")
            for seg in segments
            if isinstance(seg, dict) and seg.get("type") == "text"
        ]
        return "".join(parts).strip()
    raw = str(pack.get("raw_message", "") or "")
    return _CQ_AT_RE.sub("", raw).strip()


class GroupBindModule:
    """群绑定密钥的签发（WebUI）与消费（群聊指令）。"""

    def __init__(self, plugin: "LumenBridgePlugin") -> None:
        self.plugin = plugin
        self.logger = getattr(plugin, "_tee_logger", None) or plugin.logger
        self.bus = plugin.bus
        self._lock = threading.RLock()
        # adapter_id → {"key": 密钥, "expires_at": 到期时刻}；签发新钥直接覆盖旧钥
        self._keys: dict[str, dict[str, Any]] = {}

        # 绑定指令不经 group_allowed 过滤：目标群尚未写入群列表时会被群过滤拦截
        self.bus.on("message.group.normal", self._on_group_message)

    # ---------------------------------------------------------------- 签发
    def issue_key(self, adapter_id: str) -> dict[str, Any] | None:
        """为适配器签发新绑定密钥（旧密钥立即销毁），明文仅本次响应返回一次。"""
        connections = getattr(self.plugin, "connections", None)
        adapter = connections.get(adapter_id) if connections is not None else None
        if adapter is None:
            return None
        key = secrets.token_hex(_KEY_BYTES)
        with self._lock:
            self._keys[adapter_id] = {
                "key": key,
                "expires_at": time.monotonic() + KEY_TTL,
            }
        self.logger.info(
            _t("groupbind.log.key_issued", name=adapter.get("name") or adapter_id)
        )
        return {
            "command": f"{_t('groupbind.command_word')} {key}",
            "key": key,
            "expires_in": int(KEY_TTL),
        }

    # ---------------------------------------------------------------- 消费
    def _pop_key(self, adapter_id: str, token: str) -> bool:
        """验证并一次性消费密钥（compare_digest 防时序侧信道）。

        比对失败保留当前密钥（打错不应连带焚毁有效密钥）；过期惰性清理。
        """
        with self._lock:
            entry = self._keys.get(adapter_id)
            if entry is None:
                return False
            if time.monotonic() > float(entry.get("expires_at", 0.0)):
                self._keys.pop(adapter_id, None)
                return False
            expected = str(entry.get("key", "")).encode("utf-8")
            provided = str(token).encode("utf-8")
            ok = hmac.compare_digest(expected, provided)
            if ok:
                self._keys.pop(adapter_id, None)
            return ok

    def _on_group_message(self, pack: dict[str, Any], reply: Any) -> None:
        text = extract_bind_text(pack)
        # 快速预筛：指令前缀必以 / 开头，绝大多数群消息零成本跳过
        if not text or not text.startswith("/"):
            return
        lowered = text.casefold()
        for cmd in _BIND_COMMANDS:
            if lowered.startswith(cmd.casefold()):
                self._handle_bind(pack, reply, text[len(cmd):].strip())
                return

    def _handle_bind(self, pack: dict[str, Any], reply: Any, token: str) -> None:
        adapter_id = str(pack.get("_lumen_adapter_id", "") or "")
        if not adapter_id:
            return
        if not token:
            reply(_t("groupbind.reply.format_hint", command=_t("groupbind.command_word")), True)
            return
        if not self._pop_key(adapter_id, token):
            reply(_t("groupbind.reply.key_invalid"), True)
            return
        ok, status, error = self._bind(adapter_id, pack)
        if not ok:
            self.logger.warning(_t("groupbind.log.bind_failed", error=error))
            reply(_t("groupbind.reply.bind_failed", error=error), True)
            return
        # 先经当前连接回执（连接必然存活），再差量重载使新群列表生效
        if status == "already":
            reply(_t("groupbind.reply.already_bound"), True)
        else:
            reply(_t("groupbind.reply.bind_success"), True)
        try:
            self.plugin.reload_onebot_connection()
        except Exception as exc:  # noqa: BLE001 - 回执已发出，重载失败仅记日志
            self.logger.warning(_t("groupbind.log.reload_failed", error=exc))

    # ---------------------------------------------------------------- 绑定
    def _bind(self, adapter_id: str, pack: dict[str, Any]) -> tuple[bool, str, str]:
        """把当前群与发送者写入适配器身份配置（列表去重）。

        返回 (ok, status, error)：status 为 "ok"（有新增）/ "already"（无变更）。
        """
        connections = getattr(self.plugin, "connections", None)
        if connections is None:
            return False, "", _t("connections.unavailable")
        adapter = connections.get(adapter_id)
        if adapter is None:
            return False, "", _t("connections.not_found", id=adapter_id)

        group_key = str(pack.get("group_id") or "").strip()
        sender = pack.get("sender")
        if not isinstance(sender, dict):
            sender = {}
        user_key = str(sender.get("user_id") or pack.get("user_id") or "").strip()
        if not group_key or not user_key or user_key == "None":
            return False, "", _t("groupbind.reply.missing_identity")

        groups = connections.parse_groups_loose(adapter.get("main_group"))
        admins = connections.parse_groups_loose(adapter.get("admin_qq"))
        group_new = group_key not in groups
        admin_new = user_key not in admins
        if not group_new and not admin_new:
            return True, "already", ""

        # 多群绑定：只追加缺失项，已有管理员 / 群不会重复出现
        patch: dict[str, Any] = {}
        if group_new:
            groups.append(group_key)
            patch["main_group"] = groups
        if admin_new:
            admins.append(user_key)
            patch["admin_qq"] = admins
        try:
            connections.update(adapter_id, patch)
        except Exception as exc:  # noqa: BLE001 - 校验失败等错误原样带给群内回执
            return False, "", str(exc)
        self.logger.info(
            _t(
                "groupbind.log.bind_success",
                name=adapter.get("name") or adapter_id,
                group=group_key,
                user=user_key,
            )
        )
        return True, "ok", ""
