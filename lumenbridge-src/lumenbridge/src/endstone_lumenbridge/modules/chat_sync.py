"""群服互通聊天同步模块：双向同步群服消息并推送游戏事件通知，模板占位符为 %s。

每个适配器卡片拥有独立的群服互通配置：入站消息按来源适配器的配置渲染，
出站广播按各适配器各自的群列表与格式发送；AstrBot 适配器未配置群列表时发送到
虚拟群 0（由 AstrBot 插件端按其 UMO 配置分发）。
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from endstone import ColorFormat

from ..i18n import t as _t

if TYPE_CHECKING:
    from ..plugin import LumenBridgePlugin


def replace_placeholders(template: str, *args: Any) -> str:
    """按顺序替换模板中的 %s 占位符"""
    if not isinstance(template, str):
        return ""
    params = list(args)
    index = 0

    def _sub(_match: re.Match) -> str:
        nonlocal index
        if index < len(params):
            value = str(params[index])
            index += 1
            return value
        return ""

    return re.sub(r"%s", _sub, template)


class ChatSyncModule:
    """双向消息同步 + 游戏事件通知（多适配器）"""

    def __init__(self, plugin: "LumenBridgePlugin") -> None:
        self.plugin = plugin
        self.logger = getattr(plugin, "_tee_logger", None) or plugin.logger
        self.hub = plugin.adapter  # AdapterHub 门面（兼容单适配器 API）
        self.bus = plugin.bus

        self.bus.on("message.group.normal", self._on_group_message)

    # ------------------------------------------------------------------ 配置
    def _sync_config(self, pack: dict[str, Any] | None = None) -> dict[str, Any]:
        """取来源适配器（无来源时取主适配器）的群服互通配置。

        读取走 get_view 免深拷贝：本方法在每条群消息上调用，
        deepcopy 整张适配器卡片是纯浪费；配置只在 update()/create()
        整体替换，引用读取安全。
        """
        connections = getattr(self.plugin, "connections", None)
        if connections is not None:
            adapter_id = str((pack or {}).get("_lumen_adapter_id", "") or "")
            if adapter_id:
                cfg = connections.get_view(adapter_id)
                if cfg and isinstance(cfg.get("sync"), dict):
                    return cfg["sync"]
            primary = connections.primary_websocket()
            if primary and isinstance(primary.get("sync"), dict):
                return primary["sync"]
        from ..connections import _default_sync

        return _default_sync()

    def _group_allowed(self, pack: dict[str, Any]) -> bool:
        """来源群是否允许互通：与 plugin.group_allowed 统一口径，
        避免两份分叉实现在同一事件上判定相反。"""
        return self.plugin.group_allowed(pack)

    # ------------------------------------------------------------------ 入站
    def _format_segments(
        self, segments: Any, conf: dict[str, Any], pack: dict[str, Any] | None = None
    ) -> str:
        """将 OneBot 消息段数组格式化为纯文本"""
        if isinstance(segments, str):
            return replace_placeholders(conf.get("text_format", "%s"), segments)
        if not isinstance(segments, list):
            return ""

        parts: list[str] = []
        for seg in segments:
            # 协议端畸形消息段（字符串 / null 等）直接跳过，防止 .get 抛异常
            if not isinstance(seg, dict):
                continue
            seg_type = seg.get("type")
            data = seg.get("data") if isinstance(seg.get("data"), dict) else {}
            if seg_type == "text":
                parts.append(
                    replace_placeholders(conf.get("text_format", "%s"), data.get("text") or "")
                )
            elif seg_type == "image":
                parts.append(conf.get("image_format", "[图片]"))
            elif seg_type == "face":
                parts.append(conf.get("face_format", "[表情]"))
            elif seg_type == "at":
                qq = str(data.get("qq", ""))
                # 官方域 @ 段携带的是 openid，须查官方域绑定库；个人号域缺省 qq 域
                domain = (
                    "official" if str((pack or {}).get("domain", "") or "") == "official" else "qq"
                )
                bound = (
                    self.plugin.whitelist_module.get_binding_by_qq(qq, domain)
                    if self.plugin.whitelist_module
                    else None
                )
                display = bound["xbox"] if bound else qq
                parts.append(replace_placeholders(conf.get("at_format", "@%s"), display))
            elif seg_type == "reply":
                parts.append(conf.get("reply_format", "[回复]"))
            elif seg_type == "forward":
                parts.append(conf.get("forward_format", "[合并转发]"))
        return "".join(parts)

    def _on_group_message(self, pack: dict[str, Any], _reply: Any) -> None:
        conf = self._sync_config(pack)
        if not conf.get("chat_to_server_enable", True):
            return
        if not self._group_allowed(pack):
            return

        sender = pack.get("sender") if isinstance(pack.get("sender"), dict) else {}
        sender_name = (
            sender.get("card") or sender.get("nickname") or str(pack.get("user_id") or "") or ""
        )
        content = self._format_segments(
            pack.get("message", pack.get("raw_message", "")), conf, pack
        )

        # 聊天屏蔽（QQ → 游戏方向）：block 模式命中时整条丢弃
        chat_filter = getattr(self.plugin, "chat_filter", None)
        if chat_filter is not None and content:
            qq_id = str(pack.get("user_id") or "")
            content, hit = chat_filter.check(content, direction="qq_to_game", qq=qq_id)
            if hit and not content:
                return

        if not content:
            return
        try:
            max_len = int(conf.get("max_message_length", 256))
            if max_len <= 0:
                max_len = 256
        except (TypeError, ValueError):
            max_len = 256
        if len(content) > max_len:
            # 截断含 "..." 后总长不超过 max_len；max_len 过小时退化为硬截断
            content = content[: max_len - 3] + "..." if max_len > 3 else content[:max_len]

        line = replace_placeholders(
            conf.get("chat_to_server_format", "[群聊] %s: %s"), sender_name, content
        )

        def broadcast() -> None:
            self.plugin.server.broadcast_message(
                f"{ColorFormat.AQUA}{line}{ColorFormat.RESET}"
            )

        self.plugin.run_on_main(broadcast)

    # ------------------------------------------------------------------ 出站
    def _broadcast(self, enable_key: str, fmt_key: str, fallback: str, *args: Any) -> None:
        """按各适配器自己的群服互通配置广播游戏事件。"""
        connections = getattr(self.plugin, "connections", None)
        hub = self.hub
        targets: list[tuple[Any, dict[str, Any], list[Any]]] = []
        if connections is not None:
            # adapters_view() 持锁返回列表快照，避免迭代期间并发 CRUD 修改列表
            for cfg in connections.adapters_view():
                if not cfg.get("enabled"):
                    continue
                sync = cfg.get("sync") if isinstance(cfg.get("sync"), dict) else {}
                adapter = hub.get(str(cfg.get("id"))) if hasattr(hub, "get") else None
                if adapter is None:
                    continue
                groups = list(getattr(adapter, "groups", None) or [])
                targets.append((adapter, sync, groups))
        else:  # 兼容路径：无连接管理器时退回单适配器
            primary = hub.primary() if hasattr(hub, "primary") else hub
            if primary is not None:
                targets.append((primary, self._sync_config(), list(getattr(primary, "groups", None) or [])))

        for adapter, sync, groups in targets:
            if not sync.get(enable_key, True):
                continue
            line = replace_placeholders(sync.get(fmt_key, fallback), *args)
            # 单个目标发送失败只记日志，不中断其余适配器/群的广播
            if groups:
                for gid in groups:
                    try:
                        adapter.send_group_msg(gid, line)
                    except Exception as e:  # noqa: BLE001
                        self.logger.warning(f"broadcast to {gid} failed: {e}")
            else:
                self._broadcast_without_groups(adapter, line)

    def _broadcast_without_groups(self, adapter: Any, line: str) -> None:
        """无配置群列表适配器的广播目标选择。"""
        try:
            if getattr(adapter, "adapter_type", "") == "astrbot":
                # AstrBot 适配器：群号在其插件端（UMO）配置，这里发虚拟群 0
                adapter.send_group_msg(0, line)
                return
            # 无配置群列表的适配器（如 QQ 官方）：询问其广播目标。
            # QQ 官方无群列表 API，broadcast_groups 返回动态发现的群
            #（「未填群 openid = 全局转发」）；无任何目标时跳过，
            # 绝不发往不存在的虚拟群 0（官方侧即 HTTP 400）
            broadcast = getattr(adapter, "broadcast_groups", None)
            discovered = list(broadcast()) if callable(broadcast) else []
            if discovered:
                for gid in discovered:
                    adapter.send_group_msg(gid, line)
            else:
                self.logger.warning(_t("chatsync.no_broadcast_target", adapter=adapter.display_name))
        except Exception as e:  # noqa: BLE001
            self.logger.warning(f"broadcast via {getattr(adapter, 'display_name', adapter)} failed: {e}")

    def on_player_chat(self, player_name: str, message: str) -> None:
        # 聊天屏蔽（游戏 → QQ 方向）：block 模式命中时整条不广播
        chat_filter = getattr(self.plugin, "chat_filter", None)
        if chat_filter is not None:
            message, hit = chat_filter.check(
                message, direction="game_to_qq", player=player_name
            )
            if hit and not message:
                return
        self._broadcast(
            "chat_to_group_enable", "chat_to_group_format", "[玩家] %s: %s", player_name, message
        )

    def on_player_join(self, player_name: str) -> None:
        self._broadcast("join_to_group_enable", "join_format", "[玩家] %s 进服", player_name)

    def on_player_quit(self, player_name: str) -> None:
        self._broadcast("leave_to_group_enable", "leave_format", "[玩家] %s 退服", player_name)

    def on_player_death(self, death_message: str) -> None:
        self._broadcast("death_to_group_enable", "death_format", "[死亡] %s", death_message)

    def on_server_start(self) -> None:
        self._broadcast("server_start_to_group", "server_start_format", "[服务器] 已启动")

    def on_server_stop(self) -> None:
        self._broadcast("server_stop_to_group", "server_stop_format", "[服务器] 已关闭")
