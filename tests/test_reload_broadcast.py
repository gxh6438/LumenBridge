"""回归：服务端 /reload 后误播报"服务器已关闭/已启动"。

Endstone 的 reload 会 disable → 重新 enable 全部插件：
- 旧实例 on_disable 无条件播关服（误报①）；
- 新实例 bot 上线后 on_server_start 播开服（误报②）。

修复双向判定：
- 关服侧：reload 意图窗口（命令事件 / 框架更新标记）内禁用 → 跳过关服播报；
- 开服侧：server.start_time 与持久化记录相同（同一进程 reload）→ 跳过开服播报。
"""

from __future__ import annotations

import tempfile
import threading
import unittest
import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from endstone_lumenbridge.plugin import (
    _RELOAD_INTENT_WINDOW,
    _SERVER_START_TIME_FILE,
    LumenBridgePlugin,
)


class _FakeLogger:
    def _log(self, msg, *args, **kwargs):
        pass

    info = warning = error = debug = _log


class _FakeServer:
    def __init__(self, start_time: datetime):
        self.start_time = start_time


class _FakeChatSync:
    """记录开服/关服播报调用次数。"""

    def __init__(self):
        self.start_calls: list[str] = []
        self.stop_calls: list[str] = []

    def on_server_start(self) -> None:
        self.start_calls.append("start")

    def on_server_stop(self) -> None:
        self.stop_calls.append("stop")


def _make_plugin(data_dir: Path, start_time: datetime, *, started: bool = False):
    """__new__ 桩：只补齐 on_disable / _on_bot_online 涉及的属性。

    注意：endstone Plugin.logger 为只读 property（C++ 侧），实例赋值抛
    AttributeError，logger 经 _patch_logger() 在类级遮蔽注入。
    """
    plugin = LumenBridgePlugin.__new__(LumenBridgePlugin)
    plugin._tee_logger = None
    plugin.server = _FakeServer(start_time)
    plugin.data_folder = str(data_dir)
    plugin.chat_sync_module = _FakeChatSync()
    plugin._bot_profile_lock = threading.Lock()
    plugin._started = started
    plugin._reload_requested_at = 0.0
    # on_disable 触碰但允许为 None 的成员
    plugin._market_check_stop = threading.Event()
    plugin._market_thread = None
    plugin.webui = None
    plugin.subplugin_manager = None
    plugin.hub = None
    plugin.bus = None
    plugin._pip_manager_lock = threading.Lock()
    plugin._pip_manager = None
    plugin._bot_profiles = {}
    plugin.config_manager = None
    plugin.connections = None
    return plugin


class _LoggerShadowMixin(unittest.TestCase):
    """类级遮蔽 endstone Plugin 的只读 property（logger/server/data_folder）。

    __new__ 构造的实例没有 C++ 侧对象，直接读 self.logger 等会段错误，
    实例赋值则抛 AttributeError（property 无 setter）；patch 在子类上新增
    同名属性遮蔽父类 property，用例结束删除恢复继承。
    """

    def setUp(self) -> None:
        for name, value in (
            ("logger", _FakeLogger()),
            ("server", None),
            ("data_folder", ""),
        ):
            patcher = mock.patch.object(LumenBridgePlugin, name, new=value)
            patcher.start()
            self.addCleanup(patcher.stop)


class ReloadCommandDetectionTests(unittest.TestCase):
    """_note_reload_if_reload_command：仅全服 reload 计入意图。"""

    def _plugin(self):
        plugin = LumenBridgePlugin.__new__(LumenBridgePlugin)
        plugin._reload_requested_at = 0.0
        return plugin

    def test_reload_variants_marked(self):
        for cmd in ("reload", "/reload", "RELOAD", "  reload  ", "/Reload extra"):
            plugin = self._plugin()
            plugin._note_reload_if_reload_command(cmd)
            self.assertGreater(plugin._reload_requested_at, 0.0, msg=cmd)

    def test_other_commands_not_marked(self):
        # /lumen reload 是插件级热重载（不禁用插件、不该静默关服播报），
        # 其他命令同样不得误触发窗口
        for cmd in ("lumen reload", "/lumen reload", "reloadplayer", "say reload", ""):
            plugin = self._plugin()
            plugin._note_reload_if_reload_command(cmd)
            self.assertEqual(plugin._reload_requested_at, 0.0, msg=cmd)

    def test_window_expiry(self):
        plugin = self._plugin()
        plugin.note_reload_intent()
        self.assertTrue(plugin._is_reload_pending())
        # 手动把时间戳拨回窗口之外（不依赖真实睡眠）
        plugin._reload_requested_at -= _RELOAD_INTENT_WINDOW + 1.0
        self.assertFalse(plugin._is_reload_pending())


