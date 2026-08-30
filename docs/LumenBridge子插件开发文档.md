# LumenBridge 子插件开发文档

> 版本：1.0.0　|　许可证：MIT　|　适用 LumenBridge 1.0.0 及以上

## 开篇介绍

LumenBridge 是基于 Endstone 的 QQ 群服互通插件，负责在 Minecraft 基岩版服务器与 QQ（个人号 OneBot 协议端 / QQ 官方机器人）之间双向传递消息、事件与命令。子插件体系是 LumenBridge 的扩展机制：它把框架核心能力封装成一个注入对象 `lumen`（`LumenContext`），让第三方开发者无需触碰框架内部、只需编写一个 `main.py` 入口和一份 `lumen.json` 清单，就能监听群消息、收发 OneBot 消息、调用游戏命令、读写私有存储、注册 WebUI 配置面板、联动正则引擎、注册游戏内命令，并直达 Endstone 全量 API。子插件支持热重载、自动依赖安装与双域白名单查询，覆盖从简单关键词回复到复杂业务逻辑的全部场景。本文档是子插件开发的完整参考，从最小示例出发，逐节展开每一类 API 的用法与注意事项。

## 快速开始

下面是一个最小可运行子插件：监听群里发送的「你好」，回复一句问候。三个文件部署到 `plugins/lumenbridge/subplugins/hello/` 即可。

**main.py**

```python
lumen = None


def on_load(ctx):
    global lumen
    lumen = ctx
    lumen.on("message.group.normal", on_group_message)
    lumen.logger.info("hello 子插件已加载")


def on_group_message(pack, reply):
    # 只处理主群消息
    if pack.get("group_id") != lumen.env.get("main_group"):
        return
    if pack.get("raw_message", "").strip() == "你好":
        reply("你好，我是 LumenBridge 子插件～", True)
```

**lumen.json**

```json
{
    "name": "hello",
    "version": "1.0.0",
    "author": "you",
    "desc": "最小示例子插件",
    "load": true,
    "priority": "post",
    "min_v": "",
    "dependencies": []
}
```

**三步部署**

1. 在服务器目录下创建子插件目录 `plugins/lumenbridge/subplugins/hello/`。
2. 将上面的 `main.py` 与 `lumen.json` 放入该目录。
3. 执行 `/lumen reload`（或通过 WebUI 触发热重载），群里发送「你好」即可看到回复。

如果 `lumen.json` 缺失，LumenBridge 会以目录名作为插件名自动生成一份默认清单并加载，但建议始终显式提供清单以便声明版本与依赖。

## 子插件目录结构

子插件全部位于 `plugins/lumenbridge/subplugins/` 下，每个子插件独占一个以插件名命名的子目录。一个规范的子插件目录长这样：

```
plugins/lumenbridge/subplugins/
└── my_plugin/
    ├── main.py          # 入口文件，必须定义 on_load(ctx)
    ├── lumen.json       # 清单文件，声明元信息与依赖
    ├── config.json      # （可选）由 storage 读写产生
    └── data/            # （可选）私有数据文件
```

`main.py` 是唯一入口，LumenBridge 加载时调用其中的 `on_load(ctx)`，`ctx` 即 `LumenContext` 对象（惯例命名为 `lumen`）。可选的 `on_unload(ctx)` 在热重载或关服时调用，用于额外收尾；事件监听器、定时任务等资源会在卸载时被框架自动清理，无需手动注销。

### lumen.json 清单字段

| 字段 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| name | str | 是 | 目录名 | 插件名，唯一标识。仅允许字母、数字、下划线、连字符 |
| version | str | 否 | "1.0.0" | 语义化版本号，用于升级判断与市场展示 |
| author | str | 否 | "unknown" | 作者名 |
| desc | str | 否 | "" | 一句话描述，显示在 `/lumen plugins` 与 WebUI |
| load | bool | 否 | true | 是否加载。设为 false 则跳过加载但保留记录供开关 |
| priority | str | 否 | "post" | 加载优先级，可选 "pre" / "main" / "post"，按此顺序分段加载 |
| min_v | str | 否 | "" | 最低兼容的 LumenBridge 版本，高于当前版本则拒绝加载 |
| dependencies | list[str] | 否 | [] | 第三方 pip 依赖声明，如 ["pillow>=10.0.0"]，缺失时自动安装 |

清单文件采用原子写入（临时文件 + 替换），即使写入中途断电也不会留下半个 JSON。如果清单内容损坏（非法 JSON、非 UTF-8、非对象），LumenBridge 会跳过该子插件并记录错误，绝不回退默认值继续执行其代码。

## LumenContext API 总览

每个子插件加载时获得独立的 `LumenContext` 实例（惯例命名 `lumen`），它聚合了框架的全部对外能力。下表按功能分类列出 `lumen.*` 的所有成员，后续各节展开说明。

### 日志与元信息

| 成员 | 类型 | 说明 |
|------|------|------|
| lumen.logger | PrefixedLogger | 带子插件名前缀的日志器，提供 info / warning / error / debug |
| lumen.VERSION | str | LumenBridge 当前版本号，如 "1.0.0" |
| lumen.debug | bool | 是否开启调试模式（读取配置） |
| lumen.pluginName | str | 当前子插件名 |

### 事件系统

| 成员 | 说明 |
|------|------|
| lumen.on(event, handler) | 注册持久监听器，返回 handler |
| lumen.once(event, handler) | 注册一次性监听器，触发一次后自动移除 |
| lumen.off(event, handler) | 移除指定监听器 |
| lumen.emit(event, *args, **kwargs) | 触发事件 |

### OneBot 消息

| 成员 | 说明 |
|------|------|
| lumen.QClient | 适配器中枢，双域自动路由，封装全部 OneBot 动作 |
| lumen.call_action(action, params, callback, timeout) | 通用 OneBot action 调用入口 |
| lumen.msgbuilder | 消息段构建器（text / at / face / image / reply / poke / video / record / format_message） |
| lumen.packbuilder | 数据包构建器（构造原始 action 请求） |

### 游戏接口

| 成员 | 说明 |
|------|------|
| lumen.mc.listen(event_name, callback) | 监听游戏事件，支持别名或 Endstone 事件类名 |
| lumen.mc.runcmd(cmd) | 异步执行命令，返回 bool |
| lumen.mc.runcmdEx(cmd, timeout=5) | 同步执行命令并捕获输出，返回 {"success", "output"} |
| lumen.mc.broadcast(message) | 全服广播文本 |
| lumen.mc.online_players | 在线玩家名列表（线程安全快照） |

### 数据持久化

| 成员 | 说明 |
|------|------|
| lumen.storage.read(filename, default=None) | 读私有 JSON，损坏自动备份 |
| lumen.storage.write(filename, data) | 写私有 JSON，原子写入 |
| lumen.storage.path(filename) | 获取存储文件绝对路径 |

### 环境变量池

| 成员 | 说明 |
|------|------|
| lumen.env.get(key, default) | 读取共享变量（main_group / main_groups / admin_qq 等内置键） |
| lumen.env.set(key, value) | 写入共享变量，触发 env.update.\<key\> 与 env.update 事件 |

### Web 扩展

| 成员 | 说明 |
|------|------|
| lumen.web.createConfig(name) | 创建 WebUI 配置表单构建器（ConfigFormBuilder） |
| lumen.web.registerApi(method, path, handler, need_auth) | 注册 REST API |
| lumen.web.registerPage(title, relative_path) | 注册自定义页面 |

### 国际化

| 成员 | 说明 |
|------|------|
| lumen.i18n.language | 当前语言代码 |
| lumen.i18n.lang | 当前语言代码（别名） |
| lumen.i18n.register_namespace(ns, translations) | 注册命名空间翻译 |
| lumen.i18n.unregister_namespace(ns) | 卸载命名空间 |
| lumen.i18n.tn(ns, key, **kwargs) | 翻译命名空间键，支持 {placeholder} |
| lumen.i18n.t(key, **kwargs) | 翻译主语言包键 |
| lumen.i18n.available_languages() | 返回可用语言列表 |

### 正则引擎

| 成员 | 说明 |
|------|------|
| lumen.register_regex_action(action_type, handler) | 注册自定义动作，可在 rules.json 中用 callPluginCommand 调用 |

### 命令注册

