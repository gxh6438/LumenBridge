"""连接配置管理：适配器卡片（WebSocket / AstrBot）列表，存放在插件数据目录的 connections.json。

从 v1.2.0 起，连接配置与群服互通配置从 config.json 剥离到本文件；每个适配器
拥有独立的身份（机器人 QQ / 管理员 / 主群）与群服互通设置。
"""

from __future__ import annotations

import copy
import json
import os
import re
import threading
import uuid
from pathlib import Path
from typing import Any

from .i18n import t as _t

ADAPTER_TYPES = ("websocket", "astrbot", "qqofficial")


def _default_sync() -> dict[str, Any]:
    """单个适配器的群服互通默认配置（与 v1.1.x config.sync 字段一致）。"""
    return {
        "chat_to_server_enable": True,
        "chat_to_group_enable": True,
        "join_to_group_enable": True,
        "leave_to_group_enable": True,
        "death_to_group_enable": True,
        "server_start_to_group": True,
        "server_stop_to_group": True,
        "text_format": "%s",
        "face_format": "[表情]",
        "image_format": "[图片]",
        "at_format": "@%s",
        "reply_format": "[回复]",
        "forward_format": "[合并转发]",
        "join_format": "[玩家] %s 进服",
        "leave_format": "[玩家] %s 退服",
        "death_format": "[死亡] %s",
        "chat_to_group_format": "[玩家] %s: %s",
        "chat_to_server_format": "[群聊] %s: %s",
        "server_start_format": "[服务器] 已启动",
        "server_stop_format": "[服务器] 已关闭",
        "max_message_length": 256,
    }


# 默认展示两张卡片：QQ 个人号（WebSocket 直连）+ QQ 官方机器人（均未启用、未配置）
DEFAULT_ADAPTERS: list[dict[str, Any]] = [
    {
        "id": "ws_default",
        "type": "websocket",
        "name": "WebSocket",
        "enabled": False,
        "ws_type": 0,
        "target": "",
        "listen_host": "0.0.0.0",
        "listen_port": 3002,
        "access_token": "",
        "bot_qq": 0,
        "admin_qq": [],
        "main_group": "",
        "sync": _default_sync(),
    },
    {
        "id": "qqofficial_default",
        "type": "qqofficial",
        "name": "QQ 官方机器人",
        "enabled": False,
        "ws_type": 0,
        "target": "",
        "listen_host": "0.0.0.0",
        "listen_port": 3002,
        "access_token": "",
        "app_id": "",
        "app_secret": "",
        "sandbox": False,
        # 后台日志开关：关闭后抑制连接/断连/重连类日志（防刷屏），其它日志不受影响
        "suppress_connection_log": False,
        # 连接间隔（毫秒）：两次网关连接尝试之间的最小等待时间，0 表示按指数退避自动重连
        "connect_interval": 60000,
        # 附加事件订阅位（按位或叠加到默认 Intents）：
        # 1<<0 频道变更 / 1<<1 频道成员 / 1<<3 语音房 / 1<<9 私域频道消息 /
        # 1<<10 表情表态 / 1<<24 群成员进退群 / 1<<26 互动 / 1<<27 审核 / 1<<28 论坛
        "extra_intents": 0,
        "bot_qq": 0,
        "admin_qq": [],
        # QQ 官方适配器此处填 group_openid（逗号分隔字符串）
        "main_group": "",
        "sync": _default_sync(),
    },
]


# 全类型空白模板（含 AstrBot：默认卡片不展示，但“添加适配器”仍可手动创建）
# 构建时深拷贝，避免模板与 DEFAULT_ADAPTERS 共享内部 dict 引用（改一处动全部）
ADAPTER_TEMPLATES: dict[str, dict[str, Any]] = {
    a["type"]: copy.deepcopy(a) for a in DEFAULT_ADAPTERS
}
ADAPTER_TEMPLATES.setdefault(
    "astrbot",
    {
        "id": "astrbot_default",
        "type": "astrbot",
        "name": "AstrBot",
        "enabled": False,
        "ws_type": 0,
        "target": "",
        "listen_host": "0.0.0.0",
        "listen_port": 6200,
        "access_token": "",
        "bot_qq": 0,
        "admin_qq": [],
        "main_group": "",
        "sync": _default_sync(),
    },
)


