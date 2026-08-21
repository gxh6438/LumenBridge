"""LumenBridge 插件主类：Endstone 群服互通框架。"""

import logging
import threading
import time
from pathlib import Path
from typing import Any, Callable

from endstone import ColorFormat
from endstone.command import Command, CommandSender
from endstone.event import (
    EventPriority,
    PlayerChatEvent,
    PlayerDeathEvent,
    PlayerJoinEvent,
    PlayerQuitEvent,
    event_handler,
)
from endstone.plugin import Plugin

from . import __version__
from .config import ConfigManager
from .connections import ConnectionManager
from .event_bus import EventBus
from .i18n import (
    AUTO_DETECT,
    DEFAULT_LANGUAGE,
    _read_server_properties_language,
    detect_endstone_language,
    get_i18n,
    t as _t,
)
from .pip_manager import PipManager
from .modules import ChatSyncModule, RegexEngineModule, WhitelistModule
from .marketplace import MarketplaceClient
from .onebot import AdapterHub, EventDispatcher, OneBotAdapter
from .onebot.message import set_local_image_roots
from .subplugin import SubPluginManager
from .subplugin.context import (
    EnvPool,
    merge_command_palette_into,
    plugin_register_command_compat,
)
from .webui import LogBuffer, LoggerTee, WebUIServer

# 启动横幅：figlet standard 细线字体（保留 LumenBridge 大小写），
# "Lumen"(前 32 列) 与 "Bridge" 双色拼接
_BANNER_LINES: tuple[str, ...] = (
    " _                               ____       _     _ ",
    "| |   _   _ _ __ ___   ___ _ __ | __ ) _ __(_) __| | __ _  ___",
    "| |  | | | | '_ ` _ \\ / _ \\ '_ \\|  _ \\| '__| |/ _` |/ _` |/ _ \\",
    "| |__| |_| | | | | | |  __/ | | | |_) | |  | | (_| | (_| |  __/",
    "|_____|__,_|_| |_| |_|\\___|_| |_|____/|_|  |_|\\__,_|\\__, |\\___/",
    "                                                    |___/       ",
)
_BANNER_SPLIT = 32


