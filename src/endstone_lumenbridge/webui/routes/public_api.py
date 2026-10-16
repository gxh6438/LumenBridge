"""无需鉴权的公共 API：健康检查、i18n 语言包、登录页背景配置。"""

from __future__ import annotations

import re
from typing import Any

from ...i18n import (
    AUTO_DETECT,
    DEFAULT_LANGUAGE,
    SUPPORTED_LANGUAGES,
    get_i18n,
    normalize_locale,
)
from .base import RouteMixin


class PublicApi(RouteMixin):

    def api_health(self, m: re.Match, query: dict[str, str]) -> None:
        self._send_json({"code": 200, "data": {
            "ok": True,
            "version": str(getattr(self.plugin, "VERSION", "")),
        }})

    def api_i18n_languages(self, m: re.Match, query: dict[str, str]) -> None:
        i18n = get_i18n()
        self._send_json({"code": 200, "data": {
            "languages": i18n.available_languages(),
            "default": DEFAULT_LANGUAGE,
            "auto": AUTO_DETECT,
        }})

    def api_i18n_current(self, m: re.Match, query: dict[str, str]) -> None:
        plugin = self.plugin
        configured = "auto"
        try:
            configured = plugin.config_manager.language
        except Exception:  # noqa: BLE001
            pass
        self._send_json({"code": 200, "data": {
            "language": plugin.language if hasattr(plugin, "language") else DEFAULT_LANGUAGE,
            "configured": configured,
        }})

    def api_i18n_language(self, m: re.Match, query: dict[str, str]) -> None:
        lang = normalize_locale(m.group(1))
        if lang not in SUPPORTED_LANGUAGES:
            self.api_error(404, "webui.msg.lang_not_found")
            return
        self._send_json({"code": 200, "data": get_i18n().export(lang)})

    def api_public_background(self, m: re.Match, query: dict[str, str]) -> None:
        bg: dict[str, Any] = {}
        try:
            bg = self.plugin.config_manager.background or {}
        except Exception:  # noqa: BLE001
            bg = {}
        self._send_json({"code": 200, "data": bg})