| 成员 | 说明 |
|------|------|
| lumen.register_command(name, handler, description, aliases, usages) | 注册 Endstone 游戏内命令 |

### 主线程调度

| 成员 | 说明 |
|------|------|
| lumen.run_on_main(func, delay=1) | 把函数调度到游戏主线程执行 |

### Endstone API 直达

| 成员 | 说明 |
|------|------|
| lumen.endstone | endstone 顶级模块，随版本升级自动获得新 API |
| lumen.plugin | LumenBridge 插件实例 |
| lumen.server | Endstone Server 对象 |
| lumen.scheduler | Endstone 任务调度器（带任务追踪，卸载自动 cancel） |
| lumen.import_module(name) | 导入任意 endstone 子模块 |
| lumen.get_player(name_or_uuid) | 获取在线玩家对象 |

### 白名单辅助

| 成员 | 说明 |
|------|------|
| lumen.domain_of(pack) | 判断事件包所属域，返回 "official" 或 "qq" |
| lumen.get_xbox_by_pack(pack) | 按事件包发送者查绑定 XboxID |
| lumen.get_xbox_by_qq(qq, domain) | 按 QQ 号或 openid 查绑定 XboxID |

## 日志与元信息

子插件自身的运行信息通过 `lumen.logger` 输出，每条日志自动加上 `[插件名]` 前缀，便于在控制台与 WebUI 日志缓冲区里区分来源。日志器提供四个标准级别，其中 `debug` 级别受配置项 `debug` 控制，关闭时不会输出也不产生开销。

`lumen.VERSION` 是 LumenBridge 当前运行版本，可用于特性探测或与 `lumen.json` 的 `min_v` 配合做兼容判断。`lumen.debug` 读取配置实时返回布尔值，`lumen.pluginName` 即子插件名，常用于在通用工具函数里标记日志来源。

| 成员 | 类型 | 说明 |
|------|------|------|
| lumen.logger.info(msg) | method | 输出 INFO 级日志 |
| lumen.logger.warning(msg) | method | 输出 WARNING 级日志 |
| lumen.logger.error(msg) | method | 输出 ERROR 级日志 |
| lumen.logger.debug(msg) | method | 输出 DEBUG 级日志（受 debug 开关控制） |
| lumen.VERSION | str | LumenBridge 版本号，如 "1.0.0" |
| lumen.debug | bool | 是否开启调试模式 |
| lumen.pluginName | str | 当前子插件名 |

```python
def on_load(ctx):
    global lumen
    lumen = ctx
    if lumen.debug:
        lumen.logger.debug(f"运行于 LumenBridge {lumen.VERSION}，调试模式已开启")
    lumen.logger.info(f"{lumen.pluginName} 启动完成")
```

日志器本身是线程安全的，可在 OneBot 异步线程与游戏主线程任意调用，无需加锁。

## 事件系统

LumenBridge 内置线程安全的事件总线，事件名沿用 OneBot 上报类型的分层格式（如 `message.group.normal`、`notice.group_increase`）。子插件通过 `lumen.on` 注册的监听器会被框架登记，热重载或卸载时自动移除，不必担心残留。

四个核心方法签名如下：

| 方法 | 参数 | 说明 |
|------|------|------|
| lumen.on(event, handler) | event: str, handler: Callable | 注册持久监听器，返回 handler。同一 handler 重复注册会去重 |
| lumen.once(event, handler) | event: str, handler: Callable | 注册一次性监听器，触发一次后自动移除 |
| lumen.off(event, handler) | event: str, handler: Callable | 移除指定监听器 |
| lumen.emit(event, *args, **kwargs) | event: str, 任意参数 | 触发事件，逐个调用监听器。单个监听器抛异常不影响其他监听器 |

### 事件层级

OneBot 原始数据包到达后，分发器按 `post_type` 拆成细粒度事件并注入 `reply` 快捷回复函数。常用的内置事件层级如下：

| 事件名 | 触发时机 | 回调签名 |
|--------|----------|----------|
| onebot.pack | 任意原始数据包到达 | handler(pack) |
| message.group.normal | 群普通消息 | handler(pack, reply) |
| message.group.anonymous | 群匿名消息 | handler(pack, reply) |
| message.private.friend | 私聊消息 | handler(pack, reply) |
| notice.group_increase | 群成员增加（入群） | handler(pack) |
| notice.group_decrease | 群成员减少（退群/被踢） | handler(pack) |
| notice.group_ban | 群禁言 | handler(pack) |
| notice.group_admin | 群管理员变动 | handler(pack) |
| notice.group_upload | 群文件上传 | handler(pack) |
| notice.notify_poke | 戳一戳 | handler(pack) |
| request.group | 加群请求 | handler(pack) |
| request.friend | 加好友请求 | handler(pack) |
| meta_event.heartbeat | 心跳 | handler(pack) |
| meta_event.lifecycle | 生命周期 | handler(pack) |
| bot.online | 适配器上线 | handler(adapter) |
| bot.offline | 适配器离线 | handler(adapter) |
| env.update | 共享变量被 set | handler(key, value) |
| env.update.\<key\> | 指定键被 set | handler(value) |
| config.update.\<name\> | WebUI 配置表单保存 | handler(key, value) |
| mc.player_join | 玩家进服 | handler(player_name) |
| mc.player_left | 玩家离服 | handler(player_name) |
| mc.player_chat | 玩家聊天 | handler(player_name, message) |
| mc.player_death | 玩家死亡 | handler(player_name) |

消息事件的 `pack` 是 OneBot 原始事件字典，`reply` 是快捷回复函数，签名 `reply(msg, quote=False)`：`msg` 可为字符串或消息段列表，`quote=True` 时会在消息前插入对原消息的引用。非消息事件（notice / request / meta_event）的回调只有 `pack` 单个参数。

```python
def on_load(ctx):
    global lumen
    lumen = ctx

    # 群消息：含快捷回复
    lumen.on("message.group.normal", on_group_message)

    # 入群事件：单参 pack
    lumen.on("notice.group_increase", on_member_join)

    # 一次性监听：适配器首次上线后做初始化
    lumen.once("bot.online", on_first_online)

    # 监听环境变量变更
    lumen.on("env.update", on_env_update)


def on_group_message(pack, reply):
    if pack.get("raw_message", "").strip() == "ping":
        reply("pong", True)


def on_member_join(pack):
    gid = pack.get("group_id")
    uid = pack.get("user_id")
    lumen.logger.info(f"群 {gid} 新成员 {uid} 入群")


def on_first_online(adapter):
    lumen.logger.info("OneBot 适配器已上线，开始拉取群列表")
```

子插件也可以用 `lumen.emit` 触发自定义事件，其他子插件或框架模块可监听该事件实现跨插件通信。事件总线的监听器注册是线程安全的，但回调执行本身不在主线程，涉及 Endstone API 时需配合 `lumen.run_on_main`。

## OneBot 消息收发

`lumen.QClient` 是 OneBot 适配器中枢，封装了发送消息、管理群成员、获取信息等全部动作。它运行在独立的 asyncio 线程上，因此所有方法都可以在任意线程直接调用，框架会线程安全地把请求送入发送队列。

### 双域自动路由

LumenBridge 同时支持个人号 OneBot 协议端与 QQ 官方机器人。事件包通过 `domain` 字段区分来源：`"official"` 表示官方机器人（用户标识为 openid），`"qq"` 表示个人号（用户标识为 QQ 号）。`lumen.QClient` 发送消息时由适配器根据目标 ID 自动路由到对应域：纯数字群号走个人号域，openid 走官方域。子插件通常无需关心路由细节，直接调用 `send_group_msg` 即可。

### 发送消息

| 方法 | 参数 | 说明 |
|------|------|------|
| lumen.QClient.send_group_msg(group_id, message) | group_id: int/str, message: str/Segment/list | 发送群消息，message 自动经 format_message 规范化 |
| lumen.QClient.send_private_msg(user_id, message) | user_id: int/str, message: str/Segment/list | 发送私聊消息 |
| lumen.QClient.send_group_forward_msg(group_id, messages) | group_id: int, messages: list | 发送合并转发消息，节点由 ForwardMessageBuilder 构建 |
| lumen.QClient.delete_msg(message_id) | message_id: int | 撤回消息 |

