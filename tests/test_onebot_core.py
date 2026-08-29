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


if __name__ == "__main__":
    unittest.main()
