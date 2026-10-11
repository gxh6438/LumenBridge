from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from endstone_lumenbridge.event_bus import EventBus
from endstone_lumenbridge.onebot.adapter import OneBotAdapter
from endstone_lumenbridge.onebot.dispatcher import EventDispatcher
from endstone_lumenbridge.onebot.message import decode_cq_entities, format_message
from endstone_lumenbridge.onebot import packets


class DummyLogger:
    def __init__(self) -> None:
        self.entries: list[tuple[str, str]] = []

    def _record(self, level: str, value: object) -> None:
        self.entries.append((level, str(value)))

    def info(self, value: object) -> None:
        self._record("info", value)

    def warning(self, value: object) -> None:
        self._record("warning", value)

    def error(self, value: object) -> None:
        self._record("error", value)

    def exception(self, value: object) -> None:
        self._record("exception", value)


class DummyAdapter:
    def __init__(self) -> None:
        self.group_messages: list[tuple[object, object]] = []
        self.private_messages: list[tuple[object, object]] = []

    def send_group_msg(self, target: object, message: object) -> None:
        self.group_messages.append((target, message))

    def send_private_msg(self, target: object, message: object) -> None:
        self.private_messages.append((target, message))


class DummyEnvPool:
    def __init__(self) -> None:
        self.current: list[object] = []

    def set_current_group(self, group: object, source: object = None) -> None:
        self.current.append(group)

    def clear_current_group(self) -> None:
        self.current.append(None)


class OneBotCoreTests(unittest.TestCase):
    def test_dispatcher_decodes_entities_builds_reply_and_clears_context(self) -> None:
        logger = DummyLogger()
        adapter = DummyAdapter()
        bus = EventBus(logger)
        dispatcher = EventDispatcher(adapter, bus, logger)
        env = DummyEnvPool()
        dispatcher.env_pool = env
        received: list[dict] = []

        def listener(pack: dict, reply) -> None:
            received.append(pack)
            reply("received", quote=True)

        bus.on("message.group.normal", listener)
        dispatcher._on_pack({
            "post_type": "message",
            "message_type": "group",
            "sub_type": "normal",
            "group_id": 123,
            "message_id": 456,
            "raw_message": "hello &#91;x&#93; &amp; &#44;",
        })
        self.assertEqual(received[0]["raw_message"], "hello [x] & ,")
        self.assertEqual(env.current, [123, None])
        target, message = adapter.group_messages[0]
        self.assertEqual(target, 123)
        self.assertEqual(message[0], {"type": "reply", "data": {"id": "456"}})
        self.assertEqual(message[1], {"type": "text", "data": {"text": "received"}})

    def test_adapter_dispatches_valid_json_and_ignores_invalid_payloads(self) -> None:
        logger = DummyLogger()
        bus = EventBus(logger)
        adapter = OneBotAdapter(logger, bus)
        packets_seen: list[dict] = []
        bus.on("onebot.pack", packets_seen.append)
        adapter._dispatch_raw('{"post_type":"meta_event","meta_event_type":"heartbeat"}')
        adapter._dispatch_raw("not json")
        adapter._dispatch_raw("[]")
        self.assertEqual(len(packets_seen), 1)
        self.assertEqual(packets_seen[0]["post_type"], "meta_event")
        self.assertTrue(any(level == "error" for level, _entry in logger.entries))

    def test_adapter_echo_resolves_pending_future(self) -> None:
        logger = DummyLogger()
        bus = EventBus(logger)
        adapter = OneBotAdapter(logger, bus)
        loop = asyncio.new_event_loop()
        try:
            future = loop.create_future()
            adapter._pending["echo-1"] = future
            adapter._dispatch_raw('{"echo":"echo-1","data":{"ok":true}}')
            self.assertTrue(future.done())
            self.assertEqual(future.result(), {"ok": True})
            self.assertNotIn("echo-1", adapter._pending)
        finally:
            loop.close()

    def test_message_and_packet_helpers_preserve_onebot_shape(self) -> None:
        self.assertEqual(format_message("hello"), [{"type": "text", "data": {"text": "hello"}}])
        self.assertEqual(decode_cq_entities("&#91;a&#93;&amp;&#44;"), "[a]&,")
        self.assertEqual(
            packets.group_kick(1, 2, True),
            {"action": "set_group_kick", "params": {"group_id": 1, "user_id": 2, "reject_add_request": True}},
        )

    def test_extended_packet_builders(self) -> None:
        """NapCat 扩展与群管理类动作包构建。"""
        self.assertEqual(packets.group_card(1, 2, "新名片")["action"], "set_group_card")
        self.assertFalse(packets.group_kick(1, 2)["params"]["reject_add_request"])
        self.assertEqual(packets.group_whole_ban(1, True)["action"], "set_group_whole_ban")
        self.assertEqual(packets.group_member_info(1, 2)["action"], "get_group_member_info")
        self.assertEqual(packets.group_member_list(1)["action"], "get_group_member_list")
        self.assertEqual(packets.stranger_info(2)["action"], "get_stranger_info")
        self.assertEqual(packets.group_info(1)["action"], "get_group_info")
        self.assertEqual(packets.group_list()["action"], "get_group_list")
        self.assertEqual(packets.group_name(1, "新群名")["params"]["group_name"], "新群名")
        self.assertEqual(packets.group_admin(1, 2, True)["action"], "set_group_admin")
        self.assertEqual(packets.group_leave(1)["action"], "set_group_leave")
        self.assertEqual(packets.friend_add_request("f1", True)["action"], "set_friend_add_request")
        self.assertFalse(packets.group_add_request("f2", "add", False, "拒绝")["params"]["approve"])
        self.assertEqual(packets.get_message(123)["action"], "get_msg")
        self.assertEqual(packets.send_like(2, 10)["params"]["times"], 10)
        self.assertEqual(packets.login_info()["action"], "get_login_info")
        self.assertEqual(packets.version_info()["action"], "get_version_info")
        self.assertEqual(packets.group_msg_emoji_like(5, "128077")["action"], "set_msg_emoji_like")
        self.assertEqual(packets.group_poke(1, 2)["action"], "group_poke")
        self.assertEqual(packets.friend_poke(2)["action"], "friend_poke")
        self.assertEqual(packets.group_sign(1)["action"], "send_group_sign")
        self.assertEqual(packets.essence_msg(5)["action"], "set_essence_msg")
        self.assertEqual(packets.group_special_title(1, 2, "头衔")["params"]["special_title"], "头衔")
        self.assertEqual(packets.upload_group_file(1, "/tmp/a.txt", "a.txt")["action"], "upload_group_file")
        self.assertEqual(packets.group_root_files(1)["action"], "get_group_root_files")
        self.assertEqual(packets.group_file_url(1, "fid")["action"], "get_group_file_url")
        self.assertEqual(packets.status_info()["action"], "get_status")
        self.assertEqual(packets.image_info("abc.image")["action"], "get_image")
        # 通用透传
        self.assertEqual(
            packets.build("custom_action", {"key": "val"}, echo="e9"),
            {"action": "custom_action", "params": {"key": "val"}, "echo": "e9"},
        )


if __name__ == "__main__":
    unittest.main()
