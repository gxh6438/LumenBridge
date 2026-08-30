"""本次安全加固专项回归测试。

覆盖：
1. message.image 本地文件白名单（防任意文件读取外发）
2. qqofficial _extract_payload 单 dict 消息段解析
3. 正则引擎 ReDoS 防护（pattern 风险检测 + 超长截断）
4. _run_command_capture 主线程自死锁修复（主线程直接同步执行）
5. 子插件 ZIP 安装解压限额（防 ZIP 炸弹）
6. marketplace._run_on_main_wait 主线程自死锁修复
"""

from __future__ import annotations

import io
import json
import sys
import tempfile
import threading
import types
import urllib.request
import zipfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from endstone_lumenbridge.marketplace import MarketplaceClient
from endstone_lumenbridge.modules.regex_engine import (
    RegexEngineModule,
    _is_risky_pattern,
)
from endstone_lumenbridge.onebot import message as msg_builder
from endstone_lumenbridge.onebot.message import set_local_image_roots
from endstone_lumenbridge.onebot.qqofficial_adapter import _extract_payload
from endstone_lumenbridge.subplugin import loader as subplugin_loader
from endstone_lumenbridge.subplugin.loader import SubPluginManager


class QuietLogger:
    def info(self, _msg):
        pass

    def warning(self, _msg):
        pass

    def error(self, _msg):
        pass

    def debug(self, _msg):
        pass


class FakeBus:
    def on(self, *_args, **_kwargs):
        pass


class RecordingAdapter:
    def __init__(self):
        self.sent = []

    def send_group_msg(self, group_id, message):
        self.sent.append((group_id, message))


class FakeLanguage:
    def translate(self, message, params=None):
        if isinstance(message, str):
            return message
        return getattr(message, "text", str(message))


class FakeMessage:
    def __init__(self, text: str, params=()):
        self.text = text
        self.params = list(params)


class FakeCommandSenderWrapper:
    def __init__(self, _sender, on_message=None, on_error=None):
        self.on_message = on_message
        self.on_error = on_error


class FakeServer:
    def __init__(self):
        self.command_sender = object()
        self.language = FakeLanguage()
        self.messages = [FakeMessage("commands.players.list", ["2", "20"])]
        self.errors = []
        self.dispatch_result = True
        self.commands = []

    def dispatch_command(self, sender, command):
        self.commands.append(command)
        for message in self.messages:
            if getattr(sender, "on_message", None):
                sender.on_message(message)
        return self.dispatch_result


class FakeConfigManager:
    whitelist = {"enable": False, "auto_add": False, "remove_on_leave": False,
                 "bind_keyword": "绑定白名单", "unbind_keyword": "解绑白名单"}
    regex_engine = {"enable": False, "command_timeout": 2.0, "only_on_main": False}
    main_group = 10000
    admin_qq = []
    admin_keys = set()
    data = {}


def with_fake_command_module(func):
    original = sys.modules.get("endstone.command")
    module = types.ModuleType("endstone.command")
    module.CommandSenderWrapper = FakeCommandSenderWrapper
    sys.modules["endstone.command"] = module
    try:
        func()
    finally:
        if original is None:
            sys.modules.pop("endstone.command", None)
        else:
            sys.modules["endstone.command"] = original


def test_image_whitelist():
    with tempfile.TemporaryDirectory() as raw_a, tempfile.TemporaryDirectory() as raw_b:
        root_a = Path(raw_a)
        outside = Path(raw_b)
        img = root_a / "banner.png"
        img.write_bytes(b"\x89PNG-fake-bytes")
        secret = outside / "secret.txt"
        secret.write_text("TOP SECRET", encoding="utf-8")

        # 默认未注册白名单：即使文件存在也不读取本地文件
        set_local_image_roots([])
        seg = msg_builder.image(str(img))
        assert seg["data"]["file"] == str(img), "白名单为空时不应读取本地文件"

        # 注册后：白名单内文件转 base64
        set_local_image_roots([root_a])
        seg = msg_builder.image(str(img))
        assert seg["data"]["file"].startswith("base64://"), "白名单内文件应转 base64"

        # 白名单外文件即使存在也不读取（防任意文件读取外发）
        seg = msg_builder.image(str(secret))
        assert seg["data"]["file"] == str(secret), "白名单外文件不应被读取"

        # 相对路径穿越（../）不能逃出白名单
        seg = msg_builder.image(str(root_a / ".." / outside.name / "secret.txt"))
        assert not str(seg["data"]["file"]).startswith("base64://"), "路径穿越不应命中白名单"

        # bytes 输入不受影响
        seg = msg_builder.image(b"raw-bytes")
        assert seg["data"]["file"].startswith("base64://")

        # URL 输入不受影响
        seg = msg_builder.image("https://example.com/a.png")
        assert seg["data"]["file"] == "https://example.com/a.png"

        set_local_image_roots([])


