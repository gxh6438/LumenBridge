"""GROUP_JOIN_REQUEST 事件翻译与审批验证。

覆盖 QQ 官方机器人「用户申请加群」功能（intent 1<<25）：
1. 事件翻译：GROUP_JOIN_REQUEST → OneBot v11 request.group.add
   （主动申请 / 邀请入群 / 问答验证 / 畸形载荷安全）；
2. 审批回传：set_group_add_request → POST /v2/groups/{g}/approval_join_request/{m}
   （同意 / 拒绝附理由 / 未知 flag 告警不调用 / 成功后缓存清除）。
"""

from __future__ import annotations

import asyncio
import sys
import threading
import time
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

# 仅在真实 endstone 缺失时注入 stub；真实环境存在时 setdefault 会给
# endstone.event 装上空模块，毁掉真实包并污染同进程后续测试
try:
    import endstone.command  # noqa: F401
    import endstone.event  # noqa: F401
    import endstone.plugin  # noqa: F401
except ImportError:
    for _name in ("endstone", "endstone.color", "endstone.command", "endstone.event", "endstone.plugin"):
        sys.modules.setdefault(_name, types.ModuleType(_name))

from endstone_lumenbridge.onebot.qqofficial.translate import EventTranslator


class FakeBus:
    def __init__(self):
        self.packs = []

    def emit(self, name, *args):
        self.packs.append((name, args[0] if args else None))


class FakeLogger:
    def __init__(self):
        self.lines = []

    def info(self, msg):
        self.lines.append(("info", msg))

    def warning(self, msg):
        self.lines.append(("warn", msg))


class FakeAdapter:
    app_id = "102345678"
    adapter_id = "qqo1"

    def __init__(self):
        self.logger = FakeLogger()
        self.bus = FakeBus()
        self._loop = None
        self._join_requests = {}
        self.api_calls = []

    def remember_join_request(self, flag, group_openid, member_openid):
        self._join_requests[flag] = (group_openid, member_openid)

    def _emit_pack(self, pack):
        # 与真实适配器的 stop() 后 fallback 路径一致：直接同步 emit
        self.bus.emit("onebot.pack", pack)

    async def _api_request(self, method, path, body):
        self.api_calls.append((method, path, body))
        return {}


