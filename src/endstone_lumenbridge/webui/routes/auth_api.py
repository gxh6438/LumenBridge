"""登录认证 API。"""

from __future__ import annotations

import re

from .. import auth as auth_util
from .base import RouteMixin


class AuthApi(RouteMixin):

    def api_login(self, m: re.Match, query: dict[str, str]) -> None:
        body = self._read_body() or {}
        if not isinstance(body, dict):
            self.api_error(400, "webui.msg.invalid_body")
            return
        ip = str(self.client_address[0]) if self.client_address else ""
        ok, guard_msg = self.webui._login_check_allowed(ip)
        if not ok:
            self._send_json({"code": 429, "msg": guard_msg}, 429)
            return
        input_pw = str(body.get("password", ""))
        if not auth_util.verify_password(input_pw, self.webui.password):
            self.webui._login_record_result(ip, False)
            self.api_error(401, "webui.msg.password_error")
            return
        self.webui._login_record_result(ip, True)
        token = self.webui.auth_provider.issue_token()
        if (self.webui._password_from_config
                and not auth_util.is_hashed_password(self.webui.password)):
            self.webui._upgrade_password_storage(input_pw)
        self._send_json({"code": 200, "data": {"token": token}})
