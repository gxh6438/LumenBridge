#!/usr/bin/env python3
"""群内 @ 添加管理员命令回归：权限校验（非管理员拒绝）、@ 段解析
（段列表 / CQ 码兜底 / 排除 all 与机器人自身）、写入去重、命令变体。

运行：python3 tests/test_admin_command.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

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
        raise AssertionError(name)


# ---------------------------------------------------------------- fakes
class FakeLogger:
    def info(self, *a): pass
    def warning(self, *a): pass
    def error(self, *a): pass
    def debug(self, *a): pass
    def exception(self, *a): pass


class RecordingReply:
    """dispatcher 回执替身：记录 (消息, 是否引用回复)。"""

    def __init__(self) -> None:
        self.messages: list[tuple[str, bool]] = []

    def __call__(self, msg: str, quote: bool = False) -> None:  # noqa: FBT001
        self.messages.append((str(msg), bool(quote)))


class AdminFixture:
    """模块级测试脚手架：真实 EventBus + ConnectionManager + AdminCommandModule。"""

    def __init__(self, tmp: Path) -> None:
        from endstone_lumenbridge.connections import ConnectionManager
        from endstone_lumenbridge.event_bus import EventBus
        from endstone_lumenbridge.modules.admin_command import AdminCommandModule

        class FakePlugin:
            logger = FakeLogger()
            _tee_logger = None

            def __init__(self) -> None:
                self.bus = EventBus(FakeLogger())
                self.connections = ConnectionManager(tmp, FakeLogger())

        self.plugin = FakePlugin()
        self.module = AdminCommandModule(self.plugin)
        adapter = self.plugin.connections.adapters_view()[0]
        self.adapter_id = str(adapter["id"])
        # 预置发送者为管理员（默认适配器）
        self.plugin.connections.update(self.adapter_id, {"admin_qq": [10001]})

    def pack(
        self,
        text: str,
        *,
        user=10001,
        at_ids: list | None = None,
        cq_raw: str | None = None,
        self_id=88888,
    ) -> dict:
        """构造群消息包：at_ids 走 message 段列表；cq_raw 走 raw_message CQ 码。"""
        base = {
            "_lumen_adapter_id": self.adapter_id,
            "group_id": 111,
            "user_id": user,
            "sender": {"user_id": user},
            "self_id": self_id,
        }
        if cq_raw is not None:
            return {**base, "message": None, "raw_message": cq_raw}
        segments: list[dict] = []
        if text:
            segments.append({"type": "text", "data": {"text": text}})
        for qq in at_ids or []:
            segments.append({"type": "at", "data": {"qq": qq}})
        return {**base, "message": segments, "raw_message": text}

    def feed(self, text: str, **kw) -> RecordingReply:
        reply = RecordingReply()
        self.module._on_group_message(self.pack(text, **kw), reply)
        return reply

    def admins(self) -> list[str]:
        adapter = self.plugin.connections.get(self.adapter_id)
        assert adapter is not None
        return [str(a) for a in adapter["admin_qq"]]


# ---------------------------------------------------------------- 1. @ 段解析
def test_extract_at_ids() -> None:
    from endstone_lumenbridge.modules.admin_command import extract_at_ids

    pack = {
        "self_id": 88888,
        "message": [
            {"type": "text", "data": {"text": "/添加管理员"}},
            {"type": "at", "data": {"qq": 111}},
            {"type": "at", "data": {"qq": "222"}},
            {"type": "at", "data": {"qq": 111}},      # 重复 → 去重
            {"type": "at", "data": {"qq": "all"}},     # 全体成员 → 排除
            {"type": "at", "data": {"qq": 88888}},     # 机器人自身 → 排除
            {"type": "at", "data": {"qq": ""}},        # 空值 → 排除
        ],
    }
    check("解析：段列表提取 at 并去重/排除 all 与自身", extract_at_ids(pack) == ["111", "222"])

    pack_raw = {
        "self_id": 88888,
        "message": None,
        "raw_message": "/添加管理员 [CQ:at,qq=333] [CQ:at,qq=all] [CQ:at,qq=88888] [CQ:at,qq=333]",
    }
    check("解析：raw_message CQ 码兜底", extract_at_ids(pack_raw) == ["333"])

    check("解析：无 at 段返回空", extract_at_ids({"message": [], "raw_message": "hello"}) == [])


# ---------------------------------------------------------------- 2. 权限与写入
def test_permission_gate(tmp: Path) -> None:
    fix = AdminFixture(tmp)
    # 非管理员（10002 不在 admin_qq）→ 拒绝，无写入
    reply = fix.feed("/添加管理员", user=10002, at_ids=[10003])
    check("权限：非管理员被拒绝（1 条回执）", len(reply.messages) == 1)
    check("权限：拒绝后无写入", fix.admins() == ["10001"])

    # 普通消息与其它斜杠命令零回执
    check("权限：普通聊天零回执", len(fix.feed("今天天气不错").messages) == 0)
    check("权限：其它斜杠命令零回执", len(fix.feed("/help").messages) == 0)


def test_add_admin_success(tmp: Path) -> None:
    fix = AdminFixture(tmp)
    reply = fix.feed("/添加管理员", at_ids=[10002, 10003])
    check("添加：管理员 @ 多人成功回执", len(reply.messages) == 1)
    check("添加：两位新管理员均已写入", fix.admins() == ["10001", "10002", "10003"])

    # 部分/全部已存在 → already 回执，不重复写入
    reply = fix.feed("/添加管理员", at_ids=[10002, 10004])
    check("添加：部分新增仅追加缺失项", fix.admins() == ["10001", "10002", "10003", "10004"])
    reply = fix.feed("/添加管理员", at_ids=[10002, 10003])
    check("添加：全部已存在返回 already 回执", len(reply.messages) == 1)
    check("添加：已存在不重复写入", fix.admins() == ["10001", "10002", "10003", "10004"])

    # 无 @ 段 → 格式提示
    reply = fix.feed("/添加管理员")
    check("添加：缺 @ 返回格式提示", len(reply.messages) == 1 and fix.admins().count("10001") == 1)


def test_command_variants_and_official(tmp: Path) -> None:
    fix = AdminFixture(tmp)
    # 繁体 / 英文（大小写不敏感）变体
    check("变体：繁体命令可用", len(fix.feed("/添加管理員", at_ids=[10002]).messages) == 1)
    check("变体：繁体命令写入生效", "10002" in fix.admins())
    check("变体：英文命令大小写不敏感", len(fix.feed("/AddAdmin", at_ids=[10003]).messages) == 1)
    check("变体：英文命令写入生效", "10003" in fix.admins())

    # raw_message CQ 码路径（个人号域 @ 提及）
    reply = fix.feed("", cq_raw="/添加管理员 [CQ:at,qq=10005] [CQ:at,qq=10006]")
    check("CQ 码：raw_message 路径可添加", len(reply.messages) == 1)
    check("CQ 码：CQ 路径写入生效", {"10005", "10006"} <= set(fix.admins()))

    # QQ 官方域：openid 字符串
    official = fix.plugin.connections.create({"type": "qqofficial"})
    fix.adapter_id = str(official["id"])
    fix.plugin.connections.update(fix.adapter_id, {"admin_qq": ["USER_OPENID_A"]})
    reply = fix.feed(
        "/添加管理员",
        user="USER_OPENID_A",
        at_ids=["USER_OPENID_B", "USER_OPENID_C"],
        self_id="APP_ID",
    )
    check("官方域：openid 管理员可 @ 添加", len(reply.messages) == 1)
    adapter = fix.plugin.connections.get(fix.adapter_id)
    assert adapter is not None
    check(
        "官方域：openid 写入生效",
        [str(a) for a in adapter["admin_qq"]] == ["USER_OPENID_A", "USER_OPENID_B", "USER_OPENID_C"],
    )


# ---------------------------------------------------------------- 3. i18n
def test_i18n_keys_complete() -> None:
    from endstone_lumenbridge.i18n import get_i18n

    i18n = get_i18n()
    for lang in ("zh_CN", "en", "zh_TW"):
        i18n.set_language(lang)
        for key in (
            "admincmd.command_word",
            "admincmd.log.added",
            "admincmd.log.denied",
            "admincmd.reply.permission_denied",
            "admincmd.reply.format_hint",
            "admincmd.reply.add_success",
            "admincmd.reply.already_admin",
            "admincmd.reply.add_failed",
        ):
            text = i18n.t(key)
            check(f"i18n：{lang} {key} 非空且非键名回显", text and text != key)
    i18n.set_language("zh_CN")


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as td:
        tmpdir = Path(td)
        test_extract_at_ids()
        test_permission_gate(tmpdir / "a")
        test_add_admin_success(tmpdir / "b")
        test_command_variants_and_official(tmpdir / "c")
    test_i18n_keys_complete()
    print(f"\n{PASSED} passed, {FAILED} failed")
    sys.exit(1 if FAILED else 0)
