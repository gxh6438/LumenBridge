"""LumenBridge 插件市场与更新客户端。

不信任网络下载：所有市场插件必须先核对发布记录中的 SHA-256，再交给
SubPluginManager 已有的 ZIP 路径穿越防护流程。

除 pip 依赖外，这里还负责子插件声明的插件级依赖（``requires``）：
Endstone 插件缺失时收集进结果提示用户安装；子插件缺失且市场在售时，
像 pip 依赖那样自动下载安装（递归处理依赖自身的依赖）。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import tempfile
import zipfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .subplugin.requires import (
    PluginRequirement,
    RequiresDeclaration,
    check_endstone_requirements,
    parse_requires_from_manifest,
    version_cmp,
)

if TYPE_CHECKING:
    from .plugin import LumenBridgePlugin

# requires 递归解析的最大层级：超过视为循环依赖（A→B→A）或恶意嵌套
_MAX_REQUIREMENT_DEPTH = 8

_MARKET_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,63}$")
_VERSION_RE = re.compile(r"^[0-9A-Za-z.+-]{3,80}$")
# 市场站点的 API 版本前缀；配置里只填站点根地址时会自动补全
_API_PREFIX_RE = re.compile(r"/api/v\d+$", re.IGNORECASE)
_DEFAULT_API_PREFIX = "/api/v1"
# 匿名写操作（点赞/举报）的市场会话：PHP 端按会话 Cookie 校验 CSRF
_SESSION_COOKIE_NAME = "LBMARKETSESSID"
_CSRF_ATTR_RE = re.compile(r'data-csrf="([0-9a-fA-F]{16,128})"')


def _normalize_distribution_name(value: str) -> str:
    """按 Python 包名比较规则规范化发行区名。"""
    return re.sub(r"[-_.]+", "_", str(value).strip().lower())


def _version_tuple(value: str) -> tuple[int, ...]:
    parts: list[int] = []
    for segment in str(value or "0").lstrip("vV").split("."):
        match = re.match(r"(\d+)", segment)
        parts.append(int(match.group(1)) if match else 0)
    return tuple(parts or [0])


def _is_newer(remote: str, local: str) -> bool:
    # 补 0 对齐比较（1.2 与 1.2.0 相等），段数不同不再误报“有更新”
    return version_cmp(remote, local) > 0


class MarketplaceError(RuntimeError):
    """市场端点、下载或完整性校验失败。

    ``code`` 保存服务端错误码（如 csrf_failed），供调用方做针对性重试。
    """

    def __init__(self, message: str, *, code: str = "") -> None:
        super().__init__(message)
        self.code = code


class _RouteNotFoundError(MarketplaceError):
    """HTTP 404 且无业务错误信息：触发 pretty→query 路由形式回退（内部信号）。"""

    def __init__(self) -> None:
        super().__init__("市场服务返回 HTTP 404")


class MarketplaceClient:
    """市场浏览、安装、版本检查和依赖更新的服务层。"""

    def __init__(self, plugin: "LumenBridgePlugin") -> None:
        self.plugin = plugin
        self.logger = getattr(plugin, "_tee_logger", None) or plugin.logger
        self._check_lock = threading.RLock()
        # 防止两个 WebUI 框架更新任务并发执行，导致 wheel 互相覆盖、备份目录错乱或暂存文件残留
        self._framework_update_lock = threading.Lock()
        self._last_checked: dict[str, dict[str, Any]] = {}
        # 市场站点的匿名访客身份（LBMARKETVISITOR Cookie）：点赞状态与防刷计数
        # 都绑定该身份。Python 客户端不自动管理 Cookie，若每次请求换一个身份，
        # 点赞会变成"永远新增"且无法取消；因此持久化到插件数据目录。
        self._visitor_token: str = ""
        self._visitor_lock = threading.Lock()
        # 匿名写会话（LBMARKETSESSID Cookie + CSRF token）：点赞/举报无需
        # 任何密钥，先访问市场页面建立匿名会话，再携带会话与 CSRF 头提交。
        # PHP 会话有服务端过期时间，失效时自动刷新重试。
        self._market_session: dict[str, str] = {}
        self._market_session_lock = threading.Lock()
        # 市站路由形式探测："pretty" = /api/v1/... 直接路径（需主机支持
        # rewrite）；"query" = index.php?lb_route=/api/v1/...（任何 PHP 主机
        # 都可用）。"" = 尚未探测。探测结果绑定站点根地址，换站自动重置。
        self._route_style: str = ""
        self._route_style_site: str = ""

    @property
    def config(self) -> dict[str, Any]:
        source = self.plugin.config_manager.data.get("marketplace", {})
        return source if isinstance(source, dict) else {}

    @property
    def enabled(self) -> bool:
        return bool(self.config.get("enable", False)) and bool(self.api_url)

    @property
    def api_url(self) -> str:
        return str(self.config.get("api_url") or "").rstrip("/")

    @property
    def timeout(self) -> int:
        try:
            return max(5, min(120, int(self.config.get("timeout", 30))))
        except (TypeError, ValueError):
            return 30

    @property
    def max_download_bytes(self) -> int:
        try:
            return max(1024 * 1024, min(128 * 1024 * 1024, int(self.config.get("max_download_bytes", 64 * 1024 * 1024))))
        except (TypeError, ValueError):
            return 64 * 1024 * 1024

    def _open(self, request: urllib.request.Request):
        """统一 URL 打开入口（尊重环境代理，兼容内网自建源与代理环境）。"""
        return urllib.request.urlopen(request, timeout=self.timeout)

    def _validate_endpoint(self, url: str) -> str:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in {"https", "http"} or not parsed.netloc:
            raise MarketplaceError("市场 API 必须是有效的 http(s) 地址")
        return url

    def _api_url(self, path: str) -> str:
        if not self.enabled:
            raise MarketplaceError("插件市场未配置或未启用")
        return self._validate_endpoint(self._api_base() + "/" + path.lstrip("/"))

    def _api_base(self, base: str = "") -> str:
        """返回带 /api/v1 前缀的 API 根地址。

        配置里只需填站点根地址（如 https://market.mxcraft.vip），
        后台调用时自动补全版本前缀；显式填了 /api/v2 之类的地址也兼容。
        """
        url = (base or self.api_url).rstrip("/")
        if not _API_PREFIX_RE.search(url):
            url += _DEFAULT_API_PREFIX
        return url

    def _site_root(self, base: str = "") -> str:
        """返回市场站点根地址（去掉 /api/v1 后缀），用于拼接封面等静态资源。"""
        url = (base or self.api_url).rstrip("/")
        return _API_PREFIX_RE.sub("", url).rstrip("/")

    def _resolve_cover_url(self, cover_url: Any, base: str = "") -> str:
        """把市场返回的相对封面路径解析为绝对 URL。

        兼容两代服务端格式：
        - 旧版：media/images/xx.png（裸相对路径，需按路由风格补前缀）
        - v3 修复版：/index.php?lb_route=%2Fmedia%2Fimages%2Fxx.png
          （已自带 query 入口前缀，任何主机都能直接输出，不能再包装）
        """
        value = str(cover_url or "").strip()
        if not value:
            return ""
        parsed = urllib.parse.urlparse(value)
        if parsed.scheme in {"http", "https"} and parsed.netloc:
            return value
        if not value.startswith("/"):
            value = "/" + value
        root = self._site_root(base)
        # 服务端返回的地址已自带 index.php?lb_route= 前缀（v3 修复版）：
        # 直接挂站点根即可；再次包装会双重编码导致封面 404
        if "index.php?lb_route=" in value.lower():
            return root + value
        if "index.php?lb_route=" in root.lower():
            # 用户显式配置 query 形式地址：root 已带 ?lb_route= 尾巴
            return root + value
        # 探测到站点需要 query 形式路由（不支持 rewrite）：媒体文件同样要走
        # index.php 入口，否则封面 404
        if base == "" and self._route_style == "query" and self._route_style_site == root:
            return root + "/index.php?lb_route=" + urllib.parse.quote(value, safe="/")
        return root + value

    def fetch_cover(self, url: str) -> tuple[str, bytes]:
        """服务端代取市场封面图（附访客 Cookie），供 WebUI 图片代理使用。

        浏览器直连市场站点加载封面会因混合内容（WebUI 走 https 而市场为
        http）/防盗链校验（需 LBMARKETVISITOR Cookie）等场景失败；改为
        WebUI 后端代理获取。仅允许访问配置的市场站点主机，防止该接口
        被当成开放代理（SSRF）。
        """
        url = str(url or "").strip()
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise MarketplaceError("封面地址无效")
        site = urllib.parse.urlparse(self._site_root())
        if not site.netloc or parsed.netloc.lower() != site.netloc.lower():
            raise MarketplaceError("封面地址不在配置的市场站点内")
        headers = {"User-Agent": "LumenBridge-Market/1", "Accept": "image/*"}
        try:
            headers["Cookie"] = f"LBMARKETVISITOR={self._get_visitor_token()}"
        except Exception:  # noqa: BLE001 - data_folder 不可用时退化为无 Cookie
            pass
        request = urllib.request.Request(url, headers=headers)
        limit = 8 * 1024 * 1024
        try:
            # SSRF 防护不依赖对内网地址的额外校验：同主机白名单 + 重定向
            # 主机固定 + 大小/类型限制已足够约束该代理端点。
            with self._open(request) as response:
                # H2：重定向不得离开配置的市场站点
                final_host = (urllib.parse.urlsplit(response.geturl()).hostname or "").lower()
                if final_host != (site.hostname or "").lower():
                    raise RuntimeError("封面重定向到其它主机")
                if response.status < 200 or response.status >= 300:
                    raise RuntimeError(f"市场服务返回 HTTP {response.status}")
                ctype = str(response.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                raw = response.read(limit + 1)
        except Exception as exc:  # noqa: BLE001
            # H2：对外统一“下载失败”，不回显连接错误细节；完整异常仅记日志
            self.logger.warning(f"[Market] 封面下载失败 url={url} error={exc!r}")
            raise MarketplaceError("下载失败") from exc
        if len(raw) > limit:
            raise MarketplaceError("封面图片超过大小限制")
        # M1：SVG 可内嵌脚本构成 XSS，白名单收敛为位图格式
        if ctype not in {"image/png", "image/jpeg", "image/gif", "image/webp"}:
            raise MarketplaceError("封面内容不是允许的图片类型")
        return ctype, raw

    def _get_visitor_token(self) -> str:
        """加载或生成市场匿名访客身份（与 PHP 端 LBMARKETVISITOR Cookie 对齐）。

        PHP 端校验规则为 ^[A-Za-z0-9_-]{32,128}$；token_urlsafe(32) 恰好是
        43 个 base64url 字符。持久化在插件数据目录；进程内加锁 + O_EXCL
        原子创建 + 空文件重试，保证并发首调不会分裂出多个身份。
        """
        if self._visitor_token:
            return self._visitor_token
        with self._visitor_lock:
            if self._visitor_token:
                return self._visitor_token
            token_file = Path(self.plugin.data_folder) / "market_visitor.txt"
            try:
                token = token_file.read_text(encoding="utf-8").strip()
            except OSError:
                token = ""
            if not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", token):
                token = secrets.token_urlsafe(32)
                try:
                    token_file.parent.mkdir(parents=True, exist_ok=True)
                    # O_CREAT|O_EXCL：多进程并发时保留先创建者的 token
                    fd = os.open(token_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(fd, "w", encoding="utf-8") as handle:
                        handle.write(token)
                except FileExistsError:
                    # 文件已由他人创建：等待写入完成后回读。回读值必须通过同一
                    # 格式校验——空（尚未写完）或非法（损坏数据）都不能用作身份；
                    # 非法内容时用本进程的 token 截断重写
                    existing = self._read_token_with_retry(token_file)
                    if re.fullmatch(r"[A-Za-z0-9_-]{32,128}", existing):
                        token = existing
                    else:
                        try:
                            with open(token_file, "w", encoding="utf-8") as handle:
                                handle.write(token)
                        except OSError:
                            pass
                except OSError:
                    pass  # 持久化失败时退化为内存身份（本次运行内仍一致）
            self._visitor_token = token
            return token

    @staticmethod
    def _read_token_with_retry(token_file: Path, *, attempts: int = 10, delay: float = 0.05) -> str:
        for _ in range(attempts):
            try:
                value = token_file.read_text(encoding="utf-8").strip()
            except OSError:
                value = ""
            if value:
                return value
            time.sleep(delay)
        return ""

    def _api_prefix(self) -> str:
        """返回配置地址中的 API 版本前缀（默认 /api/v1）。"""
        match = _API_PREFIX_RE.search(self.api_url.rstrip("/"))
        return match.group(0) if match else _DEFAULT_API_PREFIX

    @staticmethod
    def _with_query(url: str, query: dict[str, str] | None) -> str:
        """把查询参数合并进 URL（保留已有 query，如 index.php?lb_route=...）。"""
        if not query:
            return url
        parts = urllib.parse.urlsplit(url)
        extra = urllib.parse.urlencode(query)
        merged = "&".join(x for x in (parts.query, extra) if x)
        return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, merged, parts.fragment))

    def _query_route_url(self, path: str) -> str:
        """构造 index.php?lb_route=/api/v1/... 形式的兼容 URL。

        LumenMarket 生成的所有链接都是这种形式：许多托管 nginx 不解析
        .htaccess，pretty 路径（/api/v1/...）会 404，必须走入口查询参数。
        """
        route = self._api_prefix() + "/" + path.lstrip("/")
        return self._site_root() + "/index.php?lb_route=" + urllib.parse.quote(route, safe="/")

    def _remember_route_style(self, style: str) -> None:
        self._route_style = style
        self._route_style_site = self._site_root()

    def _build_request_urls(self, path: str, query: dict[str, str] | None) -> list[str]:
        """返回请求候选 URL（首选在前，404 时依次回退）。"""
        if not self.enabled:
            raise MarketplaceError("插件市场未配置或未启用")
        base = self._api_base()
        root = self._site_root()
        explicit_query = "index.php?lb_route=" in base.lower()
        style = self._route_style if self._route_style_site == root else ""
        if explicit_query:
            # 用户显式填写 query 形式地址：直接续路径，无需回退
            return [self._validate_endpoint(self._with_query(base + "/" + path.lstrip("/"), query))]
        if style == "query":
            return [self._validate_endpoint(self._with_query(self._query_route_url(path), query))]
        pretty = self._validate_endpoint(self._with_query(base + "/" + path.lstrip("/"), query))
        if style == "pretty":
            return [pretty]
        # 未探测：pretty 优先，query 兜底（对应不支持 rewrite 的主机）
        return [pretty, self._validate_endpoint(self._with_query(self._query_route_url(path), query))]

    def _request_json(self, path: str, *, query: dict[str, str] | None = None, method: str = "GET", body: dict[str, Any] | None = None, headers: dict[str, str] | None = None) -> dict[str, Any]:
        candidates = self._build_request_urls(path, query)
        for index, url in enumerate(candidates):
            try:
                result = self._request_json_at(url, method=method, body=body, headers=headers)
            except _RouteNotFoundError:
                if index + 1 >= len(candidates):
                    raise MarketplaceError("市场服务返回 HTTP 404") from None
                continue
            # 成功：记录实际可用的路由形式（query 形式用于后续请求与封面拼接）
            self._remember_route_style("query" if "index.php?lb_route=" in url.lower() else "pretty")
            return result
        raise MarketplaceError("市场服务返回 HTTP 404")

    def _request_json_at(self, url: str, *, method: str = "GET", body: dict[str, Any] | None = None, headers: dict[str, str] | None = None) -> dict[str, Any]:
        request_headers = {"Accept": "application/json", "User-Agent": "LumenBridge-Market/1"}
        # 稳定的访客身份：让服务端返回的 liked 状态与点赞/取消行为绑定同一身份
        try:
            request_headers["Cookie"] = f"LBMARKETVISITOR={self._get_visitor_token()}"
        except Exception:  # noqa: BLE001 - data_folder 不可用时退化为无 Cookie
            pass
        if headers:
            request_headers.update(headers)
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, method=method, headers=request_headers)
        try:
            with self._open(request) as response:
                # 接受所有 2xx：浏览/详情返回 200，举报接口返回 201 Created
                if response.status < 200 or response.status >= 300:
                    raise MarketplaceError(f"市场服务返回 HTTP {response.status}")
                raw = response.read(2 * 1024 * 1024 + 1)
        except urllib.error.HTTPError as exc:
            message = ""
            error_code = ""
            try:
                err_raw = exc.read(2048)
                err_payload = json.loads(err_raw.decode("utf-8", errors="replace"))
                if isinstance(err_payload, dict) and isinstance(err_payload.get("error"), dict):
                    message = str(err_payload["error"].get("message") or "")
                    error_code = str(err_payload["error"].get("code") or "")
            except (ValueError, AttributeError):
                pass
            # 404 且服务端未返回业务错误信息：可能是主机不支持 rewrite 导致
            # pretty 路径不存在，用内部信号通知上层尝试 query 形式回退
            if exc.code == 404 and not message:
                raise _RouteNotFoundError() from exc
            raise MarketplaceError(message or f"市场服务返回 HTTP {exc.code}", code=error_code) from exc
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise MarketplaceError(f"无法连接插件市场：{exc}") from exc
        if len(raw) > 2 * 1024 * 1024:
            raise MarketplaceError("市场响应超过大小限制")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise MarketplaceError("市场返回了无效 JSON") from exc
        if not isinstance(payload, dict) or payload.get("ok") is not True or not isinstance(payload.get("data"), dict):
            message = ""
            error_code = ""
            if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
                message = str(payload["error"].get("message") or "")
                error_code = str(payload["error"].get("code") or "")
            raise MarketplaceError(message or "市场请求失败", code=error_code)
        return payload["data"]

    def _market_page_url(self) -> str:
        """市场社区页地址：任何请求都会建立会话并渲染 data-csrf 属性。"""
        root = self._site_root()
        if "index.php?lb_route=" in root.lower():
            # 用户显式配置 query 形式地址时根地址即入口
            return root
        return root + "/"

    def _ensure_market_session(self, *, force: bool = False) -> dict[str, str]:
        """获取匿名市场会话（LBMARKETSESSID Cookie + CSRF token）。

        PHP 市场对点赞/举报写操作按会话校验 CSRF；匿名客户端先 GET 一次
        社区页，从 Set-Cookie 取会话 ID、从 <body data-csrf="..."> 取令牌，
        即可携带两者通过校验——全程无需任何密钥或登录身份。
        """
        with self._market_session_lock:
            session = self._market_session
            if not force and session.get("cookie") and session.get("csrf"):
                return dict(session)
            url = self._validate_endpoint(self._market_page_url())
            headers = {"User-Agent": "LumenBridge-Market/1", "Accept": "text/html,*/*"}
            try:
                headers["Cookie"] = f"LBMARKETVISITOR={self._get_visitor_token()}"
            except Exception:  # noqa: BLE001 - data_folder 不可用时退化为无 Cookie
                pass
            request = urllib.request.Request(url, headers=headers)
            try:
                with self._open(request) as response:
                    raw = response.read(512 * 1024 + 1)
                    set_cookies = response.headers.get_all("Set-Cookie") or []
            except urllib.error.HTTPError as exc:
                raise MarketplaceError(f"无法访问市场页面（HTTP {exc.code}）") from exc
            except (urllib.error.URLError, OSError, TimeoutError) as exc:
                raise MarketplaceError(f"无法访问市场页面：{exc}") from exc
            if len(raw) > 512 * 1024:
                raise MarketplaceError("市场页面超过大小限制")
            cookie_value = ""
            for item in set_cookies:
                match = re.match(rf"\s*{re.escape(_SESSION_COOKIE_NAME)}=([^;\s]+)", str(item))
                if match:
                    cookie_value = match.group(1)
                    break
            token_match = _CSRF_ATTR_RE.search(raw.decode("utf-8", "replace"))
            csrf = token_match.group(1) if token_match else ""
            if not cookie_value or not csrf:
                raise MarketplaceError("无法建立市场匿名会话（缺少会话 Cookie 或 CSRF 令牌）")
            fresh = {"cookie": cookie_value, "csrf": csrf}
            self._market_session = fresh
            return dict(fresh)

    def _anon_market_write(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        """以匿名会话执行市场写操作（点赞/举报）。

        会话在服务端过期时返回 csrf_failed，此时强制刷新会话重试一次。
        """
        last_error: MarketplaceError | None = None
        for attempt in range(2):
            session = self._ensure_market_session(force=attempt > 0)
            cookie = f"{_SESSION_COOKIE_NAME}={session['cookie']}"
            try:
                cookie = f"LBMARKETVISITOR={self._get_visitor_token()}; {cookie}"
            except Exception:  # noqa: BLE001
                pass
            headers = {"X-CSRF-Token": session["csrf"], "Cookie": cookie}
            try:
                return self._request_json(path, method="POST", body=body, headers=headers)
            except MarketplaceError as exc:
                last_error = exc
                if exc.code == "csrf_failed":
                    continue
                raise
        assert last_error is not None
        raise last_error

    def browse(self, *, search: str = "", category: str = "", sort: str = "score", page: int = 1, limit: int = 24) -> dict[str, Any]:
        safe_sort = sort if sort in {"time", "likes", "downloads", "score"} else "score"
        query = {"page": str(max(1, page)), "limit": str(max(1, min(100, limit))), "sort": safe_sort}
        if search.strip():
            query["q"] = search.strip()[:100]
        if category.strip():
            query["category"] = category.strip()[:32]
        data = self._request_json("market/plugins", query=query)
        items = data.get("items")
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict) and item.get("cover_url"):
                    item["cover_url"] = self._resolve_cover_url(item.get("cover_url"))
        return data

    def plugin_detail(self, market_id: str) -> dict[str, Any]:
        if not _MARKET_ID_RE.fullmatch(market_id):
            raise MarketplaceError("市场插件 ID 非法")
        data = self._request_json(f"market/plugins/{market_id}")
        if isinstance(data, dict) and data.get("cover_url"):
            data["cover_url"] = self._resolve_cover_url(data.get("cover_url"))
        return data

    def report_plugin(self, market_id: str, reason: str, contact: str = "") -> dict[str, Any]:
        """完全匿名举报：只需匿名会话 + CSRF，服务端以随机访客 Cookie 标识。"""
        if not _MARKET_ID_RE.fullmatch(market_id):
            raise MarketplaceError("市场插件 ID 非法")
        reason = str(reason).strip()[:4000]
        contact = str(contact).strip()[:254]
        if not reason:
            raise MarketplaceError("请填写举报内容")
        return self._anon_market_write(
            f"market/plugins/{market_id}/report", body={"reason": reason, "contact": contact}
        )

    def like_plugin(self, market_id: str, liked: bool) -> dict[str, Any]:
        """点赞/取消点赞市场插件（完全匿名，无需任何密钥）。

        访客身份由持久化的 LBMARKETVISITOR 随机 Cookie 承担，服务端只看到
        该 Cookie 的 HMAC——不涉及 QQ/管理员身份，也无法反推。
        """
        if not _MARKET_ID_RE.fullmatch(market_id):
            raise MarketplaceError("市场插件 ID 非法")
        return self._anon_market_write(
            f"market/plugins/{market_id}/like",
            body={"liked": bool(liked)},
        )

    @staticmethod
    def _select_release(detail: dict[str, Any], requested_version: str = "") -> dict[str, Any]:
        versions = detail.get("versions", [])
        if not isinstance(versions, list):
            raise MarketplaceError("市场版本信息格式无效")
        release = None
        for item in versions:
            if not isinstance(item, dict):
                continue
            if requested_version and str(item.get("version")) != requested_version:
                continue
            if isinstance(item.get("download_url"), str) and isinstance(item.get("sha256"), str):
                release = item
                break
        if release is None:
            raise MarketplaceError("未找到可安装的插件版本")
        if not _VERSION_RE.fullmatch(str(release.get("version") or "")):
            raise MarketplaceError("市场返回的版本号非法")
        digest = str(release.get("sha256") or "").lower()
        if not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise MarketplaceError("市场未提供有效 SHA-256")
        return release

    def _download_verified(self, url: str, sha256: str, *, expected_base: str = "", log=None, progress=None) -> str:
        """下载并校验 SHA-256；expected_base 用于指定主机 pin 的基准地址。

        H20：默认 pin 到配置的市场 api_url（scheme+host 必须一致），使"哈希来源"
        与"下载通道"解耦——市场端被篡改也无法把下载指向任意主机；
        框架更新等其它 API 来源可显式传入自身 base。
        log / progress 可选回调用于前端进度展示。
        """
        log = log or (lambda _msg: None)
        progress = progress or (lambda _pct, _label="": None)
        self._validate_endpoint(url)
        parsed = urllib.parse.urlparse(url)
        base_parsed = urllib.parse.urlparse(self._validate_endpoint(expected_base or self._api_base()))
        url_pair = (parsed.scheme.lower(), (parsed.hostname or "").lower())
        base_pair = (base_parsed.scheme.lower(), (base_parsed.hostname or "").lower())
        if url_pair != base_pair:
            self.logger.warning(f"[Market] 插件下载地址与市场 API 主机不一致，已拒绝：{url}")
            raise MarketplaceError("插件下载地址必须与市场 API 同一主机")
        # 下载文件在未校验 hash 前不会进入插件目录；HTTP 仅允许管理员显式配置开启
        allow_http = bool(self.config.get("allow_http", False))
        if parsed.scheme == "http" and not allow_http:
            raise MarketplaceError("插件下载地址必须使用 HTTPS")
        log(f"开始下载: {url}")
        temporary = tempfile.NamedTemporaryFile(prefix="lumen_market_", suffix=".zip", delete=False)
        path = temporary.name
        digest = hashlib.sha256()
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "LumenBridge-Market/1"})
            with self._open(request) as response:
                # H20：重定向后必须仍满足 allow_http 约束，且不得离开 pinned 主机
                # （修复 HTTPS 下载被 30x 降级为 HTTP 绕过 allow_http=False 的路径）
                final = urllib.parse.urlparse(response.geturl())
                if (final.hostname or "").lower() != (parsed.hostname or "").lower():
                    self.logger.warning(f"[Market] 插件下载重定向到其它主机，已拒绝：{response.geturl()}")
                    raise MarketplaceError("插件下载重定向地址非法")
                if final.scheme == "http" and not allow_http:
                    raise MarketplaceError("插件下载地址必须使用 HTTPS")
                # 接受 2xx，部分 CDN/代理会对下载返回 206 Partial Content
                if response.status < 200 or response.status >= 300:
                    raise MarketplaceError(f"插件下载返回 HTTP {response.status}")
                content_type = str(response.headers.get("Content-Type") or "").lower()
                if "zip" not in content_type and "octet-stream" not in content_type:
                    raise MarketplaceError("插件下载响应不是 ZIP 文件")
                content_length = response.headers.get("Content-Length")
                expected_size = int(content_length) if content_length and content_length.isdigit() else 0
                total = 0
                while True:
                    chunk = response.read(64 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > self.max_download_bytes:
                        raise MarketplaceError("插件下载超过配置大小限制")
                    temporary.write(chunk)
                    digest.update(chunk)
                    if expected_size > 0:
                        pct = min(90, int(total / expected_size * 80) + 10)
                        progress(pct, f"下载中 {total // 1024}KB / {expected_size // 1024}KB")
                    elif total % (512 * 1024) < 64 * 1024:
                        progress(50, f"下载中 {total // 1024}KB")
            temporary.close()
            actual = digest.hexdigest()
            # 用常量时间比较避免时序侧信道
            if not hmac.compare_digest(actual, sha256.lower()):
                raise MarketplaceError("插件 SHA-256 校验失败，已拒绝安装")
            log(f"下载完成，SHA-256 校验通过 ({total // 1024}KB)")
            progress(90, "校验完成")
            return path
        except Exception:
            temporary.close()
            try:
                os.unlink(path)
            except OSError:
                pass
            raise

    def _run_on_main_wait(self, fn, timeout: int = 45):
        done = threading.Event()
        result: dict[str, Any] = {}
        cancelled = threading.Event()

        def wrapped() -> None:
            # 超时后调用方已报错返回；排队中的任务不再执行，避免不可取消的副作用
            if cancelled.is_set():
                done.set()
                return
            try:
                result["value"] = fn()
            except Exception as exc:  # noqa: BLE001
                result["error"] = exc
            finally:
                done.set()

        # 防自死锁：主线程上调度 + 阻塞等待永远等不到任务，直接同步执行。
        # getattr 兼容测试桩等未实现该方法的插件替身对象。
        _on_main = getattr(self.plugin, "is_on_main_thread", None)
        if callable(_on_main) and _on_main():
            wrapped()
        else:
            self.plugin.run_on_main(wrapped)
        if not done.wait(timeout=timeout):
            cancelled.set()
            raise MarketplaceError("服务器主线程处理插件安装超时")
        if "error" in result:
            raise MarketplaceError(f"插件安装失败：{result['error']}")
        return result.get("value")

    def _stamp_origin(self, plugin_name: str, market_id: str, release: dict[str, Any]) -> None:
        manager = self.plugin.subplugin_manager
        if manager is None:
            raise MarketplaceError("子插件管理器不可用")
        with manager._lock:
            subplugin = manager.subplugins.get(plugin_name)
            folder = subplugin.folder if subplugin else manager.plugins_dir / plugin_name
            manifest_path = folder / "lumen.json"
            if not manifest_path.is_file():
                raise MarketplaceError("插件已安装但找不到 lumen.json")
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise MarketplaceError(f"无法写入市场来源记录：{exc}") from exc
            # 合法 JSON 但非对象（null/123/[]）：无法写入 _market 字段
            if not isinstance(manifest, dict):
                raise MarketplaceError("lumen.json 内容不是 JSON 对象，无法写入市场来源记录")
            manifest["_market"] = {
                "id": market_id,
                "source": "marketplace",
                "installed_version": str(release["version"]),
                "sha256": str(release["sha256"]).lower(),
                "download_url": str(release["download_url"]),
                "last_checked_at": int(time.time()),
            }
            # tmp + 原子替换，防进程中断留下半个 lumen.json
            tmp_manifest = manifest_path.with_name(manifest_path.name + ".tmp")
            tmp_manifest.write_text(json.dumps(manifest, ensure_ascii=False, indent=4), encoding="utf-8")
            tmp_manifest.replace(manifest_path)
            if subplugin:
                subplugin.manifest = manifest

    def _install_declared_dependencies(self, plugin_name: str, dependencies: list[str], *, upgrade: bool = False, log=None, progress=None) -> tuple[bool, str]:
        log = log or (lambda _msg: None)
        progress = progress or (lambda _pct, _label="": None)
        if not dependencies:
            return True, ""
        manager = self.plugin.subplugin_manager
        pip_manager = self.plugin._get_pip_manager()
        if manager is None or pip_manager is None:
            return False, "pip 管理器不可用"
        # 依赖安装纳入任务日志/进度条：下载完插件本体后，前端能看到
        # "正在安装依赖 → pip 逐行输出 → 热重载" 的完整过程
        log(f"检测到声明的依赖: {' '.join(dependencies)}")
        progress(96, "正在安装依赖")
        # 必须持 plugin._pip_serial_lock：旧版本直接调用 pip_manager.install 绕过了 WebUI 的
        # _pip_serial_lock，可与 WebUI 安装任务并发执行而损坏 site-packages 元数据。
        with self.plugin._pip_serial_lock:
            ok, message = pip_manager.install(dependencies, on_log=log, upgrade=upgrade)
        if not ok:
            return False, message
        progress(98, "正在热重载子插件")
        loaded = bool(self._run_on_main_wait(lambda: manager.reload_one(plugin_name), timeout=20))
        if not loaded:
            return False, "依赖已安装，但子插件热重载失败；请在子插件页面查看错误详情"
        log("依赖安装完成，子插件已重载")
        return True, message

    # ------------------------------------------------------------------
    # 插件级强制依赖（requires）：子插件依赖自动安装 / Endstone 依赖提示
    # ------------------------------------------------------------------

    def _read_installed_requires(self, plugin_name: str) -> RequiresDeclaration:
        """读取已安装子插件的 requires 声明（优先运行记录，退回磁盘清单）。"""
        manager = self.plugin.subplugin_manager
        if manager is None:
            return RequiresDeclaration()
        with manager._lock:
            sp = manager.subplugins.get(plugin_name)
            if sp is not None:
                return parse_requires_from_manifest(sp.manifest)
        try:
            from .subplugin.loader import _read_manifest_dict

            manifest = _read_manifest_dict(manager.plugins_dir / plugin_name / "lumen.json")
        except OSError:
            manifest = None
        return parse_requires_from_manifest(manifest)

    def _find_market_plugin_by_manifest_name(self, name: str) -> dict[str, Any] | None:
        """按子插件名（lumen.json 的 name）在市场精确匹配插件。

        服务端支持 ``manifest_name`` 精确过滤；旧版服务端会忽略该参数返回
        未过滤列表，因此客户端必须逐项核对 ``manifest_name`` 字段，防止
        把同名关键词的无关插件当成依赖装进来。
        """
        try:
            data = self._request_json("market/plugins", query={"manifest_name": name, "limit": "20"})
        except MarketplaceError as exc:
            self.logger.warning(f"[Market] 按子插件名搜索市场失败: {name} ({exc})")
            return None
        items = data.get("items") if isinstance(data, dict) else None
        if not isinstance(items, list):
            return None
        for item in items:
            if not isinstance(item, dict) or str(item.get("manifest_name") or "") != name:
                continue
            market_id = str(item.get("id") or "")
            if not _MARKET_ID_RE.fullmatch(market_id):
                continue
            try:
                return self.plugin_detail(market_id)
            except MarketplaceError as exc:
                self.logger.warning(f"[Market] 获取依赖插件详情失败: {market_id} ({exc})")
                return None
        return None

    @staticmethod
    def _select_release_for_requirement(detail: dict[str, Any], req: PluginRequirement) -> dict[str, Any] | None:
        """从详情版本列表中选出满足约束的最高版本（不依赖列表顺序）。"""
        versions = detail.get("versions", [])
        if not isinstance(versions, list):
            return None
        best: dict[str, Any] | None = None
        best_version = ""
        for item in versions:
            if not isinstance(item, dict):
                continue
            if not isinstance(item.get("download_url"), str) or not isinstance(item.get("sha256"), str):
                continue
            version = str(item.get("version") or "")
            if not _VERSION_RE.fullmatch(version):
                continue
            if req.op and not req.satisfied_by(version):
                continue
            if best is None or version_cmp(version, best_version) > 0:
                best, best_version = item, version
        return best

    def _ensure_subplugin_requirement(
        self,
        req: PluginRequirement,
        *,
        plugin_name: str,
        log=None,
        progress=None,
        _dep_depth: int = 0,
        _dep_chain: tuple[str, ...] = (),
    ) -> tuple[bool, str]:
        """确保一条子插件依赖已满足；缺失时从市场自动安装（递归处理其依赖）。

        返回 (是否满足, 说明)：满足且本次未安装时说明为空；本次新装时说明
        为依赖名；无法满足时说明为原因（含提示安装/升级的完整信息）。
        """
        log = log or (lambda _msg: None)
        progress = progress or (lambda _pct, _label="": None)
        if req.name in _dep_chain:
            chain = " → ".join(_dep_chain + (req.name,))
            return False, f"循环依赖: {chain}，请修正相关子插件的 requires 声明"
        manager = self.plugin.subplugin_manager
        if manager is None:
            return False, "子插件管理器不可用"
        with manager._lock:
            dep = manager.subplugins.get(req.name)
            local_version = str(dep.manifest.get("version") or "") if dep is not None else ""
        if dep is not None and dep.loaded and req.satisfied_by(local_version):
            return True, ""
        if dep is not None and not dep.loaded and req.satisfied_by(local_version):
            # 已安装且版本满足，但没加载成功：被清单禁用或自身加载失败，
            # 市场重装同一版本会被拒绝，升级也解决不了——提示用户处理
            return False, (
                f"依赖 {req.display()} 已安装（v{local_version}）但未加载，"
                "可能被禁用或自身加载失败，请到子插件页面查看其错误信息"
            )

        # 未安装 / 版本不满足 → 市场按 manifest_name 精确匹配自动安装（像 pip 依赖那样）
        detail = self._find_market_plugin_by_manifest_name(req.name)
        if detail is None:
            return False, f"缺少子插件依赖 {req.display()}：插件市场中没有找到，请手动安装后再试"
        release = self._select_release_for_requirement(detail, req)
        if release is None:
            return False, f"缺少子插件依赖 {req.display()}：市场中所有版本均不满足该约束"
        version = str(release.get("version") or "")
        if dep is not None and not _is_newer(version, local_version):
            # 走到这里时 req.satisfied_by(local_version) 必为 False（满足的
            # 两种情况——已加载/未加载——在上方都已提前返回），市场能提供的
            # 最高满足版本又不高于本地：约束是上限型（如 <3.0 而本地 3.0）
            return False, (
                f"依赖 {req.display()} 不满足：本地 v{local_version}，"
                f"市场可提供的最高满足版本为 v{version}，无法升级"
            )
        market_id = str(detail.get("id") or "")
        log(f"正在安装子插件依赖 {req.display()}（来自插件市场 {market_id}）")
        try:
            self.install(market_id, version, log=log, progress=progress, _dep_depth=_dep_depth, _dep_chain=_dep_chain)
        except MarketplaceError as exc:
            return False, f"自动安装依赖 {req.display()} 失败：{exc}"
        with manager._lock:
            dep = manager.subplugins.get(req.name)
            if dep is not None and dep.loaded and req.satisfied_by(str(dep.manifest.get("version") or "")):
                return True, req.name
        return False, f"依赖 {req.display()} 已安装但仍未成功加载，请查看子插件页面错误信息"

    def install_plugin_requirements(
        self,
        plugin_name: str,
        *,
        log=None,
        progress=None,
        _dep_depth: int = 1,
        _dep_chain: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        """补齐已安装子插件的插件级依赖（requires），像 pip 依赖那样自动安装。

        - 子插件依赖：本地已满足则跳过；否则到市场按 manifest_name 精确
          匹配并自动安装满足约束的最高版本（递归处理其自身依赖）
        - Endstone 插件依赖：无法代装，缺失时收集进 missing 提示用户安装

        返回 ``{"ok", "message", "installed", "missing", "endstone_missing"}``；
        ``ok=False`` 不代表目标插件安装失败，只代表依赖未能全部满足。
        """
        log = log or (lambda _msg: None)
        progress = progress or (lambda _pct, _label="": None)
        empty = {"ok": True, "message": "", "installed": [], "missing": [], "endstone_missing": []}
        if _dep_depth > _MAX_REQUIREMENT_DEPTH:
            chain = " → ".join(_dep_chain)
            return {
                "ok": False,
                "message": f"依赖层级过深（可能存在循环依赖）：{chain}",
                "installed": [], "missing": [], "endstone_missing": [],
            }
        declaration = self._read_installed_requires(plugin_name)
        if declaration.empty:
            return dict(empty)
        manager = self.plugin.subplugin_manager
        if manager is None:
            return dict(empty)
        if declaration.invalid:
            log(f"注意：{plugin_name} 的 requires 声明存在无法解析的项: {', '.join(declaration.invalid)}")

        installed: list[str] = []
        missing: list[str] = []

        # Endstone 插件依赖：只能提示安装，无法自动处理。
        # server 不可用（None）→ 无法核实，不误报缺失
        endstone_missing: list[str] = []
        if declaration.endstone:
            try:
                installed_endstone = manager._installed_endstone_plugins()
            except Exception:  # noqa: BLE001 - 服务器 API 不可用时按无法核实处理
                installed_endstone = None
            if installed_endstone is None:
                log(f"注意：无法获取服务器插件列表，跳过 {plugin_name} 的 Endstone 插件依赖核实")
            else:
                for req, _actual in check_endstone_requirements(declaration.endstone, installed_endstone):
                    endstone_missing.append(req.display())

        # 子插件依赖：缺什么补什么
        for req in declaration.subplugins:
            if req.name == plugin_name:
                continue
            ok, note = self._ensure_subplugin_requirement(
                req, plugin_name=plugin_name, log=log, progress=progress,
                _dep_depth=_dep_depth, _dep_chain=_dep_chain,
            )
            if ok:
                if note:
                    installed.append(note)
            else:
                missing.append(note)

        # 本次装了新依赖 → 热重载目标插件，让其 requires 检查重新通过
        if installed:
            progress(98, "正在热重载子插件")
            try:
                self._run_on_main_wait(lambda: manager.reload_one(plugin_name), timeout=30)
            except MarketplaceError as exc:
                log(f"依赖安装完成，但热重载 {plugin_name} 失败：{exc}")

        ok = not missing and not endstone_missing
        parts: list[str] = []
        if installed:
            parts.append(f"已自动安装依赖: {', '.join(installed)}")
        if endstone_missing:
            parts.append(
                "缺少 Endstone 插件 " + ", ".join(endstone_missing)
                + "，请安装对应插件并重启服务器后重试"
            )
        if missing:
            parts.append("; ".join(missing))
        return {
            "ok": ok,
            "message": "；".join(parts),
            "installed": installed,
            "missing": missing,
            "endstone_missing": endstone_missing,
        }

    def install(self, market_id: str, requested_version: str = "", *, upgrade_dependencies: bool = False, log=None, progress=None, _dep_depth: int = 0, _dep_chain: tuple[str, ...] = ()) -> dict[str, Any]:
        log = log or (lambda _msg: None)
        progress = progress or (lambda _pct, _label="": None)
        log(f"正在获取插件详情: {market_id}")
        detail = self.plugin_detail(market_id)
        if str(detail.get("id") or "") != market_id:
            raise MarketplaceError("市场响应的插件 ID 不匹配")
        release = self._select_release(detail, requested_version)
        log(f"选中版本 v{release['version']}")
        path = self._download_verified(str(release["download_url"]), str(release["sha256"]), log=log, progress=progress)
        try:
            manager = self.plugin.subplugin_manager
            if manager is None:
                raise MarketplaceError("子插件管理器不可用")
            log("正在安装子插件...")
            progress(95, "正在安装")
            outcome = self._run_on_main_wait(lambda: manager.install_from_zip(path))
            if not isinstance(outcome, tuple) or len(outcome) != 3:
                raise MarketplaceError("子插件安装器返回无效结果")
            ok, message, name = bool(outcome[0]), str(outcome[1]), str(outcome[2])
            if not ok:
                raise MarketplaceError(message)
            log(f"子插件 {name} 安装成功")
            self._stamp_origin(name, market_id, release)
            dependencies = release.get("dependencies", [])
            if not isinstance(dependencies, list):
                dependencies = []
            dep_ok, dep_message = self._install_declared_dependencies(
                name, [str(x) for x in dependencies], upgrade=upgrade_dependencies, log=log, progress=progress
            )
            # 插件级强制依赖（requires）：子插件依赖自动安装，Endstone 依赖提示安装
            requirements = self.install_plugin_requirements(
                name, log=log, progress=progress,
                _dep_depth=_dep_depth + 1, _dep_chain=_dep_chain + (name,),
            )
            loaded = False
            with manager._lock:
                installed = manager.subplugins.get(name)
                loaded = bool(installed and installed.loaded)
            return {
                "name": name,
                "market_id": market_id,
                "version": release["version"],
                "installed": True,
                "loaded": loaded,
                "message": message,
                "dependencies_ok": dep_ok,
                "dependencies_message": dep_message,
                "requirements_ok": bool(requirements.get("ok")),
                "requirements_message": str(requirements.get("message") or ""),
                "requirements_installed": list(requirements.get("installed") or []),
                "missing_requirements": list(requirements.get("missing") or []) + list(requirements.get("endstone_missing") or []),
                "endstone_missing": list(requirements.get("endstone_missing") or []),
                "restart_required": not loaded,
            }
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

    def cached_updates(self) -> dict[str, dict[str, Any]]:
        """返回 WebUI 可安全读取的最近更新检查快照。"""
        with self._check_lock:
            return {name: dict(info) for name, info in self._last_checked.items()}

    def check_subplugin_updates(self, *, force: bool = False) -> dict[str, dict[str, Any]]:
        """检查所有市场来源子插件；网络失败只记录错误，不影响运行中的子插件。

        网络请求在锁外执行，避免长时间持 _check_lock 阻塞 cached_updates()。
        """
        if not self.enabled:
            return {}
        manager = self.plugin.subplugin_manager
        if manager is None:
            return {}
        now = int(time.time())
        try:
            interval = max(60, min(7 * 86400, int(self.config.get("check_interval_seconds", 21600))))
        except (TypeError, ValueError):
            interval = 21600

        to_check: list[tuple[str, str, str]] = []  # (name, market_id, local_version)
        cached_results: dict[str, dict[str, Any]] = {}
        with self._check_lock:
            with manager._lock:
                plugins = list(manager.subplugins.values())
            for subplugin in plugins:
                origin = subplugin.manifest.get("_market", {}) if isinstance(subplugin.manifest, dict) else {}
                if not isinstance(origin, dict) or origin.get("source") != "marketplace":
                    continue
                market_id = str(origin.get("id") or "")
                local_version = str(subplugin.manifest.get("version") or origin.get("installed_version") or "0")
                if not _MARKET_ID_RE.fullmatch(market_id):
                    continue
                cached = self._last_checked.get(subplugin.name)
                if not force and cached and now - int(cached.get("checked_at", 0)) < interval:
                    cached_results[subplugin.name] = dict(cached)
                else:
                    to_check.append((subplugin.name, market_id, local_version))

        fresh_results: dict[str, dict[str, Any]] = {}
        for name, market_id, local_version in to_check:
            try:
                response = self._request_json(f"updates/plugin/{market_id}")
                latest = response.get("latest", {})
                if not isinstance(latest, dict):
                    raise MarketplaceError("市场更新数据格式无效")
                remote_version = str(latest.get("version") or "")
                available = bool(_VERSION_RE.fullmatch(remote_version) and _is_newer(remote_version, local_version))
                # 市场端字段类型不可信：字符串会被 list() 拆成单字符列表、
                # 数字直接抛 TypeError 中断整个检查流程
                deps_raw = latest.get("dependencies", [])
                deps_list = [str(d) for d in deps_raw if d] if isinstance(deps_raw, list) else []
                snapshot = {
                    "market_id": market_id,
                    "local_version": local_version,
                    "latest_version": remote_version,
                    "available": available,
                    "checked_at": now,
                    "error": "",
                    "dependencies": deps_list,
                }
                fresh_results[name] = snapshot
            except MarketplaceError as exc:
                fresh_results[name] = {
                    "market_id": market_id, "local_version": local_version,
                    "latest_version": "", "available": False, "checked_at": now,
                    "error": str(exc), "dependencies": [],
                }

        with self._check_lock:
            for name, snapshot in fresh_results.items():
                self._last_checked[name] = snapshot
                self._update_manifest_check_time(name, now)
        result: dict[str, dict[str, Any]] = {}
        with self._check_lock:
            for name, snapshot in fresh_results.items():
                result[name] = dict(snapshot)
        result.update(cached_results)
        return result

    def _update_manifest_check_time(self, plugin_name: str, timestamp: int) -> None:
        manager = self.plugin.subplugin_manager
        if manager is None:
            return
        with manager._lock:
            subplugin = manager.subplugins.get(plugin_name)
            if subplugin is None:
                return
            origin = subplugin.manifest.get("_market")
            if not isinstance(origin, dict):
                return
            origin["last_checked_at"] = timestamp
            manifest_path = subplugin.folder / "lumen.json"
            try:
                tmp_path = manifest_path.with_name(manifest_path.name + ".tmp")
                tmp_path.write_text(json.dumps(subplugin.manifest, ensure_ascii=False, indent=4), encoding="utf-8")
                os.replace(tmp_path, manifest_path)
            except OSError:
                pass

    def update(self, plugin_name: str, requested_version: str = "", *, update_dependencies: bool = True, log=None, progress=None) -> dict[str, Any]:
        log = log or (lambda _msg: None)
        progress = progress or (lambda _pct, _label="": None)
        log(f"正在查找子插件 {plugin_name} 的市场来源...")
        manager = self.plugin.subplugin_manager
        if manager is None:
            raise MarketplaceError("子插件管理器不可用")
        with manager._lock:
            subplugin = manager.subplugins.get(plugin_name)
            if subplugin is None:
                raise MarketplaceError("未找到子插件")
            origin = subplugin.manifest.get("_market", {})
            if not isinstance(origin, dict) or origin.get("source") != "marketplace":
                raise MarketplaceError("该插件不是通过插件市场安装，仍可使用上传或下载链接手动升级")
            market_id = str(origin.get("id") or "")
            local_version = str(subplugin.manifest.get("version") or "")
        if not _MARKET_ID_RE.fullmatch(market_id):
            raise MarketplaceError("市场来源记录无效")
        if requested_version:
            # 市场更新弹窗已限定"仅高于当前版本"，后端再兜底校验一次，
            # 防止旧前端/直接调 API 把版本降级覆盖本地更新的 lumen.json
            if not _VERSION_RE.fullmatch(requested_version):
                raise MarketplaceError("请求更新的版本号非法")
            if not _is_newer(requested_version, local_version):
                raise MarketplaceError(f"目标版本 v{requested_version} 不高于当前版本 v{local_version}，已拒绝降级更新")
            log(f"目标更新版本: v{requested_version}")
        return self.install(market_id, requested_version, upgrade_dependencies=update_dependencies, log=log, progress=progress)

    def update_all(self, *, log=None, progress=None) -> dict[str, Any]:
        """一键更新全部有新版本的市场子插件到最新版。

        单个插件失败不中断其余更新（逐个隔离）；进度按插件数量均分，
        每个插件内部再由 install 的下载/安装阶段细分。
        """
        log = log or (lambda _msg: None)
        progress = progress or (lambda _pct, _label="": None)
        log("正在检查所有子插件的可用更新...")
        updates = self.check_subplugin_updates(force=True)
        pending = [
            (name, info) for name, info in sorted(updates.items())
            if info.get("available") and info.get("market_id")
        ]
        total = len(pending)
        results: list[dict[str, Any]] = []
        if not total:
            return {"total": 0, "updated": [], "failed": [], "message": "没有需要更新的子插件"}

        for idx, (name, info) in enumerate(pending):
            base = idx * 90 // total
            span = max(1, (idx + 1) * 90 // total - base)
            label = f"[{idx + 1}/{total}] {name}"
            log(f"开始更新 {label}: v{info.get('local_version', '?')} → v{info.get('latest_version', '?')}")
            entry = {
                "name": name,
                "market_id": str(info.get("market_id") or ""),
                "from_version": str(info.get("local_version") or ""),
                "to_version": "",
                "ok": False,
                "error": "",
            }
            try:
                # 默认参数在 def 时求值，绑定本轮 base/span/label：
                # 否则闭包引用循环变量，回调整发（如 pip 输出线程延迟触发）时
                # 会读到后续迭代的值，进度条与标签错乱
                def _sub_progress(
                    pct: float,
                    _label: str = "",
                    *,
                    _base: int = base,
                    _span: int = span,
                    _fallback_label: str = label,
                ) -> None:
                    progress(_base + int(pct * _span / 100), _label or _fallback_label)

                result = self.update(name, "", update_dependencies=True, log=log, progress=_sub_progress)
                entry["ok"] = True
                entry["to_version"] = str(result.get("version") or info.get("latest_version") or "")
                log(f"{label} 更新成功")
            except Exception as exc:  # noqa: BLE001
                # 单个失败（网络/市场数据/安装器）不阻断批量更新其余插件
                entry["error"] = str(exc)
                self.logger.warning(f"[Market] 批量更新 {name} 失败: {exc}")
                log(f"{label} 更新失败: {exc}")
            results.append(entry)

        ok_count = sum(1 for r in results if r["ok"])
        fail_count = total - ok_count
        progress(100, "完成")
        return {
            "total": total,
            "updated": [r for r in results if r["ok"]],
            "failed": [r for r in results if not r["ok"]],
            "message": f"批量更新完成: 成功 {ok_count} 个, 失败 {fail_count} 个",
        }

    def update_dependencies(self, plugin_name: str, *, log=None, progress=None) -> dict[str, Any]:
        log = log or (lambda _msg: None)
        progress = progress or (lambda _pct, _label="": None)
        manager = self.plugin.subplugin_manager
        if manager is None:
            raise MarketplaceError("子插件管理器不可用")
        with manager._lock:
            subplugin = manager.subplugins.get(plugin_name)
            if subplugin is None:
                raise MarketplaceError("未找到子插件")
            # 与 _load_one 一致：dependencies 为字符串时 [str(x) for x in "openai"]
            # 会拆成单字符列表，导致 pip install o p e n a i
            deps_raw = subplugin.manifest.get("dependencies", [])
            dependencies = [str(x) for x in deps_raw if x] if isinstance(deps_raw, list) else []
        ok, message = self._install_declared_dependencies(plugin_name, dependencies, upgrade=True, log=log, progress=progress)
        return {"name": plugin_name, "dependencies_ok": ok, "message": message, "restart_required": not ok}

    def framework_update_info(self) -> dict[str, Any]:
        base = str(self.plugin.config_manager.data.get("updates", {}).get("api_url") or "").strip()
        if not base:
            return {"configured": False, "available": False}
        # 与插件市场一致：配置只填站点根地址，这里自动补全 /api/v1/updates/lumenbridge；
        # 若已显式填了完整接口地址（含 /updates/lumenbridge）则按原样请求。
        root = self._site_root(base)
        prefix_match = _API_PREFIX_RE.search(base.rstrip("/"))
        prefix = prefix_match.group(0) if prefix_match else _DEFAULT_API_PREFIX
        query_url = root + "/index.php?lb_route=" + urllib.parse.quote(prefix + "/updates/lumenbridge", safe="/")
        if base.rstrip("/").endswith("/updates/lumenbridge"):
            candidates = [base] if "index.php?lb_route=" in base.lower() else [base, query_url]
        else:
            pretty = self._api_base(base).rstrip("/") + "/updates/lumenbridge"
            candidates = [pretty, query_url]
        # 与市场同站时复用路由形式探测缓存，避免每次更新检查都先吃一次 404
        if root == self._site_root() and self._route_style:
            wanted = "index.php?lb_route=" if self._route_style == "query" else ""
            filtered = [u for u in candidates if ("index.php?lb_route=" in u.lower()) == bool(wanted)]
            candidates = filtered or candidates
        headers = {"User-Agent": "LumenBridge-Update/1"}
        for url in candidates:
            self._validate_endpoint(url)
            try:
                data = self._request_json_at(url, headers=headers)
            except _RouteNotFoundError:
                continue
            if root == self._site_root():
                self._remember_route_style("query" if "index.php?lb_route=" in url.lower() else "pretty")
            latest = data.get("latest", {}) if data.get("available") else {}
            latest_version = str(latest.get("version") or "") if isinstance(latest, dict) else ""
            current = str(getattr(self.plugin, "VERSION", "0"))
            return {"configured": True, "available": bool(latest_version and _is_newer(latest_version, current)), "current_version": current, "latest": latest}
        raise MarketplaceError("框架更新服务返回 HTTP 404")

    def stage_framework_update(self, *, log=None, progress=None) -> dict[str, Any]:
        """验证并原子暂存新 wheel，供 Endstone 下次完整启动加载。

        Endstone 未暴露将运行中同名 wheel 卸载并替换的安全 API，禁用自身会留下半初始化
        管理面板，因此只做可逆的文件级原子更新并要求完整重启；子插件仍可热重载。
        """
        log = log or (lambda _msg: None)
        progress = progress or (lambda _pct, _label="": None)
        with self._framework_update_lock:
            return self._do_stage_framework_update(log=log, progress=progress)

    def _do_stage_framework_update(self, *, log=None, progress=None) -> dict[str, Any]:
        log = log or (lambda _msg: None)
        progress = progress or (lambda _pct, _label="": None)
        info = self.framework_update_info()
        if not info.get("configured"):
            raise MarketplaceError("未配置版本更新 API")
        if not info.get("available"):
            raise MarketplaceError("当前没有可用的 LumenBridge 新版本")
        latest = info.get("latest")
        if not isinstance(latest, dict):
            raise MarketplaceError("版本更新数据格式无效")
        version = str(latest.get("version") or "")
        download_url = str(latest.get("download_url") or "")
        sha256 = str(latest.get("sha256") or "").lower()
        if not _VERSION_RE.fullmatch(version) or not download_url or not re.fullmatch(r"[a-f0-9]{64}", sha256):
            raise MarketplaceError("版本更新记录缺少有效版本、下载地址或 SHA-256")
        # 幂等复用：目标 wheel 已按同一发布记录暂存且哈希一致时跳过重复下载，
        # 供"暂存→立即热重载"两步流程与自动更新共用。
        receipt_path = Path(self.plugin.data_folder) / "data" / "framework_update.json"
        plugins_dir = Path(self.plugin.data_folder).parent
        target = plugins_dir / f"endstone_lumenbridge-{version}-py3-none-any.whl"
        if receipt_path.is_file() and target.is_file():
            try:
                receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError, UnicodeDecodeError):
                receipt = None
            if (
                isinstance(receipt, dict)
                and receipt.get("to_version") == version
                and str(receipt.get("sha256") or "").lower() == sha256
                and receipt.get("wheel") == target.name
                and self._file_sha256(target) == sha256
            ):
                return receipt
        # H20：框架更新的哈希来自 updates.api_url，pin 基准同样是该 API 主机
        #（未配置时退回市场 api_url 基准）
        updates_base = str(self.plugin.config_manager.data.get("updates", {}).get("api_url") or "").strip()
        log(f"开始下载 LumenBridge v{version}")
        download = self._download_verified(
            download_url, sha256,
            expected_base=self._api_base(updates_base) if updates_base else "",
            log=log, progress=progress,
        )
        try:
            log("正在校验 wheel 元数据...")
            try:
                with zipfile.ZipFile(download) as wheel:
                    metadata_name = next(name for name in wheel.namelist() if name.endswith(".dist-info/METADATA"))
                    metadata = wheel.read(metadata_name).decode("utf-8", "replace")
            except (OSError, zipfile.BadZipFile, StopIteration) as exc:
                raise MarketplaceError("更新下载不是有效的 Python wheel") from exc
            name_match = re.search(r"^Name:\s*(.+)$", metadata, re.MULTILINE | re.IGNORECASE)
            version_match = re.search(r"^Version:\s*(.+)$", metadata, re.MULTILINE | re.IGNORECASE)
            dist_name = _normalize_distribution_name(name_match.group(1).strip()) if name_match else ""
            wheel_version = version_match.group(1).strip() if version_match else ""
            if dist_name != "endstone_lumenbridge" or wheel_version != version:
                raise MarketplaceError("wheel 元数据与市场发布版本不匹配")

            plugins_dir = Path(self.plugin.data_folder).parent
            if not plugins_dir.is_dir():
                raise MarketplaceError(f"无法确定 Endstone plugins 目录：{plugins_dir}")
            target = plugins_dir / f"endstone_lumenbridge-{version}-py3-none-any.whl"
            stage = plugins_dir / f".{target.name}.download"
            backup_dir = plugins_dir / ".lumenbridge_update_backups" / time.strftime("%Y%m%d-%H%M%S")
            try:
                shutil.copyfile(download, stage)
                # 先原子放置新 wheel 再移出旧 wheel：若先移出旧 wheel、放置
                # 新 wheel 失败，plugins 目录将没有任何 wheel（重启后插件
                # 彻底丢失）。代价是存在短暂的新旧共存窗口；若移出旧 wheel
                # 中途失败，必须删掉新 wheel 并放回已移出的旧 wheel——
                # 否则重启时两个 wheel 都被安装，安装顺序不确定，
                # 可能旧版覆盖新版（如 1.0.9 与 1.0.10 的字典序颠倒）
                os.replace(stage, target)
                moved: list[Path] = []
                try:
                    for old in plugins_dir.glob("endstone_lumenbridge-*.whl"):
                        if old.resolve() == target.resolve():
                            continue
                        backup_dir.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(old), str(backup_dir / old.name))
                        moved.append(old)
                except OSError:
                    try:
                        target.unlink(missing_ok=True)
                        for old in moved:
                            shutil.move(str(backup_dir / old.name), str(old))
                    except OSError:
                        pass
                    raise
            except OSError as exc:
                try:
                    stage.unlink(missing_ok=True)
                except OSError:
                    pass
                raise MarketplaceError(f"无法原子暂存更新 wheel：{exc}") from exc
            receipt = {
                "from_version": str(info.get("current_version") or ""),
                "to_version": version,
                "sha256": sha256,
                "wheel": target.name,
                "backup_directory": str(backup_dir) if backup_dir.exists() else "",
                "staged_at": int(time.time()),
                # 支持进程内热重载（apply_framework_update），无需强制重启
                "restart_required": False,
            }
            receipt_dir = Path(self.plugin.data_folder) / "data"
            receipt_dir.mkdir(parents=True, exist_ok=True)
            (receipt_dir / "framework_update.json").write_text(
                json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            log(f"新版本 v{version} 已暂存，准备热重载")
            return receipt
        finally:
            try:
                os.unlink(download)
            except OSError:
                pass

    @staticmethod
    def _file_sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def apply_framework_update(self, *, delay_ticks: int = 20, log=None, progress=None) -> dict[str, Any]:
        """暂存新 wheel 并调度进程内热重载，实现"自动更新、立即生效"。

        热重载走 Endstone 官方 ``Server.reload()``（即 ``/reload`` 命令的同
        一实现，endstone 0.11.x ``server.cpp``）：``disablePlugins`` →
        ``clearPlugins``（清空插件注册表）→ ``reloadData`` → ``loadPlugins``
        （重建 PythonPluginLoader：自动清理 sys.modules 中 endstone_* 模块
        与 plugins/.local 旧发行版，再 pip install plugins/ 下全部 wheel）→
        ``enablePlugins``。

        不能用 ``disable_plugin`` + ``load_plugin`` 手工替换：disable 只停用
        不注销，旧插件名仍留在 PluginManager 的 lookup_names_ 里，
        ``load_plugin`` 会被 "Another plugin with the same name has been
        loaded" 拒绝。只有 clearPlugins 才清注册表，而这正是 reload 的一部分。

        注意 reload 语义与 ``/reload`` 一致：服务器上所有 Endstone 插件都会
        重载（非仅 LumenBridge 自身）。
        """
        log = log or (lambda _msg: None)
        progress = progress or (lambda _pct, _label="": None)
        with self._framework_update_lock:
            receipt = self._do_stage_framework_update(log=log, progress=progress)
        plugins_dir = Path(self.plugin.data_folder).parent
        target = plugins_dir / str(receipt.get("wheel") or "")
        if not target.is_file():
            raise MarketplaceError(f"暂存的更新 wheel 不存在：{target}")
        version = str(receipt.get("to_version") or "")
        backup_directory = str(receipt.get("backup_directory") or "")
        old_plugin = self.plugin
        logger = self.logger
        old_plugin.logger.info(f"[Update] 已暂存 v{version}，{delay_ticks} tick 后开始热重载")
        log(f"已暂存 v{version}，{delay_ticks} tick 后开始热重载")

        def _restore_backup(reason: str) -> bool:
            """移除失败的新 wheel 并放回备份中最高版本的旧 wheel，返回是否放回成功。"""
            logger.error(f"[Update] {reason}")
            try:
                target.unlink(missing_ok=True)
                if backup_directory:
                    backup_dir = Path(backup_directory)
                    # 按语义版本挑最高（不能按文件名字典序：字典序下
                    # "1.0.9" > "1.0.10"，会放回更旧的版本）
                    wheels = list(backup_dir.glob("endstone_lumenbridge-*.whl"))
                    if wheels:
                        def _wheel_version(p: Path) -> tuple[int, ...]:
                            parts = p.stem.split("-")
                            return _version_tuple(parts[1]) if len(parts) > 1 else (0,)
                        wheel = max(wheels, key=_wheel_version)
                        shutil.copyfile(wheel, plugins_dir / wheel.name)
                        return True
            except OSError:
                pass
            return False

        def _trigger_reload(server: Any) -> None:
            reload_api = getattr(server, "reload", None)
            if callable(reload_api):
                reload_api()
            else:  # 旧版 Endstone 无 Server.reload 绑定，回退命令派发
                server.dispatch_command(server.command_sender, "reload")

        def _hot_swap() -> None:
            # 运行在服务器主线程；只使用局部引用，禁用后不再触碰旧插件对象
            server = old_plugin.server
            manager = server.plugin_manager
            try:
                log("正在热重载（等同 /reload，服务器插件将全部重载）...")
                _trigger_reload(server)
                new_plugin = manager.get_plugin("lumenbridge")
                if new_plugin is None or not manager.is_plugin_enabled(new_plugin):
                    raise RuntimeError("热重载后未检测到已启用的 LumenBridge 插件")
                log(f"热重载完成，当前版本 v{new_plugin.version}")
                new_plugin.logger.info(f"[Update] 热重载完成，当前版本 v{new_plugin.version}")
            except Exception as exc:  # noqa: BLE001
                logger.error(f"[Update] 热重载失败：{exc}")
                if _restore_backup("已回滚旧 wheel，正在再次热重载以恢复旧版本"):
                    try:
                        _trigger_reload(server)
                        recovered = manager.get_plugin("lumenbridge")
                        if recovered is not None and manager.is_plugin_enabled(recovered):
                            logger.error(
                                f"[Update] 已恢复旧版本 v{recovered.version} 运行；"
                                "新版本更新失败，请检查服务器日志或联系开发者"
                            )
                            return
                    except Exception as exc2:  # noqa: BLE001
                        logger.error(f"[Update] 恢复旧版本时出错：{exc2}")
                logger.error("[Update] 自动恢复未完成，请重启服务器以恢复 LumenBridge 运行")

        old_plugin.run_on_main(_hot_swap, delay=delay_ticks)
        return {"scheduled": True, "to_version": version, "wheel": target.name, "restart_required": False}