def test_extract_payload_single_dict():
    # 单个 text 段 dict 应按单元素列表解析，而不是 str(dict) 序列化
    text, media = _extract_payload({"type": "text", "data": {"text": "你好"}})
    assert text == "你好", f"单 dict text 段解析错误: {text!r}"
    assert media is None

    # 单个 image 段 dict 应提取出富媒体描述
    text, media = _extract_payload({"type": "image", "data": {"url": "https://x/y.png"}})
    assert text == ""
    assert media and media["type"] == "image" and media["url"] == "https://x/y.png"

    # 混合列表保持原行为
    text, media = _extract_payload([
        {"type": "text", "data": {"text": "看图"}},
        {"type": "image", "data": {"url": "https://x/y.png"}},
    ])
    assert text == "看图" and media["type"] == "image"


def test_regex_risky_pattern_detection():
    assert _is_risky_pattern("(a+)+") is True
    assert _is_risky_pattern("(?:ab*)+") is True
    assert _is_risky_pattern("(a{2,3})+") is True
    assert _is_risky_pattern("a{100000}") is True
    assert _is_risky_pattern("a{99999999}") is True
    assert _is_risky_pattern("^查白名单") is False
    assert _is_risky_pattern("^执行(.+)") is False
    assert _is_risky_pattern(r"^\d+$") is False
    assert _is_risky_pattern("[a+]+") is False  # 字符类内的 + 不构成嵌套量词


def _build_engine(tmp: Path, rules, adapter, is_main=True):
    server = FakeServer()

    def _forbidden_run_on_main(func, delay=1):
        raise AssertionError("run_on_main 不应在主线程路径上被调用")

    plugin = SimpleNamespace(
        data_folder=str(tmp),
        logger=QuietLogger(),
        _tee_logger=None,
        config_manager=FakeConfigManager(),
        bus=FakeBus(),
        adapter=adapter,
        server=server,
        whitelist_module=None,
        group_allowed=lambda pack: True,
        run_on_main=_forbidden_run_on_main,
        is_on_main_thread=lambda: is_main,
    )
    engine = RegexEngineModule(plugin)
    engine.rules = rules
    # conf 已改为实时读取 config_manager 的 property，测试改为注入配置管理器
    plugin.config_manager.regex_engine = {"enable": True, "only_on_main": False, "command_timeout": 2.0}
    return engine, server


def test_regex_rule_skip_risky_pattern():
    with tempfile.TemporaryDirectory() as raw:
        adapter = RecordingAdapter()
        engine, _ = _build_engine(Path(raw), [
            {"id": "bomb", "name": "炸弹规则", "enabled": True, "triggerType": "message",
             "pattern": "(a+)+$", "flags": "", "conditions": [],
             "actions": [{"type": "replyText", "params": "不应出现"}], "block": True},
            {"id": "safe", "name": "安全规则", "enabled": True, "triggerType": "message",
             "pattern": "^ping$", "flags": "", "conditions": [],
             "actions": [{"type": "replyText", "params": "pong"}], "block": False},
        ], adapter)
        # 长文本含大量 a，灾难性 pattern 若被执行会长时间阻塞；被跳过则瞬间完成
        pack = {
            "post_type": "message", "message_type": "group", "message_id": 1,
            "group_id": 10000, "user_id": 1, "self_id": 100,
            "sender": {"role": "member", "nickname": "t"},
            "message": [{"type": "text", "data": {"text": "a" * 60 + "!"}}],
            "raw_message": "a" * 60 + "!", "time": 1,
        }
        engine._on_group_message(pack, None)
        assert adapter.sent == [], f"高风险规则应被跳过，不应有任何回复: {adapter.sent}"

        # 命中安全规则的普通消息正常回复
        pack2 = dict(pack, message_id=2,
                     message=[{"type": "text", "data": {"text": "ping"}}],
                     raw_message="ping")
        engine._on_group_message(pack2, None)
        assert adapter.sent == [(10000, "pong")], f"安全规则应正常执行: {adapter.sent}"

        # 超长消息（> 截断上限）不应崩溃
        adapter.sent.clear()
        pack3 = dict(pack, message_id=3,
                     message=[{"type": "text", "data": {"text": "x" * 3000}}],
                     raw_message="x" * 3000)
        engine._on_group_message(pack3, None)
        assert adapter.sent == []


def test_run_command_capture_main_thread_no_deadlock():
    def scenario():
        with tempfile.TemporaryDirectory() as raw:
            adapter = RecordingAdapter()
            engine, server = _build_engine(Path(raw), [], adapter, is_main=True)
            result = engine._run_command_capture("list")
            # 主线程直接同步执行：命令已派发且输出被翻译，没有走调度器
            assert server.commands == ["list"], "命令应在主线程直接执行"
            assert "在线玩家：2/20" in result, f"捕获输出异常: {result!r}"

    with_fake_command_module(scenario)


def test_run_command_capture_worker_thread_uses_scheduler():
    def scenario():
        with tempfile.TemporaryDirectory() as raw:
            adapter = RecordingAdapter()
            engine, server = _build_engine(Path(raw), [], adapter, is_main=False)
            # run_on_main 在 _build_engine 中是"禁止调用"桩，这里替换为立即执行
            def run_now(func, delay=1):
                func()
            engine.plugin.run_on_main = run_now
            result = engine._run_command_capture("list")
            assert server.commands == ["list"]
            assert "在线玩家：2/20" in result

    with_fake_command_module(scenario)