class ConnectionValidationError(ValueError):
    """连接配置不符合 schema 时抛出。"""


_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")
_NAME_RE = re.compile(r"^.+$", re.S)


def _norm_id_list(value: Any) -> list[Any]:
    """把 int / csv 字符串 / 列表统一为列表（元素保持原样，交由上层解析）。"""
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value] if value not in (None, "") else []


def _validate_adapter(adapter: dict[str, Any], *, partial: bool = False) -> None:
    """校验单个适配器配置。

    partial=True 时仅校验给定键（用于增量更新），False 时校验完整对象。
    """
    if not isinstance(adapter, dict):
        raise ConnectionValidationError("adapter 必须是对象")

    def _get(key: str, default: Any) -> Any:
        return adapter[key] if key in adapter else default

    if "type" in adapter or not partial:
        if _get("type", "") not in ADAPTER_TYPES:
            raise ConnectionValidationError("adapter.type 只能为 websocket、astrbot 或 qqofficial")

    # qqofficial 专属字段：AppID / AppSecret / 沙箱开关
    if "app_id" in adapter or not partial:
        app_id = str(_get("app_id", "")).strip()
        if not re.fullmatch(r"[0-9A-Za-z]{0,32}", app_id):
            raise ConnectionValidationError("adapter.app_id 只能是不超过 32 位的字母数字")

    if "app_secret" in adapter or not partial:
        secret = _get("app_secret", "")
        if not isinstance(secret, str) or not (0 <= len(secret) <= 128):
            raise ConnectionValidationError("adapter.app_secret 必须是长度不超过 128 的字符串")

    if "sandbox" in adapter or not partial:
        if type(_get("sandbox", False)) is not bool:
            raise ConnectionValidationError("adapter.sandbox 必须是布尔值")

    # qqofficial 专属字段：连接间隔（毫秒），0 表示按指数退避自动重连
    if "connect_interval" in adapter or not partial:
        interval = _get("connect_interval", 60000)
        if isinstance(interval, bool) or not isinstance(interval, (int, float)) \
                or not 0 <= float(interval) <= 86400000:
            raise ConnectionValidationError("adapter.connect_interval 必须是 0 至 86400000 之间的毫秒数")

    # qqofficial 专属字段：附加事件订阅位（按位或叠加到默认 Intents）
    # 官方 Intents 域上限为 1<<30，非法位（超出 30 位的值）会导致网关拒连
    if "extra_intents" in adapter or not partial:
        intents = _get("extra_intents", 0)
        if isinstance(intents, bool) or not isinstance(intents, int) \
                or not 0 <= intents <= (1 << 31) - 1:
            raise ConnectionValidationError("adapter.extra_intents 必须是 0 至 2^31-1 之间的整数")

    if "name" in adapter or not partial:
        name = str(_get("name", "")).strip()
        if not name or len(name) > 64:
            raise ConnectionValidationError("adapter.name 必须是 1 至 64 个字符")

    if "enabled" in adapter or not partial:
        if type(_get("enabled", False)) is not bool:
            raise ConnectionValidationError("adapter.enabled 必须是布尔值")

    if "ws_type" in adapter or not partial:
        ws = _get("ws_type", 0)
        # bool 是 int 子类：True/False 会通过 in (0,1) 检查，须显式排除
        if isinstance(ws, bool) or ws not in (0, 1):
            raise ConnectionValidationError("adapter.ws_type 只能为 0（正向）或 1（反向）")

    # target：反向 WS 时必填且必须是 ws:// 或 wss://
    target = str(_get("target", "") or "").strip()
    if "target" in adapter or not partial:
        if target:
            from urllib.parse import urlparse

            parsed = urlparse(target)
            if parsed.scheme not in {"ws", "wss"} or not parsed.netloc:
                raise ConnectionValidationError("adapter.target 必须是有效的 ws:// 或 wss:// 地址")

    if "listen_host" in adapter or not partial:
        host = str(_get("listen_host", "")).strip()
        if not host or "://" in host:
            raise ConnectionValidationError("adapter.listen_host 必须是有效监听地址")

    if "listen_port" in adapter or not partial:
        port = _get("listen_port", 0)
        if type(port) is not int or not 1 <= port <= 65535:
            raise ConnectionValidationError("adapter.listen_port 必须位于 1 至 65535 之间")

    if "access_token" in adapter or not partial:
        token = _get("access_token", "")
        if not isinstance(token, str) or len(token) > 4096:
            raise ConnectionValidationError("adapter.access_token 必须是长度不超过 4096 的字符串")

    if "bot_qq" in adapter or not partial:
        qq = _get("bot_qq", 0)
        if isinstance(qq, bool) or not isinstance(qq, int) or not 0 <= qq <= 999999999999999:
            raise ConnectionValidationError("adapter.bot_qq 必须是非负整数")

    # qqofficial 的 main_group / admin_qq 是 openid 字符串（字母数字），仅做字符集校验
    qq_official = str(_get("type", "")) == "qqofficial"
    for key in ("admin_qq", "main_group"):
        if key in adapter or not partial:
            items = _norm_id_list(_get(key, []))
            if len(items) > 100:
                raise ConnectionValidationError(f"adapter.{key} 最多包含 100 个号码")
            if qq_official:
                # 官方域：group_openid / 用户 openid 均为字符串标识
                for item in items:
                    if isinstance(item, bool) or not re.fullmatch(
                        r"[0-9A-Za-z_-]{4,64}", str(item).strip()
                    ):
                        raise ConnectionValidationError(
                            f"adapter.{key} 中包含无效的 openid"
                        )
                continue
            for item in items:
                if isinstance(item, bool):
                    raise ConnectionValidationError(f"adapter.{key} 中不能包含布尔值")
                try:
                    number = int(str(item).strip())
                except (TypeError, ValueError) as exc:
                    raise ConnectionValidationError(f"adapter.{key} 中包含无效号码") from exc
                if number <= 0 or number > 999999999999999:
                    raise ConnectionValidationError(f"adapter.{key} 中包含超出范围的号码")

    if "sync" in adapter or not partial:
        sync = _get("sync", {})
        if not isinstance(sync, dict):
            raise ConnectionValidationError("adapter.sync 必须是对象")
        merged = copy.deepcopy(_default_sync())
        for key, value in sync.items():
            if key not in merged:
                raise ConnectionValidationError(f"adapter.sync 不支持的配置项：{key}")
            expected = merged[key]
            if isinstance(expected, bool) and type(value) is not bool:
                raise ConnectionValidationError(f"adapter.sync.{key} 必须是布尔值")
            if isinstance(expected, int) and not isinstance(expected, bool):
                if isinstance(value, bool) or not isinstance(value, int):
                    raise ConnectionValidationError(f"adapter.sync.{key} 必须是整数")
            if isinstance(expected, str) and not isinstance(value, str):
                raise ConnectionValidationError(f"adapter.sync.{key} 必须是字符串")
        if "max_message_length" in sync:
            length = int(sync["max_message_length"])
            if not 1 <= length <= 4096:
                raise ConnectionValidationError("adapter.sync.max_message_length 必须位于 1 至 4096 之间")


