from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from endstone_lumenbridge.config import DEFAULT_CONFIG

LOCALES = ROOT / "src" / "endstone_lumenbridge" / "locales"


def leaf_paths(value: Any, prefix: str = "") -> list[str]:
    if isinstance(value, dict):
        paths: list[str] = []
        for key, child in value.items():
            paths.extend(leaf_paths(child, f"{prefix}.{key}" if prefix else key))
        return paths
    return [prefix]


def get_nested(root: dict[str, Any], path: str) -> Any:
    value: Any = root
    for key in path.split("."):
        if not isinstance(value, dict) or key not in value:
            return None
        value = value[key]
    return value


paths = leaf_paths(DEFAULT_CONFIG)
result: dict[str, Any] = {"paths": paths, "languages": {}}
for code in ("en", "zh_CN", "zh_TW"):
    locale = json.loads((LOCALES / f"{code}.json").read_text(encoding="utf-8"))
    labels = locale.get("config_labels", {})
    missing: list[str] = []
    incomplete: list[str] = []
    for path in paths:
        item = get_nested(labels, path)
        if not isinstance(item, dict):
            missing.append(path)
        elif not isinstance(item.get("label"), str) or not item["label"].strip() or not isinstance(item.get("desc"), str) or not item["desc"].strip():
            incomplete.append(path)
    result["languages"][code] = {
        "covered": len(paths) - len(missing) - len(incomplete),
        "total": len(paths),
        "missing": missing,
        "incomplete": incomplete,
    }

print(json.dumps(result, ensure_ascii=False, indent=2))
