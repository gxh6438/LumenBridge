from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "src" / "endstone_lumenbridge" / "webui" / "static" / "index.html").read_text(encoding="utf-8")
APP = (ROOT / "src" / "endstone_lumenbridge" / "webui" / "static" / "app.js").read_text(encoding="utf-8")
LIQUID_JS = ROOT / "src" / "endstone_lumenbridge" / "webui" / "static" / "liquid-glass.js"
VENDOR_LIQUID_JS = ROOT / "src" / "endstone_lumenbridge" / "webui" / "static" / "vendor" / "liquidglass-1.0.3.js"


class WebUiDesignContractTests(unittest.TestCase):
    def test_liquid_glass_is_removed_and_frosted_glass_remains(self) -> None:
        # 液态玻璃 WebGL 层已完全移除
        self.assertFalse(LIQUID_JS.is_file())
        self.assertFalse(VENDOR_LIQUID_JS.is_file())
        self.assertNotIn("liquid-glass.js", HTML)
        self.assertNotIn("liquid-glass-surface", HTML)
        self.assertNotIn("data-liquid-glass", HTML)
        self.assertNotIn("webgl-liquid-glass-ready", HTML)
        self.assertNotIn("LumenLiquidGlass", APP)
        # CSS 毛玻璃（backdrop-filter）仍保留
        self.assertIn(".glass", HTML)
        self.assertIn("backdrop-filter:", HTML)
        self.assertNotIn("body.has-bg #bg-layer", HTML)

    def test_package_cards_use_neutral_marker_not_colored_letter_avatars(self) -> None:
        self.assertIn('class="pkg-marker"', APP)
        self.assertNotIn("pkg-avatar.c", HTML)
        self.assertNotIn("colorIdx", APP)

    def test_rule_title_and_badges_have_independent_responsive_regions(self) -> None:
        self.assertIn('class="rule-badges"', APP)
        self.assertIn(".rule-head .rule-badges", HTML)
        self.assertIn("flex-direction: column", HTML)
        self.assertIn("overflow-wrap: anywhere", HTML)

    def test_nested_config_i18n_merge_is_recursive(self) -> None:
        self.assertIn("const mergeNode = (node, path)", APP)
        self.assertIn('mergeNode(value, path + "." + key)', APP)
        self.assertIn("commands.status.allow_player", APP)


if __name__ == "__main__":
    unittest.main()