```python
mb = lumen.msgbuilder

# 发送纯文本
lumen.QClient.send_group_msg(lumen.env.get("main_group"), "服务器已开服")

# 发送富文本：@某人 + 文本
lumen.QClient.send_group_msg(
    lumen.env.get("main_group"),
    [mb.at(123456), mb.text(" 欢迎回来")],
)

# 合并转发
fwd = mb.ForwardMessageBuilder()
fwd.add_custom_message("小助手", 10000, "第一条")
fwd.add_custom_message("小助手", 10000, "第二条")
lumen.QClient.send_group_forward_msg(lumen.env.get("main_group"), fwd.build())

# 撤回消息
lumen.QClient.delete_msg(pack["message_id"])
```

### 群管理操作

| 方法 | 参数 | 说明 |
|------|------|------|
| lumen.QClient.set_group_ban(group_id, user_id, duration) | group_id, user_id: int, duration: int(秒) | 禁言某成员，duration=0 解禁 |
| lumen.QClient.set_group_whole_ban(group_id, enable) | group_id: int, enable: bool | 全群禁言开关 |
| lumen.QClient.set_group_kick(group_id, user_id, reject) | group_id, user_id: int, reject: bool | 踢出成员，reject=True 拒绝其再次加群 |
| lumen.QClient.set_group_card(group_id, user_id, card) | group_id, user_id: int, card: str | 设置群名片 |
| lumen.QClient.set_group_name(group_id, name) | group_id: int, name: str | 修改群名 |
| lumen.QClient.set_group_admin(group_id, user_id, enable) | group_id, user_id: int, enable: bool | 设置/取消管理员 |
| lumen.QClient.set_group_special_title(group_id, user_id, title, duration) | group_id, user_id: int, title: str, duration: int | 设置专属头衔 |
| lumen.QClient.set_group_leave(group_id, dismiss) | group_id: int, dismiss: bool | 退群 |

```python
# 禁言某成员 10 分钟
lumen.QClient.set_group_ban(group_id, user_id, 600)

# 踢出并拒绝再次加群
lumen.QClient.set_group_kick(group_id, user_id, reject=True)

# 修改群名片
lumen.QClient.set_group_card(group_id, user_id, "新名片")
```

### 官方机器人入群审批

`set_group_add_request` 用于处理加群请求，官方机器人收到加群请求时通过它审批：

| 方法 | 参数 | 说明 |
|------|------|------|
| lumen.QClient.set_group_add_request(flag, sub_type, approve, reason) | flag: str, sub_type: str, approve: bool, reason: str | 处理加群请求。flag 与 sub_type 来自 request 事件包 |
| lumen.QClient.set_friend_add_request(flag, approve) | flag: str, approve: bool | 处理加好友请求 |

```python
def on_group_request(pack):
    # 自动同意主群的加群请求
    if pack.get("group_id") in lumen.env.get("main_groups"):
        lumen.QClient.set_group_add_request(
            pack["flag"], pack.get("sub_type", "add"), True,
        )

lumen.on("request.group", on_group_request)
```

### 回调式信息查询

获取群成员列表、成员信息、消息内容等需要协议端返回数据的动作采用回调式：调用时传入 `callback`，回执到达后在 WebSocket 线程回调 `callback(data)`。`data` 为 OneBot 返回的数据体，超时或失败时回调 `None`。

| 方法 | 参数 | 说明 |
|------|------|------|
| lumen.QClient.get_group_member_list(group_id, callback) | group_id: int, callback: Callable | 获取群成员列表 |
| lumen.QClient.get_group_member_info(group_id, user_id, callback) | group_id, user_id: int, callback: Callable | 获取单个成员信息 |
| lumen.QClient.get_msg(message_id, callback) | message_id: int, callback: Callable | 获取消息内容 |
| lumen.QClient.get_group_list(callback) | callback: Callable | 获取群列表 |
| lumen.QClient.get_login_info(callback) | callback: Callable | 获取登录号信息 |
| lumen.QClient.get_stranger_info(user_id, callback) | user_id: int, callback: Callable | 获取陌生人信息 |

```python
def on_load(ctx):
    global lumen
    lumen = ctx
    lumen.once("bot.online", on_online)


def on_online(adapter):
    # 上线后查询主群成员列表
    lumen.QClient.get_group_member_list(
        lumen.env.get("main_group"), on_member_list
    )


def on_member_list(data):
    if not data:
        lumen.logger.warning("获取群成员列表失败")
        return
    lumen.logger.info(f"群成员数：{len(data)}")
```

### 通用 action 调用

如果需要调用上述便捷方法未覆盖的 OneBot 动作（如各协议端的扩展 API），用 `lumen.call_action` 或 `lumen.QClient.call_action` 直接发起任意 action：

| 方法 | 参数 | 说明 |
|------|------|------|
| lumen.call_action(action, params, callback, timeout) | 见下表 | 通用 action 调用入口 |
| lumen.QClient.call_action(action, params, callback, timeout) | 同上 | 适配器上的等价方法 |

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| action | str | 是 | OneBot 动作名，如 "send_group_msg" |
| params | dict | 否 | 动作参数字典 |
| callback | Callable | 否 | 回执回调，None 时为 fire-and-forget 发送 |
| timeout | float | 否 | 超时秒数，默认 10.0 |

```python
# 调用 NapCat 扩展：群内戳一戳
lumen.call_action(
    "group_poke",
    {"group_id": 123456, "user_id": 10001},
)

# 带回调的调用：获取版本信息
lumen.call_action("get_version_info", {}, on_version)

def on_version(data):
    if data:
        lumen.logger.info(f"协议端：{data.get('app_name')} {data.get('app_version')}")
```

### QQ 官方机器人的 action 支持

个人号适配器（OneBot 协议端）天然支持全量 OneBot action；QQ 官方适配器自 v1.0.4 起实现了 `call_action` 分发，下表动作会映射到 QQ 开放平台官方端点，未列出的动作回调 `None` 并记录警告日志（官方协议无对应能力，如踢人、设群名片）：

| action | 官方端点 | 说明与限制 |
|--------|----------|-----------|
| send_group_msg / send_private_msg | /v2/groups 或 /v2/users messages | 等价便捷方法 |
| delete_msg | DELETE /v2/groups/{g}/messages/{id} | 仅群聊；自己的消息限 2 分钟内，管理员可撤成员消息 |
| set_group_add_request | POST /v2/groups/{g}/approval_join_request/{m} | 需群管理员 |
| **set_group_ban** | POST /v2/groups/{g}/restrict_chat_setting | OneBot v11 标准禁言；`duration` 秒（0=解禁），最长 30 天，仅能禁普通成员，需群管理员 |
| set_group_ban_batch（官方扩展） | 同上 | 批量禁言，`user_ids` 列表单次最多 10 人 |
| get_group_restrict_chat_setting（官方扩展） | GET /v2/groups/{g}/restrict_chat_setting | 查询全员禁言规则与当前禁言成员列表，需群管理员 |
| get_group_bot_state（官方扩展） | GET /v2/groups/{g}/bot_state | 查询机器人在群内的角色（member/admin/owner）、主动消息开关；白名单接口 |
| get_group_join_request_list（官方扩展） | GET /v2/groups/{g}/join_request_list | 拉取待审批入群申请（cursor/limit 分页），需群管理员 |
| get_group_info | GET /v2/groups/{g}/info | 官方端点优先（白名单），失败自动回退本地兜底 |
| get_group_list / get_login_info | 本地 | 同便捷方法 |

```python
# 官方 bot 禁言某成员 10 分钟（group_id 为 group_openid，user_id 为 member_openid）
lumen.call_action("set_group_ban", {"group_id": gid, "user_id": uid, "duration": 600})

# 解除禁言
lumen.call_action("set_group_ban", {"group_id": gid, "user_id": uid, "duration": 0})

# 查询群内当前禁言列表
def on_mute_list(data):
    for m in (data or {}).get("members", []):
        lumen.logger.info(f"{m['username']} 禁言至 {m['mute_expire_at']}")
lumen.call_action("get_group_restrict_chat_setting", {"group_id": gid}, on_mute_list)
```

### 消息段构建器

`lumen.msgbuilder` 提供构造 OneBot 消息段的函数。字符串会被 `format_message` 自动转成 text 段，因此发送时可以混用字符串与消息段。

