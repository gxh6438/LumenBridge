"""pip 包管理器：为子插件提供第三方依赖的自动安装能力。

含 dry-run 预检冲突（避免覆盖 Endstone/LumenBridge 核心依赖）、install/uninstall/list
与安装日志回调（供 WebUI 实时展示）。
"""

from __future__ import annotations

import importlib.metadata
import importlib.machinery
import importlib.util
import json
import logging
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

from .i18n import t as _t

_LOG = logging.getLogger(__name__)

# Endstone / LumenBridge 核心依赖，禁止被升级或卸载
PROTECTED_PACKAGES = {
    "endstone", "websockets", "pip", "setuptools", "wheel",
    "endstone-lumenbridge", "endstone_lumenbridge",
}

# pip 包名（归一化小写）与 Python import 名不一致的常见包映射，用于 check_dependency 检测。
# 查找侧用 _normalize()（连字符→下划线），键必须同样归一化，否则带连字符的包
# （python-dotenv / opencv-python / scikit-learn 等）永远查不到映射，快速路径失效。
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


# H17：版本约束运算符（=== 按 == 处理）
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
    """H17：手写版本约束校验，支持 >= > <= < == != ~= 及逗号组合（如 ">=2.0,<3"）。

    - 版本按 . 和 - 拆段比较（纯手写元组比较，不依赖 packaging——嵌入式
      环境可能没有该库）；
    - ``~=`` 等价于 ``>=x.y`` 且 ``==x.*``（PEP 440 兼容发行版语义）；
    - ``==x.*`` / ``!=x.*`` 按前缀匹配；
    - 解析不了的运算符/片段保守放行（返回满足）并记 debug，
      宁可放过也不因校验器缺陷误报"依赖缺失"。
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

    _uv_available: bool | None = None  # 延迟检测

    @staticmethod
    def _is_uv_available() -> bool:
        """检测 uv 是否可用。"""
        if PipManager._uv_available is None:
            PipManager._uv_available = shutil.which("uv") is not None
        return PipManager._uv_available

    # uv pip 不支持的 pip 原生参数，使用 uv 时自动剔除
    # 格式: flag -> 是否携带值（True 表示下一个参数是值也需要跳过）
    _UV_UNSUPPORTED_FLAGS: dict[str, bool] = {
        "--no-input": False,
        "--progress-bar": True,
        "--no-color": False,
        "--no-python-version-warning": False,
    }

    @staticmethod
    def _pip_cmd(subcommand: list[str]) -> list[str]:
        """构建 pip 命令。

        Endstone 嵌入式 Python 的用户 site-packages 位于
        ``plugins/.local/lib/pythonX.Y/site-packages``（由 ``PYTHONUSERBASE``
        指向 ``plugins/.local``）。uv 不尊重 ``PYTHONUSERBASE``，会把包装到
        系统 Python 导致 ``import`` 失败，因此 install/uninstall 一律用
        ``sys.executable -m pip`` + ``--user``。会修改环境的命令还需
        ``--break-system-packages``（PEP 668）。
        """
        pip_args = list(subcommand)
        if pip_args and pip_args[0] in ("install", "uninstall") and "--break-system-packages" not in pip_args:
            pip_args = [pip_args[0], "--break-system-packages"] + pip_args[1:]
        return [sys.executable, "-m", "pip"] + pip_args

    def __init__(self, config: dict[str, Any], logger: Any) -> None:
        self.logger = logger
        cfg = config.get("pip", {}) if isinstance(config, dict) else {}
        self.enable: bool = bool(cfg.get("enable", True))
        # 用 `or ""` 防 None：config.json 写 "index_url": null 时 str(None) 会得到
        # 字符串 "None" 被 pip 当作 URL，导致安装失败
        self.index_url: str = str(cfg.get("index_url") or "")
        try:
            self.timeout: int = int(cfg.get("timeout") or 300)
        except (TypeError, ValueError):
            self.timeout = 300

    @staticmethod
    def _get_pip_installed_names() -> set[str]:
        """通过 ``pip list`` 获取已安装包名集合（PEP 503 规范化），作为 importlib.metadata 的回退。

        Endstone 嵌入式 Python 或特殊 venv 下 importlib.metadata 可能因 sys.path 配置
        差异无法检测到 pip 刚安装的包；``pip list`` 直接查询同一解释器元数据，不受
        importlib 缓存影响。
        """
        try:
            cmd = PipManager._pip_cmd(["list", "--format=json"])
            result = subprocess.run(
                cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=15,
            )
            if result.returncode == 0 and result.stdout.strip():
                packages = json.loads(result.stdout)
                names: set[str] = set()
                for p in packages:
                    if isinstance(p, dict) and "name" in p:
                        canonical = re.sub(r"[-_.]+", "-", str(p["name"]).lower())
                        names.add(canonical)
                return names
        except Exception:
            pass
        return set()

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
        # 去重并保持顺序
        seen: set[str] = set()
        ordered: list[str] = []
        for d in dirs:
            if d and d not in seen:
                seen.add(d)
                ordered.append(d)
        return ordered

    @staticmethod
    def _find_spec_disk(name: str) -> Any:
        """检测包是否真实安装于磁盘，全程不触碰 sys.modules（M23）。

        旧实现为绕过 sys.modules 缓存会临时 pop 顶层模块再恢复——在并发
        import 场景存在竞态（弹出/恢复窗口内其他线程可能读到半状态）。现改为
        纯磁盘判定，按顺序：

        1. ``importlib.metadata.distribution(name)`` 存在 → 已安装（返回
           Distribution 对象，调用方仅判真值；pip uninstall 后即消失）；
        2. 找不到 distribution 时回退扫描 site-packages 目录下
           ``name*.dist-info`` / ``name*.egg-info`` 目录存在性；
        3. 仍找不到时最后检查 site-packages 下的顶层模块/包文件
           （``name/__init__.py`` 或 ``name.py``），覆盖 pip 名与 import 名
           不一致且未在 PACKAGE_IMPORT_MAP 登记的包（如 cv2）。
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
        # 4) PathFinder 磁盘查找终局回退：覆盖 vendored 库（如内置 lib/websockets，
        #    经 sys.path 注入）与 PACKAGE_IMPORT_MAP / packages_distributions
        #    索引都未覆盖的 import 名。
        #    使用 PathFinder.find_spec 而非 importlib.util.find_spec，因为后者会
        #    先查 sys.modules 缓存——当依赖曾被 import 后又被 pip uninstall 时，
        #    pip 只删磁盘不清 sys.modules，find_spec 仍返回旧 __spec__（假阳性）。
        #    PathFinder 直接扫 sys.path 做真实磁盘查找，不读 sys.modules。
        try:
            return importlib.machinery.PathFinder.find_spec(name)
        except (ImportError, ValueError, AttributeError):
            return None

    @staticmethod
    def _metadata_version(name: str) -> str | None:
        """H17：按分发（PyPI）名查询已安装版本；查不到返回 None（不抛异常）。

        优先用约束串中的包名直查 importlib.metadata（3.11+ 自带 PEP 503
        归一化）；老版本解释器再补试连字符/下划线变体。
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
    def check_dependency(
        package_spec: str,
        *,
        pip_installed: set[str] | None = None,
    ) -> bool:
        """检测某个依赖是否已安装（package_spec 可含版本号，如 'openai>=1.0.0'）。

        H17：manifest 声明了版本约束时，优先用**约束串中的分发（PyPI）名**
        查 ``importlib.metadata`` 版本并逐条校验约束：

        - 元数据命中且满足全部约束 → 已装，跳过安装；
        - 元数据命中但不满足 → 视为缺失，调用方以完整约束串触发 pip
          安装/升级（loader 报缺失依赖，WebUI/marketplace 传原始 spec 安装）；
        - 元数据查不到（vendored / 无 dist-info 的包）→ 回退原有 import 名
          磁盘检查（``_find_spec_disk``），保留现状行为。

        无版本约束时维持原有判据：``_find_spec_disk`` 是唯一权威判据——实际
        测试当前解释器能否从磁盘找到该包，metadata / ``pip list`` 仅用于发现
        import 名（处理 pip 名与 import 名不一致的包）。``pip_installed``
        参数保留以兼容旧调用方。
        """
        if not isinstance(package_spec, str):
            return False
        name = re.split(r"[<>=!~;\[]", package_spec.strip(), maxsplit=1)[0].strip()
        if not name:
            # 空字符串/纯空白视为未安装，避免恶意空声明绕过依赖检查
            return False

        # H17：带版本约束 → 元数据版本 + 约束校验（distro 名优先于 import 名）
        constraint = package_spec.strip()[len(name):].strip()
        if constraint and _VERSION_OP_RE.search(constraint):
            installed_version = PipManager._metadata_version(name)
            if installed_version is not None:
                return _version_satisfies(installed_version, constraint)
            # 元数据查不到：落到下方 import 名磁盘检查（保留现状行为）

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

        # 通过 metadata 的「导入包 → 发行包」索引发现 PACKAGE_IMPORT_MAP 未覆盖的
        # import 名候选，再用 _find_spec_disk 验证可导入性
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

        pip/uv 在 WebUI 后台线程写入新分发包后，FileFinder 与元数据路径可能仍保留旧目录
        快照，清理缓存后再检查避免把已安装依赖误报为缺失。嵌入式 Python（如 Endstone
        打包的解释器）可能不会把 ``site.getsitepackages()`` 和
        ``site.getusersitepackages()`` 全部加入 ``sys.path``，pip 装包成功后子插件仍
        ``import`` 失败的根因就在于此，这里显式补齐。

        pip uninstall 只删磁盘文件不清 ``sys.modules``，曾 import 过的依赖会以旧模块
        残留在 ``sys.modules`` 中。这里对磁盘上已不存在的模块做一次 ``sys.modules``
        清理，使后续 ``_find_spec_disk`` 的 PathFinder 判定不受残留影响。
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

        # 清理 sys.modules 中磁盘文件已不存在的顶层第三方模块，
        # 避免 PathFinder 因旧 __spec__ 缓存返回假阳性
        stale: list[str] = []
        for mod_name, mod in list(sys.modules.items()):
            # 只清理顶层模块（无点的），且排除核心 / 标准库模块
            if "." in mod_name:
                continue
            if mod_name in PROTECTED_PACKAGES or mod_name.startswith("_"):
                continue
            spec = getattr(mod, "__spec__", None)
            if spec is None:
                continue
            # 检查模块的磁盘路径是否还存在
            origin = getattr(spec, "origin", None)
            # 跳过内置 / 冻结模块（origin 为 "built-in" / "frozen"），
            # 跳过非文件路径的 origin，只清理指向真实文件但文件已不存在的模块
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
        # uv pip 不支持 pip 的 --report 参数，而预检必须得到结构化 JSON 才能可靠识别
        # 受保护依赖；因此无论正式安装器是否使用 uv，预检都通过同一解释器的标准 pip
        # 执行，且显式处理 PEP 668。
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
        # M24：预检超时上限从 60s 放宽到 180s——慢网络/大依赖树解析容易超 60s；
        # 用户配置 pip.timeout > 60 时取 min(用户值, 180)，否则维持原 min(用户值, 60)
        precheck_timeout = (
            min(self.timeout, 180) if self.timeout > 60 else min(self.timeout, 60)
        )
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=precheck_timeout,
            )
        except subprocess.TimeoutExpired:
            # M24：预检超时 ≠ 检测到真冲突（慢网络/解析慢而已）。记 warning 后
            # 放行安装，不再直接拒绝；受保护包防线仍在：uninstall 侧拒绝 +
            # 预检报告解析失败路径依旧返回 False
            self.logger.warning(
                f"pip dry-run 预检超时（上限 {precheck_timeout}s），已跳过冲突预检并放行安装"
            )
            return True, "", []
        except (FileNotFoundError, OSError) as e:
            return False, _t("pip.dry_run_failed", error=e), []

        if result.returncode != 0:
            combined = result.stdout + result.stderr
            # pip<22.0 不支持 --report/--dry-run：预检降级跳过（安装照常执行，
            # 受保护包在 uninstall 侧仍有防线），避免旧 pip 环境下完全无法安装
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
            # 预检报告无法验证时绝不继续安装，否则核心依赖冲突检测形同虚设
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

        # M24：dry_run 预检超时时返回 (True, "", [])，此处不再因预检超时而中断，
        # 继续执行正式安装；真正的冲突/参数错误仍会在此被拦截
        safe, reason, _conflicts = self.dry_run(packages, upgrade=upgrade)
        if not safe:
            return False, reason

        # -i 必须在 -- 之前（-- 后的参数 pip 不再解析为选项）；--user 让包装到
        # site.getusersitepackages()（PYTHONUSERBASE 指向 plugins/.local），与
        # LumenBridge 自身所在目录一致，子插件 import 即可命中
        base_cmd = PipManager._pip_cmd(["install", "--no-input", "--progress-bar", "off", "--user"])
        if self.index_url:
            base_cmd.extend(["-i", self.index_url])
        if upgrade:
            base_cmd.append("--upgrade")
        base_cmd.append("--")
        cmd = base_cmd + packages

        if on_log:
            on_log(_t("pip.installing", packages=" ".join(packages)))

        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=self.timeout,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
            return False, _t("pip.install_failed", error=e)

        if on_log:
            if result.stdout:
                on_log(result.stdout)
            if result.stderr:
                on_log(result.stderr)

        if result.returncode == 0:
            # pip 退出码成功不等于当前运行中的解释器已可导入；立即在同一解释器验证，
            # 把环境目标错误或陈旧查找缓存转为可行动错误
            unavailable = self.missing_dependencies(packages)
            if unavailable:
                # pip 已报告成功但 importlib 仍未检测到，通常发生在嵌入式 Python 或
                # 特殊 venv（importlib 缓存滞后）而非真正失败；返回成功但附警告，
                # 子插件加载时若 import 失败会给出准确的 ModuleNotFoundError
                msg = _t("pip.install_success", packages=" ".join(packages))
                msg += _t("pip.install_not_visible_warning", packages=", ".join(unavailable))
                return True, msg
            return True, _t("pip.install_success", packages=" ".join(packages))
        return False, _t("pip.install_failed", error=result.stderr[-500:] or result.stdout[-500:])

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
