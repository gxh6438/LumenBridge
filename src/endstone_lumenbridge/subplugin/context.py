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


# 子插件命令面板：endstone 的 Command name/aliases setter 在注册后为 no-op，
# Python API 不支持运行期注册新命令，BDS 命令表在插件加载时冻结。子插件命令
# 由此面板在服务器启动时并入 LumenBridgePlugin.commands 预声明（见
# plugin._merge_subplugin_command_palette），运行期仅做 handler 绑定。
COMMAND_PALETTE_PATH = Path("plugins/lumenbridge/data/command_palette.json")

# 低危项：command_palette.json 读-改-写锁。多个子插件并发注册命令时，
# 无锁的 read → merge → write 会互相覆盖丢失对方的条目
_PALETTE_LOCK = threading.Lock()

_PALETTE_NAME_RE = re.compile(r"[a-z0-9_\-]+")
# Endstone usage 语法中的合法参数 token：
# 可选 (a|b) 枚举组 + <必选参数> / [可选参数]（参数名后可带 ": 类型"，类型可含空格）
_USAGE_TOKEN_RE = re.compile(
    r"(?:\([A-Za-z0-9_|]+\))?[<\[][A-Za-z0-9_]+(?::\s*[A-Za-z][A-Za-z0-9_]*)?[>\]]"
)


def read_command_palette() -> dict[str, dict[str, Any]]:
    """读取启动命令面板（损坏/缺失返回空 dict，绝不抛异常）。"""
    try:
        data = json.loads(COMMAND_PALETTE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): v for k, v in data.items() if isinstance(v, dict)}


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

    Endstone 在启动时按 usage 语法构建命令树，参数必须是 <x>/[x]/(a|b) 形式；
    此前默认 "/{name} ..." 中的字面 "..." 会导致
    "Syntax Error: expect '(', '<' or '['" 且命令注册失败。
    [args: message] 为官方贪心字符串参数，可接住任意子命令与参数。
    """
    return f"/{name} [args: message]"


def sanitize_usages(name: str, usages: list[str] | None) -> list[str]:
    """清洗 usage 列表，剔除 Endstone 无法解析的非法项，全无效时回退默认。

    非法 usage（如旧版默认 "/{name} ..."、无类型中括号 "[页码]"）会导致
    Endstone 启动时 "Unable to register command"，此处逐项校验语法：
    usage 须以 "/{name}" 开头，其余部分只能是若干参数 token ——
    (a|b)枚举组、<参数> / [参数] / <参数: 类型> / [参数: 类型]（类型可含空格）。
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


def add_command_palette_entry(
    name: str,
    description: str = "",
    usages: list[str] | None = None,
    aliases: list[str] | None = None,
    permissions: list[str] | None = None,
) -> None:
    """向启动面板登记一个子插件命令（下次服务器启动时并入类级 commands）。"""
    # 低危项：整个读-改-写过程持锁，防并发注册互相覆盖
    with _PALETTE_LOCK:
        palette = read_command_palette()
        entry: dict[str, Any] = {
            "description": str(description or f"LumenBridge subplugin command /{name}"),
            "usages": sanitize_usages(name, usages),
        }
        if aliases:
            entry["aliases"] = [str(a).strip().lower() for a in aliases if str(a).strip()]
        if permissions:
            entry["permissions"] = [str(p) for p in permissions]
        palette[str(name)] = entry
        write_command_palette(palette)


