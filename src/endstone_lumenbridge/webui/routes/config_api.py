"""核心配置与子插件配置表单 API。"""

from __future__ import annotations

import json
import re
import threading
from typing import Any

from ...i18n import t as _t
from .. import auth as auth_util
from ..sensitive import build_config_labels, mask_sensitive, unmask_sensitive
from .base import RouteMixin


def _schema_value_ok(item: dict[str, Any], v: Any) -> bool:
    """按 schema item 的 type 校验值类型，非法类型直接跳过不写入。"""
    t = item.get("type")
    if t == "number":
        # bool 是 int 子类，必须显式排除
        ok = isinstance(v, (int, float)) and not isinstance(v, bool)
        if ok and isinstance(item.get("min"), (int, float)):
            ok = v >= item["min"]
        if ok and isinstance(item.get("max"), (int, float)):
            ok = v <= item["max"]
        return ok
    if t == "switch":
        return isinstance(v, bool)
    if t in ("select", "multiselect"):
        options = item.get("options")
        if isinstance(options, list):
            allowed = {o.get("value") if isinstance(o, dict) else o for o in options}
            if t == "multiselect":
                return isinstance(v, list) and all(x in allowed for x in v)
            return v in allowed
        return True
    if t == "array":
        return isinstance(v, list)
    return isinstance(v, str)


class ConfigApi(RouteMixin):

    def api_config_get(self, m: re.Match, query: dict[str, str]) -> None:
        plugin = self.plugin
        cm = plugin.config_manager
        # 外部手改 config.json 热重载（指纹检测）
        try:
            if cm is not None and cm.file_changed_on_disk():
                cm.load()
                self.webui._schedule_config_hot_apply(plugin)
        except Exception:  # noqa: BLE001
            pass
        # 先取快照，避免与保存线程并发修改
        snapshot = cm.snapshot() if hasattr(cm, "snapshot") else json.loads(json.dumps(cm.data))
        data = json.loads(json.dumps(snapshot))
        mask_sensitive(data)
        self._send_json({"code": 200, "data": data})

    def api_config_post(self, m: re.Match, query: dict[str, str]) -> None:
        plugin = self.plugin
        cm = plugin.config_manager
        body = self._read_body()
        if not isinstance(body, dict):
            self.api_error(400, "webui.msg.invalid_config")
            return

        unmask_sensitive(body, cm.data)

        body_webui = body.get("webui")
        if isinstance(body_webui, dict) and isinstance(body_webui.get("password"), str):
            typed_pw = body_webui["password"].strip()
            if (typed_pw and set(typed_pw) != {"*"}
                    and not auth_util.is_hashed_password(typed_pw)):
                body_webui["password"] = auth_util.hash_password(typed_pw)

        old_language = str(cm.data.get("language", "auto"))
        try:
            cm.apply_patch(body)
        except Exception as exc:  # noqa: BLE001
            self.api_error(400, "webui.msg.invalid_config", error=exc)
            return

        # 自动检测须访问 server 对象，调度到主线程
        new_language = str(body.get("language", old_language))
        init_i18n = getattr(plugin, "_init_i18n", None)
        if callable(init_i18n) and new_language != old_language:
            done = threading.Event()

            def _apply_language() -> None:
                try:
                    init_i18n()
                except Exception:  # noqa: BLE001
                    pass
                finally:
                    done.set()

            try:
                plugin.run_on_main(_apply_language)
            except Exception:  # noqa: BLE001
                done.set()
            done.wait(timeout=2.0)

        try:
            self.webui._apply_webui_secret_changes(cm)
        except Exception:  # noqa: BLE001
            pass
        plugin.bus.emit("config.update.core", body)
        self._send_json({"code": 200, "msg": _t("webui.msg.config_saved")})

    def api_config_labels(self, m: re.Match, query: dict[str, str]) -> None:
        self._send_json({"code": 200, "data": build_config_labels()})

    def api_plugins_configs(self, m: re.Match, query: dict[str, str]) -> None:
        with self.webui._ext_lock:
            keys = list(self.webui.plugins_config_schema.keys())
        self._send_json({"code": 200, "data": keys})

    def api_plugin_config_get(self, m: re.Match, query: dict[str, str]) -> None:
        with self.webui._ext_lock:
            schema = self.webui.plugins_config_schema.get(m.group(1))
            if not schema:
                self.api_error(404, "webui.msg.plugin_no_config")
                return
            snapshot = json.loads(json.dumps(schema))
        self._send_json({"code": 200, "data": snapshot})

    def api_plugin_config_post(self, m: re.Match, query: dict[str, str]) -> None:
        with self.webui._ext_lock:
            schema = self.webui.plugins_config_schema.get(m.group(1))
        if not schema:
            self.api_error(404, "webui.msg.plugin_no_config")
            return
        body = self._read_body() or {}
        if not isinstance(body, dict):
            self.api_error(400, "webui.msg.invalid_body")
            return

        # 加锁防并发 POST 迭代异常
        applied: list[tuple[str, Any]] = []
        with self.webui._ext_lock:
            for item in list(schema["items"]):
                if item.get("type") == "section":
                    continue
                if item["key"] in body:
                    value = body[item["key"]]
                    if _schema_value_ok(item, value):
                        item["val"] = value
                        applied.append((item["key"], value))
        # 只广播真正写入的键值
        for key, value in applied:
            self.plugin.bus.emit(f"config.update.{m.group(1)}", key, value)
        self._send_json({"code": 200, "msg": _t("webui.msg.plugin_config_saved")})