class LumenBridgePlugin(Plugin):
    """LumenBridge —— Endstone 群服互通框架。"""

    # Endstone 0.11 元数据：类属性声明（0.11 加载器官方风格）
    version = __version__
    api_version = "0.11"
    VERSION = __version__

    commands = {
        "lumen": {
            "description": "LumenBridge 群服互通管理命令",
            "usages": ["/lumen (status|reload|say|plugins|pip|update)<action: LumenAction> [message: message]"],
            "permissions": ["lumenbridge.command.lumen"],
        },
    }

    permissions = {
        "lumenbridge.command.lumen": {
            "description": "允许使用 /lumen 管理命令",
            "default": "op",
        },
    }

    def __init__(self) -> None:
        super().__init__()
        self.config_manager: ConfigManager | None = None
        self.connections: ConnectionManager | None = None
        self.bus: EventBus | None = None
        self.hub: AdapterHub | None = None
        self.adapter: AdapterHub | OneBotAdapter | None = None  # 多适配器门面（= hub）
        self.dispatcher: EventDispatcher | None = None
        self.chat_sync_module: ChatSyncModule | None = None
        self.whitelist_module: WhitelistModule | None = None
        self.regex_module: RegexEngineModule | None = None
        self.env_pool: EnvPool | None = None
        self.subplugin_manager: SubPluginManager | None = None
        self.marketplace: MarketplaceClient | None = None
        self.log_buffer: LogBuffer = LogBuffer()
        self.webui: WebUIServer | None = None
        self._raw_logger: Any = None
        self._tee_logger: Any = None
        self._started = False
        self._bot_profile_lock = threading.Lock()
        self._pip_manager_lock = threading.Lock()
        self._pip_manager: Any = None
        # pip/uv 非线程安全，并发写 site-packages 会损坏元数据。提到 plugin 级别统一管理，
        # 使 WebUI 安装/卸载与插件市场依赖安装共享同一把锁，避免绕过 WebUI 的安装路径
        # （如 marketplace._install_declared_dependencies）与 WebUI 安装任务并发执行损坏环境。
        self._pip_serial_lock = threading.Lock()
        # 机器人资料（多账号）：adapter_id → 资料快照
        self._bot_profiles: dict[str, dict[str, Any]] = {}
        self._language: str = DEFAULT_LANGUAGE
        # 服务器主线程 ident：插件加载/启用均在主线程执行，用于
        # run_on_main + 等待结果的模式在主线程上直接调用，避免自死锁
        self._main_thread_id: int = threading.get_ident()

    @property
    def i18n(self):
        """全局 I18n 实例。"""
        return get_i18n()

    def _cmd_log(self) -> Any:
        """命令处理路径安全取日志器。

        /lumen pip、/lumen update 的后台线程必须用 tee logger（replxx 线程
        安全，见 logbuffer.py）；但命令处理可能运行在未完成初始化的实例上
        （如 __new__ 构造的测试替身，缺 _tee_logger / logger 属性），
        直接属性访问会 AttributeError。逐级回退到模块级 logger 兜底。
        """
        return (
            getattr(self, "_tee_logger", None)
            or getattr(self, "logger", None)
            or logging.getLogger("LumenBridge")
        )

    @property
    def language(self) -> str:
        """当前生效的语言代码（已规范化）。"""
        return self._language

    def _init_i18n(self) -> None:
        """根据配置初始化 i18n 语言；"auto" 时检测 Endstone 服务器语言。"""
        log = self._tee_logger or self.logger
        configured = self.config_manager.language if self.config_manager else AUTO_DETECT
        i18n = get_i18n()
        if configured == AUTO_DETECT:
            detected = detect_endstone_language(
                server=self.server, data_folder=self.data_folder
            )
            source = "server.properties"
            try:
                sp = Path(self.data_folder).parent.parent / "server.properties"
                if not (sp.is_file() and _read_server_properties_language(sp)):
                    source = "endstone API/默认"
            except Exception:
                source = "endstone API/默认"
            self._language = i18n.set_language(detected)
            if log:
                log.info(_t("plugin.language_detected", lang=self._language, source=source))
        else:
            self._language = i18n.set_language(configured)
            if log:
                log.info(_t("plugin.language_set", lang=self._language))

        if self.config_manager:
            try:
                self.config_manager.apply_pip_index_by_language(self._language)
            except Exception:
                pass

    def on_load(self) -> None:
        # on_load 阶段 i18n 尚未初始化，先用默认语言保证配置加载失败时也能显示日志
        get_i18n().set_language(DEFAULT_LANGUAGE)
        self._print_banner()
        self.logger.info(
            f"{ColorFormat.GOLD}LumenBridge{ColorFormat.RESET} {_t('plugin.loading')}"
        )

    def on_enable(self) -> None:
        try:
            # on_enable 必然在服务器主线程执行，刷新主线程 ident 兜底
            self._main_thread_id = threading.get_ident()
            # 本地图片白名单：image() 仅允许读取插件数据目录内的文件（含子插件目录），
            # 防止消息变量拼接任意路径把服务器文件外发
            set_local_image_roots([Path(self.data_folder)])
            # 日志三通：后台线程日志调度回主线程输出，避免与 Windows 控制台 replxx 线程竞争崩溃
            self._raw_logger = self.logger
            self._tee_logger = LoggerTee(
                self.logger,
                self.log_buffer,
                main_thread_dispatch=lambda fn: self.server.scheduler.run_task(self, fn, delay=1),
            )

            self.config_manager = ConfigManager(self.data_folder, self._tee_logger)

            self._init_i18n()

            self.bus = EventBus(self._tee_logger)

            # 适配器卡片独立存于 connections.json，首次生成时自动迁移
            # 旧 config.json 的 connection/admin_qq/main_group/sync
            self.connections = ConnectionManager(
                self.data_folder,
                self._tee_logger,
                legacy=self.config_manager.legacy_connection or None,
            )
            self.config_manager.attach_connections(self.connections)

            self._rebuild_bot_profiles_baseline()
            self.hub = AdapterHub(self._tee_logger, self.bus, self.connections)
            self.adapter = self.hub

            self.dispatcher = EventDispatcher(self.hub, self.bus, self._tee_logger)

            self.whitelist_module = WhitelistModule(self)
            self.chat_sync_module = ChatSyncModule(self)
            self.regex_module = RegexEngineModule(self)

            self.bus.on("bot.online", self._on_bot_online)
            self.bus.on("bot.offline", self._on_bot_offline)

            self.register_events(self)

            self.env_pool = EnvPool(self)
            # 注入到分发器，使子插件 env.get("main_group") 能感知当前事件来源群
            self.dispatcher.env_pool = self.env_pool
            self.subplugin_manager = SubPluginManager(self)
            self.subplugin_manager.load_all()

            # 插件市场：网络检查绝不在游戏主线程执行，也不未经管理员确认自动安装
            self.marketplace = MarketplaceClient(self)
            market_cfg = self.config_manager.data.get("marketplace", {})
            updates_cfg = self.config_manager.data.get("updates", {})
            market_check = isinstance(market_cfg, dict) and bool(market_cfg.get("enable")) and bool(market_cfg.get("check_on_start", True))
            auto_update = isinstance(updates_cfg, dict) and bool(updates_cfg.get("enable", True)) and bool(updates_cfg.get("auto_update", True))
            if market_check or auto_update:
                threading.Thread(
                    target=self._check_market_updates_background,
                    name="LumenBridge-MarketCheck",
                    daemon=True,
                ).start()

            webui_cfg = self.config_manager.data.get("webui", {})
            if isinstance(webui_cfg, dict) and webui_cfg.get("enable", True):
                self.webui = WebUIServer(self)
                # webui 就绪后补注册子插件在 on_load 中暂存的 config/page/api
                # （子插件加载早于 webui 创建，延迟注册解决加载顺序问题）
                with self.subplugin_manager._lock:
                    pending = [sp for sp in self.subplugin_manager.subplugins.values() if sp.loaded and sp.context]
                for sp in pending:
                    sp.context.web._flush_pending()
                self.webui.start()

            self.hub.sync_from_manager()

            mode = self._connection_mode_summary()
            (self._tee_logger or self.logger).info(
                f"{ColorFormat.GREEN}✔ {_t('plugin.enabled', mode=mode)}{ColorFormat.RESET}"
            )
        except Exception as e:
            (self._tee_logger or self.logger).error(_t("plugin.enable_failed", error=e))
            raise

    def _connection_mode_summary(self) -> str:
        """当前启用的连接模式摘要（保持顺序去重）。

        优先取 hub 中运行中的适配器模式（含已启用但尚未连上的实例）；
        无运行实例时退回已启用的适配器卡片名称；全部未启用时返回"未启用"。
        """
        if self.hub is not None:
            modes = [a.mode_name for a in self.hub.all()]
            if modes:
                return " / ".join(dict.fromkeys(modes))
        if self.connections is not None:
            names = [
                str(a.get("name") or a.get("type") or "").strip()
                for a in self.connections.adapters_view()
                if a.get("enabled")
            ]
            names = [n for n in names if n]
            if names:
                return " / ".join(dict.fromkeys(names))
        return _t("plugin.no_adapter_enabled")

    def _print_banner(self) -> None:
        """加载横幅：LumenBridge ASCII Logo + 版本与标语（on_load 时打印一次）。"""
        for line in _BANNER_LINES:
            left = line[:_BANNER_SPLIT].rstrip()
            right = line[_BANNER_SPLIT:].rstrip()
            colored = f"{ColorFormat.GOLD}{left}"
            if right:
                colored += f"{ColorFormat.AQUA}{right}"
            self.logger.info(f"{colored}{ColorFormat.RESET}")
        self.logger.info(
            f"{ColorFormat.GOLD}➤ LumenBridge {ColorFormat.WHITE}v{self.VERSION} "
            f"{ColorFormat.GRAY}{_t('plugin.tagline')}{ColorFormat.RESET}"
        )

    def on_disable(self) -> None:
        # 每个清理步骤独立 try/except，避免单步异常阻断后续清理导致资源泄漏
        log = self._tee_logger or self.logger
        for cleanup in (
            lambda: self.webui.stop() if self.webui else None,
            lambda: self.subplugin_manager.unload_all() if self.subplugin_manager else None,
            lambda: self.chat_sync_module.on_server_stop() if self.chat_sync_module else None,
            lambda: self.hub.stop_all() if self.hub else None,
            lambda: self.bus.remove_all() if self.bus else None,
        ):
            try:
                cleanup()
            except Exception as e:
                log.error(_t("plugin.stop_exception", error=e))
        # 与 _on_bot_online 的 check-then-set 用同一把锁原子化：
        # 防止停用期间 bot 上线把 _started 重新置 True、在拆卸后的模块上广播
        with self._bot_profile_lock:
            self._started = False
        # 组件引用清理：所有清理 lambda 执行完毕后再置空，尽早释放资源、
        # 避免停用后残留引用继续被误用（LoggerTee 无 stop 方法，仅做引用清理）
        self.webui = None
        self.subplugin_manager = None
        self.chat_sync_module = None
        self.hub = None
        self.adapter = None
        self.bus = None
        self.dispatcher = None
        self.env_pool = None
        self.whitelist_module = None
        self.regex_module = None
        self.marketplace = None
        # 失效 pip manager 缓存：禁用→启用复用同一实例时会持有旧 config_manager.data
        with self._pip_manager_lock:
            self._pip_manager = None
        self._tee_logger = None
        self._raw_logger = None
        self.logger.info(_t("plugin.disabled"))

    def _check_market_updates_background(self) -> None:
        """低频市场更新检查：子插件只记录可用更新（安装需 WebUI 管理员确认）；
        框架本体只通知新版本，绝不自动更新——热重载会重载服务器内全部插件，
        必须由管理员经 /lumen update framework -y 或 WebUI 确认弹窗手动确认。
        """
        client = self.marketplace
        if client is None:
            return
        log = self._tee_logger or self.logger
        if client.enabled:
            try:
                updates = client.check_subplugin_updates()
                available = [name for name, info in updates.items() if info.get("available")]
                if available:
                    log.info("[Market] 子插件有可用更新: " + ", ".join(available))
            except Exception as exc:  # noqa: BLE001
                log.warning(f"[Market] 更新检查失败: {exc}")
        updates_cfg = self.config_manager.data.get("updates", {}) if self.config_manager else {}
        if not (isinstance(updates_cfg, dict) and updates_cfg.get("enable", True) and updates_cfg.get("auto_update", True)):
            return
        try:
            info = client.framework_update_info()
            if not info.get("available"):
                return
            version = str((info.get("latest") or {}).get("version") or "?")
            log.info(
                f"[Update] 发现 LumenBridge 新版本 v{version}（不会自动更新）。"
                f"确认更新请执行 /lumen update framework，准备就绪后再执行 "
                f"/lumen update framework -y；或在 WebUI 面板点击更新并确认。"
                f"注意：更新会重载服务器内所有插件。"
            )
        except Exception as exc:  # noqa: BLE001
            log.warning(f"[Update] 框架更新检查失败: {exc}")

    @staticmethod
    def _qq_avatar_url(qq: int) -> str:
        """返回公开 QQ 头像地址；无有效 QQ 时由前端显示本地占位。"""
        return f"https://q1.qlogo.cn/g?b=qq&nk={qq}&s=100" if qq > 0 else ""

    def bot_profiles_snapshot(self) -> list[dict[str, Any]]:
        """供 Web 线程读取全部机器人账号资料（多账号卡片）。"""
        with self._bot_profile_lock:
            return [dict(p) for p in self._bot_profiles.values()]

    def bot_profile_snapshot(self) -> dict[str, Any]:
        """兼容视图：首个机器人账号资料。"""
        profiles = self.bot_profiles_snapshot()
        return profiles[0] if profiles else {}

    def _rebuild_bot_profiles_baseline(self) -> None:
        """按 connections.json 重建各适配器的资料基线（未连接也显示卡片）。"""
        if not self.connections:
            return
        # 持锁快照，避免与 WebUI 并发 CRUD 的列表变更冲突
        adapters_view = self.connections.adapters_view()
        # 连接状态取 hub 实时值：重建的实例尚未握手应为 False，
        # 未变化的实例保持连接则仍为 True（旧实现盲目继承上一轮
        # 值，会把已断线/已重建的适配器永久显示为已连接）。
        # hub.get 可能触及其内部锁，一律在 _bot_profile_lock 外先收集完毕
        live_states: dict[str, bool] = {}
        if self.hub:
            for cfg in adapters_view:
                if not cfg.get("enabled"):
                    continue
                aid = str(cfg.get("id") or "")
                live = self.hub.get(aid)
                live_states[aid] = bool(live is not None and live.is_connected)
        with self._bot_profile_lock:
            rebuilt: dict[str, dict[str, Any]] = {}
            for cfg in adapters_view:
                if not cfg.get("enabled"):
                    continue
                aid = str(cfg.get("id") or "")
                prev = self._bot_profiles.get(aid) or {}
                is_official = str(cfg.get("type")) == "qqofficial"
                try:
                    qq = int(cfg.get("bot_qq", 0) or 0)
                except (TypeError, ValueError):
                    qq = 0
                rebuilt[aid] = {
                    "adapter_id": aid,
                    "adapter_name": str(cfg.get("name") or ""),
                    "adapter_type": str(cfg.get("type") or ""),
                    "qq": qq,
                    "nickname": str(prev.get("nickname") or ""),
                    "avatar_url": self._qq_avatar_url(qq),
                    "app_id": str(cfg.get("app_id", "") or "") if is_official else "",
                    "connected": live_states.get(aid, False),
                    "source": "config",
                }
            self._bot_profiles = rebuilt

    def _update_bot_profile(self, adapter: Any, data: Any) -> None:
        """``get_login_info`` 回调（运行在 WS 线程）：更新对应适配器的账号资料。"""
        if not isinstance(data, dict) or adapter is None:
            return
        aid = str(getattr(adapter, "adapter_id", "") or "")
        nickname = str(data.get("nickname") or "").strip()
        try:
            qq = int(data.get("user_id") or 0)
        except (TypeError, ValueError):
            qq = 0
        with self._bot_profile_lock:
            prev = self._bot_profiles.get(aid) or {}
            if not qq:
                qq = int(prev.get("qq") or 0)
            self._bot_profiles[aid] = {
                "adapter_id": aid,
                "adapter_name": str(
                    getattr(adapter, "adapter_name", "") or prev.get("adapter_name") or ""
                ),
                "adapter_type": str(
                    getattr(adapter, "adapter_type", "") or prev.get("adapter_type") or ""
                ),
                "qq": qq,
                "nickname": nickname or str(prev.get("nickname") or ""),
                "avatar_url": self._qq_avatar_url(qq),
                "app_id": str(data.get("app_id") or prev.get("app_id") or ""),
                # 沿用实时连接状态：回调到达瞬间断线时不会被硬编码 True 覆盖
                "connected": bool(getattr(adapter, "is_connected", False)),
                "source": "onebot",
            }

    def reload_onebot_connection(self) -> None:
        """按 connections.json 最新状态差量重建适配器。

        未变化的适配器保持连接不中断；事件总线与模块引用不变（门面对象恒为 hub）。
        """
        if not self.connections or not self.hub:
            return
        self.connections.load()
        self.hub.sync_from_manager()

        for module in (self.chat_sync_module, self.whitelist_module, self.regex_module):
            if module:
                module.adapter = self.hub
        if self.subplugin_manager:
            with self.subplugin_manager._lock:
                for subplugin in self.subplugin_manager.subplugins.values():
                    if subplugin.context:
                        subplugin.context.QClient = self.hub

        self._rebuild_bot_profiles_baseline()

        summary = self._connection_mode_summary()
        (self._tee_logger or self.logger).info(
            _t("plugin.connection_reloaded", mode=summary)
        )

    def _reload_webui(self) -> None:
        """按最新配置启停 / 刷新 WebUI（enable、host、port、password、secret 热生效）。

        既有实例只刷新运行参数（WebUIServer.refresh_config），扩展注册表与
        运行中任务状态保留；enable 由关到开时才补建实例（子插件 WebBridge
        实时读取 plugin.webui，替换/新建实例后引用自动跟上）。
        """
        conf = self.config_manager.data.get("webui", {}) if self.config_manager else {}
        if not isinstance(conf, dict):
            conf = {}
        if not conf.get("enable", True):
            if self.webui is not None and self.webui.is_running:
                self.webui.stop()
            return
        if self.webui is None:
            self.webui = WebUIServer(self)
            # 补注册子插件在 on_load 中暂存的 config/page/api（与 on_enable 同序）
            if self.subplugin_manager is not None:
                # 持锁快照：marketplace 安装/卸载线程（WebUI 线程）会并发增删该 dict，
                # 裸迭代可能 RuntimeError: dictionary changed size during iteration
                with self.subplugin_manager._lock:
                    pending = [sp for sp in self.subplugin_manager.subplugins.values() if sp.loaded and sp.context]
                for sp in pending:
                    sp.context.web._flush_pending()
            self.webui.start()
            return
        # 曾被 disable 停掉的实例：refresh_config 只刷新参数不重启监听，
        # 必须显式 start（stop 已置空 httpd，start 可安全复用实例）
        if not self.webui.is_running:
            self.webui.start()
            return
        self.webui.refresh_config()

    def _on_bot_online(self, adapter: Any = None) -> None:
        # 此回调在 WS 线程触发，必须用线程安全的 tee logger
        name = str(getattr(adapter, "display_name", "") or "")
        # 连接成功属运行提示类日志：来源适配器开启后台静默日志时不打印
        # （防刷屏，READY/RESUMED 每次重连都会触发本回调）；
        # WS/AstrBot 适配器无此开关，保持原样打印
        if not getattr(adapter, "suppress_connection_log", False):
            (self._tee_logger or self.logger).info(_t("plugin.bot_connected", name=name))
        if adapter is not None:
            adapter.get_login_info(
                lambda data, a=adapter: self._update_bot_profile(a, data)
            )
        elif self.hub:
            for online_adapter in self.hub.connected():
                online_adapter.get_login_info(
                    lambda data, a=online_adapter: self._update_bot_profile(a, data)
                )
        # 多个 WS 线程可能同时回调：check-then-set 用 _bot_profile_lock 原子化，
        # 网络相关调用（上方 get_login_info 与下方 on_server_start）都留在锁外
        with self._bot_profile_lock:
            if self._started:
                return
            self._started = True
        if self.chat_sync_module:
            self.chat_sync_module.on_server_start()

    def _on_bot_offline(self, adapter: Any = None) -> None:
        """断线回调（WS 线程）：把对应适配器的资料卡片连接状态置回 False。"""
        aid = str(getattr(adapter, "adapter_id", "") or "")
        if not aid:
            return
        with self._bot_profile_lock:
            profile = self._bot_profiles.get(aid)
            if profile is not None:
                profile["connected"] = False

    def run_on_main(self, func: Callable[[], None], delay: int = 1) -> None:
        """从 WS 线程安全调度回游戏主线程执行。"""
        self.server.scheduler.run_task(self, func, delay=delay)

    def is_on_main_thread(self) -> bool:
        """当前代码是否运行在服务器主线程。

        供「调度到主线程再阻塞等待结果」的调用模式判断：在主线程上
        调度会因主线程自身阻塞而永远等不到任务执行（自死锁），
        此时必须改为直接同步调用。
        """
        return threading.get_ident() == self._main_thread_id

    def call_on_main(
        self, func: Callable[[], Any], timeout: float = 5.0, default: Any = None
    ) -> Any:
        """把 func 调度到主线程执行并阻塞等待返回值（同步主线程桥）。

        - 已在主线程：直接同步执行（调度会自死锁）；
        - 后台线程：调度后阻塞等待，超时/异常返回 default；
        - 超时后排队中的任务不再执行（cancelled 标志，同 runcmdEx），
          避免「调用方已按失败处理、副作用却延迟发生」的不一致。

        供子插件在 OneBot/WebUI 线程安全触碰 Endstone API（玩家、
        记分板、其他插件实例等），如统一经济服务的余额读写。
        """
        if self.is_on_main_thread():
            try:
                return func()
            except Exception:
                return default
        box: list[Any] = [default]
        done = threading.Event()
        cancelled = threading.Event()

        def _run() -> None:
            if cancelled.is_set():
                done.set()
                return
            try:
                box[0] = func()
            except Exception:
                box[0] = default
            finally:
                done.set()

        self.server.scheduler.run_task(self, _run, delay=0)
        if not done.wait(timeout):
            cancelled.set()
            return default
        return box[0]

    def group_allowed(self, pack: dict[str, Any]) -> bool:
        """来源群是否允许处理：属于任一启用适配器的群列表。

        未填写任何群 openid / 群 QQ 号时，默认对所有群生效：
        - 所有适配器均未配置群列表 → 放行任意来源群；
        - 来源适配器自身未配置群列表 → 放行其任意来源群
          （AstrBot 群号在其插件端配置、QQ 官方 openid 可后置抄录均依赖此行为）；
        - 配置了群列表 → 仅命中列表（或其它适配器列表）的群放行。
        QQ 官方适配器的群标识为 group_openid 字符串，统一用字符串比较。
        """
        gid = pack.get("group_id")
        if gid is None:
            return False
        key = str(gid)
        connections = self.connections
        if connections is None:
            # 连接管理器未就绪（加载中/已卸载）时不处理任何群消息
            return False
        # group_key_set 为缓存的 frozenset（热路径 O(1)）；getattr 兼容
        # 仅实现 all_group_keys 的测试桩/旧实现
        key_set = getattr(connections, "group_key_set", None)
        configured = key_set() if callable(key_set) else frozenset(connections.all_group_keys())
        if not configured:
            # 没有任何适配器填写群号 → 默认所有群生效
            return True
        if key in configured:
            return True
        adapter_id = str(pack.get("_lumen_adapter_id", "") or "")
        # get_view 免深拷贝：该回退路径在未配置群的消息上每条触发一次；
        # 同样以 getattr 兼容只有 get() 的桩
        if adapter_id:
            view = getattr(connections, "get_view", None)
            cfg = view(adapter_id) if callable(view) else connections.get(adapter_id)
        else:
            cfg = None
        if cfg is not None and cfg.get("enabled"):
            groups = connections.parse_groups_loose(cfg.get("main_group"))
            return key in groups or not groups
        return False

    @event_handler(priority=EventPriority.MONITOR)
    def on_player_chat(self, event: PlayerChatEvent) -> None:
        if event.is_cancelled:
            return
        name = event.player.name
        message = event.message
        if self.chat_sync_module:
            self.chat_sync_module.on_player_chat(name, message)
        if self.regex_module:
            self.regex_module.on_mc_player_chat(name, message)
        if self.bus:
            self.bus.emit("mc.player_chat", name, message)

    @event_handler(priority=EventPriority.MONITOR)
    def on_player_join(self, event: PlayerJoinEvent) -> None:
        # join/quit 事件部分版本无 is_cancelled 属性，getattr 缺省 False 不受影响
        if getattr(event, "is_cancelled", False):
            return
        name = event.player.name
        if self.chat_sync_module:
            self.chat_sync_module.on_player_join(name)
        if self.regex_module:
            self.regex_module.on_mc_player_join(name)
        if self.bus:
            self.bus.emit("mc.player_join", name)

    @event_handler(priority=EventPriority.MONITOR)
    def on_player_quit(self, event: PlayerQuitEvent) -> None:
        if getattr(event, "is_cancelled", False):
            return
        name = event.player.name
        if self.chat_sync_module:
            self.chat_sync_module.on_player_quit(name)
        if self.regex_module:
            self.regex_module.on_mc_player_left(name)
        if self.bus:
            self.bus.emit("mc.player_left", name)

    @event_handler(priority=EventPriority.MONITOR)
    def on_player_death(self, event: PlayerDeathEvent) -> None:
        if getattr(event, "is_cancelled", False):
            return
        death_message = self._resolve_death_message(event)
        if self.chat_sync_module:
            self.chat_sync_module.on_player_death(death_message)
        if self.bus:
            self.bus.emit("mc.player_death", event.player.name, death_message)

    def _resolve_death_message(self, event: PlayerDeathEvent) -> str:
        """将死亡消息解析为可读文本。

        ``event.death_message`` 可能是 ``str``、``Translatable`` 或 ``None``；
        ``Translatable`` 是本地化键加参数的容器，直接 ``str()`` 只会得到对象 repr，
        必须通过 ``server.language.translate`` 翻译。
        """
        msg = event.death_message
        if not msg:
            return _t("plugin.death_fallback", player=event.player.name)
        if isinstance(msg, str):
            return msg
        try:
            translated = self.server.language.translate(msg, event.player.locale)
            if translated:
                return translated
        except Exception:
            pass
        # 兼容回退：手动用参数替换 %s 占位符
        try:
            text = getattr(msg, "text", "") or ""
            params = [str(p) for p in (getattr(msg, "params", None) or [])]
            if text:
                for p in params:
                    text = text.replace("%s", p, 1)
                return text
        except Exception:
            pass
        return _t("plugin.death_fallback", player=event.player.name)

    def register_command(
        self,
        name: str,
        handler: Callable[[Any, list[str]], bool],
        description: str = "",
        aliases: list[str] | None = None,
        usages: list[str] | None = None,
    ) -> bool:
        """插件对象上的 register_command 兼容入口。

        PicServer_Rank3 等子插件经 ``lumen.plugin.register_command(...)`` 注册
        命令；子插件加载期间转发给当前上下文（归属、卸载清理与上下文注册一致）。
        """
        return plugin_register_command_compat(self, name, handler, description, aliases, usages)

    def on_command(self, sender: CommandSender, command: Command, args: list[str]) -> bool:
        if command.name != "lumen":
            # 子插件命令面板路由（见模块尾部 _merge_subplugin_command_palette）：
            # 命令经启动面板声明、由 LumenBridge 持有，执行时分发给注册 handler
            # 的子插件
            entry = getattr(self, "_lumen_sub_commands", {}).get(command.name)
            if entry is None:
                return False
            handler = entry.get("handler")
            try:
                # 子插件命令默认 usage 为 [args: message]（贪心参数），Endstone 会把
                # 整行剩余内容作为单个字符串传入；PicServer 兼容 handler 约定
                # args 为空白分隔的 token 列表，此处统一展开
                tokens = [tok for arg in args for tok in str(arg).split()]
                return bool(handler(sender, tokens)) if callable(handler) else False
            except Exception as e:  # noqa: BLE001
                self.logger.error(f"/{command.name}: {e}")
                sender.send_message(f"{ColorFormat.RED}/{command.name} execution failed: {e}{ColorFormat.RESET}")
                return False

        action = args[0].lower() if args else "status"

        known_actions = {"status", "reload", "say", "plugins", "pip", "update"}
        if action not in known_actions:
            sender.send_message(f"{ColorFormat.RED}{_t('plugin.command.unknown_action', action=action)}{ColorFormat.RESET}")
            return True

        # config_manager 为 None 时默认拒绝（安全降级），避免配置加载失败后权限体系失效
        if self.config_manager:
            allowed, reason_key = self.config_manager.check_command_permission(sender, action)
            if not allowed:
                sender.send_message(f"{ColorFormat.RED}{_t(reason_key)}{ColorFormat.RESET}")
                return True
        else:
            sender.send_message(f"{ColorFormat.RED}{_t('commands.config_unavailable')}{ColorFormat.RESET}")
            return True

        if action == "pip":
            return self._handle_pip_command(sender, args[1:] if len(args) > 1 else [])

        if action == "update":
            return self._handle_update_command(sender, args[1:] if len(args) > 1 else [])

        if action == "status":
            adapter_lines: list[str] = []
            if self.hub:
                for item in self.hub.status():
                    if not item["enabled"]:
                        state = ColorFormat.GRAY + _t("plugin.command.state_disabled")
                    elif item["connected"]:
                        state = ColorFormat.GREEN + _t("plugin.command.state_connected")
                    else:
                        state = ColorFormat.RED + _t("plugin.command.state_disconnected")
                    groups = ", ".join(str(g) for g in item["groups"]) or _t("plugin.command.status_no_group")
                    adapter_lines.append(
                        f" - {item['name']} [{item['type']}] ({state}{ColorFormat.RESET}) "
                        f"{_t('plugin.command.status_main_group', group=groups)}"
                    )
            rules = len(self.regex_module.rules) if self.regex_module else 0
            bindings = len(self.whitelist_module.snapshot()) if self.whitelist_module else 0
            if self.subplugin_manager:
                # 持锁快照：WebUI 安装/卸载线程会并发增删 subplugins dict
                with self.subplugin_manager._lock:
                    sub_count = sum(1 for sp in self.subplugin_manager.subplugins.values() if sp.loaded)
                    sub_total = len(self.subplugin_manager.subplugins)
            else:
                sub_count = sub_total = 0
            webui_state = (
                f"{ColorFormat.GREEN}{self.webui.url}" if self.webui and self.webui.is_running
                else f"{ColorFormat.RED}{_t('plugin.command.state_disabled')}"
            )
            sender.send_message(
                f"{ColorFormat.GOLD}{_t('plugin.command.status_title')}{ColorFormat.RESET}\n"
                + "\n".join(adapter_lines)
                + f"\n{_t('plugin.command.status_rule_count', count=rules)} | "
                f"{_t('plugin.command.status_whitelist', count=bindings)} | "
                f"{_t('plugin.command.status_subplugins', loaded=sub_count, total=sub_total)}\n"
                f"{_t('plugin.command.status_webui', state=webui_state)}{ColorFormat.RESET}"
            )
            return True

        if action == "reload":
            try:
                self.config_manager.load()
                self._init_i18n()
                # 连接配置同样热重载：手动编辑 connections.json 后 /lumen reload 也能生效
                self.reload_onebot_connection()
                # WebUI 配置热重载：host/port/password/secret/enable 变化立即生效
                self._reload_webui()
                # 失效 pip manager 缓存使新 pip 配置生效；加锁避免与 _get_pip_manager 双检锁并发竞态
                with self._pip_manager_lock:
                    self._pip_manager = None
                count = self.regex_module.reload_rules() if self.regex_module else 0
                sub_count = self.subplugin_manager.reload_all() if self.subplugin_manager else 0
                # 市场/更新检查线程只在启动时拉起；reload 后按最新配置补启，
                # 使启动时禁用、后续改开的 marketplace/updates 也能生效
                # （线程内部按 enable 实时判断，重复补启无副作用）
                if self.marketplace is not None:
                    market_cfg = self.config_manager.data.get("marketplace", {})
                    updates_cfg = self.config_manager.data.get("updates", {})
                    market_check = isinstance(market_cfg, dict) and bool(market_cfg.get("enable")) and bool(market_cfg.get("check_on_start", True))
                    auto_update = isinstance(updates_cfg, dict) and bool(updates_cfg.get("enable", True)) and bool(updates_cfg.get("auto_update", True))
                    if market_check or auto_update:
                        threading.Thread(
                            target=self._check_market_updates_background,
                            name="LumenBridge-MarketCheck",
                            daemon=True,
                        ).start()
                sender.send_message(
                    f"{ColorFormat.GREEN}{_t('plugin.command.reload_success', rules=count, subplugins=sub_count)}"
                    f"{ColorFormat.RESET}"
                )
            except Exception as e:
                sender.send_message(f"{ColorFormat.RED}{_t('plugin.command.reload_failed', error=e)}{ColorFormat.RESET}")
            return True

        if action == "plugins":
            lines = self.subplugin_manager.status_text_lines() if self.subplugin_manager else [_t("plugin.command.plugins_list_empty")]
            sender.send_message(
                f"{ColorFormat.GOLD}{_t('plugin.command.plugins_list_title')}{ColorFormat.RESET}\n"
                + "\n".join(lines)
            )
            return True

        if action == "say":
            if len(args) < 2 or not args[1]:
                sender.send_message(f"{ColorFormat.RED}{_t('plugin.command.say_usage')}{ColorFormat.RESET}")
                return True
            if self.adapter and self.adapter.is_connected:
                text = args[1]
                # 全部适配器的群标识并集（含 QQ 官方 openid），
                # 与 group_allowed 口径一致；hub 会按群路由到对应适配器
                groups = (
                    self.connections.all_group_keys()
                    if self.connections
                    else [str(g) for g in self.config_manager.main_groups]
                )
                if not groups:
                    sender.send_message(f"{ColorFormat.RED}{_t('plugin.command.say_no_group')}{ColorFormat.RESET}")
                    return True
                line = _t("plugin.command.say_broadcast_format", sender=sender.name, text=text)
                for gid in groups:
                    self.adapter.send_group_msg(gid, line)
                sender.send_message(f"{ColorFormat.GREEN}{_t('plugin.command.say_success', count=len(groups))}{ColorFormat.RESET}")
            else:
                sender.send_message(f"{ColorFormat.RED}{_t('plugin.command.say_failed')}{ColorFormat.RESET}")
            return True

        return True

    def _get_pip_manager(self):
        """获取或懒构造 PipManager（线程安全，双检锁）。"""
        cached = getattr(self, "_pip_manager", None)
        if cached is not None:
            return cached
        if not self.config_manager:
            return None
        log = self._cmd_log()
        with self._pip_manager_lock:
            cached = getattr(self, "_pip_manager", None)
            if cached is not None:
                return cached
            mgr = PipManager(self.config_manager.data, log)
            self._pip_manager = mgr
            return mgr

    def _handle_pip_command(self, sender: CommandSender, args: list[str]) -> bool:
        """处理 /lumen pip install|list|uninstall 子命令。

        BDS 命令声明的 [message: message] 是贪心参数，"install xxx" 会作为
        单个字符串到达，必须按空白展开后再解析子动作与包名。
        """
        tokens = [tok for arg in args for tok in str(arg).split()]
        sub = tokens[0].lower() if tokens else "list"
        mgr = self._get_pip_manager()
        if mgr is None:
            sender.send_message(f"{ColorFormat.RED}{_t('pip.manager_unavailable')}{ColorFormat.RESET}")
            return True
        if not mgr.enable:
            sender.send_message(f"{ColorFormat.RED}{_t('pip.disabled')}{ColorFormat.RESET}")
            return True

        if sub == "install":
            if len(tokens) < 2:
                sender.send_message(f"{ColorFormat.RED}{_t('pip.cmd_install_usage')}{ColorFormat.RESET}")
                return True
            target = tokens[1]
            # 先尝试当作子插件名取其声明的依赖，否则当作包名
            packages: list[str] = []
            if self.subplugin_manager:
                with self.subplugin_manager._lock:
                    sp = self.subplugin_manager.subplugins.get(target)
                if sp and sp.manifest.get("dependencies"):
                    packages = list(sp.manifest["dependencies"])
            if not packages:
                packages = tokens[1:]

            sender.send_message(f"{ColorFormat.GOLD}{_t('pip.cmd_installing', packages=' '.join(packages))}{ColorFormat.RESET}")

            # 异步执行安装避免阻塞命令调用线程；结果通过 run_on_main 回传主线程
            # 后台线程必须用 tee logger：Endstone 原始 logger 经 replxx 写控制台，
            # 非主线程调用会与控制台输入线程竞争导致服务端崩溃（见 logbuffer.py）
            _log = self._cmd_log()

            def _async_install() -> None:
                log_lines: list[str] = []
                try:
                    # 持 _pip_serial_lock：与 WebUI 安装/卸载、marketplace 依赖安装互斥，避免并发写损坏元数据
                    with self._pip_serial_lock:
                        success, msg = mgr.install(packages, on_log=lambda line: log_lines.append(line))
                except Exception as e:  # noqa: BLE001
                    _log.exception("pip install thread error")
                    success, msg = False, str(e)

                def _send_result() -> None:
                    try:
                        for line in log_lines:
                            sender.send_message(line)
                        color = ColorFormat.GREEN if success else ColorFormat.RED
                        sender.send_message(f"{color}{msg}{ColorFormat.RESET}")
                        # 子插件依赖安装成功则自动 reload；reload_one 调用 Endstone API 必须在主线程执行
                        if success and self.subplugin_manager and target:
                            with self.subplugin_manager._lock:
                                sp_exists = target in self.subplugin_manager.subplugins
                            if sp_exists:
                                ok = self.subplugin_manager.reload_one(target)
                                reload_msg = _t("pip.cmd_reload_success" if ok else "pip.cmd_reload_failed", name=target)
                                rcolor = ColorFormat.GREEN if ok else ColorFormat.RED
                                sender.send_message(f"{rcolor}{reload_msg}{ColorFormat.RESET}")
                    except Exception:  # noqa: BLE001
                        # run_on_main 只调度不执行：_send_result 内部异常须自捕获，
                        # 否则直接抛进主线程任务上下文
                        _log.exception("pip install result send failed")

                try:
                    self.run_on_main(_send_result)
                except Exception:  # noqa: BLE001
                    # 插件已停用、调度器不可用时线程不得静默死亡，留日志供排查
                    _log.exception("pip install result dispatch failed")

            threading.Thread(target=_async_install, name="LumenBridge-pip-install", daemon=True).start()
            return True

        if sub == "uninstall":
            if len(tokens) < 2:
                sender.send_message(f"{ColorFormat.RED}{_t('pip.cmd_uninstall_usage')}{ColorFormat.RESET}")
                return True
            package = tokens[1]

            # 卸载同步执行会阻塞命令调用线程，与 install 分支一致改为后台
            # daemon 线程执行，结果经 run_on_main 回发主线程
            _log = self._cmd_log()

            def _async_uninstall() -> None:
                try:
                    # 持 _pip_serial_lock：与 install 路径互斥，避免并发卸载/安装损坏环境
                    with self._pip_serial_lock:
                        success, msg = mgr.uninstall(package)
                except Exception as e:  # noqa: BLE001
                    _log.exception("pip uninstall thread error")
                    success, msg = False, str(e)

                def _send_result() -> None:
                    try:
                        color = ColorFormat.GREEN if success else ColorFormat.RED
                        sender.send_message(f"{color}{msg}{ColorFormat.RESET}")
                    except Exception:  # noqa: BLE001
                        _log.exception("pip uninstall result send failed")

                try:
                    self.run_on_main(_send_result)
                except Exception:  # noqa: BLE001
                    _log.exception("pip uninstall result dispatch failed")

            threading.Thread(target=_async_uninstall, name="LumenBridge-pip-uninstall", daemon=True).start()
            return True

        if sub == "list":
            # pip list 是同步子进程调用（冷启动 1-3s、timeout 30s），
            # 与 install/uninstall 一致移入后台线程避免阻塞命令调用线程
            _log = self._cmd_log()

            def _async_list() -> None:
                try:
                    pkgs = mgr.list_packages()
                except Exception as e:  # noqa: BLE001
                    _log.exception("pip list thread error")
                    pkgs = []

                def _send_result() -> None:
                    try:
                        if not pkgs:
                            sender.send_message(f"{ColorFormat.YELLOW}{_t('pip.cmd_list_empty')}{ColorFormat.RESET}")
                            return
                        sender.send_message(f"{ColorFormat.GOLD}{_t('pip.cmd_list_title', count=len(pkgs))}{ColorFormat.RESET}")
                        lines = [f"{p.get('name', '?')}=={p.get('version', '?')}" for p in pkgs[:50]]
                        sender.send_message("\n".join(lines))
                        if len(pkgs) > 50:
                            sender.send_message(f"{ColorFormat.YELLOW}{_t('pip.cmd_list_truncated', total=len(pkgs))}{ColorFormat.RESET}")
                    except Exception:  # noqa: BLE001
                        _log.exception("pip list result send failed")

                try:
                    self.run_on_main(_send_result)
                except Exception:  # noqa: BLE001
                    _log.exception("pip list result dispatch failed")

            threading.Thread(target=_async_list, name="LumenBridge-pip-list", daemon=True).start()
            return True

        sender.send_message(f"{ColorFormat.RED}{_t('pip.cmd_unknown_sub')}{ColorFormat.RESET}")
        return True

    def _handle_update_command(self, sender: CommandSender, args: list[str]) -> bool:
        """处理 /lumen update <子插件名 | -A|--all> 子命令。

        下载安装会阻塞数秒至数分钟，与 pip 命令一致在后台 daemon 线程执行，
        结果经 run_on_main 回发主线程（send_message 线程安全性同 pip 分支）。
        """
        tokens = [tok for arg in args for tok in str(arg).split()]
        if not tokens:
            sender.send_message(f"{ColorFormat.RED}{_t('plugin.command.update_usage')}{ColorFormat.RESET}")
            return True

        # /lumen update framework [-y]：框架本体更新（手动确认，热重载重载全服插件）
        if tokens[0] in ("framework", "框架"):
            return self._handle_framework_update_command(sender, tokens[1:])

        update_all = tokens[0] in ("-A", "--all")
        target = "" if update_all else tokens[0]

        client = self.marketplace
        if client is None or not client.enabled:
            sender.send_message(f"{ColorFormat.RED}{_t('plugin.command.update_market_disabled')}{ColorFormat.RESET}")
            return True

        if not update_all:
            if self.subplugin_manager is None:
                sender.send_message(f"{ColorFormat.RED}{_t('plugin.command.update_manager_unavailable')}{ColorFormat.RESET}")
                return True
            with self.subplugin_manager._lock:
                sp = self.subplugin_manager.subplugins.get(target)
                origin = sp.manifest.get("_market", {}) if sp else None
            if sp is None:
                sender.send_message(f"{ColorFormat.RED}{_t('plugin.command.update_unknown_plugin', name=target)}{ColorFormat.RESET}")
                return True
            if not isinstance(origin, dict) or origin.get("source") != "marketplace":
                sender.send_message(f"{ColorFormat.RED}{_t('plugin.command.update_not_market', name=target)}{ColorFormat.RESET}")
                return True

        sender.send_message(
            f"{ColorFormat.GOLD}{_t('plugin.command.update_starting_all' if update_all else 'plugin.command.update_starting', name=target)}{ColorFormat.RESET}"
        )

        # 后台线程必须用 tee logger（replxx 线程安全，见 logbuffer.py）
        _thread_log = self._cmd_log()

        def _dispatch(color: str, text: str) -> None:
            try:
                sender.send_message(f"{color}{text}{ColorFormat.RESET}")
            except Exception:  # noqa: BLE001
                _thread_log.exception("update command result dispatch failed")

        def _run() -> None:
            def _log(msg: str) -> None:
                # 市场日志同步抄一份到控制台，方便无 WebUI 时排查
                _thread_log.info(f"[Market] {msg}")

            try:
                if update_all:
                    result = client.update_all(log=_log)
                    total = int(result.get("total") or 0)
                    if not total:
                        self.run_on_main(lambda: _dispatch(ColorFormat.YELLOW, _t("plugin.command.update_none_available")))
                        return
                    ok = len(result.get("updated") or [])
                    fail = len(result.get("failed") or [])
                    lines = [
                        _t("plugin.command.update_success", name=str(i.get("name") or "?"), version=str(i.get("to_version") or "?"))
                        for i in result.get("updated") or []
                    ] + [
                        _t("plugin.command.update_failed", name=str(i.get("name") or "?"), error=str(i.get("error") or "?"))
                        for i in result.get("failed") or []
                    ]
                    summary_color = ColorFormat.GREEN if not fail else ColorFormat.YELLOW
                    summary = _t("plugin.command.update_all_result", ok=ok, fail=fail)

                    def _send_batch(summary_color: str = summary_color, summary: str = summary, lines: list[str] = lines) -> None:
                        _dispatch(summary_color, summary)
                        if lines:
                            _dispatch(ColorFormat.RESET, "\n".join(lines))

                    self.run_on_main(_send_batch)
                else:
                    result = client.update(target, "", update_dependencies=True, log=_log)
                    version = str(result.get("version") or "?")
                    self.run_on_main(
                        lambda: _dispatch(ColorFormat.GREEN, _t("plugin.command.update_success", name=target, version=version))
                    )
            except Exception as e:  # noqa: BLE001
                _thread_log.exception("lumen update thread error")
                error = str(e)
                self.run_on_main(lambda: _dispatch(ColorFormat.RED, _t("plugin.command.update_failed", name=target or "-A", error=error)))

        threading.Thread(target=_run, name="LumenBridge-subplugin-update", daemon=True).start()
        return True

    def _handle_framework_update_command(self, sender: CommandSender, args: list[str]) -> bool:
        """处理 /lumen update framework [-y]：框架本体更新（手动确认模式）。

        两步确认流程：
        - 无 -y：检查新版本并下载校验暂存（不更新），就绪后提示管理员
          用 -y 确认——因为热重载走 Server.reload()，会重载服务器内全部插件；
        - 带 -y：执行暂存（幂等，已暂存则跳过下载）并调度热重载。
        """
        client = self.marketplace
        if client is None or not client.enabled:
            sender.send_message(f"{ColorFormat.RED}{_t('plugin.command.update_market_disabled')}{ColorFormat.RESET}")
            return True

        confirm = any(tok in ("-y", "--yes") for tok in args)
        _thread_log = self._cmd_log()

        def _dispatch(color: str, text: str) -> None:
            try:
                sender.send_message(f"{color}{text}{ColorFormat.RESET}")
            except Exception:  # noqa: BLE001
                _thread_log.exception("framework update command result dispatch failed")

        if confirm:
            sender.send_message(f"{ColorFormat.GOLD}{_t('plugin.command.framework_confirm_start')}{ColorFormat.RESET}")

            def _run_apply() -> None:
                try:
                    result = client.apply_framework_update(
                        log=lambda msg: _thread_log.info(f"[Update] {msg}")
                    )
                    version = str(result.get("to_version") or "?")
                    self.run_on_main(
                        lambda: _dispatch(
                            ColorFormat.GREEN,
                            _t("plugin.command.framework_reload_scheduled", version=version),
                        )
                    )
                except Exception as e:  # noqa: BLE001
                    _thread_log.exception("framework apply thread error")
                    error = str(e)
                    self.run_on_main(
                        lambda: _dispatch(ColorFormat.RED, _t("plugin.command.framework_apply_failed", error=error))
                    )

            threading.Thread(target=_run_apply, name="LumenBridge-framework-update", daemon=True).start()
            return True

        sender.send_message(f"{ColorFormat.GOLD}{_t('plugin.command.framework_checking')}{ColorFormat.RESET}")

        def _run_stage() -> None:
            try:
                info = client.framework_update_info()
                if not info.get("available"):
                    current = str(info.get("current_version") or "?")
                    self.run_on_main(
                        lambda: _dispatch(ColorFormat.GREEN, _t("plugin.command.framework_latest", version=current))
                    )
                    return
                version = str((info.get("latest") or {}).get("version") or "?")
                self.run_on_main(
                    lambda: _dispatch(
                        ColorFormat.GOLD,
                        _t("plugin.command.framework_staging", version=version),
                    )
                )
                client.stage_framework_update(log=lambda msg: _thread_log.info(f"[Update] {msg}"))
                self.run_on_main(
                    lambda: _dispatch(ColorFormat.GOLD, _t("plugin.command.framework_ready", version=version))
                )
            except Exception as e:  # noqa: BLE001
                _thread_log.exception("framework stage thread error")
                error = str(e)
                self.run_on_main(
                    lambda: _dispatch(ColorFormat.RED, _t("plugin.command.framework_stage_failed", error=error))
                )

        threading.Thread(target=_run_stage, name="LumenBridge-framework-stage", daemon=True).start()
        return True


def _merge_subplugin_command_palette() -> None:
    """服务器启动时把子插件命令面板并入类级 commands（模块导入期执行）。

    endstone 加载器在 ep.load()（即本模块导入）之后立即快照
    ``cls.__dict__['commands']`` 构造 Command 并冻结 BDS 命令表，因此合并
    必须发生在模块导入期——放 __init__/on_load 均为时已晚。面板文件损坏
    时静默跳过，绝不阻断插件加载。
    """
    try:
        merge_command_palette_into(LumenBridgePlugin.commands)
    except Exception:
        pass


_merge_subplugin_command_palette()
