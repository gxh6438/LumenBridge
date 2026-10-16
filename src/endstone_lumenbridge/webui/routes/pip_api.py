"""pip 包管理 API：安装、卸载、列表与异步任务查询。"""

from __future__ import annotations

import re
import time
from typing import Any

from .base import RouteMixin

# pip 选项 / URL / 路径 / 空白注入防护
_FORBIDDEN_PKG_CHARS = ("git+", "://", "/", "\\")


def _valid_package(p: Any) -> bool:
    if not isinstance(p, str) or not p or not p.strip():
        return False
    if p.startswith("-") or p != p.strip():
        return False
    return not any(frag in p for frag in _FORBIDDEN_PKG_CHARS) and not any(ch.isspace() for ch in p)


class PipApi(RouteMixin):

    def api_pip_install(self, m: re.Match, query: dict[str, str]) -> None:
        body = self.read_json_dict()
        if body is None:
            return
        packages = body.get("packages", [])
        if not packages or not isinstance(packages, list):
            self.api_error(400, "pip.no_packages_specified")
            return
        if not all(_valid_package(p) for p in packages):
            self.api_error(400, "pip.invalid_package_arg", packages="...")
            return
        mgr = self.require_pip_manager()
        if mgr is None:
            return
        task_id = self.webui._start_pip_task(mgr, packages, "install")
        if task_id is None:
            self.api_error(429, "webui.msg.too_many_tasks")
            return
        self._send_json({"code": 200, "data": {"task_id": task_id}})

    def api_pip_uninstall(self, m: re.Match, query: dict[str, str]) -> None:
        body = self.read_json_dict()
        if body is None:
            return
        raw_pkg = body.get("package", "")
        if not isinstance(raw_pkg, str):
            self.api_error(400, "pip.no_packages_specified")
            return
        package = raw_pkg.strip()
        if not package:
            self.api_error(400, "pip.no_packages_specified")
            return
        mgr = self.require_pip_manager()
        if mgr is None:
            return
        task_id = self.webui._start_pip_task(mgr, [package], "uninstall")
        if task_id is None:
            self.api_error(429, "webui.msg.too_many_tasks")
            return
        self._send_json({"code": 200, "data": {"task_id": task_id}})

    def api_pip_list(self, m: re.Match, query: dict[str, str]) -> None:
        mgr = self.require_pip_manager()
        if mgr is None:
            return
        # 10s 缓存防并发 pip 子进程
        now = time.time()
        with self.webui._pip_list_lock:
            cached = self.webui._pip_list_cache
            if cached is not None and now - cached[0] < 10.0:
                pkgs = cached[1]
            else:
                pkgs = mgr.list_packages()
                self.webui._pip_list_cache = (now, pkgs)
        from ...pip_manager import PROTECTED_PACKAGES, _normalize

        protected_norm = {_normalize(p) for p in PROTECTED_PACKAGES}
        result = []
        for p in pkgs:
            item = dict(p)
            item["protected"] = _normalize(str(p.get("name", ""))) in protected_norm
            result.append(item)
        self._send_json({"code": 200, "data": result})

    def api_pip_config(self, m: re.Match, query: dict[str, str]) -> None:
        cfg = self.plugin.config_manager.data.get("pip", {}) if self.plugin.config_manager else {}
        self._send_json({"code": 200, "data": {
            "enable": cfg.get("enable", True),
            "index_url": cfg.get("index_url", ""),
            "timeout": cfg.get("timeout", 300),
        }})

    def api_pip_task(self, m: re.Match, query: dict[str, str]) -> None:
        task_id = m.group(1)
        # 锁内拷贝，避免 on_log 并发 append
        with self.webui._pip_tasks_lock:
            task = self.webui._pip_tasks.get(task_id)
            if not task:
                self.api_error(404, "pip.task_not_found")
                return
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
        self._send_json({"code": 200, "data": snapshot})
