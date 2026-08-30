"""WebUI HTTP 服务器（纯标准库 ThreadingHTTPServer，无第三方 Web 框架）。"""

from __future__ import annotations

import copy
import gzip
import hashlib
import hmac
import json
import os
import platform
import re
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from ..i18n import (
    AUTO_DETECT,
    DEFAULT_LANGUAGE,
    SUPPORTED_LANGUAGES,
    get_i18n,
    normalize_locale,
    t as _t,
)
from ..subplugin.requires import parse_requires_from_manifest
from . import auth as auth_util
from .configform import ConfigFormBuilder
from .logbuffer import LogBuffer
from .metrics import ServerMetricsCollector

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

# 允许 gzip 传输压缩的文本资源类型
_GZIP_SUFFIXES = frozenset({".html", ".js", ".css", ".json", ".svg", ".txt", ".md"})
# 静态资源 gzip 压缩缓存：path → (mtime_ns, size, compressed)
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
    # 压缩无收益（已压缩内容 / 极小文件）时回退原文直发
    if len(compressed) >= size:
        return None
    with _gzip_lock:
        if len(_GZIP_CACHE) >= _GZIP_CACHE_MAX:
            _GZIP_CACHE.clear()
        _GZIP_CACHE[path] = (mtime_ns, size, compressed)
    return compressed

# 敏感键掩码集合：展示时统一替换为固定 6 个 *（不回显长度），保存时按纯星串自动还原
SENSITIVE_KEYS = {
    "access_token", "password", "secret",
    "app_secret", "client_secret", "api_key", "token", "private_key",
}

# 上传/URL 下载的统一大小上限（16MB），防止全量读入内存导致 DoS
_MAX_UPLOAD_BYTES = 16 * 1024 * 1024

EDITABLE_SUFFIXES = {".json", ".py", ".txt", ".md", ".yml", ".yaml", ".cfg", ".ini", ".html", ".css", ".js"}

# ------------------------------------------------- URL 下载防护（按内网部署需求调整）
# 按项目需求（tests/test_security.py::test_ssrf_defense 规格说明）：面板主要部署于
# 内网，管理员需要从内网私有源（如 127.0.0.1 / 192.168.x.x 的插件仓库）直装子插件，
# 因此**不做内网/保留地址拦截**，仅保留以下防护：
#   1. 协议白名单：仅 http(s)；
#   2. 同主机重定向限制：HTTP 重定向不得跳到其它主机（_SameHostRedirectHandler）；
#   3. 响应大小上限与超时（_MAX_UPLOAD_BYTES / timeout）；
#   4. 可选完整性锚点：请求带 sha256 时强校验；配置了 webui.install_url_allow_hosts
#      白名单后强制主机校验（C5）。


def _http_url_host(url: str) -> str:
    """校验下载 URL 为 http(s) 且含主机名（语法级校验，不做 DNS 解析）。

    返回小写主机名，供重定向目标比对；非法时抛 ValueError（消息仅用于日志）。
    """
    try:
        parsed = urllib.parse.urlparse(str(url or ""))
    except ValueError as exc:
        raise ValueError("下载地址格式非法") from exc
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("下载地址必须是 http(s)")
    host = (parsed.hostname or "").lower()
    if not host:
        raise ValueError("下载地址缺少主机名")
    return host


