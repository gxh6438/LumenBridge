"""pip 包管理器：为子插件提供第三方依赖的自动安装能力。

含 dry-run 预检冲突（避免覆盖 Endstone/LumenBridge 核心依赖）、install/uninstall/list
与安装日志回调（供 WebUI 实时展示）。
"""

from __future__ import annotations

import importlib.metadata
import importlib.machinery
import json
import logging
import re
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Callable

from .i18n import t as _t

_LOG = logging.getLogger(__name__)

# Endstone / LumenBridge 核心依赖，禁止被升级或卸载
PROTECTED_PACKAGES = {
    "endstone", "websockets", "pip", "setuptools", "wheel",
    "endstone-lumenbridge", "endstone_lumenbridge",
}

# pip 包名与 import 名不一致的常见映射；键须与 _normalize() 同样归一化（连字符→下划线）才能命中。
PACKAGE_IMPORT_MAP: dict[str, str] = {
    key.replace("-", "_"): value
    for key, value in {
        "beautifulsoup4": "bs4",
        "pillow": "PIL",
        "python-dotenv": "dotenv",
        "pyyaml": "yaml",
        "python-dateutil": "dateutil",
        "opencv-python": "cv2",
        "protobuf": "google.protobuf",
        "google-api-python-client": "googleapiclient",
        "scikit-learn": "sklearn",
        "scikit-image": "skimage",
        "tensorflow": "tensorflow",
        "pyjwt": "jwt",
        "python-multipart": "multipart",
        "msgpack": "msgpack",
        "redis": "redis",
        "pymongo": "pymongo",
        "psycopg2-binary": "psycopg2",
        "mysql-connector-python": "mysql.connector",
        "attrs": "attr",
        "wrapt": "wrapt",
        # PyPI hello_pip 的发行名与实际导入包不同：import hello.hello
        "hello_pip": "hello",
    }.items()
}


def _normalize(name: str) -> str:
    """包名归一化：小写 + 连字符转下划线。"""
    return name.strip().lower().replace("-", "_")


# 版本约束运算符（=== 按 == 处理）
_VERSION_OP_RE = re.compile(r"(===|==|~=|>=|<=|!=|>|<)\s*([A-Za-z0-9.*+!_\-]+)")


def _version_key(version: str) -> tuple[tuple[int, str], ...]:
    """把版本号按 . 和 - 拆成 (数字, 字母后缀) 段元组，供纯手写比较。

    例：'2.10.0b1' → ((2,''),(10,''),(0,'b1'))；无数字的段记 (0, 段原文)。
    PEP 440 的简化实现：不处理 epoch、本地版本与预发布精确排序。
    """
    key: list[tuple[int, str]] = []
    for seg in re.split(r"[.\-]", str(version or "").strip().lstrip("vV")):
        if not seg:
            continue
        m = re.match(r"(\d+)(.*)", seg)
        if m:
            key.append((int(m.group(1)), m.group(2)))
        else:
            key.append((0, seg))
    return tuple(key)


def _strip_trailing_zeros(key: tuple[tuple[int, str], ...]) -> tuple[tuple[int, str], ...]:
    """去掉末尾的 (0,'') 段，使 2.10 与 2.10.0 相等（PEP 440 语义近似）"""
    parts = list(key)
    while parts and parts[-1] == (0, ""):
        parts.pop()
    return tuple(parts)