| 函数 | 参数 | 说明 |
|------|------|------|
| msgbuilder.text(raw) | raw: str | 文本段 |
| msgbuilder.at(qq) | qq: int/str | @某人 |
| msgbuilder.face(face_id) | face_id: int/str | QQ 表情 |
| msgbuilder.image(file, sub_type) | file: str/bytes, sub_type: int | 图片（本地路径/URL/base64/bytes） |
| msgbuilder.reply(message_id) | message_id: int/str | 回复引用 |
| msgbuilder.poke(qq) | qq: int/str | 戳一戳 |
| msgbuilder.video(file) | file: str | 视频 |
| msgbuilder.record(file) | file: str | 语音 |
| msgbuilder.format_message(msg) | msg: str/Segment/list | 统一格式化，字符串转 text 段，单段包装为列表 |
| msgbuilder.ForwardMessageBuilder() | 无 | 合并转发构建器，链式 add 后 build() |
| msgbuilder.decode_cq_entities(raw) | raw: str | 解码 OneBot raw_message 中的 HTML 实体 |

```python
mb = lumen.msgbuilder

# 图片（bytes 自动转 base64）
with open("banner.png", "rb") as f:
    img_bytes = f.read()
lumen.QClient.send_group_msg(gid, [mb.image(img_bytes), mb.text(" 活动开始啦")])

# 合并转发
fwd = mb.ForwardMessageBuilder()
fwd.add_message_by_id(111)
fwd.add_custom_message("公告员", 9999, [mb.text("今日维护完成")])
lumen.QClient.send_group_forward_msg(gid, fwd.build())
```

注意 `image` 读取本地文件受白名单根目录限制：只有在框架注册的数据目录内的本地路径才会被读取并转 base64，白名单外的路径按原始字符串透传（当作 URL/标识符），以防任意文件读取导致信息泄露。

### 数据包构建器

`lumen.packbuilder` 是更底层的工具，提供与每个 OneBot action 一一对应的构建函数，返回 `{"action", "params", "echo"}` 字典。子插件通常用不到它——`lumen.QClient` 的便捷方法已覆盖常见动作；只有在需要自定义发送流程时才用它配合 `lumen.QClient.send_pack` 或 `call_api`。

```python
# 等价于 send_group_msg，但手动构建数据包
pack = lumen.packbuilder.group_message(123456, "手动构建的消息")
lumen.QClient.send_pack(pack)
```

## 游戏接口

`lumen.mc` 是 Minecraft 侧的桥接对象，封装了事件监听、命令执行、广播与在线玩家查询。所有命令执行都在内部切到游戏主线程完成，因此子插件可以在 OneBot 异步线程里直接调用而不必自行处理线程切换。

### 监听游戏事件

`lumen.mc.listen(event_name, callback)` 注册游戏事件监听器。事件名既可以用易记的别名，也可以用 Endstone 官方事件类名，框架会按类名反射构造监听器。别名与内部事件的映射如下：

| 别名 | 内部事件 | 回调签名 |
|------|----------|----------|
| onJoin | mc.player_join | callback(player_name) |
| onLeft | mc.player_left | callback(player_name) |
| onChat | mc.player_chat | callback(player_name, message) |
| onDeath | mc.player_death | callback(player_name) |

| 方法 | 参数 | 说明 |
|------|------|------|
| lumen.mc.listen(event_name, callback) | event_name: str, callback: Callable | 监听游戏事件。别名走内部事件总线；Endstone 事件类名走原生监听器，回调收原生事件对象。返回 bool 表示是否注册成功 |

```python
def on_load(ctx):
    global lumen
    lumen = ctx
    lumen.mc.listen("onJoin", on_player_join)
    lumen.mc.listen("onChat", on_player_chat)

    # 也可用 Endstone 官方事件类名，回调收原生事件对象
    lumen.mc.listen("PlayerCommandEvent", on_player_command)


def on_player_join(player_name):
    lumen.mc.broadcast(f"欢迎 {player_name} 加入服务器")
    lumen.QClient.send_group_msg(
        lumen.env.get("main_group"),
        [lumen.msgbuilder.text(f"{player_name} 进服了")],
    )


def on_player_chat(player_name, message):
    # 同步玩家聊天到群
    lumen.QClient.send_group_msg(lumen.env.get("main_group"), f"<{player_name}> {message}")
```

### 执行命令

| 方法 | 参数 | 返回 | 说明 |
|------|------|------|------|
| lumen.mc.runcmd(cmd) | cmd: str | bool | 异步执行命令（内部切主线程），返回 dispatch_command 的真实结果。超时或异常返回 False |
| lumen.mc.runcmdEx(cmd, timeout=5) | cmd: str, timeout: float | {"success": bool, "output": str} | 同步执行命令并捕获输出。output 已去除颜色代码（§.）。不要在游戏主线程调用，否则死锁 |
| lumen.mc.broadcast(message) | message: str | 无 | 全服广播文本 |

```python
# 执行命令只关心是否成功
ok = lumen.mc.runcmd("weather rain")

# 执行命令并取回输出
result = lumen.mc.runcmdEx("list", timeout=5)
if result["success"]:
    reply(result["output"] or "当前无玩家在线")

# 全服广播
lumen.mc.broadcast("活动即将开始，请各位做好准备")
```

`runcmdEx` 内部用 `CommandSenderWrapper` 捕获命令的正常与错误输出，超时后会取消排队中的任务避免副作用。命令开头的 `/` 会被自动去掉。

### 在线玩家

| 成员 | 类型 | 说明 |
|------|------|------|
| lumen.mc.online_players | list[str] | 在线玩家名列表（线程安全快照）。阻塞主线程最多 2 秒，勿在主线程调用 |

```python
players = lumen.mc.online_players
reply(f"当前在线 {len(players)} 人：{', '.join(players) if players else '无'}")
```

## 数据持久化

`lumen.storage` 为每个子插件提供隔离的私有 JSON 存储空间，所有读写都加锁，文件位于子插件目录下。子插件之间互不可见，也避免并发写入互相覆盖或读到半截 JSON。

| 方法 | 参数 | 返回 | 说明 |
|------|------|------|------|
| lumen.storage.read(filename, default=None) | filename: str, default: Any | Any | 读私有 JSON。文件不存在或损坏时返回 default；损坏文件会自动备份为 .corrupt-\<时间戳\> |
| lumen.storage.write(filename, data) | filename: str, data: Any | 无 | 写私有 JSON。tmp 文件 + 原子替换，防进程中断留下半个 JSON |
| lumen.storage.path(filename) | filename: str | str | 获取存储文件绝对路径。filename 为空时返回存储目录本身 |

`filename` 必须是相对路径，绝对路径与目录穿越（`..`）会被拒绝并抛 `ValueError`。文件可带子目录，缺失的父目录会自动创建。

```python
DEFAULT_CONFIG = {
    "greet_keyword": "你好",
    "greet_reply": "你好呀，$name！",
    "welcome_new_player": True,
    "stats": {"greet_count": 0},
}

def on_load(ctx):
    global lumen, conf
    lumen = ctx
    # 首次加载自动写入默认配置
    conf = lumen.storage.read("config.json", DEFAULT_CONFIG)
    # 补全缺失字段
    changed = False
    for k, v in DEFAULT_CONFIG.items():
        if k not in conf:
            conf[k] = v
            changed = True
    if changed:
        lumen.storage.write("config.json", conf)


def on_group_message(pack, reply):
    raw = pack.get("raw_message", "").strip()
    if raw == conf["greet_keyword"]:
        conf["stats"]["greet_count"] += 1
        lumen.storage.write("config.json", conf)
        reply(f"已问候 {conf['stats']['greet_count']} 次")
```

## 环境变量池

`lumen.env` 是跨子插件共享的变量池，用于在子插件之间传递状态，也提供几个由框架维护的内置键。其中 `main_group` 具有多群自适应特性：在事件回调中返回当前来源群号，在回调外返回配置的首个主群，兼容旧插件 `if gid == env.get("main_group")` 的写法。

| 方法 | 参数 | 返回 | 说明 |
|------|------|------|------|
| lumen.env.get(key, default) | key: str, default: Any | Any | 读取共享变量。内置键：main_group（多群自适应）、main_groups（主群列表）、admin_qq（管理员 QQ 集合） |
| lumen.env.set(key, value) | key: str, value: Any | 无 | 写入共享变量，并依次触发 env.update.\<key\>(value) 与 env.update(key, value) 事件 |

