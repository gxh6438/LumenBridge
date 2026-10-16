"""子插件管理 API：列表、启停、重载、安装（上传/URL）、卸载、文件编辑与依赖补全。"""

from __future__ import annotations

import json
import os
import re
import tempfile
import urllib.parse
from pathlib import Path
from typing import Any, Callable

from ...i18n import t as _t
from ...marketplace import MarketplaceError
from ...subplugin.requires import parse_requires_from_manifest, check_endstone_requirements
from .base import InstallUrlError, RouteMixin

# 允许在线编辑的文件后缀（server.py 重导出保持兼容）
EDITABLE_SUFFIXES = {".json", ".py", ".txt", ".md", ".yml", ".yaml", ".cfg", ".ini", ".html", ".css", ".js"}


class SubpluginsApi(RouteMixin):

    def api_subplugins_list(self, m: re.Match, query: dict[str, str]) -> None:
        plugin = self.plugin
        mgr = plugin.subplugin_manager
        market_client = getattr(plugin, "marketplace", None)
        market_updates = market_client.cached_updates() if market_client else {}
        data = []
        if mgr:
            # 锁内转纯 dict，避免锁外被并发改写
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
        self._send_json({"code": 200, "data": data})

    def api_subplugin_toggle(self, m: re.Match, query: dict[str, str]) -> None:
        mgr = self.plugin.subplugin_manager
        body = self.read_json_dict()
        if body is None:
            return
        enable = bool(body.get("enable", True))
        if mgr and mgr.set_enabled(m.group(1), enable):
            self._send_json({"code": 200, "msg": _t(
                "webui.msg.subplugin_toggled",
                state=(_t("common.enabled") if enable else _t("common.disabled")),
            )})
            return
        self.api_error(404, "webui.msg.subplugin_not_found")

    def api_subplugins_reload_all(self, m: re.Match, query: dict[str, str]) -> None:
        count = [0]
        err = [""]

        def do_reload() -> None:
            try:
                if self.plugin.subplugin_manager:
                    count[0] = self.plugin.subplugin_manager.reload_all()
            except Exception as e:  # noqa: BLE001
                err[0] = str(e)

        state, serr = self.run_main_op(do_reload, 10.0)
        if self.main_op_failure(
            state, serr,
            timeout_key="webui.msg.reload_timeout",
            error_key="webui.msg.reload_failed",
        ):
            return
        if err[0]:
            self.api_error(500, "webui.msg.reload_failed", error=err[0])
            return
        self._send_json({"code": 200, "msg": _t("webui.msg.subplugin_reload_success", count=count[0])})

    def api_subplugin_reload(self, m: re.Match, query: dict[str, str]) -> None:
        name = m.group(1)
        if not self.plugin.subplugin_manager:
            self.api_error(500, "webui.msg.subplugin_not_found")
            return
        ok = [False]
        err = [""]

        def do_reload_one() -> None:
            try:
                ok[0] = self.plugin.subplugin_manager.reload_one(name)
            except Exception as e:  # noqa: BLE001
                err[0] = str(e)

        state, serr = self.run_main_op(do_reload_one, 15.0)
        if self.main_op_failure(
            state, serr,
            timeout_key="webui.msg.reload_timeout",
            error_key="webui.msg.reload_failed",
        ):
            return
        if err[0] or not ok[0]:
            self.api_error(500 if err[0] else 400, "webui.msg.subplugin_reload_one_failed", name=name)
            return
        self._send_json({"code": 200, "msg": _t("webui.msg.subplugin_reload_one_success", name=name)})

    def api_subplugin_install_deps(self, m: re.Match, query: dict[str, str]) -> None:
        name = m.group(1)
        mgr = self.require_subplugin_manager()
        if mgr is None:
            return
        with mgr._lock:
            sp = mgr.subplugins.get(name)
            deps = list(sp.manifest.get("dependencies", []) or []) if sp else []
        if not sp:
            self.api_error(404, "webui.msg.subplugin_not_found")
            return
        if not deps:
            self.api_error(400, "pip.no_declared_deps", name=name)
            return
        pip_mgr = self.require_pip_manager()
        if pip_mgr is None:
            return
        task_id = self.webui._start_pip_task(
            pip_mgr, deps, "install_deps", subplugin_name=name, reload_after_install=True
        )
        if task_id is None:
            self.api_error(429, "webui.msg.too_many_tasks")
            return
        self._send_json({"code": 200, "data": {"task_id": task_id}})

    def api_subplugin_deps_sources(self, m: re.Match, query: dict[str, str]) -> None:
        # 依赖补全方式探测：区分子插件依赖（市场/链接/上传）与
        # Endstone 插件依赖（链接/上传，市场不售 Endstone 插件）
        mgr = self.require_subplugin_manager()
        if mgr is None:
            return
        name = m.group(1)
        from ...subplugin.loader import parse_requirements_links

        with mgr._lock:
            sp = mgr.subplugins.get(name)
            manifest = dict(sp.manifest) if sp is not None else None
        if manifest is None:
            self.api_error(404, "webui.msg.subplugin_not_found")
            return
        declaration = parse_requires_from_manifest(manifest)
        try:
            links = parse_requirements_links(mgr.plugins_dir / name)
        except Exception:  # noqa: BLE001 - 解析失败按无链接处理
            links = {"subplugins": {}, "endstone": {}}
        market_client = getattr(self.plugin, "marketplace", None)
        market_enabled = bool(market_client is not None and market_client.enabled)

        subplugin_deps: list[dict[str, Any]] = []
        with mgr._lock:
            for req in declaration.subplugins:
                if req.name == name:
                    continue
                dep = mgr.subplugins.get(req.name)
                local_version = str(dep.manifest.get("version") or "") if dep is not None else ""
                if dep is not None and dep.loaded and req.satisfied_by(local_version):
                    continue
                in_market = False
                if market_enabled:
                    try:
                        in_market = market_client._find_market_plugin_by_manifest_name(req.name) is not None
                    except Exception:  # noqa: BLE001 - 市场查询失败按未命中处理
                        in_market = False
                subplugin_deps.append({
                    "name": req.name,
                    "display": req.display(),
                    "installed": dep is not None,
                    "loaded": bool(dep is not None and dep.loaded),
                    "local_version": local_version,
                    "in_market": in_market,
                    "url": links["subplugins"].get(req.name.lower(), ""),
                })

        endstone_deps: list[dict[str, Any]] = []
        unverified = False
        if declaration.endstone:
            try:
                installed_es = mgr._installed_endstone_plugins()
            except Exception:  # noqa: BLE001 - 服务器 API 不可用时无法核实，仍列出全部声明
                installed_es = None
            if installed_es is None:
                unverified = True
                unmet_es = [(req, "") for req in declaration.endstone]
            else:
                unmet_es = check_endstone_requirements(declaration.endstone, installed_es)
            for req, actual in unmet_es:
                endstone_deps.append({
                    "name": req.name,
                    "display": req.display(),
                    "local_version": actual,
                    "url": links["endstone"].get(req.name.lower(), ""),
                })

        self._send_json({"code": 200, "data": {
            "name": name,
            "market_enabled": market_enabled,
            "subplugins": subplugin_deps,
            "endstone": endstone_deps,
            "unverified": unverified,
        }})

    # ---- ZIP 安装（上传 / URL）：任务模式，进度窗 + 依赖自动补全 ----

    def _install_client(self) -> Any:
        """安装管线客户端：市场客户端承载全部安装流程，缺失时构造临时实例。"""
        client = getattr(self.plugin, "marketplace", None)
        if client is None:
            from ...marketplace import MarketplaceClient

            client = MarketplaceClient(self.plugin)
        return client

    def _start_zip_install_task(self, action: str, tmp_zip: str, download_job: Any = None) -> None:
        """启动 ZIP 安装任务并响应 task_id。

        download_job(log, progress) -> bytes：URL 安装的下载阶段（含进度），
        上传安装为 None。任务失败时 tmp_zip 由本方法清理。
        """
        client = self._install_client()

        def _run(log: Callable[[str], None], progress: Callable[[int, str], None]) -> dict[str, Any]:
            try:
                if download_job is not None:
                    log(_t("task_log.downloading_url"))
                    progress(10, _t("task_log.downloading_url"))

                    def _dl_progress(loaded: int, total: int) -> None:
                        if total > 0:
                            progress(10 + int(loaded / total * 45), f"{loaded // 1024}KB / {total // 1024}KB")

                    data = download_job(log, _dl_progress)
                    with open(tmp_zip, "wb") as f:
                        f.write(data)
                result = client.install_subplugin_zip(tmp_zip, log=log, progress=progress)
                progress(100, _t("task_log.done"))
                return result
            finally:
                try:
                    os.unlink(tmp_zip)
                except OSError:
                    pass

        task_id = self.start_market_task_or_429(action, _run)
        if task_id is None:
            # 429 已发送：任务不会执行，此处清理临时文件
            try:
                os.unlink(tmp_zip)
            except OSError:
                pass
            return
        self.send_task_started(task_id)

    def api_subplugin_install_upload(self, m: re.Match, query: dict[str, str]) -> None:
        file_info = self._read_multipart_file()
        if not file_info:
            self.api_error(400, "webui.msg.no_file_received")
            return
        filename, content = file_info
        if not filename.lower().endswith(".zip"):
            self.api_error(400, "webui.msg.zip_only")
            return
        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tf:
            tf.write(content)
            tmp_zip = tf.name
        self._start_zip_install_task("install_upload", tmp_zip)

    def api_subplugin_install_url(self, m: re.Match, query: dict[str, str]) -> None:
        body = self.read_json_dict()
        if body is None:
            return
        url = str(body.get("url", "")).strip()
        sha256 = str(body.get("sha256", "") or "")
        checked = self.validate_install_url(url, sha256)
        if checked is None:
            return
        url, sha256 = checked
        if not sha256:
            self.webui.logger.warning(_t("webui.log.url_install_no_sha256", url=url, ip=self.client_ip()))
        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tf:
            tmp_zip = tf.name

        def _download(log: Any, progress: Any) -> bytes:
            try:
                return self.download_install_bytes(
                    url, sha256, mismatch_key="webui.msg.sha256_mismatch_install", on_progress=progress,
                )
            except InstallUrlError as e:
                raise MarketplaceError(str(_t(e.msg_key))) from e

        self._start_zip_install_task("install_url", tmp_zip, download_job=_download)

    # ---- Endstone 插件依赖落盘 ----

    def _save_endstone_dep(self, filename: str, data: bytes) -> tuple[dict[str, Any], int] | None:
        """Endstone 插件包落盘到服务器 plugins/ 目录（BDS 根，非子插件目录）。

        返回 None 表示成功；否则返回 (错误响应, HTTP 状态码)。
        """
        base = os.path.basename(filename.replace("\\", "/")).strip()
        if not base or base.startswith(".") or base != filename:
            return ({"code": 400, "msg": _t("webui.msg.invalid_filename")}, 400)
        if not base.lower().endswith((".whl", ".zip")):
            return ({"code": 400, "msg": _t("webui.msg.endstone_pkg_only")}, 400)
        plugins_dir = Path(self.plugin.data_folder).parent
        try:
            plugins_dir.mkdir(parents=True, exist_ok=True)
            (plugins_dir / base).write_bytes(data)
        except OSError as exc:
            return ({"code": 500, "msg": _t("webui.msg.write_plugins_failed", error=exc)}, 500)
        return None

    def api_endstone_dep_upload(self, m: re.Match, query: dict[str, str]) -> None:
        file_info = self._read_multipart_file()
        if not file_info:
            self.api_error(400, "webui.msg.no_file_received")
            return
        filename, content = file_info
        error = self._save_endstone_dep(filename, content)
        if error is not None:
            self._send_json(*error)
            return
        self.webui.logger.info(_t(
            "webui.log.endstone_pkg_saved",
            name=filename, size=len(content),
        ))
        self._send_json({
            "code": 200,
            "data": {"saved": filename},
            "msg": _t("webui.msg.saved_to_plugins"),
        })

    def api_endstone_dep_url(self, m: re.Match, query: dict[str, str]) -> None:
        body = self.read_json_dict()
        if body is None:
            return
        url = str(body.get("url", "")).strip()
        # 文件名取 URL 末段并校验扩展，防止把任意内容写进 plugins 目录
        filename = os.path.basename((urllib.parse.urlparse(url).path or "").rstrip("/")) or ""
        if not filename.lower().endswith((".whl", ".zip")):
            self.api_error(400, "webui.msg.url_filename_invalid")
            return
        sha256 = str(body.get("sha256", "") or "")
        data = self.fetch_url_payload(url, sha256, mismatch_key="webui.msg.sha256_mismatch_write")
        if data is None:
            return
        error = self._save_endstone_dep(filename, data)
        if error is not None:
            self._send_json(*error)
            return
        self.webui.logger.info(_t("webui.log.endstone_pkg_url_saved", name=filename))
        self._send_json({
            "code": 200,
            "data": {"saved": filename},
            "msg": _t("webui.msg.downloaded_to_plugins"),
        })

    # ---- 卸载 ----

    def api_subplugin_uninstall_preview(self, m: re.Match, query: dict[str, str]) -> None:
        name = m.group(1)
        try:
            removable, kept = self._compute_uninstall_deps(name)
        except Exception as exc:  # noqa: BLE001
            self._send_json({"code": 400, "msg": str(exc)}, 400)
            return
        dependents: list[dict[str, Any]] = []
        mgr = self.plugin.subplugin_manager
        if mgr is not None:
            try:
                dependents = mgr.dependents_of(name)
            except Exception:  # noqa: BLE001 - 反向依赖查询失败不影响卸载预检
                dependents = []
        self._send_json({"code": 200, "data": {
            "deps": removable, "kept_deps": kept, "dependents": dependents,
        }})

    def api_subplugin_uninstall(self, m: re.Match, query: dict[str, str]) -> None:
        name = m.group(1)
        mgr = self.require_subplugin_manager()
        if mgr is None:
            return
        # with_deps=1 连带卸载闲置 pip 依赖（须在卸载前算好：卸载后清单即失）
        removable: list[str] = []
        if str(query.get("with_deps", "")).strip().lower() in ("1", "true", "yes"):
            try:
                removable, _kept = self._compute_uninstall_deps(name)
            except Exception:  # noqa: BLE001 - 预检失败不影响主卸载流程
                removable = []
        result: list[Any] = [False, _t("webui.msg.uninstall_timeout")]

        def do_uninstall() -> None:
            try:
                result[0], result[1] = mgr.uninstall(name)
            except Exception as e:  # noqa: BLE001
                result[0], result[1] = False, _t("webui.msg.uninstall_exception", error=e)

        state, serr = self.run_main_op(do_uninstall, 15.0)
        if self.main_op_failure(
            state, serr,
            timeout_key="webui.msg.uninstall_timeout",
            error_key="webui.msg.uninstall_exception",
        ):
            return
        if result[0] and removable:
            self._uninstall_idle_deps(removable, result)
        if result[0]:
            self._send_json({"code": 200, "msg": str(result[1])})
            return
        self._send_json({"code": 400, "msg": str(result[1])}, 400)

    def _uninstall_idle_deps(self, removable: list[str], result: list[Any]) -> None:
        """卸载成功后连带移除闲置 pip 依赖；失败不影响主流程。"""
        removed: list[str] = []
        pip_mgr = self.webui._get_pip_manager_for_plugin(self.plugin)
        if pip_mgr is None:
            return
        # 持串行锁防并发写坏 site-packages
        with self.plugin._pip_serial_lock:
            for dep in removable:
                try:
                    ok_dep, _msg_dep = pip_mgr.uninstall(dep)
                except Exception:  # noqa: BLE001
                    ok_dep = False
                if ok_dep:
                    removed.append(dep)
        if removed:
            result[1] = str(result[1]) + "；" + _t(
                "webui.msg.uninstall_deps_removed", deps=", ".join(removed)
            )

    # ---- 文件浏览与编辑 ----

    def _resolve_subplugin_folder(self, name: str) -> Path | None:
        """解析子插件目录并防符号链接绕过；非法时已发送响应。"""
        mgr = self.require_subplugin_manager()
        if mgr is None:
            return None
        plugins_base = mgr.plugins_dir.resolve()
        folder = (mgr.plugins_dir / name).resolve()
        try:
            folder.relative_to(plugins_base)
        except ValueError:
            self.api_error(403, "webui.msg.forbidden_path")
            return None
        return folder

    def api_subplugin_files(self, m: re.Match, query: dict[str, str]) -> None:
        folder = self._resolve_subplugin_folder(m.group(1))
        if folder is None:
            return
        if not folder.is_dir():
            self.api_error(404, "webui.msg.subplugin_not_found")
            return
        files = []
        for p in sorted(folder.rglob("*")):
            # 不跟随符号链接，防目录外文件泄漏
            if p.is_symlink():
                continue
            if p.is_file() and "__pycache__" not in p.parts:
                try:
                    stat = p.stat()
                except OSError:
                    continue
                files.append({
                    "path": p.relative_to(folder).as_posix(),
                    "size": stat.st_size,
                    "editable": p.suffix.lower() in EDITABLE_SUFFIXES and stat.st_size <= 1024 * 1024,
                })
        self._send_json({"code": 200, "data": files})

    def _resolve_editable_target(self, folder: Path, rel: str) -> Path | None:
        """解析待读写的文件路径；越界时已发送 403。"""
        if not rel:
            self.api_error(403, "webui.msg.forbidden_path")
            return None
        try:
            # %00 解码出 NUL 会使 resolve() 抛 ValueError，按非法路径处理而非 500
            target = (folder / rel).resolve()
        except (ValueError, OSError):
            self.api_error(403, "webui.msg.forbidden_path")
            return None
        try:
            target.relative_to(folder)
        except ValueError:
            self.api_error(403, "webui.msg.forbidden_path")
            return None
        return target

    def api_subplugin_file_get(self, m: re.Match, query: dict[str, str]) -> None:
        folder = self._resolve_subplugin_folder(m.group(1))
        if folder is None:
            return
        target = self._resolve_editable_target(folder, query.get("path", ""))
        if target is None:
            return
        if not target.is_file():
            self.api_error(404, "webui.msg.file_not_exist")
            return
        if target.suffix.lower() not in EDITABLE_SUFFIXES:
            self.api_error(400, "webui.msg.file_type_unsupported")
            return
        try:
            stat = target.stat()
        except OSError as e:
            self.api_error(400, "webui.msg.read_failed", error=e)
            return
        if stat.st_size > 1024 * 1024:
            self.api_error(400, "webui.msg.file_too_large_edit")
            return
        try:
            content = target.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError) as e:
            self.api_error(400, "webui.msg.read_failed", error=e)
            return
        self._send_json({"code": 200, "data": {"path": query.get("path", ""), "content": content}})

    def api_subplugin_file_post(self, m: re.Match, query: dict[str, str]) -> None:
        folder = self._resolve_subplugin_folder(m.group(1))
        if folder is None:
            return
        target = self._resolve_editable_target(folder, query.get("path", ""))
        if target is None:
            return
        body = self._read_body() or {}
        content = body.get("content") if isinstance(body, dict) else None
        if not isinstance(content, str):
            self.api_error(400, "webui.msg.missing_content")
            return
        if len(content) > 1024 * 1024:
            self.api_error(400, "webui.msg.file_too_large_edit")
            return
        if target.suffix.lower() not in EDITABLE_SUFFIXES:
            self.api_error(400, "webui.msg.file_type_unsupported")
            return
        if target.suffix.lower() == ".json":
            try:
                json.loads(content)
            except json.JSONDecodeError as e:
                self.api_error(400, "webui.msg.json_format_error", error=e)
                return
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        self._send_json({"code": 200, "msg": _t("webui.msg.file_saved")})

    # ---- 卸载依赖计算与 ZIP 安装执行（供本域多路由复用）----

    def _compute_uninstall_deps(self, name: str) -> tuple[list[str], list[str]]:
        """计算卸载子插件时可连带移除的 pip 依赖，返回 (可移除, 需保留)。"""
        plugin = self.plugin
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
        from ...pip_manager import _normalize

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
