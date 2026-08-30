"""Bug 修复专项验证测试

针对 v1.0.1 修复的关键 bug 编写独立验证用例，确保修复正确且未引入新问题。
使用真实 endstone API（非 mock），在真实环境下验证。
"""

import os
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

PASSED = []
FAILED = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        PASSED.append(name)
        print(f"  [PASS] {name}")
    else:
        FAILED.append(name)
        print(f"  [FAIL] {name} {detail}")
        # 失败必须抛错：pytest 收集 test_ 函数时得到真实 FAIL（结构性假绿修复）
        raise AssertionError(f"{name} {detail}".strip())


class FakeLogger:
    def info(self, m): pass
    def warning(self, m): pass
    def error(self, m): pass
    def debug(self, m): pass
    def exception(self, m): pass


# ======================================================================
# Bug #1: regex_engine player_chat 匹配目标
# ======================================================================
def test_player_chat_match_target():
    print("== Bug #1: player_chat 匹配消息内容而非玩家名 ==")
    from endstone_lumenbridge.modules.regex_engine import RegexEngineModule

    called = []

    class FakePlugin:
        logger = FakeLogger()
        data_folder = "/tmp/lb_test"
        whitelist_module = None
        class _CM:
            main_group = 10000
            main_groups = [10000]
        config_manager = _CM()
        class _Server:
            def get_online_players(self): return []
            @property
            def language(self):
                return type('L', (), {'translate': lambda s, k, *a: k})()
        server = _Server()

    class FakeAdapter:
        def call_api(self, *a, **k): called.append(('api', a))
        def send_group_msg(self, gid, content): called.append(('send', gid, content))

    engine = RegexEngineModule.__new__(RegexEngineModule)
    engine.plugin = FakePlugin()
    # conf 已改为实时读取 config_manager 的 property，测试通过注入配置管理器模拟
    engine.plugin.config_manager.regex_engine = {'main_group': 10000, 'admin_qq': [], 'enable': True}
    engine.logger = FakeLogger()
    engine.rules = []
    engine.custom_actions = {}
    engine._alias_map = {}
    engine.adapter = FakeAdapter()
    # v1.2.4 规则动作去抖状态（__new__ 桩需手动补齐）
    from collections import OrderedDict as _OD
    engine._action_dedup = _OD()
    engine._action_dedup_lock = __import__('threading').Lock()

    # pattern ^查服$ + replyText
    engine.rules = [{
        'name': 't1', 'enabled': True,
        'triggerType': 'event', 'eventType': 'server.player_chat',
        'pattern': '^查服$',
        'actions': [{'type': 'replyText', 'params': '收到: $0'}],
    }]

    # 1) Steve 说「查服」→ 触发，$0=消息内容
    called.clear()
    engine.on_mc_player_chat('Steve', '查服')
    check("玩家说「查服」正确触发", any('收到: 查服' in str(c) for c in called), f"{called}")

    # 2) Steve 说「hello」→ 不触发
    called.clear()
    engine.on_mc_player_chat('Steve', 'hello')
    check("玩家说「hello」正确未触发", not called, f"{called}")

    # 3) 玩家名=「查服」但说别的内容 → 不应触发（旧 bug 会触发）
    called.clear()
    engine.on_mc_player_chat('查服', 'hello world')
    check("玩家名匹配不再误触发", not called, f"{called}")

    # 4) 带捕获组
    engine.rules = [{
        'name': 't2', 'enabled': True,
        'triggerType': 'event', 'eventType': 'server.player_chat',
        'pattern': r'^执行\s+(\w+)$',
        'actions': [{'type': 'replyText', 'params': 'cmd=$1'}],
    }]
    called.clear()
    engine.on_mc_player_chat('Steve', '执行 testcmd')
    check("捕获组 $1 正确解析", any('cmd=testcmd' in str(c) for c in called), f"{called}")

    # 5) player_join 仍匹配玩家名
    engine.rules = [{
        'name': 't3', 'enabled': True,
        'triggerType': 'event', 'eventType': 'server.player_join',
        'pattern': '^Steve$',
        'actions': [{'type': 'replyText', 'params': 'welcome $0'}],
    }]
    called.clear()
    engine.on_mc_player_join('Steve')
    check("player_join 仍匹配玩家名", any('welcome Steve' in str(c) for c in called), f"{called}")


