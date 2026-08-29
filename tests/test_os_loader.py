"""用真实 SubPluginManager 加载 os 子插件，验证清单解析、加载与安装打包兼容性"""

import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

PASS = []
FAIL = []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'} - {name}" + (f" ({detail})" if detail and not cond else ""))
    if not cond:
        # 失败必须抛错：pytest 收集 test_ 函数时得到真实 FAIL（结构性假绿修复）
        raise AssertionError(f"{name} {detail}".strip())


class FakeLogger:
    def info(self, m):
        print(f"    [log] {m}")

    warning = error = debug = info


class FakeConfigManager:
    debug = False

    class _C:
        main_group = 123456
        admin_qq = [111]

    config = _C()


class FakeScheduler:
    def run_task(self, plugin, func, delay=0):
        func()

        class T:
            task_id = 1

        return T()


class FakeServer:
    minecraft_version = "1.21.100"
    protocol_version = 800
    average_tps = 20.0
    online_players = []
    scheduler = FakeScheduler()

    def broadcast_message(self, m):
        pass


class FakeBus:
    def __init__(self):
        self.handlers = {}

    def on(self, event, handler):
        self.handlers.setdefault(event, []).append(handler)

    def once(self, event, handler):
        self.on(event, handler)

    def off(self, event, handler):
        if handler in self.handlers.get(event, []):
            self.handlers[event].remove(handler)

    def emit(self, event, *args, **kwargs):
        for h in list(self.handlers.get(event, [])):
            h(*args, **kwargs)


class FakeEnvPool:
    def __init__(self):
        self._d = {"main_group": 123456}

    def get(self, k, default=None):
        return self._d.get(k, default)

    def set(self, k, v):
        self._d[k] = v


class FakePlugin:
    """最小可用的 LumenBridgePlugin 替身"""

    def __init__(self, data_dir):
        self.logger = FakeLogger()
        self._tee_logger = None
        self.server = FakeServer()
        self.bus = FakeBus()
        self.env_pool = FakeEnvPool()
        self.adapter = None
        self.regex_module = None
        self.webui = None
        self.config_manager = FakeConfigManager()
        self.data_folder = str(data_dir)

    def run_on_main(self, func, delay=1):
        func()

    def register_events(self, listener):
        pass


def _run():
    from endstone_lumenbridge.subplugin.loader import SubPluginManager

    print("==== os 子插件 · 真实加载器测试 ====")
    src_plugin = ROOT / "examples_plugins" / "subplugins" / "os"

    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td)
        plugins_dir = data_dir / "plugins"
        plugins_dir.mkdir(parents=True)

        plugin = FakePlugin(data_dir)
        mgr = SubPluginManager(plugin)

        # 1. 目录发现加载
        shutil.copytree(src_plugin, plugins_dir / "os")
        mgr.load_all()
        loaded = mgr.subplugins.get("os")
        check("加载器发现并加载 os", loaded is not None and getattr(loaded, "loaded", False),
              str(getattr(loaded, "error", "")) if loaded else "未发现")

        # 2. 群消息触发（走真实事件总线 + 真实 LumenContext）
        replies = []
        plugin.bus.emit(
            "message.group.normal",
            {"group_id": 123456, "raw_message": "服务器状态", "sender": {}},
            lambda msg, at=False: replies.append(msg),
        )
        check("真实上下文状态查询有回复", len(replies) == 1)
        if replies:
            ok = "游戏版本：1.21.100" in replies[0] and "平均TPS: 20.00" in replies[0]
            check("回复内容含版本与TPS", ok, replies[0][:100])

        # 3. 热重载
        mgr.reload_all()
        check("热重载正常", mgr.subplugins.get("os") is not None and mgr.subplugins["os"].loaded)

        # 4. ZIP 安装（模拟网页安装路径）
        zpath = data_dir / "os.zip"
        with zipfile.ZipFile(zpath, "w") as z:
            for f in src_plugin.rglob("*"):
                if f.is_file():
                    z.write(f, f"os/{f.relative_to(src_plugin)}")
        ok, msg = mgr.uninstall("os")
        check("卸载成功", ok and "os" not in mgr.subplugins, msg)
        ok2, msg2, name2 = mgr.install_from_zip(str(zpath))
        check("ZIP 安装成功", ok2, msg2)
        check("安装后已加载", mgr.subplugins.get("os") is not None and mgr.subplugins["os"].loaded)

    print("==== 结果 ====")
    print("全部通过" if not FAIL else f"失败 {len(FAIL)} 项: {FAIL}")


# C7：pytest 入口 —— 依赖 examples_plugins/subplugins/os 示例目录，缺失时才 skip
OS_PLUGIN_DIR = ROOT / "examples_plugins" / "subplugins" / "os"


def test_os_loader_real_loader():
    if not OS_PLUGIN_DIR.is_dir():
        import pytest
        pytest.skip(f"缺少示例子插件目录: {OS_PLUGIN_DIR}")
    _run()


def main():
    try:
        _run()
    except AssertionError:
        pass
    print("==== 结果 ====")
    print("全部通过" if not FAIL else f"失败 {len(FAIL)} 项: {FAIL}")
    sys.exit(0 if not FAIL else 1)


if __name__ == "__main__":
    main()
