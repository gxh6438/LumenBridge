from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "src" / "endstone_lumenbridge" / "webui" / "static"
HTML = (STATIC / "index.html").read_text(encoding="utf-8")
APP = (STATIC / "app.js").read_text(encoding="utf-8")
# 样式已从 index.html 内联拆分为共享层(lumen.css) + 主面板层(app.css)，
# CSS 契约断言统一指向两层文件的内容（HTML 中仅保留 <link> 引用）
CSS = "\n".join((STATIC / name).read_text(encoding="utf-8") for name in ("lumen.css", "app.css"))
LIQUID_JS = STATIC / "liquid-glass.js"
VENDOR_LIQUID_JS = STATIC / "vendor" / "liquidglass-1.0.3.js"


class WebUiDesignContractTests(unittest.TestCase):
    def test_liquid_glass_is_removed_and_frosted_glass_remains(self) -> None:
        # 液态玻璃 WebGL 层已完全移除
        self.assertFalse(LIQUID_JS.is_file())
        self.assertFalse(VENDOR_LIQUID_JS.is_file())
        self.assertNotIn("liquid-glass.js", HTML)
        self.assertNotIn("liquid-glass.js", CSS)
        self.assertNotIn("liquid-glass-surface", HTML)
        self.assertNotIn("data-liquid-glass", HTML)
        self.assertNotIn("webgl-liquid-glass-ready", HTML)
        self.assertNotIn("LumenLiquidGlass", APP)
        # CSS 毛玻璃（backdrop-filter）仍保留
        self.assertIn(".glass", CSS)
        self.assertIn("backdrop-filter:", CSS)
        self.assertNotIn("body.has-bg #bg-layer", CSS)
        # index.html 通过两层 link 引用外部样式（lumen.css 共享层 + app.css 主面板层）
        self.assertIn('href="/lumen.css"', HTML)
        self.assertIn('href="/app.css"', HTML)

    def test_package_cards_use_neutral_marker_not_colored_letter_avatars(self) -> None:
        self.assertIn('class="pkg-marker"', APP)
        self.assertNotIn("pkg-avatar.c", CSS)
        self.assertNotIn("colorIdx", APP)

    def test_rule_title_and_badges_have_independent_responsive_regions(self) -> None:
        self.assertIn('class="rule-badges"', APP)
        self.assertIn(".rule-head .rule-badges", CSS)
        self.assertIn("flex-direction: column", CSS)
        self.assertIn("overflow-wrap: anywhere", CSS)

    def test_nested_config_i18n_merge_is_recursive(self) -> None:
        self.assertIn("const mergeNode = (node, path)", APP)
        self.assertIn('mergeNode(value, path + "." + key)', APP)
        self.assertIn("commands.status.allow_player", APP)


if __name__ == "__main__":
    unittest.main()