def _merge_adapter(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """深合并适配器补丁（sync 子对象递归合并）。"""
    result = copy.deepcopy(base)
    for key, value in patch.items():
        if key == "sync" and isinstance(value, dict) and isinstance(result.get("sync"), dict):
            result["sync"].update(copy.deepcopy(value))
        else:
            result[key] = copy.deepcopy(value)
    return result


class ConnectionManager:
    """适配器连接配置加载器：connections.json 持久化 + 旧版配置迁移。"""

    def __init__(self, data_folder: Path, logger: Any, legacy: dict[str, Any] | None = None) -> None:
        self.logger = logger
        self.path = Path(data_folder) / "connections.json"
        self.adapters: list[dict[str, Any]] = []
        self._lock = threading.RLock()
        self.load(legacy=legacy)

    # ------------------------------------------------------------------ load
    def load(self, *, legacy: dict[str, Any] | None = None) -> None:
        """加载 connections.json；首次生成时若提供 legacy（旧 config.json 键）则迁移。"""
        with self._lock:
            raw: Any = {}
            fresh = True
            if self.path.is_file():
                try:
                    raw = json.loads(self.path.read_text(encoding="utf-8"))
                    fresh = False
                except (json.JSONDecodeError, OSError) as e:
                    self.logger.error(_t("connections.load_error", error=e))
                    raw = {}
                    # 文件损坏视同首次生成：允许 legacy 迁移重建可用配置
                    fresh = True
            raw = raw if isinstance(raw, dict) else {}
            adapters = raw.get("adapters")
            if not isinstance(adapters, list) or not adapters:
                adapters = copy.deepcopy(DEFAULT_ADAPTERS)
                # 旧版 config.json 存在连接信息时，迁移进首张 WebSocket 卡片
                if legacy and fresh:
                    adapters[0] = self._migrate_legacy(adapters[0], legacy)
                raw = {"version": 1, "adapters": adapters}
            else:
                # 补全缺失字段并按默认顺序排列已知键
                normalized: list[dict[str, Any]] = []
                for item in adapters:
                    if not isinstance(item, dict) or item.get("type") not in ADAPTER_TYPES:
                        continue
                    merged = _merge_adapter(self._blank(item.get("type", "websocket")), item)
                    if not str(merged.get("id") or "") or not _ID_RE.match(str(merged["id"])):
                        merged["id"] = self._gen_id(str(merged.get("type")))
                    # 撤下默认 AstrBot 卡片：未启用且从未配置的 astrbot_default 直接移除
                    if (
                        merged.get("id") == "astrbot_default"
                        and not merged.get("enabled")
                        and not str(merged.get("target") or "")
                        and not str(merged.get("access_token") or "")
                    ):
                        continue
                    # 合并后逐个校验：单个适配器非法仅告警跳过，不中断其余适配器加载
                    try:
                        _validate_adapter(merged)
                    except ConnectionValidationError as e:
                        self.logger.warning(
                            _t(
                                "connections.adapter_invalid",
                                name=merged.get("name"),
                                id=merged.get("id"),
                                error=e,
                            )
                        )
                        continue
                    normalized.append(merged)
                raw = {"version": 1, "adapters": normalized or copy.deepcopy(DEFAULT_ADAPTERS)}
            self.adapters = raw["adapters"]
            self._write_locked()

    def _blank(self, adapter_type: str) -> dict[str, Any]:
        """按类型生成空白适配器模板。"""
        template = ADAPTER_TEMPLATES.get(adapter_type)
        if template is None:
            raise ConnectionValidationError(f"未知适配器类型：{adapter_type}")
        result = copy.deepcopy(template)
        result["id"] = self._gen_id(adapter_type)
        default_names = {
            "websocket": "WebSocket",
            "astrbot": "AstrBot",
            "qqofficial": "QQ 官方机器人",
        }
        result["name"] = default_names.get(adapter_type, adapter_type)
        return result

    @staticmethod
    def _gen_id(adapter_type: str) -> str:
        prefix = {"websocket": "ws", "astrbot": "astrbot", "qqofficial": "qo"}.get(
            adapter_type, "ad"
        )
        return f"{prefix}_{uuid.uuid4().hex[:8]}"

    @staticmethod
    def _migrate_legacy(blank: dict[str, Any], legacy: dict[str, Any]) -> dict[str, Any]:
        """把旧 config.json 的 connection/admin_qq/main_group/sync 迁移进适配器。"""
        result = copy.deepcopy(blank)
        conn = legacy.get("connection") or {}
        if isinstance(conn, dict):
            result["ws_type"] = int(conn.get("ws_type", 0) or 0)
            result["target"] = str(conn.get("target", "") or "")
            result["listen_host"] = str(conn.get("listen_host", "0.0.0.0") or "0.0.0.0")
            result["listen_port"] = int(conn.get("listen_port", 3002) or 3002)
            result["access_token"] = str(conn.get("access_token", "") or "")
            result["bot_qq"] = int(conn.get("bot_qq", 0) or 0)
        if legacy.get("admin_qq"):
            # 旧配置可能为 int / csv 字符串 / 列表；list(str) 会把 csv 拆成
            # 单字符列表导致管理员全部失效，统一走 parse_groups 归一化
            result["admin_qq"] = ConnectionManager.parse_groups(legacy["admin_qq"])
        if legacy.get("main_group") not in (None, "", 0):
            # 归一化为列表，与新卡片保存格式一致（parse_groups 亦兼容 int/csv）
            result["main_group"] = ConnectionManager.parse_groups(legacy["main_group"])
        sync = legacy.get("sync")
        if isinstance(sync, dict):
            merged = copy.deepcopy(result["sync"])
            for key, value in sync.items():
                if key in merged:
                    merged[key] = copy.deepcopy(value)
            result["sync"] = merged
        return result

    def _write_locked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        payload = {"version": 1, "adapters": self.adapters}
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=4), encoding="utf-8"
        )
        os.replace(tmp, self.path)

    # ------------------------------------------------------------------ CRUD
    @staticmethod
    def is_masked(value: Any) -> bool:
        """值是否为密钥掩码（纯 * 字符串，长度与原值一致）。"""
        return isinstance(value, str) and len(value) > 0 and set(value) == {"*"}

    def snapshot(self, *, mask: bool = True) -> list[dict[str, Any]]:
        """返回适配器列表深拷贝；mask=True 时以等长 * 隐藏密钥。"""
        with self._lock:
            data = copy.deepcopy(self.adapters)
        if mask:
            for adapter in data:
                for key in ("access_token", "app_secret"):
                    value = adapter.get(key)
                    if value:
                        adapter[key] = "*" * len(str(value))
        return data

    def get(self, adapter_id: str) -> dict[str, Any] | None:
        with self._lock:
            for adapter in self.adapters:
                if adapter.get("id") == adapter_id:
                    return copy.deepcopy(adapter)
        return None

    def create(self, patch: dict[str, Any]) -> dict[str, Any]:
        """新建适配器；patch 至少包含 type，其余字段取默认值。"""
        if not isinstance(patch, dict):
            raise ConnectionValidationError("adapter 必须是对象")
        adapter_type = str(patch.get("type", "")).strip()
        if adapter_type not in ADAPTER_TYPES:
            raise ConnectionValidationError("adapter.type 只能为 websocket、astrbot 或 qqofficial")
        created = self._blank(adapter_type)
        # 「添加适配器」创建的卡片默认启用：开关默认打开，未配置完成前
        # hub 因 is_configured() 不通过不会实际建连（初始默认卡片不受影响）
        created["enabled"] = True
        # 默认命名：同类型已有 N 个则追加序号
        with self._lock:
            same = [a for a in self.adapters if a.get("type") == adapter_type]
            base_name = {"websocket": "WebSocket", "astrbot": "AstrBot",
                         "qqofficial": "QQ 官方机器人"}.get(adapter_type, "Adapter")
            default_name = base_name if not same else f"{base_name} {len(same) + 1}"
        created["name"] = default_name
        patch = {k: v for k, v in patch.items() if k not in ("id",)}
        # WebUI 未配置密钥时（******）不覆盖
        patch = self._unmask_patch(patch, created)
        created = _merge_adapter(created, patch)
        _validate_adapter(created)
        with self._lock:
            self._ensure_unique_name(created, exclude=None)
            self._ensure_unique_listen_port(created, exclude=None)
            self.adapters.append(created)
            self._write_locked()
            return copy.deepcopy(created)

    def update(self, adapter_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        """按 id 增量更新适配器配置。"""
        if not isinstance(patch, dict):
            raise ConnectionValidationError("adapter 必须是对象")
        patch = {k: v for k, v in patch.items() if k not in ("id", "type")}
        with self._lock:
            # enumerate 按 id 定位：list.index 按 == 匹配整个字典，
            # 两张内容相同的卡片会定位到错误索引
            current: dict[str, Any] | None = None
            current_index = -1
            for index, adapter in enumerate(self.adapters):
                if adapter.get("id") == adapter_id:
                    current, current_index = adapter, index
                    break
            if current is None:
                raise ConnectionValidationError(_t("connections.not_found", id=adapter_id))
            patch = self._unmask_patch(patch, current)
            merged = _merge_adapter(current, patch)
            _validate_adapter(merged)
            self._ensure_unique_name(merged, exclude=adapter_id)
            self._ensure_unique_listen_port(merged, exclude=adapter_id)
            self.adapters[current_index] = merged
            self._write_locked()
            return copy.deepcopy(merged)

    def delete(self, adapter_id: str) -> bool:
        with self._lock:
            for adapter in self.adapters:
                if adapter.get("id") == adapter_id:
                    if len(self.adapters) <= 1:
                        raise ConnectionValidationError(_t("connections.keep_one"))
                    self.adapters.remove(adapter)
                    self._write_locked()
                    return True
        return False

    def _unmask_patch(self, patch: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
        """密钥掩码回填：前端提交等长 * 掩码时保留原值。"""
        if self.is_masked(patch.get("access_token")) or self.is_masked(patch.get("app_secret")):
            patch = dict(patch)
            if self.is_masked(patch.get("access_token")):
                patch["access_token"] = str(current.get("access_token", "") or "")
            if self.is_masked(patch.get("app_secret")):
                patch["app_secret"] = str(current.get("app_secret", "") or "")
        return patch

    def _ensure_unique_name(self, adapter: dict[str, Any], *, exclude: str | None) -> None:
        """卡片名去重：重名自动追加序号（保持展示唯一）。"""
        names = {
            str(a.get("name"))
            for a in self.adapters
            if a.get("id") != exclude and a is not adapter
        }
        name = str(adapter.get("name", "")).strip()
        if name not in names:
            adapter["name"] = name
            return
        for i in range(2, 100):
            candidate = f"{name} {i}"
            if candidate not in names:
                adapter["name"] = candidate
                return
        adapter["name"] = f"{name} {uuid.uuid4().hex[:4]}"

    def _ensure_unique_listen_port(self, adapter: dict[str, Any], *, exclude: str | None) -> None:
        """反向适配器的监听端口不得与其他适配器冲突（0.0.0.0 会抢占全部网卡，仅按端口判断）。"""
        if int(adapter.get("ws_type", 0) or 0) != 1:
            return
        port = int(adapter.get("listen_port", 0) or 0)
        for other in self.adapters:
            if other is adapter or (exclude is not None and str(other.get("id")) == str(exclude)):
                continue
            if int(other.get("ws_type", 0) or 0) == 1 and int(other.get("listen_port", 0) or 0) == port:
                raise ConnectionValidationError(
                    _t("connections.port_conflict", name=other.get("name"), port=port)
                )

    # ------------------------------------------------------------------ views
    def adapters_view(self) -> list[dict[str, Any]]:
        """持锁返回适配器列表浅拷贝，供外部安全迭代（防并发 CRUD 变更列表）。"""
        with self._lock:
            return list(self.adapters)

    @property
    def websocket_adapters(self) -> list[dict[str, Any]]:
        with self._lock:
            return [a for a in self.adapters if a.get("type") == "websocket"]

    @property
    def astrbot_adapters(self) -> list[dict[str, Any]]:
        with self._lock:
            return [a for a in self.adapters if a.get("type") == "astrbot"]

    def primary_websocket(self) -> dict[str, Any] | None:
        """主 WebSocket 适配器（深拷贝）：第一个启用且配置完整的，否则第一个。

        返回深拷贝防止调用方改写返回值直接污染内部 adapters 列表；
        需要持久化修改请走 update()。
        """
        candidates = self.websocket_adapters
        if not candidates:
            return None
        for adapter in candidates:
            if adapter.get("enabled") and self.is_configured(adapter):
                return copy.deepcopy(adapter)
        return copy.deepcopy(candidates[0])

    @staticmethod
    def is_configured(adapter: dict[str, Any]) -> bool:
        """适配器是否已填写有效连接信息。

        websocket / astrbot 判定一致：正向(ws_type=0) 需填目标地址，
        反向(ws_type=1) 需填监听端口；qqofficial 需填 AppID 与 AppSecret。
        """
        if str(adapter.get("type", "")) == "qqofficial":
            return bool(
                str(adapter.get("app_id", "") or "").strip()
                and str(adapter.get("app_secret", "") or "").strip()
            )
        if int(adapter.get("ws_type", 0) or 0) == 0:
            return bool(str(adapter.get("target", "") or "").strip())
        return int(adapter.get("listen_port", 0) or 0) > 0

    @staticmethod
    def parse_groups(value: Any) -> list[int]:
        """解析 main_group（int / csv / list）为群号列表。"""
        result: list[int] = []
        for item in _norm_id_list(value):
            try:
                number = int(str(item).strip())
            except (TypeError, ValueError):
                continue
            if number > 0:
                result.append(number)
        return result

    @staticmethod
    def parse_groups_loose(value: Any) -> list[str]:
        """宽松解析 main_group：保留原始 token（QQ 官方适配器的 group_openid）。"""
        result: list[str] = []
        for item in _norm_id_list(value):
            token = str(item).strip()
            if token and token not in result:
                result.append(token)
        return result

    def all_groups(self) -> list[int]:
        """所有 WebSocket 适配器的群号并集（保持出现顺序）。"""
        seen: dict[int, None] = {}
        for adapter in self.websocket_adapters:
            for gid in self.parse_groups(adapter.get("main_group")):
                seen.setdefault(gid, None)
        return list(seen.keys())

    def all_group_keys(self) -> list[str]:
        """所有适配器群标识的宽松并集（含 QQ 官方的 group_openid 字符串）。"""
        seen: dict[str, None] = {}
        with self._lock:
            for adapter in self.adapters:
                for key in self.parse_groups_loose(adapter.get("main_group")):
                    seen.setdefault(key, None)
        return list(seen.keys())

    def all_admins(self) -> list[int]:
        """所有 WebSocket 适配器管理员的并集。"""
        seen: dict[int, None] = {}
        for adapter in self.websocket_adapters:
            for qq in self.parse_groups(adapter.get("admin_qq")):
                seen.setdefault(qq, None)
        return list(seen.keys())

    def all_admin_keys(self) -> list[str]:
        """全部适配器管理员的宽松并集（含 QQ 官方域的 openid 字符串）。"""
        seen: dict[str, None] = {}
        with self._lock:
            for adapter in self.adapters:
                for key in self.parse_groups_loose(adapter.get("admin_qq")):
                    seen.setdefault(key, None)
        return list(seen.keys())

    def adapter_for_group(self, group_id: int) -> dict[str, Any] | None:
        """按群号查找所属的 WebSocket 适配器。"""
        for adapter in self.websocket_adapters:
            if group_id in self.parse_groups(adapter.get("main_group")):
                return adapter
        return None

    def primary_sync(self) -> dict[str, Any]:
        """主适配器的群服互通配置（深拷贝兼容视图）。"""
        primary = self.primary_websocket()
        if primary and isinstance(primary.get("sync"), dict):
            return primary["sync"]
        return _default_sync()
