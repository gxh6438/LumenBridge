"""多适配器管理枢纽（AdapterHub）。

支持同时运行多个 OneBot v11 端点：``websocket``（直连 NapCat / Lagrange /
LLOneBot 等协议端）与 ``astrbot``（AstrBot 插件端）。Hub 按 connections.json
的启用卡片创建 / 停止 / 热重建适配器实例，并作为统一门面提供与单适配器
完全兼容的 OneBot API：发送类调用按群号智能路由（未知群广播到全部）；
查询类调用路由到主适配器（首个已连接实例）。
"""

from __future__ import annotations

import threading
from typing import Any, Callable

from ..connections import ConnectionManager
from ..i18n import t as _t
from .adapter import OneBotAdapter
from .qqofficial_adapter import QQOfficialAdapter

# 首参为 group_id 的方法：按群路由而非广播
_GROUP_ROUTED_METHODS = frozenset(
    {
        "send_group_msg",
        "send_group_forward_msg",
        "set_group_ban",
        "set_group_whole_ban",
        "set_group_kick",
        "set_group_leave",
        "set_group_name",
        "set_group_card",
        "set_group_admin",
        "set_group_special_title",
        "set_group_anonymous_ban",
        "send_group_sign",
        "group_poke",
        "upload_group_file",
        "delete_group_file",
        "get_group_member_list",
        "get_group_member_info",
        "get_group_info",
        "get_group_honor_info",
        "get_group_root_files",
        "get_group_files_by_folder",
        "get_group_file_url",
    }
)

# 查询 / 通用调用类方法：路由到主适配器
_PRIMARY_METHODS = frozenset(
    {
        "call_api",
        "get_msg",
        "get_login_info",
        "get_stranger_info",
        "get_group_list",
        "get_friend_list",
        "get_forward_msg",
        "get_version_info",
        "get_status",
        "get_image",
        "get_record",
    }
)


