"""WebUI HTTP 服务器（纯标准库 ThreadingHTTPServer，无第三方 Web 框架）。

本模块只负责 HTTP 层（连接管理、请求体读取、响应发送、鉴权、静态资源）
与服务器生命周期；API 路由逻辑见 ``routes/`` 包，敏感配置处理见 ``sensitive.py``。
"""

from __future__ import annotations

import gzip
import json
import re
import sys
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from ..i18n import t as _t
from . import auth as auth_util
from .configform import ConfigFormBuilder
from .logbuffer import LogBuffer
from .metrics import ServerMetricsCollector
from .routes import ApiRoutes, dispatch_api

if TYPE_CHECKING:
    from ..plugin import LumenBridgePlugin

STATIC_DIR = Path(__file__).parent / "static"

import socket as _socket_mod  # noqa: E402

_MSG_NOSIGNAL = getattr(_socket_mod, "MSG_NOSIGNAL", 0)
_SO_NOSIGPIPE = getattr(_socket_mod, "SO_NOSIGPIPE", 0)


class _NosigWFile:
    """包装 wfile，write 改为带 MSG_NOSIGNAL 的 send，避免向已关闭 socket 写响应触发 SIGPIPE 杀进程。"""

    def __init__(self, wfile: Any, sock: _socket_mod.socket) -> None:
        self._wfile = wfile
        self._sock = sock
        if _SO_NOSIGPIPE:
            try:
                self._sock.setsockopt(_socket_mod.SOL_SOCKET, _SO_NOSIGPIPE, 1)
            except OSError:
                pass

    def write(self, data: bytes) -> int:
        if _MSG_NOSIGNAL:
            sent = 0
            total = len(data)
            try:
                while sent < total:
                    n = self._sock.send(data[sent:], _MSG_NOSIGNAL)
                    if n <= 0:
                        raise BrokenPipeError
                    sent += n
                return sent
            except BlockingIOError:
                # 仅回退写入【未发出】的剩余部分，避免前缀重复破坏响应流
                if sent >= total:
                    return sent
                data = data[sent:]
        return self._wfile.write(data)

    def flush(self) -> None:
        self._wfile.flush()

    def close(self) -> None:
        self._wfile.close()

    def __getattr__(self, name: str) -> Any:
        # 私有属性缺失时直接抛 AttributeError，防止构造未完成/拷贝/反序列化时无限递归
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self.__dict__["_wfile"], name)


MIME_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
}

_GZIP_SUFFIXES = frozenset({".html", ".js", ".css", ".json", ".svg", ".txt", ".md"})
_GZIP_CACHE: dict[Path, tuple[int, int, bytes]] = {}
_GZIP_CACHE_MAX = 64
_gzip_lock = threading.Lock()


def _gzip_static(path: Path, mtime_ns: int, size: int) -> bytes | None:
    """压缩静态文本资源；结果按文件 (mtime, size) 缓存，文件变更自动失效。"""
    with _gzip_lock:
        hit = _GZIP_CACHE.get(path)
    if hit is not None and hit[0] == mtime_ns and hit[1] == size:
        return hit[2]
    try:
        data = path.read_bytes()
    except OSError:
        return None
    compressed = gzip.compress(data, 6)
    if len(compressed) >= size:
        return None
    with _gzip_lock:
        if len(_GZIP_CACHE) >= _GZIP_CACHE_MAX:
            _GZIP_CACHE.clear()
        _GZIP_CACHE[path] = (mtime_ns, size, compressed)
    return compressed


# 上传大小上限 16MB，防内存 DoS
_MAX_UPLOAD_BYTES = 16 * 1024 * 1024

# 进程级密钥挂在 sys 属性上：热重载复用，不落盘
_PROCESS_SECRET_ATTR = "_lumenbridge_webui_secret"


# 面板面向内网部署：不做内网地址拦截；防护 = 仅 http(s) + 初始 URL 白名单
# （调用侧）+ 重定向次数上限 + 大小/超时上限，允许 30x 跳转到其它主机
class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """允许跨主机重定向（如链接跳转到 CDN），但仍由 urllib 内置的
    次数上限（10 次）与协议约束兜底；初始 URL 的白名单校验在调用侧。"""

    max_redirections = 10


def _fetch_url_bytes(
    url: str, max_bytes: int, timeout: float = 30.0, headers: dict[str, str] | None = None,
    on_progress: Any = None,
) -> bytes:
    """按防护策略下载；失败统一抛 RuntimeError("下载失败")，不回显细节。

    允许 30x 跳转到其它主机（初始 URL 白名单由调用方校验）。
    on_progress(loaded, total)：total 为 0 表示响应未声明长度。
    """
    req_headers = {"User-Agent": "LumenBridge-WebUI"}
    if headers:
        req_headers.update(headers)
    opener = urllib.request.build_opener(_SafeRedirectHandler)
    try:
        req = urllib.request.Request(url, headers=req_headers)
        with opener.open(req, timeout=timeout) as resp:
            total = 0
            length_header = resp.headers.get("Content-Length")
            if length_header and length_header.isdigit():
                total = min(int(length_header), max_bytes)
            chunks: list[bytes] = []
            loaded = 0
            while True:
                chunk = resp.read(64 * 1024)
                if not chunk:
                    break
                loaded += len(chunk)
                if loaded > max_bytes:
                    raise RuntimeError(f"文件超过大小限制（{max_bytes} 字节）")
                chunks.append(chunk)
                if on_progress is not None:
                    try:
                        on_progress(loaded, total)
                    except Exception:  # noqa: BLE001 - 进度回调失败不影响下载
                        pass
            data = b"".join(chunks)
    except RuntimeError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(_t("webui.msg.download_failed")) from exc
    if len(data) > max_bytes:
        raise RuntimeError(f"文件超过大小限制（{max_bytes} 字节）")
    return data


