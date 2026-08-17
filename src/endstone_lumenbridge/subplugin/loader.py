"""子插件加载器：发现、加载、卸载与热重载 plugins/lumenbridge/plugins/ 下的子插件。

``lumen.json`` 清单字段::

    {
        "name": "my_plugin",         # 子插件名
        "version": "1.0.0",
        "author": "you",
        "desc": "示例子插件",
        "load": true,                # 是否加载
        "priority": "main",          # pre / main / post 三段加载顺序
        "min_v": ""                  # 最低 LumenBridge 版本要求（可选）
    }

入口文件 ``main.py`` 必须暴露 ``on_load(lumen)``，可选暴露 ``on_unload(lumen)``。
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import traceback
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .. import __version__
from ..i18n import t as _t
from .context import LumenContext

if TYPE_CHECKING:
    from ..plugin import LumenBridgePlugin

MANIFEST_NAME = "lumen.json"
ENTRY_NAME = "main.py"
PRIORITY_ORDER = {"pre": 0, "main": 1, "post": 2}

# 防路径穿越与非法字符：仅允许字母数字下划线连字符
_NAME_RE = re.compile(r"^[A-Za-z0-9_\-]+$")

# ZIP 炸弹防护：解压前按声明大小预检（entry 数 / 单文件 / 总解压体积）。
# 市场下载另有压缩包大小上限，但高压缩比 ZIP 仍可在解压时耗尽磁盘与内存。
_MAX_ZIP_ENTRIES = 2000
_MAX_ZIP_FILE_BYTES = 64 * 1024 * 1024
# M30：解压总上限从 256MB 收紧到 64MB，进一步压缩 ZIP 炸弹的破坏面
_MAX_ZIP_TOTAL_BYTES = 64 * 1024 * 1024


def _is_safe_name(name: str) -> bool:
    """校验子插件名合法性，禁止 . / \\ 等路径字符"""
    # 恶意/畸形 lumen.json 可把 name 写成数字/列表等非字符串真值，
    # 直接 match 会抛 TypeError
    if not isinstance(name, str):
        return False
    return bool(name) and bool(_NAME_RE.match(name))


def _read_manifest_dict(path: Path) -> dict[str, Any] | None:
    """安全读取 lumen.json：返回 dict；内容损坏（非法 JSON / 非 UTF-8 /
    合法 JSON 但非对象如 null/123/[]）时返回 None，绝不向上抛异常。

    旧实现只捕获 (JSONDecodeError, OSError)：
    - 非 UTF-8 字节抛 UnicodeDecodeError 不被捕获；
    - manifest.update(null)/update("str") 抛 TypeError/ValueError 不被捕获，
      discover() 中单个坏清单会阻断全部子插件加载。
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _ensure_under(folder: Path, base: Path) -> bool:
    """校验 folder 解析后仍在 base 之下，防路径穿越（如 .. / 符号链接）"""
    try:
        folder.resolve().relative_to(base.resolve())
        return True
    except (ValueError, OSError):
        return False


def _write_manifest_atomic(path: Path, manifest: dict[str, Any]) -> None:
    """tmp 文件 + os.replace 原子写 lumen.json，防进程中断留下半个清单文件。

    参考 modules/regex_engine.py 的 _write_rules_atomic 模式；直接 write_text
    在写入中途崩溃/断电会留下截断 JSON，下次启动被当作"损坏清单"跳过加载。
    """
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=4), encoding="utf-8"
    )
    os.replace(temp, path)

DEFAULT_MANIFEST = {
    "name": "",
    "version": "1.0.0",
    "author": "unknown",
    "desc": "",
    "load": True,
    "priority": "main",
    "min_v": "",
    "dependencies": [],  # 第三方 pip 依赖声明，如 ["openai>=1.0.0", "pillow"]
}


class SubPlugin:
    """已加载子插件的运行时记录"""

    def __init__(self, folder: Path, manifest: dict[str, Any]) -> None:
        self.folder = folder
        self.manifest = manifest
        # 低危：name 可能被恶意/畸形 lumen.json 写成数字/列表/含路径字符的字符串，
        # 非法时回退目录名，保证后续以 name 为键的字典操作与文件路径安全
        raw_name = manifest.get("name")
        self.name: str = raw_name if _is_safe_name(raw_name) else folder.name
        self.module: Any = None
        self.context: LumenContext | None = None
        self.loaded = False
        self.error: str = ""
        self.missing_deps: list[str] = []
        self.missing_modules: list[str] = []


