from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from endstone_lumenbridge.pip_manager import PipManager

PipManager.refresh_dependency_cache()
if not PipManager.check_dependency("hello_pip>=1.0"):
    raise SystemExit("hello_pip was installed but LumenBridge still marked it missing")

print("Real hello_pip distribution/import detection passed.")