class _NullLock:
    """空 contextmanager：当 ConfigManager 未提供 _save_lock 时的占位"""

    def __enter__(self) -> "_NullLock":
        return self

    def __exit__(self, *args: Any) -> None:
        pass


class WebUIServer:
    """LumenBridge Web 管理面板服务器"""

    def __init__(self, plugin: "LumenBridgePlugin") -> None:
        self.plugin = plugin
        self.logger = getattr(plugin, "_tee_logger", None) or plugin.logger
        self.log_buffer: LogBuffer = plugin.log_buffer

        webui_conf = plugin.config_manager.data.get("webui", {})
        self.host: str = str(webui_conf.get("host") or "127.0.0.1")
        self.port: int = int(webui_conf.get("port") or 8300)
        # ""/"*" 或空白视为未设置（随机密码）；明文首次登录后自动迁移
        raw_password = str(webui_conf.get("password") or "").strip()
        self.password: str = raw_password if raw_password else "*"
        self._password_from_config: bool = bool(raw_password)
        self.secret: str = str(webui_conf.get("secret") or self._process_scoped_secret())

        if not self.password or self.password == "*":
            self.password = auth_util.generate_password(8)
            # 密码只 print 到控制台（不经 LoggerTee），避免明文进入 WebUI 日志/SSE 被回看
            print(f"[WebUI] {_t('plugin.random_password', password=self.password)}", flush=True)
            self.logger.info(_t("webui.log.random_password_printed"))

        self.auth_provider = auth_util.AuthProvider(self.secret)

        # 主线程长操作互斥：防超时返回后后台任务仍在执行又叠加新操作
        self._main_op_lock = threading.Lock()

        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        # 活跃连接注册表：shutdown 只关监听 socket，stop() 须主动断开防僵尸连接
        self._live_conns: set[_socket_mod.socket] = set()
        self._conn_lock = threading.Lock()

        # 跨线程读写，须持 _ext_lock
        self.custom_apis: dict[tuple[str, str], tuple[Callable, bool]] = {}
        self.custom_pages: list[dict[str, str]] = []
        self.plugins_config_schema: dict[str, dict[str, Any]] = {}
        self._ext_lock = threading.RLock()
        # 兼容旧引用
        self._schema_lock = self._ext_lock
        self.start_time = time.time()

        self._pip_tasks: dict[str, dict[str, Any]] = {}
        self._pip_tasks_lock = threading.Lock()
        # 与 marketplace 共享串行锁，防并发写坏 site-packages
        self._pip_serial_lock = plugin._pip_serial_lock
        self._pip_list_cache: tuple[float, list[dict[str, Any]]] | None = None
        self._pip_list_lock = threading.Lock()
        self._market_tasks: dict[str, dict[str, Any]] = {}
        self._market_tasks_lock = threading.Lock()

        self._qr_bind_tasks: dict[str, dict[str, Any]] = {}
        self._qr_bind_lock = threading.Lock()

        self._login_guard_lock = threading.Lock()
        self._login_guard: dict[str, dict[str, Any]] = {}

        self._qr_create_lock = threading.Lock()
        self._qr_create_last: dict[str, float] = {}

        self.metrics_collector = ServerMetricsCollector(self.logger, interval=3.0)

    def _process_scoped_secret(self) -> str:
        """进程级签名密钥（不落盘）：热重载经 sys 属性复用，重启重生成。"""
        legacy = Path(self.plugin.data_folder) / "data" / "webui_secret.txt"
        try:
            if legacy.is_file():
                legacy.unlink()
        except OSError:
            pass
        secret = getattr(sys, _PROCESS_SECRET_ATTR, None)
        if isinstance(secret, str) and secret:
            return secret
        secret = auth_util.generate_secret()
        try:
            setattr(sys, _PROCESS_SECRET_ATTR, secret)
        except Exception:  # noqa: BLE001
            pass  # sys 模块只读的极端环境：退化为实例级密钥
        return secret

    def start(self) -> None:
        if self._httpd:
            return
        self._stop_event.clear()
        try:
            import signal as _signal
            _signal.signal(_signal.SIGPIPE, _signal.SIG_IGN)
        except (ValueError, AttributeError, OSError):
            pass
        server = self

        class Handler(_RequestHandler):
            webui = server

        try:
            self._httpd = ThreadingHTTPServer((self.host, self.port), Handler)
        except OSError as e:
            self.logger.error(_t("plugin.webui_port_failed", port=self.port, error=e))
            return
        self._httpd.daemon_threads = True
        self._install_sigpipe_ignore()
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, name="LumenBridge-WebUI", daemon=True
        )
        self._thread.start()
        self.metrics_collector.start()
        self.logger.info(_t("plugin.webui_started", url=f"http://{self.host}:{self.port}"))
        if self.host == "0.0.0.0":
            self.logger.info(_t("plugin.webui_listening_all", port=self.port))

    @staticmethod
    def _install_sigpipe_ignore() -> None:
        """SIGPIPE 设为忽略防 broken pipe 杀进程；仅在 SIG_DFL 时改写。"""
        try:
            import ctypes
            import ctypes.util

            libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
            SIG_DFL, SIG_IGN, SIGPIPE = 0, 1, 13
            # signal() 返回旧处理器指针，restype 须为 c_void_p：按 c_int 截断后恢复会跳坏地址崩溃
            libc.signal.restype = ctypes.c_void_p
            libc.signal.argtypes = [ctypes.c_int, ctypes.c_void_p]
            old = libc.signal(SIGPIPE, SIG_IGN)
            if old not in (SIG_DFL, SIG_IGN, None):
                libc.signal(SIGPIPE, ctypes.c_void_p(old))
        except (OSError, AttributeError, ValueError):
            pass

        try:
            import signal

            sigpipe = getattr(signal, "SIGPIPE", None)
            if sigpipe is not None:
                current = signal.getsignal(sigpipe)
                if current == signal.SIG_DFL:
                    signal.signal(sigpipe, signal.SIG_IGN)
        except (ValueError, OSError, RuntimeError):
            # 非主线程抛 ValueError，可忽略
            pass

    @staticmethod
    def _is_sigpipe_ignored() -> bool:
        """探测当前 SIGPIPE 是否处于被忽略状态（用于诊断）。"""
        try:
            import signal

            sigpipe = getattr(signal, "SIGPIPE", None)
            if sigpipe is None:
                return True  # Windows 无 SIGPIPE 概念，视为已忽略
            return signal.getsignal(sigpipe) == signal.SIG_IGN
        except (ValueError, OSError):
            return False

    def stop(self) -> None:
        self._stop_event.set()
        self.metrics_collector.stop()
        # 先断开活跃连接再 shutdown()：防旧实例 daemon 线程继续应答
        with self._conn_lock:
            conns = list(self._live_conns)
            self._live_conns.clear()
        for conn in conns:
            try:
                conn.shutdown(_socket_mod.SHUT_RDWR)
            except OSError:
                pass
        httpd = self._httpd
        self._httpd = None
        if httpd:
            httpd.shutdown()
            httpd.server_close()
        thread = self._thread
        self._thread = None
        if thread and thread is not threading.current_thread():
            thread.join(timeout=3)
        if httpd:
            self.logger.info(_t("plugin.webui_stopped"))

    @property
    def is_running(self) -> bool:
        return self._httpd is not None

    # ---- 登录防护 ----

    LOGIN_MAX_ATTEMPTS_PER_WINDOW = 10
    LOGIN_WINDOW_SECONDS = 10.0
    LOGIN_FAIL_THRESHOLD = 5
    LOGIN_LOCK_BASE_SECONDS = 60.0
    LOGIN_LOCK_MAX_SECONDS = 900.0
    # 超时未再失败即清零，避免"连续"退化为"累计"导致永久锁定
    LOGIN_FAIL_DECAY_SECONDS = 300.0

    def _login_check_allowed(self, ip: str) -> tuple[bool, str]:
        """登录前置检查：锁定状态与请求频率窗口。返回 (是否放行, 提示消息)。"""
        now = time.time()
        with self._login_guard_lock:
            if len(self._login_guard) > 4096:
                stale = [
                    key for key, state in self._login_guard.items()
                    if state["locked_until"] <= now
                    and now - state.get("last_fail", 0.0) >= self.LOGIN_FAIL_DECAY_SECONDS
                ]
                for key in stale:
                    self._login_guard.pop(key, None)
            state = self._login_guard.get(ip)
            if state is not None and state["locked_until"] > now:
                wait = int(state["locked_until"] - now) + 1
                return False, _t("webui.msg.login_locked", wait=wait)
            if state is None:
                state = {"fails": 0, "locked_until": 0.0, "window_start": now, "attempts": 0, "last_fail": 0.0}
                self._login_guard[ip] = state
            elif now - state["window_start"] >= self.LOGIN_WINDOW_SECONDS:
                state["window_start"] = now
                state["attempts"] = 0
            if state["fails"] and now - state.get("last_fail", 0.0) >= self.LOGIN_FAIL_DECAY_SECONDS:
                state["fails"] = 0
            if state["attempts"] >= self.LOGIN_MAX_ATTEMPTS_PER_WINDOW:
                return False, _t("webui.msg.login_too_fast")
            state["attempts"] += 1
            return True, ""

    def _login_record_result(self, ip: str, success: bool) -> None:
        """登录结果回写：失败累计并指数退避锁定，成功清零。"""
        now = time.time()
        with self._login_guard_lock:
            state = self._login_guard.get(ip)
            if state is None:
                return
            if success:
                self._login_guard.pop(ip, None)
                return
            state["fails"] += 1
            state["last_fail"] = now
            if state["fails"] >= self.LOGIN_FAIL_THRESHOLD:
                exceed = state["fails"] - self.LOGIN_FAIL_THRESHOLD + 1
                lock = min(self.LOGIN_LOCK_BASE_SECONDS * (2 ** (exceed - 1)), self.LOGIN_LOCK_MAX_SECONDS)
                state["locked_until"] = now + lock

    def _upgrade_password_storage(self, plain: str) -> None:
        """首次明文登录成功后迁移为 pbkdf2 哈希；随机密码不参与。"""
        try:
            hashed = auth_util.hash_password(plain)
            cm = self.plugin.config_manager
            if cm is not None:
                cm.apply_patch({"webui": {"password": hashed}})
            self.password = hashed
            self._password_from_config = True
            self.logger.info(_t("webui.log.password_migrated"))
        except Exception:  # noqa: BLE001
            # 失败不影响登录，下次再试
            pass

    def _apply_webui_secret_changes(self, cm: Any) -> None:
        """配置保存后按新值刷新密码/密钥；变更即失效既有 token。"""
        new_webui_conf = cm.data.get("webui", {}) or {}
        new_password = str(new_webui_conf.get("password") or "").strip()
        new_secret = str(new_webui_conf.get("secret") or "").strip()
        if new_password and new_password != self.password and set(new_password) != {"*"}:
            self.password = new_password
            self._password_from_config = True
            self.auth_provider.invalidate_tokens()
        if new_secret and new_secret != self.secret and set(new_secret) != {"*"}:
            self.secret = new_secret
            self.auth_provider.set_secret(new_secret)

    MAX_CONSOLE_PASSWORD_LENGTH = 128

    def set_webui_password(self, plain: str) -> None:
        """控制台重置密码：哈希落盘并使全部 token 失效；无效输入抛 ValueError。"""
        pw = str(plain).strip()
        if not pw:
            raise ValueError("empty password")
        if len(pw) > self.MAX_CONSOLE_PASSWORD_LENGTH:
            raise ValueError("password too long")
        hashed = auth_util.hash_password(pw)
        cm = self.plugin.config_manager
        if cm is not None:
            cm.apply_patch({"webui": {"password": hashed}})
        self.password = hashed
        self._password_from_config = True
        self.auth_provider.invalidate_tokens()

    def clear_webui_password(self) -> str:
        """清除密码转随机模式：配置写回 "*"，新密码打印到控制台（不走 logger，防回看）；token 全部失效。"""
        cm = self.plugin.config_manager
        if cm is not None:
            cm.apply_patch({"webui": {"password": "*"}})
        new_pw = auth_util.generate_password(12)
        self.password = new_pw
        self._password_from_config = False
        self.auth_provider.invalidate_tokens()
        return new_pw

    def refresh_config(self) -> None:
        """按最新配置刷新运行参数：密码/密钥变更失效 token，host/port 变更重启监听；留空项保持现值。"""
        conf: dict[str, Any] = {}
        if self.plugin.config_manager:
            raw = self.plugin.config_manager.data.get("webui", {})
            if isinstance(raw, dict):
                conf = raw

        new_password = str(conf.get("password") or "").strip()
        if new_password and new_password != "*" and new_password != self.password:
            self.password = new_password
            self._password_from_config = True
            self.auth_provider.invalidate_tokens()
            self.logger.info(_t("webui.log.password_updated"))

        new_secret = str(conf.get("secret") or "")
        if new_secret and new_secret != self.auth_provider.secret:
            self.auth_provider.set_secret(new_secret)
            # 同步实例属性：否则保存路径会误判密钥又变更而重复失效 token
            self.secret = new_secret
            self.logger.info(_t("webui.log.secret_updated"))

        try:
            new_host = str(conf.get("host") or "127.0.0.1")
            new_port = int(conf.get("port") or 8300)
        except (TypeError, ValueError):
            return
        if new_host == self.host and new_port == self.port:
            return
        self.host = new_host
        self.port = new_port
        if self.is_running:
            self.stop()
            self.start()

    def _schedule_config_hot_apply(self, plugin: Any) -> None:
        """外部手改 config.json 后热生效（语言/监听地址/pip 配置）；延迟 2 tick 让本轮响应先发完。"""
        def _apply() -> None:
            init_i18n = getattr(plugin, "_init_i18n", None)
            if callable(init_i18n):
                try:
                    init_i18n()
                except Exception:  # noqa: BLE001
                    pass
            try:
                with getattr(plugin, "_pip_manager_lock", threading.RLock()):
                    if getattr(plugin, "_pip_manager", None) is not None:
                        plugin._pip_manager = None
            except Exception:  # noqa: BLE001
                pass
            try:
                self.refresh_config()
            except Exception:  # noqa: BLE001
                pass

        try:
            plugin.run_on_main(_apply, delay=2)
        except Exception:  # noqa: BLE001
            # 调度失败退化为后台定时器
            try:
                threading.Timer(1.0, _apply).start()
            except Exception:  # noqa: BLE001
                pass

    # ---- pip / 市场异步任务 ----

    def _get_pip_manager_for_plugin(self, plugin: "LumenBridgePlugin"):
        """委托给 plugin._get_pip_manager()，复用其双检锁线程安全。"""
        return plugin._get_pip_manager()

    def _start_pip_task(
        self,
        mgr,
        packages: list[str],
        action: str,
        subplugin_name: str | None = None,
        *,
        reload_after_install: bool = False,
    ) -> str | None:
        """启动异步 pip 安装任务，返回 task_id；前端轮询 GET /api/pip/task/<id>。"""
        import uuid
        task_id = uuid.uuid4().hex[:12]
        now = time.time()
        task = {
            "status": "running",
            "log_lines": [],
            "packages": list(packages),
            "done": False,
            "success": False,
            "msg": "",
            "subplugin_name": subplugin_name,
            # reload_required 仅为兼容旧前端字段保留（恒为 False）
            "reload_required": bool(subplugin_name and not reload_after_install),
            "reload_after_install": bool(reload_after_install),
            "action": action,
            "start_time": now,
        }
        with self._pip_tasks_lock:
            if len(self._pip_tasks) >= 20:
                done_tasks = [
                    (tid, t) for tid, t in self._pip_tasks.items()
                    if t.get("done") and now - float(t.get("start_time", 0) or 0) > 300
                ]
                done_tasks.sort(key=lambda x: x[1].get("start_time", 0))
                need_remove = len(self._pip_tasks) - 20 + 1
                for tid, _ in done_tasks[:need_remove]:
                    self._pip_tasks.pop(tid, None)
                # 全部 running 时不强制淘汰（会破坏状态更新），直接拒绝新任务
                if len(self._pip_tasks) >= 20:
                    return None
            self._pip_tasks[task_id] = task

        plugin = self.plugin

        def on_log(line: str) -> None:
            with self._pip_tasks_lock:
                t = self._pip_tasks.get(task_id)
                if t and len(t["log_lines"]) < 500:
                    t["log_lines"].append(line)

        def run_install() -> None:
            install_success = False
            install_msg = ""
            reload_success: bool | None = None
            reload_error = ""
            if action == "uninstall":
                try:
                    with self._pip_serial_lock:
                        install_success, install_msg = mgr.uninstall(packages[0] if packages else "")
                except Exception as exc:  # noqa: BLE001
                    install_msg = str(exc)
                with self._pip_tasks_lock:
                    current = self._pip_tasks.get(task_id)
                    if current:
                        current.update({
                            "done": True, "success": bool(install_success),
                            "installation_success": bool(install_success),
                            "reload_success": None,
                            "reload_required": False,
                            "msg": install_msg or (
                                _t("pip.task_uninstall_done") if install_success else _t("pip.task_uninstall_failed")
                            ),
                            "status": "success" if install_success else "failed",
                        })
                with self._pip_list_lock:
                    self._pip_list_cache = None
                return
            try:
                with self._pip_serial_lock:
                    install_success, install_msg = mgr.install(packages, on_log=on_log)
            except Exception as exc:  # noqa: BLE001
                install_msg = str(exc)

            if install_success:
                # 失效 import 缓存以便 reload 发现新装模块
                try:
                    import importlib
                    importlib.invalidate_caches()
                except Exception:  # noqa: BLE001
                    pass

            if install_success and subplugin_name and reload_after_install:
                reload_success, reload_error = self._reload_subplugin_after_install(plugin, subplugin_name, on_log)

            # 安装与重载状态分离，防依赖写入成功但加载失败时误导
            overall_success = bool(install_success and reload_success is not False)
            if not install_success:
                final_message = install_msg or _t("pip.task_install_failed")
            elif reload_success is False:
                final_message = _t(
                    "pip.task_deps_reload_failed",
                    name=subplugin_name, error=reload_error or _t("pip.task_see_error_detail"),
                )
            elif subplugin_name and not reload_after_install:
                final_message = _t(
                    "pip.task_need_manual_reload",
                    msg=install_msg or _t("pip.task_deps_installed"), name=subplugin_name,
                )
            else:
                final_message = install_msg or _t("pip.task_install_done")
            with self._pip_tasks_lock:
                current = self._pip_tasks.get(task_id)
                if current:
                    current.update({
                        "done": True, "success": overall_success,
                        "installation_success": bool(install_success),
                        "reload_success": reload_success,
                        "reload_required": bool(subplugin_name and not reload_after_install and install_success),
                        "msg": final_message,
                        "status": "success" if overall_success else "failed",
                    })
            with self._pip_list_lock:
                self._pip_list_cache = None

        threading.Thread(target=run_install, name=f"lumen-pip-{task_id}", daemon=True).start()
        return task_id

    def _reload_subplugin_after_install(
        self, plugin: Any, subplugin_name: str, on_log: Callable[[str], None],
    ) -> tuple[bool, str]:
        """安装成功后在主线程重载子插件；返回 (是否成功, 错误信息)。"""
        if not plugin.subplugin_manager:
            return False, _t("pip.task_manager_unavailable")
        reload_done = threading.Event()
        reload_ok = [False]
        reload_exc = [""]

        def do_reload() -> None:
            try:
                reload_ok[0] = bool(plugin.subplugin_manager.reload_one(subplugin_name))
                if not reload_ok[0]:
                    with plugin.subplugin_manager._lock:
                        current = plugin.subplugin_manager.subplugins.get(subplugin_name)
                        reload_exc[0] = (current.error if current else "") or ""
            except Exception as exc:  # noqa: BLE001
                reload_exc[0] = str(exc)
            finally:
                reload_done.set()

        def log_and_return(ok: bool, err: str) -> tuple[bool, str]:
            on_log(
                _t("pip.auto_reload_success", name=subplugin_name) if ok
                else _t("pip.auto_reload_failed", name=subplugin_name, error=err or _t("pip.task_see_error_detail"))
            )
            return ok, err

        try:
            plugin.run_on_main(do_reload, delay=1)
        except Exception as exc:  # noqa: BLE001
            return log_and_return(False, str(exc))
        if not reload_done.wait(timeout=30):
            return log_and_return(False, _t("pip.task_reload_timeout"))
        return log_and_return(reload_ok[0], reload_exc[0])

    def _start_market_task(
        self, action: str,
        runner: Callable[[Callable[[str], None], Callable[[int, str], None]], dict[str, Any]],
    ) -> str | None:
        """启动市场异步任务；runner 接收 log 与 progress 回调，不阻塞请求线程。"""
        import uuid
        task_id = uuid.uuid4().hex[:12]
        task = {
            "status": "running", "done": False, "success": False, "msg": "",
            "action": action, "result": {}, "start_time": time.time(),
            "log_lines": [], "progress": 0, "progress_label": "",
        }
        with self._market_tasks_lock:
            now = time.time()
            completed = sorted(
                (
                    (key, value) for key, value in self._market_tasks.items()
                    if value.get("done") and now - float(value.get("start_time", 0) or 0) > 300
                ),
                key=lambda item: item[1].get("start_time", 0),
            )
            while len(self._market_tasks) >= 20 and completed:
                self._market_tasks.pop(completed.pop(0)[0], None)
            if len(self._market_tasks) >= 20:
                return None
            self._market_tasks[task_id] = task

        def on_log(line: str) -> None:
            with self._market_tasks_lock:
                t = self._market_tasks.get(task_id)
                if t and len(t["log_lines"]) < 500:
                    t["log_lines"].append(line)

        def on_progress(percent: int, label: str = "") -> None:
            with self._market_tasks_lock:
                t = self._market_tasks.get(task_id)
                if t:
                    t["progress"] = max(0, min(100, int(percent)))
                    t["progress_label"] = label

        def execute() -> None:
            try:
                result = runner(on_log, on_progress)
                msg = result.get("message", "") if isinstance(result, dict) else str(result) if result else ""
                with self._market_tasks_lock:
                    current = self._market_tasks.get(task_id)
                    if current:
                        current.update({
                            "status": "success", "done": True, "success": True,
                            "result": result, "msg": msg, "progress": 100,
                        })
            except Exception as exc:  # noqa: BLE001
                with self._market_tasks_lock:
                    current = self._market_tasks.get(task_id)
                    if current:
                        current.update({"status": "failed", "done": True, "success": False, "msg": str(exc)})

        threading.Thread(target=execute, name=f"lumen-market-{task_id}", daemon=True).start()
        return task_id

    @property
    def url(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"http://{host}:{self.port}"

    def _debug_log(self, key: str, **kwargs: Any) -> None:
        """仅在 config.debug 开启时输出（注册类提示默认静默防刷屏）。"""
        try:
            enabled = bool(self.plugin.config_manager.debug)
        except Exception:  # noqa: BLE001
            enabled = False
        if enabled:
            self.logger.debug(_t(key, **kwargs))

    # ---- 子插件扩展注册（context 调用）----

    def register_api(
        self, method: str, path: str, handler: Callable[[dict[str, Any]], Any],
        need_auth: bool = True,
    ) -> None:
        """注册子插件 API 到 /api/plugin/<path>；handler 收到 {"query","body","path","file"}。"""
        full = "/api/plugin" + (path if path.startswith("/") else "/" + path)
        with self._ext_lock:
            self.custom_apis[(method.upper(), full)] = (handler, need_auth)
        self._debug_log("plugin.webui_api_registered", method=method.upper(), path=full)

    def register_config(self, builder: ConfigFormBuilder) -> None:
        """注册配置表单 Schema（由 ConfigFormBuilder.register() 调用）。"""
        with self._ext_lock:
            self.plugins_config_schema[builder.name] = builder.to_schema()
        self._debug_log("plugin.webui_config_registered", name=builder.name)

    def create_config(self, name: str) -> ConfigFormBuilder:
        """Fluent 配置表单构建器入口。"""
        return ConfigFormBuilder(name, self.register_config)

    def register_custom_page(
        self,
        plugin_name: str,
        folder: str,
        title: str,
        relative_path: str,
        tab: bool = False,
        icon: str = "",
    ) -> None:
        """挂载子插件自定义页面；tab=True 进移动端底栏。"""
        import uuid as _uuid
        url = f"/plugin-views/{folder}/{relative_path}"
        with self._ext_lock:
            if any(p["url"] == url for p in self.custom_pages):
                return
            self.custom_pages.append({
                "id": f"{plugin_name}_{_uuid.uuid4().hex[:8]}",
                "pluginName": plugin_name,
                "title": title,
                "url": url,
                "tab": bool(tab),
                "icon": str(icon or "")[:16],
            })
            self._debug_log("plugin.webui_page_registered", plugin=plugin_name, title=title)


class _RequestHandler(BaseHTTPRequestHandler, ApiRoutes):
    """HTTP 请求处理器：连接管理与请求读写在本类，业务路由经 dispatch_api 分派。"""

    webui: WebUIServer  # 由 start() 中的子类注入
    protocol_version = "HTTP/1.1"

    # 断连 socket 异常必须吞掉：防 SIGPIPE 杀 BDS 进程
    _NETWORK_ERRORS = (
        ConnectionResetError,
        ConnectionAbortedError,
        BrokenPipeError,
        TimeoutError,
    )
    # 部分 Python 版本不映射为上面子类的 errno
    _SWALLOW_ERRNOS = {32, 103, 104, 110, 113}

    @classmethod
    def _is_network_error(cls, exc: BaseException) -> bool:
        if isinstance(exc, cls._NETWORK_ERRORS):
            return True
        if isinstance(exc, OSError):
            return getattr(exc, "errno", None) in cls._SWALLOW_ERRNOS
        return False

    @property
    def plugin(self) -> Any:
        return self.webui.plugin

    # ---- 连接生命周期 ----

    def setup(self) -> None:  # type: ignore[override]
        """wfile 包装为 MSG_NOSIGNAL 写，是抵御 SIGPIPE 杀进程的根本手段（即便宿主重置全局 SIGPIPE）。"""
        super().setup()
        try:
            with self.webui._conn_lock:
                self.webui._live_conns.add(self.connection)
        except Exception:  # noqa: BLE001
            pass
        try:
            self.connection.settimeout(30)  # 防 Slowloris 耗尽线程池
        except OSError:
            pass
        try:
            self.wfile = _NosigWFile(self.wfile, self.connection)  # type: ignore[assignment]
        except Exception:  # noqa: BLE001
            pass

    def finish(self) -> None:  # type: ignore[override]
        try:
            with self.webui._conn_lock:
                self.webui._live_conns.discard(self.connection)
        except Exception:  # noqa: BLE001
            pass
        super().finish()

    def handle(self) -> None:  # type: ignore[override]
        try:
            super().handle()
        except BaseException as exc:  # noqa: BLE001
            if self._is_network_error(exc):
                self.close_connection = True
            else:
                raise

    def handle_one_request(self) -> None:  # type: ignore[override]
        try:
            super().handle_one_request()
        except BaseException as exc:  # noqa: BLE001
            if self._is_network_error(exc):
                self.close_connection = True
            else:
                raise

    def version_string(self) -> str:  # type: ignore[override]
        return "LumenBridge"

    def log_message(self, fmt: str, *args: Any) -> None:
        pass

    # ---- 安全写响应 ----

    def _safe_write(self, data: bytes) -> bool:
        """安全写响应体；客户端已断开返回 False，绝不抛异常。"""
        try:
            self.wfile.write(data)
            return True
        except BaseException as exc:  # noqa: BLE001
            if self._is_network_error(exc):
                self.close_connection = True
                return False
            raise

    def _safe_send_headers(self, status: int, headers: list[tuple[str, str]]) -> bool:
        """安全发送状态行+响应头；客户端已断开返回 False。"""
        try:
            self.send_response(status)
            for k, v in headers:
                self.send_header(k, v)
            self.end_headers()
            return True
        except BaseException as exc:  # noqa: BLE001
            if self._is_network_error(exc):
                self.close_connection = True
                return False
            raise

    # ---- 请求解析 ----

    def _parse(self) -> tuple[str, dict[str, str]]:
        parsed = urllib.parse.urlparse(self.path)
        query = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
        return parsed.path, query

    def _read_body_with_deadline(self, length: int) -> bytes | None:
        """按总时限分块读取请求体（Slowloris dribble 防护）。"""
        # 最低 16KB/s 吞吐给足余量：16MB 上限 → 256s；小 body → 30s 下限
        deadline = time.monotonic() + max(30.0, min(300.0, length / 16384.0))
        chunks: list[bytes] = []
        remaining = length
        while remaining > 0:
            if time.monotonic() >= deadline:
                self.close_connection = True
                return None
            try:
                chunk = self.rfile.read1(min(remaining, 64 * 1024))
            except OSError:
                self.close_connection = True
                return None
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def _read_body(self) -> Any:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            self.close_connection = True
            return None
        if length <= 0:
            return None
        if length > _MAX_UPLOAD_BYTES:
            # 未读 body 会污染 keep-alive 下一请求
            self.close_connection = True
            return None
        raw = self._read_body_with_deadline(length)
        if raw is None:
            return None
        self._body_consumed = True
        try:
            return json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None

    def _drain_request_body(self, limit: int = _MAX_UPLOAD_BYTES + 64 * 1024) -> None:
        """尽量消费请求体（请求走私防护）；声明长度超上限由调用方断连兜底。"""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            return
        if length <= 0:
            return
        remaining = min(length, limit)
        try:
            while remaining > 0:
                chunk = self.rfile.read(min(remaining, 64 * 1024))
                if not chunk:
                    break
                remaining -= len(chunk)
        except OSError:
            pass

    def _read_multipart_file(self) -> tuple[str, bytes] | None:
        """解析 multipart/form-data 的第一个文件字段，返回 (文件名, 内容)。"""
        ctype = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in ctype:
            # 前置失败须断连并消费 body，防 keep-alive 请求走私
            self.close_connection = True
            self._drain_request_body()
            return None
        m = re.search(r"boundary=([^;]+)", ctype)
        if not m:
            self.close_connection = True
            self._drain_request_body()
            return None
        boundary = m.group(1).strip().strip('"').encode()
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            self.close_connection = True
            return None
        if length <= 0 or length > _MAX_UPLOAD_BYTES:
            self.close_connection = True
            if length > 0:
                self._drain_request_body()
            return None
        raw = self._read_body_with_deadline(length)
        if raw is None:
            return None
        self._body_consumed = True
        # multipart body 第一个分隔符前有 \r\n，需剥除避免空 part
        prefix = b"\r\n--" + boundary
        if raw.startswith(prefix):
            raw = raw[len(prefix):]
        elif raw.startswith(b"--" + boundary):
            raw = raw[len(b"--" + boundary):]
        for part in raw.split(b"\r\n--" + boundary):
            if part.startswith(b"--"):
                continue
            if b"filename=" not in part:
                continue
            header_end = part.find(b"\r\n\r\n")
            if header_end < 0:
                continue
            headers = part[:header_end].decode("utf-8", "ignore")
            # 兼容转义引号的文件名：filename="a\"b.zip"
            fm = re.search(r'filename="((?:[^"\\]|\\.)*)"', headers)
            filename = fm.group(1).replace('\\"', '"') if fm else "upload.zip"
            return filename, part[header_end + 4:]
        return None

    # ---- 响应 ----

    def _send_json(self, data: Any, status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        if not self._safe_send_headers(status, [
            ("Content-Type", "application/json; charset=utf-8"),
            ("Content-Length", str(len(body))),
            ("Cache-Control", "no-store"),
            ("Referrer-Policy", "no-referrer"),
            ("X-Content-Type-Options", "nosniff"),
        ]):
            return
        self._safe_write(body)

    def _send_file(self, file_path: Path) -> None:
        if not file_path.is_file():
            self._send_json({"code": 404, "msg": _t("webui.msg.not_found")}, 404)
            return
        try:
            st = file_path.stat()
        except OSError:
            self._send_json({"code": 404, "msg": _t("webui.msg.not_found")}, 404)
            return
        size = st.st_size
        if size > 8 * 1024 * 1024:
            self._send_json({"code": 413, "msg": _t("webui.msg.file_too_large")}, 413)
            return
        suffix = file_path.suffix.lower()
        mime = MIME_TYPES.get(suffix, "application/octet-stream")
        etag = f'"{size:x}-{st.st_mtime_ns:x}"'
        cache_hdr = self._static_cache_header(suffix)
        if self.headers.get("If-None-Match") == etag:
            self._safe_send_headers(304, [
                ("ETag", etag),
                ("Cache-Control", cache_hdr),
                ("Referrer-Policy", "no-referrer"),
            ])
            return

        headers = [
            ("Content-Type", mime),
            ("ETag", etag),
            ("Referrer-Policy", "no-referrer"),
            ("X-Content-Type-Options", "nosniff"),
            ("Cache-Control", cache_hdr),
        ]
        gz_body: bytes | None = None
        if (
            suffix in _GZIP_SUFFIXES
            and "gzip" in (self.headers.get("Accept-Encoding") or "")
            and 0 < size <= 2 * 1024 * 1024
        ):
            gz_body = _gzip_static(file_path, st.st_mtime_ns, size)
            if gz_body is not None:
                headers.append(("Content-Encoding", "gzip"))
                headers.append(("Vary", "Accept-Encoding"))

        if not self._safe_send_headers(200, headers + [("Content-Length", str(len(gz_body) if gz_body is not None else size))]):
            return
        if gz_body is not None:
            self._safe_write(gz_body)
            return
        try:
            with file_path.open("rb") as fh:
                while True:
                    chunk = fh.read(64 * 1024)
                    if not chunk:
                        break
                    if not self._safe_write(chunk):
                        return
        except OSError:
            self.close_connection = True

    @staticmethod
    def _static_cache_header(suffix: str) -> str:
        if suffix in (".png", ".ico", ".svg"):
            return "public, max-age=86400"
        # html/js/css 全部 no-cache：保证升级后及时生效
        return "no-cache"

    # ---- 鉴权 ----

    def _check_auth(self, query: dict[str, str]) -> bool:
        header = self.headers.get("Authorization", "")
        token = header[7:] if header.startswith("Bearer ") else query.get("token", "")
        return self.webui.auth_provider.verify_token(token)

    def _unauthorized(self) -> None:
        self._send_json({"code": 401, "msg": _t("webui.msg.unauthorized")}, 401)

    # ---- 主线程长操作互斥 ----

    def _acquire_main_op(
        self, done: threading.Event,
    ) -> tuple[Callable[[], None], Callable[[], None]] | tuple[None, None]:
        """主线程长操作互斥入口，返回 (finish, defer_release)；超时由 defer 起守护线程兜底释放。"""
        if not self.webui._main_op_lock.acquire(blocking=False):
            return None, None
        released = threading.Event()

        def finish() -> None:
            if not released.is_set():
                released.set()
                try:
                    self.webui._main_op_lock.release()
                except RuntimeError:
                    pass

        def defer_release() -> None:
            def _wait_and_release() -> None:
                done.wait()
                finish()
            threading.Thread(target=_wait_and_release, daemon=True).start()

        return finish, defer_release

    # ---- 子插件自定义 API ----

    def call_custom_api(self, hit: tuple[Callable, bool], query: dict[str, str], path: str) -> None:
        handler, need_auth = hit
        if need_auth and not self._check_auth(query):
            return self._unauthorized()
        ctype = self.headers.get("Content-Type", "")
        if "multipart/form-data" in ctype:
            file_info = self._read_multipart_file()
            body: Any = {}
        else:
            file_info = None
            body = self._read_body() if self.command in ("POST", "PUT", "DELETE") else None
        try:
            result = handler({"query": query, "body": body, "path": path, "file": file_info})
        except Exception as exc:  # noqa: BLE001
            self.webui.logger.warning(f"[WebUI] subplugin API error path={path} error={exc!r}")
            return self._send_json({"code": 500, "msg": _t("webui.msg.internal_error")}, 500)
        self._send_json({"code": 200, "data": result})

    # ---- 路由入口 ----

    def do_GET(self) -> None:
        self._route("GET")

    def do_POST(self) -> None:
        self._route("POST")

    def do_PUT(self) -> None:
        self._route("PUT")

    def do_DELETE(self) -> None:
        self._route("DELETE")

    def do_OPTIONS(self) -> None:
        # 仅同源：不回 CORS 头；带 body 时消费并断连
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = -1
        if length > 0:
            self._drain_request_body()
            self.close_connection = True
        self._safe_send_headers(405, [
            ("Allow", "GET, POST, PUT, DELETE"),
            ("Content-Length", "0"),
            ("Referrer-Policy", "no-referrer"),
        ])

    def _route(self, method: str) -> None:
        path, query = self._parse()
        # keep-alive 下同一 handler 串行处理多请求，逐请求重置消费标志
        self._body_consumed = False
        try:
            if path.startswith("/api/"):
                return dispatch_api(self, method, path, query)

            if path.startswith("/plugin-views/"):
                if not self._check_auth(query):
                    return self._unauthorized()
                # 文件名可含空格/中文，先百分号解码
                rel = urllib.parse.unquote(path[len("/plugin-views/"):])
                base = (Path(self.plugin.data_folder) / "plugins").resolve()
                target = (base / rel).resolve()
                # 防目录穿越：relative_to 组件匹配（startswith 不安全）
                try:
                    target.relative_to(base)
                except ValueError:
                    return self._send_json({"code": 403, "msg": _t("webui.msg.forbidden_path")}, 403)
                return self._send_file(target)

            rel = urllib.parse.unquote(path.lstrip("/")) or "index.html"
            static_base = STATIC_DIR.resolve()
            target = (STATIC_DIR / rel).resolve()
            try:
                target.relative_to(static_base)
                in_static = True
            except ValueError:
                in_static = False
            if in_static and target.is_file():
                return self._send_file(target)
            return self._send_file(STATIC_DIR / "index.html")
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:  # noqa: BLE001
            # 响应只返回通用错误，不泄漏内部路径/模块名
            try:
                import traceback as _tb
                self.webui.logger.error(f"[WebUI] unhandled {method} {path}: {e}\n{_tb.format_exc()}")
            except Exception:  # noqa: BLE001
                pass
            try:
                self._send_json({"code": 500, "msg": _t("webui.msg.internal_error")}, 500)
            except Exception:  # noqa: BLE001
                pass
        finally:
            self._drain_leftover_body()

    def _drain_leftover_body(self) -> None:
        """兜底消费未读取的请求体；声明长度超上限直接断连。"""
        if getattr(self, "_body_consumed", True):
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            return
        if length <= 0:
            return
        if length > _MAX_UPLOAD_BYTES + 64 * 1024:
            self.close_connection = True
            return
        self._drain_request_body()
