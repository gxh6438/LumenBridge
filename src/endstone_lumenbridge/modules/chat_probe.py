"""聊天吸收探针：识别“被聊天美化插件取消后重发”的玩家聊天。

问题背景
--------
聊天美化 / 格式化类插件（如 u_beautiful_chat 等）会接管原生
PlayerChatEvent：将事件取消，再调用 ``server.broadcast_message()``
以格式化文本重新展示。LumenBridge 以 MONITOR 优先级观察聊天
（Endstone 处理器调用顺序 LOWEST → LOW → NORMAL → HIGH → HIGHEST
→ MONITOR），事件到达时已被取消，直接按“消息被抑制”处理，
导致聊天不再转发到 QQ 群。

识别原理
--------
利用 Endstone 事件系统的同步分发特性（同事件按优先级顺序调用；
处理期间调用的 ``broadcast_message`` 会在调用点同步嵌套触发
BroadcastMessageEvent）：

1. LOWEST   on_player_chat_probe    开窗：标记一次聊天分发的起点
                                    （最先执行，先于一切取消方）；
2. NORMAL   （聊天美化插件）        取消事件并调用 broadcast_message
                                    → 同步触发 BroadcastMessageEvent；
   MONITOR  on_broadcast_message    记录窗口内发生的广播；
3. MONITOR  on_player_chat          关窗并三态判定：
     - 未取消                → 正常聊天，照常转发；
     - 已取消 + 窗口内有广播 → 聊天被插件接手重发（“吸收”），
                               玩家实际看到了消息，照常转发；
     - 已取消 + 窗口内无广播 → 聊天被管理插件抑制（如禁言），
                               不转发。

线程模型
--------
窗口状态存于 threading.local。聊天事件与它触发的广播在同一分发
线程上同步发生，天然关联；其他线程上的广播（如 WebUI / 调度
线程的 QQ→游戏广播）不会污染窗口。

边界与取舍
--------
- 聊天插件若取消事件后**异步/延迟**重发（调度器、跨线程），
  关窗时无广播记录，将按“抑制”处理不转发（该模式极罕见，
  且游戏内同样出现延迟展示）；
- 聊天插件若取消事件后逐玩家 ``send_message`` 而非广播，
  无法识别（无广播信号）；
- 被其他插件取消的 BroadcastMessageEvent 不会真正送达玩家，
  不构成“已展示”证据，由调用方过滤后不计入窗口。
"""

from __future__ import annotations

import threading


class ChatAbsorptionProbe:
    """跨事件关联的聊天吸收探针（状态机见模块 docstring）。

    状态生命周期：open（LOWEST）→ record_broadcast×N（分发期间）→
    close（MONITOR），仅在同一次聊天事件分发内有效。
    """

    def __init__(self) -> None:
        # threading.local：窗口按分发线程隔离（见模块 docstring 线程模型）
        self._local = threading.local()

    def open(self) -> None:
        """开窗：标记一次玩家聊天分发的起点（LOWEST 优先级调用）。

        每次开窗重置窗口，上一轮分发若因异常未关窗，
        残留状态不会泄漏到本轮。
        """
        self._local.broadcasts = []

    def record_broadcast(self, message: str) -> None:
        """记录窗口内发生的一次广播（BroadcastMessageEvent，MONITOR 调用）。

        无窗口时（不在聊天分发中）不记录：普通广播与聊天无关。
        """
        broadcasts = getattr(self._local, "broadcasts", None)
        if broadcasts is not None:
            broadcasts.append(str(message))

    def close(self) -> bool:
        """关窗并判定是否发生吸收（MONITOR 优先级调用）。

        :return: True = 本次聊天分发期间发生过广播
                 （聊天被插件接手重发展示），False = 未发生。
        """
        broadcasts = getattr(self._local, "broadcasts", None)
        self._local.broadcasts = None
        return bool(broadcasts)
