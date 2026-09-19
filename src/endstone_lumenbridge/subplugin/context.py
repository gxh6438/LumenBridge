"""子插件上下文 API。

每个子插件加载时获得独立的 :class:`LumenContext` 实例（惯例命名 ``lumen``），
提供事件总线、共享变量池、OneBot 适配器、MC 桥接与 Endstone 全 API 直达。
"""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from ..i18n import get_i18n, t as _t
from ..onebot import message as msgbuilder
from ..onebot import packets as packbuilder
from .. import __version__

if TYPE_CHECKING:
    from ..plugin import LumenBridgePlugin


# 子插件命令面板：BDS 命令表在插件加载时冻结，无法运行期注册新命令，
# 由该面板在启动时并入 LumenBridgePlugin.commands 预声明，运行期仅绑定 handler。
COMMAND_PALETTE_PATH = Path("plugins/lumenbridge/data/command_palette.json")

# 面板读-改-写锁：防并发注册命令时互相覆盖条目
_PALETTE_LOCK = threading.Lock()

# 命令注册表查重-写入锁：防并发注册同名命令互相覆盖
_COMMAND_REGISTRY_LOCK = threading.Lock()

_PALETTE_NAME_RE = re.compile(r"[a-z0-9_\-]+")
# Endstone usage 合法参数 token：(a|b) 枚举组、<参数>/[参数]（可带 ": 类型"）
_USAGE_TOKEN_RE = re.compile(
    r"(?:\([A-Za-z0-9_|]+\))?[<\[][A-Za-z0-9_]+(?::\s*[A-Za-z][A-Za-z0-9_]*)?[>\]]"
)


def read_command_palette() -> dict[str, dict[str, Any]]:
    """读取启动命令面板（损坏/缺失返回空 dict，绝不抛异常）。

    损坏时先备份为 .corrupt 再返回空：read→merge→write 会整体覆写原文件，
    不备份则其他子插件的命令声明全部丢失。
    """
    try:
        data = json.loads(COMMAND_PALETTE_PATH.read_text(encoding="utf-8"))
    except Exception:
        _backup_corrupt_palette()
        return {}
    if not isinstance(data, dict):
        _backup_corrupt_palette()
        return {}
    return {str(k): v for k, v in data.items() if isinstance(v, dict)}


def _backup_corrupt_palette() -> None:
    """把损坏的面板文件改名保留为 .corrupt（失败静默，不影响主流程）。"""
    try:
        if COMMAND_PALETTE_PATH.is_file():
            COMMAND_PALETTE_PATH.replace(
                COMMAND_PALETTE_PATH.with_name(COMMAND_PALETTE_PATH.name + ".corrupt")
            )
    except OSError:
        pass


def write_command_palette(palette: dict[str, dict[str, Any]]) -> None:
    """全量写回启动命令面板（原子替换）。"""
    try:
        COMMAND_PALETTE_PATH.parent.mkdir(parents=True, exist_ok=True)
        temp = COMMAND_PALETTE_PATH.with_name(COMMAND_PALETTE_PATH.name + ".tmp")
        temp.write_text(json.dumps(palette, ensure_ascii=False, indent=4), encoding="utf-8")
        temp.replace(COMMAND_PALETTE_PATH)
    except Exception:
        pass  # 只读文件系统等：面板写失败不阻断子插件加载


def default_usage(name: str) -> str:
    """命令的默认 usage：/{name} [args: message]。

    参数必须是 <x>/[x]/(a|b) 形式，字面 "..." 会触发 Syntax Error；
    [args: message] 为官方贪心字符串参数。
    """
    return f"/{name} [args: message]"


def sanitize_usages(name: str, usages: list[str] | None) -> list[str]:
    """清洗 usage 列表，剔除 Endstone 无法解析的非法项，全无效时回退默认。

    非法 usage 会导致 Endstone 启动时 "Unable to register command"：
    须以 "/{name}" 开头，其余只能由参数 token 组成。
    """
    prefix = f"/{name}"
    valid: list[str] = []
    for raw in usages or []:
        text = str(raw).strip()
        if not text.startswith(prefix):
            continue
        rest = text[len(prefix):].strip()
        # 逐 token 摘除合法参数，若剩余非空白则视为非法 usage
        leftover = _USAGE_TOKEN_RE.sub("", rest).strip()
        if not leftover:
            valid.append(text)
    return valid or [default_usage(name)]


def _clean_command_identifiers(values: list[str] | None) -> list[str]:
    """清洗 alias / 权限名为合法标识符（与命令名同规），非法项直接剔除。

    非法 alias / 权限名会导致 Endstone 启动时 "Unable to register command"。
    """
    out: list[str] = []
    for raw in values or []:
        v = str(raw).strip().lower()
        if v and _PALETTE_NAME_RE.fullmatch(v) and v not in out:
            out.append(v)
    return out


def add_command_palette_entry(
    name: str,
    description: str = "",
    usages: list[str] | None = None,
    aliases: list[str] | None = None,
    permissions: list[str] | None = None,
) -> None:
    """向启动面板登记一个子插件命令（下次服务器启动时并入类级 commands）。"""
    with _PALETTE_LOCK:
        palette = read_command_palette()
        entry: dict[str, Any] = {
            "description": str(description or f"LumenBridge subplugin command /{name}"),
            "usages": sanitize_usages(name, usages),
        }
        clean_aliases = _clean_command_identifiers(aliases)
        if clean_aliases:
            entry["aliases"] = clean_aliases
        clean_perms = _clean_command_identifiers(permissions)
        if clean_perms:
            entry["permissions"] = clean_perms
        palette[str(name)] = entry
        write_command_palette(palette)


