"""线程安全日志专项测试

验证 LoggerTee 的行为：
1. 主线程日志直接输出（不经调度器）
2. 后台线程日志经 main_thread_dispatch 调度输出，绝不直接调用底层 logger
3. LogBuffer 在任何线程都实时写入
4. 高并发多线程写日志无异常、无丢失（缓冲区侧）
"""

import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from endstone_lumenbridge.webui import LogBuffer, LoggerTee

PASSED = []
FAILED = []


def check(name, cond, detail=""):
    (PASSED if cond else FAILED).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name} {detail if not cond else ''}")
    if not cond:
        # 失败必须抛错：pytest 收集 test_ 函数时得到真实 FAIL（结构性假绿修复）
        raise AssertionError(f"{name} {detail}".strip())


class RecordingLogger:
    """记录每次调用发生在哪个线程"""

    def __init__(self):
        self.calls = []  # (level, msg, thread_id)
        self.lock = threading.Lock()

    def _rec(self, level, msg):
        with self.lock:
            self.calls.append((level, str(msg), threading.get_ident()))

    def info(self, msg): self._rec("info", msg)
    def warning(self, msg): self._rec("warning", msg)
    def error(self, msg): self._rec("error", msg)
    def debug(self, msg): self._rec("debug", msg)


def _run():
    main_tid = threading.get_ident()

    # 模拟 Endstone scheduler：收集任务，由"主线程"稍后统一执行
    pending = []
    plock = threading.Lock()

    def dispatch(fn):
        with plock:
            pending.append(fn)

    def drain():  # 模拟主线程 tick
        with plock:
            tasks, pending[:] = list(pending), []
        for t in tasks:
            t()

    raw = RecordingLogger()
    buf = LogBuffer()
    tee = LoggerTee(raw, buf, main_thread_dispatch=dispatch)

    print("== 1. 主线程日志直接输出 ==")
    tee.info("main-thread-msg")
    check("主线程日志立即写入底层 logger", any(m == "main-thread-msg" for _, m, _ in raw.calls))
    check("主线程日志不进入调度队列", len(pending) == 0)
    check("主线程日志写入缓冲区", any(e["msg"] == "main-thread-msg" for e in buf.cache))

    print("== 2. 后台线程日志必须经调度器 ==")
    def bg():
        tee.warning("bg-thread-msg")
    t = threading.Thread(target=bg)
    t.start(); t.join()
    check("后台线程日志未直接调用底层 logger",
          not any(m == "bg-thread-msg" for _, m, _ in raw.calls))
    check("后台线程日志已进入调度队列", len(pending) == 1)
    check("后台线程日志实时写入缓冲区（SSE 不延迟）",
          any(e["msg"] == "bg-thread-msg" for e in buf.cache))
    drain()
    calls = [(l, m, tid) for l, m, tid in raw.calls if m == "bg-thread-msg"]
    check("调度后由主线程输出", len(calls) == 1 and calls[0][2] == main_tid)
    check("warning 级别保留", calls[0][0] == "warning")

    print("== 3. 无调度器时（测试环境）直接输出不崩溃 ==")
    raw2 = RecordingLogger()
    tee2 = LoggerTee(raw2, LogBuffer())  # 不传 dispatch
    def bg2():
        tee2.error("no-dispatch-msg")
    t2 = threading.Thread(target=bg2)
    t2.start(); t2.join()
    check("无调度器时后台线程直接输出（降级行为）",
          any(m == "no-dispatch-msg" for _, m, _ in raw2.calls))

    print("== 4. 高并发压力 ==")
    N, THREADS = 200, 8
    buf3 = LogBuffer(max_size=N * THREADS + 10)
    raw3 = RecordingLogger()
    tee3 = LoggerTee(raw3, buf3, main_thread_dispatch=dispatch)
    errors = []
    def worker(wid):
        try:
            for i in range(N):
                tee3.info(f"w{wid}-{i}")
        except Exception as e:
            errors.append(e)
    ts = [threading.Thread(target=worker, args=(w,)) for w in range(THREADS)]
    [t.start() for t in ts]; [t.join() for t in ts]
    drain()
    check("并发写入无异常", not errors, str(errors[:1]))
    check("缓冲区无丢失", len(buf3.cache) == N * THREADS,
          f"got {len(buf3.cache)}")
    check("底层输出全部经主线程", all(tid == main_tid for _, m, tid in raw3.calls if m.startswith("w")))

    print(f"\n===== 结果: {len(PASSED)} 通过, {len(FAILED)} 失败 =====")


# C7：pytest 入口 —— 薄包装驱动真实流程，check 失败抛 AssertionError 即 FAIL
def test_thread_safe_logger_tee():
    _run()


def main():
    try:
        _run()
    except AssertionError:
        pass
    print(f"\n===== 结果: {len(PASSED)} 通过, {len(FAILED)} 失败 =====")
    if FAILED:
        for f in FAILED:
            print(f"  FAILED: {f}")
        sys.exit(1)


if __name__ == "__main__":
    main()