class ServerStopBroadcastTests(_LoggerShadowMixin):
    """on_disable：reload 窗口内跳过关服播报，真实停服正常播报。"""

    def test_stop_broadcast_on_real_shutdown(self):
        with tempfile.TemporaryDirectory() as d:
            plugin = _make_plugin(Path(d), datetime.now())
            chat = plugin.chat_sync_module
            plugin.on_disable()
            self.assertEqual(chat.stop_calls, ["stop"])

    def test_stop_suppressed_during_reload(self):
        with tempfile.TemporaryDirectory() as d:
            plugin = _make_plugin(Path(d), datetime.now())
            chat = plugin.chat_sync_module
            plugin.note_reload_intent()  # 玩家 /reload 或框架更新
            plugin.on_disable()
            self.assertEqual(chat.stop_calls, [])

    def test_stop_suppressed_via_command_probe(self):
        with tempfile.TemporaryDirectory() as d:
            plugin = _make_plugin(Path(d), datetime.now())
            chat = plugin.chat_sync_module
            # 模拟控制台执行 reload 命令触发的事件处理器
            plugin._on_server_command_reload_probe(SimpleNamespace(command="reload"))
            plugin.on_disable()
            self.assertEqual(chat.stop_calls, [])


class ServerStartBroadcastTests(_LoggerShadowMixin):
    """_on_bot_online：同进程 reload 不播开服，新进程正常播。"""

    def test_first_start_broadcasts(self):
        with tempfile.TemporaryDirectory() as d:
            plugin = _make_plugin(Path(d), datetime.now(), started=False)
            plugin._on_bot_online(None)
            self.assertEqual(plugin.chat_sync_module.start_calls, ["start"])

    def test_reload_restart_does_not_broadcast(self):
        # 进程启动时间不变（reload 不换进程）→ 第二次上线属 reload 重启
        boot = datetime(2026, 10, 7, 12, 0, 0)
        with tempfile.TemporaryDirectory() as d:
            first = _make_plugin(Path(d), boot, started=False)
            first._on_bot_online(None)  # 首次开服：播报并落盘 start_time
            self.assertEqual(first.chat_sync_module.start_calls, ["start"])

            reloaded = _make_plugin(Path(d), boot, started=False)
            reloaded._on_bot_online(None)  # reload 后 bot 重连：不播
            self.assertEqual(reloaded.chat_sync_module.start_calls, [])

    def test_new_process_broadcasts_again(self):
        boot = datetime(2026, 10, 7, 12, 0, 0)
        with tempfile.TemporaryDirectory() as d:
            first = _make_plugin(Path(d), boot, started=False)
            first._on_bot_online(None)
            # 真重启：server.start_time 变化 → 重新播报并更新记录
            restarted = _make_plugin(Path(d), boot + timedelta(hours=2), started=False)
            restarted._on_bot_online(None)
            self.assertEqual(restarted.chat_sync_module.start_calls, ["start"])

    def test_start_time_file_persisted(self):
        boot = datetime(2026, 10, 7, 12, 0, 0)
        with tempfile.TemporaryDirectory() as d:
            plugin = _make_plugin(Path(d), boot, started=False)
            plugin._on_bot_online(None)
            saved = (Path(d) / _SERVER_START_TIME_FILE).read_text(encoding="utf-8")
            self.assertEqual(saved, boot.isoformat())

    def test_started_flag_still_guards_duplicates(self):
        # _started 去重逻辑不受影响：同一实例内重复上线只播一次
        boot = datetime.now()
        with tempfile.TemporaryDirectory() as d:
            plugin = _make_plugin(Path(d), boot, started=False)
            plugin._on_bot_online(None)
            plugin._started = False  # on_disable 重置后的重复上线
            plugin._on_bot_online(None)
            # 第二次因 start_time 记录已写为本进程 → 判定 reload → 不播
            self.assertEqual(plugin.chat_sync_module.start_calls, ["start"])


class DegradedEnvironmentTests(_LoggerShadowMixin):
    """start_time 不可用等异常场景保持旧行为（播报，不抛异常）。"""

    def test_missing_start_time_still_broadcasts(self):
        class _BrokenServer:
            @property
            def start_time(self):
                raise RuntimeError("boom")

        with tempfile.TemporaryDirectory() as d:
            plugin = _make_plugin(Path(d), datetime.now(), started=False)
            plugin.server = _BrokenServer()
            plugin._on_bot_online(None)
            self.assertEqual(plugin.chat_sync_module.start_calls, ["start"])

    def test_missing_data_folder_still_broadcasts(self):
        # data_folder 指向不可写路径：读/写失败 → 保守播报
        boot = datetime.now()
        plugin = _make_plugin(Path("/nonexistent-root-xyz/lumen"), boot, started=False)
        plugin._on_bot_online(None)
        self.assertEqual(plugin.chat_sync_module.start_calls, ["start"])


if __name__ == "__main__":
    unittest.main()