def merge_command_palette_into(
    commands: dict[str, dict[str, Any]],
    allowed_permissions: set[str] | None = None,
) -> int:
    """把启动面板中的命令并入目标 commands 字典（如 LumenBridgePlugin.commands）。

    必须在插件模块导入期调用：endstone 在 ``ep.load()`` 后立即快照类级
    commands 构造 Command 对象，之后再改无效。条目损坏只跳过该条，不抛异常。
    ``allowed_permissions``：已声明权限名集合，未在其中的一律剔除（未声明的权限会让注册失败）。
    """
    merged = 0
    try:
        palette = read_command_palette()
    except Exception:
        return 0
    for raw_name, entry in palette.items():
        name = str(raw_name).strip().lower()
        # 跳过非法名与主命令冲突，防止覆盖 /lumen
        if not name or not _PALETTE_NAME_RE.fullmatch(name) or name in commands:
            continue
        try:
            clean: dict[str, Any] = {
                "description": str(entry.get("description") or f"LumenBridge subplugin command /{name}"),
                # 清洗面板中的 usage，非法项会导致 Endstone 注册失败
                "usages": sanitize_usages(name, entry.get("usages")),
            }
            aliases = _clean_command_identifiers(entry.get("aliases"))
            # 剔除与其他已并入命令的 name/alias 冲突的别名
            taken = set(commands) | {a for c in commands.values() for a in (c.get("aliases") or [])}
            aliases = [a for a in aliases if a not in taken and a != name]
            if aliases:
                clean["aliases"] = aliases
            # 权限名未在插件 permissions 声明会导致注册失败，面板默认不带权限
            permissions = _clean_command_identifiers(entry.get("permissions"))
            if allowed_permissions is not None:
                permissions = [p for p in permissions if p in allowed_permissions]
            if permissions:
                clean["permissions"] = permissions
            commands[name] = clean
            merged += 1
        except Exception:
            continue
    return merged


class EnvPool:
    """全局共享变量池。

    ``main_group`` 在事件回调中返回当前来源群、否则返回配置首个主群，兼容旧插件
    ``if gid == env.get("main_group")`` 风格；广播全部主群用 ``main_groups``。
    """

    def __init__(self, plugin: "LumenBridgePlugin") -> None:
        self._plugin = plugin
        self._data: dict[str, Any] = {}
        self._lock = threading.RLock()
        self._ctx = threading.local()

    def set_current_group(self, gid: int, source: Any = None) -> None:
        """事件分发前设置当前来源群号（线程本地），仅主群才设置以兼容旧插件过滤。

        适配器自身群列表同样视为"主群"；未配置群列表时接受任意来源群。
        """
        # config_manager 在 reload 过程中可能为 None
        cm = self._plugin.config_manager
        if cm is None:
            return
        if gid in cm.main_groups:
            self._ctx.group = gid
            return
        if source is not None:
            groups = list(getattr(source, "groups", None) or [])
            if (gid in groups) or not groups:
                self._ctx.group = gid

    def clear_current_group(self) -> None:
        """派发结束后清除当前来源群号"""
        try:
            del self._ctx.group
        except AttributeError:
            pass

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            if key == "main_group":
                gid = getattr(self._ctx, "group", None)
                if gid is not None:
                    return gid
                cm = self._plugin.config_manager
                # reload 中间态无配置管理器时返回 None 而非 0，避免 0 被当有效群号
                return cm.main_group if cm is not None else None
            if key == "main_groups":
                cm = self._plugin.config_manager
                return cm.main_groups if cm is not None else []
            if key == "admin_qq":
                cm = self._plugin.config_manager
                return cm.admin_qq if cm is not None else []
            return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self._data[key] = value
        bus = getattr(self._plugin, "bus", None)
        if bus is not None:
            bus.emit(f"env.update.{key}", value)
            bus.emit("env.update", key, value)


class PrefixedLogger:
    """带子插件名前缀的日志包装器"""

    def __init__(self, logger: Any, name: str) -> None:
        self._logger = logger
        self._prefix = f"[{name}] "

    def info(self, msg: Any) -> None:
        self._logger.info(f"{self._prefix}{msg}")

    def warning(self, msg: Any) -> None:
        self._logger.warning(f"{self._prefix}{msg}")

    def error(self, msg: Any) -> None:
        self._logger.error(f"{self._prefix}{msg}")

    def debug(self, msg: Any) -> None:
        self._logger.debug(f"{self._prefix}{msg}")


