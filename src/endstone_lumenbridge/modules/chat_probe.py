"""聊天吸收探针：识别“被聊天美化插件取消后重发”的玩家聊天。

问题背景
--------
聊天美化 / 格式化类插件会接管原生 PlayerChatEvent：将事件取消，再以
格式化文本重新展示（``server.broadcast_message()`` 或逐玩家
``send_message``）。LumenBridge 以 MONITOR 优先级观察聊天，事件到达时
已被取消，若直接按“消息被抑制”处理，聊天将不再转发到 QQ 群。

识别原理（v1.0.7：时间戳回溯）
--------
v1.0.6 曾用 “LOWEST 开窗 → 分发期间记广播 → MONITOR 关窗” 的
threading.local 窗口方案，实测存在两个致命缺陷：

1. **threading.local 跨事件回调丢状态**：Endstone 在 C++/Python 边界
   切换回调时可能更换 PyThreadState（OS 线程相同），open() 写入的
   列表在 record_broadcast() 中读不到，窗口形同虚设；
2. **依赖插件注册顺序**：美化插件若先于本插件注册，其 LOWEST 处理器
   里触发的广播早于开窗，照样漏记。

v1.0.7 改为**时间戳回溯**：BroadcastMessageEvent 无条件记录
``(消息, 时间戳)``；PlayerChatEvent（MONITOR）到达时回看最近 N 毫秒
内是否出现过「包含原始聊天内容」的广播。美化插件取消事件后在同一分
发链内同步重发（绝大多数插件的实现方式），其广播必然落在这个时间窗
内；注册顺序不再影响结果。事件全部分发在服务器主线程同步执行，普通
实例属性即可安全承载状态（无需线程隔离）。

匹配与消费
--------
- **强匹配**：广播同时包含玩家名与消息文本（美化格式几乎都回显
  两者），排除他人同文本消息与 join/quit 广播的误判；
- **弱匹配**：仅包含消息文本（美化广播不含真实玩家名时的兼容路径，
  如显示昵称的插件）；
- **消费制**：命中的广播条目立即从窗口移除，同一条广播不会被两轮
  聊天重复用作证据（避免窗口内同文本连发时前一轮证据污染后一轮）。

三态判定
--------
- 事件未取消            → 正常聊天，照常转发（调用方处理）；
- 已取消 + 窗口内有
  含原始消息的广播      → 聊天被美化插件接手重发（“吸收”），
                          玩家实际看到了消息，照常转发；
- 已取消 + 无匹配广播   → 聊天被管理插件抑制（禁言/屏蔽词/范围聊天），
                          不转发。

边界与取舍
--------
- 强匹配优先，弱匹配兜底；join/quit 广播 ``[+] 玩家名`` 不含消息
  文本，不会被误判为吸收；
- 美化插件若将消息文本打散变形（逐字插入颜色代码），子串匹配失败，
  按抑制处理；可通过 ``chat.forward_cancelled = "always"`` 兜底；
- 取消后**逐玩家 send_message** 重发（范围聊天插件的设计意图）与
  延迟/异步重发（下一 tick 调度）均无广播信号，按抑制处理不转发；
- 被其他插件取消的 BroadcastMessageEvent 不会真正送达玩家，不构成
  “已展示”证据，由调用方过滤后不计入。
"""

from __future__ import annotations

import time
from typing import Any

# 回溯窗口内最多保留的广播条数（防极端刷屏撑爆内存）
_MAX_RECENT_BROADCASTS = 64


class ChatAbsorptionProbe:
    """跨事件关联的聊天吸收探针（状态机见模块 docstring）。

    状态生命周期：record_broadcast×N（任意时刻）→ absorb_evidence
    （PlayerChatEvent MONITOR 时查询回溯窗口）。
    """

    def __init__(self, window_ms: int = 1000) -> None:
        # 事件分发在服务器主线程同步执行，实例属性即线程安全
        # （v1.0.6 的 threading.local 在嵌入式 Python 跨回调丢状态，已弃用）
        self._recent: list[tuple[float, str]] = []
        self._window_ms = max(100, int(window_ms))

    def set_window(self, window_ms: int) -> None:
        """更新回溯窗口（毫秒）。"""
        self._window_ms = max(100, int(window_ms))

    def record_broadcast(self, message: str) -> None:
        """记录一次已确认送达的广播（BroadcastMessageEvent，MONITOR 调用）。

        无条件记录（带时间戳）；是否构成“吸收证据”由查询侧按内容
        与时间窗判定，因此与插件注册顺序无关。join/quit 等系统广播的
        message 是 Translatable 对象，取其 .text 键名便于诊断（对匹配
        无影响——键名不含聊天文本，天然不会命中）。
        """
        text = getattr(message, "text", None)
        if not isinstance(text, str):
            text = str(message)
        self._recent.append((time.monotonic(), text))
        if len(self._recent) > _MAX_RECENT_BROADCASTS:
            del self._recent[: len(self._recent) - _MAX_RECENT_BROADCASTS]

    def absorb_evidence(self, player_name: str, message: str) -> str | None:
        """查询最近窗口内是否存在“该聊天被重发展示”的证据。

        命中的广播条目会被消费（从窗口移除），同一条证据不会重复
        匹配两轮聊天。

        :param player_name: 聊天玩家名（强匹配条件之一）
        :param message: 原始聊天消息
        :return: 命中的广播文本（证据）；无证据返回 None
        """
        now = time.monotonic()
        deadline = now - self._window_ms / 1000.0
        needle = (message or "").strip()
        # 丢弃过期条目（顺带清理，窗口外的不参与匹配）
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
    # v1.0.6 的 open/close 接口保留为空操作，避免外部（旧测试/子插件）
    # 调用报错；新逻辑不再依赖开窗关窗。
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
