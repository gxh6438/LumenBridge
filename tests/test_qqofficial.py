"""QQ 官方机器人适配器专项测试

覆盖 qqofficial 落地后的关键路径（不依赖真实网络 / endstone）：
1. 入站翻译：群消息（全量 GROUP_MESSAGE_CREATE / @ 消息）与 C2C → OneBot v11 包
   （domain="official"、self_id=app_id、openid 字符串标识、附件转媒体段）
2. 被动回复凭据：窗口内递增 msg_seq，过期返回 None
3. 出站富媒体提取：url 段 / 本地文件段 / 纯文本；_upload_media 请求体
4. notice 翻译：机器人入群 / 被移出 / 好友增删
5. connections：qqofficial 校验矩阵（openid 宽松 / 数字严格 / 掩码回填）
6. hub 双域路由：数字群号→非官方适配器，openid→官方适配器，专属群命中优先
7. 白名单双域存储：qq / official 分文件、跨域 xbox 唯一、解绑走对应域
8. get_group_list / get_group_info 本地兜底
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

SRC = Path(__file__).parent.parent / "src"
sys.path.insert(0, str(SRC))

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        PASSED.append(name)
        print(f"  [PASS] {name}")
    else:
        FAILED.append(name)
        print(f"  [FAIL] {name} {detail}")
        # pytest 收集 test_ 函数直接调用本 check：失败必须抛错才是真实 FAIL，
        # 不再"只打印不抛错"（结构性假绿修复）
        raise AssertionError(f"{name} {detail}".strip())


class FakeLogger:
    def __init__(self) -> None:
        self.logs: list[tuple[str, str]] = []

    def _log(self, level: str, msg: str) -> None:
        self.logs.append((level, str(msg)))

    def debug(self, msg: str, *a: Any) -> None:
        self._log("debug", msg)

    def info(self, msg: str, *a: Any) -> None:
        self._log("info", msg)

    def warning(self, msg: str, *a: Any) -> None:
        self._log("warning", msg)

    def error(self, msg: str, *a: Any) -> None:
        self._log("error", msg)

    def exception(self, msg: str, *a: Any) -> None:
        self._log("error", msg)


class FakeBus:
    def __init__(self) -> None:
        self.events: list[tuple[str, Any]] = []

    def emit(self, event: str, payload: Any = None) -> None:
        self.events.append((event, payload))


def make_adapter(**overrides: Any) -> Any:
    from endstone_lumenbridge.onebot.qqofficial_adapter import QQOfficialAdapter

    kwargs: dict[str, Any] = dict(
        app_id="102000001",
        app_secret="test-secret",
        groups=["GRPOPENID1"],
        adapter_id="qo_test",
        adapter_name="官方测试",
    )
    kwargs.update(overrides)
    return QQOfficialAdapter(FakeLogger(), FakeBus(), **kwargs)


def run_async(coro: Any) -> Any:
    return asyncio.new_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------- 入站翻译
def test_inbound_group_message() -> None:
    ad = make_adapter()
    data = {
        "id": "MSG1",
        "group_openid": "GRPOPENID1",
        "content": "<@!102000001> 你好服务器",
        "author": {"member_openid": "MEMBER1", "username": "Steve"},
    }
    run_async(ad._emit_group_message(data))
    packs = [p for e, p in ad.bus.events if e == "onebot.pack"]
    check("群消息生成 onebot.pack", len(packs) == 1)
    pack = packs[0]
    check(
        "群消息字段翻译",
        pack["message_type"] == "group"
        and pack["group_id"] == "GRPOPENID1"
        and pack["user_id"] == "MEMBER1"
        and pack["sender"]["nickname"] == "Steve",
    )
    check("domain 标记 official", pack.get("domain") == "official")
    check("self_id 为 app_id", str(pack.get("self_id")) == "102000001")
    check("@ 前缀被剥离", pack["raw_message"] == "你好服务器")
    check("适配器 id 注入", pack.get("_lumen_adapter_id") == "qo_test")
    # OneBot v11 群消息标准字段完整性（post_type/message_type/sub_type/message_id/
    # group_id/user_id/anonymous/message/raw_message/font/sender/time/self_id）
    required = ["post_type", "message_type", "sub_type", "message_id", "group_id",
                "user_id", "anonymous", "message", "raw_message", "font", "sender",
                "time", "self_id"]
    check("群消息 OneBot v11 标准字段齐全", all(k in pack for k in required)
          and pack["post_type"] == "message" and pack["sub_type"] == "normal"
          and pack["anonymous"] is None and isinstance(pack["message"], list))
    check(
        "被动凭据已入池",
        "GRPOPENID1" in ad._passive and ad._passive["GRPOPENID1"][0][0] == "MSG1",
    )


def test_inbound_c2c_message() -> None:
    ad = make_adapter()
    data = {
        "id": "MSG2",
        "content": "私聊你好",
        "author": {"user_openid": "USEROPEN1", "username": "Alex"},
    }
    run_async(ad._emit_c2c_message(data))
    packs = [p for e, p in ad.bus.events if e == "onebot.pack"]
    check("C2C 生成消息包", len(packs) == 1 and packs[0]["message_type"] == "private")
    check(
        "C2C openid 翻译",
        packs[0]["user_id"] == "USEROPEN1" and packs[0]["sub_type"] == "friend",
    )
    # OneBot v11 私聊消息标准字段（私聊无 anonymous：标准即无此字段）
    required = ["post_type", "message_type", "sub_type", "message_id", "user_id",
                "message", "raw_message", "font", "sender", "time", "self_id"]
    check("C2C OneBot v11 标准字段齐全", all(k in packs[0] for k in required))


def test_inbound_attachments() -> None:
    ad = make_adapter()
    data = {
        "id": "MSG3",
        "group_openid": "GRPOPENID1",
        "content": "看图",
        "author": {"member_openid": "MEMBER1"},
        "attachments": [
            {"content_type": "image/png", "filename": "shot.png", "url": "https://cdn.example.com/a.png"},
            {"content_type": "video/mp4", "filename": "clip.mp4"},
        ],
    }
    # 劫持 media 接口：视频缺 url 时应被调用换取
    calls: list[str] = []

    async def fake_api(method: str, path: str, body: dict | None = None) -> Any:
        calls.append(path)
        return {"file_info": "https://dl.example.com/video.mp4"}

    ad._api_request = fake_api  # type: ignore[method-assign]
    run_async(ad._emit_group_message(data))
    pack = [p for e, p in ad.bus.events if e == "onebot.pack"][0]
    segs = pack["message"]
    check("附件转 image 段", segs[1]["type"] == "image" and segs[1]["data"]["url"].startswith("https://cdn"))
    check("缺 url 附件经 media 接口补取", "/media" in calls[-1] and segs[2]["data"]["url"] == "https://dl.example.com/video.mp4")


def test_inbound_mention_position() -> None:
    """回归：@其他成员 的 at 段保持在原始位置（修复转发成"你好你好呀@xxx"）。"""
    ad = make_adapter()
    data = {
        "id": "MSG4",
        "group_openid": "GRPOPENID1",
        # 官方群消息固定以 <@!bot> 触发，成员 @ 在文本中间
        "content": "<@!102000001> 你好<@!MEMBER2>你好呀",
        "author": {"member_openid": "MEMBER1", "username": "Steve"},
    }
    run_async(ad._emit_group_message(data))
    pack = [p for e, p in ad.bus.events if e == "onebot.pack"][0]
    segs = pack["message"]
    check(
        "at 段保持原始位置",
        [s["type"] for s in segs] == ["text", "at", "text"]
        and segs[0]["data"]["text"] == "你好"
        and segs[1]["data"]["qq"] == "MEMBER2"
        and segs[2]["data"]["text"] == "你好呀",
        str(segs),
    )
    check(
        "bot 自身 @ 不产生 at 段",
        all(s["type"] != "at" or s["data"]["qq"] != "102000001" for s in segs),
    )
    check("raw_message 不含 @ 标记", pack["raw_message"] == "你好你好呀")

    # 表情标记转 [表情] 文本；@ 后间距保留
    data2 = {
        "id": "MSG5",
        "group_openid": "GRPOPENID1",
        "content": "<@!102000001>嗨 <@!MEMBER2> 早<faceType=1,id=4,ext=\"{}\">",
        "author": {"member_openid": "MEMBER1"},
    }
    run_async(ad._emit_group_message(data2))
    segs2 = [p for e, p in ad.bus.events if e == "onebot.pack"][-1]["message"]
    check(
        "@ 间距保留 / 表情转文本",
        [s["type"] for s in segs2] == ["text", "at", "text"]
        and segs2[0]["data"]["text"] == "嗨 "
        and segs2[2]["data"]["text"] == " 早[表情]",
        str(segs2),
    )


# ---------------------------------------------------------------- 被动凭据
def test_passive_seq() -> None:
    from endstone_lumenbridge.onebot.qqofficial_adapter import _PASSIVE_WINDOW_GROUP

    ad = make_adapter()
    ad._cache_passive("GRP1", "MSG_X", _PASSIVE_WINDOW_GROUP)
    first = ad._take_passive("GRP1")
    second = ad._take_passive("GRP1")
    check("被动凭据 msg_seq 递增", first == ("MSG_X", 1) and second == ("MSG_X", 2))
    ad._cache_passive("GRP2", "MSG_Y", -1.0)
    check("过期凭据返回 None", ad._take_passive("GRP2") is None)


def test_passive_pool() -> None:
    """凭据池（Gensokyo 懒池）：优先未用过的最新条目、5 次上限、池容量上限。"""
    from endstone_lumenbridge.onebot.qqofficial_adapter import (
        _PASSIVE_MAX_SEQ,
        _PASSIVE_POOL_MAX,
        _PASSIVE_WINDOW_GROUP,
    )

    ad = make_adapter()
    # 两条凭据入池：旧 msg_id 已用 1 次，新 msg_id 未用
    ad._cache_passive("GRP", "OLD", _PASSIVE_WINDOW_GROUP)
    ad._take_passive("GRP")  # OLD 消耗一次
    ad._cache_passive("GRP", "NEW", _PASSIVE_WINDOW_GROUP)
    taken = ad._take_passive("GRP")
    check("池优先取未用过的最新凭据", taken == ("NEW", 1), str(taken))
    # NEW 用尽 5 次后回落到仍有额度的 OLD
    for _ in range(_PASSIVE_MAX_SEQ - 1):
        ad._take_passive("GRP")
    fallback = ad._take_passive("GRP")
    check("最新凭据用尽后回落旧凭据", fallback == ("OLD", 2), str(fallback))
    # OLD 再用 3 次（共 5 次）后池内全部耗尽 → None
    for _ in range(3):
        ad._take_passive("GRP")
    check("池内凭据全部用尽返回 None", ad._take_passive("GRP") is None)
    # 同一 msg_id 重复入池去重
    ad._cache_passive("DEDUP", "M1", _PASSIVE_WINDOW_GROUP)
    ad._cache_passive("DEDUP", "M1", _PASSIVE_WINDOW_GROUP)
    check("同一 msg_id 入池去重", len(ad._passive["DEDUP"]) == 1)
    # 池容量上限
    for i in range(_PASSIVE_POOL_MAX + 3):
        ad._cache_passive("CAP", f"M{i}", _PASSIVE_WINDOW_GROUP)
    check("凭据池容量上限", len(ad._passive["CAP"]) == _PASSIVE_POOL_MAX)


# ---------------------------------------------------------------- event_id 凭据
def test_event_id_credential() -> None:
    """入群 event_id：缓存→复用→过期。"""
    from endstone_lumenbridge.onebot.qqofficial_adapter import _EVENT_ID_WINDOW

    ad = make_adapter()
    ad._emit_robot_added({
        "group_openid": "GRPOPENID1",
        "op_member_openid": "ADMIN1",
        "event_id": "EVT_JOIN_1",
    })
    check("入群 event_id 已缓存", ad._take_event_id("GRPOPENID1") == "EVT_JOIN_1")
    check("event_id 窗口内可复用", ad._take_event_id("GRPOPENID1") == "EVT_JOIN_1")
    check("无 event_id 的目标返回空串", ad._take_event_id("NOPE") == "")
    ad._event_ids["GRPOPENID1"] = ("EVT_JOIN_1", time.time() + _EVENT_ID_WINDOW - 1e9)
    check("过期 event_id 返回空串", ad._take_event_id("GRPOPENID1") == "")


# ---------------------------------------------------------------- 发送可靠性
def test_biz_code() -> None:
    from endstone_lumenbridge.onebot.qqofficial_adapter import ApiHTTPError, _biz_code

    check("解析业务码 22009", _biz_code(ApiHTTPError(400, '{"code": 22009, "message": "x"}')) == 22009)
    check("解析业务码 40034025", _biz_code(ApiHTTPError(400, '{"code":40034025}')) == 40034025)
    check("无业务码返回 0", _biz_code(ApiHTTPError(500, "oops")) == 0)


def test_post_message_matrix() -> None:
    """重试矩阵：超时重试递增 msg_seq / 22009 直接入栈 / 40034025 清 event_id 重发。"""
    import endstone_lumenbridge.onebot.qqofficial_adapter as qm

    old_text, old_media = qm._SEND_RETRY_DELAY_TEXT, qm._SEND_RETRY_DELAY_MEDIA
    qm._SEND_RETRY_DELAY_TEXT = qm._SEND_RETRY_DELAY_MEDIA = 0.01
    try:
        # 1) 超时重试：前两次网络异常，第三次成功；msg_seq 随重试递增
        ad = make_adapter()
        calls: list[dict[str, Any]] = []

        async def flaky(method: str, path: str, body: dict[str, Any] | None = None) -> Any:
            calls.append({"path": path, "body": dict(body or {})})
            if len(calls) < 3:
                raise TimeoutError("gateway timeout")
            return {}

        ad._api_request = flaky  # type: ignore[assignment]
        result = run_async(ad._post_message("group", "GRP1", {"content": "hi", "msg_seq": 1, "msg_id": "M"}, False))
        check("超时重试后成功", result == "ok" and len(calls) == 3)
        check("重试时 msg_seq 递增", calls[0]["body"]["msg_seq"] == 1 and calls[2]["body"]["msg_seq"] == 3)
        check("群路径正确", calls[0]["path"] == "/v2/groups/GRP1/messages")

        # 2) 22009：主动消息被拒 → rejected，不重试
        ad2 = make_adapter()
        rejected_calls: list[int] = []

        async def reject(method: str, path: str, body: dict[str, Any] | None = None) -> Any:
            rejected_calls.append(1)
            raise qm.ApiHTTPError(400, '{"code": 22009, "message": "active limit"}')

        ad2._api_request = reject  # type: ignore[assignment]
        result2 = run_async(ad2._post_message("private", "USER1", {"content": "hi"}, False))
        check("22009 判定 rejected 不重试", result2 == "rejected" and len(rejected_calls) == 1)

        # 3) 40034025：event_id 无效 → 清除后重发一次成功
        ad3 = make_adapter()
        event_calls: list[dict[str, Any]] = []

        async def bad_event(method: str, path: str, body: dict[str, Any] | None = None) -> Any:
            event_calls.append(dict(body or {}))
            if "event_id" in (body or {}):
                raise qm.ApiHTTPError(400, '{"code": 40034025, "message": "invalid event"}')
            return {}

        ad3._api_request = bad_event  # type: ignore[assignment]
        result3 = run_async(ad3._post_message("group", "GRP1", {"content": "hi", "event_id": "EVT"}, False))
        check("40034025 清 event_id 重发成功", result3 == "ok" and len(event_calls) == 2)
        check("重发时 event_id 已清除", "event_id" not in event_calls[1])

        # 4) 重试耗尽 → failed
        ad4 = make_adapter()

        async def always_timeout(method: str, path: str, body: dict[str, Any] | None = None) -> Any:
            raise TimeoutError("down")

        ad4._api_request = always_timeout  # type: ignore[assignment]
        result4 = run_async(ad4._post_message("group", "GRP1", {"content": "hi"}, False))
        check("重试耗尽判定 failed", result4 == "failed")
    finally:
        qm._SEND_RETRY_DELAY_TEXT, qm._SEND_RETRY_DELAY_MEDIA = old_text, old_media


def test_active_stack() -> None:
    """22009 补发栈：入栈上限、被动回复后借额度补发、每次上限、凭据耗尽即止。"""
    from endstone_lumenbridge.onebot.qqofficial_adapter import (
        _ACTIVE_STACK_FLUSH,
        _ACTIVE_STACK_MAX,
        _PASSIVE_WINDOW_GROUP,
    )

    ad = make_adapter()
    for i in range(_ACTIVE_STACK_MAX + 2):
        ad._push_active_stack("group", "GRP1", f"msg{i}", None)
    check("补发栈每目标上限", len(ad._active_stack["GRP1"]) == _ACTIVE_STACK_MAX)
    check("栈按入队顺序排列", ad._active_stack["GRP1"][0][2] == "msg0")

    # 被动凭据入池（1 条 msg_id = 5 次额度），栈内 5 条待补发
    ad._cache_passive("GRP1", "FRESH", _PASSIVE_WINDOW_GROUP)
    sent: list[dict[str, Any]] = []

    async def ok_api(method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        sent.append(dict(body or {}))
        return {}

    ad._api_request = ok_api  # type: ignore[assignment]
    # 第一次 flush：每次至多 _ACTIVE_STACK_FLUSH 条
    run_async(ad._flush_active_stack("GRP1"))
    check(
        "首次补发条数受每次上限约束",
        len(sent) == _ACTIVE_STACK_FLUSH and len(ad._active_stack["GRP1"]) == _ACTIVE_STACK_MAX - _ACTIVE_STACK_FLUSH,
        f"sent={len(sent)} left={len(ad._active_stack.get('GRP1', []))}",
    )
    check("补发均携带被动凭据", all(b.get("msg_id") == "FRESH" for b in sent))
    seqs = [b["msg_seq"] for b in sent]
    check("补发 msg_seq 递增不重复", seqs == sorted(set(seqs)), str(seqs))

    # 第二次 flush：清空剩余栈（凭据 5 次额度刚好用完）
    run_async(ad._flush_active_stack("GRP1"))
    check("第二次补发清空栈内剩余", len(ad._active_stack.get("GRP1", [])) == 0)
    total_seqs = [b["msg_seq"] for b in sent]
    check("累计补发 5 条且 seq 覆盖 1..5", sorted(total_seqs) == [1, 2, 3, 4, 5], str(total_seqs))

    # 凭据耗尽后 flush 直接返回，不再发送
    sent.clear()
    run_async(ad._flush_active_stack("GRP1"))
    check("无凭据时不补发", len(sent) == 0)


# ---------------------------------------------------------------- 出站富媒体
def test_extract_payload() -> None:
    from endstone_lumenbridge.onebot.qqofficial_adapter import _extract_payload

    text, media = _extract_payload("纯文本")
    check("纯文本无媒体", text == "纯文本" and media is None)
    text, media = _extract_payload(
        [{"type": "text", "data": {"text": "看图:"}}, {"type": "image", "data": {"url": "https://x/i.png"}}]
    )
    check("url 图片段提取", text == "看图:" and media == {"type": "image", "url": "https://x/i.png", "data": None})
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as fp:
        fp.write(b"\x89PNG-fake")
        local = fp.name
    text, media = _extract_payload([{"type": "image", "data": {"file": local}}])
    check("本地文件读为 bytes", media is not None and media["data"] == b"\x89PNG-fake" and media["url"] is None)
    Path(local).unlink()
    # base64:// 内联图片（msgbuilder.image(bytes) 生成）：官 bot 域必须本地解码，
    # 否则媒体被静默丢弃（"我的统计"卡片无响应无日志的根因）
    import base64 as _b64

    inline = "base64://" + _b64.b64encode(b"\x89PNG-fake-card").decode()
    text, media = _extract_payload([{"type": "image", "data": {"file": inline}}])
    check(
        "base64 图片解码为 bytes",
        text == "" and media is not None and media["data"] == b"\x89PNG-fake-card" and media["url"] is None,
    )
    # 非法 base64 不致命：丢弃媒体但不出错
    text, media = _extract_payload([{"type": "image", "data": {"file": "base64://@@@not-b64@@@@"}}])
    check("非法 base64 安全丢弃", media is None and text == "")


def test_mention_markdown_body() -> None:
    """官方 @ 出站：at 段 → 官方文本链 → 切 markdown 载体（Gensokyo 实测组合）。

    markdown（msg_type=2）+ <qqbot-at-user id="openid" /> 是 Gensokyo-ForSpark
    实测可渲染真实 @ 的组合（官方开发者实测 markdown 消息可渲染真 at）；
    纯文本 + 文本链、markdown + <at id=""> 均实测显示原文。
    其余文本同步做 markdown 转义（防 * # < 被客户端误解析）。
    """
    from endstone_lumenbridge.onebot.qqofficial.sender import _payload_body
    from endstone_lumenbridge.onebot.qqofficial_adapter import _extract_payload

    text, media = _extract_payload(
        [{"type": "at", "data": {"qq": "ABC123OPENID"}}, {"type": "text", "data": {"text": " 欢迎新成员！"}}]
    )
    check(
        "at 段渲染官方文本链",
        text == '<qqbot-at-user id="ABC123OPENID" /> 欢迎新成员！' and media is None,
        text,
    )
    body = _payload_body(text, "")
    check("含 @ 切 markdown 载体", body == {"msg_type": 2, "markdown": {"content": text}})
    check("纯文本载体不变", _payload_body("普通文本", "") == {"msg_type": 0, "content": "普通文本"})
    check("富媒体载体不变", _payload_body(text, "FI1") == {"msg_type": 7, "content": text})
    check("@all 丢弃不出标记", "<qqbot-at-user" not in _extract_payload([{"type": "at", "data": {"qq": "all"}}])[0])

    # markdown 转义：特殊字符加反斜杠前缀，@ 标记保持原样
    from endstone_lumenbridge.onebot.qqofficial.utils import escape_markdown_text

    check(
        "markdown 转义保护特殊字符",
        escape_markdown_text("发 绑定白名单<你的游戏ID> #1 *重要*") ==
        "发 绑定白名单\\<你的游戏ID\\> \\#1 \\*重要\\*",
    )
    check(
        "markdown 转义不动 @ 标记",
        escape_markdown_text('<qqbot-at-user id="U1" /> hi') == '<qqbot-at-user id="U1" /> hi',
    )


def test_at_marker_converter() -> None:
    """出站格式转换器：任意风格的 @ 标记统一转为官方文本链标准格式。

    <qqbot-at-user id="id" /> + markdown 载体是 Gensokyo-ForSpark 实测可渲染
    组合；旧协议 <@!id> / <@id>（已弃用）与旧社区写法 <at id="id"></at>
    一并归一化为官方格式；id 原样保留（openid / unionid 均可）。
    覆盖 at 段、text 段字面量、纯字符串消息（子插件直拼字符串）三条路径。
    """
    from endstone_lumenbridge.onebot.qqofficial_adapter import _extract_payload
    from endstone_lumenbridge.onebot.qqofficial.sender import _payload_body

    # at 段 → 官方文本链
    text, media = _extract_payload(
        [{"type": "at", "data": {"qq": "ABC123OPENID"}}, {"type": "text", "data": {"text": " 欢迎新成员！"}}]
    )
    check(
        "at 段渲染官方文本链",
        text == '<qqbot-at-user id="ABC123OPENID" /> 欢迎新成员！' and media is None,
    )
    # text 段字面量：无空格自闭合变体 → 归一化为标准官方格式
    text2, _ = _extract_payload([{"type": "text", "data": {"text": '<qqbot-at-user id="U1"/> 你好'}}])
    check("官方文本链变体归一化", text2 == '<qqbot-at-user id="U1" /> 你好', text2)
    # 旧协议字面量（含 / 不含感叹号），id 原样保留（unionid 同样处理）
    text3, _ = _extract_payload(
        [{"type": "text", "data": {"text": "叫 <@!U2> 和 <@UNION9> 一下"}}]
    )
    check(
        "旧协议字面量转换（id 原样）",
        text3 == '叫 <qqbot-at-user id="U2" /> 和 <qqbot-at-user id="UNION9" /> 一下',
        text3,
    )
    # 纯字符串消息（子插件直拼字符串场景）
    text4, _ = _extract_payload('<qqbot-at-user id="U4"/>收到')
    check("纯字符串标记转换", text4 == '<qqbot-at-user id="U4" />收到', text4)
    # 空列表 str 段同样归一化
    text5, _ = _extract_payload(["str 段 <@!U5>"])
    check("str 段标记转换", text5 == 'str 段 <qqbot-at-user id="U5" />', text5)
    # 旧社区写法 <at id=""></at>（频道模板语法，实测不渲染）也转换为官方格式
    text6, _ = _extract_payload('<at id="U6"></at>保持')
    check("旧社区写法转换", text6 == '<qqbot-at-user id="U6" />保持', text6)
    # 已是标准官方格式的不受影响（幂等）
    text7, _ = _extract_payload('<qqbot-at-user id="U6" />保持')
    check("标准格式幂等", text7 == '<qqbot-at-user id="U6" />保持', text7)
    # 转换后切 markdown 载体；无标记纯文本载体不变
    check("转换结果切 markdown 载体", _payload_body(text3, "") == {"msg_type": 2, "markdown": {"content": text3}})
    check("无标记纯文本载体不变", _payload_body("普通文本", "") == {"msg_type": 0, "content": "普通文本"})
    check("富媒体载体优先", _payload_body(text3, "FI1") == {"msg_type": 7, "content": text3})
    # @all 丢弃；空 id 官方占位标记整体丢弃不留残壳
    check("@all 丢弃不出标记", "<qqbot-at-user" not in _extract_payload([{"type": "at", "data": {"qq": "all"}}])[0])
    check(
        "空 id 官方标记丢弃",
        _extract_payload([{"type": "text", "data": {"text": 'a<qqbot-at-user id="" />b'}}])[0] == "ab",
    )
    # 转换不吞标记后随空白
    check(
        "转换保留后随空白",
        _extract_payload([{"type": "text", "data": {"text": "<@!U7> 早"}}])[0] == '<qqbot-at-user id="U7" /> 早',
    )


def test_markdown_fallback_conversion() -> None:
    """markdown 被拒降级通路：官方文本链 → @Openid前8位，并反转义 markdown 转义。"""
    from endstone_lumenbridge.onebot.qqofficial.utils import (
        escape_markdown_text,
        markdown_to_plain_text,
        normalize_target,
    )

    # @ 标记还原为 @Openid前8位 + 转义字符还原（不带反斜杠）；
    # 真实 openid 为 32 位 hex，测试用 32 位桩验证；短 id 标记直接移除
    md = escape_markdown_text('<qqbot-at-user id="E1B33508D3DAA286235A838A6C6D9EAD" /> 发 绑定白名单<你的游戏ID>')
    plain = markdown_to_plain_text(md)
    check(
        "降级还原为可读文本并反转义",
        plain == "@OpenidE1B33508 发 绑定白名单<你的游戏ID>",
        plain,
    )
    check("短 id 标记移除", markdown_to_plain_text('<qqbot-at-user id="U1" /> hi') == " hi")
    # 目标规范化：带备注前缀 / 空白的 openid 提取合法片段
    check(
        "目标带备注前缀提取 openid",
        normalize_target("BBC E1B33508D3DAA286235A838A6C6D9EAD") == "E1B33508D3DAA286235A838A6C6D9EAD",
    )
    check("标准 openid 原样", normalize_target(" E1B33508D3DAA286235A838A6C6D9EAD ") == "E1B33508D3DAA286235A838A6C6D9EAD")

    # 存量 connections.json 含已移除的 at_format 字段：加载无害（多余键忽略）
    from endstone_lumenbridge.connections import ConnectionManager

    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "connections.json"
        p.write_text(
            json.dumps(
                {
                    "version": 1,
                    "adapters": [
                        {
                            "id": "qo_old",
                            "type": "qqofficial",
                            "name": "旧卡",
                            "enabled": False,
                            "app_id": "102000011",
                            "app_secret": "s",
                            "at_format": "official",
                            "main_group": "",
                            "admin_qq": [],
                            "sync": {},
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        cm = ConnectionManager(Path(td), FakeLogger())
        check(
            "存量 at_format 字段加载无害",
            len(cm.adapters) == 1 and cm.adapters[0].get("app_id") == "102000011",
            str(cm.adapters),
        )


def test_inbound_qqbot_at() -> None:
    """入站：新官方文本链 <qqbot-at-user id=""/> 与旧 <@!openid> 同等解析。

    腾讯若按弃用公告迁移推送格式，@ 解析 / bot 触发剥离 / raw_message 均不失效。
    """
    ad = make_adapter()
    data = {
        "id": "MSG6",
        "group_openid": "GRPOPENID1",
        # 新格式 bot 触发标记 + 成员 @ 在文本中间（自闭合空格风格与紧凑风格各一）
        "content": '<qqbot-at-user id="102000001" /> 你好<qqbot-at-user id="MEMBER9"/>在吗',
        "author": {"member_openid": "MEMBER1", "username": "Steve"},
    }
    run_async(ad._emit_group_message(data))
    pack = [p for e, p in ad.bus.events if e == "onebot.pack"][0]
    segs = pack["message"]
    check(
        "新格式 at 段保持原始位置",
        [s["type"] for s in segs] == ["text", "at", "text"]
        and segs[0]["data"]["text"] == "你好"
        and segs[1]["data"]["qq"] == "MEMBER9"
        and segs[2]["data"]["text"] == "在吗",
        str(segs),
    )
    check(
        "新格式 bot 自身 @ 不产生 at 段",
        all(s["type"] != "at" or s["data"]["qq"] != "102000001" for s in segs),
    )
    check("raw_message 不含新格式标记", pack["raw_message"] == "你好在吗")

    from endstone_lumenbridge.onebot.qqofficial.utils import mention_segments, plain_content

    check(
        "mention_segments 新旧格式并提",
        mention_segments('<qqbot-at-user id="U1" /> 和 <@!U2>')
        == [{"type": "at", "data": {"qq": "U1"}}, {"type": "at", "data": {"qq": "U2"}}],
    )
    check(
        "plain_content 剥离新旧标记",
        plain_content('<qqbot-at-user id="U1"/> hi <@!U2>') == "hi",
    )


def test_upload_media_body() -> None:
    ad = make_adapter()
    captured: list[tuple[str, Any]] = []

    async def fake_api(method: str, path: str, body: dict | None = None) -> Any:
        captured.append((path, body))
        return {"file_info": "FILEINFO1", "file_uuid": "UUID1"}

    ad._api_request = fake_api  # type: ignore[method-assign]
    fi = run_async(ad._upload_media("group", "GRP1", {"type": "image", "url": "https://x/i.png", "data": None}))
    path, body = captured[0]
    check("files 接口路径", path == "/v2/groups/GRP1/files")
    check("url 上传请求体", body == {"file_type": 1, "srv_send_msg": False, "url": "https://x/i.png"})
    check("file_info 返回", fi == "FILEINFO1")
    run_async(
        ad._upload_media("private", "USER1", {"type": "record", "url": None, "data": b"abc"})
    )
    path2, body2 = captured[1]
    check("C2C files 路径", path2 == "/v2/users/USER1/files")
    check("base64 上传请求体", body2["file_type"] == 3 and body2["file_data"] == "YWJj")


# ---------------------------------------------------------------- notice 翻译
def test_notice_events() -> None:
    ad = make_adapter()
    ad._emit_robot_added({"group_openid": "GRP9", "op_member_openid": "OP1"})
    ad._emit_robot_removed({"group_openid": "GRP9", "op_member_openid": "OP2"})
    ad._emit_friend_change({"openid": "USER77"}, "friend_add")
    ad._emit_friend_change({"openid": "USER77"}, "friend_del")
    # 管理员 关闭/开启 机器人主动消息（GROUP_MSG_REJECT / GROUP_MSG_RECEIVE）
    asyncio.run(
        ad._on_dispatch({"t": "GROUP_MSG_REJECT", "d": {"group_openid": "GRP9", "op_member_openid": "OP3"}})
    )
    asyncio.run(
        ad._on_dispatch({"t": "GROUP_MSG_RECEIVE", "d": {"group_openid": "GRP9", "op_member_openid": "OP3"}})
    )
    # 用户 关闭/开启 机器人主动消息推送（C2C_MSG_REJECT / C2C_MSG_RECEIVE）
    asyncio.run(
        ad._on_dispatch({"t": "C2C_MSG_REJECT", "d": {"openid": "USER77"}})
    )
    asyncio.run(
        ad._on_dispatch({"t": "C2C_MSG_RECEIVE", "d": {"openid": "USER77"}})
    )
    packs = [p for e, p in ad.bus.events if e == "onebot.pack"]
    check("notice 事件共 8 条", len(packs) == 8)
    check(
        "机器人入群→group_increase",
        packs[0]["notice_type"] == "group_increase"
        and packs[0]["group_id"] == "GRP9"
        and str(packs[0]["user_id"]) == "102000001"  # OneBot 语义：user_id=加入者（机器人自身）
        and str(packs[0]["operator_id"]) == "OP1",   # operator_id=操作者
    )
    check(
        "机器人被移出→kick_me",
        packs[1]["notice_type"] == "group_decrease"
        and packs[1]["sub_type"] == "kick_me"
        and str(packs[1]["user_id"]) == "102000001",
    )
    check("好友增删翻译", packs[2]["notice_type"] == "friend_add" and packs[3]["notice_type"] == "friend_del")
    check(
        "关闭主动消息→group_msg_switch/reject（OneBot 无对应标准事件，用扩展类型不污染 group_ban 语义）",
        packs[4]["notice_type"] == "group_msg_switch"
        and packs[4]["sub_type"] == "reject"
        and packs[4]["group_id"] == "GRP9"
        and str(packs[4]["operator_id"]) == "OP3",
    )
    check(
        "重新开启主动消息→group_msg_switch/receive",
        packs[5]["notice_type"] == "group_msg_switch" and packs[5]["sub_type"] == "receive",
    )
    check(
        "用户关闭主动推送→friend_msg_switch/reject",
        packs[6]["notice_type"] == "friend_msg_switch"
        and packs[6]["sub_type"] == "reject"
        and packs[6]["user_id"] == "USER77",
    )
    check(
        "用户重新开启主动推送→friend_msg_switch/receive",
        packs[7]["notice_type"] == "friend_msg_switch" and packs[7]["sub_type"] == "receive",
    )


def test_robot_removed_cleanup() -> None:
    """移群清理：动态发现记录 + 被动池 / event_id / 补发栈全部清空。"""
    ad = make_adapter(groups=[])
    ad._emit_robot_added({"group_openid": "GRPX", "op_member_openid": "OP1", "event_id": "EVT1"})
    ad.credentials.cache_passive("GRPX", "M1", 300.0)
    ad.credentials.push_active(("group", "GRPX", "hello", None))
    check(
        "移群前凭据就绪",
        "GRPX" in ad._discovered_groups
        and ad.credentials.take_event_id("GRPX") == "EVT1"
        and ad.credentials.active_size("GRPX") == 1,
    )
    ad._emit_robot_removed({"group_openid": "GRPX", "op_member_openid": "OP2"})
    check("移群清空动态发现记录", "GRPX" not in ad._discovered_groups)
    check(
        "移群清空全部回复凭据",
        ad.credentials.take_passive("GRPX") is None
        and ad.credentials.take_event_id("GRPX") == ""
        and ad.credentials.active_size("GRPX") == 0,
    )
    # 配置群：凭据同样清理（移群后必失效），但 groups 配置本身不动
    ad2 = make_adapter()
    ad2.credentials.cache_passive("GRPOPENID1", "M2", 300.0)
    ad2._emit_robot_removed({"group_openid": "GRPOPENID1", "op_member_openid": "OP3"})
    check("配置群凭据同样清理", ad2.credentials.take_passive("GRPOPENID1") is None)
    check("配置 groups 不被移群改动", list(ad2.groups) == ["GRPOPENID1"])


# ---------------------------------------------------------------- 全事件分发
def test_full_event_dispatch() -> None:
    """官方全事件矩阵：OneBot 有语义的翻译，无语义的按官方名（小写）扩展转发。"""
    ad = make_adapter()

    async def dispatch(event: str, data: dict[str, Any]) -> None:
        await ad._on_dispatch({"t": event, "d": data})

    # 频道消息（1<<30）
    asyncio.run(dispatch("AT_MESSAGE_CREATE", {
        "id": "GM1", "channel_id": "CH1", "guild_id": "GD1",
        "content": "频道你好", "author": {"id": "U1", "username": "Alex"},
    }))
    # 频道私信（1<<12）
    asyncio.run(dispatch("DIRECT_MESSAGE_CREATE", {
        "id": "DM1", "guild_id": "GD1", "src_guild_id": "SGD1",
        "content": "私信你好", "author": {"id": "U2", "username": "Bob"},
    }))
    # 频道撤回：公开消息 / 私信（OneBot v11：user_id=发送者 operator_id=操作者）
    asyncio.run(dispatch("PUBLIC_MESSAGE_DELETE", {
        "channel_id": "CH1",
        "message": {"id": "GM1", "author": {"id": "U9"}},
        "operator": {"id": "U1"},
    }))
    asyncio.run(dispatch("DIRECT_MESSAGE_DELETE", {
        "message": {"id": "DM1", "author": {"id": "U2"}}, "operator": {"id": "U2"},
    }))
    # QQ 群成员进退群（1<<24，需 extra_intents 开启）
    asyncio.run(dispatch("GROUP_MEMBER_ADD", {"group_openid": "GRP1", "member_openid": "M1"}))
    asyncio.run(dispatch("GROUP_MEMBER_REMOVE", {"group_openid": "GRP1", "member_openid": "M1"}))
    # 频道成员进退 / 资料变更（1<<1 特权）
    asyncio.run(dispatch("GUILD_MEMBER_ADD", {"guild_id": "GD1", "user": {"id": "U3"}}))
    asyncio.run(dispatch("GUILD_MEMBER_REMOVE", {"guild_id": "GD1", "user": {"id": "U3"}}))
    asyncio.run(dispatch("GUILD_MEMBER_UPDATE", {"guild_id": "GD1", "user": {"id": "U3"}}))
    # OneBot 无对应语义的官方事件：互动回调 / 消息审核 / 论坛 / 语音房 / 表情表态 / 频道变更
    asyncio.run(dispatch("INTERACTION_CREATE", {"group_openid": "GRP1", "openid": "M2", "data": {"resolved": {}}}))
    asyncio.run(dispatch("MESSAGE_AUDIT_PASS", {"audit_id": "A1", "group_openid": "GRP1"}))
    asyncio.run(dispatch("OPEN_FORUM_THREAD_CREATE", {"guild_id": "GD1", "thread_info": {}}))
    asyncio.run(dispatch("AUDIO_START", {"guild_id": "GD1", "channel_id": "CH2"}))
    asyncio.run(dispatch("ADD_REACTION", {"guild_id": "GD1", "user_id": "U1"}))
    asyncio.run(dispatch("GUILD_CREATE", {"id": "GD2", "name": "NewGuild"}))
    # 官方新增 / 未枚举事件：兜底同样转发
    asyncio.run(dispatch("SOME_FUTURE_EVENT", {"group_openid": "GRP1", "openid": "M9"}))

    packs = [p for e, p in ad.bus.events if e == "onebot.pack"]
    check("全事件分发共 16 条包", len(packs) == 16, f"got {len(packs)}")
    guild_msg, guild_dm = packs[0], packs[1]
    check(
        "频道@消息→guild 域群消息",
        guild_msg["post_type"] == "message" and guild_msg["message_type"] == "group"
        and guild_msg["group_id"] == "CH1" and guild_msg["domain"] == "guild"
        and guild_msg["guild_id"] == "GD1" and guild_msg["user_id"] == "U1",
    )
    check(
        "频道私信→guild 域私聊",
        guild_dm["post_type"] == "message" and guild_dm["message_type"] == "private"
        and guild_dm["user_id"] == "U2" and guild_dm["domain"] == "guild",
    )
    check(
        "频道公开撤回→group_recall（user_id=发送者 operator_id=操作者）",
        packs[2]["notice_type"] == "group_recall" and packs[2]["group_id"] == "CH1"
        and packs[2]["message_id"] == "GM1" and packs[2]["user_id"] == "U9"
        and packs[2]["operator_id"] == "U1",
    )
    check(
        "频道私信撤回→friend_recall（user_id=好友）",
        packs[3]["notice_type"] == "friend_recall" and packs[3]["message_id"] == "DM1"
        and packs[3]["user_id"] == "U2",
    )
    check(
        "群成员进群→group_increase",
        packs[4]["notice_type"] == "group_increase" and packs[4]["sub_type"] == "approve"
        and packs[4]["group_id"] == "GRP1" and packs[4]["user_id"] == "M1"
        and packs[4]["domain"] == "official",
    )
    check(
        "群成员退群→group_decrease/leave",
        packs[5]["notice_type"] == "group_decrease" and packs[5]["sub_type"] == "leave",
    )
    check(
        "频道成员进退→guild 域 group_increase/decrease",
        packs[6]["notice_type"] == "group_increase" and packs[6]["group_id"] == "GD1"
        and packs[6]["domain"] == "guild"
        and packs[7]["notice_type"] == "group_decrease",
    )
    check(
        "频道成员资料变更→扩展转发 guild_member_update",
        packs[8]["notice_type"] == "guild_member_update"
        and packs[8]["official_event"] == "GUILD_MEMBER_UPDATE"
        and packs[8]["raw"]["guild_id"] == "GD1",
    )
    raw_names = [p["notice_type"] for p in packs[9:15]]
    check(
        "官方专属事件按官方名（小写）转发",
        raw_names == [
            "interaction_create", "message_audit_pass", "open_forum_thread_create",
            "audio_start", "add_reaction", "guild_create",
        ],
        str(raw_names),
    )
    interaction = packs[9]
    check(
        "扩展事件附 raw 原始载荷与官方事件名",
        interaction["post_type"] == "notice"
        and interaction["official_event"] == "INTERACTION_CREATE"
        and interaction["raw"] == {"group_openid": "GRP1", "openid": "M2", "data": {"resolved": {}}}
        and interaction["user_id"] == "M2" and interaction["group_id"] == "GRP1",
    )
    future = packs[15]
    check(
        "未知事件兜底转发",
        future["notice_type"] == "some_future_event"
        and future["official_event"] == "SOME_FUTURE_EVENT",
    )
    check(
        "未知事件打 DEBUG 日志",
        any(level == "debug" for level, msg in ad.logger.logs),
    )


def test_extra_intents() -> None:
    """extra_intents：构造参数钳制 + Identify 位合并 + connections 校验。"""
    from endstone_lumenbridge.onebot.qqofficial_adapter import _DEFAULT_INTENTS

    # 构造参数：合法值保留，非法值回退 0
    ad = make_adapter(extra_intents=(1 << 24) | (1 << 1))
    check("extra_intents 构造合法值保留", ad.extra_intents == (1 << 24) | (1 << 1))
    ad_bad = make_adapter(extra_intents="not-a-number")
    check("extra_intents 非法值回退 0", ad_bad.extra_intents == 0)

    # Identify 载荷：默认订阅位 | 附加订阅位
    import json as _json

    sent: list[str] = []

    class FakeWS:
        async def send(self, data: str) -> None:
            sent.append(data)

    async def fake_token() -> str:
        return "token123"

    ad._ws = FakeWS()
    ad._access_token_async = fake_token  # 避免真实 HTTP 取 token
    ad._session_id = ""  # 走 Identify 分支
    asyncio.run(ad._identify_or_resume())
    identify = _json.loads(sent[0])
    check(
        "Identify intents = 默认 | extra",
        identify["op"] == 2 and identify["d"]["intents"] == (_DEFAULT_INTENTS | (1 << 24) | (1 << 1)),
        f"got {identify['d'].get('intents')}",
    )

    # connections 校验：合法 / 越界 / 布尔
    from endstone_lumenbridge.connections import ConnectionManager, ConnectionValidationError

    with tempfile.TemporaryDirectory() as td:
        cm = ConnectionManager(Path(td), FakeLogger())
        try:
            cm.create({
                "type": "qqofficial", "name": "官方位", "enabled": False,
                "app_id": "102000009", "app_secret": "s",
                "extra_intents": 1 << 30,
            })
            check("extra_intents 合法值通过校验", True)
        except ConnectionValidationError as e:
            check("extra_intents 合法值通过校验", False, str(e))
        try:
            cm.create({
                "type": "qqofficial", "name": "越界", "enabled": False,
                "app_id": "102000010", "app_secret": "s",
                "extra_intents": 1 << 31,
            })
            check("extra_intents 越界被拒", False)
        except ConnectionValidationError:
            check("extra_intents 越界被拒", True)
        try:
            target_id = next(a["id"] for a in cm.adapters if a.get("type") == "qqofficial")
            cm.update(target_id, {"extra_intents": True})
            check("extra_intents 布尔被拒", False)
        except ConnectionValidationError:
            check("extra_intents 布尔被拒", True)


# ---------------------------------------------------------------- 本地兜底
def test_local_fallbacks() -> None:
    ad = make_adapter()
    box: list[Any] = []
    ad.get_group_list(box.append)
    check("get_group_list 本地兜底", box == [[{"group_id": "GRPOPENID1", "group_name": "GRPOPENID1"}]])
    box.clear()
    ad.get_group_info("GRPOPENID1", box.append)
    check("get_group_info 命中", box and box[0]["group_id"] == "GRPOPENID1")
    box.clear()
    ad.get_group_info("UNKNOWN", box.append)
    check("get_group_info 未命中返回 None", box == [None])


# ---------------------------------------------------------------- connections 校验
def test_connections_validation() -> None:
    from endstone_lumenbridge.connections import ConnectionManager, ConnectionValidationError

    with tempfile.TemporaryDirectory() as td:
        cm = ConnectionManager(Path(td), FakeLogger())
        # 官方域：openid 合法
        try:
            cm.create(
                {
                    "type": "qqofficial",
                    "name": "官方",
                    "enabled": True,
                    "app_id": "102000001",
                    "app_secret": "sec",
                    "main_group": ["GRPOPENID_X"],
                    "admin_qq": ["ADMINOPENID_Y"],
                }
            )
            check("官方域 openid 校验通过", True)
        except ConnectionValidationError as e:
            check("官方域 openid 校验通过", False, str(e))
        # 官方域：非法 openid 被拒
        try:
            cm.create(
                {
                    "type": "qqofficial",
                    "name": "官方2",
                    "enabled": False,
                    "app_id": "102000002",
                    "app_secret": "sec",
                    "main_group": ["bad openid!"],
                }
            )
            check("官方域非法 openid 被拒", False)
        except ConnectionValidationError:
            check("官方域非法 openid 被拒", True)
        # 个人号域：字符串群号仍被拒
        try:
            cm.create(
                {
                    "type": "websocket",
                    "name": "个人",
                    "enabled": False,
                    "main_group": ["NOT_A_NUMBER"],
                }
            )
            check("个人号域字符串群号被拒", False)
        except ConnectionValidationError:
            check("个人号域字符串群号被拒", True)
        # 管理员宽松并集含官方 openid
        keys = cm.all_admin_keys()
        check("all_admin_keys 含官方 openid", "ADMINOPENID_Y" in keys)


# ---------------------------------------------------------------- hub 双域路由
class FakeAdapter:
    def __init__(self, adapter_type: str, groups: list[str], connected: bool = True) -> None:
        self.adapter_type = adapter_type
        self.groups = groups
        self.is_connected = connected
        self.sent: list[tuple[str, Any]] = []

    def send_group_msg(self, group_id: Any, message: Any) -> None:
        self.sent.append(("group", group_id))

    def send_private_msg(self, user_id: Any, message: Any) -> None:
        self.sent.append(("private", user_id))


def test_hub_dual_domain_routing() -> None:
    from endstone_lumenbridge.onebot.hub import AdapterHub

    hub = AdapterHub.__new__(AdapterHub)  # 绕过 __init__ 依赖
    hub._lock = __import__("threading").Lock()  # type: ignore[attr-defined]
    ws = FakeAdapter("websocket", ["12345"])
    official = FakeAdapter("qqofficial", ["GRPOPENID1"])
    hub._adapters = {"a1": ws, "a2": official}  # type: ignore[attr-defined]

    hub.send_group_msg(12345, "hi")  # type: ignore[attr-defined]
    hub.send_group_msg("GRPOPENID1", "hi")  # type: ignore[attr-defined]
    check("数字群号只发个人号域", ws.sent == [("group", 12345)] and official.sent == [("group", "GRPOPENID1")])

    ws2 = FakeAdapter("websocket", [], connected=False)
    ws.is_connected = False
    hub._adapters["a3"] = ws2  # type: ignore[attr-defined]
    ws.sent.clear()
    official.sent.clear()
    hub.send_group_msg(67890, "hi")  # type: ignore[attr-defined]
    check("未命中数字群只发已连接个人号", ws.sent == [] and official.sent == [])
    ws.is_connected = True

    ws.sent.clear()
    official.sent.clear()
    hub.send_private_msg(10001, "hi")  # type: ignore[attr-defined]
    hub.send_private_msg("USEROPENID9", "hi")  # type: ignore[attr-defined]
    check(
        "私聊双域过滤",
        ws.sent == [("private", 10001)] and official.sent == [("private", "USEROPENID9")],
    )


# ---------------------------------------------------------------- 白名单双域
def test_whitelist_dual_store() -> None:
    from endstone_lumenbridge.modules.whitelist import WhitelistModule

    class FakePlugin:
        data_folder = ""
        # conf 已改为实时读取 config_manager 的 property，注入带 whitelist 的配置管理器
        class _CM:
            whitelist = {}
        config_manager = _CM()
        bus = None
        adapter = None

    with tempfile.TemporaryDirectory() as td:
        FakePlugin.data_folder = td
        wl = WhitelistModule.__new__(WhitelistModule)
        wl.plugin = FakePlugin()
        wl.logger = FakeLogger()
        wl._data_lock = __import__("threading").RLock()
        wl._pending_qq = set()
        wl._pending_xbox = set()
        wl.path = Path(td) / "whitelist.json"
        wl.path_official = Path(td) / "whitelist_official.json"
        wl.bindings = []
        wl.bindings_official = []

        check("QQ 域绑定", wl.add_binding(10001, "Steve", "qq"))
        check("官方域 openid 绑定", wl.add_binding("USEROPENID1", "Alex", "official"))
        check("xbox 跨域唯一", not wl.add_binding("USEROPENID2", "steve", "official"))
        check(
            "双文件分离",
            wl.path.is_file() and wl.path_official.is_file()
            and json.loads(wl.path.read_text()) == [{"qid": "10001", "xbox": "Steve"}],
        )
        check("跨域 xbox 查询", wl.get_qq_by_xbox("Alex") == "USEROPENID1")
        merged = wl.snapshot()
        check("合并快照含 domain 字段", {b["domain"] for b in merged} == {"qq", "official"})
        check("官方域解绑", wl.remove_binding_by_qq("USEROPENID1", "official") is not None)
        check("解绑后官方域为空", wl.snapshot("official") == [])
        check("QQ 域不受影响", wl.snapshot("qq") == [{"qid": "10001", "xbox": "Steve"}])


def main() -> int:
    tests = [
        test_inbound_group_message,
        test_inbound_c2c_message,
        test_inbound_attachments,
        test_inbound_mention_position,
        test_passive_seq,
        test_passive_pool,
        test_event_id_credential,
        test_biz_code,
        test_post_message_matrix,
        test_active_stack,
        test_extract_payload,
        test_mention_markdown_body,
        test_at_marker_converter,
        test_inbound_qqbot_at,
        test_upload_media_body,
        test_notice_events,
        test_full_event_dispatch,
        test_extra_intents,
        test_local_fallbacks,
        test_connections_validation,
        test_hub_dual_domain_routing,
        test_whitelist_dual_store,
    ]
    for fn in tests:
        print(f"\n== {fn.__name__} ==")
        try:
            fn()
        except AssertionError:
            # check 失败已抛 AssertionError 并打印详情；吞掉以继续跑完其余
            # 用例，最终仍以 FAILED 非空 → 返回码 1 结束（手动运行语义保留）
            pass
    print(f"\n{'=' * 46}\nQQOfficial 测试: {len(PASSED)} 通过, {len(FAILED)} 失败")
    if FAILED:
        print("失败项:", ", ".join(FAILED))
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
