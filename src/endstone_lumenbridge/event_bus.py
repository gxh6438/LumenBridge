"""LumenBridge 事件总线：线程安全的事件枢纽（on / once / emit / off）。

事件名沿用 OneBot 上报类型（如 message.group.normal、notice.group_increase）。
"""

from __future__ import annotations

import threading
import traceback
from collections import defaultdict
from typing import Any, Callable

from .i18n import t as _t


class EventBus:
    """线程安全的轻量级事件总线"""

    def __init__(self, logger: Any = None) -> None:
        self._logger = logger
        self._lock = threading.RLock()
        self._listeners: dict[str, list[Callable[..., Any]]] = defaultdict(list)
        self._once_listeners: dict[str, list[Callable[..., Any]]] = defaultdict(list)

    def on(self, event: str, handler: Callable[..., Any]) -> Callable[..., Any]:
        """注册持久事件监听器，返回 handler 便于后续解绑"""
        with self._lock:
            # 同一 handler 重复注册时去重，避免热重载后同一事件被触发多次
            if handler not in self._listeners[event]:
                self._listeners[event].append(handler)
        return handler

    def once(self, event: str, handler: Callable[..., Any]) -> Callable[..., Any]:
        """注册一次性事件监听器，触发一次后自动移除"""
        with self._lock:
            # 与 on() 一致去重：同一 handler 重复 once 只注册一次，
            # 避免重载流程叠加注册后回调被连续触发多次
            if handler not in self._once_listeners[event]:
                self._once_listeners[event].append(handler)
        return handler

    def off(self, event: str, handler: Callable[..., Any]) -> None:
        """移除指定事件的监听器"""
        with self._lock:
            for pool in (self._listeners, self._once_listeners):
                if handler in pool.get(event, []):
                    pool[event].remove(handler)

    def remove_all(self, event: str | None = None) -> None:
        """移除某事件（或全部事件）的所有监听器"""
        with self._lock:
            if event is None:
                self._listeners.clear()
                self._once_listeners.clear()
            else:
                self._listeners.pop(event, None)
                self._once_listeners.pop(event, None)

    def emit(self, event: str, *args: Any, **kwargs: Any) -> None:
        """触发事件，逐个调用监听器；单个监听器异常不影响其他监听器"""
        with self._lock:
            handlers = list(self._listeners.get(event, []))
            once_handlers = self._once_listeners.pop(event, [])

        for handler in handlers + once_handlers:
            try:
                handler(*args, **kwargs)
            except Exception:
                if self._logger:
                    self._logger.error(
                        _t("errors.event_listener_error", event=event, traceback=traceback.format_exc())
                    )

    def listener_count(self, event: str) -> int:
        with self._lock:
            return len(self._listeners.get(event, [])) + len(
                self._once_listeners.get(event, [])
            )
