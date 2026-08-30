from importlib.metadata import entry_points
from endstone_lumenbridge.plugin import LumenBridgePlugin

entries = entry_points(group="endstone")
entry = next((ep for ep in entries if ep.name == "lumenbridge"), None)
assert entry is not None, "missing endstone entry point"
loaded = entry.load()
assert loaded is LumenBridgePlugin, "entry point resolved unexpected object"
print(f"ENTRYPOINT_OK name={entry.name} value={entry.value} api={loaded.api_version}")