class Storage:
    """子插件私有 JSON 存储（所有读写加锁，防并发写入互相覆盖或读到半截 JSON）"""

    def __init__(self, data_dir: Path) -> None:
        self.dir = data_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()  # 保护文件读写，防并发损坏

    def _resolve(self, filename: str) -> Path:
        """解析子插件存储路径并拒绝绝对路径与目录穿越。"""
        if not isinstance(filename, str) or not filename.strip():
            raise ValueError("storage filename must be a non-empty relative path")
        base = self.dir.resolve()
        candidate = (base / filename).resolve()
        try:
            candidate.relative_to(base)
        except ValueError as exc:
            raise ValueError("storage path escapes plugin data directory") from exc
        return candidate

    def read(self, filename: str, default: Any = None) -> Any:
        with self._lock:
            path = self._resolve(filename)
            if path.is_file():
                try:
                    return json.loads(path.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    # 损坏时先备份再写默认值，避免直接覆盖用户数据
                    import time as _time
                    backup = path.with_suffix(path.suffix + f".corrupt-{int(_time.time())}")
                    try:
                        path.replace(backup)
                    except OSError:
                        pass  # 备份失败时不写默认值，返回 default
                    else:
                        if default is not None:
                            path.parent.mkdir(parents=True, exist_ok=True)
                            path.write_text(json.dumps(default, ensure_ascii=False, indent=4), encoding="utf-8")
                    return default
                except OSError:
                    # I/O 错误：不覆盖文件，直接返回 default
                    return default
            if default is not None:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(default, ensure_ascii=False, indent=4), encoding="utf-8")
            return default

    def write(self, filename: str, data: Any) -> None:
        with self._lock:
            path = self._resolve(filename)
            path.parent.mkdir(parents=True, exist_ok=True)
            # tmp + 原子替换，防进程中断留下半个 JSON
            temp = path.with_name(path.name + ".tmp")
            temp.write_text(json.dumps(data, ensure_ascii=False, indent=4), encoding="utf-8")
            temp.replace(path)

    def path(self, filename: str = "") -> str:
        return str(self._resolve(filename)) if filename else str(self.dir.resolve())


class MCBridge:
    """Minecraft 接口桥（支持直达 Endstone 全部事件）"""

    _EVENT_MAP = {
        "onJoin": "mc.player_join",
        "onLeft": "mc.player_left",
        "onChat": "mc.player_chat",
        "onDeath": "mc.player_death",
    }

    def __init__(self, plugin: "LumenBridgePlugin") -> None:
        self._plugin = plugin
        # 原生 listener 为 per-event 单例，回调挂可变分发表（详见 _listen_endstone）
        self._endstone_dispatch: dict[str, list[Callable[..., Any]]] = {}
        # 已注册的原生 listener 登记表：(event_name, listener)
        self._endstone_listeners: list[tuple[str, Any]] = []
        # 经内部总线注册的回调，卸载时必须 off，防热重载后重复触发
        self._bus_handlers: list[tuple[str, Callable[..., Any]]] = []

    def listen(self, event_name: str, callback: Callable[..., Any]) -> bool:
        """监听游戏事件：兼容别名或任意 Endstone 事件类名（回调收原生事件对象）。"""
        internal = self._EVENT_MAP.get(event_name)
        if internal:
            bus = getattr(self._plugin, "bus", None)
            if bus is None:
                # 主插件停用/reload 中间态：注册拒绝而非抛 AttributeError
                return False
            bus.on(internal, callback)
            self._bus_handlers.append((internal, callback))
            return True
        return self._listen_endstone(event_name, callback)

    def _listen_endstone(self, event_name: str, callback: Callable[..., Any]) -> bool:
        """动态注册原生 Endstone 事件监听（按类名反射构造 @event_handler 监听器类）。

        Endstone 未暴露 unregister_events，故同一事件仅创建一个 listener，
        回调存于可变分发表；_cleanup 清空分发表即停止分发。
        """
        try:
            import endstone.event as es_event

            event_cls = getattr(es_event, event_name, None)
            if event_cls is None or not isinstance(event_cls, type):
                return False

            # 先保证分发表就绪：listener 注册成功后事件可能立即触发
            dispatch = self._endstone_dispatch.setdefault(event_name, [])
            if not any(evt == event_name for evt, _l in self._endstone_listeners):
                # 闭包捕获分发表本身，_cleanup 原地清空后 listener 自动 no-op
                dispatch_table = self._endstone_dispatch

                def _handler(listener_self: Any, event: Any) -> None:  # noqa: ANN401
                    if not getattr(listener_self, "_lumen_active", True):
                        return
                    for cb in list(dispatch_table.get(event_name, ())):
                        cb(event)

                _handler.__annotations__ = {"event": event_cls, "return": None}
                decorated = es_event.event_handler(_handler)
                listener_cls = type(f"_LumenSubListener_{event_name}", (), {"on_event": decorated})
                listener = listener_cls()
                listener._lumen_active = True  # type: ignore[attr-defined]
                self._plugin.register_events(listener)
                self._endstone_listeners.append((event_name, listener))
            dispatch.append(callback)
            return True
        except Exception:
            return False

    def runcmd(self, cmd: str) -> bool:
        """在游戏主线程执行命令，返回 dispatch_command 的真实结果。

        最多等 5 秒，超时/异常返回 False。勿在游戏主线程调用（会死锁）。
        """
        done = threading.Event()
        cancelled = threading.Event()
        box: list[bool] = [False]

        def run() -> None:
            # 超时后排队任务不再执行，避免命令延迟生效
            if cancelled.is_set():
                done.set()
                return
            try:
                box[0] = bool(
                    self._plugin.server.dispatch_command(
                        self._plugin.server.command_sender, cmd.lstrip("/")
                    )
                )
            except Exception as e:
                (getattr(self._plugin, "_tee_logger", None) or self._plugin.logger).error(_t("subplugin_runtime.log.runcmd_failed", error=e))
            finally:
                done.set()

        try:
            self._plugin.run_on_main(run)
        except Exception:
            # 调度失败（插件停用/调度器不可用）：按失败语义返回 False
            return False
        if not done.wait(timeout=5.0):
            cancelled.set()
            return False
        return box[0]

    def runcmdEx(self, cmd: str, timeout: float = 5.0) -> dict[str, Any]:
        """执行命令并捕获输出（阻塞调用线程）。不要在游戏主线程调用，否则死锁。"""
        outputs: list[str] = []
        done = threading.Event()
        cancelled = threading.Event()
        result: dict[str, Any] = {"success": False}

        def run() -> None:
            # 超时后排队任务不再执行，避免延迟副作用
            if cancelled.is_set():
                done.set()
                return
            try:
                from endstone.command import CommandSenderWrapper

                def capture(msg: Any) -> None:
                    outputs.append(
                        msg if isinstance(msg, str) else getattr(msg, "text", str(msg))
                    )

                # Endstone 0.11：on_message / on_error 双回调捕获正常与错误输出
                sender = CommandSenderWrapper(
                    self._plugin.server.command_sender,
                    on_message=capture,
                    on_error=capture,
                )
                result["success"] = self._plugin.server.dispatch_command(
                    sender, cmd.lstrip("/")
                )
            except Exception as e:
                outputs.append(_t("subplugin_runtime.log.cmd_exec_exception", error=e))
            finally:
                done.set()

        try:
            self._plugin.run_on_main(run)
        except Exception:
            # 调度失败：run 不会执行，直接返回失败快照（不等满超时）
            output = re.sub(r"§.", "", "\n".join(outputs), flags=re.DOTALL).strip()
            return {"success": False, "output": output}
        if not done.wait(timeout=timeout):
            cancelled.set()
        output = re.sub(r"§.", "", "\n".join(outputs), flags=re.DOTALL).strip()
        return {"success": bool(result.get("success")), "output": output}

    def broadcast(self, message: str) -> None:
        """向全服广播消息"""
        def run() -> None:
            self._plugin.server.broadcast_message(message)

        try:
            self._plugin.run_on_main(run)
        except Exception:
            # 调度失败吞掉异常，避免杀死子插件残留线程的清理逻辑
            pass

    @property
    def online_players(self) -> list[str]:
        """在线玩家名列表（线程安全快照）。阻塞主线程最多 2 秒，勿在主线程调用。"""
        box: list[list[str]] = [[]]
        done = threading.Event()
        cancelled = threading.Event()

        def _fetch() -> None:
            if cancelled.is_set():
                done.set()
                return
            try:
                box[0] = [p.name for p in self._plugin.server.online_players]
            except Exception:
                box[0] = []
            finally:
                done.set()

        try:
            self._plugin.run_on_main(_fetch)
            if not done.wait(timeout=2.0):
                cancelled.set()
                return []
        except Exception:
            return []
        return box[0]


