#!/usr/bin/env python3
"""群绑定密钥功能回归：签发（仅一次/5 分钟过期/新钥焚旧钥）、指令消费
（@ 与不@、多语言指令、大小写）、绑定写入（群/管理员去重）、HTTP API 与前端装配。

运行：python3 tests/test_group_bind.py
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))  # 复用 test_webui_http 的桩

PASSED = 0
FAILED = 0


def check(name: str, ok: bool) -> None:
    global PASSED, FAILED
    tag = "PASS" if ok else "FAIL"
    if ok:
        PASSED += 1
    else:
        FAILED += 1
    print(f"[{tag}] {name}")
    if not ok:
        # 失败必须抛错：pytest 收集 test_ 函数时得到真实 FAIL
        raise AssertionError(name)


# ---------------------------------------------------------------- fakes
class FakeLogger:
    def info(self, *a): pass
    def warning(self, *a): pass
    def error(self, *a): pass
    def debug(self, *a): pass
    def exception(self, *a): pass


class RecordingReply:
    """dispatcher 回执替身：记录 (消息, 是否引用回复)。

    注意：dispatcher._build_reply 的第二个参数是 quote（引用原消息），
    不是错误标志——成功回执同样会传 True。成功/失败判定看数据写入副作用。
    """

    def __init__(self) -> None:
        self.messages: list[tuple[str, bool]] = []

    def __call__(self, msg: str, quote: bool = False) -> None:  # noqa: FBT001
        self.messages.append((str(msg), bool(quote)))

    @property
    def text(self) -> str:
        return "\n".join(m for m, _ in self.messages)


class BindFixture:
    """模块级测试脚手架：真实 EventBus + ConnectionManager + GroupBindModule。"""

    def __init__(self, tmp: Path) -> None:
        from endstone_lumenbridge.connections import ConnectionManager
        from endstone_lumenbridge.event_bus import EventBus
        from endstone_lumenbridge.modules.group_bind import GroupBindModule

        class FakePlugin:
            logger = FakeLogger()
            _tee_logger = None

            def __init__(self) -> None:
                self.bus = EventBus(FakeLogger())
                self.connections = ConnectionManager(tmp, FakeLogger())
                self.reload_calls = 0

            def reload_onebot_connection(self) -> None:
                self.reload_calls += 1

        self.plugin = FakePlugin()
        self.module = GroupBindModule(self.plugin)
        ws = self.plugin.connections.adapters_view()[0]
        self.adapter_id = str(ws["id"])

    def pack(self, text: str, *, group=111, user=10001, cq_at=False) -> dict:
        """构造群消息包：默认走 message 段列表；cq_at=True 走 raw_message + CQ 码。"""
        base = {
            "_lumen_adapter_id": self.adapter_id,
            "group_id": group,
            "user_id": user,
            "sender": {"user_id": user},
        }
        if cq_at:
            return {**base, "message": None, "raw_message": f"[CQ:at,qq=22233]{text}"}
        return {
            **base,
            "message": [{"type": "text", "data": {"text": text}}],
            "raw_message": text,
        }

    def feed(self, text: str, **kw) -> RecordingReply:
        reply = RecordingReply()
        self.module._on_group_message(self.pack(text, **kw), reply)
        return reply

    def bound(self, *, group=None, user=None) -> bool:
        """判定指定群/管理员是否已写入当前适配器（宽松 str 比较，数字/openid 通用）。"""
        adapter = self.plugin.connections.get(self.adapter_id)
        if adapter is None:
            return False
        groups = [str(g) for g in adapter["main_group"]]
        admins = [str(a) for a in adapter["admin_qq"]]
        return (group is None or str(group) in groups) and (user is None or str(user) in admins)


# ---------------------------------------------------------------- 1. 签发
def test_issue_key_structure(tmp: Path) -> None:
    fix = BindFixture(tmp)
    info = fix.module.issue_key(fix.adapter_id)
    assert info is not None
    from endstone_lumenbridge.i18n import t as _t

    word = _t("groupbind.command_word")
    check("签发：返回「指令 密钥」格式", info["command"].startswith(word + " "))
    key = info["command"][len(word) + 1:]
    check("签发：密钥为 12 位十六进制", len(key) == 12 and all(c in "0123456789abcdef" for c in key))
    check("签发：key 字段与指令一致", info["key"] == key)
    check("签发：有效期 300 秒", info["expires_in"] == 300)
    check("签发：不存在的适配器返回 None", fix.module.issue_key("no_such") is None)
    # 两次签发密钥不同（随机熵）
    again = fix.module.issue_key(fix.adapter_id)
    assert again is not None
    check("签发：两次签发密钥不同", again["key"] != key)


def test_new_key_burns_old(tmp: Path) -> None:
    fix = BindFixture(tmp)
    first = fix.module.issue_key(fix.adapter_id)
    assert first is not None
    second = fix.module.issue_key(fix.adapter_id)
    assert second is not None
    # 旧钥立刻销毁：用 first 的指令绑定应提示无效（1 条回执 + 无写入）
    reply = fix.feed(first["command"])
    check(
        "新钥焚旧钥：旧密钥绑定被拒绝",
        len(reply.messages) == 1 and not fix.bound(group=111, user=10001),
    )
    # 新钥仍可正常绑定（旧钥误用不连带焚毁新钥）
    ok = fix.feed(second["command"])
    check(
        "新钥焚旧钥：新密钥可正常绑定",
        len(ok.messages) == 1 and fix.bound(group=111, user=10001),
    )


# ---------------------------------------------------------------- 2. 指令消费
def test_command_variants(tmp: Path) -> None:
    fix = BindFixture(tmp)

    def fresh_key() -> str:
        info = fix.module.issue_key(fix.adapter_id)
        assert info is not None
        return info["key"]

    # 中文指令、@ 机器人（message 段列表，at 段天然分离）
    reply = fix.feed(f"/lumen绑定 {fresh_key()}", cq_at=True)
    check("指令：@ 机器人 + raw_message CQ 码可绑定", fix.bound(group=111, user=10001))
    # 英文别名 + 大小写不敏感
    reply = fix.feed(f"/LUMEN BIND {fresh_key()}", group=222, user=10002)
    check("指令：英文别名大小写不敏感", fix.bound(group=222, user=10002))
    # 繁体指令
    reply = fix.feed(f"/lumen綁定 {fresh_key()}", group=333, user=10003)
    check("指令：繁体指令变体可绑定", fix.bound(group=333, user=10003))
    # 缺密钥 → 格式提示（1 条回执，无写入）
    reply = fix.feed("/lumen绑定")
    check("指令：缺密钥返回格式提示", len(reply.messages) == 1 and not fix.bound(group=444))
    # 错误密钥 → 无效提示
    reply = fix.feed("/lumen绑定 deadbeef1234")
    check("指令：错误密钥返回无效提示", len(reply.messages) == 1 and not fix.bound(group=444))
    # 普通聊天消息不触发（无回执）
    reply = fix.feed("今天天气不错")
    check("指令：普通聊天零回执", len(reply.messages) == 0)
    reply = fix.feed("/help me")
    check("指令：其他斜杠命令零回执", len(reply.messages) == 0)

    adapter = fix.plugin.connections.get(fix.adapter_id)
    assert adapter is not None
    groups = [str(g) for g in adapter["main_group"]]
    check("指令：多群依次写入", groups == ["111", "222", "333"])


def test_key_expiry_and_one_shot(tmp: Path) -> None:
    fix = BindFixture(tmp)
    info = fix.module.issue_key(fix.adapter_id)
    assert info is not None

    # 惰性过期：把到期时刻拨到过去，密钥应被判过期销毁
    with fix.module._lock:
        fix.module._keys[fix.adapter_id]["expires_at"] = time.monotonic() - 1
    reply = fix.feed(info["command"])
    check(
        "过期：过期密钥绑定被拒绝",
        len(reply.messages) == 1 and not fix.bound(group=111, user=10001),
    )
    with fix.module._lock:
        check("过期：过期条目已清理", fix.adapter_id not in fix.module._keys)

    # 一次性消费：绑定成功后密钥销毁，同钥二次绑定失败
    info = fix.module.issue_key(fix.adapter_id)
    assert info is not None
    first = fix.feed(info["command"])
    check(
        "一次性：首次绑定成功",
        len(first.messages) == 1 and fix.bound(group=111, user=10001),
    )
    second = fix.feed(info["command"], group=444, user=10004)
    check(
        "一次性：同密钥二次绑定被拒绝",
        len(second.messages) == 1 and not fix.bound(group=444),
    )


def test_extract_bind_text() -> None:
    from endstone_lumenbridge.modules.group_bind import extract_bind_text

    seg = extract_bind_text({
        "message": [
            {"type": "at", "data": {"qq": "10086"}},
            {"type": "text", "data": {"text": " /lumen绑定 abc123"}},
        ],
    })
    check("提取：message 段列表剥离 @ 段", seg == "/lumen绑定 abc123")
    raw = extract_bind_text({"raw_message": "[CQ:at,qq=10086]  /lumen绑定 abc123"})
    check("提取：raw_message 剥离 CQ @ 码", raw == "/lumen绑定 abc123")
    both = extract_bind_text({
        "message": [{"type": "text", "data": {"text": "/lumen绑定 abc123"}}],
        "raw_message": "[CQ:at,qq=10086]/lumen绑定 abc123",
    })
    check("提取：优先 message 段列表", both == "/lumen绑定 abc123")


# ---------------------------------------------------------------- 3. 绑定写入
def test_bind_dedup_and_official_openid(tmp: Path) -> None:
    fix = BindFixture(tmp)

    def bind(group, user):
        info = fix.module.issue_key(fix.adapter_id)
        assert info is not None
        return fix.feed(info["command"], group=group, user=user)

    # 群一（管理员 10001）→ 群二（同一管理员）：管理员不重复
    bind(111, 10001)
    reply = bind(222, 10001)
    adapter = fix.plugin.connections.get(fix.adapter_id)
    assert adapter is not None
    admins = [str(a) for a in adapter["admin_qq"]]
    groups = [str(g) for g in adapter["main_group"]]
    check("绑定：第二次绑定成功回执", len(reply.messages) == 1 and groups == ["111", "222"])
    check("绑定：多群管理员不重复", admins.count("10001") == 1)
    check("绑定：两个群均已写入", groups == ["111", "222"])

    # 重复绑定同群同管理员 → already 回执且不重复写入
    reply = bind(111, 10001)
    check("绑定：重复绑定返回已绑定回执", len(reply.messages) == 1)
    adapter = fix.plugin.connections.get(fix.adapter_id)
    assert adapter is not None
    check(
        "绑定：重复绑定不产生重复条目",
        [str(g) for g in adapter["main_group"]] == ["111", "222"]
        and [str(a) for a in adapter["admin_qq"]] == ["10001"],
    )
    check("绑定：成功后触发连接重载", fix.plugin.reload_calls >= 2)

    # QQ 官方域：openid 字符串同样适用（宽松解析 + 字符集校验）
    official = fix.plugin.connections.create({"type": "qqofficial"})
    fix.adapter_id = str(official["id"])
    info = fix.module.issue_key(fix.adapter_id)
    assert info is not None
    reply = fix.feed(info["command"], group="GROUP_OPENID_1", user="USER_OPENID_1")
    check(
        "绑定：官方域 openid 可绑定",
        len(reply.messages) == 1 and fix.bound(group="GROUP_OPENID_1", user="USER_OPENID_1"),
    )


# ---------------------------------------------------------------- 4. HTTP API
def test_bindkey_http_api(tmp: Path) -> None:
    import tempfile as _tf
    from urllib.error import HTTPError
    from urllib.request import Request, urlopen

    from endstone_lumenbridge.modules.group_bind import GroupBindModule
    from endstone_lumenbridge.webui.logbuffer import LogBuffer
    from endstone_lumenbridge.webui.server import WebUIServer
    from test_webui_http import DummyPlugin

    data_folder = tmp / "http"
    data_folder.mkdir(parents=True)
    # DummyPlugin 期望 data_folder 为其内部目录的父目录
    plugin = DummyPlugin(data_folder)
    plugin.group_bind_module = GroupBindModule(plugin)
    plugin.log_buffer = LogBuffer()
    plugin.webui = WebUIServer(plugin)
    plugin.webui.start()
    assert plugin.webui._httpd is not None
    port = plugin.webui._httpd.server_address[1]
    base = f"http://127.0.0.1:{port}"

    def request(method, path, body=None, token=""):
        headers = {}
        payload = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            payload = json.dumps(body).encode("utf-8")
        if token:
            headers["Authorization"] = "Bearer " + token
        req = Request(base + path, data=payload, headers=headers, method=method)
        try:
            with urlopen(req, timeout=3) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    try:
        # 未登录 → 401
        status, data = request("POST", "/api/connections/bindkey", {"id": "x"})
        check("API：未鉴权请求被拒绝", status == 401)

        _s, login = request("POST", "/api/auth/login", {"password": "test-password"})
        token = login["data"]["token"]

        ws = plugin.connections.adapters_view()[0]
        adapter_id = str(ws["id"])
        # 正常签发
        status, data = request("POST", "/api/connections/bindkey", {"id": adapter_id}, token)
        check("API：签发返回 200", status == 200 and data["code"] == 200)
        info = data["data"]
        check("API：响应含绑定指令", isinstance(info.get("command"), str) and " " in info["command"])
        check("API：响应含有效期", info.get("expires_in") == 300)
        # 不存在的适配器 → 404
        status, data = request("POST", "/api/connections/bindkey", {"id": "ghost"}, token)
        check("API：不存在适配器返回 404", status == 404 and data["code"] == 404)
        # 保留字不被 <id> 路由吞掉：issue 后立即用返回的密钥在模块层验证有效性
        status, data = request("POST", "/api/connections/bindkey", {"id": adapter_id}, token)
        check("API：重复签发仍走字面量路由", status == 200 and data["code"] == 200)
        key = data["data"]["key"]
        with plugin.group_bind_module._lock:
            alive = plugin.group_bind_module._keys.get(adapter_id)
        check("API：签发密钥进入模块密钥表", alive is not None and alive["key"] == key)
    finally:
        plugin.webui.stop()


# ---------------------------------------------------------------- 5. 前端装配
def test_frontend_wiring() -> None:
    static = Path(__file__).resolve().parent.parent / "src" / "endstone_lumenbridge" / "webui" / "static"
    html = (static / "index.html").read_text(encoding="utf-8")
    js = (static / "app.js").read_text(encoding="utf-8")
    css = (static / "app.css").read_text(encoding="utf-8")

    for needle in (
        'id="bindkey-modal"', 'id="bindkey-command"', 'id="bindkey-copy-btn"',
        'id="bindkey-countdown-bar"', 'id="bindkey-countdown-text"', "bindkey-warn",
        "data-i18n=\"connections.bindkey_title\"", "data-i18n=\"connections.bindkey_intro\"",
    ):
        check(f"HTML：包含 {needle}", needle in html)

    for fn in ("openBindKeyModal", "closeBindKeyModal", "copyBindKeyCommand",
               "startBindKeyCountdown", "markBindKeyExpired", "resetBindKeyUi"):
        check(f"JS：定义 {fn}", f"function {fn}(" in js)
    check("JS：调用 bindkey API", '"/api/connections/bindkey"' in js)
    check("JS：身份设置区含签发入口", "bindkey-launch-btn" in js and "openBindKeyModal('" in js)
    check("JS：过期态模糊处理", "expired" in js)

    for cls in (".bindkey-modal", ".bindkey-launch-btn", ".bindkey-cmd-card",
                ".bindkey-once-badge", ".bindkey-cmd-value", ".bindkey-copy-btn",
                ".bindkey-countdown", ".bindkey-warn", ".bindkey-cmd-card.expired"):
        check(f"CSS：包含 {cls}", cls in css)


def test_i18n_keys_complete() -> None:
    locales = Path(__file__).resolve().parent.parent / "src" / "endstone_lumenbridge" / "locales"
    conn_keys = (
        "bindkey_button", "bindkey_button_hint", "bindkey_title", "bindkey_intro",
        "bindkey_command_label", "bindkey_copy", "bindkey_copied", "bindkey_copy_failed",
        "bindkey_expires_suffix", "bindkey_expired", "bindkey_once_badge", "bindkey_warn",
        "bindkey_generate_failed",
    )
    groupbind_keys = ("command_word",)
    for lang in ("zh_CN", "en", "zh_TW"):
        data = json.loads((locales / f"{lang}.json").read_text(encoding="utf-8"))
        conn = data.get("connections", {})
        missing = [k for k in conn_keys if not isinstance(conn.get(k), str) or not conn[k]]
        check(f"i18n：{lang} connections.bindkey_* 完整", not missing)
        gb = data.get("groupbind", {})
        missing_gb = [k for k in groupbind_keys if not isinstance(gb.get(k), str) or not gb[k]]
        reply = gb.get("reply", {})
        log = gb.get("log", {})
        for sub in ("bind_success", "already_bound", "key_invalid", "format_hint",
                    "bind_failed", "missing_identity"):
            if not isinstance(reply.get(sub), str):
                missing_gb.append(f"reply.{sub}")
        for sub in ("key_issued", "bind_success", "bind_failed", "reload_failed"):
            if not isinstance(log.get(sub), str):
                missing_gb.append(f"log.{sub}")
        check(f"i18n：{lang} groupbind.* 完整", not missing_gb)


# ---------------------------------------------------------------- 入口
if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as td:
        tmpdir = Path(td)
        test_issue_key_structure(tmpdir / "a")
        test_new_key_burns_old(tmpdir / "b")
        test_command_variants(tmpdir / "c")
        test_key_expiry_and_one_shot(tmpdir / "d")
        test_bind_dedup_and_official_openid(tmpdir / "e")
        test_bindkey_http_api(tmpdir / "f")
    test_frontend_wiring()
    test_i18n_keys_complete()
    print(f"\n{PASSED} passed, {FAILED} failed")
    sys.exit(1 if FAILED else 0)
