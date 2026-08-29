from __future__ import annotations

import inspect
import json

import endstone
import endstone.command as command
import endstone.event as event
import endstone.plugin as plugin
from endstone_lumenbridge.plugin import LumenBridgePlugin

required = {
    "endstone": ["ColorFormat"],
    "endstone.command": ["Command", "CommandSender"],
    "endstone.event": [
        "EventPriority",
        "PlayerChatEvent",
        "PlayerDeathEvent",
        "PlayerJoinEvent",
        "PlayerQuitEvent",
        "event_handler",
    ],
    "endstone.plugin": ["Plugin"],
}
modules = {
    "endstone": endstone,
    "endstone.command": command,
    "endstone.event": event,
    "endstone.plugin": plugin,
}
missing = {
    module_name: [name for name in names if not hasattr(modules[module_name], name)]
    for module_name, names in required.items()
}
missing = {key: value for key, value in missing.items() if value}
result = {
    "endstone_version": getattr(endstone, "__version__", "unknown"),
    "endstone_api_version": getattr(endstone, "__api_version__", None),
    "plugin_api_version": LumenBridgePlugin.api_version,
    "plugin_base": LumenBridgePlugin.__bases__[0].__module__ + "." + LumenBridgePlugin.__bases__[0].__name__,
    "plugin_load_signature": str(inspect.signature(getattr(LumenBridgePlugin, "on_load"))),
    "plugin_enable_signature": str(inspect.signature(getattr(LumenBridgePlugin, "on_enable"))),
    "missing_required_api": missing,
}
print(json.dumps(result, ensure_ascii=False, indent=2))
if missing:
    raise SystemExit(2)
