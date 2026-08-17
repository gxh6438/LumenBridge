"""服务器 CPU/内存监控（纯标准库，跨平台）。

后台线程定时采样，API 返回最新快照；按 psutil -> Linux /proc -> Windows ctypes 优先级选择实现。
"""

from __future__ import annotations

import os
import platform
import threading
import time
from typing import Any


def _try_import_psutil() -> Any:
    try:
        import psutil  # type: ignore

        return psutil
    except Exception:
        return None


class ServerMetricsCollector:
    """服务器资源监控采集器（线程安全，后台线程采样）"""

    def __init__(self, logger: Any, interval: float = 3.0) -> None:
        self.logger = logger
        self.interval = max(1.0, float(interval))
        self._lock = threading.RLock()  # RLock：start/stop 持锁时 _sample 可重入获取
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

        self._cpu_percent: float = 0.0
        self._mem_percent: float = 0.0
        self._mem_used: int = 0
        self._mem_total: int = 0
        self._cpu_count: int = os.cpu_count() or 1
        self._available: bool = False
        self._reason: str = ""

        self._psutil = _try_import_psutil()
        self._system = platform.system()
        self._prev_cpu: tuple[float, float] | None = None  # (busy, total)
        self._prev_time: float | None = None

    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop_event.clear()
            # 立即做一次采样（可能 CPU% 仍为 0，因为需要两帧）
            self._sample()
            self._thread = threading.Thread(
                target=self._loop, name="LumenBridge-Metrics", daemon=True
            )
            self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        with self._lock:
            thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=2)
        with self._lock:
            self._thread = None

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            # 用 wait 代替 sleep，便于快速响应停止信号
            if self._stop_event.wait(self.interval):
                break
            try:
                self._sample()
            except Exception as e:  # noqa: BLE001
                # 采集异常不能拖垮整个 WebUI
                with self._lock:
                    self._available = False
                    self._reason = str(e)

    def _sample(self) -> None:
        if self._psutil is not None:
            self._sample_psutil()
        elif self._system == "Linux":
            self._sample_linux()
        elif self._system == "Windows":
            self._sample_windows()
        else:
            with self._lock:
                self._available = False
                self._reason = f"不支持的平台: {self._system}"

    def _sample_psutil(self) -> None:
        try:
            cpu_percent = self._psutil.cpu_percent(interval=None)
            mem = self._psutil.virtual_memory()
            with self._lock:
                self._cpu_percent = float(cpu_percent)
                self._mem_percent = float(mem.percent)
                self._mem_total = int(mem.total)
                self._mem_used = int(mem.used)
                self._cpu_count = (
                    self._psutil.cpu_count(logical=True) or self._cpu_count
                )
                self._available = True
                self._reason = ""
        except Exception as e:  # noqa: BLE001
            with self._lock:
                self._available = False
                self._reason = str(e)

    def _sample_linux(self) -> None:
        # 整个采样纳入 self._lock：_prev_cpu/_prev_time 的读写
        # 与 start/stop/snapshot 并发时保持一致（RLock 可重入）
        with self._lock:
            cpu_busy, cpu_total = self._read_proc_cpu()
            mem_total, mem_available = self._read_proc_mem()

            now = time.time()
            cpu_percent = 0.0
            prev = self._prev_cpu
            prev_t = self._prev_time
            if prev is not None and prev_t is not None and now > prev_t:
                total_delta = cpu_total - prev[1]
                busy_delta = cpu_busy - prev[0]
                if total_delta > 0:
                    cpu_percent = max(0.0, min(100.0, busy_delta / total_delta * 100.0))
            self._prev_cpu = (cpu_busy, cpu_total)
            self._prev_time = now

            mem_used = mem_total - mem_available if mem_total > 0 else 0
            mem_percent = (mem_used / mem_total * 100.0) if mem_total > 0 else 0.0

            self._cpu_percent = cpu_percent
            self._mem_percent = mem_percent
            self._mem_total = mem_total
            self._mem_used = mem_used
            self._available = True
            self._reason = ""

    @staticmethod
    def _read_proc_cpu() -> tuple[float, float]:
        """读取 /proc/stat 的聚合 CPU 行，返回 (busy, total)"""
        with open("/proc/stat", "r", encoding="utf-8") as f:
            line = f.readline()
        if not line.startswith("cpu"):
            return 0.0, 0.0
        parts = line.split()[1:]
        nums = [float(x) for x in parts[:8]]
        # 标准字段：user nice system idle iowait irq softirq steal
        if len(nums) < 4:
            return 0.0, 0.0
        user, nice, system, idle = nums[0], nums[1], nums[2], nums[3]
        iowait = nums[4] if len(nums) > 4 else 0.0
        irq = nums[5] if len(nums) > 5 else 0.0
        softirq = nums[6] if len(nums) > 6 else 0.0
        steal = nums[7] if len(nums) > 7 else 0.0
        idle_all = idle + iowait
        non_idle = user + nice + system + irq + softirq + steal
        total = idle_all + non_idle
        busy = non_idle
        return busy, total

    @staticmethod
    def _read_proc_mem() -> tuple[int, int]:
        """读取 /proc/meminfo，返回 (total_bytes, available_bytes)"""
        total = 0
        # available 用 None 哨兵区分"未读到"与"值恰好为 0"
        available: int | None = None
        free = 0
        with open("/proc/meminfo", "r", encoding="utf-8") as f:
            for line in f:
                parts = line.split()
                if len(parts) < 2:
                    continue
                if line.startswith("MemTotal:"):
                    total = int(parts[1]) * 1024
                elif line.startswith("MemAvailable:"):
                    available = int(parts[1]) * 1024
                elif line.startswith("MemFree:"):
                    free = int(parts[1]) * 1024
                if total and available is not None:
                    break
        if available is None and total:
            # 老内核没有 MemAvailable，退化为 free
            available = free
        return total, available if available is not None else 0

    def _sample_windows(self) -> None:
        # 整个采样纳入 self._lock：_prev_cpu/_prev_time 的读写
        # 与 start/stop/snapshot 并发时保持一致（RLock 可重入）
        with self._lock:
            mem = self._win_mem_status()
            if mem is None:
                # 采集失败时置不可用状态，而不是把 0 假数据当真实值上报
                self._available = False
                self._reason = "Windows 内存状态读取失败（GlobalMemoryStatusEx）"
                return
            mem_total, mem_avail = mem
            mem_used = mem_total - mem_avail if mem_total > 0 else 0
            mem_percent = (mem_used / mem_total * 100.0) if mem_total > 0 else 0.0

            cpu = self._win_cpu_times()
            if cpu is None:
                self._available = False
                self._reason = "Windows CPU 时间读取失败（GetSystemTimes）"
                return
            cpu_busy, cpu_total = cpu
            now = time.time()
            cpu_percent = 0.0
            prev = self._prev_cpu
            prev_t = self._prev_time
            if prev is not None and prev_t is not None and now > prev_t:
                total_delta = cpu_total - prev[1]
                busy_delta = cpu_busy - prev[0]
                if total_delta > 0:
                    cpu_percent = max(0.0, min(100.0, busy_delta / total_delta * 100.0))
            self._prev_cpu = (cpu_busy, cpu_total)
            self._prev_time = now

            self._cpu_percent = cpu_percent
            self._mem_percent = mem_percent
            self._mem_total = mem_total
            self._mem_used = mem_used
            self._available = True
            self._reason = ""

    @staticmethod
    def _win_mem_status() -> tuple[int, int] | None:
        """读取 Windows 内存状态；失败返回 None（由调用方置 _available=False）"""
        try:
            import ctypes

            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            stat = MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))  # type: ignore[attr-defined]
            return int(stat.ullTotalPhys), int(stat.ullAvailPhys)
        except Exception:
            return None

    @staticmethod
    def _win_cpu_times() -> tuple[float, float] | None:
        """读取 Windows CPU 时间；失败返回 None（由调用方置 _available=False）"""
        try:
            import ctypes

            idle = ctypes.c_ulonglong(0)
            kernel = ctypes.c_ulonglong(0)
            user = ctypes.c_ulonglong(0)
            ctypes.windll.kernel32.GetSystemTimes(  # type: ignore[attr-defined]
                ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)
            )
            # kernel 时间包含了 idle，busy = kernel + user - idle
            total = float(kernel.value) + float(user.value)
            busy = total - float(idle.value)
            return max(0.0, busy), max(0.0, total)
        except Exception:
            return None

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "available": self._available,
                "reason": self._reason,
                "cpu_percent": round(self._cpu_percent, 1),
                "mem_percent": round(self._mem_percent, 1),
                "mem_used": self._mem_used,
                "mem_total": self._mem_total,
                "cpu_count": self._cpu_count,
                "platform": self._system,
                "interval": self.interval,
            }