def _version_satisfies(installed: str, constraint: str) -> bool:
    """手写版本约束校验，支持 >= > <= < == != ~= 及逗号组合（如 ">=2.0,<3"）。

    纯元组比较不依赖 packaging（嵌入式环境可能没有）；``~=`` 等价于 ``>=x.y``
    且 ``==x.*``；``==x.*`` / ``!=x.*`` 按前缀匹配；解析不了的片段保守放行，
    宁可放过也不误报"依赖缺失"。
    """
    inst_key = _version_key(installed)
    inst_norm = _strip_trailing_zeros(inst_key)
    for op, ver in _VERSION_OP_RE.findall(str(constraint or "")):
        wildcard = ver.endswith(".*")
        base = ver[:-2] if wildcard else ver
        base_key = _version_key(base)
        base_norm = _strip_trailing_zeros(base_key)
        if op in (">=", ">"):
            ok = inst_norm >= base_norm if op == ">=" else inst_norm > base_norm
        elif op in ("<=", "<"):
            ok = inst_norm <= base_norm if op == "<=" else inst_norm < base_norm
        elif op in ("==", "==="):
            if wildcard:
                ok = _strip_trailing_zeros(inst_key[: len(base_norm)]) == base_norm
            else:
                ok = inst_norm == base_norm
        elif op == "!=":
            if wildcard:
                ok = _strip_trailing_zeros(inst_key[: len(base_norm)]) != base_norm
            else:
                ok = inst_norm != base_norm
        elif op == "~=":
            # ~=x.y.z ≡ >=x.y.z 且 ==x.y.*（前缀取去掉最后一段）
            if not base_key:
                continue
            ok = inst_norm >= base_norm
            if ok:
                prefix = _strip_trailing_zeros(base_key[:-1])
                ok = _strip_trailing_zeros(inst_key)[: len(prefix)] == prefix
        else:  # pragma: no cover - 正则已限定运算符集合，防御分支
            _LOG.debug("版本约束 %r 使用了不支持的运算符 %r，保守视为满足", constraint, op)
            continue
        if not ok:
            return False
    return True


