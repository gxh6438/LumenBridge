"""OneBot 事件分发器：按 ``post_type`` 分层派发细粒度事件，并为消息事件注入 reply 快捷回复。

v1.2.0 起支持多适配器：事件包携带来源适配器实例，回复经来源适配器发出；
群号过滤 / 上下文注入也按来源适配器的群列表判定。
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import Any, Callable

from .message import decode_cq_entities, format_message, reply as reply_segment

ReplyFunc = Callable[..., None]

# 事件去抖：同一 notice/request 事件（如入群、退群、撤回）经多条适配器链路
# 或协议端重发/重连回放重复到达时，仅在窗口内处理第一次
_DEDUP_WINDOW = 5.0
_DEDUP_MAX = 1024


class EventDispatcher:
    """将 OneBot 原始数据包解析为分层业务事件"""

    def __init__(self, adapter: Any, event_bus: Any, logger: Any) -> None:
        self.adapter = adapter  # AdapterHub 或单 OneBotAdapter
        self.bus = event_bus
        self.logger = logger
        self.env_pool: Any = None  # 由插件主类在初始化后注入，用于多群上下文感知
        self._dedup_lock = threading.Lock()
        # OrderedDict 维护插入/访问序，超限时按最旧淘汰而非全清
        self._dedup_seen: OrderedDict[tuple[str, ...], float] = OrderedDict()
        self.bus.on("onebot.pack", self._on_pack)

    def _resolve_sender(self, source: Any) -> Any:
        """回复目标：优先来源适配器，其次主适配器。"""
        if source is not None and getattr(source, "is_connected", False):
            return source
        primary = getattr(self.adapter, "primary", None)
        return primary() if callable(primary) else self.adapter

    def _source_of(self, pack: dict[str, Any]) -> Any:
        """按事件包内的来源适配器 id 从 hub 回查适配器实例。

        onebot.pack 事件保持单参 (pack) 以兼容子插件监听约定（见子插件开发文档），
        来源信息通过 ``_lumen_adapter_id`` 内部字段传递。
        """
        adapter_id = str(pack.get("_lumen_adapter_id", "") or "")
        getter = getattr(self.adapter, "get", None)
        if adapter_id and callable(getter):
            return getter(adapter_id)
        return None

    def _build_reply(self, pack: dict[str, Any], source: Any = None) -> ReplyFunc:
        """构建快捷回复函数（回复到来源适配器）"""
        message_type = pack.get("message_type")
        message_id = pack.get("message_id")
        target_id = pack.get("group_id") if pack.get("group_id") else pack.get("user_id")

        def _reply(msg: Any, quote: bool = False) -> None:
            if target_id is None:
                return
            sender = self._resolve_sender(source)
            if sender is None:
                return
            segments = format_message(msg)
            if quote and message_id is not None:
                segments.insert(0, reply_segment(message_id))
            if message_type == "group":
                sender.send_group_msg(target_id, segments)
            else:
                sender.send_private_msg(target_id, segments)

        return _reply

    @staticmethod
    def _event_fingerprint(pack: dict[str, Any]) -> tuple[str, ...]:
        """notice / request 事件指纹。

        不含适配器 id：同一协议端事件经多条链路（如同一 NapCat 同时挂在
        正向与反向两个适配器上）重复上报时指纹一致，可跨适配器去重；
        time / target_id 等字段参与指纹，避免正常连续事件被误判为重复。
        """
        return (
            str(pack.get("post_type", "")),
            str(pack.get("notice_type") or pack.get("request_type") or ""),
            str(pack.get("sub_type", "")),
            str(pack.get("group_id", "")),
            str(pack.get("user_id", "")),
            str(pack.get("operator_id", "")),
            str(pack.get("target_id", "")),
            str(pack.get("self_id", "")),
            str(pack.get("time", "")),
        )

    def _is_duplicate_event(self, pack: dict[str, Any]) -> bool:
        """窗口内重复事件返回 True（并记录本次指纹）。"""
        now = time.monotonic()
        fp = self._event_fingerprint(pack)
        with self._dedup_lock:
            if len(self._dedup_seen) > _DEDUP_MAX:
                expired = [k for k, ts in self._dedup_seen.items() if now - ts > _DEDUP_WINDOW]
                for key in expired:
                    self._dedup_seen.pop(key, None)
                if len(self._dedup_seen) > _DEDUP_MAX:
                    # 仍超限：淘汰最旧 25% 而非全清，保留近期指纹维持去重窗口
                    evict = max(1, len(self._dedup_seen) // 4)
                    for _ in range(evict):
                        self._dedup_seen.popitem(last=False)
            seen = self._dedup_seen.get(fp)
            if seen is not None and now - seen < _DEDUP_WINDOW:
                return True
            # 新插入或过期重记录后移到末尾，维持"越新越靠后"的淘汰序
            self._dedup_seen[fp] = now
            self._dedup_seen.move_to_end(fp)
            return False

    def _on_pack(self, pack: dict[str, Any]) -> None:
        post_type = pack.get("post_type")
        if not post_type:
            return

        # notice / request 去抖：多链路重复上报 / 协议端重发只处理第一次。
        # message 不去重（不同平台转发链可能合法产生相同内容，且带唯一 message_id）。
        if post_type in ("notice", "request") and self._is_duplicate_event(pack):
            return

        # 来源适配器 id 由各适配器在派发前注入（下划线前缀表示 LumenBridge 内部字段），
        # 供 chat_sync / whitelist / regex / 子插件按来源适配器取配置
        source = self._source_of(pack)

        # 多群上下文：派发前注入来源群号，使子插件 env.get("main_group") 返回当前来源群。
        # set_current_group 放在 try 内：若其抛错也必须走 finally 清理，避免
        # 线程局部残留污染后续事件
        gid = pack.get("group_id")
        has_ctx = gid is not None and self.env_pool is not None
        try:
            if has_ctx:
                self.env_pool.set_current_group(gid, source)
            if post_type == "meta_event":
                self.bus.emit(f"meta_event.{pack.get('meta_event_type')}", pack)

            elif post_type == "message":
                raw = pack.get("raw_message")
                if raw is None:
                    raw = ""
                elif not isinstance(raw, str):
                    raw = str(raw)
                if any(tok in raw for tok in ("&#91;", "&#93;", "&#44;", "&amp;")):
                    pack["raw_message"] = decode_cq_entities(raw)
                event_name = f"message.{pack.get('message_type')}.{pack.get('sub_type')}"
                self.bus.emit(event_name, pack, self._build_reply(pack, source))

            elif post_type == "notice":
                self.bus.emit(f"notice.{pack.get('notice_type')}", pack)

            elif post_type == "request":
                self.bus.emit(f"request.{pack.get('request_type')}", pack)
        finally:
            if has_ctx:
                self.env_pool.clear_current_group()
