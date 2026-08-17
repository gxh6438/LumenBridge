"""日志缓冲模块：环形缓冲 + 订阅推送 + LoggerTee 包装器。"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any, Callable

MAX_CACHE = 500
MAX_SUBSCRIBERS = 8

LogEntry = dict[str, Any]


class LogBuffer:
    """线程安全的日志环形缓冲区 + 订阅推送"""

    def __init__(
        self,
        max_size: int = MAX_CACHE,
        max_subscribers: int = MAX_SUBSCRIBERS,
    ) -> None:
        self._cache: deque[LogEntry] = deque(maxlen=max_size)
        self._subscribers: list[Callable[[LogEntry], None]] = []
        self._max_subscribers = max(1, int(max_subscribers))
        self._lock = threading.RLock()

    def push(self, level: str, source: str, msg: str) -> None:
        entry: LogEntry = {
            "level": level,
            "plugin": source,
            "msg": str(msg),
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        with self._lock:
            self._cache.append(entry)
            subscribers = list(self._subscribers)
        for cb in subscribers:
            try:
                cb(entry)
            except Exception:
                pass

    @property
    def cache(self) -> list[LogEntry]:
        with self._lock:
            return list(self._cache)

    def subscribe(self, cb: Callable[[LogEntry], None]) -> bool:
        """注册实时日志订阅；达到连接上限时返回 ``False``。"""
        with self._lock:
            if cb in self._subscribers:
                return True
            if len(self._subscribers) >= self._max_subscribers:
                return False
            self._subscribers.append(cb)
            return True

    def unsubscribe(self, cb: Callable[[LogEntry], None]) -> None:
        with self._lock:
            if cb in self._subscribers:
                self._subscribers.remove(cb)

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)


class LoggerTee:
    """线程安全的 logger 包装器：转发到原 logger 的同时写入 LogBuffer

    重要：Endstone 的 logger 底层经由 replxx 写 Windows 控制台，
    从非主线程直接调用会与控制台输入线程产生竞争导致服务端崩溃
    （replxx::Terminal::write8 异常）。因此后台线程的日志一律通过
    scheduler 调度回游戏主线程后再输出；LogBuffer 本身线程安全，直接写入。
    """

    def __init__(
        self,
        logger: Any,
        buffer: LogBuffer,
        source: str = "LumenBridge",
        main_thread_dispatch: "Callable[[Callable[[], None]], None] | None" = None,
    ) -> None:
        self._logger = logger
        self._buffer = buffer
        self._source = source
        self._dispatch = main_thread_dispatch
        self._main_thread_id = threading.get_ident()

    def _emit(self, level: str, msg: Any, forward_level: str | None = None) -> None:
        text = str(msg)
        # 缓冲区只保留前端兼容的级别（critical 归并为 error），原始级别经转发保留
        self._buffer.push("warn" if level == "warning" else level, self._source, text)
        forward = forward_level or level

        def write() -> None:
            try:
                getattr(self._logger, forward, self._logger.info)(text)
            except Exception:
                pass

        if threading.get_ident() == self._main_thread_id or self._dispatch is None:
            write()
        else:
            try:
                self._dispatch(write)
            except Exception:
                pass

    def info(self, msg: Any) -> None:
        self._emit("info", msg)

    def warning(self, msg: Any) -> None:
        self._emit("warning", msg)

    def error(self, msg: Any) -> None:
        self._emit("error", msg)

    def debug(self, msg: Any) -> None:
        self._emit("debug", msg)

    def exception(self, msg: Any) -> None:
        self._emit("error", msg)

    def critical(self, msg: Any) -> None:
        # 缓冲区记录为 error（前端兼容），转发到控制台/文件时保留 critical 级别
        self._emit("error", msg, "critical")

    def __getattr__(self, name: str) -> Any:
        # 直接读 __dict__ 避免在 _logger 尚未设置时触发自身递归。
        if name.startswith("_"):
            raise AttributeError(name)
        logger = self.__dict__.get("_logger")
        if logger is None:
            raise AttributeError(name)
        return getattr(logger, name)
