"""LumenBridge 2.0.1 用户报告问题专项回归测试。"""

from __future__ import annotations

import sys
import tempfile
import threading
import types
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from endstone_lumenbridge.modules.regex_engine import RegexEngineModule
from endstone_lumenbridge.modules.whitelist import WhitelistModule
from endstone_lumenbridge.plugin import LumenBridgePlugin
from endstone_lumenbridge.webui.logbuffer import LogBuffer


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


class FakeAdapter:
    def send_group_msg(self, *_args, **_kwargs):
        pass


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
        self.messages = []
        self.errors = []
        self.dispatch_result = True
        self.commands = []

    def dispatch_command(self, sender, command):
        self.commands.append(command)
        for message in self.messages:
            if getattr(sender, "on_message", None):
                sender.on_message(message)
        for message in self.errors:
            if getattr(sender, "on_error", None):
                sender.on_error(message)
        return self.dispatch_result


class FakeConfigManager:
    whitelist = {
        "enable": False,
        "auto_add": True,
        "remove_on_leave": True,
        "bind_keyword": "绑定白名单",
        "unbind_keyword": "解绑白名单",
    }
    regex_engine = {"enable": False, "command_timeout": 1.0}
    main_group = 10000
    admin_qq = []


def build_plugin(tmp: Path):
    server = FakeServer()
    plugin = SimpleNamespace(
        data_folder=str(tmp),
        logger=QuietLogger(),
        config_manager=FakeConfigManager(),
        bus=FakeBus(),
        adapter=FakeAdapter(),
        server=server,
        whitelist_module=None,
        _tee_logger=None,
    )
    plugin.run_on_main = lambda func, delay=1: func()
    return plugin, server


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


def test_logbuffer_subscription_lifecycle():
    buffer = LogBuffer(max_size=4, max_subscribers=2)
    first = lambda _entry: None
    second = lambda _entry: None
    third = lambda _entry: None

    assert buffer.subscribe(first) is True
    assert buffer.subscribe(first) is True
    assert buffer.subscriber_count == 1
    assert buffer.subscribe(second) is True
    assert buffer.subscribe(third) is False
    assert buffer.subscriber_count == 2
    buffer.unsubscribe(first)
    buffer.unsubscribe(first)
    assert buffer.subscriber_count == 1


def test_get_xbox_id_domain_aware():
    """官 bot 下 getXboxID 误报"未绑定"回归：user_id 为 openid 时须查 official 域。"""

    def scenario():
        with tempfile.TemporaryDirectory() as raw_tmp:
            plugin, _server = build_plugin(Path(raw_tmp))
            whitelist = WhitelistModule(plugin)
            plugin.whitelist_module = whitelist
            engine = RegexEngineModule(plugin)

            # 双域各建一条绑定：qq 域 QQ 号 / official 域 openid
            assert whitelist.add_binding("10001", "SteveQQ") is True
            assert whitelist.add_binding("OPENID123", "SteveOfficial", "official") is True

            # 官方域事件包（user_id 为 openid）：查 official 域命中
            result = engine._action_get_xbox_id([], {"user_id": "OPENID123", "domain": "official"}, {})
            assert result["xbox"] == "SteveOfficial", result
            # 个人号域事件包（user_id 为 QQ 号）：查 qq 域命中
            result = engine._action_get_xbox_id([], {"user_id": "10001"}, {})
            assert result["xbox"] == "SteveQQ", result
            # 显式传参（$at）也按事件包选域：official 域 @成员的 openid
            result = engine._action_get_xbox_id(["OPENID123"], {"user_id": "OTHER", "domain": "official"}, {})
            assert result["xbox"] == "SteveOfficial", result
            # 未绑定仍提示 not_bound
            result = engine._action_get_xbox_id([], {"user_id": "NOPE", "domain": "official"}, {})
            assert result["xbox"] != "SteveOfficial"

    with_fake_command_module(scenario)


def test_whitelist_transaction_and_quoted_name():
    def scenario():
        with tempfile.TemporaryDirectory() as raw_tmp:
            plugin, server = build_plugin(Path(raw_tmp))
            whitelist = WhitelistModule(plugin)
            plugin.whitelist_module = whitelist

            server.messages = ["commands.whitelist.remove.success"]
            done, result = whitelist._run_allowlist_command("remove", "Player With Space")
            assert done.wait(1.0)
            assert result["success"] is True
            assert server.commands[-1] == 'whitelist remove "Player With Space"'

            assert whitelist.add_binding("10001", "Player With Space") is True
            ok, message, _entry = whitelist.unbind_sync("10001")
            assert ok is True
            assert "已解绑" in message
            assert whitelist.get_binding_by_qq("10001") is None

            assert whitelist.add_binding("10002", "Player Two") is True
            server.messages = ["commands.whitelist.remove.failed"]
            ok, message, entry = whitelist.unbind_sync("10002")
            assert ok is False
            assert "failed" in message
            assert entry and entry["xbox"] == "Player Two"
            assert whitelist.get_binding_by_qq("10002") is not None

    with_fake_command_module(scenario)


