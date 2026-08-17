"""QQ 官方机器人回复凭据管理。

集中管理三类发送凭据（借鉴 Gensokyo 懒池 / AtoP 机制）：
1. 被动凭据池：入站消息 msg_id 池化，同一 msg_id 配递增 msg_seq
   可多次回复（群 5 次 / 单聊 4 次，见 constants）；
2. 入群 event_id：机器人入群事件自带，可作被动回复凭据且
   不消耗主动额度（窗口见 EVENT_ID_WINDOW）；
3. 主动补发栈：主动消息被拒（22009）后暂存，待下次被动回复
   成功时借剩余额度补发。

线程安全：入池发生在适配器事件循环，取出可能来自不同协程，
统一以 threading.Lock 保护（锁不绑定事件循环）。
"""

from __future__ import annotations

import threading
import time
from typing import Any

from .constants import (
    ACTIVE_STACK_MAX,
    EVENT_ID_WINDOW,
    PASSIVE_MAX_SEQ_GROUP,
    PASSIVE_POOL_MAX,
)

# 被动凭据条目：(msg_id, 过期时间戳, 下一个 msg_seq, 已用次数, 单条最大回复次数)
PassiveEntry = tuple[str, float, int, int, int]
# 补发栈条目：(kind, target, content, media)
ActiveItem = tuple[str, str, str, dict[str, Any] | None]


class CredentialsStore:
    """回复凭据存储：被动池 + event_id + 主动补发栈。"""

    def __init__(self) -> None:
        # 目标 openid → 凭据条目列表（最新在尾部）
        self.passive: dict[str, list[PassiveEntry]] = {}
        # 目标 openid → (event_id, 过期时间戳)
        self.event_ids: dict[str, tuple[str, float]] = {}
        # 目标 openid → 待补发消息队列
        self.active_stack: dict[str, list[ActiveItem]] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------ 被动凭据池
    def cache_passive(
        self, target: str, msg_id: str, window: float, max_seq: int = PASSIVE_MAX_SEQ_GROUP
    ) -> None:
        """被动凭据入池：同一 msg_id 去重，池上限 PASSIVE_POOL_MAX，顺手清理过期项。"""
        if not target or not msg_id:
            return
        now = time.time()
        with self._lock:
            pool = [
                entry
                for entry in self.passive.get(target, [])
                if entry[1] > now and entry[0] != msg_id
            ]
            pool.append((msg_id, now + window, 1, 0, max_seq))
            self.passive[target] = pool[-PASSIVE_POOL_MAX:]

    def take_passive(self, target: str) -> tuple[str, int] | None:
        """从池中取出被动凭据 (msg_id, msg_seq)；无可用凭据返回 None。

        官方规则：同一 msg_id 配递增 msg_seq 最多回复 max_seq 次（群 5 / C2C 4）。
        优先取未用过（uses=0）的凭据以摊平限额；否则取最新的（Gensokyo 懒池
        语义）；全部用尽或过期返回 None（调用方降级主动发送）。
        """
        now = time.time()
        with self._lock:
            pool = [entry for entry in self.passive.get(target, []) if entry[1] > now]
            if not pool:
                self.passive.pop(target, None)
                return None
            usable = [entry for entry in pool if entry[2] <= entry[4]]
            if not usable:
                self.passive[target] = pool
                return None
            fresh = [entry for entry in usable if entry[3] == 0]
            chosen = fresh[-1] if fresh else max(usable, key=lambda e: e[1])
            index = pool.index(chosen)
            msg_id, expires, next_seq, uses, max_seq = chosen
            pool[index] = (msg_id, expires, next_seq + 1, uses + 1, max_seq)
            self.passive[target] = pool
            return msg_id, next_seq

    # ------------------------------------------------------------ 入群 event_id
    def cache_event_id(self, target: str, event_id: Any) -> None:
        """缓存机器人入群事件的 event_id：可作被动回复凭据，不消耗主动额度。"""
        token = str(event_id or "").strip()
        if not target or not token:
            return
        self.event_ids[target] = (token, time.time() + EVENT_ID_WINDOW)

    def take_event_id(self, target: str) -> str:
        """取出目标可用的入群 event_id（窗口内可复用）；无则返回空串。"""
        entry = self.event_ids.get(target)
        if not entry:
            return ""
        event_id, expires = entry
        if time.time() >= expires:
            self.event_ids.pop(target, None)
            return ""
        return event_id

    # ------------------------------------------------------------ 主动补发栈
    def push_active(self, item: ActiveItem) -> bool:
        """主动消息被拒后入补发栈（每目标上限 ACTIVE_STACK_MAX，队满丢新）。"""
        target = item[1]
        with self._lock:
            queue = self.active_stack.setdefault(target, [])
            if len(queue) >= ACTIVE_STACK_MAX:
                return False
            queue.append(item)
            return True

    def pop_active(self, target: str) -> ActiveItem | None:
        """取出目标栈首的待补发消息；栈空返回 None。"""
        with self._lock:
            queue = self.active_stack.get(target)
            if not queue:
                return None
            item = queue.pop(0)
            if not queue:
                self.active_stack.pop(target, None)
            return item

    def active_size(self, target: str) -> int:
        """目标补发栈当前长度（日志用）。"""
        with self._lock:
            return len(self.active_stack.get(target, ()))
