"""系统级 API：总览、资源指标、日志（含 SSE 流）与框架热重载。"""

from __future__ import annotations

import json
import os
import platform
import queue
import re
import signal as _signal
import threading
import time
from typing import Any

from ...i18n import DEFAULT_LANGUAGE, t as _t
from .base import RouteMixin


class SystemApi(RouteMixin):

    def api_overview(self, m: re.Match, query: dict[str, str]) -> None:
        plugin = self.plugin
        cm = plugin.config_manager
        adapter = plugin.adapter
        primary = adapter.primary() if hasattr(adapter, "primary") else adapter

        # 在线玩家必须从游戏主线程读取，否则非主线程访问 BDS 数据会导致崩溃
        players_box: list[list[str]] = [[]]
        done = threading.Event()

        def fetch_players() -> None:
            try:
                players_box[0] = [p.name for p in plugin.server.online_players]
            except Exception:  # noqa: BLE001
                players_box[0] = []
            finally:
                done.set()

        try:
            plugin.run_on_main(fetch_players)
        except Exception:  # noqa: BLE001
            players_box[0] = []
            done.set()  # 调度失败任务不会执行，手动置位免等满超时
        done.wait(timeout=2.0)

        wl_count = len(plugin.whitelist_module.snapshot()) if getattr(plugin, "whitelist_module", None) else 0
        rules_count = len(getattr(plugin.regex_module, "rules", [])) if getattr(plugin, "regex_module", None) else 0
        sub_mgr = getattr(plugin, "subplugin_manager", None)

        profiles: list[dict[str, Any]] = []
        if hasattr(plugin, "bot_profiles_snapshot"):
            profiles = plugin.bot_profiles_snapshot()
        elif hasattr(plugin, "bot_profile_snapshot"):
            single = plugin.bot_profile_snapshot()
            profiles = [single] if single else []
        profile = profiles[0] if profiles else {}
        configured_qq = int(cm.connection.get("bot_qq", 0) or 0)
        bot_qq = int(profile.get("qq") or configured_qq)

        # QQ 官方适配器未知属性返回桩函数，ws_type 须校验为 int 才可 JSON 序列化
        raw_mode = getattr(primary, "ws_type", None) if primary else None
        mode_value = raw_mode if isinstance(raw_mode, int) and not isinstance(raw_mode, bool) else -1
        # 用并集：cm.main_groups 仅统计数字群号，官方域会漏
        connections = getattr(plugin, "connections", None)
        group_keys: list[Any] = connections.all_group_keys() if connections is not None else []

        self._send_json({"code": 200, "data": {
            "version": plugin.VERSION,
            "connected": bool(adapter and adapter.is_connected),
            "mode": mode_value,
            "mode_name": adapter.mode_name if adapter else _t("webui.msg.not_started"),
            "adapters": adapter.status() if hasattr(adapter, "status") else [],
            "main_group": group_keys[0] if group_keys else 0,
            "main_groups": group_keys,
            "bot_qq": bot_qq,
            "bot_profile": profile,
            "bot_profiles": profiles,
            "online_players": players_box[0],
            "whitelist_count": wl_count,
            "rules_count": rules_count,
            "subplugin_count": len(sub_mgr.subplugins) if sub_mgr else 0,
            "uptime": int(time.time() - self.webui.start_time),
            "python_version": platform.python_version(),
            "os_version": f"{platform.system()} {platform.release()}",
            "pid": os.getpid(),
            "arch": platform.machine(),
            "language": plugin.language if hasattr(plugin, "language") else DEFAULT_LANGUAGE,
        }})

    def api_server_metrics(self, m: re.Match, query: dict[str, str]) -> None:
        self._send_json({"code": 200, "data": self.webui.metrics_collector.snapshot()})

    def api_logs(self, m: re.Match, query: dict[str, str]) -> None:
        self._send_json({"code": 200, "data": self.webui.log_buffer.cache})

    def api_logs_stream(self, m: re.Match, query: dict[str, str]) -> None:
        """SSE 实时日志流：本线程屏蔽 SIGPIPE 防 kill BDS；socket 超时 30s + 最大连接 10min。"""
        # 仅屏蔽本线程 SIGPIPE（Windows 无此信号）
        try:
            _signal.pthread_sigmask(_signal.SIG_BLOCK, {_signal.SIGPIPE})
        except (ValueError, AttributeError, OSError):
            pass

        # 防 send() 在接收窗口满时无限阻塞
        try:
            self.connection.settimeout(30.0)
        except (AttributeError, OSError):
            pass

        q: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=200)
        stopped = self.webui._stop_event
        deadline = time.monotonic() + 600.0

        def on_log(entry: dict[str, Any]) -> None:
            try:
                q.put_nowait(entry)
            except queue.Full:
                # 慢客户端丢最旧一条，防阻塞或内存膨胀
                try:
                    q.get_nowait()
                    q.put_nowait(entry)
                except (queue.Empty, queue.Full):
                    pass

        if not self.webui.log_buffer.subscribe(on_log):
            self.api_error(503, "webui.msg.sse_limit_reached")
            return

        try:
            if not self._safe_send_headers(200, [
                ("Content-Type", "text/event-stream; charset=utf-8"),
                ("Cache-Control", "no-cache, no-store"),
                ("Connection", "keep-alive"),
                ("X-Accel-Buffering", "no"),
                ("Referrer-Policy", "no-referrer"),
                ("X-Content-Type-Options", "nosniff"),
            ]):
                return  # 客户端在响应头阶段就断开

            hello = {
                "level": "info",
                "plugin": "System",
                "msg": _t("logs.stream_connected_msg"),
                "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            first = "retry: 3000\n" + f"data: {json.dumps(hello, ensure_ascii=False)}\n\n"
            if not self._safe_write(first.encode("utf-8")):
                return
            try:
                self.wfile.flush()
            except self._NETWORK_ERRORS:
                return

            while not stopped.is_set():
                if time.monotonic() >= deadline:
                    return
                try:
                    entry = q.get(timeout=1.0)
                    payload = f"data: {json.dumps(entry, ensure_ascii=False)}\n\n"
                except queue.Empty:
                    payload = ": keepalive\n\n"
                if not self._safe_write(payload.encode("utf-8")):
                    return
                try:
                    self.wfile.flush()
                except self._NETWORK_ERRORS:
                    return
        except (BrokenPipeError, ConnectionResetError, OSError, ValueError):
            pass
        except Exception:  # noqa: BLE001
            # 任何意外异常不得传播到主线程
            pass
        finally:
            try:
                self.webui.log_buffer.unsubscribe(on_log)
            except Exception:  # noqa: BLE001
                pass
            self.close_connection = True

    def api_framework_reload(self, m: re.Match, query: dict[str, str]) -> None:
        plugin = self.plugin
        err = [""]

        def do_framework_reload() -> None:
            try:
                plugin.config_manager.load()
                plugin._init_i18n()
                # 失效 pip manager 缓存；加锁避免双检锁竞态
                with plugin._pip_manager_lock:
                    plugin._pip_manager = None
                plugin.reload_onebot_connection()
                if plugin.regex_module:
                    plugin.regex_module.reload_rules()
                if plugin.subplugin_manager:
                    plugin.subplugin_manager.reload_all()
            except Exception as e:  # noqa: BLE001
                err[0] = str(e)

        state, serr = self.run_main_op(do_framework_reload, 20.0)
        if self.main_op_failure(
            state, serr,
            timeout_key="webui.msg.reload_timeout",
            error_key="webui.msg.reload_failed",
        ):
            return
        if err[0]:
            self.api_error(500, "webui.msg.reload_failed", error=err[0])
            return
        self._send_json({"code": 200, "msg": _t("webui.msg.reload_success")})
