"""QQ 官方机器人「后台静默日志」开关契约回归。

用户报告：适配器卡片开启后台静默日志后，控制台仍周期性输出
    [QQ官方] access_token 已刷新（3520s 后过期）

审计口径（qq_suppress_log 开关文档，locales/*.json）：
  - 受抑制（运行类日志，静默开启时不打印）：
    连接/断连/重连、token 例行刷新、登录成功（READY）、会话恢复
    （RESUMED）、凭据降级主动发送、重试与补发提示；
  - 不受抑制（异常/一次性事件）：
    发送失败、网关错误码、intent 降级、能力不支持、管理操作结果等。
本文件回归前者中曾遗漏的两处：token 刷新（debug）与 READY 登录成功
（info）——修复前两者均绕过开关直接打印。
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from endstone_lumenbridge.onebot.qqofficial_adapter import QQOfficialAdapter


class FakeLogger:
    def __init__(self) -> None:
        self.logs: list[tuple[str, str]] = []

    def _log(self, level: str, msg: object) -> None:
        self.logs.append((level, str(msg)))

    def debug(self, msg: object, *a: object) -> None:
        self._log("debug", msg)

    def info(self, msg: object, *a: object) -> None:
        self._log("info", msg)

    def warning(self, msg: object, *a: object) -> None:
        self._log("warning", msg)

    def error(self, msg: object, *a: object) -> None:
        self._log("error", msg)

    def exception(self, msg: object, *a: object) -> None:
        self._log("error", msg)


class FakeBus:
    def emit(self, *_args, **_kwargs) -> None:
        pass


def _make_adapter(suppress: bool) -> tuple[QQOfficialAdapter, FakeLogger]:
    logger = FakeLogger()
    adapter = QQOfficialAdapter(logger, FakeBus(), suppress_connection_log=suppress)
    return adapter, logger


def _run_async(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class TokenRefreshSuppressTests(unittest.TestCase):
    def _refresh(self, adapter: QQOfficialAdapter) -> None:
        # 绕过网络：直接提供 token 响应
        adapter._fetch_token_sync = lambda: ("fake-token", 7200)
        _run_async(adapter._access_token_async(force=True))

    def test_token_refresh_silent_when_switch_on(self) -> None:
        """静默开启时 token 刷新不得打印（用户报告的 bug）。"""
        adapter, logger = _make_adapter(suppress=True)
        self._refresh(adapter)
        self.assertEqual(
            [entry for entry in logger.logs if entry[0] == "debug"],
            [],
        )

    def test_token_refresh_printed_when_switch_off(self) -> None:
        """排障需求：关闭静默后 token 刷新照常打印。"""
        adapter, logger = _make_adapter(suppress=False)
        self._refresh(adapter)
        debug_logs = [entry[1] for entry in logger.logs if entry[0] == "debug"]
        self.assertEqual(len(debug_logs), 1)
        self.assertIn("access_token", debug_logs[0])

    def test_valid_cached_token_never_logs(self) -> None:
        """token 缓存有效期内不刷新、不打印（静默关闭时同样）。"""
        adapter, logger = _make_adapter(suppress=False)
        adapter._access_token = "cached"
        adapter._token_expires = 1e18  # 永不过期
        result = _run_async(adapter._access_token_async())
        self.assertEqual(result, "cached")
        self.assertEqual(logger.logs, [])


class ReadyLogSuppressTests(unittest.TestCase):
    def _ready(self, adapter: QQOfficialAdapter) -> None:
        # _dispatch_queue 为 None 时 _emit_pack 安全回退到 bus.emit；
        # on_ready 仅访问会话/资料字段，无需事件循环
        adapter.on_ready({"session_id": "s1", "user": {"username": "TestBot"}})

    def test_ready_silent_when_switch_on(self) -> None:
        """静默开启时 READY「登录成功」不得打印（连接类运行日志，
        修复前绕过开关；connecting/connected/resumed 均已受控）。"""
        adapter, logger = _make_adapter(suppress=True)
        self._ready(adapter)
        self.assertEqual(logger.logs, [])

    def test_ready_printed_when_switch_off(self) -> None:
        """排障需求：关闭静默后 READY 照常打印。"""
        adapter, logger = _make_adapter(suppress=False)
        self._ready(adapter)
        info_logs = [entry[1] for entry in logger.logs if entry[0] == "info"]
        self.assertEqual(len(info_logs), 1)


if __name__ == "__main__":
    unittest.main()
