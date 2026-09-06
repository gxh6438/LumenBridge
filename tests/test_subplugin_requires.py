"""子插件插件级强制依赖（requires）功能测试。

覆盖三层：
1. requires.py 纯解析：约束语法 / 版本比较 / 宽容容错；
2. loader 集成：加载阻断、拓扑排序、循环依赖、反向依赖；
3. marketplace 自动补装：市场匹配、版本选择、递归与环检测。
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from endstone_lumenbridge.config import ConfigManager
from endstone_lumenbridge.marketplace import MarketplaceClient, _MAX_REQUIREMENT_DEPTH
from endstone_lumenbridge.subplugin.context import EnvPool
from endstone_lumenbridge.subplugin.loader import SubPluginManager
from endstone_lumenbridge.subplugin.requires import (
    PluginRequirement,
    check_endstone_requirements,
    parse_requirement,
    parse_requires,
    parse_requires_from_manifest,
    version_tuple,
)


# ----------------------------------------------------------------------
# requires.py：纯解析与比较
# ----------------------------------------------------------------------
class ParseRequirementTests(unittest.TestCase):
    def test_name_only_requires_existence(self) -> None:
        req = parse_requirement("economy")
        self.assertIsNotNone(req)
        self.assertEqual(req.name, "economy")
        self.assertEqual(req.op, "")
        self.assertEqual(req.version, "")
        self.assertTrue(req.satisfied_by("0.0.1"))  # 仅要求存在

    def test_all_operators_parsed(self) -> None:
        for op in (">=", "<=", ">", "<", "==", "!="):
            req = parse_requirement(f"economy{op}1.2.0")
            self.assertIsNotNone(req, op)
            self.assertEqual(req.op, op)
            self.assertEqual(req.version, "1.2.0")

    def test_operator_without_version_is_invalid(self) -> None:
        self.assertIsNone(parse_requirement("economy>="))
        self.assertIsNone(parse_requirement("economy=="))

    def test_illegal_names_rejected(self) -> None:
        for bad in ("", "has space", "bad/name", "bad\\name", "点", "a" * 65, 123, None, ["x"]):
            self.assertIsNone(parse_requirement(bad), repr(bad))

    def test_version_with_build_segments_accepted(self) -> None:
        req = parse_requirement("economy>=1.2.3-beta+build")
        self.assertIsNotNone(req)
        self.assertEqual(req.version, "1.2.3-beta+build")

    def test_display_prefers_raw(self) -> None:
        req = parse_requirement("  economy>=1.2.0 ")
        self.assertEqual(req.display(), "economy>=1.2.0")
        self.assertEqual(PluginRequirement(name="x").display(), "x")

    def test_describe_unmet_includes_current_version(self) -> None:
        req = parse_requirement("economy>=1.2.0")
        self.assertEqual(req.describe_unmet(), "economy>=1.2.0")
        self.assertEqual(req.describe_unmet("1.0.0"), "economy>=1.2.0 (当前 v1.0.0)")


class VersionTupleTests(unittest.TestCase):
    def test_basic(self) -> None:
        self.assertEqual(version_tuple("1.2.3"), (1, 2, 3))
        self.assertEqual(version_tuple("v1.2"), (1, 2))
        self.assertEqual(version_tuple("V2.0.0"), (2, 0, 0))

    def test_loose_segments(self) -> None:
        # 每段仅取前导数字
        self.assertEqual(version_tuple("1.2.3a"), (1, 2, 3))
        self.assertEqual(version_tuple("1.2.3-beta"), (1, 2, 3))

    def test_empty_and_none(self) -> None:
        self.assertEqual(version_tuple(""), (0,))
        self.assertEqual(version_tuple(None), (0,))


class SatisfiedByTests(unittest.TestCase):
    def test_operators(self) -> None:
        cases = [
            (">=", "1.2.0", "1.2.0", True),
            (">=", "1.2.0", "1.1.9", False),
            ("<=", "1.2.0", "1.2.1", False),
            (">", "1.0", "1.0.0", True),  # 宽松元组比较：(1,0,0) > (1,0)
            (">", "1.0", "1.0.1", True),
            ("<", "2.0", "1.9.9", True),
            ("==", "1.2.0", "1.2.0", True),
            ("==", "1.2.0", "1.2", False),  # 宽松元组比较：(1,2,0) != (1,2)
            ("==", "1.2.0", "1.2.1", False),
            ("!=", "1.2.0", "1.2.1", True),
            ("!=", "1.2.0", "1.2.0", False),
        ]
        for op, required, actual, expected in cases:
            req = parse_requirement(f"x{op}{required}")
            self.assertEqual(req.satisfied_by(actual), expected, f"{op} {required} vs {actual}")


class ParseRequiresTests(unittest.TestCase):
    def test_none_and_missing(self) -> None:
        self.assertTrue(parse_requires(None).empty)

    def test_list_shorthand_is_subplugins(self) -> None:
        decl = parse_requires(["a", "b>=1.0"])
        self.assertEqual([r.name for r in decl.subplugins], ["a", "b"])
        self.assertEqual(decl.endstone, [])
        self.assertFalse(decl.empty)
        self.assertEqual(decl.subplugin_names(), {"a", "b"})

    def test_dict_form(self) -> None:
        decl = parse_requires({"subplugins": ["a"], "endstone": ["es>=2.0"]})
        self.assertEqual([r.name for r in decl.subplugins], ["a"])
        self.assertEqual([r.name for r in decl.endstone], ["es"])

    def test_invalid_items_collected_not_raised(self) -> None:
        decl = parse_requires({"subplugins": ["ok", 123, "bad name", "x>="], "endstone": "not-a-list"})
        self.assertEqual([r.name for r in decl.subplugins], ["ok"])
        self.assertEqual(decl.invalid, ["123", "bad name", "x>=", "not-a-list"])

    def test_malformed_toplevel(self) -> None:
        decl = parse_requires("just-a-string")
        self.assertTrue(decl.empty)
        self.assertEqual(decl.invalid, ["just-a-string"])

    def test_from_manifest(self) -> None:
        decl = parse_requires_from_manifest({"requires": ["a"]})
        self.assertEqual([r.name for r in decl.subplugins], ["a"])
        # 非 dict manifest / 缺 requires → 空声明
        self.assertTrue(parse_requires_from_manifest(None).empty)
        self.assertTrue(parse_requires_from_manifest("nope").empty)
        self.assertTrue(parse_requires_from_manifest({}).empty)


class CheckEndstoneRequirementsTests(unittest.TestCase):
    def test_case_insensitive_match(self) -> None:
        req = parse_requirement("Economy")
        unmet = check_endstone_requirements([req], {"economy": "1.0.0"})
        self.assertEqual(unmet, [])

    def test_missing_reported_with_empty_actual(self) -> None:
        req = parse_requirement("economy>=1.0")
        unmet = check_endstone_requirements([req], {})
        self.assertEqual(len(unmet), 1)
        self.assertIs(unmet[0][0], req)
        self.assertEqual(unmet[0][1], "")

    def test_version_mismatch_reported(self) -> None:
        req = parse_requirement("economy>=2.0")
        unmet = check_endstone_requirements([req], {"economy": "1.0"})
        self.assertEqual(len(unmet), 1)
        self.assertEqual(unmet[0][1], "1.0")

    def test_installed_without_version_satisfies_name_only(self) -> None:
        req = parse_requirement("economy")  # 仅要求存在
        self.assertEqual(check_endstone_requirements([req], {"economy": ""}), [])


# ----------------------------------------------------------------------
# loader 集成：安装 / 加载 / 拓扑 / 反向依赖
# ----------------------------------------------------------------------
class DummyLogger:
    def info(self, _message: object) -> None:
        pass

    def warning(self, _message: object) -> None:
        pass

    def error(self, _message: object) -> None:
        pass

    def debug(self, _message: object) -> None:
        pass


class _FakeEndstonePlugin:
    def __init__(self, name: str, version: str) -> None:
        self.name = name
        self.version = version


class DummyPlugin:
    """测试替身：无 server 属性 → _installed_endstone_plugins 返回 {}。"""

    def __init__(self, data_folder: Path) -> None:
        self.data_folder = data_folder
        self.logger = DummyLogger()
        self._tee_logger = self.logger
        self.config_manager = ConfigManager(data_folder, self.logger)
        self.adapter = None
        self.env_pool = EnvPool(self)
        self.webui = None
        self.regex_module = None
        self.endstone_plugins: list[_FakeEndstonePlugin] | None = None

    @property
    def server(self):
        plugins = self.endstone_plugins
        if plugins is None:
            raise AttributeError("no server")
        return SimpleNamespace(
            plugin_manager=SimpleNamespace(get_plugins=lambda: plugins)
        )

    def _get_pip_manager(self):
        return None


class RequiresLoaderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.data = Path(self.tempdir.name)
        self.plugin = DummyPlugin(self.data)
        self.manager = SubPluginManager(self.plugin)
        self.addCleanup(self.tempdir.cleanup)

    def make_zip(
        self,
        name: str,
        version: str = "1.0.0",
        requires: object = None,
        main_code: str = "def on_load(lumen):\n    pass\n",
    ) -> Path:
        manifest: dict = {"name": name, "version": version, "load": True, "dependencies": []}
        if requires is not None:
            manifest["requires"] = requires
        archive = self.data / f"{name}.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("lumen.json", json.dumps(manifest))
            zf.writestr("main.py", main_code)
        return archive

    def install(self, name: str, version: str = "1.0.0", requires: object = None) -> str:
        ok, _message, installed_name = self.manager.install_from_zip(
            self.make_zip(name, version, requires)
        )
        self.assertTrue(ok, f"install {name} failed")
        return installed_name

    def test_subplugin_requirement_satisfied_loads(self) -> None:
        self.install("economy", "1.2.0")
        self.install("shop", requires={"subplugins": ["economy>=1.2.0"]})
        self.assertTrue(self.manager.subplugins["shop"].loaded)

    def test_missing_subplugin_requirement_blocks_load(self) -> None:
        self.install("shop", requires={"subplugins": ["economy>=1.2.0"]})
        sp = self.manager.subplugins["shop"]
        self.assertFalse(sp.loaded)
        self.assertIn("economy>=1.2.0", sp.error)
        self.assertEqual(sp.missing_requirements, ["economy>=1.2.0"])

    def test_unmet_version_constraint_blocks_load(self) -> None:
        self.install("economy", "1.0.0")
        self.install("shop", requires={"subplugins": ["economy>=2.0.0"]})
        sp = self.manager.subplugins["shop"]
        self.assertFalse(sp.loaded)
        self.assertIn("2.0.0", sp.error)
        self.assertIn("1.0.0", sp.error)
        # 描述含当前版本
        self.assertTrue(any("1.0.0" in item for item in sp.missing_requirements))

    def test_disabled_dependency_blocks_load(self) -> None:
        self.install("economy")
        self.install("shop", requires={"subplugins": ["economy"]})
        # 手动禁用 economy 后重载全部：economy 不再 loaded → shop 被阻断
        manifest_path = self.manager.plugins_dir / "economy" / "lumen.json"
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        data["load"] = False
        manifest_path.write_text(json.dumps(data), encoding="utf-8")
        self.manager.load_all()
        self.assertFalse(self.manager.subplugins["economy"].loaded)
        self.assertFalse(self.manager.subplugins["shop"].loaded)

    def test_endstone_requirement_missing_prompts_install(self) -> None:
        self.plugin.endstone_plugins = [_FakeEndstonePlugin("other", "1.0.0")]
        self.install("shop", requires={"endstone": ["economy-es>=2.0"]})
        sp = self.manager.subplugins["shop"]
        self.assertFalse(sp.loaded)
        self.assertIn("economy-es>=2.0", sp.error)
        self.assertIn("安装", sp.error)  # 提示用户安装

    def test_endstone_requirement_satisfied_loads(self) -> None:
        self.plugin.endstone_plugins = [_FakeEndstonePlugin("Economy-ES", "2.1.0")]
        self.install("shop", requires={"endstone": ["economy-es>=2.0"]})
        self.assertTrue(self.manager.subplugins["shop"].loaded)

    def test_endstone_uncheckable_when_server_unavailable(self) -> None:
        # server 不可用 → 无法核实 → 不阻断（宽容口径）
        self.install("shop", requires={"endstone": ["anything"]})
        self.assertTrue(self.manager.subplugins["shop"].loaded)

    def test_list_shorthand_requires(self) -> None:
        self.install("economy")
        self.install("shop", requires=["economy"])  # 数组简写 = subplugins
        self.assertTrue(self.manager.subplugins["shop"].loaded)

    def test_invalid_requires_entries_do_not_block(self) -> None:
        self.install("shop", requires={"subplugins": ["!!!", 42]})
        sp = self.manager.subplugins["shop"]
        self.assertTrue(sp.loaded)  # 无效项忽略，不阻断
        self.assertEqual(sp.missing_requirements, [])

    def test_self_requirement_ignored(self) -> None:
        self.install("shop", requires={"subplugins": ["shop"]})
        self.assertTrue(self.manager.subplugins["shop"].loaded)

    def test_topological_order_loads_dependency_first(self) -> None:
        # a_shop（字典序靠前）依赖 z_economy（靠后）：不排序会先加载 a_shop 而误报
        self.install("z_economy")
        self.install("a_shop", requires={"subplugins": ["z_economy"]})
        self.manager.load_all()
        self.assertTrue(self.manager.subplugins["z_economy"].loaded)
        self.assertTrue(self.manager.subplugins["a_shop"].loaded)

    def test_circular_dependency_reports_both(self) -> None:
        self.install("a_one", requires={"subplugins": ["b_two"]})
        self.install("b_two", requires={"subplugins": ["a_one"]})
        self.manager.load_all()
        a = self.manager.subplugins["a_one"]
        b = self.manager.subplugins["b_two"]
        self.assertFalse(a.loaded)
        self.assertFalse(b.loaded)
        # 各自报出对方的缺失/未加载
        self.assertTrue(
            ("b_two" in a.error) or ("a_one" in b.error),
            f"a: {a.error!r} b: {b.error!r}",
        )

    def test_dependents_of_lists_reverse_deps(self) -> None:
        self.install("economy")
        self.install("shop", requires={"subplugins": ["economy>=1.0.0"]})
        dependents = self.manager.dependents_of("economy")
        self.assertEqual(dependents, [{"name": "shop", "loaded": True, "req": "economy>=1.0.0"}])
        self.assertEqual(self.manager.dependents_of("shop"), [])

    def test_status_lines_expose_missing_requirements(self) -> None:
        self.install("shop", requires={"subplugins": ["economy"]})
        lines = self.manager.status_lines()
        shop = next(item for item in lines if item["name"] == "shop")
        self.assertFalse(shop["loaded"])
        self.assertEqual(shop["missing_requirements"], ["economy"])
        text = "\n".join(self.manager.status_text_lines())
        self.assertIn("shop", text)
        self.assertIn("economy", text)

    def test_reload_after_dependency_installed(self) -> None:
        self.install("shop", requires={"subplugins": ["economy>=1.0.0"]})
        self.assertFalse(self.manager.subplugins["shop"].loaded)
        self.install("economy", "1.1.0")
        self.assertTrue(self.manager.reload_one("shop"))
        self.assertTrue(self.manager.subplugins["shop"].loaded)


# ----------------------------------------------------------------------
# marketplace：requires 自动补装（不触网，全部 mock）
# ----------------------------------------------------------------------
class _MarketStubPlugin(DummyPlugin):
    """供 MarketplaceClient 使用的替身：同步执行主线程任务。"""

    def __init__(self, data_folder: Path, manager: SubPluginManager) -> None:
        super().__init__(data_folder)
        self.subplugin_manager = manager
        self._pip_serial_lock = __import__("threading").RLock()

    def is_on_main_thread(self) -> bool:
        return True

    def run_on_main(self, fn):
        return fn()


class RequiresMarketplaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.data = Path(self.tempdir.name)
        # 同一个插件对象同时充当 loader 宿主与 market 宿主：
        # manager 的 requires 检查与 client 的依赖补装看到同一份 server 状态
        self.plugin = _MarketStubPlugin(self.data, manager=None)
        self.manager = SubPluginManager(self.plugin)
        self.plugin.subplugin_manager = self.manager
        config = {
            "marketplace": {"enable": True, "api_url": "http://market.test", "allow_http": True},
        }
        self.plugin.config_manager = SimpleNamespace(data=config)
        self.client = MarketplaceClient(self.plugin)
        self.addCleanup(self.tempdir.cleanup)

    def make_zip(self, name: str, version: str, requires: object = None) -> Path:
        manifest: dict = {"name": name, "version": version, "load": True, "dependencies": []}
        if requires is not None:
            manifest["requires"] = requires
        archive = self.data / f"{name}.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("lumen.json", json.dumps(manifest))
            zf.writestr("main.py", "def on_load(lumen):\n    pass\n")
        return archive

    def install_local(self, name: str, version: str, requires: object = None) -> None:
        ok, _msg, _n = self.manager.install_from_zip(self.make_zip(name, version, requires))
        self.assertTrue(ok)

    def test_noop_when_all_satisfied(self) -> None:
        self.install_local("economy", "1.2.0")
        self.install_local("shop", "1.0.0", requires={"subplugins": ["economy>=1.0.0"]})
        result = self.client.install_plugin_requirements("shop")
        self.assertTrue(result["ok"])
        self.assertEqual(result["installed"], [])
        self.assertEqual(result["missing"], [])

    def test_no_requires_declaration(self) -> None:
        self.install_local("plain", "1.0.0")
        result = self.client.install_plugin_requirements("plain")
        self.assertTrue(result["ok"])
        self.assertEqual(result["message"], "")

    def test_endstone_missing_reported(self) -> None:
        self.plugin.endstone_plugins = []
        self.install_local("shop", "1.0.0", requires={"endstone": ["some-es>=2.0"]})
        result = self.client.install_plugin_requirements("shop")
        self.assertFalse(result["ok"])
        self.assertEqual(result["endstone_missing"], ["some-es>=2.0"])
        self.assertIn("some-es>=2.0", result["message"])
        self.assertIn("Endstone", result["message"])

    def test_subplugin_dependency_installed_from_market(self) -> None:
        # shop 缺 economy → 市场按 manifest_name 找到并安装
        self.install_local("shop", "1.0.0", requires={"subplugins": ["economy>=1.2.0"]})
        dep_zip = self.make_zip("economy", "1.3.0")

        detail = {
            "id": "econ-pkg",
            "manifest_name": "economy",
            "versions": [
                {"version": "1.1.0", "download_url": "http://x/1.zip", "sha256": "0" * 64},
                {"version": "1.3.0", "download_url": "http://x/2.zip", "sha256": "0" * 64},
            ],
        }
        with patch.object(
            self.client, "_find_market_plugin_by_manifest_name", return_value=detail
        ), patch.object(
            self.client, "install", side_effect=lambda *a, **k: self.manager.install_from_zip(dep_zip)
        ) as mock_install:
            result = self.client.install_plugin_requirements("shop")

        self.assertTrue(result["ok"], result["message"])
        self.assertEqual(result["installed"], ["economy"])
        self.assertTrue(self.manager.subplugins["economy"].loaded)
        self.assertTrue(self.manager.subplugins["shop"].loaded)  # 热重载后通过检查
        mock_install.assert_called_once()

    def test_missing_from_market_reported(self) -> None:
        self.install_local("shop", "1.0.0", requires={"subplugins": ["economy"]})
        with patch.object(self.client, "_find_market_plugin_by_manifest_name", return_value=None):
            result = self.client.install_plugin_requirements("shop")
        self.assertFalse(result["ok"])
        self.assertTrue(any("没有找到" in item for item in result["missing"]))

    def test_market_versions_all_unsatisfying(self) -> None:
        self.install_local("shop", "1.0.0", requires={"subplugins": ["economy>=3.0.0"]})
        detail = {
            "id": "econ-pkg",
            "manifest_name": "economy",
            "versions": [{"version": "1.0.0", "download_url": "http://x/1.zip", "sha256": "0" * 64}],
        }
        with patch.object(self.client, "_find_market_plugin_by_manifest_name", return_value=detail):
            result = self.client.install_plugin_requirements("shop")
        self.assertFalse(result["ok"])
        self.assertTrue(any("不满足" in item for item in result["missing"]))

    def test_installed_but_unloaded_dependency_reported(self) -> None:
        # economy 已安装但禁用 → 提示用户处理而非重装
        self.install_local("economy", "1.0.0")
        manifest_path = self.manager.plugins_dir / "economy" / "lumen.json"
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        data["load"] = False
        manifest_path.write_text(json.dumps(data), encoding="utf-8")
        self.manager.load_all()
        self.install_local("shop", "1.0.0", requires={"subplugins": ["economy"]})
        result = self.client.install_plugin_requirements("shop")
        self.assertFalse(result["ok"])
        self.assertTrue(any("未加载" in item for item in result["missing"]))

    def test_cycle_detected_via_dep_chain(self) -> None:
        self.install_local("a_one", "1.0.0", requires={"subplugins": ["b_two"]})
        # 递归链中已含 b_two（模拟 a→b→a 环）
        result = self.client.install_plugin_requirements(
            "a_one", _dep_depth=2, _dep_chain=("x", "b_two")
        )
        self.assertFalse(result["ok"])
        self.assertTrue(any("循环依赖" in item for item in result["missing"]))

    def test_depth_limit_enforced(self) -> None:
        self.install_local("shop", "1.0.0", requires={"subplugins": ["economy"]})
        result = self.client.install_plugin_requirements(
            "shop", _dep_depth=_MAX_REQUIREMENT_DEPTH + 1
        )
        self.assertFalse(result["ok"])
        self.assertIn("层级过深", result["message"])

    def test_select_release_picks_highest_satisfying(self) -> None:
        detail = {
            "versions": [
                {"version": "1.0.0", "download_url": "u", "sha256": "s"},
                {"version": "1.5.0", "download_url": "u", "sha256": "s"},
                {"version": "2.0.0", "download_url": "u", "sha256": "s"},
            ]
        }
        req = parse_requirement("economy>=1.2.0")
        release = MarketplaceClient._select_release_for_requirement(detail, req)
        self.assertEqual(release["version"], "2.0.0")

    def test_select_release_skips_incomplete_entries(self) -> None:
        detail = {
            "versions": [
                {"version": "1.0.0"},  # 无下载地址
                {"version": "1.5.0", "download_url": "u"},  # 无 sha256
                {"bad-version!", "x"},  # 非 dict
                {"version": "1.4.0", "download_url": "u", "sha256": "s"},
            ]
        }
        req = parse_requirement("economy")
        release = MarketplaceClient._select_release_for_requirement(detail, req)
        self.assertEqual(release["version"], "1.4.0")

    def test_select_release_none_when_unsatisfiable(self) -> None:
        detail = {"versions": [{"version": "1.0.0", "download_url": "u", "sha256": "s"}]}
        req = parse_requirement("economy>=3.0.0")
        self.assertIsNone(MarketplaceClient._select_release_for_requirement(detail, req))

    def test_find_market_plugin_filters_by_manifest_name(self) -> None:
        # 旧服务端忽略 manifest_name 参数返回未过滤列表：客户端必须逐项核对
        responses = [
            {"items": [
                {"id": "other-pkg", "manifest_name": "economy-pro"},
                {"id": "econ-pkg", "manifest_name": "economy"},
            ]},
        ]
        detail = {"id": "econ-pkg", "manifest_name": "economy", "versions": []}
        with patch.object(
            self.client, "_request_json", side_effect=responses
        ), patch.object(self.client, "plugin_detail", return_value=detail) as pd:
            found = self.client._find_market_plugin_by_manifest_name("economy")
        self.assertIs(found, detail)
        pd.assert_called_once_with("econ-pkg")

    def test_find_market_plugin_none_when_no_match(self) -> None:
        with patch.object(
            self.client, "_request_json", return_value={"items": [{"id": "x-pkg", "manifest_name": "other"}]}
        ):
            self.assertIsNone(self.client._find_market_plugin_by_manifest_name("economy"))


if __name__ == "__main__":
    unittest.main()
