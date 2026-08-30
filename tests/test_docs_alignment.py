"""官方文档对齐验证：intent 订阅 / 扩展事件转发 / 默认全群生效规则。

覆盖 2026-08 依据官方文档（bot.q.qq.com/api-v2）核对后的三组修正：
1. DEFAULT_INTENTS 默认订阅 GUILDS(1<<0) 与 INTERACTION(1<<26)——
   此前 guild/channel 生命周期与互动事件虽已实现转发，但默认不订阅收不到；
2. INTERACTION_CREATE 等 raw 转发事件的 user_id 提取
   （官方载荷字段 user_openid / group_member_openid）；
3. 「未填写群 openid / 群 QQ 号 → 默认对所有群生效」规则：
   plugin.group_allowed / chat_sync._group_allowed / 官方适配器 get_group_info。
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

# endstone stub 由 tests/conftest.py 在收集前统一注入（真实 endstone 存在时跳过），
# 本文件不再自行向 sys.modules 注入残缺模块。

from endstone_lumenbridge.onebot.qqofficial.constants import (
    DEFAULT_INTENTS,
    INTENT_DIRECT_MESSAGE,
    INTENT_GUILDS,
    INTENT_GROUP_MEMBER,
    INTENT_INTERACTION,
    INTENT_PUBLIC_GUILD_MESSAGES,
    INTENT_PUBLIC_MESSAGES,
)
from endstone_lumenbridge.onebot.qqofficial_adapter import QQOfficialAdapter
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

    def debug(self, msg):
        self.lines.append(("debug", msg))


class FakeAdapter:
    app_id = "102345678"
    adapter_id = "qqo1"
    display_name = "qqo1"

    def __init__(self):
        self.logger = FakeLogger()
        self.bus = FakeBus()
        self._loop = None
        self.api_calls = []
        self._discovered_groups = {}

    def remember_group(self, group_openid):
        self._discovered_groups[str(group_openid or "")] = 0.0

    def _emit_pack(self, pack):
        # 与真实适配器的 stop() 后 fallback 路径一致：直接同步 emit
        self.bus.emit("onebot.pack", pack)

    # 与真实适配器一致：撤回等官方 API 经 _run_official_api 在事件循环上执行
    _run_official_api = QQOfficialAdapter._run_official_api


class IntentTests(unittest.TestCase):
    def test_default_intents_cover_docs_events(self):
        """默认订阅位覆盖文档事件：群聊+C2C / 群成员 / 频道@ / 频道私信 / 频道变更 / 互动。"""
        for bit in (
            INTENT_PUBLIC_MESSAGES,  # 1<<25 群+C2C（含加群申请）
            INTENT_GROUP_MEMBER,  # 1<<24 群成员进退群（文档总表归属，双订阅防 CDN 双版本）
            INTENT_PUBLIC_GUILD_MESSAGES,  # 1<<30 频道@
            INTENT_DIRECT_MESSAGE,  # 1<<12 频道私信
            INTENT_GUILDS,  # 1<<0 GUILD_*/CHANNEL_*
            INTENT_INTERACTION,  # 1<<26 INTERACTION_CREATE
        ):
            self.assertEqual(DEFAULT_INTENTS & bit, bit, hex(bit))

    def test_intent_fallback_after_identify_failures(self):
        """连续 Identify 失败（1<<24 无权限拒连）→ 驱动真实 _main 自动摘除该位降级。"""
        import endstone_lumenbridge.onebot.qqofficial_adapter as qm
        from endstone_lumenbridge.onebot.qqofficial_adapter import QQOfficialAdapter
        from endstone_lumenbridge.onebot.qqofficial.constants import (
            INTENT_FALLBACK_THRESHOLD,
        )

        # 真实适配器实例（非 FakeAdapter）：驱动 _main 的降级分支
        ad = QQOfficialAdapter(
            FakeLogger(), FakeBus(),
            app_id="102000001", app_secret="test-secret",
            adapter_id="qo_docs", adapter_name="官方文档",
        )
        ad._group_member_intent = True
        ad._identify_failures = 0
        ad._running = True
        ad.connect_interval = 0  # 重连不叠加 connect_interval 下限

        calls = {"n": 0}

        async def reject_gateway():
            # 真实 _main 每轮调用一次 _gateway_session：前 N 轮模拟网关拒连
            # （WS 已建立、全新 Identify 未 READY 即被服务端断开），跑满阈值后
            # 以 CancelledError 结束驱动（_main 捕获 CancelledError 后退出循环）。
            # 必须先置 _ws 模拟"WS 已建立后的失败"：启动期网关地址获取失败
            # （was_online=False）与 Identify 权限无关，已被正确排除在计数之外
            calls["n"] += 1
            if calls["n"] > INTENT_FALLBACK_THRESHOLD:
                raise asyncio.CancelledError()
            ad._ws = object()  # WS 已建立，随后被拒断开
            raise RuntimeError(f"simulated gateway reject #{calls['n']}")

        ad._gateway_session = reject_gateway
        # 压缩重连退避，避免测试等待真实指数退避
        old_base, old_max = qm.RECONNECT_BASE_DELAY, qm.RECONNECT_MAX_DELAY
        qm.RECONNECT_BASE_DELAY = qm.RECONNECT_MAX_DELAY = 1.0
        try:
            asyncio.run(ad._main())
        finally:
            qm.RECONNECT_BASE_DELAY, qm.RECONNECT_MAX_DELAY = old_base, old_max

        self.assertGreaterEqual(calls["n"], INTENT_FALLBACK_THRESHOLD)
        self.assertEqual(ad._identify_failures, INTENT_FALLBACK_THRESHOLD)
        self.assertFalse(ad._group_member_intent)
        self.assertTrue(any(lv == "warn" for lv, _ in ad.logger.lines))

        # Identify intents 应不再含 1<<24（驱动真实 _identify_or_resume 掩码逻辑）
        sent: list[str] = []

        class _IdentifyWS:
            async def send(self, data: str) -> None:
                sent.append(data)

        async def fake_token() -> str:
            return "token123"

        ad._ws = _IdentifyWS()
        ad._session_id = ""  # 走 Identify 分支
        ad._access_token_async = fake_token  # 避免真实 HTTP 取 token
        asyncio.run(ad._identify_or_resume())
        identify = json.loads(sent[0])
        self.assertEqual(identify["op"], 2)
        intents = identify["d"]["intents"]
        self.assertEqual(intents & INTENT_GROUP_MEMBER, 0)
        self.assertEqual(intents & INTENT_PUBLIC_MESSAGES, INTENT_PUBLIC_MESSAGES)


class RawForwardTests(unittest.TestCase):
    def setUp(self):
        self.ad = FakeAdapter()
        self.tr = EventTranslator(self.ad)

    def _dispatch(self, event, payload):
        asyncio.run(self.tr.on_dispatch({"t": event, "d": payload}))

    def test_ready_emits_lifecycle_connect(self):
        """网关 READY → OneBot v11 meta_event.lifecycle.connect（子插件统一订阅上线）。"""
        import types as _types

        from endstone_lumenbridge.onebot.qqofficial_adapter import QQOfficialAdapter

        self.ad.on_ready = _types.MethodType(QQOfficialAdapter.on_ready, self.ad)
        self._dispatch(
            "READY",
            {"session_id": "sess-1", "user": {"id": "B1", "username": "官bot"}},
        )
        pack = self.ad.bus.packs[0][1]
        self.assertEqual(pack["post_type"], "meta_event")
        self.assertEqual(pack["meta_event_type"], "lifecycle")
        self.assertEqual(pack["sub_type"], "connect")
        self.assertEqual(pack["domain"], "official")

    def test_interaction_create_user_id_extraction(self):
        """互动事件：user_id 提取 user_openid / group_member_openid，群聊附 group_id。"""
        self._dispatch(
            "INTERACTION_CREATE",
            {
                "id": "itx-1",
                "type": 11,
                "scene": "group",
                "group_openid": "G1",
                "group_member_openid": "M1",
                "data": {"resolved": {"button_data": "confirm"}},
            },
        )
        pack = self.ad.bus.packs[0][1]
        self.assertEqual(pack["post_type"], "notice")
        self.assertEqual(pack["notice_type"], "interaction_create")
        self.assertEqual(pack["user_id"], "M1")
        self.assertEqual(pack["group_id"], "G1")
        self.assertEqual(pack["official_event"], "INTERACTION_CREATE")

        self._dispatch(
            "INTERACTION_CREATE",
            {"id": "itx-2", "type": 18, "scene": "c2c", "user_openid": "U1"},
        )
        pack2 = self.ad.bus.packs[1][1]
        self.assertEqual(pack2["user_id"], "U1")

    def test_guild_create_forwarded(self):
        """频道创建事件 raw 转发（1<<0 默认订阅后即可到达）。"""
        self._dispatch(
            "GUILD_CREATE",
            {"id": "GD1", "name": "频道", "owner_id": "O1", "op_user_id": "O1"},
        )
        pack = self.ad.bus.packs[0][1]
        self.assertEqual(pack["notice_type"], "guild_create")
        self.assertEqual(pack["group_id"], "GD1")
        self.assertEqual(pack["raw"]["name"], "频道")

    def test_captured_doc_events_all_forwarded(self):
        """用户抓包文档 46 事件中的 raw 转发类全部可达（论坛新名/频道成员）。"""
        cases = [
            ("FORUM_THREAD_CREATE", {"guild_id": "G1", "channel_id": "C1", "id": "T1"}),
            ("FORUM_THREAD_UPDATE", {"guild_id": "G1", "channel_id": "C1", "id": "T1"}),
            ("FORUM_THREAD_DELETE", {"guild_id": "G1", "channel_id": "C1", "id": "T1"}),
            ("FORUM_POST_CREATE", {"guild_id": "G1", "channel_id": "C1", "id": "P1"}),
            ("FORUM_POST_DELETE", {"guild_id": "G1", "channel_id": "C1", "id": "P1"}),
            ("FORUM_REPLY_CREATE", {"guild_id": "G1", "channel_id": "C1", "id": "R1"}),
            ("FORUM_REPLY_DELETE", {"guild_id": "G1", "channel_id": "C1", "id": "R1"}),
            ("FORUM_PUBLISH_AUDIT_RESULT", {"guild_id": "G1", "audit_id": "A1"}),
            ("GUILD_MEMBER_UPDATE", {"guild_id": "G1", "user": {"id": "U1"}}),
        ]
        for event, payload in cases:
            self.ad.bus.packs.clear()
            self._dispatch(event, payload)
            self.assertEqual(len(self.ad.bus.packs), 1, event)
            pack = self.ad.bus.packs[0][1]
            self.assertEqual(pack["post_type"], "notice", event)
            self.assertEqual(pack["notice_type"], event.lower(), event)
            self.assertIn("raw", pack, event)

    def test_private_guild_message_semantic_translation(self):
        """私域频道全量消息 MESSAGE_CREATE（1<<9，载荷同 AT_MESSAGE_CREATE）→ 群消息。

        抓包文档确认：私域机器人收到的频道全量消息事件，此前仅 raw 转发，
        现升级为语义翻译，频道互通/正则引擎可正常触发。
        """
        self._dispatch(
            "MESSAGE_CREATE",
            {
                "id": "GM1",
                "guild_id": "G1",
                "channel_id": "C1",
                "content": "hello",
                "author": {"id": "U1", "username": "频道用户"},
            },
        )
        pack = self.ad.bus.packs[0][1]
        self.assertEqual(pack["post_type"], "message")
        self.assertEqual(pack["message_type"], "group")
        self.assertEqual(pack["domain"], "guild")
        self.assertEqual(pack["group_id"], "C1")
        self.assertEqual(pack["guild_id"], "G1")
        self.assertEqual(pack["user_id"], "U1")
        self.assertEqual(pack["message"][0]["data"]["text"], "hello")

    def test_private_guild_message_delete_semantic_translation(self):
        """私域频道消息撤回 MESSAGE_DELETE → group_recall（同 PUBLIC_MESSAGE_DELETE）。

        OneBot v11 规范：user_id=消息发送者、operator_id=撤回操作者。
        """
        self._dispatch(
            "MESSAGE_DELETE",
            {
                "message": {"id": "GM1", "author": {"id": "U9"}},
                "operator": {"id": "U1"},
                "channel_id": "C1",
            },
        )
        pack = self.ad.bus.packs[0][1]
        self.assertEqual(pack["post_type"], "notice")
        self.assertEqual(pack["notice_type"], "group_recall")
        self.assertEqual(pack["domain"], "guild")
        self.assertEqual(pack["message_id"], "GM1")
        self.assertEqual(pack["group_id"], "C1")
        self.assertEqual(pack["user_id"], "U9")
        self.assertEqual(pack["operator_id"], "U1")


class _StubConnections:
    """group_allowed 最小依赖桩：all_group_keys / get / parse_groups_loose。"""

    def __init__(self, adapters):
        self._adapters = adapters

    def all_group_keys(self):
        seen = []
        for cfg in self._adapters.values():
            for key in self.parse_groups_loose(cfg.get("main_group")):
                if key not in seen:
                    seen.append(key)
        return seen

    def get(self, adapter_id):
        return self._adapters.get(adapter_id)

    @staticmethod
    def parse_groups_loose(value):
        if value in (None, "", 0):
            return []
        if isinstance(value, str):
            items = value.split(",")
        elif isinstance(value, (list, tuple)):
            items = value
        else:
            items = [value]
        out = []
        for item in items:
            token = str(item).strip()
            if token and token not in out:
                out.append(token)
        return out


class _StubConfigManager:
    def __init__(self, main_groups):
        self.main_groups = main_groups


class GroupAllowedTests(unittest.TestCase):
    """「未填写群号 → 默认所有群生效」规则验证。"""

    @classmethod
    def setUpClass(cls):
        from endstone_lumenbridge.plugin import LumenBridgePlugin
        from endstone_lumenbridge.modules.chat_sync import ChatSyncModule

        cls.PluginCls = LumenBridgePlugin
        cls.ChatSyncCls = ChatSyncModule

    def _plugin(self, connections, config_manager=None):
        # 真实 endstone 的 Plugin 是 pybind11 C++ 类，object.__new__ 不安全；
        # 走类自身 __new__ 绕过 __init__（stub 环境下两者等价）
        plugin = self.PluginCls.__new__(self.PluginCls)
        plugin.connections = connections
        plugin.config_manager = config_manager
        return plugin

    def _chat_sync(self, plugin):
        module = self.ChatSyncCls.__new__(self.ChatSyncCls)
        module.plugin = plugin
        return module

    def test_empty_config_allows_all_groups(self):
        """所有适配器均未填写群号 → 任意来源群放行。"""
        connections = _StubConnections({"a1": {"enabled": True, "main_group": ""}})
        plugin = self._plugin(connections)
        self.assertTrue(plugin.group_allowed({"group_id": "ANYGROUP", "_lumen_adapter_id": "a1"}))
        chat = self._chat_sync(plugin)
        self.assertTrue(chat._group_allowed({"group_id": "ANYGROUP", "_lumen_adapter_id": "a1"}))

    def test_configured_group_matched(self):
        """填写群号 → 命中放行。"""
        connections = _StubConnections({"a1": {"enabled": True, "main_group": "G1,G2"}})
        plugin = self._plugin(connections)
        self.assertTrue(plugin.group_allowed({"group_id": "G1", "_lumen_adapter_id": "a1"}))

    def test_configured_group_miss_rejected(self):
        """填写群号 → 未命中且来源适配器列表非空 → 拒绝。"""
        connections = _StubConnections({"a1": {"enabled": True, "main_group": "G1"}})
        plugin = self._plugin(connections)
        self.assertFalse(plugin.group_allowed({"group_id": "G9", "_lumen_adapter_id": "a1"}))
        chat = self._chat_sync(plugin)
        self.assertFalse(chat._group_allowed({"group_id": "G9", "_lumen_adapter_id": "a1"}))

    def test_other_adapter_empty_source_list_allows(self):
        """其它适配器填了群，但来源适配器自身留空 → 来源任意群放行。"""
        connections = _StubConnections(
            {"a1": {"enabled": True, "main_group": "G1"}, "a2": {"enabled": True, "main_group": ""}}
        )
        plugin = self._plugin(connections)
        self.assertTrue(plugin.group_allowed({"group_id": "G9", "_lumen_adapter_id": "a2"}))

    def test_fallback_empty_main_groups_allows_all(self):
        """connections 未就绪（加载中/已卸载）→ 不处理任何群消息（v1.0.2 修复）。

        旧实现此处恒放行（死逻辑）：连接管理器未初始化时全部群消息照常
        处理，与生命周期不一致；修复后统一拒绝，仅在适配器卡片明确留空
        群号时才默认全放行（见前四个用例）。
        """
        plugin = self._plugin(None, _StubConfigManager([]))
        self.assertFalse(plugin.group_allowed({"group_id": "G1"}))
        # v1.2.0 起群列表归属 connections.json：config_manager.main_groups
        # 仅是委托视图，connections 未就绪时不再走其兜底（旧死逻辑已移除）
        plugin2 = self._plugin(None, _StubConfigManager(["G1"]))
        self.assertFalse(plugin2.group_allowed({"group_id": "G2"}))
        self.assertFalse(plugin2.group_allowed({"group_id": "G1"}))

    def test_official_get_group_info_empty_groups(self):
        """官方适配器：未配置 openid 时任意群视为已互通。"""
        from endstone_lumenbridge.onebot.qqofficial_adapter import QQOfficialAdapter

        adapter = object.__new__(QQOfficialAdapter)
        adapter.groups = []
        adapter._discovered_groups = {}
        got = []
        adapter.get_group_info("ANYGROUP", got.append)
        self.assertIsNotNone(got[0])
        self.assertEqual(got[0]["group_id"], "ANYGROUP")

        adapter.groups = ["G1"]
        got = []
        adapter.get_group_info("G2", got.append)
        self.assertIsNone(got[0])
        adapter.get_group_info("G1", got.append)
        self.assertEqual(got[1]["group_id"], "G1")


class MemberEventChainTests(unittest.TestCase):
    """官bot成员进退群 → 欢迎规则 / 退群解绑 端到端链路。

    官方载荷字段（文档）：group_openid / member_openid / user_openid。
    """

    def setUp(self):
        self.ad = FakeAdapter()
        self.tr = EventTranslator(self.ad)

    def _dispatch(self, event, payload):
        asyncio.run(self.tr.on_dispatch({"t": event, "d": payload}))

    def test_member_add_translates_to_group_increase(self):
        self._dispatch(
            "GROUP_MEMBER_ADD",
            {
                "timestamp": 1784276757,
                "group_openid": "GOPEN1",
                "member_openid": "MOPEN1",
                "user_openid": "UOPEN1",
            },
        )
        self.assertEqual(len(self.ad.bus.packs), 1)
        pack = self.ad.bus.packs[0][1]
        self.assertEqual(pack["post_type"], "notice")
        self.assertEqual(pack["notice_type"], "group_increase")
        self.assertEqual(pack["sub_type"], "approve")
        self.assertEqual(pack["group_id"], "GOPEN1")
        self.assertEqual(pack["user_id"], "MOPEN1")
        self.assertEqual(pack["domain"], "official")
        self.assertTrue(pack["_lumen_adapter_id"])

    def test_member_remove_translates_to_group_decrease(self):
        self._dispatch(
            "GROUP_MEMBER_REMOVE",
            {
                "timestamp": 1784276757,
                "group_openid": "GOPEN1",
                "member_openid": "MOPEN1",
            },
        )
        pack = self.ad.bus.packs[0][1]
        self.assertEqual(pack["notice_type"], "group_decrease")
        self.assertEqual(pack["sub_type"], "leave")
        self.assertEqual(pack["user_id"], "MOPEN1")

    def test_malformed_member_event_ignored(self):
        self._dispatch("GROUP_MEMBER_ADD", {"group_openid": "G"})  # 缺 member_openid
        self.assertEqual(len(self.ad.bus.packs), 0)

    def test_whitelist_leave_unbind_official_domain(self):
        """退群解绑：official 域按 openid 查绑定并移除白名单。"""
        from endstone_lumenbridge.modules.whitelist import WhitelistModule

        sent = []

        class _StubPlugin:
            @staticmethod
            def group_allowed(pack):
                return True

        class _StubAdapter:
            def send_group_msg(self, group_id, message):
                sent.append((group_id, message))

        # WhitelistModule.conf 为 property，借助子类覆写为普通属性注入配置
        class _WL(WhitelistModule):
            conf = {"enable": True, "remove_on_leave": True}

        wl = _WL.__new__(_WL)
        wl.plugin = _StubPlugin()
        wl.adapter = _StubAdapter()
        wl.logger = FakeLogger()

        removed = []
        wl.remove_binding_by_qq = lambda qq, domain: removed.append((qq, domain))
        wl.get_binding_by_qq = lambda qq, domain: {"qq": qq, "xbox": "Steve"}
        wl._begin_operation = lambda qq, xbox: True
        wl._end_operation = lambda qq, xbox: None
        wl._run_allowlist_command = lambda op, xbox, cb: cb({"success": True, "output": ""})

        pack = {
            "post_type": "notice",
            "notice_type": "group_decrease",
            "sub_type": "leave",
            "group_id": "GOPEN1",
            "user_id": "MOPEN1",
            "self_id": "102345678",
            "domain": "official",
        }
        wl._on_group_decrease(pack)
        self.assertEqual(removed, [("MOPEN1", "official")])
        self.assertEqual(len(sent), 1)  # 解绑通知发回原群
        self.assertEqual(sent[0][0], "GOPEN1")

        # 机器人自身被移出群不解绑任何人
        removed.clear()
        wl._on_group_decrease({**pack, "user_id": "102345678"})
        self.assertEqual(removed, [])


class _FakeCredentials:
    def cache_passive(self, *args, **kwargs):
        pass


class DeleteMsgTests(unittest.TestCase):
    """群消息撤回（DELETE /v2/groups/{g}/messages/{id}）端到端。

    官方 API 文档：撤回机器人发送在当前群的消息（2 分钟内）；
    实测机器人为群管理员时可撤普通群员消息（message_id 取自收到的事件）。
    """

    def setUp(self):
        self.ad = FakeAdapter()

        async def _fake_api(method, path, body=None):
            self.ad.api_calls.append((method, path, body))
            return {}

        self.ad._api_request = _fake_api
        self.ad.credentials = _FakeCredentials()
        self.ad._msg_scopes = {}
        self.ad.remember_msg_scope = lambda mid, kind, target: (
            self.ad._msg_scopes.setdefault(str(mid), (kind, str(target)))
        )
        self.tr = EventTranslator(self.ad)

    def _start_loop(self):
        loop = asyncio.new_event_loop()
        threading.Thread(target=loop.run_forever, daemon=True).start()
        # 等待 loop 真正进入运行态（run_forever 生效前 is_running 为 False）
        for _ in range(100):
            if loop.is_running():
                break
            time.sleep(0.01)
        self.ad._loop = loop

    def _delete(self, message_id):
        from endstone_lumenbridge.onebot.qqofficial_adapter import QQOfficialAdapter

        QQOfficialAdapter.delete_msg(self.ad, message_id)

    def _wait(self, cond, rounds=50):
        for _ in range(rounds):
            if cond():
                return True
            time.sleep(0.05)
        return cond()

    def test_incoming_group_message_recorded(self):
        """收到群消息 → 记录 id → group 映射（管理员撤成员消息场景）。"""
        asyncio.run(
            self.tr.on_dispatch(
                {
                    "t": "GROUP_AT_MESSAGE_CREATE",
                    "d": {
                        "id": "MSG1",
                        "group_openid": "GOPEN1",
                        "content": "hello",
                        "author": {"member_openid": "MOPEN1", "username": "Steve"},
                    },
                }
            )
        )
        self.assertEqual(self.ad._msg_scopes.get("MSG1"), ("group", "GOPEN1"))

    def test_incoming_c2c_message_recorded_as_private(self):
        asyncio.run(
            self.tr.on_dispatch(
                {
                    "t": "C2C_MESSAGE_CREATE",
                    "d": {
                        "id": "MSG2",
                        "content": "hi",
                        "author": {"user_openid": "UOPEN1", "username": "Alex"},
                    },
                }
            )
        )
        self.assertEqual(self.ad._msg_scopes.get("MSG2"), ("private", "UOPEN1"))

    def test_delete_group_message_calls_api(self):
        self.ad._msg_scopes["MSG1"] = ("group", "GOPEN1")
        self._start_loop()
        self._delete("MSG1")
        self.assertTrue(self._wait(lambda: len(self.ad.api_calls) >= 1))
        method, path, body = self.ad.api_calls[0]
        self.assertEqual(method, "DELETE")
        self.assertEqual(path, "/v2/groups/GOPEN1/messages/MSG1")
        # 成功后清除缓存
        self._wait(lambda: "MSG1" not in self.ad._msg_scopes)
        self.assertNotIn("MSG1", self.ad._msg_scopes)

    def test_delete_private_message_warns(self):
        """C2C 消息：官方无撤回接口 → 告警且不调用。"""
        self.ad._msg_scopes["MSG2"] = ("private", "UOPEN1")
        self._start_loop()
        self._delete("MSG2")
        self.assertTrue(any(lv == "warn" for lv, _ in self.ad.logger.lines))
        self.assertEqual(len(self.ad.api_calls), 0)

    def test_delete_unknown_id_warns(self):
        self._start_loop()
        self._delete("NOPE")
        self.assertTrue(any(lv == "warn" for lv, _ in self.ad.logger.lines))
        self.assertEqual(len(self.ad.api_calls), 0)

    def test_delete_without_loop_warns(self):
        self.ad._msg_scopes["MSG1"] = ("group", "GOPEN1")
        self._delete("MSG1")  # _loop 为 None
        self.assertTrue(any(lv == "warn" for lv, _ in self.ad.logger.lines))
        self.assertEqual(len(self.ad.api_calls), 0)

    def test_sent_receipt_recorded(self):
        """发送成功 → 回执 id 入缓存（撤回机器人自己的消息场景）。"""
        from endstone_lumenbridge.onebot.qqofficial.sender import MessageSender

        self.ad._api_calls = []

        async def _fake_api(method, path, body):
            self.ad._api_calls.append((method, path, body))
            return {"id": "SENT1"}

        self.ad._api_request = _fake_api
        self.ad._retry_params = lambda: (1, 0.0, 0.0)
        sender = object.__new__(MessageSender)
        sender.ad = self.ad
        result = asyncio.run(
            sender.post_message("group", "GOPEN1", {"content": "hi"}, False)
        )
        self.assertEqual(result, "ok")
        self.assertEqual(self.ad._msg_scopes.get("SENT1"), ("group", "GOPEN1"))


if __name__ == "__main__":
    unittest.main()