```python
def on_load(ctx):
    global lumen
    lumen = ctx

    # 读取主群与管理员
    main_group = lumen.env.get("main_group")
    admins = lumen.env.get("admin_qq", [])
    lumen.logger.info(f"主群：{main_group}，管理员：{admins}")

    # 写入自定义共享变量，触发 env.update.my_flag 事件
    lumen.env.set("my_flag", True)

    # 监听其他子插件写入的变量
    lumen.on("env.update.my_flag", on_my_flag_changed)


def on_group_message(pack, reply):
    # 多群自适应：回调内 main_group 即来源群
    if pack.get("group_id") != lumen.env.get("main_group"):
        return
    reply("收到")


def on_my_flag_changed(value):
    lumen.logger.info(f"my_flag 被改为 {value}")
```

`set` 触发的事件让子插件能响应配置或状态变更，是构建跨插件联动的基础。注意事件回调在持有锁的线程上触发，回调内避免长时间阻塞。

## Web 扩展

`lumen.web` 让子插件能向 LumenBridge 的 WebUI 注册三类扩展：配置表单、REST API、自定义页面。所有注册都会在子插件卸载时自动撤销，热重载不会让旧 handler 残留。

### 配置表单

`lumen.web.createConfig(name)` 返回一个 `ConfigFormBuilder`，用链式 API 声明字段后调用 `register()` 完成注册。表单保存时框架会触发 `config.update.<name>` 事件，回调收到 `(key, value)`，子插件据此同步本地文件。

#### 字段类型

| 方法 | 参数 | 说明 |
|------|------|------|
| section(title, desc="") | title: str | 分组标题。声明后后续字段归属该分组卡片，直到下一个 section；仅影响布局，不参与配置读写 |
| text(key, val, desc, label, *, secret=False) | key: str, val: str | 文本输入；secret=True 渲染为密码框（带明文切换按钮） |
| number(key, val, desc, label, *, minimum=None, maximum=None, step=None) | key: str, val: float | 数字输入；同时声明 minimum/maximum 时渲染为滑块+数字框联动 |
| switch(key, val, desc, label) | key: str, val: bool | 开关 |
| select(key, val, options, desc, label) | key, val, options: list | 下拉选择，options 为纯值列表或 {"value","label"} dict 列表 |
| multiselect(key, val, options, desc, label) | key, val: list, options: list | 多选，渲染为复选框组，val 为已选值列表；options 规范化规则同 select |
| array(key, val, desc, label) | key: str, val: list | 数组，渲染为 chips 列表编辑器（逐项增删 + 按行批量导入） |
| textarea(key, val, desc, label) | key: str, val: str | 多行文本，右上角提供全屏编辑入口 |
| file(key, val, desc, label, accept="image/*", upload_url="") | 见说明 | 文件上传，accept 限制类型，upload_url 指定上传端点；保存 JSON 时跳过，上传由独立端点处理 |
| register() | 无 | 完成构建并注册到 WebUI |

#### 通用可选参数

除 `file` 外，所有字段方法均支持以下关键字参数：

| 参数 | 类型 | 默认 | 说明 |
|------|------|------|------|
| default | any | None | 默认值。未显式声明时以注册时的初始值为默认值；前端据此提供「单项恢复默认」与「全部恢复默认」 |
| obvious_hint | bool | False | 标记为重要配置项，字段名旁显示 ‼️ 提示 |
| show_key | bool | False | 在字段名旁显示配置键名（等宽字体），方便对照代码 |

#### WebUI 渲染特性

配置面板为 AstrBot 风格的两列行式布局（左侧名称+描述、右侧控件），并内置以下交互：

- **分组卡片**：`section()` 声明的分组渲染为嵌套卡片，形成层级感；
- **搜索过滤**：顶部搜索框按 key / label / desc 模糊匹配，无命中的分组整组隐藏；
- **恢复默认**：当前值 ≠ 默认值的字段右侧出现恢复按钮，底部提供一键全部恢复；
- **未保存拦截**：任意输入即标记为「未保存的更改」，关闭弹窗前弹出确认；支持 Ctrl/Cmd+S 快捷保存；
- **类型感知控件**：secret 密码遮罩、数字滑块联动、多选复选框组、chips 列表编辑器（支持批量导入）、textarea 全屏编辑。

#### 完整示例

下面是一个包含分组、text、switch、select、multiselect、number（滑块）、array 字段的完整示例：

```python
DEFAULT_CONFIG = {
    "trigger": "服务器状态",
    "show_weather": True,
    "mode": "full",
    "notify_events": ["join", "leave"],
    "interval": 30,
    "token": "",
    "admins": ["10001", "10002"],
}

_MODE_OPTIONS = [
    {"value": "full", "label": "完整模式"},
    {"value": "simple", "label": "精简模式"},
]

_EVENT_OPTIONS = [
    {"value": "join", "label": "玩家加入"},
    {"value": "leave", "label": "玩家退出"},
    {"value": "death", "label": "玩家死亡"},
]


def on_load(ctx):
    global lumen, conf
    lumen = ctx
    conf = lumen.storage.read("config.json", DEFAULT_CONFIG)

    # 构建配置表单
    builder = lumen.web.createConfig("my_plugin")
    builder.section("基本设置", "触发与输出行为")
    builder.text("trigger", conf.get("trigger", "服务器状态"),
                 desc="群内发送该关键字触发查询", label="触发关键字")
    builder.switch("show_weather", conf.get("show_weather", True),
                   desc="是否显示天气", label="显示天气")
    builder.select("mode", conf.get("mode", "full"), _MODE_OPTIONS,
                   desc="输出详细程度", label="输出模式")
    builder.section("通知设置", "事件推送与轮询")
    builder.multiselect("notify_events", conf.get("notify_events", []), _EVENT_OPTIONS,
                        desc="选择要推送的游戏事件", label="推送事件")
    builder.number("interval", conf.get("interval", 30),
                   desc="轮询间隔（秒）", label="轮询间隔",
                   minimum=10, maximum=300, step=5)
    builder.section("安全设置", "凭证与权限")
    builder.text("token", conf.get("token", ""),
                 desc="API 访问令牌", label="访问令牌", secret=True, obvious_hint=True)
    builder.array("admins", conf.get("admins", []),
                  desc="允许触发的管理员 QQ 列表", label="管理员列表")
    builder.register()

    # 监听表单保存
    lumen.on("config.update.my_plugin", on_config_update)


def on_config_update(key, value):
    conf[key] = value
    lumen.storage.write("config.json", conf)
    lumen.logger.info(f"配置 {key} 已更新为 {value}")
```

### REST API

`lumen.web.registerApi(method, path, handler, need_auth=True)` 注册一个 REST 接口，完整路径为 `/api/plugin/<path>`。`need_auth=True`（默认）要求请求携带 WebUI token；设为 False 时任何能连到端口的客户端都可调用，框架会记录一条安全告警。

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| method | str | 是 | HTTP 方法，如 "GET" / "POST" |
| path | str | 是 | 路径，需以 / 开头，如 "/my_plugin/data" |
| handler | Callable | 是 | 处理函数，签名因 webui 实现而异（通常收 request 对象，返回响应体） |
| need_auth | bool | 否 | 是否要求鉴权，默认 True |

```python
def on_load(ctx):
    global lumen
    lumen = ctx
    lumen.web.registerApi("GET", "/my_plugin/stats", get_stats, need_auth=True)


def get_stats(request):
    return {"online": len(lumen.mc.online_players), "greet_count": conf["stats"]["greet_count"]}
```

### 自定义页面

`lumen.web.registerPage(title, relative_path, tab=False, icon="")` 注册一个 WebUI 页面，`relative_path` 指向子插件目录内的 HTML 文件。页面 URL 形如 `/plugin-views/<插件目录名>/<relative_path>`。

- `tab=False`（默认）：页面出现在移动端「其它」面板与桌面侧栏；
- `tab=True`：页面在移动端注册为底栏 tab（位于「其它」之前，底栏可横向滚动，激活的 tab 自动滚动居中）；`icon` 为 tab 上的纯文本图标（emoji 或字符，如 `"📊"`，缺省用默认图标）。
- 桌面端侧栏无论 `tab` 取值都会展示该页面。

