from __future__ import annotations

import json

from endstone.command import CommandSenderWrapper
from endstone.event import EventPriority, event_handler
from endstone_lumenbridge.plugin import LumenBridgePlugin

result: dict[str, object] = {
    "command_sender_wrapper_available": CommandSenderWrapper is not None,
    "event_priority_monitor_available": EventPriority.MONITOR is not None,
    "plugin_constructor": "not-run",
    "event_handlers": {},
}

try:
    plugin = LumenBridgePlugin()
    result["plugin_constructor"] = "ok"
    result["initial_state"] = {
        "adapter_none": plugin.adapter is None,
        "config_none": plugin.config_manager is None,
        "version": plugin.VERSION,
    }
except Exception as exc:  # runtime has no BDS server; capture exact compatibility boundary
    result["plugin_constructor"] = f"error:{type(exc).__name__}:{exc}"

for name in ("on_player_chat", "on_player_join", "on_player_quit", "on_player_death"):
    handler = getattr(LumenBridgePlugin, name)
    result["event_handlers"][name] = {
        "callable": callable(handler),
        "name": getattr(handler, "__name__", ""),
    }

print(json.dumps(result, ensure_ascii=False, indent=2))
if result["plugin_constructor"] != "ok":
    raise SystemExit(3)
