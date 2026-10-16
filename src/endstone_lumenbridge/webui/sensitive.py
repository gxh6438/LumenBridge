"""配置敏感键脱敏与配置项标签构建（独立于 HTTP 层，供 server 与路由层共用）。"""

from __future__ import annotations

from typing import Any

from ..i18n import t as _t

# 敏感键：展示固定 6 个 *，保存时按纯星串还原
SENSITIVE_KEYS = {
    "access_token", "password", "secret",
    "app_secret", "client_secret", "api_key", "token", "private_key",
}

CONFIG_LABEL_KEYS: dict[str, str] = {
    "debug": "debug",
    "language": "language",
    "chat": "chat.section",
    "chat.forward_cancelled": "chat.forward_cancelled",
    "whitelist": "whitelist.section",
    "whitelist.enable": "whitelist.enable",
    "whitelist.auto_add": "whitelist.auto_add",
    "whitelist.bind_keyword": "whitelist.bind_keyword",
    "whitelist.unbind_keyword": "whitelist.unbind_keyword",
    "whitelist.remove_on_leave": "whitelist.remove_on_leave",
    "regex_engine": "regex_engine.section",
    "regex_engine.enable": "regex_engine.enable",
    "regex_engine.only_on_main": "regex_engine.only_on_main",
    "regex_engine.admin_debug": "regex_engine.admin_debug",
    "regex_engine.command_timeout": "regex_engine.command_timeout",
    "webui": "webui.section",
    "webui.enable": "webui.enable",
    "webui.host": "webui.host",
    "webui.port": "webui.port",
    "webui.password": "webui.password",
    "webui.secret": "webui.secret",
    "background": "background.section",
    "background.enable": "background.enable",
    "background.api_url": "background.api_url",
    "background.blur_strength": "background.blur_strength",
    "background.fallback_to_default": "background.fallback_to_default",
    "background.cache_seconds": "background.cache_seconds",
    "pip": "pip.section",
    "pip.enable": "pip.enable",
    "pip.index_url": "pip.index_url",
    "pip.timeout": "pip.timeout",
    "marketplace": "marketplace.section",
    "marketplace.enable": "marketplace.enable",
    "marketplace.api_url": "marketplace.api_url",
    "marketplace.allow_http": "marketplace.allow_http",
    "marketplace.timeout": "marketplace.timeout",
    "marketplace.max_download_bytes": "marketplace.max_download_bytes",
    "marketplace.check_on_start": "marketplace.check_on_start",
    "marketplace.check_interval_seconds": "marketplace.check_interval_seconds",
    "updates": "updates.section",
    "updates.enable": "updates.enable",
    "updates.api_url": "updates.api_url",
    "updates.timeout": "updates.timeout",
    "updates.auto_update": "updates.auto_update",
    "commands": "commands.section",
    "commands.allow_in_game": "commands.allow_in_game",
    "commands.status.allow_player": "commands.status.allow_player",
    "commands.reload.allow_player": "commands.reload.allow_player",
    "commands.say.allow_player": "commands.say.allow_player",
    "commands.plugins.allow_player": "commands.plugins.allow_player",
    "commands.update.allow_player": "commands.update.allow_player",
    "commands.pip.allow_in_game": "commands.pip.allow_in_game",
    "commands.pip.allow_player": "commands.pip.allow_player",
}


def build_config_labels() -> dict[str, dict[str, str]]:
    """生成配置标签字典；请求时调用以使用当前语言（模块加载时语言未初始化）。"""
    result: dict[str, dict[str, str]] = {}
    for path, label_key in CONFIG_LABEL_KEYS.items():
        if label_key.endswith(".section"):
            section_key = label_key[:-8]
            result[path] = {"_": _t(f"config_labels.{section_key}.section")}
        else:
            result[path] = {
                "label": _t(f"config_labels.{label_key}.label"),
                "desc": _t(f"config_labels.{label_key}.desc"),
            }
    return result


def mask_sensitive(obj: Any) -> None:
    """递归脱敏：敏感键固定 6 个 *，防长度侧信道。"""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in SENSITIVE_KEYS and v:
                obj[k] = "******"
            else:
                mask_sensitive(v)
    elif isinstance(obj, list):
        for item in obj:
            mask_sensitive(item)


def unmask_sensitive(new: Any, old: Any) -> None:
    """还原被脱敏的值：纯星串回填旧值。"""
    if isinstance(new, dict) and isinstance(old, dict):
        for k, v in new.items():
            if k in SENSITIVE_KEYS and isinstance(v, str) and v and set(v) == {"*"}:
                new[k] = old.get(k, "")
            else:
                unmask_sensitive(v, old.get(k))
    elif isinstance(new, list) and isinstance(old, list):
        for i in range(min(len(new), len(old))):
            unmask_sensitive(new[i], old[i])