```python
def on_load(ctx):
    global lumen
    lumen = ctx
    lumen.web.registerPage("我的插件面板", "panel.html")          # 进「其它」面板
    lumen.web.registerPage("数据中心", "stats.html", True, "📊")  # 注册为底栏 tab
```

如果 WebUI 尚未初始化（加载顺序问题），注册请求会被暂存，WebUI 就绪后自动补注册，子插件无需关心时序。

## 国际化

`lumen.i18n` 提供国际化能力。框架内置 en / zh_CN / zh_TW 三种语言包，按 Endstone 服务器语言自动选择。子插件可注册自己的命名空间翻译，实现多语言文案。

| 成员 | 说明 |
|------|------|
| lumen.i18n.language | 当前语言代码（已规范化，如 "zh_CN"） |
| lumen.i18n.lang | 当前语言代码（别名，兼容子插件习惯写法） |
| lumen.i18n.register_namespace(ns, translations) | 注册命名空间翻译，translations 形如 {语言代码: {键: 文本}} |
| lumen.i18n.unregister_namespace(ns) | 卸载命名空间（卸载子插件时框架自动调用） |
| lumen.i18n.tn(ns, key, **kwargs) | 翻译命名空间下的键，支持 {placeholder} 占位符 |
| lumen.i18n.t(key, **kwargs) | 翻译主语言包的键 |
| lumen.i18n.available_languages() | 返回 {语言代码: 显示名} 字典 |

翻译查找顺序为：当前语言 -> en -> 命名空间下第一个可用语言 -> 键本身，因此子插件只提供一种语言也能正常工作。

```python
NS = "my_plugin"
TRANSLATIONS = {
    "zh_CN": {
        "welcome": "欢迎 {name} 进入服务器",
        "greet": "你好呀，{name}",
    },
    "en": {
        "welcome": "Welcome {name} to the server",
        "greet": "Hello, {name}",
    },
}


def on_load(ctx):
    global lumen
    lumen = ctx
    lumen.i18n.register_namespace(NS, TRANSLATIONS)
    lumen.logger.info(f"当前语言：{lumen.i18n.language}")


def on_player_join(player_name):
    # 用命名空间翻译，自动按当前语言取文案
    msg = lumen.i18n.tn(NS, "welcome", name=player_name)
    lumen.mc.broadcast(msg)


def on_group_message(pack, reply):
    nickname = pack.get("sender", {}).get("nickname", "朋友")
    reply(lumen.i18n.tn(NS, "greet", name=nickname))
```

`t()` 用于翻译框架主语言包的键（如错误提示），子插件一般用 `tn()` 翻译自己的文案。占位符采用 Python `str.format` 语法，缺失占位符或类型不符时回退为原文，不会抛异常。

## 正则引擎联动

LumenBridge 内置正则触发引擎，通过 `rules.json` 规则库对群消息与游戏事件做正则匹配并执行动作。子插件可以用 `lumen.register_regex_action(action_type, handler)` 注册自定义动作类型，随后在 `rules.json` 里用 `callPluginCommand` 动作调用它。

### handler 签名

注册的 handler 在规则匹配命中、执行到 `callPluginCommand` 动作时被调用，签名如下：

| 参数 | 类型 | 说明 |
|------|------|------|
| params | list[str] | 规则 params 中逗号分隔、去掉命令名后的参数列表，变量（$nickname 等）已替换 |
| pack | dict | 触发本次匹配的 OneBot 事件包 |
| context | dict | 变量上下文，含 $1/$2... 正则捕获组、$result 等，handler 返回值会写回此上下文 |

handler 的返回值决定变量写入：返回 `dict` 时其键值合并进上下文（如返回 `{"xbox": "Steve"}` 后 `$xbox` 可用）；返回字符串或其他非 None 值时写入 `$result`；返回 None 不写变量。

### 完整示例

```python
lumen = None


def on_load(ctx):
    global lumen
    lumen = ctx
    # 注册名为 "greet" 的自定义动作
    lumen.register_regex_action("greet", action_greet)
    lumen.logger.info("正则动作 greet 已注册")


def action_greet(params, pack, context):
    """规则中用 {"type":"callPluginCommand","params":"greet,$nickname"} 调用。
    返回的字符串会写入 $result，可被后续 replyText 动作用 $result 引用。"""
    who = params[0] if params else "陌生人"
    return f"来自子插件的问候：{who}！"
```

对应的 `rules.json` 规则示例：

```json
{
    "id": "rule_greet",
    "name": "问候",
    "enabled": true,
    "triggerType": "message",
    "pattern": "^你好",
    "flags": "i",
    "conditions": [],
    "actions": [
        {"type": "callPluginCommand", "params": "greet,$nickname"},
        {"type": "replyText", "params": "$result"}
    ],
    "block": true
}
```

当群里发送「你好」时，规则匹配命中，先调用 `greet` 动作（传入 `["$nickname"]` 作为 params），返回的字符串写入 `$result`，随后 `replyText` 动作把 `$result` 回复到群里。子插件卸载时框架会自动注销其注册的动作类型，热重载不会让旧动作残留。

## 命令注册

`lumen.register_command` 让子插件能在游戏内注册自己的命令。Endstone 0.11 的 Python API 不支持运行期注册新命令（BDS 命令表在插件加载时冻结，Command 的 name/aliases setter 注册后为 no-op），因此 LumenBridge 采用「启动面板 + 运行期绑定」两段式注册：

- 子插件首次调用 `register_command` 时，框架把命令名、描述、别名、usage 写入 `plugins/lumenbridge/command_palette.json` 启动面板，并在运行期绑定 handler。面板里的命令会在下次服务器启动时由 LumenBridge 并入类级 `commands` 声明，此时命令才真正对玩家可见。首次注册会告警提示「重启服务器后生效」。
- 如果命令已在面板声明（服务器启动时已并入），则本次启动即可用，直接绑定 handler。
- 子插件卸载时仅解除 handler 绑定（面板声明保留，重启后可被再次绑定），命令名本身不会被撤销。

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| name | str | 是 | 命令名，仅允许小写字母、数字、下划线、连字符 |
| handler | Callable | 是 | 处理函数，签名 handler(sender, args) -> bool，在主线程执行 |
| description | str | 否 | 命令描述，显示在 /help |
| aliases | list[str] | 否 | 命令别名列表（仅面板记录，重启后随命令一并声明） |
| usages | list[str] | 否 | 用法列表，须以 /\<name\> 开头，参数为 <x>/[x]/(a\|b) 形式。非法项会被剔除，全非法时回退默认 /\<name\> [args: message] |

handler 的 `sender` 是 Endstone 命令发送者（Player 或 ConsoleCommandSender），`args` 是去掉命令名后的参数列表。返回 True 表示执行成功。框架会捕获 handler 抛出的异常并向发送者回显错误，不会让异常崩溃服务器。

```python
lumen = None


def on_load(ctx):
    global lumen
    lumen = ctx
    lumen.register_command(
        "mycmd",
        on_mycmd,
        description="我的子插件命令",
        aliases=["mc"],
        usages=["/mycmd [args: message]", "/mycmd <player: target> <msg: message>"],
    )
    lumen.logger.info("命令 /mycmd 已注册")


def on_mycmd(sender, args):
    # sender 是 Player 或 ConsoleCommandSender
    name = getattr(sender, "name", "控制台")
    if not args:
        sender.send_message("用法：/mycmd <内容>")
        return False
    text = " ".join(args)
    sender.send_message(f"{name} 说了：{text}")
    return True
```

注意命令名跨子插件全局查重，已被其他子插件或 `/lumen` 主命令占用的名字会被拒绝（返回 False）。由于两段式机制，新增命令后通常需要重启一次服务器才能让玩家看到该命令。

## 主线程调度

`lumen.run_on_main(func, delay=1)` 把一个函数调度到游戏主线程执行。这是子插件开发中最关键的线程安全工具。

### 为什么需要主线程调度

OneBot 事件回调运行在适配器的 asyncio 线程上，而 Endstone 绝大部分 API（修改世界、操作玩家、执行命令等）只能在游戏主线程调用——从其他线程直接调用会引发线程安全问题甚至崩溃。因此凡是要在消息回调里操作游戏状态，都必须把实际操作包装进 `run_on_main`。

