"""插件市场与框架更新 API（多为异步任务模式）。"""

from __future__ import annotations

import copy
import re
from typing import Any, Callable

from ...i18n import t as _t
from .base import RouteMixin


class MarketApi(RouteMixin):

    def api_market_config(self, m: re.Match, query: dict[str, str]) -> None:
        client = getattr(self.plugin, "marketplace", None)
        cfg = self.plugin.config_manager.data.get("marketplace", {}) if self.plugin.config_manager else {}
        self._send_json({"code": 200, "data": {
            "enabled": bool(client and client.enabled),
            "configured": bool(cfg.get("api_url")),
            "api_url": str(cfg.get("api_url") or ""),
            "allow_http": bool(cfg.get("allow_http", False)),
        }})

    def api_market_installed(self, m: re.Match, query: dict[str, str]) -> None:
        # 已装映射：市场 ID → 本地名/版本/可用更新，供市场列表渲染
        # 已安装徽章与更新按钮（无网络请求，纯本地清单 + 缓存快照）
        client = getattr(self.plugin, "marketplace", None)
        updates = client.cached_updates() if client is not None else {}
        mapping: dict[str, dict[str, Any]] = {}
        mgr = self.plugin.subplugin_manager
        if mgr is not None:
            with mgr._lock:
                for name, sp in mgr.subplugins.items():
                    manifest = sp.manifest if isinstance(sp.manifest, dict) else {}
                    origin = manifest.get("_market", {})
                    if not isinstance(origin, dict) or origin.get("source") != "marketplace":
                        continue
                    market_id = str(origin.get("id") or "")
                    if not market_id:
                        continue
                    snapshot = updates.get(name, {})
                    mapping[market_id] = {
                        "name": name,
                        "version": str(manifest.get("version") or ""),
                        "update_available": bool(snapshot.get("available")),
                        "latest_version": str(snapshot.get("latest_version") or ""),
                    }
        self._send_json({"code": 200, "data": mapping})

    def api_market_plugins(self, m: re.Match, query: dict[str, str]) -> None:
        client = self.require_market()
        if client is None:
            return
        try:
            page = max(1, int(query.get("page", "1")))
            limit = max(1, min(100, int(query.get("limit", "24"))))
            data = client.browse(
                search=query.get("q", ""), category=query.get("category", ""),
                sort=query.get("sort", "score"), page=page, limit=limit,
            )
        except Exception as exc:  # noqa: BLE001
            self._send_json({"code": 400, "msg": str(exc)}, 400)
            return
        self._send_json({"code": 200, "data": data})

    def api_market_plugin_detail(self, m: re.Match, query: dict[str, str]) -> None:
        client = self.require_market()
        if client is None:
            return
        try:
            data = client.plugin_detail(m.group(1))
        except Exception as exc:  # noqa: BLE001
            self._send_json({"code": 400, "msg": str(exc)}, 400)
            return
        self._send_json({"code": 200, "data": data})

    def api_market_cover(self, m: re.Match, query: dict[str, str]) -> None:
        # 封面代理：<img> 带不了鉴权头，直连有混合内容/防盗链问题
        client = self.require_market()
        if client is None:
            return
        try:
            ctype, raw = client.fetch_cover(query.get("url", ""))
        except Exception as exc:  # noqa: BLE001
            self._send_json({"code": 400, "msg": str(exc)}, 400)
            return
        # SVG 可内嵌脚本构成 XSS，强制拒绝
        if "svg" in ctype.lower():
            self.api_error(403, "webui.msg.cover_type_forbidden")
            return
        if self._safe_send_headers(200, [
            ("Content-Type", ctype),
            ("Content-Length", str(len(raw))),
            ("Cache-Control", "private, max-age=3600"),
            ("Referrer-Policy", "no-referrer"),
            ("X-Content-Type-Options", "nosniff"),
        ]):
            self._safe_write(raw)

    def api_market_report(self, m: re.Match, query: dict[str, str]) -> None:
        client = self.require_market()
        if client is None:
            return
        body = self.read_json_dict()
        if body is None:
            return
        try:
            result = client.report_plugin(
                str(body.get("id") or ""), str(body.get("reason") or ""), str(body.get("contact") or "")
            )
        except Exception as exc:  # noqa: BLE001
            self._send_json({"code": 400, "msg": str(exc)}, 400)
            return
        self._send_json({"code": 201, "data": result}, 201)

    def api_market_like(self, m: re.Match, query: dict[str, str]) -> None:
        client = self.require_market()
        if client is None:
            return
        body = self.read_json_dict()
        if body is None:
            return
        market_id = str(body.get("id") or "").strip()
        if not market_id:
            self.api_error(400, "webui.msg.invalid_market_id")
            return
        try:
            result = client.like_plugin(market_id, bool(body.get("liked", True)))
        except Exception as exc:  # noqa: BLE001
            self._send_json({"code": 400, "msg": str(exc)}, 400)
            return
        self._send_json({"code": 200, "data": result})

    def api_market_check(self, m: re.Match, query: dict[str, str]) -> None:
        client = self.require_market()
        if client is None:
            return

        def _run(log: Callable[[str], None], progress: Callable[[int, str], None]) -> dict[str, Any]:
            log(_t("task_log.checking_updates"))
            progress(30, _t("task_log.checking_updates"))
            result = {"updates": client.check_subplugin_updates(force=True)}
            progress(100, _t("task_log.done"))
            return result

        self.send_task_started(self.start_market_task_or_429("check_updates", _run))

    def api_market_update_all(self, m: re.Match, query: dict[str, str]) -> None:
        client = self.require_market()
        if client is None:
            return

        def _run(log: Callable[[str], None], progress: Callable[[int, str], None]) -> dict[str, Any]:
            log(_t("task_log.checking_updates"))
            return client.update_all(log=log, progress=progress)

        self.send_task_started(self.start_market_task_or_429("update_all", _run))

    def api_market_install(self, m: re.Match, query: dict[str, str]) -> None:
        body = self.read_json_dict()
        if body is None:
            return
        market_id = str(body.get("id", "")).strip()
        version = str(body.get("version", "")).strip()
        client = self.require_market()
        if client is None:
            return
        if not market_id:
            self.api_error(400, "webui.msg.invalid_market_id")
            return

        def _run(log: Callable[[str], None], progress: Callable[[int, str], None]) -> dict[str, Any]:
            log(_t("task_log.installing_plugin", id=market_id))
            progress(20, _t("task_log.downloading"))
            result = client.install(market_id, version, upgrade_dependencies=True, log=log, progress=progress)
            progress(100, _t("task_log.done"))
            return result

        self.send_task_started(self.start_market_task_or_429("install", _run))

    def api_market_task(self, m: re.Match, query: dict[str, str]) -> None:
        # 锁内深拷贝：result 嵌套结构可能与后台线程共享引用
        with self.webui._market_tasks_lock:
            task = self.webui._market_tasks.get(m.group(1))
            if task is None:
                self.api_error(404, "webui.msg.market_task_not_found")
                return
            snapshot = copy.deepcopy({k: v for k, v in task.items() if k != "start_time"})
        self._send_json({"code": 200, "data": snapshot})

    # ---- 子插件市场更新（挂 /api/subplugins/ 前缀，属市场域逻辑）----

    def api_subplugin_market_update(self, m: re.Match, query: dict[str, str]) -> None:
        client = self.require_market()
        if client is None:
            return
        name = m.group(1)
        body = self._read_body() or {}
        requested_version = str(body.get("version", "")).strip() if isinstance(body, dict) else ""

        def _run(log: Callable[[str], None], progress: Callable[[int, str], None]) -> dict[str, Any]:
            log(_t("task_log.updating_plugin", name=name))
            progress(20, _t("task_log.downloading"))
            result = client.update(name, requested_version, update_dependencies=True, log=log, progress=progress)
            progress(100, _t("task_log.done"))
            return result

        self.send_task_started(self.start_market_task_or_429("plugin_update", _run))

    def api_subplugin_update_deps(self, m: re.Match, query: dict[str, str]) -> None:
        client = self.require_market()
        if client is None:
            return
        name = m.group(1)

        def _run(log: Callable[[str], None], progress: Callable[[int, str], None]) -> dict[str, Any]:
            log(_t("task_log.updating_deps", name=name))
            progress(30, _t("task_log.installing_deps"))
            result = client.update_dependencies(name, log=log, progress=progress)
            progress(100, _t("task_log.done"))
            return result

        self.send_task_started(self.start_market_task_or_429("dependencies_update", _run))

    def api_subplugin_install_requirements(self, m: re.Match, query: dict[str, str]) -> None:
        client = self.require_market()
        if client is None:
            return
        mgr = self.require_subplugin_manager()
        if mgr is None:
            return
        name = m.group(1)
        with mgr._lock:
            if mgr.subplugins.get(name) is None:
                self.api_error(404, "webui.msg.subplugin_not_found")
                return

        def _run(log: Callable[[str], None], progress: Callable[[int, str], None]) -> dict[str, Any]:
            log(_t("task_log.installing_requirements", name=name))
            progress(20, _t("task_log.installing_deps"))
            result = client.install_plugin_requirements(name, log=log, progress=progress)
            progress(100, _t("task_log.done"))
            return result

        self.send_task_started(self.start_market_task_or_429("requirements_install", _run))

    # ---- 全服热重载与框架更新 ----

    def api_server_reload(self, m: re.Match, query: dict[str, str]) -> None:
        # 全服热重载（等同 /reload）：Endstone 依赖落盘后加载新插件的入口。
        # 复用框架更新的两阶段 ghost 调度，期间 WebUI 短暂下线由前端轮询恢复
        client = getattr(self.plugin, "marketplace", None)
        if client is None:
            self.api_error(500, "webui.msg.reload_channel_unavailable")
            return

        def _run(log: Callable[[str], None], progress: Callable[[int, str], None]) -> dict[str, Any]:
            log(_t("task_log.server_reloading"))
            progress(10, _t("task_log.waiting_tasks"))
            result = client.safe_server_reload(log=log)
            progress(100, _t("task_log.done"))
            return result

        self.send_task_started(self.start_market_task_or_429("server_reload", _run))

    def _require_update_client(self) -> Any:
        """框架更新域前置：开关 + 客户端；失败时已发送响应。"""
        if not self.updates_enabled():
            return None
        client = getattr(self.plugin, "marketplace", None)
        if client is None:
            self.api_error(500, "webui.msg.updates_unavailable")
            return None
        return client

    def api_updates_stage(self, m: re.Match, query: dict[str, str]) -> None:
        client = self._require_update_client()
        if client is None:
            return

        def _run(log: Callable[[str], None], progress: Callable[[int, str], None]) -> dict[str, Any]:
            log(_t("task_log.staging_framework"))
            progress(20, _t("task_log.downloading"))
            result = client.stage_framework_update(log=log, progress=progress)
            progress(100, _t("task_log.done"))
            return result

        self.send_task_started(self.start_market_task_or_429("framework_update", _run))

    def api_updates_apply(self, m: re.Match, query: dict[str, str]) -> None:
        client = self._require_update_client()
        if client is None:
            return

        def _run(log: Callable[[str], None], progress: Callable[[int, str], None]) -> dict[str, Any]:
            log(_t("task_log.applying_framework"))
            progress(20, _t("task_log.downloading"))
            # 只暂存 wheel 绝不重载：后台线程 Server.reload() 会崩服
            result = client.apply_framework_update(log=log, progress=progress)
            log(_t("task_log.framework_staged_hint"))
            progress(100, _t("task_log.done"))
            return result

        self.send_task_started(self.start_market_task_or_429("framework_apply", _run))

    def api_updates_reload(self, m: re.Match, query: dict[str, str]) -> None:
        client = self._require_update_client()
        if client is None:
            return
        try:
            staged = client.staged_framework_update()
        except Exception:  # noqa: BLE001
            staged = None
        if staged is None:
            self.api_error(400, "task_log.framework_reload_no_staged")
            return

        def _run(log: Callable[[str], None], progress: Callable[[int, str], None]) -> dict[str, Any]:
            log(_t("task_log.framework_reloading", version=str(staged.get("to_version") or "?")))
            progress(10, _t("task_log.waiting_tasks"))
            # 两阶段触发 reload；期间 WebUI 实例重建，前端轮询 /api/health 自刷
            result = client.safe_framework_reload(log=log)
            progress(100, _t("task_log.done"))
            return result

        self.send_task_started(self.start_market_task_or_429("framework_reload", _run))

    def api_updates_check(self, m: re.Match, query: dict[str, str]) -> None:
        update_cfg = self.plugin.config_manager.data.get("updates", {}) if self.plugin.config_manager else {}
        if not bool(update_cfg.get("enable", True)):
            self._send_json({"code": 200, "data": {"configured": False, "available": False}})
            return
        client = getattr(self.plugin, "marketplace", None)
        if client is None:
            self.api_error(500, "webui.msg.updates_unavailable")
            return
        try:
            data = client.framework_update_info()
            try:
                staged = client.staged_framework_update()
            except Exception:  # noqa: BLE001
                staged = None
            data["staged"] = staged or None
        except Exception as exc:  # noqa: BLE001
            self._send_json({"code": 400, "msg": str(exc)}, 400)
            return
        self._send_json({"code": 200, "data": data})
