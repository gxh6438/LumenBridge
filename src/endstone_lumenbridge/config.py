"""LumenBridge 配置管理：加载、校验与原子写入，存放在插件数据目录的 config.json。"""

from __future__ import annotations

import copy
import json
import os
import threading
import urllib.parse
from pathlib import Path
from typing import Any

from .i18n import t as _t

DEFAULT_CONFIG: dict[str, Any] = {
    # v1.2.0 起 connection / admin_qq / main_group / sync 迁移至 connections.json
    # （见 connections.ConnectionManager），由适配器卡片单独配置。
    "debug": False,

    # "auto" = 启动时自动检测 Endstone 服务器语言
    "language": "auto",

    "whitelist": {
        "enable": True,
        "auto_add": True,
        "bind_keyword": "绑定白名单",
        "unbind_keyword": "解绑白名单",
        "remove_on_leave": True,
    },

    "regex_engine": {
        "enable": True,
        "only_on_main": True,
        "admin_debug": False,
        "command_timeout": 5.0,
    },

    "webui": {
        "enable": True,
        "host": "127.0.0.1",               # 外网访问改为 0.0.0.0，注意安全
        "port": 8300,
        "password": "*",                   # "*" = 启动时生成随机密码并打印到控制台
        "secret": "",                      # 留空自动生成
    },

    "background": {
        "enable": True,
        "api_url": "https://t.alcy.cc/fj",
        "blur_strength": 0,
        "fallback_to_default": True,
        "cache_seconds": 600,
    },

    "pip": {
        "enable": True,
        "index_url": "",                   # 留空=官方 PyPI；中文环境首次生成默认填腾讯源
        "timeout": 300,
    },

    "marketplace": {
        "enable": True,                    # 默认启用；只需填站点根地址，后台自动补全 /api/v1
        "api_url": "https://market.mxcraft.vip",
        "allow_http": False,               # 生产环境应保持 False；仅本地调试可显式开启 HTTP
        "timeout": 30,
        "max_download_bytes": 67108864,
        "check_on_start": True,
        "check_interval_seconds": 21600,
    },

    "updates": {
        "enable": True,
        "api_url": "https://market.mxcraft.vip",  # 只填站点根地址，自动补全 /api/v1/updates/lumenbridge
        "timeout": 30,
        # 发现新版本时自动下载并热重载生效（禁用自身→安装新 wheel→启用新实例）
        "auto_update": True,
    },

    "commands": {
        "allow_in_game": False,
        "status": {"allow_player": False},
        "reload": {"allow_player": False},
        "say": {"allow_player": False},
        "plugins": {"allow_player": False},
        "pip": {
            "allow_in_game": False,
            "allow_player": False,
        },
    },
}

class ConfigValidationError(ValueError):
    """配置不符合核心 schema 时抛出。"""


def _is_nonempty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _validate_id_list(value: Any, path: str, *, allow_csv: bool = False) -> None:
    """校验 QQ/群号，拒绝 bool、空值、负数和过长集合。"""
    if allow_csv and isinstance(value, str):
        values = [item.strip() for item in value.split(",") if item.strip()]
    elif isinstance(value, (list, tuple)):
        values = list(value)
    else:
        values = [value]
    if not values or len(values) > 100:
        raise ConfigValidationError(f"{path} 必须包含 1 至 100 个有效号码")
    normalized: set[int] = set()
    for item in values:
        if isinstance(item, bool):
            raise ConfigValidationError(f"{path} 中不能包含布尔值")
        try:
            number = int(str(item).strip())
        except (TypeError, ValueError) as exc:
            raise ConfigValidationError(f"{path} 中包含无效号码") from exc
        if number <= 0 or number > 999999999999999:
            raise ConfigValidationError(f"{path} 中包含超出范围的号码")
        normalized.add(number)
    if len(normalized) != len(values):
        raise ConfigValidationError(f"{path} 不能包含重复号码")


# v1.2.0 起迁出 config.json 的旧连接键（加载时迁移到 connections.json 后剥离）
LEGACY_CONNECTION_KEYS = ("connection", "admin_qq", "main_group", "sync")


