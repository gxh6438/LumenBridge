"""功能模块 API：正则规则、白名单绑定、聊天过滤词库与自定义页面。"""

from __future__ import annotations

import copy
import re
import secrets
import time
from pathlib import Path
from typing import Any

from ...i18n import t as _t
from .base import RouteMixin

_RULES_IMAGE_EXTS = ("png", "jpg", "gif", "webp")


class ModulesApi(RouteMixin):

    # ---- 正则规则 ----

    def api_rules_get(self, m: re.Match, query: dict[str, str]) -> None:
        regex = self.plugin.regex_module
        if regex:
            # 先取快照，避免与规则保存线程竞态
            if hasattr(regex, "snapshot_rules"):
                rules_snapshot = regex.snapshot_rules()
            else:
                rules_snapshot = copy.deepcopy(regex.rules)
        else:
            rules_snapshot = []
        self._send_json({"code": 200, "data": rules_snapshot})

    def api_rules_post(self, m: re.Match, query: dict[str, str]) -> None:
        regex = self.plugin.regex_module
        body = self._read_body()
        if not isinstance(body, list):
            self.api_error(400, "webui.msg.rules_must_be_array")
            return
        if regex:
            regex.save_rules(body)
        self._send_json({"code": 200, "msg": _t("webui.msg.rules_saved")})

    def api_rules_image(self, m: re.Match, query: dict[str, str]) -> None:
        # 图片存 rules_images/（image() 白名单内）
        file_info = self._read_multipart_file()
        if not file_info:
            self.api_error(400, "webui.msg.rules_image_invalid")
            return
        filename, content = file_info
        ext = Path(filename).suffix.lower().lstrip(".")
        if ext == "jpeg":
            ext = "jpg"
        if ext not in _RULES_IMAGE_EXTS:
            self.api_error(400, "webui.msg.rules_image_type")
            return
        if len(content) > 8 * 1024 * 1024:
            self.api_error(400, "webui.msg.rules_image_too_large")
            return
        img_dir = Path(self.plugin.data_folder) / "rules_images"
        img_dir.mkdir(parents=True, exist_ok=True)
        fname = f"rule_{int(time.time() * 1000)}_{secrets.token_hex(4)}.{ext}"
        try:
            (img_dir / fname).write_bytes(content)
        except OSError as exc:
            self.api_error(500, "webui.msg.rules_image_write_failed", error=exc)
            return
        self._send_json({"code": 200, "data": {"path": str(img_dir / fname), "name": fname}})

    # ---- 白名单 ----

    def api_whitelist_get(self, m: re.Match, query: dict[str, str]) -> None:
        wl = getattr(self.plugin, "whitelist_module", None)
        domain = str(query.get("domain", "") or "")
        data = wl.snapshot(domain if domain in ("qq", "official") else None) if wl else []
        self._send_json({"code": 200, "data": data})

    def api_whitelist_domains(self, m: re.Match, query: dict[str, str]) -> None:
        statuses = self.plugin.hub.status() if getattr(self.plugin, "hub", None) else []
        enabled_qq = any(s.get("type") != "qqofficial" and s.get("enabled") for s in statuses)
        enabled_official = any(s.get("type") == "qqofficial" and s.get("enabled") for s in statuses)
        connected_qq = any(s.get("type") != "qqofficial" and s.get("connected") for s in statuses)
        connected_official = any(s.get("type") == "qqofficial" and s.get("connected") for s in statuses)
        if not enabled_official:
            default = "qq"
        elif not enabled_qq:
            default = "official"
        elif connected_qq:
            default = "qq"
        elif connected_official:
            default = "official"
        else:
            default = "qq"
        self._send_json({"code": 200, "data": {
            "has_qq": enabled_qq,
            "has_official": enabled_official,
            "connected_qq": connected_qq,
            "connected_official": connected_official,
            "default": default,
        }})

    def api_whitelist_delete(self, m: re.Match, query: dict[str, str]) -> None:
        wl = getattr(self.plugin, "whitelist_module", None)
        domain = str(query.get("domain", "") or "qq")
        if domain not in ("qq", "official"):
            domain = "qq"
        if not wl or not wl.get_binding_by_qq(m.group(1), domain):
            self.api_error(404, "webui.msg.qq_not_bound")
            return
        success, message, entry = wl.unbind_sync(m.group(1), domain=domain)
        if success:
            self._send_json({"code": 200, "msg": message, "data": entry})
            return
        self._send_json({"code": 409, "msg": _t("webui.msg.unbind_failed", msg=message), "data": entry}, 409)

    # ---- 自定义页面 ----

    def api_custom_pages(self, m: re.Match, query: dict[str, str]) -> None:
        with self.webui._ext_lock:
            pages_snapshot = list(self.webui.custom_pages)
        self._send_json({"code": 200, "data": pages_snapshot})

    # ---- 聊天过滤 ----

    def _require_chat_filter(self) -> Any:
        chat_filter = getattr(self.plugin, "chat_filter", None)
        if chat_filter is None:
            self.api_error(500, "webui.msg.module_unavailable")
            return None
        return chat_filter

    def api_chat_filter_get(self, m: re.Match, query: dict[str, str]) -> None:
        chat_filter = self._require_chat_filter()
        if chat_filter is None:
            return
        self._send_json({"code": 200, "data": chat_filter.snapshot()})

    def api_chat_filter_put(self, m: re.Match, query: dict[str, str]) -> None:
        chat_filter = self._require_chat_filter()
        if chat_filter is None:
            return
        payload = self._read_body()
        if not isinstance(payload, dict):
            self.api_error(400, "webui.msg.invalid_body")
            return
        chat_filter.update(payload)
        self._send_json({"code": 200, "msg": _t("webui.msg.chat_filter_saved")})

    def api_chat_filter_import(self, m: re.Match, query: dict[str, str]) -> None:
        chat_filter = self._require_chat_filter()
        if chat_filter is None:
            return
        payload = self._read_body()
        bank = str((payload or {}).get("file") or "") if isinstance(payload, dict) else ""
        if not bank:
            self.api_error(400, "webui.msg.chat_filter_no_file")
            return
        try:
            added = chat_filter.import_wordbank(bank)
        except FileNotFoundError:
            self.api_error(404, "webui.msg.chat_filter_no_file")
            return
        except OSError as e:
            self.api_error(500, "webui.msg.chat_filter_import_failed", error=e)
            return
        self._send_json({
            "code": 200,
            "msg": _t("webui.msg.chat_filter_imported", count=added),
            "data": chat_filter.snapshot(),
        })

    def api_chat_filter_import_text(self, m: re.Match, query: dict[str, str]) -> None:
        chat_filter = self._require_chat_filter()
        if chat_filter is None:
            return
        payload = self._read_body()
        text = str((payload or {}).get("text") or "") if isinstance(payload, dict) else ""
        name = str((payload or {}).get("name") or "import.txt") if isinstance(payload, dict) else "import.txt"
        if not text.strip():
            self.api_error(400, "webui.msg.chat_filter_no_file")
            return
        try:
            added = chat_filter.import_words_text(text, source=Path(name).name)
        except OSError as e:
            self.api_error(500, "webui.msg.chat_filter_import_failed", error=e)
            return
        self._send_json({
            "code": 200,
            "msg": _t("webui.msg.chat_filter_imported", count=added),
            "data": chat_filter.snapshot(),
        })