class SubPluginManager:
    """子插件发现、加载、卸载与热重载"""

    def __init__(self, plugin: "LumenBridgePlugin") -> None:
        self.plugin = plugin
        self.logger = getattr(plugin, "_tee_logger", None) or plugin.logger
        self.plugins_dir = Path(plugin.data_folder) / "plugins"
        self.plugins_dir.mkdir(parents=True, exist_ok=True)
        self.subplugins: dict[str, SubPlugin] = {}
        self._lock = threading.RLock()  # 保护 subplugins 字典并发读写

    def discover(self) -> list[SubPlugin]:
        """扫描子插件目录，补全缺失的 lumen.json"""
        found: list[SubPlugin] = []
        # 容错：plugins_dir 被外部删除时 iterdir 抛 FileNotFoundError，不应阻断插件启用
        try:
            iterdir = sorted(self.plugins_dir.iterdir())
        except OSError:
            return found
        for folder in iterdir:
            if not folder.is_dir() or folder.name.startswith((".", "_")):
                continue
            if not (folder / ENTRY_NAME).is_file():
                self.logger.warning(_t("subplugin_runtime.log.missing_entry", name=folder.name, entry=ENTRY_NAME))
                continue

            manifest_path = folder / MANIFEST_NAME
            manifest = dict(DEFAULT_MANIFEST)
            if manifest_path.is_file():
                mdata = _read_manifest_dict(manifest_path)
                if mdata is None:
                    # M28：清单文件存在但损坏（非法 JSON / 非 UTF-8 / 非对象）→
                    # 记 error 并跳过该子插件，绝不回退默认 load=True 继续执行其代码
                    self.logger.error(_t("subplugin_runtime.log.manifest_failed", name=folder.name, manifest=MANIFEST_NAME))
                    continue
                manifest.update(mdata)
            else:
                manifest["name"] = folder.name
                # 低危：原子写（tmp + os.replace），防中断留下半个清单文件
                _write_manifest_atomic(manifest_path, manifest)
                self.logger.info(_t("subplugin_runtime.log.manifest_generated", name=folder.name, manifest=MANIFEST_NAME))

            found.append(SubPlugin(folder, manifest))

        def _priority_of(sp: SubPlugin) -> int:
            # M27：priority 为 list/dict 等不可哈希值时 dict.get 会抛 TypeError，
            # 仅字符串才查优先级表，其余（含缺失）回退 1（main 段）
            raw = sp.manifest.get("priority")
            return PRIORITY_ORDER.get(raw, 1) if isinstance(raw, str) else 1

        found.sort(key=_priority_of)
        return found

    def load_all(self) -> None:
        with self._lock:
            # 清理历史遗留的字节码缓存（含已禁用子插件的），保持子插件目录整洁
            self._purge_pycache(self.plugins_dir)
            for sp in self.discover():
                if not sp.manifest.get("load", True):
                    self.logger.info(_t("subplugin_runtime.log.disabled", name=sp.name))
                    sp.loaded = False
                    sp.error = ""
                    self.subplugins[sp.name] = sp  # 保留记录供 WebUI 展示与开关
                    continue
                self._load_one(sp)
            count = sum(1 for sp in self.subplugins.values() if sp.loaded)
        self.logger.info(_t("subplugin_runtime.log.load_complete", count=count))

    def _purge_pycache(self, folder: Path) -> None:
        """递归清理目录下所有 ``__pycache__`` 字节码缓存。

        缓存是解释器自动生成的临时文件，对源码热重载分发的子插件无用；
        且 pyc 时间戳校验粒度可能只有 1 秒，热重载覆盖源码后旧缓存有被
        错误复用的风险。策略：加载前清旧缓存、加载中禁写新缓存。
        """
        try:
            for pycache in folder.rglob("__pycache__"):
                shutil.rmtree(pycache, ignore_errors=True)
        except OSError:
            pass

    def _load_one(self, sp: SubPlugin) -> bool:
        context: LumenContext | None = None
        module_name = f"lumenbridge_sub_{sp.folder.name}"
        try:
            # pip 安装新分发包后 importlib 可能仍缓存旧路径查找结果，且嵌入式
            # Python 可能未把 site-packages 加入 sys.path；刷新缓存确保新依赖可被发现
            from ..pip_manager import PipManager
            PipManager.refresh_dependency_cache()
            deps_raw = sp.manifest.get("dependencies", [])
            if deps_raw is None:
                deps = []
            elif isinstance(deps_raw, list):
                deps = [str(d) for d in deps_raw if d]
            else:
                self.logger.warning(
                    _t("subplugin_runtime.log.deps_not_list", name=sp.name)
                )
                deps = []
            if deps:
                pip_mgr = self._get_pip_manager()
                if pip_mgr is None:
                    # pip_mgr 不可用时保守拒绝加载，避免直接 ModuleNotFoundError
                    sp.loaded = False
                    sp.missing_deps = list(deps)
                    sp.missing_modules = [self._extract_module_name(d) for d in deps]
                    sp.error = _t(
                        "subplugin_runtime.error.missing_deps",
                        deps=", ".join(deps),
                    )
                    self.subplugins[sp.name] = sp
                    self.logger.warning(
                        _t("subplugin_runtime.log.missing_deps", name=sp.name, deps=", ".join(deps))
                    )
                    return False
                missing = pip_mgr.missing_dependencies(deps)
                sp.missing_deps = missing
                sp.missing_modules = [self._extract_module_name(d) for d in missing]
                if missing:
                    sp.loaded = False
                    sp.error = _t(
                        "subplugin_runtime.error.missing_deps",
                        deps=", ".join(missing),
                    )
                    self.subplugins[sp.name] = sp
                    self.logger.warning(
                        _t("subplugin_runtime.log.missing_deps", name=sp.name, deps=", ".join(missing))
                    )
                    return False
            else:
                sp.missing_deps = []
                sp.missing_modules = []

            # min_v 版本闸：声明的最低 LumenBridge 版本高于当前 → 拒绝加载
            unmet = self._unmet_min_version(sp.manifest)
            if unmet:
                sp.loaded = False
                sp.missing_deps = []
                sp.missing_modules = []
                sp.error = _t(
                    "subplugin_runtime.error.version_unmet",
                    required=unmet,
                    current=__version__,
                )
                self.subplugins[sp.name] = sp
                self.logger.warning(
                    _t(
                        "subplugin_runtime.log.version_unmet",
                        name=sp.name,
                        required=unmet,
                        current=__version__,
                    )
                )
                return False

            context = LumenContext(self.plugin, sp.name, sp.folder)

            for key in [k for k in sys.modules if k == module_name or k.startswith(module_name + ".")]:
                del sys.modules[key]
            # pyc 校验粒度可能只有 1 秒：同一秒覆盖 main.py 且长度未变时 exec_module
            # 会错误复用旧 pyc，故加载前清理全部字节码缓存强制重新编译
            self._purge_pycache(sp.folder)

            spec = importlib.util.spec_from_file_location(
                module_name, sp.folder / ENTRY_NAME
            )
            if spec is None:
                raise ImportError(
                    _t("subplugin_runtime.log.spec_load_failed", name=sp.name, entry=ENTRY_NAME)
                )
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            # 子插件为源码热重载分发，不需要 pyc；禁写字节码避免在子插件
            # 目录生成 __pycache__ 临时目录（加载完成后恢复原值）
            prev_dwb = sys.dont_write_bytecode
            sys.dont_write_bytecode = True
            try:
                spec.loader.exec_module(module)
            except Exception:
                # exec_module 失败后清理半初始化模块，避免残留破损对象
                sys.modules.pop(module_name, None)
                raise
            finally:
                sys.dont_write_bytecode = prev_dwb

            on_load = getattr(module, "on_load", None)
            if not callable(on_load):
                raise AttributeError(_t("subplugin_runtime.log.no_on_load", entry=ENTRY_NAME))
            # 标记当前加载中的上下文：子插件经 lumen.plugin.register_command 注册
            # 命令时（PicServer_Rank3 兼容路径），插件对象据此转发给正确上下文
            self.plugin.__dict__["_lumen_loading_context"] = context
            try:
                on_load(context)
            finally:
                self.plugin.__dict__.pop("_lumen_loading_context", None)

            sp.module = module
            sp.context = context
            sp.loaded = True
            sp.error = ""
            sp.missing_deps = []
            sp.missing_modules = []
            self.subplugins[sp.name] = sp
            self.logger.info(
                _t("subplugin_runtime.log.load_success", name=sp.name, version=sp.manifest.get('version', '?'), desc=sp.manifest.get('desc', ''))
            )
            return True
        except Exception:
            sp.loaded = False
            raw_error = traceback.format_exc()
            sp.missing_modules = self._parse_missing_modules_from_error(raw_error)
            sp.error = self._humanize_error(raw_error, sp.missing_modules)
            # on_load 抛异常前可能已注册事件监听器，必须清理避免泄漏
            if context is not None:
                try:
                    context._cleanup()
                except Exception as e:
                    self.logger.warning(_t("subplugin_runtime.log.cleanup_exception", name=sp.name, error=e))
            sys.modules.pop(module_name, None)
            # 子模块（exec_module 中 import 的本地模块）也一并清理，避免破损对象残留
            for key in [k for k in sys.modules if k.startswith(module_name + ".")]:
                del sys.modules[key]
            self.subplugins[sp.name] = sp
            self.logger.error(_t("subplugin_runtime.log.load_failed", name=sp.name, error=sp.error))
            return False
        finally:
            # 兜底清理：覆盖 on_load 内动态导入本地模块等路径意外写入的缓存
            self._purge_pycache(sp.folder)

    def _get_pip_manager(self) -> Any:
        """从主插件获取 PipManager（复用其双检锁构造，避免并发竞态）"""
        try:
            return self.plugin._get_pip_manager()
        except Exception:
            return None

    @staticmethod
    def _extract_module_name(package_spec: str) -> str:
        """从 'openai>=1.0.0' 提取 'openai'"""
        name = re.split(r"[<>=!~;\[]", package_spec.strip(), maxsplit=1)[0].strip()
        return name

    @staticmethod
    def _parse_missing_modules_from_error(tb_text: str) -> list[str]:
        """从异常 traceback 解析 ModuleNotFoundError 缺失的模块名"""
        modules: list[str] = []
        for m in re.finditer(r"No module named ['\"]([^'\"]+)['\"]", tb_text):
            mod = m.group(1).split(".")[0]
            if mod and mod not in modules:
                modules.append(mod)
        return modules

    def _humanize_error(self, raw_error: str, missing_modules: list[str]) -> str:
        """将 ModuleNotFoundError 翻译为友好提示，其它错误保留原文"""
        if not missing_modules:
            return raw_error
        friendly = _t("subplugin_runtime.error.missing_module", modules=", ".join(missing_modules))
        return f"{friendly}\n\n{raw_error}"

    def unload_all(self) -> None:
        with self._lock:
            for name in list(self.subplugins):
                self._unload_one(name)

    def _unload_one(self, name: str) -> bool:
        # 持锁贯穿 unload 流程，避免 status_* 在 on_unload 进行中观察到中间态
        with self._lock:
            sp = self.subplugins.get(name)
            if not sp:
                return False
            try:
                on_unload = getattr(sp.module, "on_unload", None)
                if callable(on_unload) and sp.context:
                    on_unload(sp.context)
            except Exception as e:
                self.logger.warning(_t("subplugin_runtime.log.on_unload_exception", name=name, error=e))
            if sp.context:
                try:
                    sp.context._cleanup()
                except Exception as e:
                    self.logger.warning(_t("subplugin_runtime.log.cleanup_exception", name=name, error=e))
            sp.loaded = False
            # H15：清掉 sys.modules 中的子插件模块及其全部子模块，
            # 释放模块级对象引用；否则 compat 注册的命令包装、全局注册表里
            # 持有的模块对象等绑定永不释放，热重载后新旧模块并存
            module_name = f"lumenbridge_sub_{sp.folder.name}"
            sys.modules.pop(module_name, None)
            for key in [k for k in sys.modules if k.startswith(module_name + ".")]:
                del sys.modules[key]
            self.subplugins.pop(name, None)
            return True

    def reload_all(self) -> int:
        """热重载全部子插件，返回加载成功数量（全程持 _lock 避免中间态被并发观察）"""
        with self._lock:
            for name in list(self.subplugins):
                self._unload_one(name)
            for sp in self.discover():
                if not sp.manifest.get("load", True):
                    self.logger.info(_t("subplugin_runtime.log.disabled", name=sp.name))
                    sp.loaded = False
                    sp.error = ""
                    self.subplugins[sp.name] = sp
                    continue
                self._load_one(sp)
            return sum(1 for sp in self.subplugins.values() if sp.loaded)

    def reload_one(self, name: str) -> bool:
        """热重载单个子插件（全程持 _lock 避免与并发 install/uninstall 竞态）"""
        # 防路径穿越：拒绝包含 . / \\ 等路径字符的输入
        if not _is_safe_name(name):
            return False
        with self._lock:
            sp = self.subplugins.get(name)
            folder = sp.folder if sp else None
            if folder is None:
                if self.plugins_dir.is_dir():
                    for d in self.plugins_dir.iterdir():
                        if not d.is_dir():
                            continue
                        mf = d / MANIFEST_NAME
                        if mf.is_file():
                            mdata = _read_manifest_dict(mf)
                            if mdata and mdata.get("name") == name:
                                folder = d
                                break
                if folder is None:
                    folder = self.plugins_dir / name
                # 二次校验：防符号链接等绕过
                if not _ensure_under(folder, self.plugins_dir):
                    return False
            if sp:
                self._unload_one(name)
            if not folder.is_dir() or not (folder / ENTRY_NAME).is_file():
                return False
            manifest = dict(DEFAULT_MANIFEST)
            manifest_path = folder / MANIFEST_NAME
            if manifest_path.is_file():
                mdata = _read_manifest_dict(manifest_path)
                if mdata is None:
                    # M28 口径：清单存在但损坏 → 记 error 并拒绝重载，
                    # 绝不回退默认 load=True 继续执行其代码
                    self.logger.error(_t("subplugin_runtime.log.manifest_failed", name=folder.name, manifest=MANIFEST_NAME))
                    return False
                manifest.update(mdata)
            # H13：尊重 manifest 的 load:false 禁用开关——
            # 禁用的插件不允许经热重载强行加载（此前 reload_one 无视该开关）
            if not manifest.get("load", True):
                self.logger.warning(f"[子插件] {name} 插件已被禁用，拒绝重载")
                return False
            new_sp = SubPlugin(folder, manifest)
            return self._load_one(new_sp)

    def set_enabled(self, name: str, enable: bool) -> bool:
        """开关子插件（写回清单 load 字段）"""
        # 防路径穿越
        if not _is_safe_name(name):
            return False
        with self._lock:
            sp = self.subplugins.get(name)
            folder = sp.folder if sp else self.plugins_dir / name
            # 二次校验：folder 必须在 plugins_dir 之下
            if not _ensure_under(folder, self.plugins_dir):
                return False
            manifest_path = folder / MANIFEST_NAME
            if not manifest_path.is_file():
                return False
            try:
                manifest = _read_manifest_dict(manifest_path)
                if manifest is None:
                    self.logger.error(_t("subplugin_runtime.log.manifest_save_failed", name=name, error="invalid manifest"))
                    return False
                manifest["load"] = enable
                # 低危：原子写（tmp + os.replace），防中断留下半个清单文件
                _write_manifest_atomic(manifest_path, manifest)
                if sp:
                    sp.manifest["load"] = enable
            except OSError as e:
                self.logger.error(_t("subplugin_runtime.log.manifest_save_failed", name=name, error=e))
                return False
            # M31：禁用不能只写标志——当前已加载的实例必须卸载，
            # 否则 load=false 的插件仍持续运行（事件回调/定时任务照常触发）
            # （_unload_one 可重入取锁；其移除记录后按 load_all 禁用分支口径补回，
            #   供 WebUI 展示与后续重新启用）
            if not enable and sp is not None and sp.loaded:
                self._unload_one(name)
                sp.loaded = False
                sp.error = ""
                self.subplugins[name] = sp
        if enable:
            self.logger.info(_t("subplugin_runtime.log.toggle_enabled", name=name))
        else:
            self.logger.info(_t("subplugin_runtime.log.toggle_disabled", name=name))
        return True

    def status_lines(self) -> list[dict[str, Any]]:
        """线程安全快照，供 WebUI 读取"""
        with self._lock:
            return [
                {
                    "name": sp.name,
                    "version": sp.manifest.get("version", ""),
                    "author": sp.manifest.get("author", ""),
                    "desc": sp.manifest.get("desc", ""),
                    "loaded": sp.loaded,
                    "enabled": sp.manifest.get("load", True),
                    "error": sp.error,
                    "missing_deps": list(sp.missing_deps),
                    "missing_modules": list(sp.missing_modules),
                }
                for sp in self.subplugins.values()
            ]

    def status_text_lines(self) -> list[str]:
        """文本格式状态行，供 /lumen plugins 命令输出"""
        with self._lock:
            lines: list[str] = []
            for sp in self.subplugins.values():
                if sp.loaded:
                    state = _t("subplugin_runtime.log.status_enabled")
                elif sp.missing_deps:
                    state = _t("subplugin_runtime.log.status_missing_deps")
                else:
                    state = _t("subplugin_runtime.log.status_failed")
                line = f"{sp.name} v{sp.manifest.get('version', '?')} [{state}] - {sp.manifest.get('desc', '')}"
                if sp.missing_deps:
                    line += f"  ← {_t('subplugin_runtime.log.missing_deps_short', deps=', '.join(sp.missing_deps))}"
                lines.append(line)
            return lines or [_t("subplugin_runtime.log.status_empty")]

    @staticmethod
    def _version_tuple(v: str) -> tuple[int, ...]:
        # 与 marketplace 的解析规则一致：仅去前缀 v/V，每段只取前导数字
        parts: list[int] = []
        for seg in str(v or "0").lstrip("vV").split("."):
            m = re.match(r"(\d+)", seg)
            parts.append(int(m.group(1)) if m else 0)
        return tuple(parts or [0])

    def _unmet_min_version(self, manifest: dict[str, Any]) -> str:
        """检查 lumen.json 的 min_v：返回不满足的版本号（满足/未声明返回 ""）。

        非字符串/畸形 min_v 一律视为未声明（宽松容错），避免畸形清单阻断加载。
        """
        required = manifest.get("min_v")
        if not isinstance(required, str):
            return ""
        required = required.strip()
        if not required:
            return ""
        if self._version_tuple(required) > self._version_tuple(__version__):
            return required
        return ""

    def install_from_zip(self, zip_path: str | Path) -> tuple[bool, str, str]:
        """从 ZIP 安装（或升级）子插件，返回 (成功, 消息, 插件名)。

        ZIP 根目录可直接含 main.py 或包一层文件夹；同名插件需版本更高才覆盖。
        """
        zip_path = Path(zip_path)
        if not zip_path.is_file():
            return False, _t("subplugin_runtime.log.install_zip_not_exist"), ""
        tmp_dir = Path(tempfile.mkdtemp(prefix="lumen_install_"))
        try:
            try:
                with zipfile.ZipFile(zip_path) as zf:
                    tmp_resolved = tmp_dir.resolve()
                    infos = zf.infolist()
                    # ZIP 炸弹防护一：条目数与 ZIP 头声明大小预检（快速失败）
                    if len(infos) > _MAX_ZIP_ENTRIES:
                        return False, _t("subplugin_runtime.log.install_zip_limit"), ""
                    declared_total = 0
                    for info in infos:
                        if info.is_dir():
                            continue
                        if info.file_size > _MAX_ZIP_FILE_BYTES:
                            return False, _t("subplugin_runtime.log.install_zip_limit"), ""
                        declared_total += info.file_size
                        # 声明体积也可能被伪造，超限即拒绝
                        if declared_total > _MAX_ZIP_TOTAL_BYTES:
                            return False, _t("subplugin_runtime.log.install_zip_limit"), ""
                    # ZIP 炸弹防护二：流式解压并按实际写入字节累计。
                    # info.file_size 来自 ZIP 头可被伪造，预检不足以防高压缩比
                    # 炸弹；逐条目 64KB 块读写，超限立即中止，不再使用 extractall。
                    # 已知限制（M30 备注）：解压在调用线程同步执行，超大 ZIP 会
                    # 阻塞调用方（WebUI 已在后台线程调用；主线程调用方需自行注意）
                    total_written = 0
                    for info in infos:
                        # 防路径穿越：用 relative_to 而非字符串前缀匹配
                        target = (tmp_dir / info.filename).resolve()
                        try:
                            target.relative_to(tmp_resolved)
                        except ValueError:
                            return False, _t("subplugin_runtime.log.install_path_traversal"), ""
                        if info.is_dir():
                            target.mkdir(parents=True, exist_ok=True)
                            continue
                        target.parent.mkdir(parents=True, exist_ok=True)
                        entry_written = 0
                        with zf.open(info) as src, open(target, "wb") as dst:
                            while True:
                                chunk = src.read(64 * 1024)
                                if not chunk:
                                    break
                                entry_written += len(chunk)
                                total_written += len(chunk)
                                # 单文件 / 总量任一超限立即中止
                                if entry_written > _MAX_ZIP_FILE_BYTES or total_written > _MAX_ZIP_TOTAL_BYTES:
                                    return False, _t("subplugin_runtime.log.install_zip_limit"), ""
                                dst.write(chunk)
            except zipfile.BadZipFile:
                return False, _t("subplugin_runtime.log.install_not_zip"), ""
            except RuntimeError:
                # M29：加密 ZIP 在 zf.open/read 时抛 RuntimeError
                #（"File ... is encrypted, password required"）等不受支持的情况
                return False, "ZIP 已加密或不支持，无法安装", ""

            root = None
            if (tmp_dir / ENTRY_NAME).is_file():
                root = tmp_dir
            else:
                candidates = [d for d in tmp_dir.iterdir() if d.is_dir() and (d / ENTRY_NAME).is_file()]
                if len(candidates) == 1:
                    root = candidates[0]
            if root is None:
                return False, _t("subplugin_runtime.log.install_no_entry", entry=ENTRY_NAME), ""

            manifest = dict(DEFAULT_MANIFEST)
            manifest_path = root / MANIFEST_NAME
            if manifest_path.is_file():
                mdata = _read_manifest_dict(manifest_path)
                if mdata is None:
                    return False, _t("subplugin_runtime.log.install_manifest_failed", manifest=MANIFEST_NAME), ""
                manifest.update(mdata)
            name = manifest.get("name") or (root.name if root != tmp_dir else zip_path.stem)
            # 防路径穿越：恶意 ZIP 可在 lumen.json 写 "name": "../evil"
            if not _is_safe_name(name):
                return False, _t("subplugin_runtime.log.install_invalid_name", name=name), ""
            manifest["name"] = name
            # min_v 版本闸：安装期即拦截（加载期 _load_one 双闸兜底，防手改清单）
            unmet = self._unmet_min_version(manifest)
            if unmet:
                return False, _t(
                    "subplugin_runtime.log.version_unmet",
                    name=name,
                    required=unmet,
                    current=__version__,
                ), name

            dest = self.plugins_dir / name
            # 二次校验：防符号链接绕过
            if not _ensure_under(dest, self.plugins_dir):
                return False, _t("subplugin_runtime.log.install_path_traversal"), ""
            if dest.exists():
                old_manifest_path = dest / MANIFEST_NAME
                old_version = "0"
                if old_manifest_path.is_file():
                    old_data = _read_manifest_dict(old_manifest_path)
                    if old_data is not None:
                        old_version = str(old_data.get("version", "0") or "0")
                new_version = manifest.get("version", "0")
                # 低危：旧目录无清单（old=0）且新包 version 缺失/解析为 0 时，
                # 0 视为"未知版本"放行升级（允许覆盖安装），不再被 0<=0 卡死
                new_tuple = self._version_tuple(new_version)
                if new_tuple != (0,) and new_tuple <= self._version_tuple(old_version):
                    return False, _t("subplugin_runtime.log.install_version_too_low", name=name, old=old_version, new=new_version), name
                # 升级：先卸载；备份用户数据文件 → 全量替换目录（清掉已删除/改名的旧代码）→ 回填数据文件
                # _lock 为 RLock（_unload_one 内部取锁可重入）：升级全程持锁，
                # 防并发 install/uninstall/reload 在备份-rmtree-回填中间态抢入
                with self._lock:
                    self._unload_one(name)
                    _data_suffixes = {".json", ".db", ".sqlite", ".sqlite3", ".txt", ".yaml", ".yml", ".csv", ".log"}
                    preserved: dict[str, Path] = {}
                    for old in dest.iterdir():
                        if old.is_file() and old.suffix.lower() in _data_suffixes:
                            preserved[old.name] = old
                    backup_dir = Path(tempfile.mkdtemp(prefix="lumen_upgrade_"))
                    upgrade_ok = False
                    try:
                        for fname, fpath in preserved.items():
                            try:
                                shutil.copy2(fpath, backup_dir / fname)
                            except OSError:
                                pass
                        shutil.rmtree(dest, ignore_errors=True)
                        if dest.exists():
                            # 清理半途失败：把已备份的数据文件回填旧目录，防丢失
                            for fname in preserved:
                                backed = backup_dir / fname
                                if backed.is_file():
                                    try:
                                        shutil.copy2(backed, dest / fname)
                                    except OSError:
                                        pass
                            return False, _t("subplugin_runtime.log.install_failed_cleanup", name=name), name
                        try:
                            shutil.copytree(root, dest)
                        except OSError:
                            # 新代码写入失败（磁盘满/权限）：回填用户数据文件到半成品目录
                            for fname in preserved:
                                backed = backup_dir / fname
                                if backed.is_file():
                                    try:
                                        shutil.copy2(backed, dest / fname)
                                    except OSError:
                                        pass
                            raise
                        # 用户数据优先于新包自带同名文件
                        for fname in preserved:
                            backed = backup_dir / fname
                            if backed.is_file():
                                try:
                                    shutil.copy2(backed, dest / fname)
                                except OSError as e:
                                    self.logger.warning(
                                        _t("subplugin_runtime.log.upgrade_restore_failed", name=name, file=fname, error=e)
                                    )
                        upgrade_ok = True
                    finally:
                        if upgrade_ok:
                            shutil.rmtree(backup_dir, ignore_errors=True)
                        elif backup_dir.is_dir():
                            # 升级未完成：保留备份目录供手动找回数据，绝不删除
                            self.logger.warning(
                                _t("subplugin_runtime.log.upgrade_backup_kept", name=name, path=str(backup_dir))
                            )
                action = _t("subplugin_runtime.log.install_action_upgrade", version=manifest.get('version', '?'))
            else:
                shutil.copytree(root, dest)
                action = _t("subplugin_runtime.log.install_action_install", version=manifest.get('version', '?'))

            # 低危：原子写（tmp + os.replace），防中断留下半个清单文件
            _write_manifest_atomic(dest / MANIFEST_NAME, manifest)

            sp = SubPlugin(dest, manifest)
            if manifest.get("load", True):
                # _load_one 会写 subplugins 字典，持锁调用避免竞态
                with self._lock:
                    ok = self._load_one(sp)
                if not ok:
                    return True, _t("subplugin_runtime.log.install_load_failed", name=name, action=action), name
            else:
                with self._lock:
                    self.subplugins[name] = sp
            self.logger.info(_t("subplugin_runtime.log.install_web_success", name=name, action=action))
            return True, _t("subplugin_runtime.log.install_success_msg", name=name, action=action), name
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def uninstall(self, name: str) -> tuple[bool, str]:
        """卸载并删除子插件目录"""
        # 防路径穿越
        if not _is_safe_name(name):
            return False, _t("subplugin_runtime.log.uninstall_path_illegal")
        folder = None
        # 持锁贯穿 unload + rmtree，避免并发 install 抢先创建新目录被误删
        with self._lock:
            sp = self.subplugins.get(name)
            if sp:
                folder = sp.folder
                self._unload_one(name)
            else:
                candidate = self.plugins_dir / name
                if candidate.is_dir():
                    folder = candidate
            if folder is None or not folder.is_dir():
                return False, _t("subplugin_runtime.log.uninstall_not_exist", name=name)
            # 安全检查：目录必须位于 plugins_dir 下
            if not _ensure_under(folder, self.plugins_dir):
                return False, _t("subplugin_runtime.log.uninstall_path_illegal")
            # rmtree 可能因 Windows 文件占用失败
            try:
                shutil.rmtree(folder)
            except OSError as e:
                self.logger.warning(_t("subplugin_runtime.log.uninstall_rmtree_failed", name=name, error=e))
                return False, _t("subplugin_runtime.log.uninstall_rmtree_failed", name=name, error=e)
        self.logger.info(_t("subplugin_runtime.log.uninstall_success", name=name))
        return True, _t("subplugin_runtime.log.uninstall_success_msg", name=name)