`lumen.mc.runcmd` / `runcmdEx` / `broadcast` / `online_players` 内部已经自行切主线程，无需再包一层；但直接访问 `lumen.server` 的属性或调用 Endstone 原生 API 时，必须自己用 `run_on_main` 包裹。

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| func | Callable[[], None] | 是 | 无参无返回的函数（闭包捕获所需变量） |
| delay | int | 否 | 延迟刻数，默认 1 |

```python
def on_group_message(pack, reply):
    raw = pack.get("raw_message", "").strip()
    if raw == "白天":
        # 切到主线程执行游戏命令
        lumen.run_on_main(lambda: lumen.server.dispatch_command(
            lumen.server.command_sender, "time set day"
        ))
        reply("已切换为白天")
    elif raw == "我的坐标":
        # 切到主线程读取玩家并操作
        def show_pos():
            xbox = lumen.get_xbox_by_pack(pack)
            if not xbox:
                reply("你还未绑定白名单")
                return
            player = lumen.get_player(xbox)
            if player is None:
                reply("你当前不在线")
                return
            loc = player.location
            reply(f"你的坐标：{loc.x:.0f}, {loc.y:.0f}, {loc.z:.0f}")
        lumen.run_on_main(show_pos)
```

注意 `runcmd` / `runcmdEx` / `online_players` 会阻塞调用线程等待主线程完成任务完成（内部用 `threading.Event`），因此绝不能在游戏主线程里调用它们，否则会死锁。

## Endstone 全量 API 直达

LumenBridge 不对 Endstone API 做任何裁剪，子插件可以通过下列成员直达全部 Endstone 能力，随 Endstone 版本升级自动获得新 API。

| 成员 | 类型 | 说明 |
|------|------|------|
| lumen.endstone | module | endstone 顶级模块，等价于 `import endstone` |
| lumen.plugin | LumenBridgePlugin | LumenBridge 插件实例，拥有 endstone.plugin.Plugin 全部能力 |
| lumen.server | Server | Endstone Server 对象 |
| lumen.scheduler | 调度器代理 | Endstone 任务调度器，run_task* 返回的 task 会被追踪，卸载时自动 cancel |
| lumen.import_module(name) | method | 导入任意 endstone 子模块，等价 importlib.import_module |
| lumen.get_player(name_or_uuid) | method | 按名称或 UUID 获取在线玩家对象，失败返回 None |

```python
def on_load(ctx):
    global lumen
    lumen = ctx

    # 直达 endstone 模块
    endstone = lumen.endstone
    lumen.logger.info(f"Endstone 版本：{endstone.__version__ if hasattr(endstone, '__version__') else '?'}")

    # 导入子模块
    color = lumen.import_module("endstone.util.color_format")
    # server 对象读取只读属性（注意线程）
    server = lumen.server


def on_group_message(pack, reply):
    if pack.get("raw_message", "").strip() != "版本":
        return

    def show_version():
        s = lumen.server
        reply(f"游戏版本：{s.minecraft_version}\n协议：{s.protocol_version}\nTPS：{s.average_tps:.2f}")
    lumen.run_on_main(show_version)
```

`lumen.scheduler` 是带任务追踪的代理：子插件经它注册的 `run_task` / `run_task_later` / `run_task_timer` 等返回的 task 会被记入上下文，子插件卸载时框架统一 cancel，避免热重载后旧定时任务残留重复执行。对外签名与用法和原生调度器完全一致，子插件无感知。

```python
def on_load(ctx):
    global lumen
    lumen = ctx
    # 周期任务：每 5 分钟（6000 刻）广播一次在线人数
    scheduler = lumen.scheduler
    scheduler.run_task_timer(lumen.plugin, broadcast_online, delay=200, period=6000)


def broadcast_online():
    lumen.mc.broadcast(f"当前在线 {len(lumen.mc.online_players)} 人")
```

## 白名单辅助

LumenBridge 维护 QQ 与 XboxID 的白名单绑定，且区分双域存储：个人号域绑定存于 `whitelist.json`（qid 为 QQ 号），官方机器人域绑定存于 `whitelist_official.json`（qid 为 openid）。子插件查白名单时必须先判断事件包所属域，否则会跨域误报「未绑定」。

| 方法 | 参数 | 返回 | 说明 |
|------|------|------|------|
| lumen.domain_of(pack) | pack: dict | str | 返回 "official"（官 bot，user_id 为 openid）或 "qq"（个人号，user_id 为 QQ 号） |
| lumen.get_xbox_by_pack(pack) | pack: dict | str / None | 按事件包发送者查绑定 XboxID，自动按域路由 |
| lumen.get_xbox_by_qq(qq, domain) | qq: int/str, domain: str | str / None | 按 QQ 号或 openid 查绑定 XboxID。domain 默认 "qq"，官 bot 用 "official" |

```python
def on_group_message(pack, reply):
    raw = pack.get("raw_message", "").strip()
    if raw == "我的白名单":
        # 自动按事件包所属域查绑定
        xbox = lumen.get_xbox_by_pack(pack)
        if xbox:
            domain = lumen.domain_of(pack)
            reply(f"你绑定的游戏 ID：{xbox}（域：{domain}）")
        else:
            reply("你还未绑定白名单，请发送 绑定白名单<你的游戏ID>")

    elif raw.startswith("查绑定 "):
        # 按 QQ 号查（个人号域）
        qq = raw.split(" ", 1)[1].strip()
        xbox = lumen.get_xbox_by_qq(qq, "qq")
        reply(f"QQ {qq} 绑定的 ID：{xbox or '未绑定'}")
```

双域白名单查询的重要性在于：官方机器人事件包的 `user_id` 是 openid 而非 QQ 号，如果直接拿 openid 去个人号域（`whitelist.json`）查，永远查不到绑定。`get_xbox_by_pack` 内部已按 `domain_of` 自动路由，是查白名单的推荐入口；只有在脱离事件包上下文（如定时任务、命令处理）时才需手动指定 domain 调用 `get_xbox_by_qq`。

## 第三方依赖声明

子插件可以使用第三方 pip 包。在 `lumen.json` 的 `dependencies` 字段声明后，LumenBridge 会在加载时自动检测并安装缺失依赖，无需用户手动 pip install。

### dependencies 字段

声明格式为 pip 包规格字符串列表，支持版本约束：

```json
{
    "dependencies": ["pillow>=10.0.0", "requests", "openai>=1.0,<2.0"]
}
```

### 自动安装流程

子插件加载时，框架按以下流程处理依赖：

1. 刷新 importlib 查找缓存，确保 site-packages 在 sys.path 中。
2. 逐项用 `importlib.metadata` 与 `find_spec` 检测依赖是否已安装；带版本约束时校验已装版本是否满足约束。
3. 缺失或不满足的依赖，通过 `pip install --user` 安装到 `plugins/.local`（与 LumenBridge 自身一致），并加 `--break-system-packages` 兼容 PEP 668。
4. 安装前执行 `pip install --dry-run --report` 预检，识别是否会升级或覆盖 Endstone / LumenBridge 核心依赖，有冲突则拒绝安装。
5. 安装完成再次验证可导入性，把环境问题转为可读错误。

### 包名与 import 名映射

许多 pip 包的发行名与 Python import 名不一致，框架内置了常见映射表用于检测：

| pip 包名 | import 名 |
|----------|-----------|
| pillow | PIL |
| beautifulsoup4 | bs4 |
| pyyaml | yaml |
| python-dotenv | dotenv |
| opencv-python | cv2 |
| scikit-learn | sklearn |
| pyjwt | jwt |
| protobuf | google.protobuf |

未在表中的包，框架还会通过 `importlib.metadata.packages_distributions()` 索引与 site-packages 目录磁盘扫描兜底发现，覆盖未登记的 import 名。子插件代码里直接 `import` 即可，不必关心映射细节。

### 核心包保护

以下包被列为受保护依赖，禁止升级或卸载，避免破坏 LumenBridge 与 Endstone 运行环境：

- endstone / endstone-lumenbridge
- websockets / pip / setuptools / wheel

`/lumen pip uninstall` 对受保护包会直接拒绝。

### pip 管理命令

LumenBridge 提供游戏内命令管理 pip 包，也可在 WebUI 操作：