def merge_command_palette_into(commands: dict[str, dict[str, Any]]) -> int:
    """把启动面板中的命令并入目标 commands 字典（如 LumenBridgePlugin.commands）。

    必须在插件模块导入期调用：endstone 加载器在 ``ep.load()`` 之后立即快照
    ``cls.__dict__['commands']`` 并构造 Command 对象，之后再改类属性无效。
    面板文件可能被手工编辑损坏，任何条目问题都只跳过该条，绝不抛异常。
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
                # 清洗面板文件中的 usage（含旧版写入的 "/name ..." 非法默认值），
                # 否则 Endstone 解析失败导致 "Unable to register command"
                "usages": sanitize_usages(name, entry.get("usages")),
            }
            aliases = [str(a).strip().lower() for a in (entry.get("aliases") or []) if str(a).strip()]
            if aliases:
                clean["aliases"] = aliases
            # 权限名未在插件 permissions 声明会导致注册失败，面板默认不带权限
            permissions = [str(p) for p in (entry.get("permissions") or []) if str(p).strip()]
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

        v1.2.0：来源适配器自身的群列表同样视为"主群"；AstrBot 适配器（群号在其
        插件端配置）未配置群列表时接受任意来源群。
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
                # 事件回调中返回当前来源群，否则返回配置首个主群
                gid = getattr(self._ctx, "group", None)
                if gid is not None:
                    return gid
                cm = self._plugin.config_manager
                # 低危项：无配置管理器（reload 中间态/未初始化）时返回 None 而非 0，
                # 避免 0 被当成有效群号参与比较（已排查仓库与示例插件，
                # 调用点均为相等性比较，无 ==0 真值判断依赖）
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
        self._plugin.bus.emit(f"env.update.{key}", value)
        self._plugin.bus.emit("env.update", key, value)


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
        # 必须用 debug 级别，否则调试日志无法通过日志级别关闭
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
                    # 文件损坏（非法 JSON 或非 UTF-8 字节）：备份原文件再写默认值，
                    # 避免直接覆盖用户数据；UnicodeDecodeError 继承自 ValueError
                    # 而非 OSError，旧实现不捕获会直接抛给子插件
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
                    # I/O 错误（文件占用/权限）：不覆盖文件，直接返回 default
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
        # M32：原生 listener 改为 per-event 单例——同事件只 register_events 一次，
        # 具体回调挂到可变分发表；_cleanup 清空分发表（listener 留存但变 no-op），
        # 避免每次热重载都新增一批无法注销的原生监听器
        self._endstone_dispatch: dict[str, list[Callable[..., Any]]] = {}
        # 已注册的原生 listener 登记表：(event_name, listener)
        self._endstone_listeners: list[tuple[str, Any]] = []
        # 经内部事件总线注册的回调（_EVENT_MAP 别名），卸载时必须 off，
        # 否则热重载后旧回调残留总线，同一事件被重复触发
        self._bus_handlers: list[tuple[str, Callable[..., Any]]] = []

    def listen(self, event_name: str, callback: Callable[..., Any]) -> bool:
        """监听游戏事件：兼容别名或任意 Endstone 事件类名（回调收原生事件对象）。"""
        internal = self._EVENT_MAP.get(event_name)
        if internal:
            self._plugin.bus.on(internal, callback)
            self._bus_handlers.append((internal, callback))
            return True
        return self._listen_endstone(event_name, callback)

    def _listen_endstone(self, event_name: str, callback: Callable[..., Any]) -> bool:
        """动态注册原生 Endstone 事件监听（按类名反射构造 @event_handler 监听器类）。

        M32：Endstone 未暴露 unregister_events，逐回调创建 listener 会在每次
        热重载后堆积一批永不移除的原生监听器。改为 per-event 单例：同一事件名
        只创建/注册一次 listener，其 handler 从可变分发表
        ``_endstone_dispatch[event_name]`` 取当前全部回调执行；_cleanup 只清空
        分发表（listener 保留但不再分发），对外 listen() 签名与行为不变。
        """
        try:
            import endstone.event as es_event

            event_cls = getattr(es_event, event_name, None)
            if event_cls is None or not isinstance(event_cls, type):
                return False

            # 先保证分发表就绪：listener 注册成功后事件可能立即触发
            dispatch = self._endstone_dispatch.setdefault(event_name, [])
            if not any(evt == event_name for evt, _l in self._endstone_listeners):
                # 闭包捕获分发表对象本身（而非 self），_cleanup 原地清空后
                # listener 自动变 no-op，且不额外持有 context 引用
                dispatch_table = self._endstone_dispatch

                def _handler(listener_self: Any, event: Any) -> None:  # noqa: ANN401
                    if not getattr(listener_self, "_lumen_active", True):
                        return
                    # 从可变分发表 dispatch：_cleanup 清空分发表即"移除"全部回调
                    for cb in list(dispatch_table.get(event_name, ())):
                        cb(event)

                _handler.__annotations__ = {"event": event_cls, "return": None}
                decorated = es_event.event_handler(_handler)
                listener_cls = type(f"_LumenSubListener_{event_name}", (), {"on_event": decorated})
                listener = listener_cls()
                listener._lumen_active = True  # type: ignore[attr-defined]
                self._plugin.register_events(listener)
                self._endstone_listeners.append((event_name, listener))
            # 同事件重复 listen：只追加回调到分发表，不再新建 listener
            dispatch.append(callback)
            return True
        except Exception:
            return False

    def runcmd(self, cmd: str) -> bool:
        """在游戏主线程执行命令，返回 dispatch_command 的真实结果。

        低危项：此前恒返回 True，子插件无法感知命令是否真的执行成功；现等待
        主线程 dispatch 完成后取真实 bool（最多等 5 秒，超时/异常返回 False）。
        勿在游戏主线程调用（等待主线程排队任务会死锁，同 runcmdEx）。
        """
        done = threading.Event()
        box: list[bool] = [False]

        def run() -> None:
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

        self._plugin.run_on_main(run)
        done.wait(timeout=5.0)
        return box[0]

    def runcmdEx(self, cmd: str, timeout: float = 5.0) -> dict[str, Any]:
        """执行命令并捕获输出（阻塞调用线程）。不要在游戏主线程调用，否则死锁。"""
        outputs: list[str] = []
        done = threading.Event()
        cancelled = threading.Event()
        result: dict[str, Any] = {"success": False}

        def run() -> None:
            # 超时后调用方已返回快照；排队中的任务不再执行，避免超时后仍产生副作用
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

        self._plugin.run_on_main(run)
        if not done.wait(timeout=timeout):
            # 超时：置取消标志，令仍在主线程队列中的任务直接跳过
            cancelled.set()
        output = re.sub(r"§.", "", "\n".join(outputs), flags=re.DOTALL).strip()
        return {"success": bool(result.get("success")), "output": output}

    def broadcast(self, message: str) -> None:
        """向全服广播消息"""
        def run() -> None:
            self._plugin.server.broadcast_message(message)

        self._plugin.run_on_main(run)

    @property
    def online_players(self) -> list[str]:
        """在线玩家名列表（线程安全快照）。阻塞主线程最多 2 秒，勿在主线程调用。"""
        box: list[list[str]] = [[]]
        done = threading.Event()
        cancelled = threading.Event()

        def _fetch() -> None:
            # 超时后调用方已返回空列表；排队中的任务不再执行
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
                # 超时：置取消标志，令仍在主线程队列中的任务直接跳过
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
        # H16：本子插件已注册的 Web 扩展记录，_revoke_registrations 据此逐项撤销：
        #   _registered_apis  → ("METHOD", "/api/plugin/xxx")，对应 webui.custom_apis 键
        #   _registered_pages → "/plugin-views/..."，对应 webui.custom_pages 条目的 url
        #   _registered_configs → schema 名，对应 webui.plugins_config_schema 键
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
        # 写 plugins_config_schema 时加 ext_lock，与 server.py 注册路径保持一致
        ext_lock = getattr(webui, "_ext_lock", None)
        for name, schema in configs:
            if ext_lock is not None:
                with ext_lock:
                    webui.plugins_config_schema[name] = schema
            else:
                webui.plugins_config_schema[name] = schema
        for title, rel_path in pages:
            webui.register_custom_page(self._name, self._folder, title, rel_path)
        for method, path, handler, need_auth in apis:
            webui.register_api(method, path, handler, need_auth)

    def createConfig(self, name: str | None = None) -> Any:
        webui = self._webui
        target_name = name or self._name
        if str(target_name) not in self._registered_configs:
            # H16：builder 后续 register 时会写入 plugins_config_schema，
            # 先记录 schema 名供卸载撤销（builder 未真正注册时撤销为无害 pop）
            self._registered_configs.append(str(target_name))
        if webui:
            return webui.create_config(target_name)
        from ..webui.configform import ConfigFormBuilder

        def _defer_register(builder: Any) -> None:
            with self._pending_lock:
                self._pending_configs.append((builder.name, builder.to_schema()))

        return ConfigFormBuilder(target_name, _defer_register)

    # snake_case 别名
    create_config = createConfig

    def registerApi(self, method: str, path: str, handler: Any, need_auth: bool = True) -> None:
        webui = self._webui
        # 与 webui.register_api 内部构造的完整路径保持一致，供卸载时按键撤销
        full = "/api/plugin" + (path if isinstance(path, str) and path.startswith("/") else "/" + str(path))
        if not need_auth:
            # H16：免鉴权 API 任何能连到 WebUI 端口的客户端都可调用，注册时即告警
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

    def registerPage(self, title: str, relative_path: str) -> None:
        webui = self._webui
        if webui:
            webui.register_custom_page(self._name, self._folder, title, relative_path)
        else:
            with self._pending_lock:
                self._pending_pages.append((title, relative_path))
        # url 构造与 webui.register_custom_page 内部一致，供卸载时按 url 移除
        self._registered_pages.append(f"/plugin-views/{self._folder}/{relative_path}")

    register_page = registerPage

    def _revoke_registrations(self) -> None:
        """H16：卸载时撤销本子插件注册的全部 API / 自定义页面 / 配置表单。

        WebUI 三个注册表是进程级全局状态，热重载若不撤销，旧 handler 会残留
        并与新 handler 并存（路由命中已卸载插件的闭包，可能持有过期 storage）。
        通过 context 持有的 webui 引用，在 _ext_lock 保护下逐键删除。
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

    # 跨子插件命令名查重（注册表挂在插件实例上，所有上下文共享）
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

    PicServer_Rank3 等子插件会经 ``lumen.plugin.register_command(...)`` 注册
    命令（而非上下文）。子插件加载期间（loader 设置了 ``_lumen_loading_context``）
    转发给对应上下文，保证归属与卸载清理正确；其余场景以 "plugin" 归属直接注册。

    H15：加载期的 compat 注册同时登记到全局 ``_lumen_plugin_commands`` 与
    context 自身的 ``_registered_commands``（按 owner 记录），_cleanup 时把
    属于本 context 的条目从全局列表移除，绑定随卸载释放。
    """
    ctx = plugin.__dict__.get("_lumen_loading_context")
    if ctx is not None:
        ok = ctx.register_command(name, handler, description, aliases, usages)
        if ok:
            # 全局列表记录归属本 context 的命令，卸载时随 _cleanup 移除
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
    """Endstone 调度器代理（H14）。

    透传 scheduler 的全部属性与方法，仅包装任务注册类方法（run_task /
    run_task_later / run_task_timer 等）：把返回的 task 对象记录到所属
    context 的 ``_scheduled_tasks``，供 ``_cleanup`` 统一 cancel——否则热
    重载后旧 context 的定时任务永不取消，周期任务会重复执行。cancel_task
    同步从记录集合移除，保持集合与调度器实际状态一致。对外签名不变，
    子插件对 ``lumen.scheduler.*`` 的调用完全无感知。
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
                    for task in args:
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
        # H15：本上下文（含 compat 路径）注册的命令名，_cleanup 时从全局
        # _lumen_plugin_commands 移除属于本上下文的条目
        self._registered_commands: list[str] = []
        # H14：本上下文经 lumen.scheduler 注册的定时任务对象，_cleanup 逐个 cancel
        self._scheduled_tasks: set[Any] = set()

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
        """Endstone 任务调度器（0.11：run_task(plugin, task, delay=0, period=0) 统一同步/延迟/周期任务；cancel_task 等）。

        H14：返回记录型代理——run_task* 返回的 task 会记入 ``_scheduled_tasks``，
        ``_cleanup`` 时统一 cancel，防热重载后旧定时任务残留重复执行；
        其余属性/方法原样透传，对外签名与用法不变。
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
        self._plugin.bus.on(event, handler)
        self._handlers.append((event, handler))
        return handler

    def once(self, event: str, handler: Callable[..., Any]) -> Callable[..., Any]:
        self._plugin.bus.once(event, handler)
        self._handlers.append((event, handler))
        return handler

    def off(self, event: str, handler: Callable[..., Any]) -> None:
        self._plugin.bus.off(event, handler)
        if (event, handler) in self._handlers:
            self._handlers.remove((event, handler))

    def emit(self, event: str, *args: Any, **kwargs: Any) -> None:
        self._plugin.bus.emit(event, *args, **kwargs)

    def register_regex_action(self, action_type: str, handler: Callable[..., Any]) -> None:
        """向正则引擎注册自定义动作"""
        regex_module = getattr(self._plugin, "regex_module", None)
        if regex_module:
            regex_module.register_action(action_type, handler)
            # 记录已注册的动作，供 _cleanup 注销，避免热重载后旧 action 残留
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

        endstone 0.11 的 Python API 不支持运行期注册新命令（BDS 命令表在
        插件加载时冻结，Command 的 name/aliases setter 注册后为 no-op），故
        采用「启动面板 + 运行期绑定」两段式：

        - 命令已在 command_palette.json 声明（服务器启动时由
          plugin._merge_subplugin_command_palette 并入类级 commands）→
          绑定 handler，本次启动即可用，返回 True；
        - 未声明 → 写入面板文件后同样返回 True 并告警提示「重启服务器后
          生效」：本启动 /name 暂不可用，重启后面板命令注册、子插件再次
          加载时绑定即生效（返回 False 会让子插件直接加载失败，故不采用）；
        - 命令名非法、handler 不可调用或已被其他子插件注册 → 返回 False。

        aliases 参数仅为面板记录（重启后随命令一并声明）。子插件卸载时解除
        handler 绑定（面板声明保留，重启后仍可被再次绑定）。
        """
        ok = register_subplugin_command(
            self._plugin, self.logger, self.pluginName,
            name, handler, description, aliases, usages,
        )
        if ok:
            cmd_name = str(name).strip().lower()
            self._commands.append(cmd_name)
            # H15：按 owner 记录到 context 自身，_cleanup 时从全局
            # _lumen_plugin_commands 移除属于本上下文的条目
            self._registered_commands.append(cmd_name)
        return ok

    def run_on_main(self, func: Callable[[], None], delay: int = 1) -> None:
        self._plugin.run_on_main(func, delay)

    def _cleanup(self) -> None:
        """卸载子插件时移除其注册的全部事件监听器"""
        for event, handler in self._handlers:
            self._plugin.bus.off(event, handler)
        self._handlers.clear()
        # 注销本上下文注册的正则引擎自定义动作，避免热重载后旧 action 残留
        # （防御部分初始化的实例：_regex_actions 可能尚未创建）
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
        # mc.listen 经内部总线注册的回调同样注销，防止热重载后重复触发
        bus_handlers = getattr(self.mc, "_bus_handlers", None)
        if bus_handlers is not None:
            for event, handler in list(bus_handlers):
                try:
                    self._plugin.bus.off(event, handler)
                except Exception:
                    pass
            bus_handlers.clear()
        # Endstone 未暴露 unregister_events，通过置 _lumen_active=False 软注销，
        # 避免热重载后子插件回调被重复触发
        # （防御部分初始化的实例：_commands 可能尚未创建）
        commands = getattr(self, "_commands", None)
        if commands:
            registry = self._plugin.__dict__.get("_lumen_sub_commands", {})
            for cmd_name in list(commands):
                registry.pop(cmd_name, None)
            commands.clear()
            # 面板声明（command_palette.json）保留：BDS 命令注册属启动期，
            # 卸载子插件不注销命令本身，仅解除 handler 绑定（on_command 找不到
            # 绑定即返回 False），重启后命令可被再次绑定
        # H15：从全局 _lumen_plugin_commands 移除属于本 context 的命令登记
        # （防御部分初始化的实例：_registered_commands 可能尚未创建）
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
        # H14：取消本上下文经 lumen.scheduler 注册的全部定时任务，
        # 避免热重载后旧 context 的周期任务残留重复执行
        scheduled = getattr(self, "_scheduled_tasks", None)
        if scheduled:
            try:
                scheduler = self._plugin.server.scheduler
                cancel = getattr(scheduler, "cancel_task", None)
            except Exception:
                cancel = None
            for task in list(scheduled):
                if callable(cancel):
                    try:
                        cancel(task)
                    except Exception:
                        pass  # 任务已结束/已被取消等情况不阻断其余清理
            scheduled.clear()
        # M32：原生 listener 为 per-event 单例且无法注销（Endstone 未暴露
        # unregister_events）；清空分发表使其不再分发，并置 _lumen_active=False
        # 双保险，避免热重载后子插件回调被重复触发
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
        # H16：撤销本子插件注册的 WebUI API / 自定义页面 / 配置表单
        try:
            self.web._revoke_registrations()
        except Exception:
            pass  # webui 结构异常不阻断其余清理
        # 清理子插件注册的翻译，避免热重载后旧翻译残留
        try:
            self.i18n.unregister_namespace(self.pluginName)
        except Exception:
            pass