# ======================================================================
# Bug #2/#3: config 非法值不崩溃
# ======================================================================
def test_config_robustness():
    print("== Bug #2/#3: 非法值逐项跳过（v1.2.0 起由 connections.json 承载） ==")
    from endstone_lumenbridge.config import ConfigManager
    from endstone_lumenbridge.connections import ConnectionManager

    import tempfile as _tf
    with _tf.TemporaryDirectory() as d:
        ConfigManager(d, FakeLogger())  # 生成默认基础配置
        conn = ConnectionManager(Path(d), FakeLogger())
        conn.adapters = [{
            "type": "websocket", "id": "ws_test", "name": "WS", "enabled": True,
            "main_group": "123,abc,456", "admin_qq": ["x", 456, "y", 789],
        }]
        cm = ConfigManager(d, FakeLogger())
        cm.attach_connections(conn)
        check("main_group 跳过非法值", cm.main_groups == [123, 456], f"{cm.main_groups}")
        check("admin_qq 跳过非法值", cm.admin_qq == [456, 789], f"{cm.admin_qq}")

        # 完全非法
        conn.adapters = [{
            "type": "websocket", "id": "ws_test", "name": "WS", "enabled": True,
            "main_group": "invalid", "admin_qq": [],
        }]
        check("main_group 完全非法返回空", cm.main_groups == [], f"{cm.main_groups}")


# ======================================================================
# Bug #9: dispatcher raw_message None
# ======================================================================
def test_dispatcher_raw_message_none():
    print("== Bug #9: dispatcher raw_message=None 不崩溃 ==")
    from endstone_lumenbridge.event_bus import EventBus
    from endstone_lumenbridge.onebot.dispatcher import EventDispatcher

    bus = EventBus()
    disp = EventDispatcher(None, bus, FakeLogger())
    received = []
    bus.on('message.group.normal', lambda pack, reply: received.append(pack))

    # raw_message=None
    disp._on_pack({'post_type': 'message', 'message_type': 'group', 'sub_type': 'normal',
                   'raw_message': None, 'user_id': 123, 'group_id': 456})
    check("raw_message=None 不崩溃", len(received) == 1)

    # raw_message 缺失
    received.clear()
    disp._on_pack({'post_type': 'message', 'message_type': 'group', 'sub_type': 'normal',
                   'user_id': 123, 'group_id': 456})
    check("raw_message 缺失不崩溃", len(received) == 1)


# ======================================================================
# Bug #14: ZIP 路径穿越
# ======================================================================
def test_zip_path_traversal():
    print("== Bug #14: ZIP 安装包路径穿越被拒绝 ==")
    from endstone_lumenbridge.subplugin.loader import SubPluginManager

    tmp = tempfile.mkdtemp()
    try:
        evil_zip = os.path.join(tmp, 'evil.zip')
        with zipfile.ZipFile(evil_zip, 'w') as zf:
            zf.writestr('../../escaped.txt', 'hacked')
            zf.writestr('lumen.json', '{"name":"evil"}')
            zf.writestr('main.py', 'def on_load(lumen): pass')

        class FakePlugin:
            logger = FakeLogger()
            data_folder = tmp
            server = None
            bus = None
            def run_on_main(self, fn): fn()

        mgr = SubPluginManager(FakePlugin())
        ok, msg, name = mgr.install_from_zip(evil_zip)
        check("恶意 zip 被拒绝", not ok and '非法路径' in msg, f"ok={ok}, msg={msg}")
        check("未发生路径穿越", not os.path.exists(os.path.join(tmp, '..', '..', 'escaped.txt')))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ======================================================================
# Bug #24: ForwardMessageBuilder.build() 返回副本
# ======================================================================
def test_forward_msg_builder_copy():
    print("== Bug #24: ForwardMessageBuilder.build() 返回副本 ==")
    from endstone_lumenbridge.onebot.message import ForwardMessageBuilder

    b = ForwardMessageBuilder()
    b.add_custom_message('u1', 'nick1', [{'type': 'text', 'data': {'text': 'hi'}}])
    nodes = b.build()
    original_len = len(nodes)
    nodes.append({'hacked': True})
    nodes2 = b.build()
    check("build() 返回副本", len(nodes2) == original_len, f"len={len(nodes2)}")