class JoinRequestTests(unittest.TestCase):
    def setUp(self):
        self.ad = FakeAdapter()
        self.tr = EventTranslator(self.ad)

    def tearDown(self):
        if self.ad._loop is not None:
            self.ad._loop.call_soon_threadsafe(self.ad._loop.stop)
            self.ad._loop = None

    def _dispatch(self, payload):
        asyncio.run(self.tr.on_dispatch({"t": "GROUP_JOIN_REQUEST", "d": payload}))

    # ------------------------------------------------ 事件翻译

    def test_self_apply_with_verify_message(self):
        """主动申请 + 验证消息：完整字段翻译。"""
        payload = {
            "group_openid": "G1",
            "join_request_id": "JR1",
            "member_openid": "M1",
            "username": "小明",
            "apply_at": "2026-08-16T10:00:00+08:00",
            "apply_source": "self_apply",
            "verify_info": {"method": "verify_message", "verify_message": "就快乐了"},
        }
        self._dispatch(payload)
        self.assertEqual(len(self.ad.bus.packs), 1)
        name, pack = self.ad.bus.packs[0]
        self.assertEqual(name, "onebot.pack")
        self.assertEqual(pack["post_type"], "request")
        self.assertEqual(pack["request_type"], "group")
        self.assertEqual(pack["sub_type"], "add")
        self.assertEqual(pack["group_id"], "G1")
        self.assertEqual(pack["user_id"], "M1")
        self.assertEqual(pack["flag"], "JR1")
        self.assertIn("小明", pack["comment"])
        self.assertIn("就快乐了", pack["comment"])
        self.assertIs(pack["raw"], payload)
        # 缓存记录供审批回传
        self.assertEqual(self.ad._join_requests["JR1"], ("G1", "M1"))

    def test_invited_join(self):
        """邀请入群：comment 含邀请标记。"""
        payload = {
            "group_openid": "G2",
            "join_request_id": "JR2",
            "member_openid": "M2",
            "username": "小红",
            "apply_source": "invited",
            "invited_by": "M1",
        }
        self._dispatch(payload)
        pack = self.ad.bus.packs[0][1]
        self.assertEqual(pack["flag"], "JR2")
        self.assertTrue(
            "邀请" in pack["comment"] or "invited" in pack["comment"],
            pack["comment"],
        )

    def test_review_qa(self):
        """问答验证：comment 含问答内容。"""
        payload = {
            "group_openid": "G3",
            "join_request_id": "JR3",
            "member_openid": "M3",
            "username": "小刚",
            "apply_source": "self_apply",
            "verify_info": {
                "method": "admin_review_qa",
                "review_qa_list": [{"question": "服务器名称?", "answer": "MXC"}],
            },
        }
        self._dispatch(payload)
        pack = self.ad.bus.packs[0][1]
        self.assertIn("服务器名称", pack["comment"])
        self.assertIn("MXC", pack["comment"])

    def test_malformed_payloads_ignored(self):
        """畸形载荷：缺关键字段时静默丢弃，不产生事件。"""
        self._dispatch({"group_openid": "G"})  # 缺 member / flag
        self._dispatch({})  # 全空
        self._dispatch({"group_openid": "G", "member_openid": "M"})  # 缺 flag
        self.assertEqual(len(self.ad.bus.packs), 0)

    # ------------------------------------------------ 审批回传

    def _start_loop(self):
        loop = asyncio.new_event_loop()
        threading.Thread(target=loop.run_forever, daemon=True).start()
        self.ad._loop = loop

    def _run_setter(self, *args, **kwargs):
        # 审批方法从真实适配器类借入（不实例化 adapter，避免重依赖）
        from endstone_lumenbridge.onebot.qqofficial_adapter import QQOfficialAdapter

        QQOfficialAdapter.set_group_add_request(self.ad, *args, **kwargs)

    def test_approve_and_decline(self):
        """同意 / 拒绝：调用官方审批接口，成功后清除缓存。"""
        self._dispatch(
            {
                "group_openid": "G1",
                "join_request_id": "JR1",
                "member_openid": "M1",
                "username": "小明",
                "apply_source": "self_apply",
            }
        )
        self._dispatch(
            {
                "group_openid": "G2",
                "join_request_id": "JR2",
                "member_openid": "M2",
                "username": "小红",
                "apply_source": "invited",
            }
        )
        self._start_loop()

        self._run_setter("JR1", "add", True)
        self._run_setter("JR2", "add", False, "验证不对")

        for _ in range(50):
            if len(self.ad.api_calls) >= 2:
                break
            time.sleep(0.05)

        self.assertEqual(len(self.ad.api_calls), 2, self.ad.api_calls)
        m, p, b = self.ad.api_calls[0]
        self.assertEqual(m, "POST")
        self.assertEqual(p, "/v2/groups/G1/approval_join_request/M1")
        self.assertEqual(b, {"op": "approve", "join_request_id": "JR1"})
        m, p, b = self.ad.api_calls[1]
        self.assertEqual(m, "POST")
        self.assertEqual(p, "/v2/groups/G2/approval_join_request/M2")
        self.assertEqual(
            b, {"op": "decline", "join_request_id": "JR2", "reject_reason": "验证不对"}
        )
        # 成功处理后缓存清除
        self.assertNotIn("JR1", self.ad._join_requests)
        self.assertNotIn("JR2", self.ad._join_requests)

    def test_unknown_flag_warns_without_api_call(self):
        """未知 flag：仅告警，不调用 API。"""
        self._start_loop()
        self._run_setter("NOPE", "add", True)
        for _ in range(20):
            if any(lv == "warn" for lv, _ in self.ad.logger.lines):
                break
            time.sleep(0.05)
        self.assertEqual(len(self.ad.api_calls), 0)
        self.assertTrue(any(lv == "warn" for lv, _ in self.ad.logger.lines))

    def test_no_loop_warns(self):
        """事件循环未运行：仅告警，不抛异常。"""
        self._dispatch(
            {
                "group_openid": "G1",
                "join_request_id": "JR1",
                "member_openid": "M1",
                "apply_source": "self_apply",
            }
        )
        self._run_setter("JR1", "add", True)  # _loop 为 None
        self.assertTrue(any(lv == "warn" for lv, _ in self.ad.logger.lines))
        self.assertEqual(len(self.ad.api_calls), 0)

    # ------------------------------------------------ 派发事件名

    def test_dispatcher_event_name(self):
        """事件包可经 EventBus 以 request.group 派发（dispatcher 分层规则）。"""
        from endstone_lumenbridge.event_bus import EventBus

        self._dispatch(
            {
                "group_openid": "G1",
                "join_request_id": "JR1",
                "member_openid": "M1",
                "apply_source": "self_apply",
            }
        )
        pack = self.ad.bus.packs[0][1]
        bus = EventBus()
        got = []
        bus.on("request.group", lambda p: got.append(p))
        bus.emit(f"request.{pack['request_type']}", pack)
        self.assertTrue(got)
        self.assertEqual(got[0]["flag"], "JR1")


if __name__ == "__main__":
    unittest.main()
