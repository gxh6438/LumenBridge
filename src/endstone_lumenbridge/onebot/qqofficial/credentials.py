"""QQ 官方机器人回复凭据管理（借鉴 Gensokyo 懒池 / AtoP 机制）。

三类凭据：被动 msg_id 池（同一 msg_id 配递增 msg_seq 可多次回复，群 5
次 / 单聊 4 次，见 constants）；入群 event_id（可作被动回复凭据且不消耗
主动额度）；主动补发栈（22009 被拒后暂存，待被动回复成功时借剩余额度
补发）。线程安全：入池与取出可能来自不同协程，统一以 threading.Lock
保护（锁不绑定事件循环）。
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

        优先取未用过（uses=0）的凭据以摊平限额；否则取最新的（Gensokyo
        懒池语义）；全部用尽或过期返回 None（调用方降级主动发送）。
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

    def purge_passive(self, target: str, msg_id: str) -> None:
        """服务端判定 msg_id 过期（40034005）时移除池中该凭据。

        本地窗口与官方过期判定存在偏差：不清理则后续消息会反复取到
        已失效凭据，每条都白白消耗一轮重试。
        """
        if not target or not msg_id:
            return
        with self._lock:
            pool = self.passive.get(target)
            if not pool:
                return
            remain = [entry for entry in pool if entry[0] != msg_id]
            if remain:
                self.passive[target] = remain
            else:
                self.passive.pop(target, None)

    def sync_passive_seq(self, target: str, msg_id: str, seq: int) -> None:
        """发送成功后回写实际消耗的 msg_seq。

        post_message 重试期间 body 内 seq 已递增（规避官方去重），
        池计数若不跟进，下次取出的 seq 会与官方已消费的序号重复，
        该回复被官方按 (msg_id, msg_seq) 去重静默丢弃。
        仅前进不后退：并发补发路径完成顺序可能与取出顺序不同。
        """
        if not target or not msg_id:
            return
        with self._lock:
            pool = self.passive.get(target)
            if not pool:
                return
            for i, entry in enumerate(pool):
                if entry[0] == msg_id:
                    if seq + 1 > entry[2]:
                        pool[i] = (entry[0], entry[1], seq + 1, entry[3], entry[4])
                    break

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

    def purge_event_id(self, target: str) -> None:
        """官方判定 event_id 无效（40034025）时移除缓存。

        不清理则窗口内后续每条发往该目标的消息都会先白白消耗一轮
        重试（必失败）再降级主动通道（群聊必被拒），消息连锁丢失。
        """
        if target:
            self.event_ids.pop(target, None)

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

    def unshift_active(self, item: ActiveItem) -> bool:
        """条目回栈首（push_active 是尾部追加）。

        flush_active_stack 凭据耗尽时取出的队首消息必须回队首，
        追加到尾部会让最旧的消息排到最后，补发顺序错乱。
        """
        target = item[1]
        with self._lock:
            queue = self.active_stack.setdefault(target, [])
            if len(queue) >= ACTIVE_STACK_MAX:
                return False
            queue.insert(0, item)
            return True

    def active_size(self, target: str) -> int:
        """目标补发栈当前长度（日志用）。"""
        with self._lock:
            return len(self.active_stack.get(target, ()))
