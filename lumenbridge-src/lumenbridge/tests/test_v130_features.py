#!/usr/bin/env python3
"""v1.3.0 功能回归：/get openid 命令、密钥等长掩码与二次查看、默认卡片调整、扫码绑定 AES 解密。

运行：python3 tests/test_v130_features.py
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))  # 复用 test_webui 的 Fake 桩

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
        # 失败必须抛错：pytest 收集 test_ 函数时得到真实 FAIL（结构性假绿修复）
        raise AssertionError(name)


# ---------------------------------------------------------------- fakes
class FakeLogger:
    def info(self, *a): pass
    def warning(self, *a): pass
    def error(self, *a): pass


class FakeBus:
    def __init__(self): self.handlers = {}
    def on(self, event, fn): self.handlers[event] = fn
    def emit(self, *a): pass


class FakeConfigManager:
    def __init__(self):
        self.regex_engine = {"enable": True}


class RecordingAdapter:
    def __init__(self):
        self.sent: list[tuple[str, str]] = []
    def send_group_msg(self, group, msg):
        self.sent.append((str(group), str(msg)))


class FakePluginLite:
    """RegexEngineModule 最小依赖桩。"""

    def __init__(self, tmp: Path):
        self.logger = FakeLogger()
        self._tee_logger = None
        self.data_folder = str(tmp)
        self.config_manager = FakeConfigManager()
        self.bus = FakeBus()
        self.adapter = RecordingAdapter()


# ---------------------------------------------------------------- 1. /get openid
def test_get_openid(tmp: Path) -> None:
    from endstone_lumenbridge.modules.regex_engine import RegexEngineModule

    plugin = FakePluginLite(tmp)
    eng = RegexEngineModule(plugin)
    ad = plugin.adapter
    base_pack = {
        "group_id": "G100",
        "user_id": "U777",
        "self_id": "APP1",
        "domain": "official",
        "message": [],
    }

    def run(text, pack=None):
        ad.sent.clear()
        eng._handle_get_openid(pack or base_pack, text)

    # 无参数：返回本群与发送者 ID
    run("/get openid")
    body = "\n".join(m for _, m in ad.sent)
    check("openid: 无参数返回群与自己的 ID", "G100" in body and "U777" in body)

    # @成员：官方域返回 openid（跳过 @机器人 自身的 AppID）
    run("/get openid", {**base_pack, "message": [
        {"type": "at", "data": {"qq": "APP1"}},
        {"type": "at", "data": {"qq": "OPENID_ABC"}},
    ]})
    check("openid: @成员返回成员 openid", any("OPENID_ABC" in m for _, m in ad.sent))

    # 个人号域：@成员返回 QQ 号
    run("/get openid", {**base_pack, "domain": "qq", "message": [
        {"type": "at", "data": {"qq": "123456"}},
    ]})
    check("openid: 个人号域 @成员返回 QQ 号", any("123456" in m for _, m in ad.sent))

    # QQ 号参数：官方域提示不支持，个人号域即用户 ID
    run("/get openid 123456", {**base_pack})
    check("openid: 官方域 QQ 号查询提示不支持", any("无法" in m or "不支持" in m or "不能" in m for _, m in ad.sent))
    run("/get openid 123456", {**base_pack, "domain": "qq"})
    check("openid: 个人号域 QQ 号即用户 ID", any("123456" in m for _, m in ad.sent))

    # 非法参数：使用说明
    run("/get openid @@x")
    check("openid: 非法参数返回用法", len(ad.sent) == 1)

    # 大小写与多空格
    ad.sent.clear()
    check("openid: 命令大小写不敏感", eng._handle_get_openid(base_pack, "/GET   OpenID"))
    check("openid: 大写命令触发回复", len(ad.sent) == 1)

    # 非本命令不拦截
    check("openid: 其他消息不拦截", not eng._handle_get_openid(base_pack, "/get ip"))


# ---------------------------------------------------------------- 2. 密钥掩码 / reveal
class FakeConnections:
    def __init__(self):
        self._store = {"a1": {"access_token": "TOKEN-abcdef", "app_secret": "SECRET-12345678"}}
    def get(self, adapter_id):
        entry = self._store.get(adapter_id)
        return dict(entry) if entry is not None else None


def test_mask_and_reveal(tmp: Path) -> None:
    from endstone_lumenbridge.connections import ConnectionManager
    from endstone_lumenbridge.webui import WebUIServer

    mgr = ConnectionManager(tmp, FakeLogger())
    check("connections: 默认仅两张卡片", len(mgr.adapters) == 2)
    check("connections: 默认卡片为 websocket + qqofficial",
          {a["type"] for a in mgr.adapters} == {"websocket", "qqofficial"})

    # 等长掩码
    aid = mgr.adapters[0]["id"]
    mgr.update(aid, {"access_token": "tk_1234567890"})
    snap = mgr.snapshot(mask=True)
    masked = next(a for a in snap if a["id"] == aid)["access_token"]
    check("connections: 掩码与原值等长", masked == "*" * len("tk_1234567890"))

    # is_masked / 掩码提交保留原值
    check("connections: is_masked 识别掩码", ConnectionManager.is_masked(masked))
    mgr.update(aid, {"access_token": masked})
    real = mgr.get(aid)["access_token"]
    check("connections: 提交掩码不覆盖原值", real == "tk_1234567890")
    snap_plain = mgr.snapshot(mask=False)
    check("connections: 明文快照可取回", any(a["access_token"] == "tk_1234567890" for a in snap_plain))

    # 手动创建 astrbot 仍可用（模板已独立于默认卡片；v1.3.1 起新建卡片默认启用）
    created = mgr.create({"type": "astrbot"})
    check("connections: 仍可手动添加 AstrBot", created["type"] == "astrbot" and created["enabled"])
    check("connections: 添加后共三张卡片", len(mgr.adapters) == 3)

    # 未配置的默认 AstrBot 卡片在下次 load 时被清理
    # （enabled: False 模拟旧版本遗留的未启用 astrbot_default）
    fresh = ConnectionManager(tmp, FakeLogger())
    fresh.create({"type": "astrbot", "id": "astrbot_default", "enabled": False})
    fresh.load()
    check("connections: 未启用的 astrbot_default 被撤下",
          all(a["id"] != "astrbot_default" for a in fresh.adapters))

    # /api/connections/reveal（HTTP 层）
    plugin = _make_webui_plugin(tmp)
    plugin.connections = FakeConnections()
    webui = WebUIServer(plugin)
    plugin.webui = webui
    webui.start()
    if not _wait_port_ready():
        raise RuntimeError(f"WebUIServer 启动超时（端口 {WEBUI_PORT} 未就绪）")
    token = _login()
    try:
        val = _post_json("/api/connections/reveal", {"id": "a1", "key": "app_secret"}, token)
        check("reveal: 返回 AppSecret 明文", val["data"]["value"] == "SECRET-12345678")
        bad = _post_json("/api/connections/reveal", {"id": "a1", "key": "password"}, token, expect_error=True)
        check("reveal: 非法字段被拒绝", bad == 400)
        missing = _post_json("/api/connections/reveal", {"id": "nope", "key": "app_secret"}, token, expect_error=True)
        check("reveal: 未知适配器返回 404", missing == 404)
    finally:
        webui.stop()


def _make_webui_plugin(tmp: Path):
    import importlib
    webui_test = importlib.import_module("test_webui")
    plugin = webui_test.FakePlugin(tmp)
    # 本文件专属端口（M42：每文件唯一端口，不与 test_webui 共享 18300/18310）
    plugin.config_manager.data["webui"]["port"] = WEBUI_PORT
    return plugin


WEBUI_PORT = 18302
BASE = f"http://127.0.0.1:{WEBUI_PORT}"


def _wait_port_ready(timeout: float = 5.0, interval: float = 0.05) -> bool:
    """轮询等待 WebUI 端口可连（替代固定 sleep 就绪等待，M42）。"""
    import socket
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", WEBUI_PORT), timeout=0.5):
                return True
        except OSError:
            time.sleep(interval)
    return False


def _login() -> str:
    payload = json.dumps({"password": "testpass"}).encode()
    r = urllib.request.Request(BASE + "/api/auth/login", data=payload, method="POST")
    r.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(r, timeout=5) as resp:
            return json.loads(resp.read().decode())["data"]["token"]
    except urllib.error.HTTPError as e:
        raise RuntimeError(e.read().decode())


def _post_json(path: str, body: dict, token: str, expect_error: bool = False):
    payload = json.dumps(body).encode()
    r = urllib.request.Request(BASE + path, data=payload, method="POST")
    r.add_header("Content-Type", "application/json")
    r.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(r, timeout=5) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        if expect_error:
            return e.code
        raise


def _get_json(path: str, token: str):
    r = urllib.request.Request(BASE + path)
    r.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(r, timeout=5) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return {"_http_error": e.code}


# ---------------------------------------------------------------- 4. overview 官方适配器回归
def test_overview_official_primary(tmp: Path) -> None:
    """QQ 官方适配器 __getattr__ 对未知属性返回桩函数，ws_type 必须防御性校验。"""
    from endstone_lumenbridge.onebot.qqofficial_adapter import QQOfficialAdapter
    from endstone_lumenbridge.webui import WebUIServer

    class OfficialPrimaryHub:
        is_connected = True
        mode_name = "QQ 官方机器人 (官方网关)"

        def primary(self):
            return QQOfficialAdapter(FakeLogger(), None)

        def status(self):
            return []

    plugin = _make_webui_plugin(tmp)
    plugin.adapter = OfficialPrimaryHub()
    webui = WebUIServer(plugin)
    plugin.webui = webui
    webui.start()
    if not _wait_port_ready():
        raise RuntimeError(f"WebUIServer 启动超时（端口 {WEBUI_PORT} 未就绪）")
    try:
        token = _login()
        res = _get_json("/api/overview", token)
        check("overview: 官方适配器作 primary 不再 500", res.get("code") == 200)
        check("overview: mode 对桩函数回退 -1", res.get("data", {}).get("mode") == -1)
    finally:
        webui.stop()


# ---------------------------------------------------------------- 3. 扫码绑定 AES-256-GCM
def test_qr_bind_crypto() -> None:
    from endstone_lumenbridge.onebot.qqofficial_bind import (
        aes256_gcm_decrypt, generate_bind_key, connect_url,
    )

    # NIST GCM 测试向量（AES-256）：key=0^32, IV=0^12, 空 AAD
    key = bytes(32)
    nonce = bytes(12)
    # 官方向量：明文 16 字节 0，密文 == 明文（CTR 与 AES-ECB 组合），tag 已知
    plain = bytes(16)
    ct = bytes.fromhex("cea7403d4d606b6e074ec5d3baf39d18")
    tag = bytes.fromhex("d0d1c8a799996bf0265b98b5d48ab919")
    out = aes256_gcm_decrypt(key, nonce, ct, tag)
    check("AES-GCM: NIST 向量解密正确", out == plain)

    # 错误 tag 必须被校验拒绝
    # （try/else 结构：check 失败抛出的 AssertionError 不会被 except 吞掉转假绿）
    try:
        aes256_gcm_decrypt(key, nonce, ct, bytes(16))
    except Exception:
        check("AES-GCM: 篡改 tag 被拒绝", True)
    else:
        check("AES-GCM: 篡改 tag 被拒绝", False)

    bk = generate_bind_key()
    check("bind_key: base64 32 字节", len(bk) >= 40 and isinstance(bk, str))
    url = connect_url("task123")
    check("connect_url: 生成扫码链接", "task123" in url and url.startswith("http"))


# ---------------------------------------------------------------- main
def main() -> None:
    # check 失败会抛 AssertionError（pytest 真实 FAIL 用）；手动运行时逐段吞掉，
    # 保持"跑完全部再汇总退出码"的原语义；tmp 目录用完即清（M42）
    tmp = Path(tempfile.mkdtemp())
    sections = [
        ("== 1. /get openid 命令 ==", lambda: test_get_openid(tmp)),
        ("== 2. 密钥等长掩码与二次查看 ==", lambda: test_mask_and_reveal(Path(tempfile.mkdtemp()))),
        ("== 3. 扫码绑定 AES-256-GCM ==", test_qr_bind_crypto),
        ("== 4. overview 官方适配器回归 ==", lambda: test_overview_official_primary(Path(tempfile.mkdtemp()))),
    ]
    try:
        for title, fn in sections:
            print(title)
            try:
                fn()
            except AssertionError:
                pass
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n===== v1.3.0 功能测试: {PASSED} 通过, {FAILED} 失败 =====")
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    main()