def _validate_patch_shape(patch: dict[str, Any], schema: dict[str, Any], prefix: str = "") -> None:
    """按 DEFAULT_CONFIG 验证补丁键与基本类型，阻止未知键写入磁盘。"""
    for key, value in patch.items():
        path = f"{prefix}.{key}" if prefix else key
        if key not in schema:
            raise ConfigValidationError(f"不支持的配置项：{path}")
        expected = schema[key]
        if isinstance(expected, dict):
            if not isinstance(value, dict):
                raise ConfigValidationError(f"{path} 必须是对象")
            _validate_patch_shape(value, expected, path)
            continue
        if isinstance(expected, bool):
            if type(value) is not bool:
                raise ConfigValidationError(f"{path} 必须是布尔值")
        elif isinstance(expected, int):
            if type(value) is not int:
                raise ConfigValidationError(f"{path} 必须是整数")
        elif isinstance(expected, float):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ConfigValidationError(f"{path} 必须是数值")
        elif isinstance(expected, list):
            if not isinstance(value, list):
                raise ConfigValidationError(f"{path} 必须是数组")
        elif isinstance(expected, str):
            if not isinstance(value, str):
                raise ConfigValidationError(f"{path} 必须是字符串")


def _validate_effective_config(config: dict[str, Any]) -> None:
    """校验合并后配置的跨字段与范围约束。"""
    if not 1 <= config["webui"]["port"] <= 65535:
        raise ConfigValidationError("webui.port 必须位于 1 至 65535 之间")
    if not 0.1 <= float(config["regex_engine"]["command_timeout"]) <= 60:
        raise ConfigValidationError("regex_engine.command_timeout 必须位于 0.1 至 60 秒之间")
    if not 0 <= config["background"]["blur_strength"] <= 100:
        raise ConfigValidationError("background.blur_strength 必须位于 0 至 100 之间")
    if not 30 <= config["background"]["cache_seconds"] <= 86400:
        raise ConfigValidationError("background.cache_seconds 必须位于 30 至 86400 秒之间")
    if not 10 <= config["pip"]["timeout"] <= 3600:
        raise ConfigValidationError("pip.timeout 必须位于 10 至 3600 秒之间")
    if not 5 <= config["marketplace"]["timeout"] <= 120:
        raise ConfigValidationError("marketplace.timeout 必须位于 5 至 120 秒之间")
    if not 1024 * 1024 <= config["marketplace"]["max_download_bytes"] <= 128 * 1024 * 1024:
        raise ConfigValidationError("marketplace.max_download_bytes 必须位于 1 至 128 MiB 之间")
    if not 60 <= config["marketplace"]["check_interval_seconds"] <= 7 * 86400:
        raise ConfigValidationError("marketplace.check_interval_seconds 必须位于 60 秒至 7 天之间")
    if not 5 <= config["updates"]["timeout"] <= 120:
        raise ConfigValidationError("updates.timeout 必须位于 5 至 120 秒之间")

    background_url = str(config["background"]["api_url"]).strip()
    if background_url:
        parsed_background = urllib.parse.urlparse(background_url)
        if parsed_background.scheme not in {"http", "https"} or not parsed_background.netloc:
            raise ConfigValidationError("background.api_url 必须是有效的 http:// 或 https:// 地址")
    index_url = str(config["pip"]["index_url"]).strip()
    if index_url:
        parsed_index = urllib.parse.urlparse(index_url)
        if parsed_index.scheme not in {"http", "https"} or not parsed_index.netloc:
            raise ConfigValidationError("pip.index_url 必须是有效的 http:// 或 https:// 地址")
    for path, value in (("marketplace.api_url", config["marketplace"]["api_url"]), ("updates.api_url", config["updates"]["api_url"])):
        value = str(value).strip()
        if value:
            parsed_url = urllib.parse.urlparse(value)
            if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
                raise ConfigValidationError(f"{path} 必须是有效的 http:// 或 https:// 地址")
    if not _is_nonempty_string(config["webui"]["password"]):
        raise ConfigValidationError("webui.password 不能为空")
    for path, value in (("webui.secret", config["webui"]["secret"]),):
        if not isinstance(value, str) or len(value) > 4096:
            raise ConfigValidationError(f"{path} 必须是长度不超过 4096 的字符串")