class AdapterHub:
    """管理多个 OneBotAdapter 实例并提供兼容单适配器的 API 门面。"""

    def __init__(self, logger: Any, event_bus: Any, connections: ConnectionManager) -> None:
        self.logger = logger
        self.bus = event_bus
        self.connections = connections
        self._adapters: dict[str, OneBotAdapter] = {}
        self._lock = threading.RLock()
        # "无已启用适配器" 告警只提示一次：WebUI 每次保存都会触发
        # sync_from_manager，无卡片时重复告警会在后台刷屏
        self._warned_no_adapters = False

    # ------------------------------------------------------------- 实例管理
    def _create(self, cfg: dict[str, Any]) -> OneBotAdapter | QQOfficialAdapter:
        adapter_type = str(cfg.get("type", "websocket") or "websocket")
        if adapter_type == "qqofficial":
            try:
                connect_interval = int(cfg.get("connect_interval", 60000) or 0)
            except (TypeError, ValueError):
                connect_interval = 60000
            try:
                # 附加事件订阅位（如 1<<1 频道成员、1<<24 群成员进退群），
                # 用于叠加默认 Intents 之外的特权事件
                extra_intents = int(cfg.get("extra_intents", 0) or 0)
            except (TypeError, ValueError):
                extra_intents = 0
            adapter = QQOfficialAdapter(
                self.logger,
                self.bus,
                app_id=str(cfg.get("app_id", "") or ""),
                app_secret=str(cfg.get("app_secret", "") or ""),
                sandbox=bool(cfg.get("sandbox", False)),
                adapter_id=str(cfg.get("id", "") or ""),
                adapter_name=str(cfg.get("name", "") or ""),
                bot_qq=int(cfg.get("bot_qq", 0) or 0),
                groups=ConnectionManager.parse_groups_loose(cfg.get("main_group")),
                connect_interval=connect_interval,
                extra_intents=extra_intents,
                # 后台静默日志：默认开启（存量卡片缺键时同样按开启处理）
                suppress_connection_log=bool(cfg.get("suppress_connection_log", True)),
            )
            adapter.config_snapshot = cfg
            return adapter
        adapter = OneBotAdapter(
            self.logger,
            self.bus,
            ws_type=int(cfg.get("ws_type", 0) or 0),
            target=str(cfg.get("target", "") or "ws://127.0.0.1:3001"),
            listen_host=str(cfg.get("listen_host", "0.0.0.0") or "0.0.0.0"),
            listen_port=int(cfg.get("listen_port", 3002) or 3002),
            access_token=str(cfg.get("access_token", "") or ""),
            bot_qq=int(cfg.get("bot_qq", 0) or 0),
            adapter_id=str(cfg.get("id", "") or ""),
            adapter_name=str(cfg.get("name", "") or ""),
            adapter_type=adapter_type,
            groups=ConnectionManager.parse_groups(cfg.get("main_group")),
        )
        adapter.config_snapshot = cfg
        return adapter

    def sync_from_manager(self) -> None:
        """按 connections.json 当前状态差量重建适配器实例（未变化卡片保持不动）。

        stop() 可能阻塞数秒（等待 WS 关闭与线程回收），持锁调用会阻塞
        其他 API 门面请求；因此锁内只做 diff 与待停/待建收集，锁外执行
        stop()，再回锁内创建并启动新实例。
        """
        with self._lock:
            desired: dict[str, dict[str, Any]] = {
                str(a.get("id")): a
                for a in self.connections.adapters_view()
                if a.get("enabled") and self.connections.is_configured(a)
            }
            # 锁内仅收集停用/删除/变更项并 pop，不在锁内阻塞
            to_stop: list[tuple[str, OneBotAdapter]] = []
            for adapter_id, adapter in list(self._adapters.items()):
                cfg = desired.get(adapter_id)
                if cfg is None or adapter.config_snapshot != cfg:
                    to_stop.append((adapter_id, adapter))
                    self._adapters.pop(adapter_id, None)
            to_create: list[tuple[str, dict[str, Any]]] = [
                (adapter_id, cfg)
                for adapter_id, cfg in desired.items()
                if adapter_id not in self._adapters
            ]
        # 锁外执行可能阻塞的 stop()
        for adapter_id, adapter in to_stop:
            try:
                adapter.stop()
            except Exception as e:
                self.logger.error(_t("hub.stop_failed", name=adapter_id, error=e))
        # 回锁内新建/重建
        with self._lock:
            for adapter_id, cfg in to_create:
                if adapter_id in self._adapters:
                    continue
                adapter = self._create(cfg)
                self._adapters[adapter_id] = adapter
                adapter.start()
                self.logger.info(_t("hub.adapter_started", name=adapter.display_name))
            if not desired:
                if not self._warned_no_adapters:
                    self._warned_no_adapters = True
                    self.logger.warning(_t("hub.no_enabled_adapters"))
            else:
                self._warned_no_adapters = False

    def stop_all(self) -> None:
        # 与 sync_from_manager 同理：锁内拷贝清空，锁外逐个 stop()
        with self._lock:
            adapters = list(self._adapters.items())
            self._adapters.clear()
        for adapter_id, adapter in adapters:
            try:
                adapter.stop()
            except Exception as e:
                self.logger.error(_t("hub.stop_failed", name=adapter_id, error=e))

    def restart_all(self) -> None:
        """全量重建（配置结构变化时使用）。"""
        self.stop_all()
        self.sync_from_manager()

    # ------------------------------------------------------------- 查询视图
    def get(self, adapter_id: str) -> OneBotAdapter | None:
        with self._lock:
            return self._adapters.get(str(adapter_id))

    def all(self) -> list[OneBotAdapter]:
        with self._lock:
            return list(self._adapters.values())

    def connected(self) -> list[OneBotAdapter]:
        return [a for a in self.all() if a.is_connected]

    def primary(self) -> OneBotAdapter | None:
        """主适配器：优先已连接的 websocket 实例，其次任意已连接，再次首个。"""
        adapters = self.all()
        if not adapters:
            return None
        online = [a for a in adapters if a.is_connected]
        if not online:
            return adapters[0]
        ws_online = [a for a in online if a.adapter_type == "websocket"]
        return ws_online[0] if ws_online else online[0]

    @property
    def is_connected(self) -> bool:
        return any(a.is_connected for a in self.all())

    @property
    def mode_name(self) -> str:
        online = self.connected()
        if online:
            return " / ".join(sorted({a.mode_name for a in online}))
        adapters = self.all()
        return adapters[0].mode_name if adapters else _t("adapter.mode_forward")

    def status(self) -> list[dict[str, Any]]:
        """供 WebUI / 命令展示的运行状态列表。"""
        result: list[dict[str, Any]] = []
        for cfg in self.connections.adapters_view():
            adapter = self.get(str(cfg.get("id")))
            ws_type = int(cfg.get("ws_type", 0) or 0)
            cfg_type = str(cfg.get("type") or "websocket")
            if cfg_type == "qqofficial":
                endpoint = (
                    f"AppID {cfg.get('app_id', '')}"
                    if str(cfg.get("app_id", "") or "").strip()
                    else ""
                ) + (" (沙箱)" if cfg.get("sandbox") else "")
            else:
                endpoint = (
                    f"ws://{cfg.get('listen_host', '0.0.0.0')}:{cfg.get('listen_port')}"
                    if ws_type == 1
                    else str(cfg.get("target", "") or "")
                )
            groups = (
                ConnectionManager.parse_groups_loose(cfg.get("main_group"))
                if cfg_type == "qqofficial"
                else ConnectionManager.parse_groups(cfg.get("main_group"))
            )
            result.append(
                {
                    "id": cfg.get("id"),
                    "name": cfg.get("name"),
                    "type": cfg.get("type"),
                    "enabled": bool(cfg.get("enabled")),
                    "running": adapter is not None,
                    "connected": bool(adapter and adapter.is_connected),
                    "ws_type": ws_type,
                    "endpoint": endpoint,
                    "groups": groups,
                }
            )
        return result

    # ------------------------------------------------------------- API 门面
    @staticmethod
    def _is_official(adapter: Any) -> bool:
        """QQ 官方适配器判定（个人号协议端无法投递 openid 目标）。"""
        return getattr(adapter, "adapter_type", "") == "qqofficial"

    def _route_by_group(self, group_id: Any) -> list[OneBotAdapter]:
        """双域路由：命中专属适配器只发它们；未命中按目标类型选域。

        - 数字群号（QQ 域）→ 仅非官方适配器（个人号协议端）；
        - openid 字符串（官方域）→ 仅 QQ 官方适配器；
        - 命中某适配器专属群列表时优先（无论何种标识）。
        """
        key = str(group_id).strip()
        connected = self.connected()
        matches = [a for a in connected if key in {str(g) for g in a.groups}]
        if matches:
            return matches
        if key.isdigit():
            return [a for a in connected if not self._is_official(a)]
        return [a for a in connected if self._is_official(a)]

    def _route_private(self, user_id: Any) -> list[OneBotAdapter]:
        """私聊路由：数字 QQ 号走个人号域，openid 走官方域。"""
        key = str(user_id).strip()
        connected = self.connected()
        if key.isdigit():
            return [a for a in connected if not self._is_official(a)]
        return [a for a in connected if self._is_official(a)]

    def _targets(self, name: str, args: tuple) -> list[OneBotAdapter]:
        if name in _GROUP_ROUTED_METHODS and args:
            routed = self._route_by_group(args[0])
            if name.startswith("get_"):
                # 查询类方法只取一个目标：多适配器命中时避免
                # 同一 callback 被触发多次
                return routed[:1]
            return routed
        return self.connected()

    def send_pack(self, pack: dict[str, Any]) -> None:
        """原始包广播到全部已连接适配器。"""
        for adapter in self.connected():
            adapter.send_pack(pack)

    def call_api(
        self,
        pack: dict[str, Any],
        callback: Callable[[Any], None] | None = None,
        timeout: float = 10.0,
    ) -> None:
        adapter = self.primary()
        # 无在线实例时立即回 None，不等满超时
        if adapter is None or not adapter.is_connected:
            if callback:
                callback(None)
            return
        adapter.call_api(pack, callback, timeout)

    def call_action(
        self,
        action: str,
        params: dict[str, Any] | None = None,
        callback: Callable[[Any], None] | None = None,
        timeout: float = 10.0,
    ) -> None:
        adapter = self.primary()
        if adapter is None or not adapter.is_connected:
            if callback:
                callback(None)
            return
        adapter.call_action(action, params, callback, timeout)

    def send_group_msg(self, group_id: Any, message: Any) -> None:
        for adapter in self._route_by_group(group_id):
            adapter.send_group_msg(group_id, message)

    def send_private_msg(self, user_id: Any, message: Any) -> None:
        for adapter in self._route_private(user_id):
            adapter.send_private_msg(user_id, message)

    def broadcast_group(self, group_id: Any, message: Any) -> None:
        """按域广播群消息（与 send_group_msg 路由一致，保留旧入口）。"""
        for adapter in self._route_by_group(group_id):
            adapter.send_group_msg(group_id, message)

    @staticmethod
    def _fail_callback(args: tuple, kwargs: dict) -> None:
        """无可用适配器时按查询回调约定回 None。

        回调可能在 args 任意位置（部分方法带默认尾参，如
        get_record(file, callback, out_format="mp3")），按位置扫描。
        """
        callback = next((a for a in args if callable(a)), None)
        if callback is None:
            callback = next((v for v in kwargs.values() if callable(v)), None)
        if callback is not None:
            try:
                callback(None)
            except Exception:
                pass

    def __getattr__(self, name: str) -> Any:
        """未显式实现的方法按语义代理到适配器集合，保证 OneBot 全 API 可用。

        - 群定向方法：按群路由；
        - 查询类方法：主适配器；
        - 其余发送 / 设置类方法：广播全部已连接实例。
        """
        if name.startswith("_"):
            raise AttributeError(name)

        def _delegate(*args: Any, **kwargs: Any) -> Any:
            if name in _PRIMARY_METHODS:
                adapter = self.primary()
                if adapter is None or not adapter.is_connected:
                    if name.startswith("get_"):
                        self._fail_callback(args, kwargs)
                    return None
                return getattr(adapter, name)(*args, **kwargs)
            targets = self._targets(name, args)
            if not targets:
                # 无连接时保持与单适配器断线一致的行为：查询回调直接给 None
                if name.startswith("get_"):
                    self._fail_callback(args, kwargs)
                return None
            results = [getattr(a, name)(*args, **kwargs) for a in targets]
            if len(results) == 1:
                return results[0]
            # fire-and-forget 方法（返回 None）多实例时也统一返回 None，
            # 避免调用方拿到 [None, None] 误判为有值
            return None if all(r is None for r in results) else results

        return _delegate