class PipManager:
    """pip 调用封装（线程安全由调用方保证）。"""

    @staticmethod
    def _pip_cmd(subcommand: list[str]) -> list[str]:
        """构建 pip 命令。

        uv 不尊重 ``PYTHONUSERBASE``，会把包装到系统 Python 导致 ``import`` 失败，
        因此 install/uninstall 一律用 ``sys.executable -m pip`` + ``--user``
        （装到 ``plugins/.local``），并加 ``--break-system-packages``（PEP 668）。
        """
        pip_args = list(subcommand)
        if pip_args and pip_args[0] in ("install", "uninstall") and "--break-system-packages" not in pip_args:
            pip_args = [pip_args[0], "--break-system-packages"] + pip_args[1:]
        return [sys.executable, "-m", "pip"] + pip_args

    def __init__(self, config: dict[str, Any], logger: Any) -> None:
        self.logger = logger
        cfg = config.get("pip", {}) if isinstance(config, dict) else {}
        self.enable: bool = bool(cfg.get("enable", True))
        # 用 or "" 防 None：index_url 为 null 时 str(None) 会得到 "None" 被当 URL
        self.index_url: str = str(cfg.get("index_url") or "")
        try:
            self.timeout: int = int(cfg.get("timeout") or 300)
        except (TypeError, ValueError):
            self.timeout = 300

    @staticmethod
    def _site_packages_dirs() -> list[str]:
        """收集全部 site-packages 目录（系统 + 用户），供磁盘回退检查。"""
        dirs: list[str] = []
        try:
            import site

            dirs.extend(site.getsitepackages())
            user_site = site.getusersitepackages()
            if user_site:
                dirs.append(user_site)
        except Exception:
            pass
        seen: set[str] = set()
        ordered: list[str] = []
        for d in dirs:
            if d and d not in seen:
                seen.add(d)
                ordered.append(d)
        return ordered

    @staticmethod
    def _find_spec_disk(name: str) -> Any:
        """检测包是否真实安装于磁盘，全程不触碰 sys.modules（并发 import 下无竞态）。

        依次尝试：1. ``importlib.metadata.distribution``；2. site-packages 下
        ``name*.dist-info`` / ``name*.egg-info`` 目录；3. 顶层模块/包文件
        （``name/__init__.py`` 或 ``name.py``，覆盖未登记的 pip/import 名差异包）。
        """
        if not name:
            return None
        # 1) 分发元数据（Python 3.11+ 自带 PEP 503 归一化匹配）
        try:
            return importlib.metadata.distribution(name)
        except importlib.metadata.PackageNotFoundError:
            pass
        except Exception:
            _LOG.debug("distribution(%r) 查询异常", name, exc_info=True)

        site_dirs = PipManager._site_packages_dirs()
        norm = _normalize(name)
        patterns: list[str] = []
        for stem in dict.fromkeys((norm, name)):
            patterns.append(f"{stem}*.dist-info")
            patterns.append(f"{stem}*.egg-info")
        # 2) dist-info / egg-info 目录回退
        for base in site_dirs:
            try:
                base_dir = Path(base)
                for pattern in patterns:
                    if next(base_dir.glob(pattern), None) is not None:
                        return True
            except Exception:
                continue
        # 3) 顶层模块/包文件回退（import 名与分发名不一致的场景）
        for base in site_dirs:
            try:
                base_dir = Path(base)
                if (base_dir / name / "__init__.py").is_file() or (base_dir / f"{name}.py").is_file():
                    return True
            except Exception:
                continue
        # 4) 终局回退：PathFinder 只做真实磁盘查找，覆盖 vendored 库与索引未覆盖
        #    的 import 名；不用 importlib.util.find_spec（先查 sys.modules，
        #    pip uninstall 后残留旧 __spec__ 会假阳性）
        try:
            return importlib.machinery.PathFinder.find_spec(name)
        except (ImportError, ValueError, AttributeError):
            return None

    @staticmethod
    def _metadata_version(name: str) -> str | None:
        """按分发（PyPI）名查询已安装版本；查不到返回 None（不抛异常）。

        3.11+ 自带 PEP 503 归一化；老版本解释器补试连字符/下划线变体。
        """
        canonical = re.sub(r"[-_.]+", "-", name.lower())
        candidates = list(dict.fromkeys((name, canonical, canonical.replace("-", "_"))))
        for cand in candidates:
            if not cand:
                continue
            try:
                return importlib.metadata.version(cand)
            except importlib.metadata.PackageNotFoundError:
                continue
            except Exception:
                _LOG.debug("metadata version(%r) 查询异常", cand, exc_info=True)
                continue
        return None

    @staticmethod
    def check_dependency(package_spec: str) -> bool:
        """检测依赖是否已安装（package_spec 可含版本号，如 'openai>=1.0.0'）。

        带版本约束时：元数据版本满足全部约束 → 已装；不满足 → 视为缺失，
        由调用方以完整约束串触发安装/升级；元数据查不到（vendored / 无
        dist-info）→ 回退 import 名磁盘检查。无约束时以 ``_find_spec_disk``
        为唯一权威判据，metadata 仅用于发现 import 名。
        """
        if not isinstance(package_spec, str):
            return False
        name = re.split(r"[<>=!~;\[]", package_spec.strip(), maxsplit=1)[0].strip()
        if not name:
            # 空字符串/纯空白视为未安装，避免恶意空声明绕过依赖检查
            return False

        # 带版本约束 → 元数据版本 + 约束校验（分发名优先于 import 名）
        constraint = package_spec.strip()[len(name):].strip()
        if constraint and _VERSION_OP_RE.search(constraint):
            installed_version = PipManager._metadata_version(name)
            if installed_version is not None:
                return _version_satisfies(installed_version, constraint)
            # 元数据查不到 → 落到下方 import 名磁盘检查

        norm = _normalize(name)
        canonical = re.sub(r"[-_.]+", "-", name.lower())

        mapped_import = PACKAGE_IMPORT_MAP.get(norm, norm).replace("-", "_")
        import_names: list[str] = []
        for candidate in (mapped_import, norm.replace("-", "_"), name.replace("-", "_")):
            if candidate and candidate not in import_names:
                import_names.append(candidate)

        for import_name in import_names:
            if PipManager._find_spec_disk(import_name) is not None:
                return True

        # 经 metadata 的「导入包→发行包」索引发现 PACKAGE_IMPORT_MAP 未覆盖的候选
        try:
            packages_map = importlib.metadata.packages_distributions()
            for imp_name, dist_names in packages_map.items():
                for dist_name in dist_names:
                    if re.sub(r"[-_.]+", "-", dist_name.lower()) == canonical:
                        if PipManager._find_spec_disk(imp_name) is not None:
                            return True
        except Exception:
            pass

        return False

    @staticmethod
    def refresh_dependency_cache() -> None:
        """刷新导入查找缓存，并确保 site-packages 在 sys.path 中。

        pip/uv 后台写入新包后 FileFinder 与元数据可能保留旧快照，清理缓存避免
        误报缺失；嵌入式 Python 可能不把全部 site-packages 加入 ``sys.path``，
        这里显式补齐。pip uninstall 只删磁盘不清 ``sys.modules``，磁盘上已
        不存在的残留模块也一并清理。
        """
        import site

        for sp in site.getsitepackages():
            if sp not in sys.path:
                site.addsitedir(sp)

        user_site = site.getusersitepackages()
        if user_site and user_site not in sys.path:
            site.addsitedir(user_site)

        importlib.invalidate_caches()
        try:
            sys.path_importer_cache.clear()
        except Exception:
            pass

        # 清理磁盘文件已不存在的顶层第三方模块（防 PathFinder 假阳性）
        stale: list[str] = []
        for mod_name, mod in list(sys.modules.items()):
            if "." in mod_name:
                continue
            if mod_name in PROTECTED_PACKAGES or mod_name.startswith("_"):
                continue
            spec = getattr(mod, "__spec__", None)
            if spec is None:
                continue
            origin = getattr(spec, "origin", None)
            # 跳过内置/冻结/非文件路径 origin，只清理指向真实文件但已不存在的模块
            if not origin or origin in ("built-in", "frozen") or not Path(origin).is_absolute():
                continue
            if not Path(origin).exists():
                stale.append(mod_name)
        for mod_name in stale:
            try:
                del sys.modules[mod_name]
            except KeyError:
                pass

    def missing_dependencies(self, dependencies: list[str]) -> list[str]:
        """返回当前 Endstone 解释器实际不可导入的依赖列表。"""
        self.refresh_dependency_cache()
        return [d for d in dependencies if not self.check_dependency(d)]

    def _extract_package_name(self, package_spec: str) -> str:
        """从 'openai>=1.0.0' 提取归一化包名 'openai'。"""
        name = re.split(r"[<>=!~;\[]", package_spec.strip(), maxsplit=1)[0].strip()
        return _normalize(name)

    @staticmethod
    def _is_valid_package_arg(pkg: str) -> bool:
        """校验单个包参数是否合法，防 pip 选项注入 / VCS URL 投毒。

        拒绝以 - 开头的参数（pip 选项，如 -i / --index-url / -e）、VCS URL
        （git+/svn+/hg+/bzr+）与 PEP 508 URL spec（@ URL），避免从任意 URL 安装代码
        导致供应链攻击；接受标准包名 + 版本约束 + extras（如 openai>=1.0, requests[socks]<3）。
        """
        if not pkg or not isinstance(pkg, str):
            return False
        pkg = pkg.strip()
        if pkg.startswith("-"):
            return False
        lower = pkg.lower()
        if lower.startswith(("git+", "svn+", "hg+", "bzr+", "http://", "https://", "file://")):
            return False
        if "@" in pkg and re.search(r"@\s*\w+://", pkg):
            return False
        if re.match(r"^[A-Za-z0-9._\-\[\]><=!~;,*]+$", pkg):
            return True
        return False

    def dry_run(self, packages: list[str], *, upgrade: bool = False) -> tuple[bool, str, list[str]]:
        """pip install --dry-run --report 预检，返回 (是否安全, 原因, 受影响受保护包列表)。"""
        if not packages:
            return True, "", []
        invalid = [p for p in packages if not self._is_valid_package_arg(p)]
        if invalid:
            return False, _t("pip.invalid_package_arg", packages=", ".join(invalid)), []
        # uv 不支持 --report，预检必须用标准 pip 拿结构化 JSON 才能识别受保护依赖
        cmd = [
            sys.executable, "-m", "pip", "install", "--break-system-packages",
            "--dry-run", "--report", "-", "--quiet", "--",
        ]
        if self.index_url:
            # -i 必须在 -- 之前
            cmd = [
                sys.executable, "-m", "pip", "install", "--break-system-packages",
                "--dry-run", "--report", "-", "--quiet", "-i", self.index_url, "--",
            ]
        if upgrade:
            cmd.insert(-1, "--upgrade")
        cmd.extend(packages)
        # 预检超时上限 180s：慢网络/大依赖树解析易超 60s，用户配置更低时取更小值
        precheck_timeout = (
            min(self.timeout, 180) if self.timeout > 60 else min(self.timeout, 60)
        )
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=precheck_timeout,
            )
        except subprocess.TimeoutExpired:
            # 预检超时≠真冲突（慢网络/解析慢），放行安装；受保护包防线仍在 uninstall 侧
            self.logger.warning(
                f"pip dry-run 预检超时（上限 {precheck_timeout}s），已跳过冲突预检并放行安装"
            )
            return True, "", []
        except (FileNotFoundError, OSError) as e:
            return False, _t("pip.dry_run_failed", error=e), []

        if result.returncode != 0:
            combined = result.stdout + result.stderr
            # pip<22.0 不支持 --report/--dry-run：降级跳过预检，避免旧环境无法安装
            lowered = combined.lower()
            if ("--report" in combined or "--dry-run" in combined) and (
                "unknown option" in lowered or "no such option" in lowered
            ):
                return True, "", []
            return False, _t("pip.dry_run_error", output=combined[-500:]), []

        conflicts: list[str] = []
        try:
            report = json.loads(result.stdout) if result.stdout.strip() else {}
            install_list = report.get("install", []) if isinstance(report, dict) else []
            protected_norm = {_normalize(p) for p in PROTECTED_PACKAGES}
            for item in install_list:
                if not isinstance(item, dict):
                    continue
                meta = item.get("metadata", {})
                pkg_name = _normalize(meta.get("name", "")) if isinstance(meta, dict) else ""
                if pkg_name in protected_norm:
                    conflicts.append(meta.get("name", pkg_name))
        except (json.JSONDecodeError, ValueError, AttributeError) as exc:
            # 报告无法验证时绝不继续安装，否则冲突检测形同虚设
            return False, _t("pip.dry_run_failed", error=exc), []

        if conflicts:
            return False, _t("pip.conflict_detected", packages=", ".join(conflicts)), conflicts
        return True, "", []

    def install(
        self,
        packages: list[str],
        on_log: Callable[[str], None] | None = None,
        *,
        upgrade: bool = False,
    ) -> tuple[bool, str]:
        """安装包，返回 (成功, 消息)；on_log 回调实时输出 pip 日志。

        本方法同步阻塞调用线程，在游戏主线程调用会冻结服务器，调用方应放到后台线程执行。
        """
        if not self.enable:
            return False, _t("pip.disabled")
        if not packages:
            return True, _t("pip.nothing_to_install")

        invalid = [p for p in packages if not self._is_valid_package_arg(p)]
        if invalid:
            return False, _t("pip.invalid_package_arg", packages=", ".join(invalid))

        safe, reason, _conflicts = self.dry_run(packages, upgrade=upgrade)
        if not safe:
            return False, reason

        # -i 必须在 -- 之前；--user 装到 plugins/.local，与 LumenBridge 同目录，子插件 import 即可命中
        base_cmd = PipManager._pip_cmd(["install", "--no-input", "--progress-bar", "off", "--user"])
        if self.index_url:
            base_cmd.extend(["-i", self.index_url])
        if upgrade:
            base_cmd.append("--upgrade")
        base_cmd.append("--")
        cmd = base_cmd + packages

        if on_log:
            on_log(_t("pip.installing", packages=" ".join(packages)))

        # 流式逐行回调 on_log，前端进度弹窗可实时看到安装过程
        stdout_tail: list[str] = []
        stderr_lines: list[str] = []
        try:
            process = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace",
            )
        except (FileNotFoundError, OSError) as e:
            return False, _t("pip.install_failed", error=e)

        # stderr 由后台线程排空：主线程逐行读 stdout 时若 stderr 缓冲区写满会互相死锁
        def _drain_stderr() -> None:
            try:
                if process.stderr is not None:
                    for line in process.stderr:
                        stderr_lines.append(line.rstrip("\n"))
            except Exception:  # noqa: BLE001
                pass

        stderr_thread = threading.Thread(target=_drain_stderr, daemon=True)
        stderr_thread.start()

        # 看门狗兜底：进程挂起不吐输出时由定时器强杀，读循环随管道 EOF 结束
        timed_out = threading.Event()

        def _kill_on_timeout() -> None:
            timed_out.set()
            try:
                process.kill()
            except OSError:
                pass

        watchdog = threading.Timer(max(0, self.timeout), _kill_on_timeout)
        watchdog.daemon = True
        watchdog.start()
        try:
            if process.stdout is not None:
                for line in process.stdout:
                    line = line.rstrip("\n")
                    if not line:
                        continue
                    if on_log:
                        # 回调异常不能中断读取循环，否则 pip 变成无人管理的后台半安装
                        try:
                            on_log(line)
                        except Exception:  # noqa: BLE001
                            pass
                    stdout_tail.append(line)
                    if len(stdout_tail) > 30:
                        stdout_tail.pop(0)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
        finally:
            watchdog.cancel()
            stderr_thread.join(timeout=5)

        if timed_out.is_set():
            return False, _t("pip.install_failed", error=f"timeout after {self.timeout}s")

        stderr_tail = [ln for ln in stderr_lines if ln.strip()][-30:]
        if on_log:
            for line in stderr_tail:
                on_log(line)

        if process.returncode == 0:
            # 退出码成功≠当前解释器可导入，立即验证把环境错误/陈旧缓存转为可行动信息
            unavailable = self.missing_dependencies(packages)
            if unavailable:
                # 仍未检测到多为嵌入式 Python/venv 缓存滞后而非真失败：返回成功但附警告
                msg = _t("pip.install_success", packages=" ".join(packages))
                msg += _t("pip.install_not_visible_warning", packages=", ".join(unavailable))
                return True, msg
            return True, _t("pip.install_success", packages=" ".join(packages))
        return False, _t(
            "pip.install_failed",
            error="\n".join(stderr_tail)[-500:] or "\n".join(stdout_tail)[-500:],
        )

    def uninstall(self, package: str) -> tuple[bool, str]:
        """卸载包（受保护包拒绝）。"""
        if not isinstance(package, str) or not package.strip():
            return False, _t("pip.no_packages_specified")
        if not self._is_valid_package_arg(package):
            return False, _t("pip.invalid_package_arg", packages=package)
        pkg_name = self._extract_package_name(package)
        if pkg_name in {_normalize(p) for p in PROTECTED_PACKAGES}:
            return False, _t("pip.protected_package", package=package)

        # pip uninstall 只接受裸包名：传入 "requests>=2.0" 会被当字面包名静默跳过
        cmd = PipManager._pip_cmd(["uninstall", "-y", "--", pkg_name])
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=min(self.timeout, 60),
            )
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
            return False, _t("pip.uninstall_failed", error=e)

        if result.returncode == 0:
            return True, _t("pip.uninstall_success", package=package)
        return False, _t("pip.uninstall_failed", error=result.stderr[-500:] or result.stdout[-500:])

    def list_packages(self) -> list[dict[str, str]]:
        """返回已装包列表 [{name, version}]。"""
        cmd = PipManager._pip_cmd(["list", "--format=json"])
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=30,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            return []
        if result.returncode != 0:
            return []
        try:
            return json.loads(result.stdout)
        except (json.JSONDecodeError, ValueError):
            return []