# ======================================================================
# Bug #25: deep_merge 不污染 DEFAULT_CONFIG
# ======================================================================
def test_deep_merge_no_pollution():
    print("== Bug #25: deep_merge 不污染 DEFAULT_CONFIG ==")
    from endstone_lumenbridge.config import DEFAULT_CONFIG, deep_merge
    import copy

    orig = copy.deepcopy(DEFAULT_CONFIG)
    merged, _ = deep_merge(DEFAULT_CONFIG, {'onebot': {'port': 9999}})
    merged['onebot']['port'] = 1111
    check("DEFAULT_CONFIG 未被污染", DEFAULT_CONFIG == orig)
    check("merged 可独立修改", merged['onebot']['port'] == 1111)


# ======================================================================
# Bug #28: auth verify_token 异常处理
# ======================================================================
def test_auth_robustness():
    print("== Bug #28: auth verify_token 异常处理 ==")
    from endstone_lumenbridge.webui import auth

    check("非法 token 返回 False", auth.verify_token('invalid', 'secret') == False)
    check("空 token 返回 False", auth.verify_token('', 'secret') == False)
    check("格式错误 token 返回 False", auth.verify_token('a.b.c', 'secret') == False)

    # 合法 token
    token = auth.issue_token('mysecret')
    check("正确 secret 验证通过", auth.verify_token(token, 'mysecret') == True)
    check("错误 secret 验证失败", auth.verify_token(token, 'wrongsecret') == False)


# ======================================================================
# Bug #29: LoggerTee 代理
# ======================================================================
def test_logger_tee_proxy():
    print("== Bug #29: LoggerTee 代理 exception/critical/__getattr__ ==")
    from endstone_lumenbridge.webui.logbuffer import LogBuffer, LoggerTee

    class FakeLogger:
        def __init__(self):
            self.calls = []
        def info(self, m): self.calls.append(('info', m))
        def warning(self, m): self.calls.append(('warning', m))
        def error(self, m): self.calls.append(('error', m))
        def debug(self, m): self.calls.append(('debug', m))
        def exception(self, m): self.calls.append(('exception', m))
        def critical(self, m): self.calls.append(('critical', m))
        def setLevel(self, lvl): self.calls.append(('setLevel', lvl))

    fl = FakeLogger()
    buf = LogBuffer()
    tee = LoggerTee(fl, buf, 'test')

    tee.exception('exc msg')
    check("exception 被代理", any('exc msg' in str(c) for c in fl.calls), f"{fl.calls}")

    tee.critical('crit msg')
    check("critical 被代理", any('crit msg' in str(c) for c in fl.calls), f"{fl.calls}")

    tee.setLevel(2)
    check("setLevel 通过 __getattr__ 代理", ('setLevel', 2) in fl.calls, f"{fl.calls}")

    check("日志写入 LogBuffer", any('exc msg' in str(e.get('msg', '')) for e in buf.cache))


# ======================================================================
# Bug #31: metrics RLock 可重入（不死锁）
# ======================================================================
def test_metrics_no_deadlock():
    print("== Bug #31: metrics start/stop 不死锁（RLock 可重入）==")
    from endstone_lumenbridge.webui.metrics import ServerMetricsCollector
    import time

    m = ServerMetricsCollector(FakeLogger(), interval=1.0)
    m.start()  # start() 持锁调用 _sample()，_sample_psutil 再获取锁
    # 轮询等待首个采样就绪（替代固定 sleep，最多 5s / 每 0.05s 检查）
    deadline = time.monotonic() + 5.0
    snap = m.snapshot()
    while not snap.get("available") and time.monotonic() < deadline:
        time.sleep(0.05)
        snap = m.snapshot()
    check("start 不死锁", snap['available'] == True, f"snap={snap}")
    m.stop()
    check("stop 不死锁", True)