| 命令 | 说明 |
|------|------|
| /lumen pip install \<包名\> | 安装包，支持版本约束 |
| /lumen pip list | 列出已安装包 |
| /lumen pip uninstall \<包名\> | 卸载包（受保护包拒绝） |

安装与卸载都是同步阻塞操作，会短暂占用调用线程；WebUI 在后台线程执行，游戏内命令则需等待完成。

## 部署与发布

### 部署目录

子插件统一部署在服务器的 `plugins/lumenbridge/subplugins/` 目录下，每个子插件一个子目录。LumenBridge 启动时会扫描该目录，按 `priority`（pre / main / post）分段加载。

### 安装方式

- **手动放置**：把子插件目录（含 `main.py` 与 `lumen.json`）放入 `subplugins/`，执行 `/lumen reload` 即可加载。
- **WebUI 上传 ZIP**：在 LumenBridge WebUI 的子插件页面上传 ZIP 包。ZIP 根目录可直接含 `main.py`，也可包一层文件夹。框架会解压、校验清单、版本判断（同名插件需更高版本才覆盖升级），升级时自动备份用户数据文件（.json/.db/.txt 等数据后缀）并在替换后回填。ZIP 炸弹防护会限制条目数、单文件大小与总解压体积。
- **插件市场安装**：访问市场 https://market.mxcraft.vip/ ，在线安装到指定服务器。

### 发布到市场

把子插件发布到市场供他人安装，需使用 GitHub 账号登录市场。账号注册满一年可直接发布；未满一年需经审核后上架。发布时提供子插件 ZIP 包与清单信息，市场会展示版本、作者、描述等元数据。

### 热重载

开发期频繁迭代时，修改 `main.py` 或 `lumen.json` 后无需重启服务器：

- 游戏内执行 `/lumen reload` 热重载全部子插件。
- WebUI 子插件页面提供单个子插件的重载、启用、禁用、卸载操作。

热重载会先卸载旧实例（清理其全部资源），再以新代码加载。重载期间持锁，避免并发 install / uninstall 观察到中间态。

### 清理机制

子插件卸载时，框架会自动清理它注册的全部资源，确保无残留：

- 事件总线监听器（`lumen.on` / `lumen.once` 注册的全部 handler）
- `mc.listen` 经内部总线注册的回调与原生 Endstone 监听器（置 inactive 软注销）
- 正则引擎自定义动作（`register_regex_action` 注册的类型）
- 服务器命令 handler 绑定（面板声明保留，仅解除绑定）
- `lumen.scheduler` 注册的全部定时任务（统一 cancel）
- WebUI 注册的 API、自定义页面、配置表单 schema
- i18n 命名空间翻译
- sys.modules 中的子插件模块及其子模块（释放对象引用）

这套清理机制让热重载可以安全反复执行，不会因旧 handler 残留导致同一事件被重复触发或内存泄漏。

## 完整示例

下面是一个综合性的子插件，演示监听群消息、读写存储、注册命令、注册正则动作与使用 i18n 的完整流程。把它放到 `plugins/lumenbridge/subplugins/welcome_plus/` 即可运行。

**lumen.json**

```json
{
    "name": "welcome_plus",
    "version": "1.0.0",
    "author": "lumenbridge",
    "desc": "综合示例：进服欢迎、关键词统计、自定义命令与正则动作",
    "load": true,
    "priority": "main",
    "min_v": "1.0.0",
    "dependencies": []
}
```

**main.py**

```python
"""welcome_plus —— 综合示例子插件

演示能力：
1. 监听群消息并做关键词统计（读写 storage）
2. 监听玩家进服，向群里发欢迎消息（QClient + msgbuilder + i18n）
3. 注册游戏内命令 /welcome_stats 查看统计
4. 注册正则引擎自定义动作 query_online，可在 rules.json 中调用
"""

lumen = None
conf = {}

DEFAULT_CONFIG = {
    "keyword": "签到",
    "stats": {"checkin_count": 0, "last_player": ""},
}

NS = "welcome_plus"
TRANSLATIONS = {
    "zh_CN": {
        "join": "{name} 进服啦，欢迎！",
        "checkin": "{name} 签到成功，你是第 {count} 位签到者",
        "stats": "累计签到 {count} 次，上次签到者：{last}",
    },
    "en": {
        "join": "{name} joined the server, welcome!",
        "checkin": "{name} checked in, you are the #{count} check-in",
        "stats": "Total check-ins: {count}, last: {last}",
    },
}


def on_load(ctx):
    global lumen, conf
    lumen = ctx

    # 1. 读写私有存储
    conf = lumen.storage.read("config.json", DEFAULT_CONFIG)
    for k, v in DEFAULT_CONFIG.items():
        conf.setdefault(k, v)
    lumen.storage.write("config.json", conf)

    # 2. 注册 i18n 命名空间
    lumen.i18n.register_namespace(NS, TRANSLATIONS)

    # 3. 监听群消息
    lumen.on("message.group.normal", on_group_message)

    # 4. 监听玩家进服
    lumen.mc.listen("onJoin", on_player_join)

    # 5. 注册游戏内命令
    lumen.register_command(
        "welcome_stats",
        on_stats_command,
        description="查看签到统计",
        aliases=["wstats"],
    )

    # 6. 注册正则引擎自定义动作
    lumen.register_regex_action("query_online", action_query_online)

    lumen.logger.info("welcome_plus 已加载")


def on_unload(ctx):
    ctx.logger.info("welcome_plus 已卸载")


# ---------------------------------------------------------------------------
# 群消息处理
# ---------------------------------------------------------------------------

def on_group_message(pack, reply):
    if pack.get("group_id") != lumen.env.get("main_group"):
        return
    raw = pack.get("raw_message", "").strip()

    if raw == conf["keyword"]:
        nickname = pack.get("sender", {}).get("nickname", "朋友")
        conf["stats"]["checkin_count"] += 1
        conf["stats"]["last_player"] = nickname
        lumen.storage.write("config.json", conf)
        reply(lumen.i18n.tn(NS, "checkin", name=nickname,
                            count=conf["stats"]["checkin_count"]), True)

    elif raw == "在线人数":
        result = lumen.mc.runcmdEx("list")
        reply(result["output"] or "查询失败", True)


# ---------------------------------------------------------------------------
# 玩家进服
# ---------------------------------------------------------------------------

def on_player_join(player_name):
    msg = lumen.i18n.tn(NS, "join", name=player_name)
    lumen.mc.broadcast(msg)
    lumen.QClient.send_group_msg(
        lumen.env.get("main_group"),
        [lumen.msgbuilder.text(msg)],
    )


# ---------------------------------------------------------------------------
# 命令处理（主线程执行）
# ---------------------------------------------------------------------------

def on_stats_command(sender, args):
    stats = conf.get("stats", {})
    text = lumen.i18n.tn(NS, "stats",
                         count=stats.get("checkin_count", 0),
                         last=stats.get("last_player", "无"))
    sender.send_message(text)
    return True


# ---------------------------------------------------------------------------
# 正则引擎自定义动作
# ---------------------------------------------------------------------------

def action_query_online(params, pack, context):
    """规则中用 {"type":"callPluginCommand","params":"query_online"} 调用。
    返回 dict 时其键合并进上下文，$online 可被后续动作引用。"""
    players = lumen.mc.online_players
    return {"online": str(len(players)), "players": ", ".join(players) if players else "无"}
```

对应的 `rules.json` 规则，让群里发送「查在线」时调用上面的 `query_online` 动作并回复结果：

```json
{
    "id": "rule_query_online",
    "name": "查在线人数",
    "enabled": true,
    "triggerType": "message",
    "pattern": "^查在线$",
    "flags": "i",
    "conditions": [],
    "actions": [
        {"type": "callPluginCommand", "params": "query_online"},
        {"type": "replyText", "params": "当前在线 $online 人：$players"}
    ],
    "block": true
}
```

这个示例覆盖了子插件开发的核心模式：用 `storage` 持久化数据、用 `i18n` 做多语言文案、用 `on` 监听群消息、用 `mc.listen` 监听游戏事件、用 `QClient` 与 `msgbuilder` 发消息、用 `runcmdEx` 执行命令取输出、用 `register_command` 注册游戏命令、用 `register_regex_action` 联动正则引擎。实际开发时按需取用即可。

