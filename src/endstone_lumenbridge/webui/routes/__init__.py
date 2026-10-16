"""WebUI API 路由表与分派器。

路由条目为 (METHOD, pattern, handler)；pattern 用 fullmatch 匹配，
handler 签名 ``fn(handler, match, query)``。
表内顺序即匹配优先级：字面量路由必须排在参数化路由之前
（如 ``/api/market/plugins`` 先于 ``/api/market/plugin/<id>``）。
"""

from __future__ import annotations

import re
from typing import Callable

from ...i18n import t as _t
from .auth_api import AuthApi
from .base import RouteMixin
from .config_api import ConfigApi
from .connections_api import ConnectionsApi
from .market_api import MarketApi
from .modules_api import ModulesApi
from .pip_api import PipApi
from .public_api import PublicApi
from .subplugins_api import SubpluginsApi
from .system_api import SystemApi

Route = tuple[str, re.Pattern[str], Callable[[RouteMixin, re.Match[str], dict[str, str]], None]]


def _lit(path: str) -> re.Pattern[str]:
    return re.compile(re.escape(path))


def _pat(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern)


# 无需鉴权的公共路由（登录页所需）
PUBLIC_ROUTES: list[Route] = [
    ("POST", _lit("/api/auth/login"), AuthApi.api_login),
    ("GET", _lit("/api/health"), PublicApi.api_health),
    ("GET", _lit("/api/i18n/languages"), PublicApi.api_i18n_languages),
    ("GET", _lit("/api/i18n/current"), PublicApi.api_i18n_current),
    ("GET", _pat(r"/api/i18n/([\w-]+)"), PublicApi.api_i18n_language),
    ("GET", _lit("/api/public/background"), PublicApi.api_public_background),
]

