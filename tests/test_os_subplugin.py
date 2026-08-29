"""os 子插件自检测试：用 Fake lumen 上下文验证加载、配置面板、状态查询全链路"""

import importlib.util
import json
import sys
import tempfile
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


# ---------------------------------------------------------------------------
# Fake lumen 上下文
# ---------------------------------------------------------------------------

class FakeLogger:
    def info(self, m):
        print(f"    [log] {m}")

    warning = error = debug = info


class FakeStorage:
    def __init__(self, d):
        self.dir = Path(d)

    def read(self, filename, default=None):
        p = self.dir / filename
        if p.exists():
            return json.loads(p.read_text("utf-8"))
        if default is not None:
            self.write(filename, default)
            return json.loads(json.dumps(default))
        return None

    def write(self, filename, data):
        (self.dir / filename).write_text(json.dumps(data, ensure_ascii=False, indent=4), "utf-8")


class FakeEnv:
    def __init__(self):
        self._d = {"main_group": 123456}

    def get(self, k, default=None):
        return self._d.get(k, default)

    def set(self, k, v):
        self._d[k] = v


class FakeMC:
    def __init__(self):
        self.online_players = ["Steve", "Alex"]

    def runcmdEx(self, cmd, timeout=5.0):
        if cmd.startswith("weather"):
            return {"success": True, "output": "The weather is clear"}
        return {"success": True, "output": ""}

    def listen(self, *a):
        return True


class FakeServer:
    minecraft_version = "1.21.100"
    protocol_version = 800
    average_tps = 19.97


class FakeBus:
    def __init__(self):
        self.handlers = {}

    def on(self, event, handler):
        self.handlers.setdefault(event, []).append(handler)

    def emit(self, event, *args):
        for h in self.handlers.get(event, []):
            h(*args)


class FakeWebBuilder:
    def __init__(self):
        self.items = []

    def _add(self, t, key, val, desc="", label=""):
        self.items.append({"type": t, "key": key, "val": val, "desc": desc, "label": label})
        return self

    def switch(self, key, val=False, desc="", label=""):
        return self._add("switch", key, val, desc, label)

    def text(self, key, val="", desc="", label=""):
        return self._add("text", key, val, desc, label)

    def register(self):
        FakeWeb.registered = self


class FakeWeb:
    registered = None

    def createConfig(self, name=None):
        return FakeWebBuilder()


class FakeLumen:
    def __init__(self, data_dir):
        self.pluginName = "os"
        self.logger = FakeLogger()
        self.storage = FakeStorage(data_dir)
        self.env = FakeEnv()
        self.mc = FakeMC()
        self.server = FakeServer()
        self.web = FakeWeb()
        self._bus = FakeBus()

    def on(self, event, handler):
        self._bus.on(event, handler)
        return handler

    def emit(self, event, *args):
        self._bus.emit(event, *args)


# ---------------------------------------------------------------------------
# 测试
# ---------------------------------------------------------------------------

def _run():
    plugin_dir = ROOT / "examples_plugins" / "subplugins" / "os"
    print("==== os 子插件测试 ====")

    # 清单校验
    manifest = json.loads((plugin_dir / "lumen.json").read_text("utf-8"))
    check("lumen.json 清单有效", manifest["name"] == "os" and manifest["load"] is True)

    # 动态加载 main.py
    spec = importlib.util.spec_from_file_location("os_subplugin_main", plugin_dir / "main.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    check("main.py 可导入", hasattr(mod, "on_load"))

    with tempfile.TemporaryDirectory() as td:
        lumen = FakeLumen(td)
        mod.on_load(lumen)

        # 配置文件自动生成
        conf_file = Path(td) / "config.json"
        check("config.json 自动生成", conf_file.exists())
        conf = json.loads(conf_file.read_text("utf-8"))
        check("配置项完整", all(k in conf for k in mod.DEFAULT_CONFIG))

        # 网页配置面板注册（全部项 + 中文标签）
        reg = FakeWeb.registered
        check("网页配置面板已注册", reg is not None and len(reg.items) == len(mod.DEFAULT_CONFIG))
        check("配置面板带中文标签", reg and all(it["label"] and it["label"] != it["key"] for it in reg.items))

        # 状态查询：模拟群消息
        replies = []

        def reply(msg, at=False):
            replies.append(msg)

        lumen.emit(
            "message.group.normal",
            {"group_id": 123456, "raw_message": "服务器状态", "sender": {"nickname": "tester"}},
            reply,
        )
        check("触发关键字有回复", len(replies) == 1)
        text = replies[0] if replies else ""
        print("    --- 回复内容 ---")
        for line in text.split("\n"):
            print(f"    {line}")
        check("包含游戏版本", "游戏版本：1.21.100" in text)
        check("包含协议版本", "服务器协议：800" in text)
        check("包含天气(晴天)", "晴天" in text)
        check("包含CPU占用", "CPU占用" in text)
        check("包含CPU核数", "CPU核数" in text)
        check("包含内存占用", "内存占用" in text and "%" in text)
        check("包含系统运行时间", "系统已运行" in text)
        check("包含BDS运行时间", "BDS已运行" in text)
        check("包含平均TPS", "平均TPS: 19.97" in text)
        check("包含在线人数", "在线2人" in text)

        # 非主群消息不响应
        lumen.emit(
            "message.group.normal",
            {"group_id": 999, "raw_message": "服务器状态"},
            reply,
        )
        check("非主群不响应", len(replies) == 1)

        # 其他消息不响应
        lumen.emit(
            "message.group.normal",
            {"group_id": 123456, "raw_message": "你好"},
            reply,
        )
        check("非关键字不响应", len(replies) == 1)

        # 网页配置更新联动
        lumen.emit("config.update.os", "show_tps", False)
        conf2 = json.loads(conf_file.read_text("utf-8"))
        check("网页配置更新已持久化", conf2["show_tps"] is False)

        replies.clear()
        lumen.emit(
            "message.group.normal",
            {"group_id": 123456, "raw_message": "服务器状态"},
            reply,
        )
        check("关闭项后不再显示TPS", replies and "平均TPS" not in replies[0])

        # 自定义触发词
        lumen.emit("config.update.os", "trigger", "查状态")
        replies.clear()
        lumen.emit(
            "message.group.normal",
            {"group_id": 123456, "raw_message": "查状态"},
            reply,
        )
        check("自定义触发词生效", len(replies) == 1)

        # on_unload 正常
        mod.on_unload(lumen)
        check("on_unload 正常", True)

    print("==== 结果 ====")
    print("全部通过" if not FAIL else f"失败 {len(FAIL)} 项: {FAIL}")


# C7：pytest 入口 —— 依赖 examples_plugins/subplugins/os 示例目录，缺失时才 skip
OS_PLUGIN_DIR = ROOT / "examples_plugins" / "subplugins" / "os"


def test_os_subplugin_selfcheck():
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
