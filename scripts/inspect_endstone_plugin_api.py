from __future__ import annotations

import inspect

try:
    from endstone.plugin import PluginManager, PluginLoader
except Exception as exc:  # noqa: BLE001
    print(f"Endstone import failed: {exc}")
else:
    for cls in (PluginManager, PluginLoader):
        print(f"[{cls.__name__}]")
        for name in ("load_plugin", "load_plugins", "disable_plugin", "enable_plugin", "clear_plugins"):
            member = getattr(cls, name, None)
            if member is None:
                print(f"{name}: absent")
                continue
            try:
                print(f"{name}: {inspect.signature(member)}")
            except (TypeError, ValueError):
                print(f"{name}: present (signature unavailable)")
