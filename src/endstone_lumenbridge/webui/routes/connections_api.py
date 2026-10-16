"""适配器连接管理 API：CRUD、敏感字段揭示、绑定密钥与 QQ 官方扫码登录。"""

from __future__ import annotations

import re
import time
from typing import Any

from ...i18n import t as _t
from .base import RouteMixin


class ConnectionsApi(RouteMixin):

    def api_connections_get(self, m: re.Match, query: dict[str, str]) -> None:
        connections = self.require_connections()
        if connections is None:
            return
        hub = getattr(self.plugin, "hub", None)
        self._send_json({"code": 200, "data": {
            "adapters": connections.snapshot(mask=True),
            "status": hub.status() if hub is not None else [],
        }})

    def api_connections_create(self, m: re.Match, query: dict[str, str]) -> None:
        connections = self.require_connections()
        if connections is None:
            return
        body = self.read_json_dict()
        if body is None:
            return
        try:
            created = connections.create(body)
        except Exception as exc:  # noqa: BLE001
            self._send_json({"code": 400, "msg": str(exc)}, 400)
            return
        self._reload_and_respond(created)

    def api_connections_update(self, m: re.Match, query: dict[str, str]) -> None:
        connections = self.require_connections()
        if connections is None:
            return
        body = self.read_json_dict()
        if body is None:
            return
        try:
            updated = connections.update(m.group(1), body)
        except Exception as exc:  # noqa: BLE001
            self._send_json({"code": 400, "msg": str(exc)}, 400)
            return
        self._reload_and_respond(updated)

    def api_connections_delete(self, m: re.Match, query: dict[str, str]) -> None:
        connections = self.require_connections()
        if connections is None:
            return
        try:
            removed = connections.delete(m.group(1))
        except Exception as exc:  # noqa: BLE001
            self._send_json({"code": 400, "msg": str(exc)}, 400)
            return
        if not removed:
            self.api_error(404, "connections.not_found", id=m.group(1))
            return
        self._reload_and_respond(None)

    def _reload_and_respond(self, data: Any) -> None:
        """保存后重载连接；重载失败只提示，不回滚配置。"""
        try:
            self.plugin.reload_onebot_connection()
        except Exception as exc:  # noqa: BLE001
            if data is None:
                self._send_json({"code": 200, "msg": _t("connections.saved_reload_failed", error=exc)})
            else:
                self._send_json({"code": 200, "data": data, "msg": _t("connections.saved_reload_failed", error=exc)})
            return
        if data is None:
            self._send_json({"code": 200, "msg": _t("connections.saved")})
        else:
            self._send_json({"code": 200, "data": data, "msg": _t("connections.saved")})

    def api_connections_reload(self, m: re.Match, query: dict[str, str]) -> None:
        try:
            self.plugin.reload_onebot_connection()
        except Exception as exc:  # noqa: BLE001
            self._send_json({"code": 500, "msg": str(exc)}, 500)
            return
        self._send_json({"code": 200, "msg": _t("connections.reloaded")})

    def api_connections_reveal(self, m: re.Match, query: dict[str, str]) -> None:
        connections = self.require_connections()
        if connections is None:
            return
        body = self._read_body()
        adapter_id = str(body.get("id", "") or "") if isinstance(body, dict) else ""
        key = str(body.get("key", "") or "") if isinstance(body, dict) else ""
        if key not in ("access_token", "app_secret"):
            self.api_error(400, "connections.reveal_invalid_key")
            return
        adapter = connections.get(adapter_id)
        if adapter is None:
            self.api_error(404, "connections.not_found", id=adapter_id)
            return
        # 高危操作，返回前记审计日志
        self.webui.logger.warning(_t(
            "webui.log.reveal_audit",
            ip=self.client_ip(), id=adapter_id, field=key,
        ))
        self._send_json({"code": 200, "data": {
            "value": str(adapter.get(key, "") or ""),
            "reveal_at": int(time.time()),
        }})

    def api_connections_bindkey(self, m: re.Match, query: dict[str, str]) -> None:
        group_bind = getattr(self.plugin, "group_bind_module", None)
        if group_bind is None:
            self.api_error(500, "connections.unavailable")
            return
        body = self._read_body()
        adapter_id = str(body.get("id", "") or "") if isinstance(body, dict) else ""
        info = group_bind.issue_key(adapter_id)
        if info is None:
            self.api_error(404, "connections.not_found", id=adapter_id)
            return
        self._send_json({"code": 200, "data": info})

    def api_qr_create(self, m: re.Match, query: dict[str, str]) -> None:
        qr_ip = self.client_ip()
        with self.webui._qr_create_lock:
            qr_now = time.time()
            if qr_now - self.webui._qr_create_last.get(qr_ip, 0.0) < 30:
                self.api_error(429, "webui.msg.qr_create_rate_limited")
                return
            if len(self.webui._qr_create_last) > 1024:
                self.webui._qr_create_last = {
                    k: v for k, v in self.webui._qr_create_last.items() if qr_now - v < 30
                }
            self.webui._qr_create_last[qr_ip] = qr_now

        from ...onebot import qqofficial_bind

        try:
            task = qqofficial_bind.create_bind_task_sync()
        except Exception as exc:  # noqa: BLE001
            self.api_error(502, "connections.qr_create_failed", error=exc)
            return
        with self.webui._qr_bind_lock:
            now = time.time()
            for stale in [
                tid for tid, info in self.webui._qr_bind_tasks.items()
                if now - float(info.get("created_at", 0)) > 600
            ]:
                self.webui._qr_bind_tasks.pop(stale, None)
            self.webui._qr_bind_tasks[task["task_id"]] = {
                "bind_key": task["bind_key"],
                "created_at": now,
            }
        self._send_json({"code": 200, "data": {
            "task_id": task["task_id"],
            "qrcode": task["qrcode"],
            "interval": int(task["interval"]),
        }})

    def api_qr_poll(self, m: re.Match, query: dict[str, str]) -> None:
        from ...onebot import qqofficial_bind

        body = self._read_body()
        task_id = str(body.get("task_id", "") or "") if isinstance(body, dict) else ""
        adapter_id = str(body.get("adapter_id", "") or "") if isinstance(body, dict) else ""
        with self.webui._qr_bind_lock:
            entry = self.webui._qr_bind_tasks.get(task_id)
        if entry is None:
            self.api_error(404, "connections.qr_task_not_found")
            return
        try:
            result = qqofficial_bind.poll_bind_task_sync(task_id, entry["bind_key"])
        except Exception as exc:  # noqa: BLE001
            self.api_error(502, "connections.qr_poll_failed", error=exc)
            return
        status = str(result.get("status") or "pending")
        if status != "created" or not adapter_id:
            self._send_json({"code": 200, "data": {"status": status, "message": result.get("message", "")}})
            return

        connections = self.require_connections()
        if connections is None:
            return
        try:
            # 扫码成功即强制启用并 reload 建连
            connections.update(adapter_id, {
                "app_id": str(result.get("appid") or ""),
                "app_secret": str(result.get("secret") or ""),
                "enabled": True,
            })
        except Exception as exc:  # noqa: BLE001
            self.api_error(500, "connections.qr_save_failed", error=exc)
            return
        try:
            self.plugin.reload_onebot_connection()
        except Exception:  # noqa: BLE001
            pass
        with self.webui._qr_bind_lock:
            self.webui._qr_bind_tasks.pop(task_id, None)
        # Secret 不回传前端，仅回执长度
        self._send_json({"code": 200, "data": {
            "status": "created",
            "appid": result.get("appid"),
            "secret_len": len(str(result.get("secret") or "")),
        }})