class _SameHostRedirectHandler(urllib.request.HTTPRedirectHandler):
    """禁止跨主机重定向：重定向目标 host 与原 host 不同则抛异常。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        old_host = (urllib.parse.urlsplit(req.full_url).hostname or "").lower()
        new_host = (urllib.parse.urlsplit(str(newurl)).hostname or "").lower()
        if not new_host or new_host != old_host:
            raise urllib.error.URLError("重定向到其它主机已被拒绝")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _fetch_url_bytes(
    url: str, max_bytes: int, timeout: float = 30.0, headers: dict[str, str] | None = None,
) -> bytes:
    """按 URL 下载防护策略下载（协议白名单 + 禁止跨主机重定向 + 大小上限）。

    任何失败统一抛 RuntimeError("下载失败")，原始异常挂在 __cause__ 供调用方记日志；
    对外不回显 Connection refused / 404 等细节。
    """
    expected_host = _http_url_host(url)
    req_headers = {"User-Agent": "LumenBridge-WebUI"}
    if headers:
        req_headers.update(headers)
    opener = urllib.request.build_opener(_SameHostRedirectHandler)
    try:
        req = urllib.request.Request(url, headers=req_headers)
        with opener.open(req, timeout=timeout) as resp:
            # 纵深防御：即使重定向处理器被绕过，最终 URL 主机也必须一致
            final_host = (urllib.parse.urlsplit(resp.geturl()).hostname or "").lower()
            if final_host != expected_host:
                raise urllib.error.URLError("重定向到其它主机已被拒绝")
            data = resp.read(max_bytes + 1)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError("下载失败") from exc
    if len(data) > max_bytes:
        raise RuntimeError(f"文件超过大小限制（{max_bytes} 字节）")
    return data


class _NullLock:
    """空 contextmanager：当 ConfigManager 未提供 _save_lock 时的占位"""

    def __enter__(self) -> "_NullLock":
        return self

    def __exit__(self, *args: Any) -> None:
        pass

CONFIG_LABEL_KEYS: dict[str, str] = {
    "debug": "debug",
    "language": "language",
    "whitelist": "whitelist.section",
    "whitelist.enable": "whitelist.enable",
    "whitelist.auto_add": "whitelist.auto_add",
    "whitelist.bind_keyword": "whitelist.bind_keyword",
    "whitelist.unbind_keyword": "whitelist.unbind_keyword",
    "whitelist.remove_on_leave": "whitelist.remove_on_leave",
    "regex_engine": "regex_engine.section",
    "regex_engine.enable": "regex_engine.enable",
    "regex_engine.only_on_main": "regex_engine.only_on_main",
    "regex_engine.admin_debug": "regex_engine.admin_debug",
    "regex_engine.command_timeout": "regex_engine.command_timeout",
    "webui": "webui.section",
    "webui.enable": "webui.enable",
    "webui.host": "webui.host",
    "webui.port": "webui.port",
    "webui.password": "webui.password",
    "webui.secret": "webui.secret",
    "background": "background.section",
    "background.enable": "background.enable",
    "background.api_url": "background.api_url",
    "background.blur_strength": "background.blur_strength",
    "background.fallback_to_default": "background.fallback_to_default",
    "background.cache_seconds": "background.cache_seconds",
    "pip": "pip.section",
    "pip.enable": "pip.enable",
    "pip.index_url": "pip.index_url",
    "pip.timeout": "pip.timeout",
    "marketplace": "marketplace.section",
    "marketplace.enable": "marketplace.enable",
    "marketplace.api_url": "marketplace.api_url",
    "marketplace.allow_http": "marketplace.allow_http",
    "marketplace.timeout": "marketplace.timeout",
    "marketplace.max_download_bytes": "marketplace.max_download_bytes",
    "marketplace.check_on_start": "marketplace.check_on_start",
    "marketplace.check_interval_seconds": "marketplace.check_interval_seconds",
    "updates": "updates.section",
    "updates.enable": "updates.enable",
    "updates.api_url": "updates.api_url",
    "updates.timeout": "updates.timeout",
    "updates.auto_update": "updates.auto_update",
    "commands": "commands.section",
    "commands.allow_in_game": "commands.allow_in_game",
    "commands.status.allow_player": "commands.status.allow_player",
    "commands.reload.allow_player": "commands.reload.allow_player",
    "commands.say.allow_player": "commands.say.allow_player",
    "commands.plugins.allow_player": "commands.plugins.allow_player",
    "commands.update.allow_player": "commands.update.allow_player",
    "commands.pip.allow_in_game": "commands.pip.allow_in_game",
    "commands.pip.allow_player": "commands.pip.allow_player",
}


def build_config_labels() -> dict[str, dict[str, str]]:
    """生成配置标签字典；请求时调用以使用当前语言（模块加载时语言未初始化）。"""
    result: dict[str, dict[str, str]] = {}
    for path, label_key in CONFIG_LABEL_KEYS.items():
        if label_key.endswith(".section"):
            section_key = label_key[:-8]
            result[path] = {"_": _t(f"config_labels.{section_key}.section")}
        else:
            label = _t(f"config_labels.{label_key}.label")
            desc = _t(f"config_labels.{label_key}.desc")
            result[path] = {"label": label, "desc": desc}
    return result


class WebUIServer:
    """LumenBridge Web 管理面板服务器"""

    def __init__(self, plugin: "LumenBridgePlugin") -> None:
        self.plugin = plugin
        self.logger = getattr(plugin, "_tee_logger", None) or plugin.logger
        self.log_buffer: LogBuffer = plugin.log_buffer

        webui_conf = plugin.config_manager.data.get("webui", {})
        self.host: str = str(webui_conf.get("host") or "127.0.0.1")
        self.port: int = int(webui_conf.get("port") or 8300)
        # ""/"*" 视为待生成随机密码，避免 hmac.compare_digest(b"", b"") 通过登录。
        # M2：纯空白字符串同样视同未设置（strip 后为空即走随机密码分支）。
        raw_password = str(webui_conf.get("password") or "").strip()
        self.password: str = raw_password if raw_password else "*"
        self.secret: str = str(webui_conf.get("secret") or auth_util.generate_secret())

        if not self.password or self.password == "*":
            self.password = auth_util.generate_password(8)
            # 密码只打印到服务器控制台：print 走进程 stdout，不经过 LoggerTee，
            # 因此不会进入 WebUI 日志缓冲/SSE（避免明文密码被登录用户回看）。
            print(f"[WebUI] {_t('plugin.random_password', password=self.password)}", flush=True)
            self.logger.info("[WebUI] 随机管理员密码已打印至服务器控制台")

        # 鉴权提供者：HMAC token + 版本号（改密/换钥后旧 token 立即失效）
        self.auth_provider = auth_util.AuthProvider(self.secret)

        # 主线程长操作（reload/uninstall 等"调度到主线程并等待"类）互斥锁：
        # 防止超时返回后后台任务仍在执行时又叠加新的主线程操作
        self._main_op_lock = threading.Lock()

        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

        # 子插件扩展注册表（被请求处理线程读、子插件加载线程写，需加锁保护）
        self.custom_apis: dict[tuple[str, str], tuple[Callable, bool]] = {}
        self.custom_pages: list[dict[str, str]] = []
        self.plugins_config_schema: dict[str, dict[str, Any]] = {}
        self._ext_lock = threading.RLock()
        # 兼容旧字段（部分代码引用 _schema_lock），实际指向 _ext_lock
        self._schema_lock = self._ext_lock
        self.start_time = time.time()

        # pip 安装任务表：task_id -> {status, log_lines, packages, start_time, done}
        self._pip_tasks: dict[str, dict[str, Any]] = {}
        self._pip_tasks_lock = threading.Lock()
        # 复用 plugin._pip_serial_lock：WebUI 与 marketplace 共享串行锁，避免并发写 site-packages 损坏元数据。
        self._pip_serial_lock = plugin._pip_serial_lock
        # pip list 结果短缓存（时间戳, 列表）：避免前端并发刷新刷出多个 pip 子进程
        self._pip_list_cache: tuple[float, list[dict[str, Any]]] | None = None
        self._pip_list_lock = threading.Lock()
        self._market_tasks: dict[str, dict[str, Any]] = {}
        self._market_tasks_lock = threading.Lock()

        # QQ 官方机器人扫码绑定任务表：task_id -> {bind_key, created_at}
        self._qr_bind_tasks: dict[str, dict[str, Any]] = {}
        self._qr_bind_lock = threading.Lock()

        # 登录限速（H1）：按客户端 IP 的失败计数/锁定 + 请求频率窗口，线程安全
        self._login_guard_lock = threading.Lock()
        self._login_guard: dict[str, dict[str, Any]] = {}

        # /api/qqofficial/qr/create 限频（低危）：每 IP 30 秒 1 次，内存计数
        self._qr_create_lock = threading.Lock()
        self._qr_create_last: dict[str, float] = {}

        self.metrics_collector = ServerMetricsCollector(self.logger, interval=3.0)

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
        display_host = "127.0.0.1" if self.host == "0.0.0.0" else self.host
        self.logger.info(_t("plugin.webui_started", url=f"http://{display_host}:{self.port}"))

    @staticmethod
    def _install_sigpipe_ignore() -> None:
        """将 SIGPIPE 设为忽略，避免 broken pipe 杀进程。

        仅在仍是 SIG_DFL 时改写，不覆盖宿主刻意设置的处理器。
        """
        try:
            import ctypes
            import ctypes.util

            libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
            SIG_DFL, SIG_IGN, SIGPIPE = 0, 1, 13
            # signal() 返回旧处理器（函数指针）；必须声明 restype 为
            # c_void_p——默认按 c_int 截断 64 位指针后，恢复宿主处理器时
            # 会跳转到坏地址导致进程崩溃
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
            # 非主线程调用会抛 ValueError，C 层已设置，可忽略
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
        # 先通知 SSE 请求线程退出，再停止主 HTTP 循环。
        self._stop_event.set()
        self.metrics_collector.stop()
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

    # --------------------------------------------------------- 登录限速（H1）
    # 每 IP 10 秒内最多 10 次登录请求；连续 5 次失败锁定 60s * 2^(超出次数-1)，上限 900s
    LOGIN_MAX_ATTEMPTS_PER_WINDOW = 10
    LOGIN_WINDOW_SECONDS = 10.0
    LOGIN_FAIL_THRESHOLD = 5
    LOGIN_LOCK_BASE_SECONDS = 60.0
    LOGIN_LOCK_MAX_SECONDS = 900.0
    # 失败计数衰减窗口：距上次失败超过该时长视为不再“连续”，计数清零。
    # 没有衰减时“连续 5 次”会退化成“累计 5 次”——管理员一周内偶尔输错
    # 5 次密码后锁定时长指数增长且永远无法恢复（锁定期间无法登录清零）
    LOGIN_FAIL_DECAY_SECONDS = 300.0

    def _login_check_allowed(self, ip: str) -> tuple[bool, str]:
        """登录前置检查：锁定状态与请求频率窗口（H1）。返回 (是否放行, 提示消息)。"""
        now = time.time()
        with self._login_guard_lock:
            # 顺带清理长期无活动的条目，防止字典无限增长
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
                return False, f"登录失败次数过多，已临时锁定，请约 {wait} 秒后重试"
            if state is None:
                state = {"fails": 0, "locked_until": 0.0, "window_start": now, "attempts": 0, "last_fail": 0.0}
                self._login_guard[ip] = state
            elif now - state["window_start"] >= self.LOGIN_WINDOW_SECONDS:
                state["window_start"] = now
                state["attempts"] = 0
            # 失败计数衰减：实现注释声明的“连续失败”语义
            if state["fails"] and now - state.get("last_fail", 0.0) >= self.LOGIN_FAIL_DECAY_SECONDS:
                state["fails"] = 0
            if state["attempts"] >= self.LOGIN_MAX_ATTEMPTS_PER_WINDOW:
                return False, "登录请求过于频繁，请稍后再试"
            state["attempts"] += 1
            return True, ""

    def _login_record_result(self, ip: str, success: bool) -> None:
        """登录结果回写（H1）：失败累计并按指数退避锁定，成功清零计数。"""
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

    def refresh_config(self) -> None:
        """按最新配置刷新运行参数（供 /lumen reload 调用，不整体重建实例）。

        - password / secret 变化：更新属性并使既有登录 token 失效，
          HTTP 服务不中断，浏览器端重新登录即可
        - host / port 变化：重启 HTTP 监听 socket；扩展注册表（custom_apis、
          custom_pages、plugins_config_schema）与运行中任务状态全部保留
        - 配置留空的项保持现值：避免 reload 把已生成的随机密码/密钥悄悄换掉
        """
        conf: dict[str, Any] = {}
        if self.plugin.config_manager:
            raw = self.plugin.config_manager.data.get("webui", {})
            if isinstance(raw, dict):
                conf = raw

        # M2：空白密码视同未设置，保持现值（不被 reload 悄悄换掉）
        new_password = str(conf.get("password") or "").strip()
        if new_password and new_password != "*" and new_password != self.password:
            self.password = new_password
            self.auth_provider.invalidate_tokens()
            self.logger.info("[WebUI] 管理员密码已按新配置更新")

        new_secret = str(conf.get("secret") or "")
        if new_secret and new_secret != self.auth_provider.secret:
            self.auth_provider.set_secret(new_secret)
            # 同步实例属性：否则后续 /api/config 保存路径用旧 self.secret
            # 做对比，会把已生效的密钥误判为"又变了"并重复失效 token
            self.secret = new_secret
            self.logger.info("[WebUI] 签名密钥已按新配置更新，既有登录状态已失效")

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
            # 依赖安装默认不自动重载子插件（用户显式确认第二步）；reload_after_install 仅内部联动。
            "reload_required": bool(subplugin_name and not reload_after_install),
            "reload_after_install": bool(reload_after_install),
            "action": action,
            "start_time": now,
        }
        with self._pip_tasks_lock:
            if len(self._pip_tasks) >= 20:
                now = time.time()
                # 低危：仅淘汰 5 分钟前已完成的任务，保留近期结果供前端轮询
                done_tasks = [
                    (tid, t) for tid, t in self._pip_tasks.items()
                    if t.get("done") and now - float(t.get("start_time", 0) or 0) > 300
                ]
                done_tasks.sort(key=lambda x: x[1].get("start_time", 0))
                need_remove = len(self._pip_tasks) - 20 + 1
                for tid, _ in done_tasks[:need_remove]:
                    self._pip_tasks.pop(tid, None)
                # 全部 running 时不再强制淘汰 running 任务（会破坏其状态更新），
                # 直接拒绝新任务，由调用方返回"任务数过多"
                if len(self._pip_tasks) >= 20:
                    return None
            self._pip_tasks[task_id] = task

        plugin = self.plugin

        def on_log(line: str) -> None:
            with self._pip_tasks_lock:
                t = self._pip_tasks.get(task_id)
                if t:
                    # 限制日志条数，避免内存膨胀
                    if len(t["log_lines"]) < 500:
                        t["log_lines"].append(line)

        def run_install() -> None:
            # 任务须等"依赖安装 +（如需要）主线程重载"均完成才结束。
            install_success = False
            install_msg = ""
            reload_success: bool | None = None
            reload_error = ""
            if action == "uninstall":
                # 卸载与安装共用任务框架：请求线程只拿 task_id 轮询，
                # 不再无限期等待串行锁 + 同步跑子进程（原实现会把
                # HTTP 请求线程挂起数分钟）
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
                            "msg": install_msg or ("卸载完成" if install_success else "卸载失败"),
                            "status": "success" if install_success else "failed",
                        })
                # 环境已变更：失效 pip list 缓存
                with self._pip_list_lock:
                    self._pip_list_cache = None
                return
            try:
                # pip/uv 非线程安全：串行化所有安装操作，避免并发写 site-packages 损坏元数据
                with self._pip_serial_lock:
                    install_success, install_msg = mgr.install(packages, on_log=on_log)
            except Exception as exc:  # noqa: BLE001
                install_msg = str(exc)

            if install_success:
                # 失效 import 缓存，使后续 reload 能发现新增的 dist-info/顶层模块。
                try:
                    import importlib
                    importlib.invalidate_caches()
                except Exception:
                    pass

            if install_success and subplugin_name and reload_after_install:
                if not plugin.subplugin_manager:
                    reload_success = False
                    reload_error = "子插件管理器不可用"
                else:
                    reload_done = threading.Event()
                    reload_ok = [False]
                    reload_exc = [""]

                    def do_reload() -> None:
                        try:
                            # loader._load_one 会 invalidate_caches，确保刚装依赖能立刻被识别。
                            reload_ok[0] = bool(plugin.subplugin_manager.reload_one(subplugin_name))
                            if not reload_ok[0]:
                                with plugin.subplugin_manager._lock:
                                    current = plugin.subplugin_manager.subplugins.get(subplugin_name)
                                    reload_exc[0] = (current.error if current else "") or "插件加载返回失败"
                        except Exception as exc:  # noqa: BLE001
                            reload_exc[0] = str(exc)
                        finally:
                            reload_done.set()

                    try:
                        plugin.run_on_main(do_reload, delay=1)
                        if not reload_done.wait(timeout=30):
                            reload_error = "等待服务器主线程重载超时（30 秒）"
                            reload_success = False
                        else:
                            reload_success = reload_ok[0]
                            reload_error = reload_exc[0]
                    except Exception as exc:  # noqa: BLE001
                        reload_success = False
                        reload_error = str(exc)

                    on_log(
                        _t("pip.auto_reload_success", name=subplugin_name)
                        if reload_success else _t("pip.auto_reload_failed", name=subplugin_name, error=reload_error or "load failed")
                    )

            # 拆分安装/重载状态，避免依赖写入成功但加载失败时误导为"安装成功"。
            overall_success = bool(install_success and reload_success is not False)
            if not install_success:
                final_message = install_msg or "依赖安装失败"
            elif reload_success is False:
                final_message = f"依赖已安装，但子插件 {subplugin_name} 重载失败：{reload_error or '请查看子插件错误详情'}"
            elif subplugin_name and not reload_after_install:
                final_message = f"{install_msg or '依赖已安装'}；请确认后重载子插件 {subplugin_name}"
            else:
                final_message = install_msg or "依赖安装完成"
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
            # 环境已变更：失效 pip list 缓存
            with self._pip_list_lock:
                self._pip_list_cache = None

        threading.Thread(target=run_install, name=f"lumen-pip-{task_id}", daemon=True).start()
        return task_id

    def _start_market_task(self, action: str, runner: Callable[[Callable[[str], None], Callable[[int, str], None]], dict[str, Any]]) -> str | None:
        """启动市场异步任务；市场网络和 pip 安装不可阻塞 WebUI 请求线程。

        runner 接收两个回调：
        - ``log(line)``：追加一行进度日志到任务面板（前端实时轮询展示）
        - ``progress(percent, label)``：更新进度条（0-100）与标签
        """
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
                # runner 可能返回非 dict（如 list/str/None），安全提取 message
                msg = result.get("message", "") if isinstance(result, dict) else str(result) if result else ""
                with self._market_tasks_lock:
                    current = self._market_tasks.get(task_id)
                    if current:
                        current.update({"status": "success", "done": True, "success": True, "result": result, "msg": msg, "progress": 100})
            except Exception as exc:  # noqa: BLE001
                with self._market_tasks_lock:
                    current = self._market_tasks.get(task_id)
                    if current:
                        current.update({"status": "failed", "done": True, "success": False, "msg": str(exc)})

        threading.Thread(target=execute, name=f"lumen-market-{task_id}", daemon=True).start()
        return task_id

    @property
    def url(self) -> str:
        display_host = "127.0.0.1" if self.host == "0.0.0.0" else self.host
        return f"http://{display_host}:{self.port}"

    def register_api(
        self, method: str, path: str, handler: Callable[[dict[str, Any]], Any],
        need_auth: bool = True,
    ) -> None:
        """注册子插件 API 到 /api/plugin/<path>；handler 收到 {"query","body","path"}，返回值序列化为 JSON。"""
        full = "/api/plugin" + (path if path.startswith("/") else "/" + path)
        with self._ext_lock:
            self.custom_apis[(method.upper(), full)] = (handler, need_auth)
        self.logger.info(_t("plugin.webui_api_registered", method=method.upper(), path=full))

    def register_config(self, builder: ConfigFormBuilder) -> None:
        """注册配置表单 Schema（由 ConfigFormBuilder.register() 调用）"""
        with self._ext_lock:
            self.plugins_config_schema[builder.name] = builder.to_schema()
        self.logger.info(_t("plugin.webui_config_registered", name=builder.name))

    def create_config(self, name: str) -> ConfigFormBuilder:
        """Fluent 配置表单构建器入口"""
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
        """挂载子插件自定义页面（静态资源经 /plugin-views/ 服务）。

        tab=True 时页面在移动端注册为底栏 tab，否则进「其它」面板；
        桌面端侧栏两种都展示。icon 为内置图标名（渲染 SVG）或短文本字符。
        """
        url = f"/plugin-views/{folder}/{relative_path}"
        with self._ext_lock:
            if not any(p["url"] == url for p in self.custom_pages):
                # 用 uuid 防同毫秒多页面 ID 冲突
                import uuid as _uuid
                self.custom_pages.append({
                    "id": f"{plugin_name}_{_uuid.uuid4().hex[:8]}",
                    "pluginName": plugin_name,
                    "title": title,
                    "url": url,
                    "tab": bool(tab),
                    "icon": str(icon or "")[:16],
                })
                self.logger.info(_t("plugin.webui_page_registered", plugin=plugin_name, title=title))


class _RequestHandler(BaseHTTPRequestHandler):
    """HTTP 请求处理器（路由分发）"""

    webui: WebUIServer  # 由子类注入
    protocol_version = "HTTP/1.1"

    def version_string(self) -> str:  # type: ignore[override]
        """低危：Server 头固定为 LumenBridge，不暴露 Python/BaseHTTP 版本。"""
        return "LumenBridge"

    # 屏蔽默认 stderr 访问日志
    def log_message(self, fmt: str, *args: Any) -> None:
        pass

    # 客户端中途断开触发的 socket 异常必须吞掉：避免 SIGPIPE 杀 BDS 进程，且写操作经 _safe_write 兜底，绝不影响游戏主进程。
    _NETWORK_ERRORS = (
        ConnectionResetError,
        ConnectionAbortedError,
        BrokenPipeError,
        TimeoutError,
    )
    # 同样需要吞掉的 OSError errno 集合（部分 Python 版本不映射到上面子类）
    _SWALLOW_ERRNOS = {32, 103, 104, 110, 113}

    @classmethod
    def _is_network_error(cls, exc: BaseException) -> bool:
        if isinstance(exc, cls._NETWORK_ERRORS):
            return True
        if isinstance(exc, OSError):
            return getattr(exc, "errno", None) in cls._SWALLOW_ERRNOS
        return False

    def _safe_write(self, data: bytes) -> bool:
        """安全写响应体。客户端已断开时返回 False，绝不抛异常。"""
        try:
            self.wfile.write(data)
            return True
        except BaseException as exc:  # noqa: BLE001
            if self._is_network_error(exc):
                self.close_connection = True
                return False
            raise

    def _safe_send_headers(self, status: int, headers: list[tuple[str, str]]) -> bool:
        """安全发送状态行+响应头。客户端已断开时返回 False。"""
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

    def handle_one_request(self) -> None:  # type: ignore[override]
        try:
            super().handle_one_request()
        except BaseException as exc:  # noqa: BLE001
            if self._is_network_error(exc):
                self.close_connection = True
            else:
                raise

    def handle(self) -> None:  # type: ignore[override]
        try:
            super().handle()
        except BaseException as exc:  # noqa: BLE001
            if self._is_network_error(exc):
                self.close_connection = True
            else:
                raise

    def setup(self) -> None:  # type: ignore[override]
        """把 wfile 包装为带 MSG_NOSIGNAL 的写，是抵御 SIGPIPE 杀进程的根本手段（即便宿主重置全局 SIGPIPE）。"""
        super().setup()
        # 设置 socket 超时，防止 Slowloris 慢速攻击耗尽线程池
        try:
            self.connection.settimeout(30)
        except OSError:
            pass
        try:
            self.wfile = _NosigWFile(self.wfile, self.connection)  # type: ignore[assignment]
        except Exception:  # noqa: BLE001
            pass  # 包装失败时退回原始 wfile，由外层 try/except 兜底

    @property
    def plugin(self) -> Any:
        return self.webui.plugin

    def _parse(self) -> tuple[str, dict[str, str]]:
        parsed = urllib.parse.urlparse(self.path)
        query = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
        return parsed.path, query

    def _read_body_with_deadline(self, length: int) -> bytes | None:
        """按总时限分块读取请求体（Slowloris dribble 防护）。

        socket 超时只约束单次 recv：攻击者以每 29 秒 1 字节的速度 dribble
        可无限期占用连接线程。read1 每次至多一次底层 recv，配合总时限
        （按体积给足慢速上传余量，下限 30s）把线程占用时间从无限收敛到
        有界。超时/中断即断连，防残留字节污染下一请求。
        """
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
            if not chunk:  # EOF：对端提前断开
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def _read_body(self) -> Any:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            # 畸形 Content-Length（如 "abc"）不应导致 500
            self.close_connection = True
            return None
        if length <= 0:
            return None
        # 限制请求体大小，防止恶意大 body 导致内存耗尽 DoS
        if length > 16 * 1024 * 1024:  # 16MB
            # 必须关闭连接：否则未读取的 body 会留在 socket 缓冲区，
            # 在 HTTP/1.1 keep-alive 下被当作下一次请求行解析，污染后续请求
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
        """尽量消费请求体（按 Content-Length 分块读取，M3 请求走私防护）。

        读取上限受 limit 约束：过大的声明长度不做全量消费（本身即攻击面），
        由调用方配合 close_connection 关闭连接兜底。
        """
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
        """解析 multipart/form-data 中的第一个文件字段，返回 (文件名, 内容)"""
        ctype = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in ctype:
            # M3：前置失败必须关闭连接并尽量消费请求体，防止残留字节
            # 在 keep-alive 下被当作下一个请求解析（请求走私）
            self.close_connection = True
            self._drain_request_body()
            return None
        m = re.search(r"boundary=([^;]+)", ctype)
        if not m:
            self.close_connection = True
            self._drain_request_body()
            return None
        # boundary 值先去空白再去引号，兼容 boundary="..." 与边界空白写法
        boundary = m.group(1).strip().strip('"').encode()
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            # 畸形 Content-Length（如 "abc"）不应导致 500；长度未知无法消费，直接断连
            self.close_connection = True
            return None
        # 上传上限 16MB：body 全量读入内存，过大的上限会被用于内存耗尽 DoS
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
            content = part[header_end + 4:]
            return filename, content
        return None

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
        # 低危：大文件防护——超过 8MB 直接 413
        try:
            st = file_path.stat()
        except OSError:
            self._send_json({"code": 404, "msg": _t("webui.msg.not_found")}, 404)
            return
        size = st.st_size
        if size > 8 * 1024 * 1024:
            self._send_json({"code": 413, "msg": "文件过大"}, 413)
            return
        suffix = file_path.suffix.lower()
        mime = MIME_TYPES.get(suffix, "application/octet-stream")
        # 强 ETag（大小 + mtime）：js/css 走 no-cache 时重验证命中即回 304，
        # 升级 UI 后文件 mtime 变化自动失效
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
        ]
        headers.append(("Cache-Control", cache_hdr))

        # 文本资源（html/js/css/svg 等）按 Accept-Encoding 协商 gzip：
        # 压缩结果按 (path, mtime, size) 缓存，热路径零重复压缩开销
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
        # 二进制资源分块发送（64KB），避免一次性把整个文件读入内存
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
        if suffix == ".html":
            # HTML 不长缓存，保证插件升级后页面骨架及时更新。
            # 面板主要部署于内网（127.0.0.1），CSP / X-Frame-Options
            # 头会静默拦截跨域背景图、内联脚本与 iframe 自定义页。
            return "no-cache"
        if suffix in (".png", ".ico", ".svg"):
            return "public, max-age=86400"
        if suffix in (".js", ".css"):
            return "no-cache"
        return "no-cache"

    def _check_auth(self, query: dict[str, str]) -> bool:
        header = self.headers.get("Authorization", "")
        token = header[7:] if header.startswith("Bearer ") else query.get("token", "")
        return self.webui.auth_provider.verify_token(token)

    def _unauthorized(self) -> None:
        self._send_json({"code": 401, "msg": _t("webui.msg.unauthorized")}, 401)

    def do_OPTIONS(self) -> None:
        # 管理面板只支持同源访问，不为任意网站提供跨域预检授权：
        # 405 + 不回 Access-Control-Allow-* 头，预检必然失败。
        # 带 body 的 OPTIONS 同样要消费/断连（M3 请求走私防护）：
        # _route 的 finally 兜底不覆盖本方法，残留字节会在 keep-alive 下
        # 被当作下一请求行解析
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

    def do_GET(self) -> None:
        self._route("GET")

    def do_POST(self) -> None:
        self._route("POST")

    def do_PUT(self) -> None:
        self._route("PUT")

    def do_DELETE(self) -> None:
        self._route("DELETE")

    def _route(self, method: str) -> None:
        path, query = self._parse()
        # keep-alive 连接同一 handler 实例会串行处理多个请求，逐请求重置消费标志
        self._body_consumed = False
        try:
            # 公共 API：固定白名单 + 全部 /api/i18n/ 语言包路由
            if method == "GET" and (
                path in ("/api/i18n/languages", "/api/i18n/current", "/api/public/background")
                or path.startswith("/api/i18n/")
            ):
                return self._route_public_api(method, path, query)

            with self.webui._ext_lock:
                hit = self.webui.custom_apis.get((method, path))
            if hit:
                handler, need_auth = hit
                if need_auth and not self._check_auth(query):
                    return self._unauthorized()
                # 支持 multipart 文件上传：若 Content-Type 为 multipart/form-data，
                # 解析第一个文件字段并传入 handler 的 "file" 键（(filename, bytes) 或 None）
                ctype = self.headers.get("Content-Type", "")
                file_info = None
                if "multipart/form-data" in ctype:
                    file_info = self._read_multipart_file()
                    body = {}
                else:
                    body = self._read_body() if method in ("POST", "PUT", "DELETE") else None
                try:
                    result = handler({"query": query, "body": body, "path": path, "file": file_info})
                except Exception as exc:  # noqa: BLE001
                    self.webui.logger.warning(f"[WebUI] 子插件 API 异常 path={path} error={exc!r}")
                    return self._send_json({"code": 500, "msg": "API 内部错误"}, 500)
                return self._send_json({"code": 200, "data": result})

            if path.startswith("/api/"):
                return self._route_api(method, path, query)

            if path.startswith("/plugin-views/"):
                if not self._check_auth(query):
                    return self._unauthorized()
                # 浏览器会把文件名里的空格/非 ASCII 字符百分号编码，
                # 不解码则磁盘上永远匹配不到对应文件
                rel = urllib.parse.unquote(path[len("/plugin-views/"):])
                base = (Path(self.plugin.data_folder) / "plugins").resolve()
                target = (base / rel).resolve()
                # 防目录穿越：用 relative_to 做路径组件匹配（startswith 不安全）
                try:
                    target.relative_to(base)
                except ValueError:
                    return self._send_json({"code": 403, "msg": _t("webui.msg.forbidden_path")}, 403)
                return self._send_file(target)

            # 静态资源路径同样先做百分号解码（自定义页面文件名可含空格/中文）
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
        except BrokenPipeError:
            pass
        except ConnectionResetError:
            pass
        except Exception as e:
            # 记录完整 traceback 到日志；响应只返回通用错误，不泄漏内部路径/模块名。
            try:
                import traceback as _tb
                self.webui.logger.error(
                    f"[WebUI] 未处理异常 {method} {path}: {e}\n{_tb.format_exc()}"
                )
            except Exception:
                pass
            try:
                self._send_json({"code": 500, "msg": _t("webui.msg.internal_error")}, 500)
            except Exception:
                pass
        finally:
            self._drain_leftover_body()

    def _drain_leftover_body(self) -> None:
        """兜底消费未被路由读取的请求体（M3 请求走私防护）。

        keep-alive 下残留 body 会被当作下一请求行解析导致 400 并断连；
        声明长度超出消费上限时直接关闭连接兜底。
        """
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

    def _acquire_main_op(self, done: threading.Event) -> tuple[Callable[[], None], Callable[[], None]] | tuple[None, None]:
        """主线程长操作互斥入口。返回 (finish, defer_release)；acquire 失败返回 (None, None)。

        - finish()：调用方完成全部后续处理（如依赖卸载）后调用，幂等释放互斥锁
        - defer_release()：等待超时放弃响应时调用——起守护线程等任务完成后兜底释放，
          防止超时返回后后台任务仍在执行时又叠加新的主线程操作
        """
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

    def _route_public_api(self, method: str, path: str, query: dict[str, str]) -> None:
        """公共 API（无需鉴权）：i18n 语言包与背景图配置，供登录页加载。"""
        if method == "GET" and path == "/api/i18n/languages":
            i18n = get_i18n()
            return self._send_json({"code": 200, "data": {
                "languages": i18n.available_languages(),
                "default": DEFAULT_LANGUAGE,
                "auto": AUTO_DETECT,
            }})

        if method == "GET" and path == "/api/i18n/current":
            plugin = self.plugin
            configured = "auto"
            try:
                configured = plugin.config_manager.language
            except Exception:
                pass
            return self._send_json({"code": 200, "data": {
                "language": plugin.language if hasattr(plugin, "language") else DEFAULT_LANGUAGE,
                "configured": configured,
            }})

        m = re.fullmatch(r"/api/i18n/([\w-]+)", path)
        if m and method == "GET":
            lang_raw = m.group(1)
            lang = normalize_locale(lang_raw)
            if lang not in SUPPORTED_LANGUAGES:
                return self._send_json({"code": 404, "msg": _t("webui.msg.lang_not_found")}, 404)
            i18n = get_i18n()
            return self._send_json({"code": 200, "data": i18n.export(lang)})

        if method == "GET" and path == "/api/public/background":
            bg = {}
            try:
                bg = self.plugin.config_manager.background or {}
            except Exception:
                bg = {}
            return self._send_json({"code": 200, "data": bg})

        return self._send_json({"code": 404, "msg": _t("webui.msg.api_not_found")}, 404)

    def _compute_uninstall_deps(self, plugin: "LumenBridgePlugin", name: str) -> tuple[list[str], list[str]]:
        """计算卸载子插件时可顺带移除的 pip 依赖。

        返回 (可移除, 需保留)：
        - 可移除：已安装、且不被其它已装子插件声明同名的依赖；
        - 需保留：仍被其它子插件使用（或未安装，无需卸载的直接忽略）。
        """
        mgr = plugin.subplugin_manager
        with mgr._lock:
            sp = mgr.subplugins.get(name)
            deps_raw = sp.manifest.get("dependencies") if sp is not None else None
            others = [s for n, s in mgr.subplugins.items() if n != name]
        if not isinstance(deps_raw, list):
            return [], []
        deps = [str(d) for d in deps_raw if d]
        if not deps:
            return [], []
        pip_mgr = self.webui._get_pip_manager_for_plugin(plugin)
        if pip_mgr is None:
            return [], []
        from ..pip_manager import _normalize

        def _norm(spec: str) -> str:
            return _normalize(pip_mgr._extract_package_name(spec))

        try:
            missing = set(pip_mgr.missing_dependencies(deps))
        except Exception:  # noqa: BLE001 - 检测失败时不建议自动卸载任何依赖
            return [], deps
        others_need: set[str] = set()
        for other in others:
            od = other.manifest.get("dependencies")
            if isinstance(od, list):
                for d in od:
                    if d:
                        others_need.add(_norm(str(d)))
        removable = [d for d in deps if d not in missing and _norm(d) not in others_need]
        kept = [d for d in deps if d not in missing and _norm(d) in others_need]
        return removable, kept

    def _route_api(self, method: str, path: str, query: dict[str, str]) -> None:
        if method == "POST" and path == "/api/auth/login":
            body = self._read_body() or {}
            # JSON 合法但非对象（如数组/字符串）时 body.get 会抛 AttributeError
            if not isinstance(body, dict):
                return self._send_json({"code": 400, "msg": _t("webui.msg.invalid_body")}, 400)
            # 限速 + 恒定时间密码比较
            ip = str(self.client_address[0]) if self.client_address else ""
            ok, gmsg = self.webui._login_check_allowed(ip)
            if not ok:
                return self._send_json({"code": 429, "msg": gmsg}, 429)
            if hmac.compare_digest(str(body.get("password", "")).encode("utf-8"),
                                   str(self.webui.password).encode("utf-8")):
                self.webui._login_record_result(ip, True)
                token = self.webui.auth_provider.issue_token()
                return self._send_json({"code": 200, "data": {"token": token}})
            self.webui._login_record_result(ip, False)
            return self._send_json({"code": 401, "msg": _t("webui.msg.password_error")}, 401)

        if not self._check_auth(query):
            return self._unauthorized()

        plugin = self.plugin
        cm = plugin.config_manager

        if method == "GET" and path == "/api/overview":
            adapter = plugin.adapter  # AdapterHub 门面
            primary = adapter.primary() if hasattr(adapter, "primary") else adapter
            # 在线玩家必须从游戏主线程读取，否则非主线程访问 BDS 数据会导致崩溃。
            players_box: list[list[str]] = [[]]
            done = threading.Event()

            def fetch_players() -> None:
                try:
                    players_box[0] = [p.name for p in plugin.server.online_players]
                except Exception:
                    players_box[0] = []
                finally:
                    done.set()

            try:
                plugin.run_on_main(fetch_players)
            except Exception:
                players_box[0] = []
                # 调度失败时任务不会执行，必须手动置位，否则必然等满 2 秒
                done.set()
            done.wait(timeout=2.0)
            players = players_box[0]
            wl_count = 0
            if getattr(plugin, "whitelist_module", None):
                wl_count = len(plugin.whitelist_module.snapshot())
            rules_count = 0
            if getattr(plugin, "regex_module", None):
                rules_count = len(getattr(plugin.regex_module, "rules", []))
            sub_mgr = getattr(plugin, "subplugin_manager", None)
            # 多账号机器人资料：每个启用的适配器一条；bot_profile 为首个（兼容）
            profiles: list[dict[str, Any]] = []
            if hasattr(plugin, "bot_profiles_snapshot"):
                profiles = plugin.bot_profiles_snapshot()
            elif hasattr(plugin, "bot_profile_snapshot"):
                single = plugin.bot_profile_snapshot()
                profiles = [single] if single else []
            profile = profiles[0] if profiles else {}
            configured_qq = int(cm.connection.get("bot_qq", 0) or 0)
            bot_qq = int(profile.get("qq") or configured_qq)
            # QQ 官方适配器 __getattr__ 对未知属性返回 _unsupported 桩函数，
            # ws_type 必须校验为 int，否则 function 无法 JSON 序列化（mode 供前端可靠分支）。
            raw_mode = getattr(primary, "ws_type", None) if primary else None
            mode_value = raw_mode if isinstance(raw_mode, int) and not isinstance(raw_mode, bool) else -1
            return self._send_json({"code": 200, "data": {
                "version": plugin.VERSION,
                "connected": bool(adapter and adapter.is_connected),
                "mode": mode_value,
                "mode_name": adapter.mode_name if adapter else _t("webui.msg.not_started"),
                "adapters": adapter.status() if hasattr(adapter, "status") else [],
                "main_group": cm.main_group,
                "main_groups": cm.main_groups,
                "bot_qq": bot_qq,
                "bot_profile": profile,
                "bot_profiles": profiles,
                "online_players": players,
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

        if method == "GET" and path == "/api/server/metrics":
            return self._send_json({
                "code": 200,
                "data": self.webui.metrics_collector.snapshot(),
            })

        if path == "/api/config":
            if method == "GET":
                # M5：序列化前先取快照（优先 ConfigManager.snapshot()），
                # 避免与保存线程并发修改 dict 导致 json.dumps 中途迭代异常
                snapshot = cm.snapshot() if hasattr(cm, "snapshot") else copy.deepcopy(cm.data)
                data = json.loads(json.dumps(snapshot))

                def mask(obj: Any) -> None:
                    if isinstance(obj, dict):
                        for k, v in obj.items():
                            if k in SENSITIVE_KEYS and v:
                                # 低危：固定 6 个 * 掩码，不再按值长度回显（防长度侧信道）
                                obj[k] = "******"
                            else:
                                mask(v)
                    elif isinstance(obj, list):
                        for item in obj:
                            mask(item)

                mask(data)
                return self._send_json({"code": 200, "data": data})
            if method == "POST":
                body = self._read_body()
                if not isinstance(body, dict):
                    return self._send_json({"code": 400, "msg": _t("webui.msg.invalid_config")}, 400)

                def unmask(new: Any, old: Any) -> None:
                    if isinstance(new, dict) and isinstance(old, dict):
                        for k, v in new.items():
                            if k in SENSITIVE_KEYS and isinstance(v, str) and v and set(v) == {"*"}:
                                new[k] = old.get(k, "")
                            else:
                                unmask(v, old.get(k))
                    elif isinstance(new, list) and isinstance(old, list):
                        for i in range(min(len(new), len(old))):
                            unmask(new[i], old[i])

                unmask(body, cm.data)

                # 由 ConfigManager 统一校验并原子合并/保存，避免未知键/错误类型/危险范围值落盘后在 reload 时失败。
                try:
                    cm.apply_patch(body)
                except Exception as exc:
                    return self._send_json({
                        "code": 400,
                        "msg": _t("webui.msg.invalid_config") + f": {exc}",
                    }, 400)
                # 修改 WebUI 密码/密钥后立即生效并使旧 token 全部失效（token 版本 +1）
                try:
                    new_webui_conf = cm.data.get("webui", {}) or {}
                    # M2：空白密码/密钥视同未设置，不生效（保持现值）
                    new_password = str(new_webui_conf.get("password") or "").strip()
                    new_secret = str(new_webui_conf.get("secret") or "").strip()
                    if new_password and new_password != self.webui.password and set(new_password) != {"*"}:
                        self.webui.password = new_password
                        self.webui.auth_provider.invalidate_tokens()
                    if new_secret and new_secret != self.webui.secret and set(new_secret) != {"*"}:
                        self.webui.secret = new_secret
                        self.webui.auth_provider.set_secret(new_secret)
                except Exception:
                    pass
                plugin.bus.emit("config.update.core", body)
                return self._send_json({
                    "code": 200,
                    "msg": _t("webui.msg.config_saved"),
                })

        # ------------------------------------------------ 连接配置
        if path == "/api/connections":
            connections = getattr(plugin, "connections", None)
            if connections is None:
                return self._send_json({"code": 500, "msg": _t("connections.unavailable")}, 500)
            if method == "GET":
                data = connections.snapshot(mask=True)
                hub = getattr(plugin, "hub", None)
                status = hub.status() if hub is not None else []
                return self._send_json({"code": 200, "data": {
                    "adapters": data,
                    "status": status,
                }})
            if method == "POST":
                body = self._read_body()
                if not isinstance(body, dict):
                    return self._send_json({"code": 400, "msg": _t("webui.msg.invalid_config")}, 400)
                try:
                    created = connections.create(body)
                except Exception as exc:
                    return self._send_json({"code": 400, "msg": str(exc)}, 400)
                try:
                    plugin.reload_onebot_connection()
                except Exception as exc:
                    return self._send_json({"code": 200, "data": created, "msg": _t("connections.saved_reload_failed", error=exc)})
                return self._send_json({"code": 200, "data": created, "msg": _t("connections.saved")})

        m_conn = re.fullmatch(r"/api/connections/([A-Za-z0-9_\-]+)", path)
        # reload / reveal 是保留子路径（POST 字面量路由），不能落入 <id> 正则
        if m_conn and m_conn.group(1) in ("reload", "reveal"):
            m_conn = None
        if m_conn:
            connections = getattr(plugin, "connections", None)
            if connections is None:
                return self._send_json({"code": 500, "msg": _t("connections.unavailable")}, 500)
            adapter_id = m_conn.group(1)
            if method == "PUT":
                body = self._read_body()
                if not isinstance(body, dict):
                    return self._send_json({"code": 400, "msg": _t("webui.msg.invalid_config")}, 400)
                try:
                    updated = connections.update(adapter_id, body)
                except Exception as exc:
                    return self._send_json({"code": 400, "msg": str(exc)}, 400)
                try:
                    plugin.reload_onebot_connection()
                except Exception as exc:
                    return self._send_json({"code": 200, "data": updated, "msg": _t("connections.saved_reload_failed", error=exc)})
                return self._send_json({"code": 200, "data": updated, "msg": _t("connections.saved")})
            if method == "DELETE":
                try:
                    removed = connections.delete(adapter_id)
                except Exception as exc:
                    return self._send_json({"code": 400, "msg": str(exc)}, 400)
                if not removed:
                    return self._send_json({"code": 404, "msg": _t("connections.not_found", id=adapter_id)}, 404)
                try:
                    plugin.reload_onebot_connection()
                except Exception as exc:
                    return self._send_json({"code": 200, "msg": _t("connections.saved_reload_failed", error=exc)})
                return self._send_json({"code": 200, "msg": _t("connections.deleted")})

        if method == "POST" and path == "/api/connections/reload":
            try:
                plugin.reload_onebot_connection()
            except Exception as exc:
                return self._send_json({"code": 500, "msg": str(exc)}, 500)
            return self._send_json({"code": 200, "msg": _t("connections.reloaded")})

        # 密钥二次查看：返回指定适配器指定密钥字段的明文（需已登录 WebUI）
        if method == "POST" and path == "/api/connections/reveal":
            connections = getattr(plugin, "connections", None)
            if connections is None:
                return self._send_json({"code": 500, "msg": _t("connections.unavailable")}, 500)
            body = self._read_body()
            adapter_id = str(body.get("id", "") or "") if isinstance(body, dict) else ""
            key = str(body.get("key", "") or "") if isinstance(body, dict) else ""
            if key not in ("access_token", "app_secret"):
                return self._send_json({"code": 400, "msg": _t("connections.reveal_invalid_key")}, 400)
            adapter = connections.get(adapter_id)
            if adapter is None:
                return self._send_json({"code": 404, "msg": _t("connections.not_found", id=adapter_id)}, 404)
            # M6：敏感值明文查看属高危操作，返回前记录审计日志（操作 / 来源 IP / 目标适配器）
            reveal_ip = str(self.client_address[0]) if self.client_address else "unknown"
            reveal_at = int(time.time())
            self.webui.logger.warning(
                f"[WebUI] 审计：查看适配器敏感字段 ip={reveal_ip} adapter={adapter_id} key={key}"
            )
            return self._send_json({"code": 200, "data": {"value": str(adapter.get(key, "") or ""), "reveal_at": reveal_at}})

        # ---------------- QQ 官方机器人扫码登录（q.qq.com lite 绑定接口）
        if method == "POST" and path == "/api/qqofficial/qr/create":
            # 低危：扫码创建接口限频（每 IP 30 秒 1 次），防止滥用拉起大量绑定任务
            qr_ip = str(self.client_address[0]) if self.client_address else "unknown"
            with self.webui._qr_create_lock:
                qr_now = time.time()
                if qr_now - self.webui._qr_create_last.get(qr_ip, 0.0) < 30:
                    return self._send_json({"code": 429, "msg": "请求过于频繁，请 30 秒后再试"}, 429)
                # 顺带清理过期项，防止字典无限增长
                if len(self.webui._qr_create_last) > 1024:
                    self.webui._qr_create_last = {
                        k: v for k, v in self.webui._qr_create_last.items() if qr_now - v < 30
                    }
                self.webui._qr_create_last[qr_ip] = qr_now
            from ..onebot import qqofficial_bind

            try:
                task = qqofficial_bind.create_bind_task_sync()
            except Exception as exc:
                return self._send_json({"code": 502, "msg": _t("connections.qr_create_failed", error=exc)}, 502)
            with self.webui._qr_bind_lock:
                # 清理 10 分钟前的过期任务，避免表无限增长
                now = time.time()
                for stale in [
                    tid for tid, info in self.webui._qr_bind_tasks.items()
                    if now - float(info.get("created_at", 0)) > 600
                ]:
                    self.webui._qr_bind_tasks.pop(stale, None)
                self.webui._qr_bind_tasks[task["task_id"]] = {
                    "bind_key": task["bind_key"],
                    "created_at": now,
                }
            return self._send_json({"code": 200, "data": {
                "task_id": task["task_id"],
                "qrcode": task["qrcode"],
                "interval": int(task["interval"]),
            }})

        if method == "POST" and path == "/api/qqofficial/qr/poll":
            from ..onebot import qqofficial_bind

            body = self._read_body()
            task_id = str(body.get("task_id", "") or "") if isinstance(body, dict) else ""
            adapter_id = str(body.get("adapter_id", "") or "") if isinstance(body, dict) else ""
            with self.webui._qr_bind_lock:
                entry = self.webui._qr_bind_tasks.get(task_id)
            if entry is None:
                return self._send_json({"code": 404, "msg": _t("connections.qr_task_not_found")}, 404)
            try:
                result = qqofficial_bind.poll_bind_task_sync(task_id, entry["bind_key"])
            except Exception as exc:
                return self._send_json({"code": 502, "msg": _t("connections.qr_poll_failed", error=exc)}, 502)
            status = str(result.get("status") or "pending")
            if status == "created" and adapter_id:
                connections = getattr(plugin, "connections", None)
                if connections is None:
                    return self._send_json({"code": 500, "msg": _t("connections.unavailable")}, 500)
                try:
                    # 扫码成功即视为"要登录"：同时强制启用该卡片，
                    # 这样无需等用户点保存，reload 后立即建连，
                    # 否则用户 QQ 客户端会一直停在"连接中"状态
                    connections.update(adapter_id, {
                        "app_id": str(result.get("appid") or ""),
                        "app_secret": str(result.get("secret") or ""),
                        "enabled": True,
                    })
                except Exception as exc:
                    return self._send_json({"code": 500, "msg": _t("connections.qr_save_failed", error=exc)}, 500)
                try:
                    plugin.reload_onebot_connection()
                except Exception:
                    pass
                with self.webui._qr_bind_lock:
                    self.webui._qr_bind_tasks.pop(task_id, None)
                # AppSecret 不回传前端，仅回执 AppID 与密钥长度（供前端渲染等长掩码）
                return self._send_json({"code": 200, "data": {
                    "status": "created",
                    "appid": result.get("appid"),
                    "secret_len": len(str(result.get("secret") or "")),
                }})
            return self._send_json({"code": 200, "data": {"status": status, "message": result.get("message", "")}})

        if path == "/api/rules":
            regex = plugin.regex_module
            if method == "GET":
                # M5：序列化前先取快照（优先 regex.snapshot_rules()），
                # 避免与规则保存线程并发替换/清空 list 导致序列化竞态
                if regex:
                    rules_snapshot = (
                        regex.snapshot_rules() if hasattr(regex, "snapshot_rules")
                        else copy.deepcopy(regex.rules)
                    )
                else:
                    rules_snapshot = []
                return self._send_json({"code": 200, "data": rules_snapshot})
            if method == "POST":
                body = self._read_body()
                if not isinstance(body, list):
                    return self._send_json({"code": 400, "msg": _t("webui.msg.rules_must_be_array")}, 400)
                if regex:
                    regex.save_rules(body)
                return self._send_json({"code": 200, "msg": _t("webui.msg.rules_saved")})

        if method == "POST" and path == "/api/rules/image":
            # 正则规则「回复图片」动作的图片上传：存到数据目录 rules_images/ 下
            # （该目录在 image() 本地白名单内，动作执行时可直接按路径读取发送）
            file_info = self._read_multipart_file()
            if not file_info:
                return self._send_json({"code": 400, "msg": _t("webui.msg.rules_image_invalid")}, 400)
            filename, content = file_info
            ext = Path(filename).suffix.lower().lstrip(".")
            if ext == "jpeg":
                ext = "jpg"
            if ext not in ("png", "jpg", "gif", "webp"):
                return self._send_json({"code": 400, "msg": _t("webui.msg.rules_image_type")}, 400)
            if len(content) > 8 * 1024 * 1024:
                return self._send_json({"code": 400, "msg": _t("webui.msg.rules_image_too_large")}, 400)
            img_dir = Path(plugin.data_folder) / "rules_images"
            img_dir.mkdir(parents=True, exist_ok=True)
            fname = f"rule_{int(time.time() * 1000)}_{secrets.token_hex(4)}.{ext}"
            try:
                (img_dir / fname).write_bytes(content)
            except OSError as exc:
                return self._send_json({"code": 500, "msg": _t("webui.msg.rules_image_write_failed", error=exc)}, 500)
            return self._send_json({"code": 200, "data": {"path": str(img_dir / fname), "name": fname}})

        if path == "/api/whitelist":
            wl = getattr(plugin, "whitelist_module", None)
            if method == "GET":
                domain = str(query.get("domain", "") or "")
                data = wl.snapshot(domain if domain in ("qq", "official") else None) if wl else []
                return self._send_json({"code": 200, "data": data})

        if method == "GET" and path == "/api/whitelist/domains":
            # 白名单域状态：按适配器启用/连接情况给出默认展示域
            statuses = plugin.hub.status() if getattr(plugin, "hub", None) else []
            enabled_qq = any(s.get("type") != "qqofficial" and s.get("enabled") for s in statuses)
            enabled_official = any(s.get("type") == "qqofficial" and s.get("enabled") for s in statuses)
            connected_qq = any(s.get("type") != "qqofficial" and s.get("connected") for s in statuses)
            connected_official = any(s.get("type") == "qqofficial" and s.get("connected") for s in statuses)
            if not enabled_official:
                # 仅个人号（或无官方适配器）：QQ 号域
                default = "qq"
            elif not enabled_qq:
                # 仅官方 bot：openid 域
                default = "official"
            elif connected_qq:
                # 双开：个人号已连接（含双连接）默认 QQ 号域
                default = "qq"
            elif connected_official:
                # 双开但仅官方连接成功：openid 域
                default = "official"
            else:
                default = "qq"
            return self._send_json({"code": 200, "data": {
                "has_qq": enabled_qq,
                "has_official": enabled_official,
                "connected_qq": connected_qq,
                "connected_official": connected_official,
                "default": default,
            }})

        m = re.fullmatch(r"/api/whitelist/([0-9A-Za-z_-]{1,64})", path)
        if m and method == "DELETE":
            wl = getattr(plugin, "whitelist_module", None)
            domain = str(query.get("domain", "") or "qq")
            if domain not in ("qq", "official"):
                domain = "qq"
            if not wl or not wl.get_binding_by_qq(m.group(1), domain):
                return self._send_json({"code": 404, "msg": _t("webui.msg.qq_not_bound")}, 404)
            success, message, entry = wl.unbind_sync(m.group(1), domain=domain)
            if success:
                return self._send_json({
                    "code": 200,
                    "msg": message,
                    "data": entry,
                })
            return self._send_json({
                "code": 409,
                "msg": _t("webui.msg.unbind_failed", msg=message),
                "data": entry,
            }, 409)

        if method == "GET" and path == "/api/market/config":
            client = getattr(plugin, "marketplace", None)
            cfg = plugin.config_manager.data.get("marketplace", {}) if plugin.config_manager else {}
            return self._send_json({"code": 200, "data": {
                "enabled": bool(client and client.enabled),
                "configured": bool(cfg.get("api_url")),
                "api_url": str(cfg.get("api_url") or ""),
                "allow_http": bool(cfg.get("allow_http", False)),
            }})

        if method == "GET" and path == "/api/market/plugins":
            client = getattr(plugin, "marketplace", None)
            if client is None or not client.enabled:
                return self._send_json({"code": 403, "msg": "插件市场未配置或未启用"}, 403)
            try:
                page = max(1, int(query.get("page", "1")))
                limit = max(1, min(100, int(query.get("limit", "24"))))
                data = client.browse(search=query.get("q", ""), category=query.get("category", ""), sort=query.get("sort", "score"), page=page, limit=limit)
                return self._send_json({"code": 200, "data": data})
            except Exception as exc:  # noqa: BLE001
                return self._send_json({"code": 400, "msg": str(exc)}, 400)

        m = re.fullmatch(r"/api/market/plugin/([a-z0-9][a-z0-9_-]{1,63})", path)
        if m and method == "GET":
            client = getattr(plugin, "marketplace", None)
            if client is None or not client.enabled:
                return self._send_json({"code": 403, "msg": "插件市场未配置或未启用"}, 403)
            try:
                return self._send_json({"code": 200, "data": client.plugin_detail(m.group(1))})
            except Exception as exc:  # noqa: BLE001
                return self._send_json({"code": 400, "msg": str(exc)}, 400)

        if method == "GET" and path == "/api/market/cover":
            # 封面图片代理：<img> 标签无法携带 Authorization 头，token 经 query 传入。
            # 浏览器直连市场站点会因混合内容 / 防盗链 Cookie 失败，统一由后端代取。
            client = getattr(plugin, "marketplace", None)
            if client is None or not client.enabled:
                return self._send_json({"code": 403, "msg": "插件市场未配置或未启用"}, 403)
            try:
                ctype, raw = client.fetch_cover(query.get("url", ""))
            except Exception as exc:  # noqa: BLE001
                return self._send_json({"code": 400, "msg": str(exc)}, 400)
            # M1：SVG 可内嵌脚本构成 XSS，代理层强制拒绝（纵深防御，与 fetch_cover 白名单互补）
            if "svg" in ctype.lower():
                return self._send_json({"code": 403, "msg": "不允许的封面图片类型"}, 403)
            if self._safe_send_headers(200, [
                ("Content-Type", ctype),
                ("Content-Length", str(len(raw))),
                ("Cache-Control", "private, max-age=3600"),
                ("Referrer-Policy", "no-referrer"),
                ("X-Content-Type-Options", "nosniff"),
            ]):
                self._safe_write(raw)
            return

        if method == "POST" and path == "/api/market/report":
            client = getattr(plugin, "marketplace", None)
            if client is None or not client.enabled:
                return self._send_json({"code": 403, "msg": "插件市场未配置或未启用"}, 403)
            body = self._read_body() or {}
            if not isinstance(body, dict):
                return self._send_json({"code": 400, "msg": _t("webui.msg.invalid_body")}, 400)
            try:
                result = client.report_plugin(str(body.get("id") or ""), str(body.get("reason") or ""), str(body.get("contact") or ""))
                return self._send_json({"code": 201, "data": result}, 201)
            except Exception as exc:  # noqa: BLE001
                return self._send_json({"code": 400, "msg": str(exc)}, 400)

        if method == "POST" and path == "/api/market/like":
            client = getattr(plugin, "marketplace", None)
            if client is None or not client.enabled:
                return self._send_json({"code": 403, "msg": "插件市场未配置或未启用"}, 403)
            body = self._read_body() or {}
            if not isinstance(body, dict):
                return self._send_json({"code": 400, "msg": _t("webui.msg.invalid_body")}, 400)
            market_id = str(body.get("id") or "").strip()
            if not market_id:
                return self._send_json({"code": 400, "msg": _t("webui.msg.invalid_market_id")}, 400)
            try:
                result = client.like_plugin(market_id, bool(body.get("liked", True)))
                return self._send_json({"code": 200, "data": result})
            except Exception as exc:  # noqa: BLE001
                return self._send_json({"code": 400, "msg": str(exc)}, 400)

        if method == "POST" and path == "/api/market/check":
            client = getattr(plugin, "marketplace", None)
            if client is None or not client.enabled:
                return self._send_json({"code": 403, "msg": "插件市场未配置或未启用"}, 403)
            def _run_check(log, progress):
                log(_t("task_log.checking_updates"))
                progress(30, _t("task_log.checking_updates"))
                result = {"updates": client.check_subplugin_updates(force=True)}
                progress(100, _t("task_log.done"))
                return result
            task_id = self.webui._start_market_task("check_updates", _run_check)
            if task_id is None:
                return self._send_json({"code": 429, "msg": "任务数过多，请稍后再试"}, 429)
            return self._send_json({"code": 200, "data": {"task_id": task_id}})

        if method == "POST" and path == "/api/market/update-all":
            client = getattr(plugin, "marketplace", None)
            if client is None or not client.enabled:
                return self._send_json({"code": 403, "msg": "插件市场未配置或未启用"}, 403)
            def _run_update_all(log, progress):
                log(_t("task_log.checking_updates"))
                return client.update_all(log=log, progress=progress)
            task_id = self.webui._start_market_task("update_all", _run_update_all)
            if task_id is None:
                return self._send_json({"code": 429, "msg": "任务数过多，请稍后再试"}, 429)
            return self._send_json({"code": 200, "data": {"task_id": task_id}})

        if method == "POST" and path == "/api/market/install":
            client = getattr(plugin, "marketplace", None)
            body = self._read_body() or {}
            market_id = str(body.get("id", "")).strip() if isinstance(body, dict) else ""
            version = str(body.get("version", "")).strip() if isinstance(body, dict) else ""
            if client is None or not client.enabled:
                return self._send_json({"code": 403, "msg": "插件市场未配置或未启用"}, 403)
            if not market_id:
                return self._send_json({"code": 400, "msg": _t("webui.msg.invalid_market_id")}, 400)
            def _run_install(log, progress):
                log(_t("task_log.installing_plugin", id=market_id))
                progress(20, _t("task_log.downloading"))
                result = client.install(market_id, version, upgrade_dependencies=True, log=log, progress=progress)
                progress(100, _t("task_log.done"))
                return result
            task_id = self.webui._start_market_task("install", _run_install)
            if task_id is None:
                return self._send_json({"code": 429, "msg": "任务数过多，请稍后再试"}, 429)
            return self._send_json({"code": 200, "data": {"task_id": task_id}})

        m = re.fullmatch(r"/api/subplugins/([A-Za-z0-9_\-]+)/market-update", path)
        if m and method == "POST":
            client = getattr(plugin, "marketplace", None)
            if client is None or not client.enabled:
                return self._send_json({"code": 403, "msg": "插件市场未配置或未启用"}, 403)
            name = m.group(1)
            body = self._read_body() or {}
            requested_version = str(body.get("version", "")).strip() if isinstance(body, dict) else ""
            def _run_plugin_update(log, progress):
                log(_t("task_log.updating_plugin", name=name))
                progress(20, _t("task_log.downloading"))
                result = client.update(name, requested_version, update_dependencies=True, log=log, progress=progress)
                progress(100, _t("task_log.done"))
                return result
            task_id = self.webui._start_market_task("plugin_update", _run_plugin_update)
            if task_id is None:
                return self._send_json({"code": 429, "msg": "任务数过多，请稍后再试"}, 429)
            return self._send_json({"code": 200, "data": {"task_id": task_id}})

        m = re.fullmatch(r"/api/subplugins/([A-Za-z0-9_\-]+)/update-deps", path)
        if m and method == "POST":
            client = getattr(plugin, "marketplace", None)
            if client is None or not client.enabled:
                return self._send_json({"code": 403, "msg": "插件市场未配置或未启用"}, 403)
            name = m.group(1)
            def _run_deps_update(log, progress):
                log(_t("task_log.updating_deps", name=name))
                progress(30, _t("task_log.installing_deps"))
                result = client.update_dependencies(name, log=log, progress=progress)
                progress(100, _t("task_log.done"))
                return result
            task_id = self.webui._start_market_task("dependencies_update", _run_deps_update)
            if task_id is None:
                return self._send_json({"code": 429, "msg": "任务数过多，请稍后再试"}, 429)
            return self._send_json({"code": 200, "data": {"task_id": task_id}})

        # 插件级强制依赖（requires）补装：子插件依赖从市场自动安装（像 pip 依赖那样）
        m = re.fullmatch(r"/api/subplugins/([A-Za-z0-9_\-]+)/install-requirements", path)
        if m and method == "POST":
            client = getattr(plugin, "marketplace", None)
            if client is None or not client.enabled:
                return self._send_json({"code": 403, "msg": "插件市场未配置或未启用"}, 403)
            mgr = plugin.subplugin_manager
            if mgr is None:
                return self._send_json({"code": 500, "msg": _t("webui.msg.subplugin_manager_unavailable")}, 500)
            name = m.group(1)
            with mgr._lock:
                if mgr.subplugins.get(name) is None:
                    return self._send_json({"code": 404, "msg": _t("webui.msg.subplugin_not_found")}, 404)
            def _run_requirements_install(log, progress):
                log(_t("task_log.installing_requirements", name=name))
                progress(20, _t("task_log.installing_deps"))
                result = client.install_plugin_requirements(name, log=log, progress=progress)
                progress(100, _t("task_log.done"))
                return result
            task_id = self.webui._start_market_task("requirements_install", _run_requirements_install)
            if task_id is None:
                return self._send_json({"code": 429, "msg": "任务数过多，请稍后再试"}, 429)
            return self._send_json({"code": 200, "data": {"task_id": task_id}})

        m = re.fullmatch(r"/api/market/task/([\w-]+)", path)
        if m and method == "GET":
            # 锁内深拷贝任务快照：result 内的嵌套结构可能与后台线程共享引用
            import copy as _copy
            with self.webui._market_tasks_lock:
                task = self.webui._market_tasks.get(m.group(1))
                if task is None:
                    return self._send_json({"code": 404, "msg": "未找到市场任务"}, 404)
                snapshot = _copy.deepcopy({k: v for k, v in task.items() if k != "start_time"})
            return self._send_json({"code": 200, "data": snapshot})

        if method == "POST" and path == "/api/updates/stage":
            client = getattr(plugin, "marketplace", None)
            update_cfg = plugin.config_manager.data.get("updates", {}) if plugin.config_manager else {}
            if not bool(update_cfg.get("enable", True)):
                return self._send_json({"code": 403, "msg": "框架更新功能已关闭"}, 403)
            if client is None:
                return self._send_json({"code": 500, "msg": "更新客户端不可用"}, 500)
            def _run_stage(log, progress):
                log(_t("task_log.staging_framework"))
                progress(20, _t("task_log.downloading"))
                result = client.stage_framework_update(log=log, progress=progress)
                progress(100, _t("task_log.done"))
                return result
            task_id = self.webui._start_market_task("framework_update", _run_stage)
            if task_id is None:
                return self._send_json({"code": 429, "msg": "任务数过多，请稍后再试"}, 429)
            return self._send_json({"code": 200, "data": {"task_id": task_id}})

        if method == "POST" and path == "/api/updates/apply":
            client = getattr(plugin, "marketplace", None)
            update_cfg = plugin.config_manager.data.get("updates", {}) if plugin.config_manager else {}
            if not bool(update_cfg.get("enable", True)):
                return self._send_json({"code": 403, "msg": "框架更新功能已关闭"}, 403)
            if client is None:
                return self._send_json({"code": 500, "msg": "更新客户端不可用"}, 500)
            def _run_apply(log, progress):
                log(_t("task_log.applying_framework"))
                progress(20, _t("task_log.downloading"))
                result = client.apply_framework_update(log=log, progress=progress)
                progress(100, _t("task_log.reloading"))
                return result
            task_id = self.webui._start_market_task("framework_apply", _run_apply)
            if task_id is None:
                return self._send_json({"code": 429, "msg": "任务数过多，请稍后再试"}, 429)
            return self._send_json({"code": 200, "data": {"task_id": task_id}})

        if method == "GET" and path == "/api/updates/check":
            client = getattr(plugin, "marketplace", None)
            update_cfg = plugin.config_manager.data.get("updates", {}) if plugin.config_manager else {}
            if not bool(update_cfg.get("enable", True)):
                return self._send_json({"code": 200, "data": {"configured": False, "available": False}})
            if client is None:
                return self._send_json({"code": 500, "msg": "更新客户端不可用"}, 500)
            try:
                return self._send_json({"code": 200, "data": client.framework_update_info()})
            except Exception as exc:  # noqa: BLE001
                return self._send_json({"code": 400, "msg": str(exc)}, 400)

        if method == "GET" and path == "/api/subplugins":
            mgr = plugin.subplugin_manager
            market_client = getattr(plugin, "marketplace", None)
            market_updates = market_client.cached_updates() if market_client else {}
            data = []
            if mgr:
                # 全部字段提取都在 mgr._lock 内完成为纯 dict，
                # 避免锁外访问 sp/manifest 对象时被并发 reload/install 改写
                with mgr._lock:
                    for name, sp in mgr.subplugins.items():
                        manifest = sp.manifest if isinstance(sp.manifest, dict) else {}
                        declaration = parse_requires_from_manifest(manifest)
                        data.append({
                            "name": name,
                            "version": manifest.get("version", "?"),
                            "description": manifest.get("desc", ""),
                            "load": manifest.get("load", True),
                            "priority": manifest.get("priority", "main"),
                            "loaded": sp.loaded,
                            "error": sp.error,
                            "missing_deps": list(sp.missing_deps),
                            "missing_modules": list(sp.missing_modules),
                            "missing_requirements": list(sp.missing_requirements),
                            "requires": {
                                "subplugins": [r.display() for r in declaration.subplugins],
                                "endstone": [r.display() for r in declaration.endstone],
                            },
                            "dependencies": list(manifest.get("dependencies", []) or []),
                            "market": dict(manifest.get("_market", {})) if isinstance(manifest.get("_market", {}), dict) else {},
                            "market_update": dict(market_updates.get(name, {})),
                        })
            return self._send_json({"code": 200, "data": data})

        m = re.fullmatch(r"/api/subplugins/([A-Za-z0-9_\-]+)/toggle", path)
        if m and method == "POST":
            mgr = plugin.subplugin_manager
            body = self._read_body() or {}
            if not isinstance(body, dict):
                return self._send_json({"code": 400, "msg": _t("webui.msg.invalid_body")}, 400)
            enable = bool(body.get("enable", True))
            if mgr and mgr.set_enabled(m.group(1), enable):
                return self._send_json({
                    "code": 200,
                    "msg": _t(
                        "webui.msg.subplugin_toggled",
                        state=(_t("common.enabled") if enable else _t("common.disabled")),
                    ),
                })
            return self._send_json({"code": 404, "msg": _t("webui.msg.subplugin_not_found")}, 404)

        if method == "POST" and path == "/api/subplugins/reload":
            done = threading.Event()
            finish, defer = self._acquire_main_op(done)
            if finish is None:
                return self._send_json({"code": 409, "msg": "另一主线程操作正在执行，请稍后再试"}, 409)
            count = [0]
            err = [""]

            def do_reload() -> None:
                try:
                    if plugin.subplugin_manager:
                        count[0] = plugin.subplugin_manager.reload_all()
                except Exception as e:
                    err[0] = str(e)
                finally:
                    done.set()

            try:
                plugin.run_on_main(do_reload)
            except Exception as e:  # noqa: BLE001
                # 调度失败（服务器关停等）：任务未入队，done 永不置位，
                # 必须立即释放互斥锁，否则所有主线程操作接口永久 409
                finish()
                return self._send_json({"code": 500, "msg": _t("webui.msg.reload_failed", error=e)}, 500)
            done.wait(timeout=10)
            if not done.is_set():
                defer()
                return self._send_json({"code": 504, "msg": _t("webui.msg.reload_timeout")}, 504)
            try:
                if err[0]:
                    return self._send_json({"code": 500, "msg": _t("webui.msg.reload_failed", error=err[0])}, 500)
                return self._send_json({"code": 200, "msg": _t("webui.msg.subplugin_reload_success", count=count[0])})
            finally:
                finish()

        m = re.fullmatch(r"/api/subplugins/([A-Za-z0-9_\-]+)/reload", path)
        if m and method == "POST":
            name = m.group(1)
            if not plugin.subplugin_manager:
                return self._send_json({"code": 500, "msg": _t("webui.msg.subplugin_not_found")}, 500)
            done = threading.Event()
            finish, defer = self._acquire_main_op(done)
            if finish is None:
                return self._send_json({"code": 409, "msg": "另一主线程操作正在执行，请稍后再试"}, 409)
            ok = [False]
            err = [""]

            def do_reload_one() -> None:
                try:
                    ok[0] = plugin.subplugin_manager.reload_one(name)
                except Exception as e:
                    err[0] = str(e)
                finally:
                    done.set()

            try:
                plugin.run_on_main(do_reload_one)
            except Exception as e:  # noqa: BLE001
                # 调度失败：任务未入队，立即释放互斥锁（同上，防永久 409）
                finish()
                return self._send_json({"code": 500, "msg": _t("webui.msg.reload_failed", error=e)}, 500)
            done.wait(timeout=15)
            if not done.is_set():
                defer()
                return self._send_json({"code": 504, "msg": _t("webui.msg.reload_timeout")}, 504)
            try:
                if err[0]:
                    return self._send_json({"code": 500, "msg": _t("webui.msg.subplugin_reload_one_failed", name=name)}, 500)
                if ok[0]:
                    return self._send_json({"code": 200, "msg": _t("webui.msg.subplugin_reload_one_success", name=name)})
                return self._send_json({"code": 400, "msg": _t("webui.msg.subplugin_reload_one_failed", name=name)}, 400)
            finally:
                finish()

        if method == "POST" and path == "/api/reload":
            done = threading.Event()
            finish, defer = self._acquire_main_op(done)
            if finish is None:
                return self._send_json({"code": 409, "msg": "另一主线程操作正在执行，请稍后再试"}, 409)
            err = [""]

            def do_framework_reload() -> None:
                try:
                    plugin.config_manager.load()
                    plugin._init_i18n()
                    # 失效 pip manager 缓存使新配置生效；加锁避免与 _get_pip_manager 双检锁竞态。
                    with plugin._pip_manager_lock:
                        plugin._pip_manager = None
                    # 连接参数只在适配器重建后生效。
                    plugin.reload_onebot_connection()
                    if plugin.regex_module:
                        plugin.regex_module.reload_rules()
                    if plugin.subplugin_manager:
                        plugin.subplugin_manager.reload_all()
                except Exception as e:
                    err[0] = str(e)
                finally:
                    done.set()

            try:
                plugin.run_on_main(do_framework_reload)
            except Exception as e:  # noqa: BLE001
                # 调度失败：任务未入队，立即释放互斥锁（同上，防永久 409）
                finish()
                return self._send_json({"code": 500, "msg": _t("webui.msg.reload_failed", error=e)}, 500)
            done.wait(timeout=20)
            if not done.is_set():
                defer()
                return self._send_json({"code": 504, "msg": _t("webui.msg.reload_timeout")}, 504)
            try:
                if err[0]:
                    return self._send_json({"code": 500, "msg": _t("webui.msg.reload_failed", error=err[0])}, 500)
                return self._send_json({"code": 200, "msg": _t("webui.msg.reload_success")})
            finally:
                finish()

        if method == "POST" and path == "/api/pip/install":
            body = self._read_body() or {}
            if not isinstance(body, dict):
                return self._send_json({"code": 400, "msg": _t("webui.msg.invalid_body")}, 400)
            packages = body.get("packages", [])
            if not packages or not isinstance(packages, list):
                return self._send_json({"code": 400, "msg": _t("pip.no_packages_specified")}, 400)
            # 校验每个包参数（防 pip 选项/URL/路径注入）：
            # 必须是字符串、不以 - 开头、不含 git+/://、路径分隔符与空白字符
            def _valid_pkg(p: Any) -> bool:
                if not isinstance(p, str) or not p or not p.strip():
                    return False
                if p.startswith("-") or p != p.strip():
                    return False
                if "git+" in p or "://" in p or "/" in p or "\\" in p:
                    return False
                return not any(ch.isspace() for ch in p)

            if not all(_valid_pkg(p) for p in packages):
                return self._send_json({"code": 400, "msg": _t("pip.invalid_package_arg", packages="...")}, 400)
            mgr = self.webui._get_pip_manager_for_plugin(plugin)
            if mgr is None:
                return self._send_json({"code": 500, "msg": _t("pip.manager_unavailable")}, 500)
            if not mgr.enable:
                return self._send_json({"code": 403, "msg": _t("pip.disabled")}, 403)
            task_id = self.webui._start_pip_task(mgr, packages, "install")
            if task_id is None:
                return self._send_json({"code": 429, "msg": "任务数过多，请稍后再试"}, 429)
            return self._send_json({"code": 200, "data": {"task_id": task_id}})

        m = re.fullmatch(r"/api/subplugins/([A-Za-z0-9_\-]+)/install-deps", path)
        if m and method == "POST":
            name = m.group(1)
            mgr = plugin.subplugin_manager
            if not mgr:
                return self._send_json({"code": 500, "msg": _t("webui.msg.subplugin_manager_unavailable")}, 500)
            # 在锁内拷贝 deps，避免锁外访问 sp.manifest
            with mgr._lock:
                sp = mgr.subplugins.get(name)
                deps = list(sp.manifest.get("dependencies", []) or []) if sp else []
            if not sp:
                return self._send_json({"code": 404, "msg": _t("webui.msg.subplugin_not_found")}, 404)
            if not deps:
                return self._send_json({"code": 400, "msg": _t("pip.no_declared_deps", name=name)}, 400)
            pip_mgr = self.webui._get_pip_manager_for_plugin(plugin)
            if pip_mgr is None:
                return self._send_json({"code": 500, "msg": _t("pip.manager_unavailable")}, 500)
            if not pip_mgr.enable:
                return self._send_json({"code": 403, "msg": _t("pip.disabled")}, 403)
            task_id = self.webui._start_pip_task(
                pip_mgr, deps, "install_deps", subplugin_name=name, reload_after_install=False
            )
            if task_id is None:
                return self._send_json({"code": 429, "msg": "任务数过多，请稍后再试"}, 429)
            return self._send_json({"code": 200, "data": {"task_id": task_id}})

        if method == "POST" and path == "/api/pip/uninstall":
            body = self._read_body() or {}
            if not isinstance(body, dict):
                return self._send_json({"code": 400, "msg": _t("webui.msg.invalid_body")}, 400)
            raw_pkg = body.get("package", "")
            if not isinstance(raw_pkg, str):
                return self._send_json({"code": 400, "msg": _t("pip.no_packages_specified")}, 400)
            package = raw_pkg.strip()
            if not package:
                return self._send_json({"code": 400, "msg": _t("pip.no_packages_specified")}, 400)
            mgr = self.webui._get_pip_manager_for_plugin(plugin)
            if mgr is None:
                return self._send_json({"code": 500, "msg": _t("pip.manager_unavailable")}, 500)
            if not mgr.enable:
                return self._send_json({"code": 403, "msg": _t("pip.disabled")}, 403)
            # 改走异步任务：原实现在请求线程内无限期等待 _pip_serial_lock
            # 再同步跑 pip 子进程（最长 60s），期间 HTTP 请求永久挂起
            task_id = self.webui._start_pip_task(mgr, [package], "uninstall")
            if task_id is None:
                return self._send_json({"code": 429, "msg": "任务数过多，请稍后再试"}, 429)
            return self._send_json({"code": 200, "data": {"task_id": task_id}})

        if method == "GET" and path == "/api/pip/list":
            mgr = self.webui._get_pip_manager_for_plugin(plugin)
            if mgr is None:
                return self._send_json({"code": 500, "msg": _t("pip.manager_unavailable")}, 500)
            if not mgr.enable:
                return self._send_json({"code": 403, "msg": _t("pip.disabled")}, 403)
            # pip list 是同步子进程（冷启动 1-3s）：10s 缓存避免前端并发
            # 刷新刷出多个 pip 进程
            now = time.time()
            with self.webui._pip_list_lock:
                cached = self.webui._pip_list_cache
                if cached is not None and now - cached[0] < 10.0:
                    pkgs = cached[1]
                else:
                    pkgs = mgr.list_packages()
                    self.webui._pip_list_cache = (now, pkgs)
            # 标记受保护包（不修改原对象，构造新 dict）
            from ..pip_manager import PROTECTED_PACKAGES, _normalize
            protected_norm = {_normalize(p) for p in PROTECTED_PACKAGES}
            result = []
            for p in pkgs:
                item = dict(p)
                item["protected"] = _normalize(str(p.get("name", ""))) in protected_norm
                result.append(item)
            return self._send_json({"code": 200, "data": result})

        if method == "GET" and path == "/api/pip/config":
            cfg = plugin.config_manager.data.get("pip", {}) if plugin.config_manager else {}
            return self._send_json({"code": 200, "data": {
                "enable": cfg.get("enable", True),
                "index_url": cfg.get("index_url", ""),
                "timeout": cfg.get("timeout", 300),
            }})

        m = re.fullmatch(r"/api/pip/task/([\w-]+)", path)
        if m and method == "GET":
            task_id = m.group(1)
            # 在锁内拷贝字段，避免 on_log 并发 append 导致序列化异常
            with self.webui._pip_tasks_lock:
                task = self.webui._pip_tasks.get(task_id)
                if not task:
                    return self._send_json({"code": 404, "msg": _t("pip.task_not_found")}, 404)
                snapshot = {
                    "status": task["status"],
                    "log_lines": list(task["log_lines"]),
                    "packages": list(task["packages"]),
                    "done": task["done"],
                    "success": task["success"],
                    "msg": task["msg"],
                    "subplugin_name": task.get("subplugin_name"),
                    "installation_success": task.get("installation_success", False),
                    "reload_success": task.get("reload_success"),
                    "reload_required": task.get("reload_required", False),
                }
            return self._send_json({"code": 200, "data": snapshot})

        if method == "GET" and path == "/api/plugins/configs":
            with self.webui._ext_lock:
                keys = list(self.webui.plugins_config_schema.keys())
            return self._send_json({
                "code": 200,
                "data": keys,
            })
        m = re.fullmatch(r"/api/plugins/config/([A-Za-z0-9_\-]+)", path)
        if m:
            name = m.group(1)
            with self.webui._ext_lock:
                schema = self.webui.plugins_config_schema.get(name)
                if method == "GET":
                    if not schema:
                        return self._send_json({"code": 404, "msg": _t("webui.msg.plugin_no_config")}, 404)
                    # 深拷贝 schema 避免外部修改影响内部对象
                    schema_snapshot = json.loads(json.dumps(schema))
                else:
                    schema_snapshot = schema
            if method == "GET":
                return self._send_json({"code": 200, "data": schema_snapshot})
            if method == "POST":
                if not schema:
                    return self._send_json({"code": 404, "msg": _t("webui.msg.plugin_no_config")}, 404)
                body = self._read_body() or {}
                # 非 dict JSON（list/str/number）会让下方 body.items() 抛
                # AttributeError 落入兜底 500；与其他 POST 端点口径一致返回 400
                if not isinstance(body, dict):
                    return self._send_json({"code": 400, "msg": _t("webui.msg.invalid_body")}, 400)

                def _type_ok(item: dict[str, Any], v: Any) -> bool:
                    """按 schema item 的 type 校验值类型，非法类型直接跳过不写入"""
                    t = item.get("type")
                    if t == "number":
                        # bool 是 int 子类，必须显式排除
                        ok = isinstance(v, (int, float)) and not isinstance(v, bool)
                        if ok and isinstance(item.get("min"), (int, float)):
                            ok = v >= item["min"]
                        if ok and isinstance(item.get("max"), (int, float)):
                            ok = v <= item["max"]
                        return ok
                    if t == "switch":
                        return isinstance(v, bool)
                    if t in ("select", "multiselect"):
                        options = item.get("options")
                        if isinstance(options, list):
                            allowed = {
                                o.get("value") if isinstance(o, dict) else o for o in options
                            }
                            if t == "multiselect":
                                # 多选：必须是列表且每个值都在可选项内
                                return isinstance(v, list) and all(x in allowed for x in v)
                            return v in allowed
                        return True
                    if t in ("array",):
                        return isinstance(v, list)
                    # text / textarea 及未知类型按字符串处理
                    return isinstance(v, str)

                # 加锁保护共享 schema，防止并发 POST 修改时迭代异常
                applied: list[tuple[str, Any]] = []
                with self.webui._ext_lock:
                    for item in list(schema["items"]):
                        if item.get("type") == "section":
                            continue  # 分组标题不参与配置读写
                        if item["key"] in body:
                            value = body[item["key"]]
                            if _type_ok(item, value):
                                item["val"] = value
                                applied.append((item["key"], value))
                # 只广播真正写入的键值：类型校验失败的值没有落盘，
                # 若照发 config.update 事件，子插件会拿到未保存的非法值
                for key, value in applied:
                    plugin.bus.emit(f"config.update.{name}", key, value)
                return self._send_json({"code": 200, "msg": _t("webui.msg.plugin_config_saved")})

        if method == "GET" and path == "/api/config/labels":
            return self._send_json({"code": 200, "data": build_config_labels()})

        if method == "POST" and path == "/api/subplugins/install/upload":
            file_info = self._read_multipart_file()
            if not file_info:
                return self._send_json(
                    {"code": 400, "msg": "未接收到有效的文件（需 multipart/form-data，最大 16MB）"}, 400
                )
            filename, content = file_info
            if not filename.lower().endswith(".zip"):
                return self._send_json({"code": 400, "msg": _t("webui.msg.zip_only")}, 400)
            import tempfile
            with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tf:
                tf.write(content)
                tmp_zip = tf.name
            tmp_zip_cleaned = [False]

            def _cleanup_tmp_zip() -> None:
                if tmp_zip_cleaned[0]:
                    return
                tmp_zip_cleaned[0] = True
                try:
                    os.unlink(tmp_zip)
                except OSError:
                    pass

            # 临时文件清理统一由 do_install 的 finally 负责（含超时场景），
            # 请求线程超时返回后不再二次等待，避免请求线程被继续占用
            result: list[Any] = [False, _t("webui.msg.install_timeout"), ""]
            done = threading.Event()
            # 安装与 reload/uninstall 同为主线程长操作：必须经 _main_op_lock
            # 串行化，否则并发读写 subplugins 字典与插件目录会产生半安装状态
            finish, defer = self._acquire_main_op(done)
            if finish is None:
                _cleanup_tmp_zip()
                return self._send_json({"code": 409, "msg": "另一主线程操作正在执行，请稍后再试"}, 409)
            deferred = False

            def do_install() -> None:
                try:
                    result[0], result[1], result[2] = plugin.subplugin_manager.install_from_zip(tmp_zip)
                except Exception as e:  # noqa: BLE001
                    result[0], result[1] = False, _t("webui.msg.install_exception", error=e)
                finally:
                    done.set()
                    _cleanup_tmp_zip()

            try:
                try:
                    plugin.run_on_main(do_install)
                except Exception as e:  # noqa: BLE001
                    # 调度失败时 do_install 不会执行，须在此清理临时文件
                    _cleanup_tmp_zip()
                    return self._send_json({"code": 500, "msg": _t("webui.msg.install_exception", error=e)}, 500)
                if not done.wait(timeout=30):
                    # 超时路径锁交由 defer 的守护线程兜底释放（等任务真正完成），
                    # 不能被 finally 的 finish() 覆盖，否则 defer 形同虚设
                    defer()
                    deferred = True
                    return self._send_json({"code": 400, "msg": _t("webui.msg.install_timeout")}, 400)
            finally:
                if not deferred:
                    finish()
            ok, msg, name = bool(result[0]), str(result[1]), str(result[2])
            loaded = False
            manager = plugin.subplugin_manager
            if name and manager:
                with manager._lock:
                    loaded = bool(getattr(manager.subplugins.get(name), "loaded", False))
            if ok:
                # ZIP 已落盘和可运行是两个状态：依赖缺失/加载异常时仍应让
                # 前端刷新列表展示可诊断错误，而不能伪装成已完全启用。
                return self._send_json({
                    "code": 200,
                    "msg": msg,
                    "data": {"name": name, "loaded": loaded},
                })
            return self._send_json({"code": 400, "msg": msg}, 400)

        if method == "POST" and path == "/api/subplugins/install/url":
            body = self._read_body() or {}
            if not isinstance(body, dict):
                return self._send_json({"code": 400, "msg": _t("webui.msg.invalid_body")}, 400)
            url = str(body.get("url", "")).strip()
            if not url.startswith(("http://", "https://")):
                return self._send_json({"code": 400, "msg": _t("webui.msg.invalid_url")}, 400)
            parsed = urllib.parse.urlparse(url)
            if not parsed.hostname:
                return self._send_json({"code": 400, "msg": _t("webui.msg.invalid_hostname")}, 400)
            # C5：完整性锚点（可选增强）——
            # - 请求带 sha256（64 位十六进制）→ 下载后强校验，不匹配拒绝安装；
            # - 配置了 webui.install_url_allow_hosts 白名单（非空）→ 强制主机校验；
            # - 两者皆未提供（默认）→ 放行但记 warning 留痕，便于事后审计。
            sha256 = str(body.get("sha256", "") or "").strip().lower()
            if sha256 and not re.fullmatch(r"[a-f0-9]{64}", sha256):
                return self._send_json({"code": 400, "msg": "sha256 字段必须是 64 位十六进制字符串"}, 400)
            client_ip = str(self.client_address[0]) if self.client_address else "unknown"
            try:
                webui_conf = plugin.config_manager.data.get("webui", {}) if plugin.config_manager else {}
            except Exception:  # noqa: BLE001 - 配置读取失败按无白名单处理
                webui_conf = {}
            allow_raw = webui_conf.get("install_url_allow_hosts", []) if isinstance(webui_conf, dict) else []
            allow_hosts = {
                str(h).strip().lower() for h in (allow_raw if isinstance(allow_raw, (list, tuple, set)) else [])
                if str(h).strip()
            }
            url_host = (parsed.hostname or "").lower()
            if allow_hosts and url_host not in allow_hosts:
                return self._send_json({
                    "code": 403,
                    "msg": "该主机不在 webui.install_url_allow_hosts 白名单内；请在配置中调整白名单或移除该限制",
                }, 403)
            if not sha256:
                # 无哈希安装留痕，便于事后审计
                self.webui.logger.warning(
                    f"[WebUI] 子插件 URL 直装（无 sha256 校验）url={url} ip={client_ip}"
                )
            import tempfile

            try:
                # H2：SSRF 防护——内网地址拒绝 + 禁止跨主机重定向
                data = _fetch_url_bytes(url, _MAX_UPLOAD_BYTES, timeout=30)
            except Exception as e:  # noqa: BLE001
                # H2：对外统一“下载失败”，不回显 Connection refused / 404 等细节
                self.webui.logger.warning(
                    f"[WebUI] 子插件 URL 直装下载失败 url={url} ip={client_ip} error={e.__cause__ or e!r}"
                )
                return self._send_json({"code": 400, "msg": "下载失败"}, 400)
            if sha256:
                actual_hash = hashlib.sha256(data).hexdigest()
                if not hmac.compare_digest(actual_hash, sha256):
                    self.webui.logger.warning(
                        f"[WebUI] 子插件 URL 直装 SHA-256 校验失败 url={url} ip={client_ip}"
                    )
                    return self._send_json({"code": 400, "msg": "SHA-256 校验失败，已拒绝安装"}, 400)
            with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tf:
                tf.write(data)
                tmp_zip = tf.name
            tmp_zip_cleaned2 = [False]

            def _cleanup_tmp_zip2() -> None:
                if tmp_zip_cleaned2[0]:
                    return
                tmp_zip_cleaned2[0] = True
                try:
                    os.unlink(tmp_zip)
                except OSError:
                    pass

            result2: list[Any] = [False, _t("webui.msg.install_timeout"), ""]
            done2 = threading.Event()
            # 与 upload 安装端点同款主线程操作互斥（见上方注释）
            finish2, defer2 = self._acquire_main_op(done2)
            if finish2 is None:
                _cleanup_tmp_zip2()
                return self._send_json({"code": 409, "msg": "另一主线程操作正在执行，请稍后再试"}, 409)
            deferred2 = False

            def do_install2() -> None:
                try:
                    result2[0], result2[1], result2[2] = plugin.subplugin_manager.install_from_zip(tmp_zip)
                except Exception as e:  # noqa: BLE001
                    result2[0], result2[1] = False, _t("webui.msg.install_exception", error=e)
                finally:
                    done2.set()
                    _cleanup_tmp_zip2()

            try:
                try:
                    plugin.run_on_main(do_install2)
                except Exception as e:  # noqa: BLE001
                    _cleanup_tmp_zip2()
                    return self._send_json({"code": 500, "msg": _t("webui.msg.install_exception", error=e)}, 500)
                if not done2.wait(timeout=30):
                    defer2()
                    deferred2 = True
                    return self._send_json({"code": 400, "msg": _t("webui.msg.install_timeout")}, 400)
            finally:
                if not deferred2:
                    finish2()
            ok, msg, name = bool(result2[0]), str(result2[1]), str(result2[2])
            loaded = False
            manager = plugin.subplugin_manager
            if name and manager:
                with manager._lock:
                    loaded = bool(getattr(manager.subplugins.get(name), "loaded", False))
            if ok:
                return self._send_json({
                    "code": 200,
                    "msg": msg,
                    "data": {"name": name, "loaded": loaded},
                })
            return self._send_json({"code": 400, "msg": msg}, 400)

        m = re.fullmatch(r"/api/subplugins/([A-Za-z0-9_\-]+)/uninstall-preview", path)
        if m and method == "GET":
            # 卸载预检：前端据此弹窗询问"是否连同卸载依赖项（列出具体依赖名）"，
            # 并在存在反向依赖（其它子插件 requires 本插件）时警告其将无法加载
            try:
                removable, kept = self._compute_uninstall_deps(plugin, m.group(1))
            except Exception as exc:  # noqa: BLE001
                return self._send_json({"code": 400, "msg": str(exc)}, 400)
            dependents: list[dict[str, Any]] = []
            mgr = plugin.subplugin_manager
            if mgr is not None:
                try:
                    dependents = mgr.dependents_of(m.group(1))
                except Exception:  # noqa: BLE001 - 反向依赖查询失败不影响卸载预检
                    dependents = []
            return self._send_json({"code": 200, "data": {"deps": removable, "kept_deps": kept, "dependents": dependents}})

        m = re.fullmatch(r"/api/subplugins/([A-Za-z0-9_\-]+)", path)
        if m and method == "DELETE":
            mgr = plugin.subplugin_manager
            if mgr is None:
                return self._send_json({"code": 500, "msg": _t("webui.msg.subplugin_manager_unavailable")}, 500)
            done3 = threading.Event()
            finish, defer = self._acquire_main_op(done3)
            if finish is None:
                return self._send_json({"code": 409, "msg": "另一主线程操作正在执行，请稍后再试"}, 409)
            # 超时路径锁交由 defer 的守护线程兜底释放（等任务真正完成），
            # 不能被 finally 的 finish() 覆盖，否则 defer 形同虚设
            deferred = False
            try:
                # with_deps=1：连同卸载不被其它子插件使用的 pip 依赖（先在卸载前计算，
                # 卸载后该子插件已从字典移除、manifest 不可得）
                with_deps = str(query.get("with_deps", "")).strip().lower() in ("1", "true", "yes")
                removable: list[str] = []
                if with_deps:
                    try:
                        removable, _kept = self._compute_uninstall_deps(plugin, m.group(1))
                    except Exception:  # noqa: BLE001 - 预检失败不影响主卸载流程
                        removable = []
                result3: list[Any] = [False, _t("webui.msg.uninstall_timeout")]

                def do_uninstall() -> None:
                    try:
                        result3[0], result3[1] = mgr.uninstall(m.group(1))
                    except Exception as e:  # noqa: BLE001
                        result3[0], result3[1] = False, _t("webui.msg.uninstall_exception", error=e)
                    finally:
                        done3.set()

                try:
                    plugin.run_on_main(do_uninstall)
                except Exception as e:  # noqa: BLE001
                    # 调度失败：任务未入队，done3 永不置位，立即释放锁（防永久 409）
                    return self._send_json({"code": 500, "msg": _t("webui.msg.uninstall_exception", error=e)}, 500)
                if not done3.wait(timeout=15):
                    defer()
                    deferred = True
                    return self._send_json({"code": 504, "msg": _t("webui.msg.uninstall_timeout")}, 504)
                # pip 卸载走子进程，在 HTTP 线程执行即可，不占用游戏主线程；
                # 必须持 _pip_serial_lock：与市场安装任务并发写 site-packages 会损坏包元数据
                if result3[0] and removable:
                    removed: list[str] = []
                    pip_mgr = self.webui._get_pip_manager_for_plugin(plugin)
                    if pip_mgr is not None:
                        with plugin._pip_serial_lock:
                            for dep in removable:
                                try:
                                    ok_dep, _msg_dep = pip_mgr.uninstall(dep)
                                except Exception:  # noqa: BLE001
                                    ok_dep = False
                                if ok_dep:
                                    removed.append(dep)
                    if removed:
                        result3[1] = str(result3[1]) + "；" + _t(
                            "webui.msg.uninstall_deps_removed", deps=", ".join(removed)
                        )
                if result3[0]:
                    return self._send_json({"code": 200, "msg": str(result3[1])})
                return self._send_json({"code": 400, "msg": str(result3[1])}, 400)
            finally:
                if not deferred:
                    finish()

        # 注意：插件名仅允许字母数字下划线连字符（禁止 . 防路径穿越，如 "..")
        m = re.fullmatch(r"/api/subplugins/([A-Za-z0-9_\-]+)/files", path)
        if m and method == "GET":
            mgr = plugin.subplugin_manager
            if mgr is None:
                return self._send_json({"code": 500, "msg": _t("webui.msg.subplugin_manager_unavailable")}, 500)
            plugins_base = mgr.plugins_dir.resolve()
            folder = (mgr.plugins_dir / m.group(1)).resolve()
            # 二次校验：folder 必须仍在 plugins_dir 之下（防符号链接绕过）
            try:
                folder.relative_to(plugins_base)
            except ValueError:
                return self._send_json({"code": 403, "msg": _t("webui.msg.forbidden_path")}, 403)
            if not folder.is_dir():
                return self._send_json({"code": 404, "msg": _t("webui.msg.subplugin_not_found")}, 404)
            files = []
            for p in sorted(folder.rglob("*")):
                # 不跟随符号链接，避免目录外文件名泄漏
                if p.is_symlink():
                    continue
                if p.is_file() and "__pycache__" not in p.parts:
                    rel = p.relative_to(folder).as_posix()
                    try:
                        stat = p.stat()
                    except OSError:
                        continue
                    files.append({
                        "path": rel,
                        "size": stat.st_size,
                        "editable": p.suffix.lower() in EDITABLE_SUFFIXES and stat.st_size <= 1024 * 1024,
                    })
            return self._send_json({"code": 200, "data": files})

        m = re.fullmatch(r"/api/subplugins/([A-Za-z0-9_\-]+)/file", path)
        if m:
            mgr = plugin.subplugin_manager
            if mgr is None:
                return self._send_json({"code": 500, "msg": _t("webui.msg.subplugin_manager_unavailable")}, 500)
            plugins_base = mgr.plugins_dir.resolve()
            folder = (mgr.plugins_dir / m.group(1)).resolve()
            try:
                folder.relative_to(plugins_base)
            except ValueError:
                return self._send_json({"code": 403, "msg": _t("webui.msg.forbidden_path")}, 403)
            rel = query.get("path", "")
            # 防路径穿越：用 relative_to 而非字符串前缀匹配
            if not rel:
                return self._send_json({"code": 403, "msg": _t("webui.msg.forbidden_path")}, 403)
            try:
                # %00 解码出的 NUL 会让 resolve() 抛 ValueError（embedded null
                # byte），必须捕获后按非法路径处理而不是 500
                target = (folder / rel).resolve()
            except (ValueError, OSError):
                return self._send_json({"code": 403, "msg": _t("webui.msg.forbidden_path")}, 403)
            try:
                target.relative_to(folder)
            except ValueError:
                return self._send_json({"code": 403, "msg": _t("webui.msg.forbidden_path")}, 403)
            if method == "GET":
                if not target.is_file():
                    return self._send_json({"code": 404, "msg": _t("webui.msg.file_not_exist")}, 404)
                if target.suffix.lower() not in EDITABLE_SUFFIXES:
                    return self._send_json({"code": 400, "msg": _t("webui.msg.file_type_unsupported")}, 400)
                try:
                    stat = target.stat()
                except OSError as e:
                    return self._send_json({"code": 400, "msg": _t("webui.msg.read_failed", error=e)}, 400)
                # 文件大小限制：禁止读取超过 1MB 的文本文件
                if stat.st_size > 1024 * 1024:
                    return self._send_json({"code": 400, "msg": _t("webui.msg.file_too_large_edit")}, 400)
                try:
                    content = target.read_text(encoding="utf-8")
                except (UnicodeDecodeError, OSError) as e:
                    return self._send_json({"code": 400, "msg": _t("webui.msg.read_failed", error=e)}, 400)
                return self._send_json({"code": 200, "data": {"path": rel, "content": content}})
            if method == "POST":
                body = self._read_body() or {}
                content = body.get("content")
                if not isinstance(content, str):
                    return self._send_json({"code": 400, "msg": _t("webui.msg.missing_content")}, 400)
                # 写入大小限制：禁止写入超过 1MB 的文本
                if len(content) > 1024 * 1024:
                    return self._send_json({"code": 400, "msg": _t("webui.msg.file_too_large_edit")}, 400)
                if target.suffix.lower() not in EDITABLE_SUFFIXES:
                    return self._send_json({"code": 400, "msg": _t("webui.msg.file_type_unsupported")}, 400)
                if target.suffix.lower() == ".json":
                    try:
                        json.loads(content)
                    except json.JSONDecodeError as e:
                        return self._send_json({"code": 400, "msg": _t("webui.msg.json_format_error", error=e)}, 400)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
                return self._send_json({"code": 200, "msg": _t("webui.msg.file_saved")})

        if method == "GET" and path in ("/api/custom_pages", "/api/plugins/custom-pages"):
            with self.webui._ext_lock:
                pages_snapshot = list(self.webui.custom_pages)
            return self._send_json({"code": 200, "data": pages_snapshot})

        # ── 聊天屏蔽：配置/词条读写 + 词库导入 ──
        chat_filter = getattr(plugin, "chat_filter", None)
        if method == "GET" and path == "/api/chat_filter":
            if chat_filter is None:
                return self._send_json({"code": 500, "msg": _t("webui.msg.module_unavailable")}, 500)
            return self._send_json({"code": 200, "data": chat_filter.snapshot()})
        if method == "PUT" and path == "/api/chat_filter":
            if chat_filter is None:
                return self._send_json({"code": 500, "msg": _t("webui.msg.module_unavailable")}, 500)
            payload = self._read_body()
            if not isinstance(payload, dict):
                return self._send_json({"code": 400, "msg": _t("webui.msg.invalid_body")}, 400)
            chat_filter.update(payload)
            return self._send_json({"code": 200, "msg": _t("webui.msg.chat_filter_saved")})
        if method == "POST" and path == "/api/chat_filter/import":
            if chat_filter is None:
                return self._send_json({"code": 500, "msg": _t("webui.msg.module_unavailable")}, 500)
            payload = self._read_body()
            bank = str((payload or {}).get("file") or "") if isinstance(payload, dict) else ""
            if not bank:
                return self._send_json({"code": 400, "msg": _t("webui.msg.chat_filter_no_file")}, 400)
            try:
                added = chat_filter.import_wordbank(bank)
            except FileNotFoundError:
                return self._send_json({"code": 404, "msg": _t("webui.msg.chat_filter_no_file")}, 404)
            except OSError as e:
                return self._send_json({"code": 500, "msg": _t("webui.msg.chat_filter_import_failed", error=e)}, 500)
            return self._send_json({
                "code": 200,
                "msg": _t("webui.msg.chat_filter_imported", count=added),
                "data": chat_filter.snapshot(),
            })
        if method == "POST" and path == "/api/chat_filter/import_text":
            # 一键导入第三方词库文本：前端读取 .txt 文件内容直接提交，
            # 兼容“每行一个”与“中英文逗号分隔”两种格式
            if chat_filter is None:
                return self._send_json({"code": 500, "msg": _t("webui.msg.module_unavailable")}, 500)
            payload = self._read_body()
            text = str((payload or {}).get("text") or "") if isinstance(payload, dict) else ""
            name = str((payload or {}).get("name") or "import.txt") if isinstance(payload, dict) else "import.txt"
            if not text.strip():
                return self._send_json({"code": 400, "msg": _t("webui.msg.chat_filter_no_file")}, 400)
            try:
                added = chat_filter.import_words_text(text, source=Path(name).name)
            except OSError as e:
                return self._send_json({"code": 500, "msg": _t("webui.msg.chat_filter_import_failed", error=e)}, 500)
            return self._send_json({
                "code": 200,
                "msg": _t("webui.msg.chat_filter_imported", count=added),
                "data": chat_filter.snapshot(),
            })

        if method == "GET" and path == "/api/logs":
            return self._send_json({"code": 200, "data": self.webui.log_buffer.cache})

        if method == "GET" and path == "/api/logs/stream":
            return self._sse_logs()

        return self._send_json({"code": 404, "msg": _t("webui.msg.api_not_found")}, 404)

    def _sse_logs(self) -> None:
        """SSE 实时日志流（线程安全 + 异常隔离 + 连接泄漏防护）

        关键修复：
        1. SSE 线程绝不能调用 End Stone 的底层 logger 或 Server API
           （replxx 控制台跨线程访问会导致服务端崩溃）。
        2. 客户端断开后向已关闭 socket 写入会触发 SIGPIPE，
           BDS 主进程未屏蔽 SIGPIPE 会被信号 -13 杀死。
           因此本线程启动时立刻屏蔽 SIGPIPE（仅作用于本线程），
           让写操作改为抛 BrokenPipeError 由 Python 兜底。
        3. 设置 socket 超时（30s）和最大连接时长（10min），
           防止慢客户端/网络中断导致的连接泄漏与线程堆积。
        """
        import queue
        import signal as _signal

        # 仅屏蔽本线程的 SIGPIPE（Linux/macOS 有效；Windows 无此信号）
        try:
            _signal.pthread_sigmask(_signal.SIG_BLOCK, {_signal.SIGPIPE})
        except (ValueError, AttributeError, OSError):
            pass

        # 设置 socket 超时，防止 send() 在客户端接收窗口满时无限阻塞
        try:
            self.connection.settimeout(30.0)
        except (AttributeError, OSError):
            pass

        q: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=200)
        stopped = self.webui._stop_event
        # 最大连接时长 10 分钟，防止客户端无限挂起
        deadline = time.monotonic() + 600.0

        def on_log(entry: dict[str, Any]) -> None:
            try:
                q.put_nowait(entry)
            except queue.Full:
                # 慢客户端只丢弃最旧一条，避免日志线程阻塞或无限占用内存。
                try:
                    q.get_nowait()
                    q.put_nowait(entry)
                except (queue.Empty, queue.Full):
                    pass

        # 订阅前先取一个稳定的服务器句柄，避免后续主线程关闭时引发悬空访问。
        if not self.webui.log_buffer.subscribe(on_log):
            return self._send_json(
                {"code": 503, "msg": _t("webui.msg.sse_limit_reached")},
                503,
            )

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
                # 连接时长超限：主动关闭，释放线程资源
                if time.monotonic() >= deadline:
                    return
                try:
                    entry = q.get(timeout=1.0)
                    payload = f"data: {json.dumps(entry, ensure_ascii=False)}\n\n"
                except queue.Empty:
                    payload = ": keepalive\n\n"
                if not self._safe_write(payload.encode("utf-8")):
                    return  # 客户端断开，退出 SSE 循环
                try:
                    self.wfile.flush()
                except self._NETWORK_ERRORS:
                    return
        except (BrokenPipeError, ConnectionResetError, OSError, ValueError):
            # 客户端关闭连接 / 主线程关闭服务 / wfile 已关闭，均吞掉
            pass
        except Exception:
            # 兜底：任何意外异常都不得把 SSE 线程的异常传播到主线程
            pass
        finally:
            try:
                self.webui.log_buffer.unsubscribe(on_log)
            except Exception:
                pass
            self.close_connection = True