def _make_zip(entries: dict[str, bytes]) -> Path:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    path = Path(tempfile.mkstemp(suffix=".zip")[1])
    path.write_bytes(buf.getvalue())
    return path


def test_zip_extraction_limits():
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        plugin = SimpleNamespace(
            data_folder=str(tmp), logger=QuietLogger(), _tee_logger=None,
        )
        manager = SubPluginManager(plugin)
        entry = "def on_load(lumen):\n    pass\n"
        manifest = json.dumps({"name": "zip_limit_sp", "version": "1.0.0"})

        # 正常小包可安装
        good = _make_zip({"main.py": entry, "lumen.json": manifest})
        ok, msg, name = manager.install_from_zip(good)
        assert ok and name == "zip_limit_sp", f"正常包应可安装: {msg}"
        good.unlink()

        # 文件数超限被拒
        many = _make_zip({f"f{i}.txt": b"x" for i in range(subplugin_loader._MAX_ZIP_ENTRIES + 1)})
        ok, msg, name = manager.install_from_zip(many)
        assert not ok, "文件数超限应被拒绝"
        many.unlink()

        # 单文件解压体积超限被拒（临时调低阈值避免构造超大文件）
        orig_file, orig_total = (
            subplugin_loader._MAX_ZIP_FILE_BYTES,
            subplugin_loader._MAX_ZIP_TOTAL_BYTES,
        )
        try:
            subplugin_loader._MAX_ZIP_FILE_BYTES = 16
            big = _make_zip({"main.py": entry, "lumen.json": manifest})
            ok, msg, name = manager.install_from_zip(big)
            assert not ok, "单文件体积超限应被拒绝"
            big.unlink()

            # 总解压体积超限被拒
            subplugin_loader._MAX_ZIP_FILE_BYTES = orig_file
            subplugin_loader._MAX_ZIP_TOTAL_BYTES = 16
            big2 = _make_zip({"main.py": entry, "lumen.json": manifest})
            ok, msg, name = manager.install_from_zip(big2)
            assert not ok, "总体积超限应被拒绝"
            big2.unlink()
        finally:
            subplugin_loader._MAX_ZIP_FILE_BYTES = orig_file
            subplugin_loader._MAX_ZIP_TOTAL_BYTES = orig_total


def test_marketplace_redirect_guard():
    # 重定向拦截机制已按需求撤销（内网部署场景）：市场下载走标准 urllib，
    # 不再对 HTTPS→HTTP 降级或内网目标做拦截，此处仅回归 _open 可用性。
    client = MarketplaceClient(SimpleNamespace(
        logger=QuietLogger(), _tee_logger=None,
        config_manager=FakeConfigManager(),
    ))
    assert callable(client._open)
    assert hasattr(urllib.request, "urlopen")


def test_marketplace_run_on_main_wait_main_thread():
    plugin = SimpleNamespace(
        logger=QuietLogger(), _tee_logger=None,
        config_manager=FakeConfigManager(),
        run_on_main=lambda func, delay=1: (_ for _ in ()).throw(
            AssertionError("主线程路径不应走 run_on_main")
        ),
        is_on_main_thread=lambda: True,
    )
    client = MarketplaceClient(plugin)
    assert client._run_on_main_wait(lambda: 42) == 42

    # 工作线程路径仍走调度器
    plugin2 = SimpleNamespace(
        logger=QuietLogger(), _tee_logger=None,
        config_manager=FakeConfigManager(),
        run_on_main=lambda func, delay=1: func(),
        is_on_main_thread=lambda: False,
    )
    client2 = MarketplaceClient(plugin2)
    assert client2._run_on_main_wait(lambda: "ok") == "ok"


def test_plugin_is_on_main_thread():
    # 类方法对 duck-typed self 亦可判断（ident 比对）
    from endstone_lumenbridge.plugin import LumenBridgePlugin

    harness = SimpleNamespace(_main_thread_id=threading.get_ident())
    assert LumenBridgePlugin.is_on_main_thread(harness) is True
    harness2 = SimpleNamespace(_main_thread_id=threading.get_ident() + 1)
    assert LumenBridgePlugin.is_on_main_thread(harness2) is False


def main():
    tests = [
        test_image_whitelist,
        test_extract_payload_single_dict,
        test_regex_risky_pattern_detection,
        test_regex_rule_skip_risky_pattern,
        test_run_command_capture_main_thread_no_deadlock,
        test_run_command_capture_worker_thread_uses_scheduler,
        test_zip_extraction_limits,
        test_marketplace_redirect_guard,
        test_marketplace_run_on_main_wait_main_thread,
        test_plugin_is_on_main_thread,
    ]
    failed = 0
    for test in tests:
        try:
            test()
            print(f"[PASS] {test.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            import traceback

            print(f"[FAIL] {test.__name__}: {exc}")
            traceback.print_exc()
    print(f"\n===== 安全加固回归: {len(tests) - failed} 通过, {failed} 失败 =====")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
