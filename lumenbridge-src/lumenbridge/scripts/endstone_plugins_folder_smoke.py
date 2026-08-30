from __future__ import annotations

import hashlib
import json
import shutil
import sys
import zipfile
from pathlib import Path

ARTIFACT = Path("dist/endstone_lumenbridge-1.1.0-py3-none-any.whl").resolve()
DEPLOY_ROOT = Path("/home/ubuntu/lumenbridge_audit/endstone_plugins_folder_smoke/bedrock_server")
PLUGINS_DIR = DEPLOY_ROOT / "plugins"

if not ARTIFACT.is_file():
    raise SystemExit(f"missing official build artifact: {ARTIFACT}")

PLUGINS_DIR.mkdir(parents=True, exist_ok=True)
deployed = PLUGINS_DIR / ARTIFACT.name
shutil.copy2(ARTIFACT, deployed)

with zipfile.ZipFile(ARTIFACT) as wheel:
    names = set(wheel.namelist())
    metadata_name = next(name for name in names if name.endswith(".dist-info/METADATA"))
    entrypoint_name = next(name for name in names if name.endswith(".dist-info/entry_points.txt"))
    metadata = wheel.read(metadata_name).decode("utf-8")
    entry_points = wheel.read(entrypoint_name).decode("utf-8")

if "Name: endstone-lumenbridge" not in metadata:
    raise SystemExit("wheel distribution name is not accepted by Endstone loader")
expected_entry = "[endstone]\nlumenbridge = endstone_lumenbridge.plugin:LumenBridgePlugin"
if expected_entry not in entry_points.replace("\r\n", "\n"):
    raise SystemExit("wheel does not contain expected Endstone entry point")

# 从 wheel 文件本身导入，而非依赖源码目录；这模拟 Endstone 在 plugins/ 目录发现
# 发行包后解析其插件入口时最关键的 Python 层行为。
sys.path.insert(0, str(deployed))
from endstone_lumenbridge.plugin import LumenBridgePlugin  # noqa: E402

if LumenBridgePlugin.api_version != "0.11":
    raise SystemExit("unexpected plugin API declaration")

sha256 = hashlib.sha256(ARTIFACT.read_bytes()).hexdigest()
print(json.dumps({
    "official_artifact": ARTIFACT.name,
    "copied_to": str(deployed),
    "deployment_copy_matches": hashlib.sha256(deployed.read_bytes()).hexdigest() == sha256,
    "distribution_name_ok": True,
    "endstone_entry_point_ok": True,
    "plugin_imported_from_wheel": LumenBridgePlugin.__module__,
    "plugin_api_version": LumenBridgePlugin.api_version,
    "sha256": sha256,
}, ensure_ascii=False, indent=2))