def _strip_deprecated_pip_fields(config: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """移除已废弃字段：pip.allow_all / allow_list（v1.1.0）、旧版连接键（v1.2.0）
    与 marketplace.report_api_key（点赞/举报已改为完全匿名会话，无需密钥）。

    旧配置或旧浏览器标签页仍可能在完整表单提交中携带这些键；其余未知键仍严格拒绝。
    v1.2.0 起连接键迁移到 connections.json，这里仅剥离不校验。
    """
    sanitized = copy.deepcopy(config)
    changed = False
    pip_config = sanitized.get("pip")
    if isinstance(pip_config, dict):
        for key in ("allow_all", "allow_list"):
            if key in pip_config:
                pip_config.pop(key, None)
                changed = True
    market_config = sanitized.get("marketplace")
    if isinstance(market_config, dict) and "report_api_key" in market_config:
        market_config.pop("report_api_key", None)
        changed = True
    for key in LEGACY_CONNECTION_KEYS:
        if key in sanitized:
            sanitized.pop(key, None)
            changed = True
    return sanitized, changed


def extract_legacy_connection(raw: dict[str, Any]) -> dict[str, Any]:
    """从旧 config.json 原始数据中提取连接相关键（供 connections.json 迁移）。"""
    payload: dict[str, Any] = {}
    for key in LEGACY_CONNECTION_KEYS:
        if key in raw:
            payload[key] = copy.deepcopy(raw[key])
    return payload


def deep_merge(base: dict, override: dict) -> tuple[dict, bool]:
    """递归合并配置，返回 (合并结果, 是否有键被补全)。

    缺失键补全与嵌套 dict 均用 deepcopy：result 浅拷贝 override 时嵌套
    对象仍是输入引用，调用方后续修改 patch / _raw 会直接污染 self.data。
    """
    patched = False
    result = copy.deepcopy(override)
    for key, value in base.items():
        if key not in result:
            result[key] = copy.deepcopy(value)
            patched = True
        elif isinstance(value, dict) and isinstance(result[key], dict):
            merged, sub_patched = deep_merge(value, result[key])
            result[key] = merged
            patched = patched or sub_patched
    return result, patched


class ConfigManager:
    """插件配置加载器：JSON 持久化 + 缺失键自动补全。"""

    def __init__(self, data_folder: Path, logger: Any) -> None:
        self.logger = logger
        self.path = Path(data_folder) / "config.json"
        self.data: dict[str, Any] = {}
        self._raw: dict[str, Any] = {}
        # 旧版 config.json 中迁移前的连接配置载荷（首次生成 connections.json 时消费）
        self.legacy_connection: dict[str, Any] = {}
        # 连接管理器（v1.2.0）：connection/sync/main_group/admin_qq 的委托数据源
        self._connections: Any = None
        # 保护 cm.data 的读改写，避免 WebUI 并发 POST 互相覆盖
        self._save_lock = threading.RLock()
        # 避免 reload 时重复按语言覆盖 pip 镜像源
        self._pip_index_applied: bool = False
        self.load()

    def attach_connections(self, connections: Any) -> None:
        """绑定 ConnectionManager，旧连接属性转为委托视图。"""
        self._connections = connections

    def load(self) -> None:
        # 与 apply_patch / save / apply_pip_index_by_language 互斥，保证 self.data 读改写原子可见
        with self._save_lock:
            if self.path.is_file():
                try:
                    raw = json.loads(self.path.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError) as e:
                    self.logger.error(_t("plugin.config_error", error=e))
                    raw = {}
            else:
                raw = {}

            raw = raw if isinstance(raw, dict) else {}
            self.legacy_connection = extract_legacy_connection(raw)
            self._raw, migrated = _strip_deprecated_pip_fields(raw)
            self.data, patched = deep_merge(DEFAULT_CONFIG, self._raw)
            # 合并后校验跨字段约束（与 apply_patch 同源）：deep_merge 只补键不查值，
            # 手改配置的越界/非法值会原样流入运行时。校验失败时回退默认配置并落盘修复。
            try:
                _validate_effective_config(self.data)
            except (ConfigValidationError, KeyError, TypeError) as e:
                self.logger.error(_t("plugin.config_error", error=e))
                self.data = copy.deepcopy(DEFAULT_CONFIG)
                patched = True
            patched = patched or migrated
            self._pip_index_applied = False
            if patched or not self.path.is_file():
                self._write_locked()
                self.logger.info(_t("plugin.config_generated", path=self.path))

    def _write_locked(self) -> None:
        """已持有 _save_lock 的内部写入入口。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # 原子写入：先写临时文件再 os.replace，避免中途崩溃留下半截文件
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=4), encoding="utf-8"
        )
        os.replace(tmp, self.path)

    def apply_pip_index_by_language(self, language: str) -> bool:
        """根据界面语言设置 pip 镜像源默认值（中文=腾讯源，英文=官方 PyPI）。

        仅在 index_url 从未被用户设置过时生效；用户一旦显式设置（哪怕设为空字符串）就以用户值为准。
        """
        # 与 apply_patch / load 互斥：避免在 apply_patch 校验前一刻改写 pip 块
        with self._save_lock:
            if self._pip_index_applied:
                return False
            raw_pip = self._raw.get("pip", {}) if isinstance(self._raw.get("pip"), dict) else {}
            # 用户在原始配置中显式写了 index_url 键（无论值是什么），不覆盖
            if "index_url" in raw_pip:
                self._pip_index_applied = True
                return False
            pip_conf = self.data.get("pip", {})
            if pip_conf.get("index_url"):
                self._pip_index_applied = True
                return False
            if language in ("zh_CN", "zh_TW"):
                pip_conf["index_url"] = "https://mirrors.cloud.tencent.com/pypi/simple"
            else:
                pip_conf["index_url"] = ""
            self.data["pip"] = pip_conf
            self._pip_index_applied = True
            self._write_locked()
            return True

    def apply_patch(self, patch: dict[str, Any]) -> dict[str, Any]:
        """校验并原子应用配置补丁，返回更新后的深拷贝快照。

        加锁覆盖整个“读取—合并—校验—保存”窗口，避免并发请求互相覆盖。
        """
        if not isinstance(patch, dict):
            raise ConfigValidationError("配置必须是对象")
        patch, _ = _strip_deprecated_pip_fields(patch)
        _validate_patch_shape(patch, DEFAULT_CONFIG)
        with self._save_lock:
            candidate, _ = deep_merge(self.data, patch)
            _validate_effective_config(candidate)
            self.data = candidate
            self._write_locked()
            return copy.deepcopy(self.data)

    def save(self) -> None:
        """原子写入配置文件。"""
        with self._save_lock:
            self._write_locked()

    @property
    def connection(self) -> dict[str, Any]:
        """委托视图：主 WebSocket 适配器的连接配置（v1.2.0 起存于 connections.json）。"""
        if self._connections is not None:
            primary = self._connections.primary_websocket()
            if primary:
                return primary
        return {}

    @property
    def sync(self) -> dict[str, Any]:
        """委托视图：主适配器的群服互通配置。"""
        if self._connections is not None:
            return self._connections.primary_sync()
        return {}

    @property
    def whitelist(self) -> dict[str, Any]:
        return self.data["whitelist"]

    @property
    def regex_engine(self) -> dict[str, Any]:
        return self.data["regex_engine"]

    @property
    def main_group(self) -> int:
        """向后兼容：返回第一个主群号（或 0）。"""
        groups = self.main_groups
        return groups[0] if groups else 0

    @property
    def main_groups(self) -> list[int]:
        """全部启用适配器的主群并集（兼容 int 与逗号分隔字符串）。"""
        if self._connections is not None:
            return self._connections.all_groups()
        return []

    @property
    def admin_qq(self) -> list[int]:
        """全部启用适配器的管理员 QQ 并集；逐项跳过非法值。"""
        if self._connections is not None:
            return self._connections.all_admins()
        return []

    @property
    def admin_keys(self) -> list[str]:
        """管理员标识宽松并集（含 QQ 官方域 openid 字符串），供跨域权限判定。"""
        if self._connections is not None:
            return self._connections.all_admin_keys()
        return []

    @property
    def debug(self) -> bool:
        return bool(self.data.get("debug", False))

    @property
    def background(self) -> dict[str, Any]:
        return self.data.get("background", {})

    @property
    def language(self) -> str:
        """界面语言配置值；"auto" 表示自动检测。"""
        return str(self.data.get("language", "auto"))

    @property
    def pip(self) -> dict[str, Any]:
        return self.data.get("pip", {})

    @property
    def commands(self) -> dict[str, Any]:
        return self.data.get("commands", {})

    def check_command_permission(self, sender: Any, subcommand: str) -> tuple[bool, str]:
        """检查命令发送者是否有权执行某子命令，返回 (是否允许, 拒绝原因 i18n key 或空)。

        控制台识别用 isinstance(ConsoleCommandSender)（Endstone 0.11 原生类型），
        最稳健——玩家即使取名 "CONSOLE" 也不属于该类型。
        """
        from endstone.command import ConsoleCommandSender  # type: ignore

        if isinstance(sender, ConsoleCommandSender):
            return True, ""

        is_op = bool(getattr(sender, "is_op", False))

        cmd_conf = self.commands
        if not bool(cmd_conf.get("allow_in_game", False)):
            return False, "commands.not_allowed_in_game"

        if subcommand == "pip":
            pip_conf = cmd_conf.get("pip", {})
            if not bool(pip_conf.get("allow_in_game", False)):
                return False, "commands.pip_not_allowed_in_game"
            # pip 即使开启游戏内，仍仅限 OP
            if not is_op:
                return False, "commands.op_only"

        sub_conf = cmd_conf.get(subcommand, {})
        if not bool(sub_conf.get("allow_player", False)) and not is_op:
            return False, "commands.op_only"

        return True, ""
