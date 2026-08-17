"""LumenBridge 插件市场与更新客户端。

不信任网络下载：所有市场插件必须先核对发布记录中的 SHA-256，再交给
SubPluginManager 已有的 ZIP 路径穿越防护流程。
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

if TYPE_CHECKING:
    from .plugin import LumenBridgePlugin

_MARKET_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,63}$")
_VERSION_RE = re.compile(r"^[0-9A-Za-z.+-]{3,80}$")
# 市场站点的 API 版本前缀；配置里只填站点根地址时会自动补全
_API_PREFIX_RE = re.compile(r"/api/v\d+$", re.IGNORECASE)
_DEFAULT_API_PREFIX = "/api/v1"


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
    return _version_tuple(remote) > _version_tuple(local)


class MarketplaceError(RuntimeError):
    """市场端点、下载或完整性校验失败。"""


class _RouteNotFoundError(MarketplaceError):
    """HTTP 404 且无业务错误信息：触发 pretty→query 路由形式回退（内部信号）。"""


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
        # 相对路径：以 / 开头直接挂在站点根下，否则补一个 /
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
            # H2：SSRF 防护——目标主机必须解析到公网地址（防 DNS 解析到内网）
            from .webui.server import _validate_public_http_url
            _validate_public_http_url(url)
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
            try:
                err_raw = exc.read(2048)
                err_payload = json.loads(err_raw.decode("utf-8", errors="replace"))
                if isinstance(err_payload, dict) and isinstance(err_payload.get("error"), dict):
                    message = str(err_payload["error"].get("message") or "")
            except (ValueError, AttributeError):
                pass
            # 404 且服务端未返回业务错误信息：可能是主机不支持 rewrite 导致
            # pretty 路径不存在，用内部信号通知上层尝试 query 形式回退
            if exc.code == 404 and not message:
                raise _RouteNotFoundError() from exc
            raise MarketplaceError(message or f"市场服务返回 HTTP {exc.code}") from exc
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
            if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
                message = str(payload["error"].get("message") or "")
            raise MarketplaceError(message or "市场请求失败")
        return payload["data"]

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
        if not _MARKET_ID_RE.fullmatch(market_id):
            raise MarketplaceError("市场插件 ID 非法")
        reason = str(reason).strip()[:4000]
        contact = str(contact).strip()[:254]
        if not reason:
            raise MarketplaceError("请填写举报内容")
        key = str(self.config.get("report_api_key") or "").strip()
        if not key:
            # 服务端对跨主机写操作要求密钥，缺失时必然被 CSRF 校验拒绝；
            # 快速失败并给出配置指引，优于放行后收到晦涩的 403
            raise MarketplaceError(
                "市场未配置访问密钥：请在市场站点后台设置 webui_report_api_key，"
                "并在 LumenBridge 配置 marketplace.report_api_key 填写同一值后重试"
            )
        headers = {"X-LumenBridge-Report-Key": key}
        return self._request_json(f"market/plugins/{market_id}/report", method="POST", body={"reason": reason, "contact": contact}, headers=headers)

    def like_plugin(self, market_id: str, liked: bool) -> dict[str, Any]:
        """点赞/取消点赞市场插件。

        PHP 市场对跨主机写操作（点赞/举报）要求 report key；未配置时给出
        明确指引，而不是让服务端 CSRF 错误误导用户。举报走同一校验，
        未配置密钥时同样快速失败。
        """
        if not _MARKET_ID_RE.fullmatch(market_id):
            raise MarketplaceError("市场插件 ID 非法")
        key = str(self.config.get("report_api_key") or "").strip()
        if not key:
            raise MarketplaceError(
                "市场未配置访问密钥：请在市场站点后台设置 webui_report_api_key，"
                "并在 LumenBridge 配置 marketplace.report_api_key 填写同一值后重试"
            )
        headers = {"X-LumenBridge-Report-Key": key}
        return self._request_json(
            f"market/plugins/{market_id}/like",
            method="POST",
            body={"liked": bool(liked)},
            headers=headers,
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

    def _download_verified(self, url: str, sha256: str, *, expected_base: str = "") -> str:
        """下载并校验 SHA-256；expected_base 用于指定主机 pin 的基准地址。

        H20：默认 pin 到配置的市场 api_url（scheme+host 必须一致），使"哈希来源"
        与"下载通道"解耦——市场端被篡改也无法把下载指向任意主机；
        框架更新等其它 API 来源可显式传入自身 base。
        """
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
            temporary.close()
            actual = digest.hexdigest()
            # 用常量时间比较避免时序侧信道
            if not hmac.compare_digest(actual, sha256.lower()):
                raise MarketplaceError("插件 SHA-256 校验失败，已拒绝安装")
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

    def _install_declared_dependencies(self, plugin_name: str, dependencies: list[str], *, upgrade: bool = False) -> tuple[bool, str]:
        if not dependencies:
            return True, ""
        manager = self.plugin.subplugin_manager
        pip_manager = self.plugin._get_pip_manager()
        if manager is None or pip_manager is None:
            return False, "pip 管理器不可用"
        # 必须持 plugin._pip_serial_lock：旧版本直接调用 pip_manager.install 绕过了 WebUI 的
        # _pip_serial_lock，可与 WebUI 安装任务并发执行而损坏 site-packages 元数据。
        with self.plugin._pip_serial_lock:
            ok, message = pip_manager.install(dependencies, upgrade=upgrade)
        if not ok:
            return False, message
        loaded = bool(self._run_on_main_wait(lambda: manager.reload_one(plugin_name), timeout=20))
        if not loaded:
            return False, "依赖已安装，但子插件热重载失败；请在子插件页面查看错误详情"
        return True, message

    def install(self, market_id: str, requested_version: str = "", *, upgrade_dependencies: bool = False) -> dict[str, Any]:
        detail = self.plugin_detail(market_id)
        if str(detail.get("id") or "") != market_id:
            raise MarketplaceError("市场响应的插件 ID 不匹配")
        release = self._select_release(detail, requested_version)
        path = self._download_verified(str(release["download_url"]), str(release["sha256"]))
        try:
            manager = self.plugin.subplugin_manager
            if manager is None:
                raise MarketplaceError("子插件管理器不可用")
            outcome = self._run_on_main_wait(lambda: manager.install_from_zip(path))
            if not isinstance(outcome, tuple) or len(outcome) != 3:
                raise MarketplaceError("子插件安装器返回无效结果")
            ok, message, name = bool(outcome[0]), str(outcome[1]), str(outcome[2])
            if not ok:
                raise MarketplaceError(message)
            self._stamp_origin(name, market_id, release)
            dependencies = release.get("dependencies", [])
            if not isinstance(dependencies, list):
                dependencies = []
            dep_ok, dep_message = self._install_declared_dependencies(name, [str(x) for x in dependencies], upgrade=upgrade_dependencies)
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
                manifest_path.write_text(json.dumps(subplugin.manifest, ensure_ascii=False, indent=4), encoding="utf-8")
            except OSError:
                pass

    def update(self, plugin_name: str, *, update_dependencies: bool = True) -> dict[str, Any]:
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
        if not _MARKET_ID_RE.fullmatch(market_id):
            raise MarketplaceError("市场来源记录无效")
        return self.install(market_id, upgrade_dependencies=update_dependencies)

    def update_dependencies(self, plugin_name: str) -> dict[str, Any]:
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
        ok, message = self._install_declared_dependencies(plugin_name, dependencies, upgrade=True)
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

    def stage_framework_update(self) -> dict[str, Any]:
        """验证并原子暂存新 wheel，供 Endstone 下次完整启动加载。

        Endstone 未暴露将运行中同名 wheel 卸载并替换的安全 API，禁用自身会留下半初始化
        管理面板，因此只做可逆的文件级原子更新并要求完整重启；子插件仍可热重载。
        """
        with self._framework_update_lock:
            return self._do_stage_framework_update()

    def _do_stage_framework_update(self) -> dict[str, Any]:
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
        # H20：框架更新的哈希来自 updates.api_url，pin 基准同样是该 API 主机
        #（未配置时退回市场 api_url 基准）
        updates_base = str(self.plugin.config_manager.data.get("updates", {}).get("api_url") or "").strip()
        download = self._download_verified(
            download_url, sha256,
            expected_base=self._api_base(updates_base) if updates_base else "",
        )
        try:
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
                # 先原子放置新 wheel 再移出旧 wheel，避免移出后放置失败导致 plugins 目录无可用 wheel
                os.replace(stage, target)
                for old in plugins_dir.glob("endstone_lumenbridge-*.whl"):
                    if old.resolve() == target.resolve():
                        continue
                    backup_dir.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(old), str(backup_dir / old.name))
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
                "restart_required": True,
            }
            Path(self.plugin.data_folder).mkdir(parents=True, exist_ok=True)
            (Path(self.plugin.data_folder) / "framework_update.json").write_text(
                json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            return receipt
        finally:
            try:
                os.unlink(download)
            except OSError:
                pass
