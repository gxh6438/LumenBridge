"""API 路由公共基座：错误响应、body 解析、前置检查、主线程操作与 URL 下载校验。

本模块的 RouteMixin 最终由 ``server._RequestHandler`` 多重继承混入，
self 上的 HTTP 层能力（``_send_json`` / ``_read_body`` 等）由宿主提供。
"""

from __future__ import annotations

import hashlib
import hmac
import re
import threading
import urllib.parse
from typing import TYPE_CHECKING, Any, Callable

from ...i18n import t as _t

if TYPE_CHECKING:
    from ..server import WebUIServer

_SHA256_RE = re.compile(r"[a-f0-9]{64}")


class InstallUrlError(RuntimeError):
    """URL 安装下载失败：msg_key 为 i18n 键，error 保留底层异常供审计日志。"""

    def __init__(self, msg_key: str, error: Any = None) -> None:
        super().__init__(msg_key)
        self.msg_key = msg_key
        self.error = error


class RouteMixin:
    """路由处理器共享辅助。所有 API mixin 继承本类。"""

    webui: "WebUIServer"

    # ---- 响应辅助 ----

    def api_error(self, code: int, key: str, status: int | None = None, **kwargs: Any) -> None:
        self._send_json({"code": code, "msg": _t(key, **kwargs)}, status or code)

    def read_json_dict(self) -> dict[str, Any] | None:
        """读取 JSON 对象 body；非法时已发送 400 并返回 None。"""
        body = self._read_body() or {}
        if not isinstance(body, dict):
            self.api_error(400, "webui.msg.invalid_body")
            return None
        return body

    def client_ip(self) -> str:
        return str(self.client_address[0]) if self.client_address else "unknown"

    # ---- 前置检查（失败时已发送响应，返回 None / False）----

    def require_market(self) -> Any:
        client = getattr(self.plugin, "marketplace", None)
        if client is None or not client.enabled:
            self.api_error(403, "webui.msg.market_disabled")
            return None
        return client

    def require_pip_manager(self) -> Any:
        mgr = self.webui._get_pip_manager_for_plugin(self.plugin)
        if mgr is None:
            self.api_error(500, "pip.manager_unavailable")
            return None
        if not mgr.enable:
            self.api_error(403, "pip.disabled")
            return None
        return mgr

    def require_subplugin_manager(self) -> Any:
        mgr = self.plugin.subplugin_manager
        if mgr is None:
            self.api_error(500, "webui.msg.subplugin_manager_unavailable")
            return None
        return mgr

    def require_connections(self) -> Any:
        connections = getattr(self.plugin, "connections", None)
        if connections is None:
            self.api_error(500, "connections.unavailable")
            return None
        return connections

    def updates_enabled(self) -> bool:
        """框架更新开关检查；关闭时已发送 403。"""
        update_cfg = self.plugin.config_manager.data.get("updates", {}) if self.plugin.config_manager else {}
        if not bool(update_cfg.get("enable", True)):
            self.api_error(403, "webui.msg.updates_disabled")
            return False
        return True

    # ---- 异步任务启动（任务表满时已发送 429）----

    def start_market_task_or_429(
        self, action: str,
        runner: Callable[[Callable[[str], None], Callable[[int, str], None]], dict[str, Any]],
    ) -> str | None:
        task_id = self.webui._start_market_task(action, runner)
        if task_id is None:
            self.api_error(429, "webui.msg.too_many_tasks")
            return None
        return task_id

    def send_task_started(self, task_id: str | None) -> None:
        if task_id is not None:
            self._send_json({"code": 200, "data": {"task_id": task_id}})

    # ---- 主线程长操作（互斥 + 调度 + 超时兜底）----

    def run_main_op(
        self, fn: Callable[[], None], timeout: float,
        on_never_ran: Callable[[], None] | None = None,
    ) -> tuple[str, str]:
        """在服务器主线程执行 fn；返回 (状态, 错误信息)。

        状态：ok / busy / timeout / schedule_error。
        fn 内部的业务异常由调用方通过闭包容器自行捕获。
        on_never_ran：fn 确定未被执行（调度失败）时的补偿回调，如清理临时文件。
        """
        done = threading.Event()
        finish, defer = self._acquire_main_op(done)
        if finish is None:
            return "busy", ""
        state, err = "ok", ""

        def wrapped() -> None:
            try:
                fn()
            finally:
                done.set()

        try:
            try:
                self.plugin.run_on_main(wrapped)
            except Exception as e:  # noqa: BLE001
                if on_never_ran is not None:
                    on_never_ran()
                state, err = "schedule_error", str(e)
            else:
                if not done.wait(timeout=timeout):
                    # 锁交由守护线程在 fn 完成后释放，避免提前放行并发长操作
                    defer()
                    state = "timeout"
        finally:
            if state != "timeout":
                finish()
        return state, err

    def main_op_failure(self, state: str, err: str, *, timeout_key: str, error_key: str) -> bool:
        """run_main_op 失败状态统一响应；已发送响应返回 True。"""
        if state == "busy":
            self.api_error(409, "webui.msg.main_op_busy")
            return True
        if state == "timeout":
            self.api_error(504, timeout_key)
            return True
        if state == "schedule_error":
            self.api_error(500, error_key, error=err)
            return True
        return False

    # ---- URL 直装下载统一校验 ----

    def install_url_allow_hosts(self) -> set[str]:
        try:
            webui_conf = self.plugin.config_manager.data.get("webui", {}) if self.plugin.config_manager else {}
        except Exception:  # noqa: BLE001 - 配置读取失败按无白名单处理
            webui_conf = {}
        allow_raw = webui_conf.get("install_url_allow_hosts", []) if isinstance(webui_conf, dict) else []
        return {
            str(h).strip().lower() for h in (allow_raw if isinstance(allow_raw, (list, tuple, set)) else [])
            if str(h).strip()
        }

    def validate_install_url(self, url: str, sha256: str) -> tuple[str, str] | None:
        """URL 安装前置校验：协议 / 主机 / sha256 格式 / 白名单。

        失败时已发送响应并返回 None；成功返回 (url, sha256)。
        """
        url = str(url or "").strip()
        if not url.startswith(("http://", "https://")):
            self.api_error(400, "webui.msg.invalid_url")
            return None
        parsed = urllib.parse.urlparse(url)
        if not parsed.hostname:
            self.api_error(400, "webui.msg.invalid_hostname")
            return None
        sha256 = str(sha256 or "").strip().lower()
        if sha256 and not _SHA256_RE.fullmatch(sha256):
            self.api_error(400, "webui.msg.invalid_sha256")
            return None
        allow_hosts = self.install_url_allow_hosts()
        if allow_hosts and (parsed.hostname or "").lower() not in allow_hosts:
            self.api_error(403, "webui.msg.host_not_allowed")
            return None
        return url, sha256

    def download_install_bytes(
        self, url: str, sha256: str, *, mismatch_key: str, on_progress: Callable[[int, int], None] | None = None,
    ) -> bytes:
        """下载 + SHA-256 比对；失败抛 InstallUrlError（请求态与任务态共用）。"""
        from .. import server as _server  # 延迟解析，保持对 server._fetch_url_bytes 的 patch 生效

        try:
            data = _server._fetch_url_bytes(
                url, _server._MAX_UPLOAD_BYTES, timeout=30, on_progress=on_progress,
            )
        except Exception as e:  # noqa: BLE001
            raise InstallUrlError("webui.msg.download_failed", e) from e
        if sha256 and not hmac.compare_digest(hashlib.sha256(data).hexdigest(), sha256):
            raise InstallUrlError(mismatch_key)
        return data

    def fetch_url_payload(
        self, url: str, sha256: str, *, mismatch_key: str, warn_no_sha: bool = False,
    ) -> bytes | None:
        """URL 下载统一校验：协议 / 主机 / sha256 格式 / 白名单 / 下载 / 哈希比对。

        失败时已发送响应并返回 None。
        """
        checked = self.validate_install_url(url, sha256)
        if checked is None:
            return None
        url, sha256 = checked
        client_ip = self.client_ip()
        if warn_no_sha and not sha256:
            self.webui.logger.warning(_t("webui.log.url_install_no_sha256", url=url, ip=client_ip))
        try:
            return self.download_install_bytes(url, sha256, mismatch_key=mismatch_key)
        except InstallUrlError as e:
            if e.msg_key == "webui.msg.download_failed":
                cause = e.error.__cause__ if e.error is not None else None
                self.webui.logger.warning(
                    _t("webui.log.url_download_failed", url=url, ip=client_ip, error=cause or repr(e.error))
                )
            else:
                self.webui.logger.warning(_t("webui.log.url_sha256_failed", url=url, ip=client_ip))
            self.api_error(400, e.msg_key)
            return None