class WebBridge:
    """子插件 Web 扩展接口（配置表单 / REST API / 自定义页面）。"""

    def __init__(self, plugin: "LumenBridgePlugin", name: str, folder_name: str) -> None:
        self._plugin = plugin
        self._name = name
        self._folder = folder_name
        self._logger = PrefixedLogger(getattr(plugin, "_tee_logger", None) or plugin.logger, name)
        # webui 未初始化时的待注册队列（解决加载顺序问题）
        self._pending_configs: list[tuple[str, Any]] = []
        self._pending_pages: list[tuple[str, str]] = []
        self._pending_apis: list[tuple[str, str, Any, bool]] = []
        self._pending_lock = threading.Lock()
        # 已注册的 Web 扩展记录（api/page/config），供卸载时逐项撤销
        self._registered_apis: list[tuple[str, str]] = []
        self._registered_pages: list[str] = []
        self._registered_configs: list[str] = []

    @property
    def _webui(self) -> Any:
        return getattr(self._plugin, "webui", None)

    def _flush_pending(self) -> None:
        """webui 就绪后补注册暂存的 config / page / api（由插件主类调用）"""
        webui = self._webui
        if not webui:
            return
        # 先取快照再清空：避免遍历期间并发 append 丢失或抛 RuntimeError
        with self._pending_lock:
            configs = self._pending_configs[:]
            pages = self._pending_pages[:]
            apis = self._pending_apis[:]
            self._pending_configs.clear()
            self._pending_pages.clear()
            self._pending_apis.clear()
        ext_lock = getattr(webui, "_ext_lock", None)
        for name, schema in configs:
            if ext_lock is not None:
                with ext_lock:
                    webui.plugins_config_schema[name] = schema
            else:
                webui.plugins_config_schema[name] = schema
        for page_item in pages:
            # 兼容旧长度（title, rel_path）与新长度（title, rel_path, tab, icon）
            title, rel_path = page_item[0], page_item[1]
            tab = page_item[2] if len(page_item) > 2 else False
            icon = page_item[3] if len(page_item) > 3 else ""
            webui.register_custom_page(self._name, self._folder, title, rel_path, tab, icon)
        for method, path, handler, need_auth in apis:
            webui.register_api(method, path, handler, need_auth)

    def createConfig(self, name: str | None = None) -> Any:
        webui = self._webui
        target_name = name or self._name
        if str(target_name) not in self._registered_configs:
            # 先记录 schema 名供卸载撤销（builder 未注册时撤销为无害 pop）
            self._registered_configs.append(str(target_name))
        if webui:
            return webui.create_config(target_name)
        from ..webui.configform import ConfigFormBuilder

        def _defer_register(builder: Any) -> None:
            with self._pending_lock:
                self._pending_configs.append((builder.name, builder.to_schema()))

        return ConfigFormBuilder(target_name, _defer_register)

    create_config = createConfig

    def registerApi(self, method: str, path: str, handler: Any, need_auth: bool = True) -> None:
        webui = self._webui
        # 与 webui.register_api 内部构造的完整路径保持一致，供卸载时按键撤销
        full = "/api/plugin" + (path if isinstance(path, str) and path.startswith("/") else "/" + str(path))
        if not need_auth:
            # 免鉴权 API 任何能连到端口的客户端都可调用，注册时即告警
            self._logger.warning(
                f"registerApi: {str(method).upper()} {full} 以 need_auth=False 注册，"
                f"未持 token 的客户端也可访问，请确认安全风险"
            )
        if webui:
            webui.register_api(method, path, handler, need_auth)
        else:
            with self._pending_lock:
                self._pending_apis.append((method, path, handler, need_auth))
        self._registered_apis.append((str(method).upper(), full))

    register_api = registerApi

    def registerPage(
        self, title: str, relative_path: str, tab: bool = False, icon: str = ""
    ) -> None:
        """注册 WebUI 自定义页面。

        tab=False（默认）：进移动端「其它」面板与桌面侧栏；tab=True：
        额外注册为移动端底栏 tab。
        icon：传内置图标名（"model"/"bot"/"chat"/"shield"/"spark"/"gear"/
        "chart"/"home"/"user"/"users"/"server"/"database"/"map"/"box"/"gift"/
        "trophy"/"crown"/"coin"/"fire"/"zap"/"heart"/"star"/"bell"/"clock"/
        "calendar"/"music"/"image"/"search"/"link"/"lock"/"book"/"code"/
        "terminal"/"globe"）渲染同风格 SVG；传 emoji 按字符渲染（建议单个）；
        缺省用默认图标。
        注意：页面在 iframe 中经带 token 的 URL 加载，相对资源不携带 token，
        须自包含（内联样式与脚本）。主面板会注入融合基础样式并按内容高度
        自适应 iframe；如需保留背景色，用内联样式覆盖：
        <body style="background:#fff !important">。
        """
        webui = self._webui
        if webui:
            webui.register_custom_page(self._name, self._folder, title, relative_path, tab, icon)
        else:
            with self._pending_lock:
                self._pending_pages.append((title, relative_path, tab, icon))
        # url 构造与 webui.register_custom_page 内部一致，供卸载时按 url 移除
        self._registered_pages.append(f"/plugin-views/{self._folder}/{relative_path}")

    register_page = registerPage

    def _revoke_registrations(self) -> None:
        """卸载时撤销本子插件注册的全部 API / 自定义页面 / 配置表单。

        WebUI 注册表是进程级全局状态，热重载不撤销则旧 handler 残留；
        在 _ext_lock 保护下逐键删除。
        """
        with self._pending_lock:
            # 尚未 flush 的暂存注册直接丢弃（webui 未就绪即被卸载的场景）
            self._pending_configs.clear()
            self._pending_pages.clear()
            self._pending_apis.clear()
            apis = self._registered_apis[:]
            pages = set(self._registered_pages)
            configs = self._registered_configs[:]
            self._registered_apis.clear()
            self._registered_pages.clear()
            self._registered_configs.clear()
        webui = self._webui
        if not webui:
            return
        ext_lock = getattr(webui, "_ext_lock", None)

        def _apply(fn: Callable[[], None]) -> None:
            # 与 server.py 注册路径一致：写注册表须持 _ext_lock（缺失时降级直写）
            if ext_lock is not None:
                with ext_lock:
                    fn()
            else:
                fn()

        custom_apis = getattr(webui, "custom_apis", None)
        if isinstance(custom_apis, dict):
            def _drop_apis() -> None:
                for key in apis:
                    custom_apis.pop(key, None)
            _apply(_drop_apis)

        schemas = getattr(webui, "plugins_config_schema", None)
        if isinstance(schemas, dict):
            def _drop_schemas() -> None:
                for schema_name in configs:
                    schemas.pop(schema_name, None)
            _apply(_drop_schemas)

        page_list = getattr(webui, "custom_pages", None)
        if isinstance(page_list, list):
            def _drop_pages() -> None:
                page_list[:] = [
                    p for p in page_list
                    if not (isinstance(p, dict) and p.get("url") in pages)
                ]
            _apply(_drop_pages)