def test_list_translation_and_stop_success():
    def scenario():
        with tempfile.TemporaryDirectory() as raw_tmp:
            plugin, server = build_plugin(Path(raw_tmp))
            engine = RegexEngineModule(plugin)

            server.messages = [
                FakeMessage("commands.players.list", ["2", "20"]),
                FakeMessage("commands.players.list.names", ["Steve, Alex"]),
            ]
            server.errors = []
            server.dispatch_result = True
            result = engine._run_command_capture("list")
            assert "在线玩家：2/20" in result
            assert "玩家：Steve, Alex" in result
            assert "commands.players.list" not in result

            server.messages = []
            server.errors = []
            server.dispatch_result = False
            result = engine._run_command_capture("stop")
            assert result == "服务器停止指令已执行，正在安全关闭"
            assert "失败" not in result

    with_fake_command_module(scenario)


def test_bot_profile_cache():
    # v1.3.0：机器人资料按适配器卡片维护，_update_bot_profile(adapter, data)
    class ProfileHarness:
        _qq_avatar_url = staticmethod(LumenBridgePlugin._qq_avatar_url)

        def __init__(self):
            self._bot_profile_lock = threading.Lock()
            self._bot_profiles = {
                "main": {
                    "adapter_id": "main",
                    "adapter_name": "Main",
                    "adapter_type": "onebot",
                    "qq": 12345,
                    "nickname": "",
                    "avatar_url": LumenBridgePlugin._qq_avatar_url(12345),
                    "app_id": "",
                    "connected": False,
                    "source": "config",
                }
            }

        def bot_profiles_snapshot(self):
            with self._bot_profile_lock:
                return [dict(p) for p in self._bot_profiles.values()]

    profile = ProfileHarness()
    adapter = SimpleNamespace(adapter_id="main", adapter_name="Main", adapter_type="onebot")
    LumenBridgePlugin._update_bot_profile(
        profile,
        adapter,
        {"user_id": 67890, "nickname": "Lumen Bot"},
    )
    snapshot = LumenBridgePlugin.bot_profile_snapshot(profile)
    assert snapshot["qq"] == 67890
    assert snapshot["nickname"] == "Lumen Bot"
    assert snapshot["source"] == "onebot"
    assert snapshot["avatar_url"] == "https://q1.qlogo.cn/g?b=qq&nk=67890&s=100"


def test_frontend_regressions():
    static = ROOT / "src" / "endstone_lumenbridge" / "webui" / "static"
    app = (static / "app.js").read_text(encoding="utf-8")
    html = (static / "index.html").read_text(encoding="utf-8")

    load_config = app[app.index("async function loadConfig()") : app.index("async function saveConfig()")]
    load_subplugins = app[app.index("async function loadSubplugins(opts)") : app.index("async function toggleSubplugin")]

    assert "/api/plugins/configs" not in load_config
    assert "/api/plugins/configs" in load_subplugins
    # 配置按钮经 data-action="config" 事件委托分发到全局 openPluginConfig
    # （避免插件名拼进 onclick 导致 JS 逃逸），两处需同时存在
    assert 'spAction(t("subplugins.config_button"), "config")' in load_subplugins
    assert "openPluginConfig" in app
    assert "closeLogStream" in app
    assert 'document.addEventListener("visibilitychange"' in app
    assert "bot-profile-inline" in app
    # 开关尺寸修正为 42x24px（避免桌面端与手机端被拉得过宽过大）
    assert ".switch { position: relative; width: 42px; height: 24px; flex: 0 0 42px" in html
    assert "width: 42px !important; height: 24px !important" in html
    assert "transform: translateX(18px)" in html
    assert ".bot-profile-inline" in html
    assert "max-height: calc(100dvh - 20px)" in html


def main():
    tests = [
        test_logbuffer_subscription_lifecycle,
        test_whitelist_transaction_and_quoted_name,
        test_list_translation_and_stop_success,
        test_bot_profile_cache,
        test_frontend_regressions,
    ]
    for test in tests:
        test()
        print(f"[PASS] {test.__name__}")
    print(f"专项回归测试通过：{len(tests)}/{len(tests)}")


if __name__ == "__main__":
    main()
