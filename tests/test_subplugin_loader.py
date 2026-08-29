from __future__ import annotations

import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from endstone_lumenbridge.config import ConfigManager
from endstone_lumenbridge.subplugin import context as context_module
from endstone_lumenbridge.subplugin.context import (
    EnvPool,
    LumenContext,
    default_usage,
    merge_command_palette_into,
    plugin_register_command_compat,
    sanitize_usages,
)
from endstone_lumenbridge.subplugin.loader import SubPluginManager


class DummyLogger:
    def info(self, _message: object) -> None:
        pass

    def warning(self, _message: object) -> None:
        pass

    def error(self, _message: object) -> None:
        pass

    def debug(self, _message: object) -> None:
        pass


class DummyPlugin:
    def __init__(self, data_folder: Path) -> None:
        self.data_folder = data_folder
        self.logger = DummyLogger()
        self._tee_logger = self.logger
        self.config_manager = ConfigManager(data_folder, self.logger)
        self.adapter = None
        self.env_pool = EnvPool(self)
        self.webui = None
        self.regex_module = None

    def _get_pip_manager(self):
        return None


class SubPluginLoaderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.data = Path(self.tempdir.name)
        self.plugin = DummyPlugin(self.data)
        self.manager = SubPluginManager(self.plugin)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def make_zip(self, name: str, version: str, main_code: str, extra: dict[str, str] | None = None) -> Path:
        archive = self.data / f"{name}-{version}.zip"
        manifest = {"name": name, "version": version, "load": True, "dependencies": []}
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("lumen.json", json.dumps(manifest))
            zf.writestr("main.py", main_code)
            for path, content in (extra or {}).items():
                zf.writestr(path, content)
        return archive

    def test_valid_zip_installs_and_loads_plugin(self) -> None:
        archive = self.make_zip(
            "hello_plugin", "1.0.0",
            "def on_load(lumen):\n    lumen.storage.write('state.json', {'loaded': True})\n",
        )
        ok, _message, name = self.manager.install_from_zip(archive)
        self.assertTrue(ok)
        self.assertEqual(name, "hello_plugin")
        self.assertTrue(self.manager.subplugins[name].loaded)
        self.assertTrue((self.manager.plugins_dir / name / "state.json").is_file())

    def test_plugin_load_failure_is_reported_as_installed_but_not_loaded(self) -> None:
        archive = self.make_zip("broken_plugin", "1.0.0", "def on_load(lumen):\n    raise RuntimeError('boom')\n")
        ok, message, name = self.manager.install_from_zip(archive)
        self.assertTrue(ok)
        self.assertEqual(name, "broken_plugin")
        self.assertFalse(self.manager.subplugins[name].loaded)
        self.assertTrue(self.manager.subplugins[name].error)
        self.assertIn("broken_plugin", message)

    def test_reload_one_executes_replaced_plugin_code(self) -> None:
        archive = self.make_zip(
            "reload_one_demo", "1.0.0",
            "def on_load(lumen):\n    lumen.storage.write('state.json', {'phase': 'v1'})\n",
        )
        ok, _message, name = self.manager.install_from_zip(archive)
        self.assertTrue(ok)
        main = self.manager.plugins_dir / name / "main.py"
        main.write_text("def on_load(lumen):\n    lumen.storage.write('state.json', {'phase': 'v2'})\n", encoding="utf-8")
        self.assertTrue(self.manager.reload_one(name))
        state = json.loads((self.manager.plugins_dir / name / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["phase"], "v2")

    def test_reload_all_executes_replaced_plugin_code(self) -> None:
        archive = self.make_zip(
            "reload_all_demo", "1.0.0",
            "def on_load(lumen):\n    lumen.storage.write('state.json', {'phase': 'v1'})\n",
        )
        ok, _message, name = self.manager.install_from_zip(archive)
        self.assertTrue(ok)
        main = self.manager.plugins_dir / name / "main.py"
        main.write_text("def on_load(lumen):\n    lumen.storage.write('state.json', {'phase': 'v3'})\n", encoding="utf-8")
        self.assertEqual(self.manager.reload_all(), 1)
        state = json.loads((self.manager.plugins_dir / name / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["phase"], "v3")

    def test_loading_does_not_leave_pycache_in_plugin_folders(self) -> None:
        # v1.2.5：加载期间禁写字节码 + 加载前后清理，子插件目录不再残留 __pycache__
        archive = self.make_zip("pycache_demo", "1.0.0", "def on_load(lumen):\n    pass\n")
        ok, _message, name = self.manager.install_from_zip(archive)
        self.assertTrue(ok)
        folder = self.manager.plugins_dir / name
        self.assertFalse((folder / "__pycache__").exists())

        # 预置历史遗留缓存（含嵌套）也应被清理
        stale = folder / "__pycache__"
        nested = folder / "helper" / "__pycache__"
        stale.mkdir(parents=True)
        (stale / "main.cpython-311.pyc").write_bytes(b"stale")
        nested.mkdir(parents=True)
        (nested / "util.cpython-311.pyc").write_bytes(b"stale")
        self.assertTrue(self.manager.reload_one(name))
        self.assertFalse(stale.exists())
        self.assertFalse(nested.exists())

    def test_load_all_purges_pycache_of_disabled_plugins(self) -> None:
        archive = self.make_zip("disabled_demo", "1.0.0", "def on_load(lumen):\n    pass\n")
        ok, _message, name = self.manager.install_from_zip(archive)
        self.assertTrue(ok)
        manifest = self.manager.plugins_dir / name / "lumen.json"
        data = json.loads(manifest.read_text(encoding="utf-8"))
        data["load"] = False
        manifest.write_text(json.dumps(data), encoding="utf-8")
        stale = self.manager.plugins_dir / name / "__pycache__"
        stale.mkdir()
        (stale / "main.cpython-311.pyc").write_bytes(b"stale")
        self.manager.load_all()
        self.assertFalse(stale.exists())

    def test_zip_path_traversal_is_rejected_before_extraction(self) -> None:
        archive = self.data / "traversal.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("../escape.txt", "should not escape")
            zf.writestr("main.py", "def on_load(lumen):\n    pass\n")
        ok, _message, _name = self.manager.install_from_zip(archive)
        self.assertFalse(ok)
        self.assertFalse((self.data / "escape.txt").exists())


class MinVersionGateTests(unittest.TestCase):
    """lumen.json 的 min_v 版本闸：安装期 + 加载期双闸。"""

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.data = Path(self.tempdir.name)
        self.plugin = DummyPlugin(self.data)
        self.manager = SubPluginManager(self.plugin)
        self.addCleanup(self.tempdir.cleanup)

    def make_zip(self, name: str, min_v: str) -> Path:
        manifest = {"name": name, "version": "1.0.0", "load": True, "min_v": min_v}
        archive = self.data / f"{name}.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("lumen.json", json.dumps(manifest))
            zf.writestr("main.py", "def on_load(lumen):\n    pass\n")
        return archive

    def test_install_rejected_when_min_v_above_current(self) -> None:
        from endstone_lumenbridge import __version__
        ok, message, _name = self.manager.install_from_zip(self.make_zip("needs_new", "999.0.0"))
        self.assertFalse(ok)
        self.assertIn(__version__, message)
        self.assertIn("999.0.0", message)

    def test_install_accepted_when_min_v_satisfied(self) -> None:
        ok, _message, name = self.manager.install_from_zip(self.make_zip("ok_min", "1.0.0"))
        self.assertTrue(ok)
        self.assertTrue(self.manager.subplugins[name].loaded)

    def test_load_gate_blocks_hand_edited_manifest(self) -> None:
        # 手改已装子插件的 min_v（绕过安装闸）→ 加载期兜底拒绝
        ok, _message, name = self.manager.install_from_zip(self.make_zip("hand_edit", "1.0.0"))
        self.assertTrue(ok)
        manifest_path = self.manager.plugins_dir / name / "lumen.json"
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        data["min_v"] = "999.0.0"
        manifest_path.write_text(json.dumps(data), encoding="utf-8")
        self.assertFalse(self.manager.reload_one(name))
        sp = self.manager.subplugins[name]
        self.assertFalse(sp.loaded)
        self.assertIn("999.0.0", sp.error)

    def test_malformed_min_v_is_ignored(self) -> None:
        # 畸形 min_v（空串/纯空白/非数字）宽松容错，不阻断安装与加载
        for idx, bad in enumerate(("", "  ", "abc")):
            ok, _message, name = self.manager.install_from_zip(self.make_zip(f"tol{idx}", bad))
            self.assertTrue(ok)


class _CompatPlugin(DummyPlugin):
    """带 register_command 兼容入口与事件总线的插件替身（模拟 LumenBridgePlugin）。"""

    def __init__(self, data_folder: Path) -> None:
        super().__init__(data_folder)
        self.bus = type("Bus", (), {"on": lambda s, e, h: None, "off": lambda s, e, h: None})()

    def register_command(self, name, handler, description="", aliases=None, usages=None) -> bool:
        return plugin_register_command_compat(self, name, handler, description, aliases, usages)


class _FakeSender:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def send_message(self, msg: str) -> None:
        self.messages.append(str(msg))


class RegisterCommandTests(unittest.TestCase):
    """lumen.register_command：PicServer_Rank3 兼容插件触发的 API。

    面板方案：注册 = 写 command_palette.json（重启后并入类级 commands）+
    运行期把 handler 绑定到 plugin._lumen_sub_commands 注册表。
    """

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.data = Path(self.tempdir.name)
        self.plugin = _CompatPlugin(self.data)
        self.ctx = LumenContext(self.plugin, "rank3", self.data / "rank3")
        self.calls: list[tuple[object, list]] = []
        # 面板文件重定向到临时目录，避免测试污染工作目录
        self._palette = patch.object(
            context_module, "COMMAND_PALETTE_PATH", self.data / "palette" / "command_palette.json"
        )
        self._palette.start()
        self.addCleanup(self._palette.stop)
        self.addCleanup(self.tempdir.cleanup)

    def _handler(self, sender: object, args: list) -> bool:
        self.calls.append((sender, args))
        return True

    def _registry(self) -> dict:
        return self.plugin.__dict__.get("_lumen_sub_commands", {})

    def test_first_registration_writes_palette_and_binds(self) -> None:
        ok = self.ctx.register_command(
            "rank", self._handler, description="排行榜",
            usages=["/rank (check|del|export|load)<action: String> [args: message]"],
        )
        self.assertTrue(ok)
        # handler 已绑定到共享注册表，归属子插件
        entry = self._registry()["rank"]
        self.assertEqual(entry["subplugin"], "rank3")
        sender = _FakeSender()
        self.assertTrue(entry["handler"](sender, ["check", "Steve"]))
        self.assertEqual(self.calls, [(sender, ["check", "Steve"])])
        # 面板已登记，含描述与用法（合法 usage 原样保留）
        palette = context_module.read_command_palette()
        self.assertIn("rank", palette)
        self.assertEqual(palette["rank"]["description"], "排行榜")
        self.assertEqual(
            palette["rank"]["usages"],
            ["/rank (check|del|export|load)<action: String> [args: message]"],
        )

    def test_default_usage_is_valid_endstone_syntax(self) -> None:
        """回归：默认 usage 必须是 Endstone 可解析语法（修复 "/rank ..." 注册失败）。"""
        self.assertEqual(default_usage("rank"), "/rank [args: message]")
        # 不传 usages 注册 → 面板写入合法默认值
        self.assertTrue(self.ctx.register_command("rank", self._handler))
        palette = context_module.read_command_palette()
        self.assertEqual(palette["rank"]["usages"], ["/rank [args: message]"])

    def test_sanitize_usages_drops_invalid(self) -> None:
        """非法 usage（含旧版 "..."/无类型中括号/前缀不符）被剔除，全无效回退默认。"""
        self.assertEqual(
            sanitize_usages("rank", ["/rank ...", "/rank [页码]", "/other <x>", "", None]),
            ["/rank [args: message]"],
        )
        # 合法项保留（枚举组 + 带空格类型 + 可选参数，参照 /lumen 语法）
        keep = ["/rank (check|del|export)<action: String> [args: message]"]
        self.assertEqual(sanitize_usages("rank", ["/rank ..."] + keep), keep)
        # 裸命令 /rank 合法
        self.assertEqual(sanitize_usages("rank", ["/rank"]), ["/rank"])

    def test_merge_sanitizes_legacy_palette(self) -> None:
        """回归：面板文件中遗留的非法 usage（旧版默认值）在并入类级 commands 时被清洗。"""
        context_module.write_command_palette({
            "rank": {"description": "排行榜", "usages": ["/rank ..."]},
        })
        commands: dict = {"lumen": {"description": "主命令"}}
        merged = merge_command_palette_into(commands)
        self.assertEqual(merged, 1)
        self.assertEqual(commands["rank"]["usages"], ["/rank [args: message]"])

    def test_predeclared_command_not_rewritten(self) -> None:
        context_module.write_command_palette({"rank": {"description": "预声明", "usages": ["/rank"]}})
        before = (self.data / "palette" / "command_palette.json").read_text(encoding="utf-8")
        self.assertTrue(self.ctx.register_command("rank", self._handler))
        after = (self.data / "palette" / "command_palette.json").read_text(encoding="utf-8")
        self.assertEqual(before, after)

    def test_duplicate_name_rejected(self) -> None:
        self.assertTrue(self.ctx.register_command("rank", self._handler))
        # 同名再注册（无论同上下文还是新上下文）→ False
        self.assertFalse(self.ctx.register_command("rank", self._handler))
        ctx2 = LumenContext(self.plugin, "rank4", self.data / "rank4")
        self.assertFalse(ctx2.register_command("rank", self._handler))
        self.assertEqual(len(self._registry()), 1)

    def test_invalid_name_and_handler_rejected(self) -> None:
        self.assertFalse(self.ctx.register_command("", self._handler))
        self.assertFalse(self.ctx.register_command("bad name!", self._handler))
        self.assertFalse(self.ctx.register_command("ok", "not-callable"))
        self.assertEqual(self._registry(), {})
        self.assertEqual(context_module.read_command_palette(), {})

    def test_handler_exception_returns_false(self) -> None:
        def boom(sender: object, args: list) -> bool:
            raise RuntimeError("boom")

        self.assertTrue(self.ctx.register_command("rank", boom))
        sender = _FakeSender()
        self.assertFalse(self._registry()["rank"]["handler"](sender, []))
        self.assertTrue(any("failed" in m for m in sender.messages))

    def test_cleanup_unbinds_handler(self) -> None:
        self.assertTrue(self.ctx.register_command("rank", self._handler))
        self.ctx._cleanup()
        # 绑定已解除；面板声明保留（重启后命令仍存在）
        self.assertNotIn("rank", self._registry())
        self.assertIn("rank", context_module.read_command_palette())
        # 注册表已清空：同名可重新绑定
        self.assertTrue(self.ctx.register_command("rank", self._handler))

    def test_plugin_object_compat_during_subplugin_load(self) -> None:
        """PicServer_Rank3 场景：子插件在 on_load 里经 lumen.plugin.register_command 注册。"""
        manager = SubPluginManager(self.plugin)
        archive = self._rank_zip(manager)
        ok, _message, name = manager.install_from_zip(archive)
        self.assertTrue(ok)
        self.assertTrue(manager.subplugins[name].loaded)
        entry = self._registry()["rank"]
        self.assertEqual(entry["subplugin"], name)
        sender = _FakeSender()
        self.assertTrue(entry["handler"](sender, []))
        # 卸载子插件 → 绑定解除（context._cleanup 路径一致）
        sp = manager.subplugins[name]
        self.assertTrue(sp.loaded and sp.context is not None)
        sp.context._cleanup()
        self.assertNotIn("rank", self._registry())
        # 热重载：旧绑定经 _unload_one 清理后可重新注册（否则查重会让加载失败）
        self.assertTrue(manager.reload_one(name))
        self.assertEqual(self._registry()["rank"]["subplugin"], name)

    def test_plugin_object_compat_outside_load(self) -> None:
        # 非加载期直接在插件对象上注册：以 "plugin" 归属入注册表
        self.assertTrue(self.plugin.register_command("rank", self._handler))
        entry = self._registry()["rank"]
        self.assertEqual(entry["subplugin"], "plugin")
        sender = _FakeSender()
        self.assertTrue(entry["handler"](sender, ["a"]))

    def _rank_zip(self, manager: SubPluginManager) -> Path:
        main_code = (
            "def on_load(lumen):\n"
            "    ok = lumen.plugin.register_command('rank', _handler, '排行榜')\n"
            "    if not ok:\n"
            "        raise RuntimeError('无法注册 /rank: 该命令已被其他兼容子插件占用')\n"
            "\n"
            "def _handler(sender, args):\n"
            "    return True\n"
        )
        manifest = {"name": "picserver_rank3", "version": "3.0.0", "load": True, "dependencies": []}
        archive = self.data / "picserver_rank3.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("lumen.json", json.dumps(manifest))
            zf.writestr("main.py", main_code)
        return archive


class MergeCommandPaletteTests(unittest.TestCase):
    """merge_command_palette_into：导入期把面板并入类级 commands。"""

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.data = Path(self.tempdir.name)
        self._palette = patch.object(
            context_module, "COMMAND_PALETTE_PATH", self.data / "palette" / "command_palette.json"
        )
        self._palette.start()
        self.addCleanup(self._palette.stop)
        self.addCleanup(self.tempdir.cleanup)

    def test_merge_valid_entries(self) -> None:
        context_module.write_command_palette({
            "rank": {"description": "排行榜", "usages": ["/rank [页码]"], "aliases": ["RK"]},
            "weather": {},
        })
        commands = {"lumen": {"description": "主命令", "usages": ["/lumen"]}}
        merged = context_module.merge_command_palette_into(commands)
        self.assertEqual(merged, 2)
        self.assertEqual(commands["rank"]["description"], "排行榜")
        self.assertEqual(commands["rank"]["aliases"], ["rk"])  # 别名小写规范化
        self.assertEqual(commands["weather"]["description"], "LumenBridge subplugin command /weather")
        self.assertEqual(commands["lumen"]["description"], "主命令")  # 原有条目不受影响

    def test_merge_skips_invalid_and_conflicting(self) -> None:
        context_module.write_command_palette({
            "lumen": {"description": "劫持主命令"},
            "bad name!": {"description": "非法名"},
            "": {"description": "空名"},
        })
        commands = {"lumen": {"description": "主命令"}}
        self.assertEqual(context_module.merge_command_palette_into(commands), 0)
        self.assertEqual(commands, {"lumen": {"description": "主命令"}})

    def test_merge_with_missing_palette_file(self) -> None:
        commands: dict = {}
        self.assertEqual(context_module.merge_command_palette_into(commands), 0)
        self.assertEqual(commands, {})

    def test_merge_with_corrupted_palette_file(self) -> None:
        palette = self.data / "palette" / "command_palette.json"
        palette.parent.mkdir(parents=True, exist_ok=True)
        palette.write_text("{not json", encoding="utf-8")
        commands: dict = {}
        self.assertEqual(context_module.merge_command_palette_into(commands), 0)


if __name__ == "__main__":
    unittest.main()