def _command_declared(plugin: Any, cmd_name: str) -> bool:
    """命令是否已在当前启动的 BDS 命令表内（经 plugin.get_command 查询）。"""
    try:
        get_command = getattr(plugin, "get_command", None)
        return get_command is not None and get_command(cmd_name) is not None
    except Exception:
        return False


def register_subplugin_command(
    plugin: Any,
    logger: Any,
    owner: str,
    name: str,
    handler: Callable[[Any, list[str]], bool],
    description: str = "",
    aliases: list[str] | None = None,
    usages: list[str] | None = None,
) -> bool:
    """register_command 的共享实现（LumenContext 与插件对象兼容入口共用）。

    返回 True 表示绑定成功（含「已写入面板、重启后生效」的首次注册）；
    返回 False 表示命令名非法、handler 不可调用或已被其他子插件占用。
    """
    cmd_name = str(name or "").strip().lower()
    # 命令名仅允许小写字母数字下划线连字符（与子插件名校验一致，防注入）
    if not cmd_name or not _PALETTE_NAME_RE.fullmatch(cmd_name):
        logger.warning(f"register_command: invalid command name {name!r}")
        return False
    if not callable(handler):
        logger.warning(f"register_command: handler for /{cmd_name} is not callable")
        return False

    # 查重到写入全程持锁：防并发注册同名命令时后写者覆盖先注册者的 handler
    with _COMMAND_REGISTRY_LOCK:
        registry = plugin.__dict__.setdefault("_lumen_sub_commands", {})
        if cmd_name in registry:
            return False

        owner_name = str(owner or "plugin")

        def _wrapped(sender: Any, args: list[str]) -> bool:  # noqa: ANN401
            try:
                return bool(handler(sender, list(args)))
            except Exception as e:  # noqa: BLE001
                logger.error(f"register_command: /{cmd_name} handler error: {e}")
                try:
                    sender.send_message(f"§c/{cmd_name} execution failed: {e}§r")
                except Exception:
                    pass
                return False

        # 已声明（启动时并入类级 commands）？未声明则登记面板，重启后生效
        declared = cmd_name in read_command_palette() or _command_declared(plugin, cmd_name)
        if not declared:
            add_command_palette_entry(cmd_name, description, usages, aliases=aliases)
            logger.warning(_t("subplugin_runtime.log.command_palette_pending", name=cmd_name))
            logger.info(_t("subplugin_runtime.log.command_palette_written", name=cmd_name))

        registry[cmd_name] = {"handler": _wrapped, "subplugin": owner_name}
        return True