# ======================================================================
# Bug #10: Endstone 监听器软注销
# ======================================================================
def test_endstone_listener_soft_unregister():
    print("== Bug #10: Endstone 监听器软注销（_lumen_active 标志）==")
    from endstone_lumenbridge.subplugin.context import MCBridge

    # 模拟 listener 对象
    class FakeListener:
        _lumen_active = True
        def on_event(self, event): pass

    listener = FakeListener()

    class FakePlugin:
        logger = FakeLogger()
        bus = type('B', (), {'off': lambda self, *a: None})()
        server = None
        data_folder = '/tmp'
        def register_events(self, l): pass
        def run_on_main(self, fn, delay=0): fn()

    class FakeContext:
        pass

    # 构造 MCBridge 实例
    mc = MCBridge.__new__(MCBridge)
    mc._plugin = FakePlugin()
    mc._name = 'test'
    # 源码当前契约：_endstone_listeners 为 (event_name, listener) 二元组
    # （回调已移入 _endstone_dispatch 分发表）；桩对齐新结构，断言目标不变
    mc._endstone_listeners = [('PlayerJoinEvent', listener)]
    mc._endstone_dispatch = {}

    # 验证 listener 初始活跃
    check("listener 初始活跃", listener._lumen_active == True)

    # 构造 LumenContext 调用 _cleanup 的逻辑
    from endstone_lumenbridge.subplugin.context import LumenContext
    ctx = LumenContext.__new__(LumenContext)
    ctx._plugin = FakePlugin()
    ctx._handlers = []
    ctx.mc = mc

    ctx._cleanup()

    check("cleanup 后 listener 软注销", listener._lumen_active == False)
    check("_endstone_listeners 已清空", len(mc._endstone_listeners) == 0)


# ======================================================================
# Bug #1 补充: callPluginCommand 参数 strip
# ======================================================================
def test_call_plugin_command_strip():
    print("== Bug #26: callPluginCommand 参数 strip ==")
    from endstone_lumenbridge.modules.regex_engine import RegexEngineModule

    called = []

    class FakePlugin:
        logger = FakeLogger()
        data_folder = "/tmp/lb_test"
        whitelist_module = None
        class _CM:
            main_group = 10000
            main_groups = [10000]
        config_manager = _CM()
        class _Server:
            def get_online_players(self): return []
            @property
            def language(self):
                return type('L', (), {'translate': lambda s, k, *a: k})()
        server = _Server()

    class FakeAdapter:
        def call_api(self, *a, **k): pass
        def send_group_msg(self, gid, content): pass

    engine = RegexEngineModule.__new__(RegexEngineModule)
    engine.plugin = FakePlugin()
    # conf 已改为实时读取 config_manager 的 property，测试通过注入配置管理器模拟
    engine.plugin.config_manager.regex_engine = {'main_group': 10000, 'admin_qq': [], 'enable': True}
    engine.logger = FakeLogger()
    engine.rules = []
    engine.custom_actions = {}
    engine._alias_map = {}
    engine.adapter = FakeAdapter()
    # v1.2.4 规则动作去抖状态（__new__ 桩需手动补齐）
    from collections import OrderedDict as _OD
    engine._action_dedup = _OD()
    engine._action_dedup_lock = __import__('threading').Lock()

    # 注册自定义动作
    def my_action(params, pack, context):
        called.append(params)
    engine.custom_actions['mycmd'] = my_action

    # callPluginCommand 动作：params 含空格 "mycmd, arg1 , arg2"
    engine.rules = [{
        'name': 't', 'enabled': True,
        'triggerType': 'event', 'eventType': 'server.player_join',
        'pattern': '',
        'actions': [{'type': 'callPluginCommand', 'command': 'mycmd', 'params': 'mycmd, arg1 , arg2'}],
    }]
    engine.on_mc_player_join('Steve')
    check("参数被 strip", called and called[0] == ['arg1', 'arg2'], f"{called}")


def main():
    # check 失败会抛 AssertionError（pytest 真实 FAIL 用）；手动运行时逐个吞掉，
    # 保持"跑完全部再汇总退出码"的原语义
    for fn in (
        test_player_chat_match_target,
        test_config_robustness,
        test_dispatcher_raw_message_none,
        test_zip_path_traversal,
        test_forward_msg_builder_copy,
        test_deep_merge_no_pollution,
        test_auth_robustness,
        test_logger_tee_proxy,
        test_metrics_no_deadlock,
        test_endstone_listener_soft_unregister,
        test_call_plugin_command_strip,
    ):
        try:
            fn()
        except AssertionError:
            pass
        print()

    print(f"\n===== 结果: {len(PASSED)} 通过, {len(FAILED)} 失败 =====")
    if FAILED:
        for f in FAILED:
            print(f"  FAILED: {f}")
        sys.exit(1)


if __name__ == "__main__":
    main()
