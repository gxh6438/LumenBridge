"""示例子插件：example_greeter

演示 LumenBridge 子插件的完整开发方式：

1. 监听 QQ 群消息事件并快捷回复
2. 使用 lumen.storage 读写私有配置
3. 监听游戏事件（mc.listen）
4. 调用 QClient 发送群消息 / 使用 msgbuilder 构建消息段
5. 向正则引擎注册自定义动作（callPluginCommand 可调用）
6. 使用 lumen.mc.runcmdEx 执行命令并取回输出
"""

# 模块级变量保存上下文，便于各回调使用
lumen = None
conf = {}

DEFAULT_CONFIG = {
    "greet_keyword": "你好",
    "greet_reply": "你好呀，$name！我是 LumenBridge 子插件~",
    "welcome_new_player": True,
}


def on_load(ctx):
    """子插件入口：LumenBridge 加载时调用，ctx 即 lumen 上下文对象"""
    global lumen, conf
    lumen = ctx

    # 1. 读取/初始化私有配置（自动生成 config.json）
    conf = lumen.storage.read("config.json", DEFAULT_CONFIG)

    # 2. 监听 QQ 群消息
    lumen.on("message.group.normal", on_group_message)

    # 3. 监听游戏事件
    if conf.get("welcome_new_player", True):
        lumen.mc.listen("onJoin", on_player_join)

    # 4. 向正则引擎注册自定义动作，规则中可用:
    #    {"type": "callPluginCommand", "params": "greet,$nickname"}
    lumen.register_regex_action("greet", action_greet)

    lumen.logger.info("示例子插件已加载！")


def on_unload(ctx):
    """可选：热重载 / 关服时调用（事件监听会被自动清理，这里做额外收尾）"""
    ctx.logger.info("示例子插件已卸载")


# ---------------------------------------------------------------------------
# 事件回调
# ---------------------------------------------------------------------------

def on_group_message(pack, reply):
    """群消息处理：关键字问候 + '在线人数' 查询演示"""
    if pack.get("group_id") != lumen.env.get("main_group"):
        return
    raw = pack.get("raw_message", "").strip()
    nickname = pack.get("sender", {}).get("nickname", "朋友")

    if raw == conf.get("greet_keyword", "你好"):
        reply(conf.get("greet_reply", "你好！").replace("$name", nickname), True)

    elif raw == "在线人数":
        # 演示 runcmdEx：执行 list 命令并把输出回复到群
        result = lumen.mc.runcmdEx("list")
        reply(result["output"] or "查询失败", True)


def on_player_join(player_name):
    """玩家进服时向群里发送一条欢迎消息（演示 QClient + msgbuilder）"""
    mb = lumen.msgbuilder
    lumen.QClient.send_group_msg(
        lumen.env.get("main_group"),
        [mb.text(f"欢迎 {player_name} 进入服务器！")],
    )


def action_greet(params, pack, context):
    """正则引擎自定义动作：返回值写入 $result 变量"""
    who = params[0] if params else "陌生人"
    return f"来自子插件的问候：{who}！"