def plugin_register_command_compat(
    plugin: Any,
    name: str,
    handler: Callable[[Any, list[str]], bool],
    description: str = "",
    aliases: list[str] | None = None,
    usages: list[str] | None = None,
) -> bool:
    """插件对象上的 register_command 兼容入口。

    子插件加载期间（loader 设置了 ``_lumen_loading_context``）转发给对应
    上下文，保证归属与卸载清理正确；其余场景以 "plugin" 归属直接注册。
    注册同时登记全局 ``_lumen_plugin_commands`` 与 context 的
    ``_registered_commands``，_cleanup 时随卸载释放。
    """
    ctx = plugin.__dict__.get("_lumen_loading_context")
    if ctx is not None:
        ok = ctx.register_command(name, handler, description, aliases, usages)
        if ok:
            plugin.__dict__.setdefault("_lumen_plugin_commands", []).append(str(name).strip().lower())
        return ok
    logger = getattr(plugin, "_tee_logger", None) or plugin.logger
    ok = register_subplugin_command(
        plugin, logger, "plugin", name, handler, description, aliases, usages
    )
    if ok:
        plugin.__dict__.setdefault("_lumen_plugin_commands", []).append(str(name).strip().lower())
    return ok


class _SchedulerWrapper:
    """Endstone 调度器代理。

    透传全部属性与方法，仅包装 run_task* 类方法：把返回的 task 记录到
    所属 context 的 ``_scheduled_tasks``，供 ``_cleanup`` 统一 cancel，
    防热重载后旧定时任务重复执行；cancel_task 同步移除记录。
    """

    def __init__(self, scheduler: Any, owner: "LumenContext") -> None:
        self._scheduler = scheduler
        self._owner = owner

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._scheduler, name)
        if not callable(attr):
            return attr
        if name.startswith("run_task"):
            def _record_and_run(*args: Any, **kwargs: Any) -> Any:
                task = attr(*args, **kwargs)
                tasks = getattr(self._owner, "_scheduled_tasks", None)
                if task is not None and tasks is not None:
                    tasks.add(task)
                return task

            return _record_and_run
        if name == "cancel_task":
            def _cancel_and_forget(*args: Any, **kwargs: Any) -> Any:
                result = attr(*args, **kwargs)
                tasks = getattr(self._owner, "_scheduled_tasks", None)
                if tasks is not None:
                    # 官方签名 cancel_task(id)：按 task_id 匹配，兼容传 task 的写法
                    ids = {id(a) for a in args}
                    for task in list(tasks):
                        if id(task) in ids or getattr(task, "task_id", None) in args:
                            tasks.discard(task)
                return result

            return _cancel_and_forget
        return attr