# 需鉴权路由
AUTHED_ROUTES: list[Route] = [
    # ---- 系统 ----
    ("GET", _lit("/api/overview"), SystemApi.api_overview),
    ("GET", _lit("/api/server/metrics"), SystemApi.api_server_metrics),
    ("GET", _lit("/api/logs"), SystemApi.api_logs),
    ("GET", _lit("/api/logs/stream"), SystemApi.api_logs_stream),
    ("POST", _lit("/api/reload"), SystemApi.api_framework_reload),
    # ---- 配置 ----
    ("GET", _lit("/api/config"), ConfigApi.api_config_get),
    ("POST", _lit("/api/config"), ConfigApi.api_config_post),
    ("GET", _lit("/api/config/labels"), ConfigApi.api_config_labels),
    ("GET", _lit("/api/plugins/configs"), ConfigApi.api_plugins_configs),
    ("GET", _pat(r"/api/plugins/config/([A-Za-z0-9_\-]+)"), ConfigApi.api_plugin_config_get),
    ("POST", _pat(r"/api/plugins/config/([A-Za-z0-9_\-]+)"), ConfigApi.api_plugin_config_post),
    # ---- 连接管理（reload/reveal/bindkey 是保留子路径，不能落入 <id> 参数路由）----
    ("GET", _lit("/api/connections"), ConnectionsApi.api_connections_get),
    ("POST", _lit("/api/connections"), ConnectionsApi.api_connections_create),
    ("POST", _lit("/api/connections/reload"), ConnectionsApi.api_connections_reload),
    ("POST", _lit("/api/connections/reveal"), ConnectionsApi.api_connections_reveal),
    ("POST", _lit("/api/connections/bindkey"), ConnectionsApi.api_connections_bindkey),
    ("PUT", _pat(r"/api/connections/((?!(?:reload|reveal|bindkey)$)[A-Za-z0-9_\-]+)"), ConnectionsApi.api_connections_update),
    ("DELETE", _pat(r"/api/connections/((?!(?:reload|reveal|bindkey)$)[A-Za-z0-9_\-]+)"), ConnectionsApi.api_connections_delete),
    ("POST", _lit("/api/qqofficial/qr/create"), ConnectionsApi.api_qr_create),
    ("POST", _lit("/api/qqofficial/qr/poll"), ConnectionsApi.api_qr_poll),
    # ---- 功能模块 ----
    ("GET", _lit("/api/rules"), ModulesApi.api_rules_get),
    ("POST", _lit("/api/rules"), ModulesApi.api_rules_post),
    ("POST", _lit("/api/rules/image"), ModulesApi.api_rules_image),
    ("GET", _lit("/api/whitelist"), ModulesApi.api_whitelist_get),
    ("GET", _lit("/api/whitelist/domains"), ModulesApi.api_whitelist_domains),
    ("DELETE", _pat(r"/api/whitelist/([0-9A-Za-z_-]{1,64})"), ModulesApi.api_whitelist_delete),
    ("GET", _lit("/api/custom_pages"), ModulesApi.api_custom_pages),
    ("GET", _lit("/api/plugins/custom-pages"), ModulesApi.api_custom_pages),
    ("GET", _lit("/api/chat_filter"), ModulesApi.api_chat_filter_get),
    ("PUT", _lit("/api/chat_filter"), ModulesApi.api_chat_filter_put),
    ("POST", _lit("/api/chat_filter/import"), ModulesApi.api_chat_filter_import),
    ("POST", _lit("/api/chat_filter/import_text"), ModulesApi.api_chat_filter_import_text),
    # ---- 市场（字面量先于参数化）----
    ("GET", _lit("/api/market/config"), MarketApi.api_market_config),
    ("GET", _lit("/api/market/plugins"), MarketApi.api_market_plugins),
    ("GET", _lit("/api/market/installed"), MarketApi.api_market_installed),
    ("GET", _lit("/api/market/cover"), MarketApi.api_market_cover),
    ("POST", _lit("/api/market/report"), MarketApi.api_market_report),
    ("POST", _lit("/api/market/like"), MarketApi.api_market_like),
    ("POST", _lit("/api/market/check"), MarketApi.api_market_check),
    ("POST", _lit("/api/market/update-all"), MarketApi.api_market_update_all),
    ("POST", _lit("/api/market/install"), MarketApi.api_market_install),
    ("GET", _pat(r"/api/market/plugin/([a-z0-9][a-z0-9_-]{1,63})"), MarketApi.api_market_plugin_detail),
    ("GET", _pat(r"/api/market/task/([\w-]+)"), MarketApi.api_market_task),
    ("POST", _pat(r"/api/subplugins/([A-Za-z0-9_\-]+)/market-update"), MarketApi.api_subplugin_market_update),
    ("POST", _pat(r"/api/subplugins/([A-Za-z0-9_\-]+)/update-deps"), MarketApi.api_subplugin_update_deps),
    ("POST", _pat(r"/api/subplugins/([A-Za-z0-9_\-]+)/install-requirements"), MarketApi.api_subplugin_install_requirements),
    # ---- 全服热重载与框架更新 ----
    ("POST", _lit("/api/server/reload"), MarketApi.api_server_reload),
    ("POST", _lit("/api/updates/stage"), MarketApi.api_updates_stage),
    ("POST", _lit("/api/updates/apply"), MarketApi.api_updates_apply),
    ("POST", _lit("/api/updates/reload"), MarketApi.api_updates_reload),
    ("GET", _lit("/api/updates/check"), MarketApi.api_updates_check),
    # ---- 子插件 ----
    ("GET", _lit("/api/subplugins"), SubpluginsApi.api_subplugins_list),
    ("POST", _lit("/api/subplugins/reload"), SubpluginsApi.api_subplugins_reload_all),
    ("POST", _lit("/api/subplugins/install/upload"), SubpluginsApi.api_subplugin_install_upload),
    ("POST", _lit("/api/subplugins/install/url"), SubpluginsApi.api_subplugin_install_url),
    ("POST", _pat(r"/api/subplugins/([A-Za-z0-9_\-]+)/toggle"), SubpluginsApi.api_subplugin_toggle),
    ("POST", _pat(r"/api/subplugins/([A-Za-z0-9_\-]+)/reload"), SubpluginsApi.api_subplugin_reload),
    ("POST", _pat(r"/api/subplugins/([A-Za-z0-9_\-]+)/install-deps"), SubpluginsApi.api_subplugin_install_deps),
    ("GET", _pat(r"/api/subplugins/([A-Za-z0-9_\-]+)/deps-sources"), SubpluginsApi.api_subplugin_deps_sources),
    ("POST", _pat(r"/api/subplugins/([A-Za-z0-9_\-]+)/endstone-dep/upload"), SubpluginsApi.api_endstone_dep_upload),
    ("POST", _pat(r"/api/subplugins/([A-Za-z0-9_\-]+)/endstone-dep/url"), SubpluginsApi.api_endstone_dep_url),
    ("GET", _pat(r"/api/subplugins/([A-Za-z0-9_\-]+)/uninstall-preview"), SubpluginsApi.api_subplugin_uninstall_preview),
    ("DELETE", _pat(r"/api/subplugins/([A-Za-z0-9_\-]+)"), SubpluginsApi.api_subplugin_uninstall),
    ("GET", _pat(r"/api/subplugins/([A-Za-z0-9_\-]+)/files"), SubpluginsApi.api_subplugin_files),
    ("GET", _pat(r"/api/subplugins/([A-Za-z0-9_\-]+)/file"), SubpluginsApi.api_subplugin_file_get),
    ("POST", _pat(r"/api/subplugins/([A-Za-z0-9_\-]+)/file"), SubpluginsApi.api_subplugin_file_post),
    # ---- pip ----
    ("POST", _lit("/api/pip/install"), PipApi.api_pip_install),
    ("POST", _lit("/api/pip/uninstall"), PipApi.api_pip_uninstall),
    ("GET", _lit("/api/pip/list"), PipApi.api_pip_list),
    ("GET", _lit("/api/pip/config"), PipApi.api_pip_config),
    ("GET", _pat(r"/api/pip/task/([\w-]+)"), PipApi.api_pip_task),
]


class ApiRoutes(
    AuthApi, PublicApi, SystemApi, ConfigApi, ConnectionsApi,
    ModulesApi, MarketApi, PipApi, SubpluginsApi,
):
    """全部 API mixin 的组合基类：server._RequestHandler 继承本类获得全部路由方法。

    路由处理函数按 ``fn(handler, match, query)`` 调用，handler 即本组合的实例，
    各 mixin 内部的 ``self._helper()`` 互助调用因此可用。
    """


def dispatch_api(handler: RouteMixin, method: str, path: str, query: dict[str, str]) -> None:
    """API 总分派：公共路由 → 子插件自定义 API → 鉴权 → 受保护路由。"""
    for route_method, pattern, fn in PUBLIC_ROUTES:
        if route_method != method:
            continue
        m = pattern.fullmatch(path)
        if m:
            fn(handler, m, query)
            return

    webui = handler.webui
    with webui._ext_lock:
        hit = webui.custom_apis.get((method, path))
    if hit:
        handler.call_custom_api(hit, query, path)
        return

    if not handler._check_auth(query):
        handler._unauthorized()
        return

    for route_method, pattern, fn in AUTHED_ROUTES:
        if route_method != method:
            continue
        m = pattern.fullmatch(path)
        if m:
            fn(handler, m, query)
            return

    handler._send_json({"code": 404, "msg": _t("webui.msg.api_not_found")}, 404)
