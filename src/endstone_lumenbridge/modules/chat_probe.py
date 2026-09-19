"""聊天吸收探针：识别“被聊天美化插件取消后重发”的玩家聊天。

聊天美化 / 格式化插件会接管原生 PlayerChatEvent：取消事件再以格式化
文本重新展示。LumenBridge 以 MONITOR 优先级观察聊天，事件到达时已被
取消，若直接按“消息被抑制”处理，聊天将不再转发到 QQ 群。

识别原理（时间戳回溯）：BroadcastMessageEvent 无条件记录
``(消息, 时间戳)``；PlayerChatEvent（MONITOR）到达时回看最近 N 毫秒
内是否出现过「包含原始聊天内容」的广播。美化插件取消事件后在同一分发
链内同步重发（绝大多数插件的实现方式），其广播必然落在时间窗内，
与插件注册顺序无关。事件分发在服务器主线程同步执行，实例属性即可
安全承载状态。

匹配与消费：强匹配=广播同时包含玩家名与消息文本（排除他人同文本消息
与 join/quit 广播的误判）；弱匹配=仅包含消息文本（美化广播不含真实
玩家名时的兼容路径）；消费制=命中的广播条目立即从窗口移除，同一条
广播不会被两轮聊天重复用作证据。

三态判定：未取消 → 正常聊天照常转发（调用方处理）；已取消 + 窗口内
有含原始消息的广播 → 聊天被美化插件接手重发（“吸收”），照常转发；
已取消 + 无匹配广播 → 聊天被管理插件抑制（禁言/屏蔽词/范围聊天），
不转发。

边界与取舍：美化插件若将消息文本打散变形（逐字插入颜色代码），子串
匹配失败按抑制处理，可通过 ``chat.forward_cancelled = "always"`` 兜底；
取消后逐玩家 send_message 重发（范围聊天插件的设计意图）与延迟/异步
重发均无广播信号，按抑制处理不转发；被其他插件取消的
BroadcastMessageEvent 不会真正送达玩家，不构成“已展示”证据，由调用
方过滤后不计入。
"""

from __future__ import annotations

import time
from typing import Any

# 回溯窗口内最多保留的广播条数（防极端刷屏撑爆内存）
_MAX_RECENT_BROADCASTS = 64


class ChatAbsorptionProbe:
    """跨事件关联的聊天吸收探针（原理见模块 docstring）。"""

    def __init__(self, window_ms: int = 1000) -> None:
        # 事件分发在主线程同步执行，实例属性即线程安全
        self._recent: list[tuple[float, str]] = []
        self._window_ms = max(100, int(window_ms))

    def set_window(self, window_ms: int) -> None:
        """更新回溯窗口（毫秒）。"""
        self._window_ms = max(100, int(window_ms))

    def record_broadcast(self, message: str) -> None:
        """记录一次已确认送达的广播（MONITOR 调用，带时间戳无条件记录）。

        是否构成“吸收证据”由查询侧按内容与时间窗判定，与插件注册顺序
        无关。join/quit 等系统广播取 .text 键名，不含聊天文本天然不会命中。
        """
        text = getattr(message, "text", None)
        if not isinstance(text, str):
            text = str(message)
        self._recent.append((time.monotonic(), text))
        if len(self._recent) > _MAX_RECENT_BROADCASTS:
            del self._recent[: len(self._recent) - _MAX_RECENT_BROADCASTS]

    def absorb_evidence(self, player_name: str, message: str) -> str | None:
        """查询最近窗口内是否存在“该聊天被重发展示”的证据（命中即消费）。

        :param player_name: 聊天玩家名（强匹配条件之一）
        :param message: 原始聊天消息
        :return: 命中的广播文本；无证据返回 None
        """
        now = time.monotonic()
        deadline = now - self._window_ms / 1000.0
        needle = (message or "").strip()
        # 丢弃过期条目（顺带清理）
        self._recent = [item for item in self._recent if item[0] >= deadline]
        if not needle:
            return None
        # 强匹配：广播同时回显玩家名与消息文本
        if player_name:
            for i in range(len(self._recent) - 1, -1, -1):
                text = self._recent[i][1]
                if needle in text and player_name in text:
                    return self._recent.pop(i)[1]
        # 弱匹配：仅回显消息文本（美化广播不含真实玩家名的兼容路径）
        for i in range(len(self._recent) - 1, -1, -1):
            text = self._recent[i][1]
            if needle in text:
                return self._recent.pop(i)[1]
        return None

    # ---------------------------------------------------------------- 兼容层
    # 旧版 open/close 接口保留为空操作，避免外部调用报错
    def open(self) -> None:  # noqa: D102 - 兼容空操作
        return None

    def close(self) -> bool:  # noqa: D102 - 兼容空操作，恒无吸收记录
        return False

    def dump_debug(self) -> dict[str, Any]:
        """诊断快照：窗口内最近广播（供 [chatdbg] 日志）。"""
        now = time.monotonic()
        return {
            "window_ms": self._window_ms,
            "recent": [
                {"age_ms": int((now - ts) * 1000), "msg": text[:80]}
                for ts, text in self._recent[-5:]
            ],
        }