class LumenContext:
    """注入给每个子插件的上下文对象，提供 Endstone 全 API 直达通道。"""

    VERSION = __version__

    def __init__(self, plugin: "LumenBridgePlugin", name: str, data_dir: Path) -> None:
        self._plugin = plugin
        self.pluginName = name
        self.logger = PrefixedLogger(getattr(plugin, "_tee_logger", None) or plugin.logger, name)
        self.env = plugin.env_pool
        self.QClient = plugin.adapter
        self.msgbuilder = msgbuilder
        self.packbuilder = packbuilder
        self.mc = MCBridge(plugin)
        self.storage = Storage(data_dir)
        self.web = WebBridge(plugin, name, data_dir.name)
        self.i18n = get_i18n()
        self._handlers: list[tuple[str, Callable[..., Any]]] = []
        # 本上下文向正则引擎注册的自定义动作类型，_cleanup 时统一注销
        self._regex_actions: list[str] = []
        # 本上下文绑定的服务器命令名，_cleanup 时统一解除 handler 绑定
        self._commands: list[str] = []
        # 本上下文注册的命令名，_cleanup 时从全局 _lumen_plugin_commands 移除
        self._registered_commands: list[str] = []
        # 本上下文经 lumen.scheduler 注册的定时任务，_cleanup 逐个 cancel
        self._scheduled_tasks: set[Any] = set()
        # _cleanup 后置位：拒绝再次注册事件（旧 context 重复注册会永久泄漏 handler）
        self._disposed = False

    @property
    def _bus(self) -> Any:
        """当前事件总线；主插件停用/reload 中间态时为 None。"""
        return getattr(self._plugin, "bus", None)

    @property
    def debug(self) -> bool:
        # reload 过程中 config_manager 可能为 None
        cm = self._plugin.config_manager
        return bool(cm.debug) if cm is not None else False

    @property
    def plugin(self) -> "LumenBridgePlugin":
        """LumenBridge 插件实例（endstone.plugin.Plugin 全部能力）"""
        return self._plugin

    @property
    def server(self) -> Any:
        """Endstone Server 对象（后台线程请配合 run_on_main 使用）"""
        return self._plugin.server

    @property
    def scheduler(self) -> Any:
        """Endstone 任务调度器（0.11：run_task 统一同步/延迟/周期任务；cancel_task 等）。

        返回记录型代理：run_task* 的 task 记入 ``_scheduled_tasks``，
        ``_cleanup`` 时统一 cancel，防热重载后旧任务重复执行。
        """
        raw = self._plugin.server.scheduler
        wrapper = getattr(self, "_scheduler_wrapper", None)
        # 底层 scheduler 对象变化（测试替身/重连）时重建代理
        if wrapper is None or wrapper._scheduler is not raw:
            wrapper = _SchedulerWrapper(raw, self)
            self._scheduler_wrapper = wrapper
        return wrapper

    @property
    def endstone(self) -> Any:
        """endstone 顶级模块透传，随版本升级自动获得全部新 API"""
        import endstone

        return endstone

    @staticmethod
    def import_module(name: str) -> Any:
        """按需导入任意 endstone 子模块（等价 importlib.import_module）"""
        import importlib

        return importlib.import_module(name)

    def get_player(self, name_or_uuid: str) -> Any:
        """按名称获取在线玩家对象（拥有 Endstone Player 全部 API）"""
        try:
            return self._plugin.server.get_player(name_or_uuid)
        except Exception:
            return None

    # -------------------------------------------------------------- 白名单
    @staticmethod
    def domain_of(pack: dict[str, Any]) -> str:
        """事件包所属消息域："official"（QQ 官方机器人，user_id 为 openid）
        或 "qq"（个人号 OneBot，user_id 为 QQ 号）。查白名单前务必用它
        选域，跨域查询会误报"未绑定"。"""
        return "official" if str((pack or {}).get("domain", "")) == "official" else "qq"

    def get_xbox_by_pack(self, pack: dict[str, Any]) -> str | None:
        """按事件包发送者查绑定 XboxID（自动按域路由，官 bot 可用）。"""
        wl = getattr(self._plugin, "whitelist_module", None)
        if wl is None:
            return None
        uid = str((pack or {}).get("user_id", "") or "")
        entry = wl.get_binding_by_qq(uid, self.domain_of(pack)) if uid else None
        return str(entry["xbox"]) if entry else None

    def get_xbox_by_qq(self, qq: int | str, domain: str = "qq") -> str | None:
        """按 QQ 号（或官 bot openid + domain="official"）查绑定 XboxID。"""
        wl = getattr(self._plugin, "whitelist_module", None)
        if wl is None:
            return None
        entry = wl.get_binding_by_qq(qq, domain)
        return str(entry["xbox"]) if entry else None

    def call_action(
        self,
        action: str,
        params: dict[str, Any] | None = None,
        callback: Callable[[Any], None] | None = None,
        timeout: float = 10.0,
    ) -> None:
        """通用 OneBot action 调用入口：传入 callback 时回执到达后回调 callback(data)。"""
        if self.QClient is not None:
            self.QClient.call_action(action, params, callback=callback, timeout=timeout)

    def on(self, event: str, handler: Callable[..., Any]) -> Callable[..., Any]:
        bus = self._bus
        if self._disposed:
            raise RuntimeError(f"[{self.pluginName}] Subplugin context has been cleaned up, event {event!r} registration rejected")
        if bus is None:
            raise RuntimeError(f"[{self.pluginName}] LumenBridge is disabled, event {event!r} registration rejected")
        bus.on(event, handler)
        self._handlers.append((event, handler))
        return handler

    def once(self, event: str, handler: Callable[..., Any]) -> Callable[..., Any]:
        bus = self._bus
        if self._disposed:
            raise RuntimeError(f"[{self.pluginName}] Subplugin context has been cleaned up, event {event!r} registration rejected")
        if bus is None:
            raise RuntimeError(f"[{self.pluginName}] LumenBridge is disabled, event {event!r} registration rejected")
        bus.once(event, handler)
        self._handlers.append((event, handler))
        return handler

    def off(self, event: str, handler: Callable[..., Any]) -> None:
        bus = self._bus
        if bus is None:
            return
        bus.off(event, handler)
        if (event, handler) in self._handlers:
            self._handlers.remove((event, handler))

    def emit(self, event: str, *args: Any, **kwargs: Any) -> None:
        bus = self._bus
        if bus is not None:
            bus.emit(event, *args, **kwargs)

    def register_regex_action(self, action_type: str, handler: Callable[..., Any]) -> None:
        """向正则引擎注册自定义动作"""
        regex_module = getattr(self._plugin, "regex_module", None)
        if regex_module:
            regex_module.register_action(action_type, handler)
            self._regex_actions.append(action_type)

    def register_command(
        self,
        name: str,
        handler: Callable[[Any, list[str]], bool],
        description: str = "",
        aliases: list[str] | None = None,
        usages: list[str] | None = None,
    ) -> bool:
        """注册子插件服务器命令（handler(sender, args) -> bool，主线程执行）。

        endstone 0.11 不支持运行期注册新命令，故用「启动面板 + 运行期绑定」
        两段式：已在 command_palette.json 声明 → 绑定 handler，本次启动即
        可用；未声明 → 写入面板并告警「重启后生效」（返回 False 会让子插件
        直接加载失败，故不采用）；命令名非法/不可调用/被占用 → 返回 False。
        aliases 仅为面板记录；卸载时解除 handler 绑定（面板声明保留）。
        """
        ok = register_subplugin_command(
            self._plugin, self.logger, self.pluginName,
            name, handler, description, aliases, usages,
        )
        if ok:
            cmd_name = str(name).strip().lower()
            self._commands.append(cmd_name)
            self._registered_commands.append(cmd_name)
        return ok

    def run_on_main(self, func: Callable[[], None], delay: int = 1) -> None:
        self._plugin.run_on_main(func, delay)

    def call_on_main(
        self, func: Callable[[], Any], timeout: float = 5.0, default: Any = None
    ) -> Any:
        """把 func 调度到主线程执行并阻塞等待返回值（同步主线程桥）。

        主线程内直接同步执行；后台线程调度并等待，超时/异常返回 default。
        子插件在后台线程触碰 Endstone API 前用它桥接。
        """
        return self._plugin.call_on_main(func, timeout=timeout, default=default)

    def _cleanup(self) -> None:
        """卸载子插件时移除其注册的全部事件监听器"""
        self._disposed = True
        bus = self._bus
        for event, handler in self._handlers:
            if bus is not None:
                try:
                    bus.off(event, handler)
                except Exception:
                    pass
        self._handlers.clear()
        # 注销正则引擎自定义动作（防御部分初始化实例：属性可能未创建）
        regex_actions = getattr(self, "_regex_actions", None)
        if regex_actions is None:
            regex_actions = []
            self._regex_actions = regex_actions
        regex_module = getattr(self._plugin, "regex_module", None)
        custom_actions = getattr(regex_module, "custom_actions", None) if regex_module is not None else None
        if isinstance(custom_actions, dict):
            for action_type in regex_actions:
                custom_actions.pop(action_type, None)
        regex_actions.clear()
        bus_handlers = getattr(self.mc, "_bus_handlers", None)
        if bus_handlers is not None:
            for event, handler in list(bus_handlers):
                if bus is not None:
                    try:
                        bus.off(event, handler)
                    except Exception:
                        pass
            bus_handlers.clear()
        commands = getattr(self, "_commands", None)
        if commands:
            registry = self._plugin.__dict__.get("_lumen_sub_commands", {})
            for cmd_name in list(commands):
                registry.pop(cmd_name, None)
            commands.clear()
            # 面板声明保留：BDS 命令注册属启动期，卸载仅解除 handler 绑定，
            # 重启后命令可被再次绑定
        # 从全局 _lumen_plugin_commands 移除属于本 context 的命令登记
        registered_commands = getattr(self, "_registered_commands", None)
        if registered_commands:
            plugin_commands = self._plugin.__dict__.get("_lumen_plugin_commands")
            if isinstance(plugin_commands, list):
                for cmd_name in registered_commands:
                    try:
                        plugin_commands.remove(cmd_name)
                    except ValueError:
                        pass  # 非 compat 路径注册的名字不在全局列表，忽略
            registered_commands.clear()
        scheduled = getattr(self, "_scheduled_tasks", None)
        if scheduled:
            try:
                scheduler = self._plugin.server.scheduler
                cancel = getattr(scheduler, "cancel_task", None)
            except Exception:
                cancel = None
            for task in list(scheduled):
                try:
                    # task.cancel() 失败时退回 cancel_task(task_id)，确保任务真正取消
                    task.cancel()
                except Exception:
                    if callable(cancel):
                        try:
                            cancel(getattr(task, "task_id", task))
                        except Exception:
                            pass  # 任务已结束/已被取消等情况不阻断其余清理
            scheduled.clear()
        # 原生 listener 无法注销：清空分发表并置 _lumen_active=False 双保险，
        # 避免热重载后子插件回调被重复触发
        dispatch_table = getattr(self.mc, "_endstone_dispatch", None)
        if dispatch_table is not None:
            dispatch_table.clear()
        endstone_listeners = getattr(self.mc, "_endstone_listeners", None)
        if endstone_listeners is not None:
            for _event_name, listener in list(endstone_listeners):
                try:
                    setattr(listener, "_lumen_active", False)
                except Exception:
                    pass
            endstone_listeners.clear()
        try:
            self.web._revoke_registrations()
        except Exception:
            pass  # webui 结构异常不阻断其余清理
        # 清理子插件注册的翻译，避免热重载后旧翻译残留
        try:
            self.i18n.unregister_namespace(self.pluginName)
        except Exception:
            pass
