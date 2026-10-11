# Context API 参考

每个子插件加载时获得独立的 `LumenContext` 实例，惯例命名 `lumen`，作为 `on_load(lumen)` 的参数传入。它是子插件与 LumenBridge/Endstone 交互的**全部入口**。

```python
def on_load(lumen):
    ...
```

## 属性总览

| 属性 | 类型 | 说明 |
|------|------|------|
| `lumen.pluginName` | str | 子插件名（lumen.json 的 `name`） |
| `lumen.VERSION` | str | LumenBridge 版本号 |
| `lumen.logger` | PrefixedLogger | 带插件名前缀的日志（info/warning/error/debug） |
| `lumen.plugin` | Plugin | LumenBridge 插件实例（Endstone Plugin 全部能力） |
| `lumen.server` | Server | Endstone Server 对象 |
| `lumen.endstone` | module | endstone 顶级模块透传 |
| `lumen.bus` 事件 | - | 经 `lumen.on / once / off / emit` 使用 |
| `lumen.QClient` | AdapterHub | OneBot 适配器门面（QQ 收发） |
| `lumen.msgbuilder` | module | 消息段构造器 |
| `lumen.packbuilder` | module | OneBot 动作包构造器 |
| `lumen.mc` | MCBridge | Minecraft 接口桥 |
| `lumen.storage` | Storage | 子插件私有 JSON 存储 |
| `lumen.web` | WebBridge | WebUI 扩展接口 |
| `lumen.env` | EnvPool | 全局共享变量池 |
| `lumen.i18n` | I18n | 主程序 i18n 实例 |
| `lumen.language` | str | 当前生效语言代码（`zh_CN` / `zh_TW` / `en`） |
| `lumen.debug` | bool | 是否调试模式 |
| `lumen.scheduler` | Scheduler 代理 | Endstone 任务调度器（自动记录任务） |

## 事件总线

```python
def on_load(lumen):
    def handler(pack, reply):
        ...

    lumen.on("message.group.normal", handler)   # 持久监听
    lumen.once("bot.online", lambda ad: ...)    # 一次性
    lumen.off("message.group.normal", handler)  # 手动解绑（通常不需要）
```

- 事件名与 handler 签名见[事件系统](events.md)
- **自动清理**：`on/once` 注册的监听器登记在上下文中，子插件卸载/热重载时自动解绑，无需手工清理
- 已清理（卸载）的上下文再注册会抛 `RuntimeError`——持有旧 context 引用时注意
- `lumen.emit("自定义事件", data)` 可以在子插件间广播自己的事件

## QQ 消息收发（QClient）

`lumen.QClient` 是 AdapterHub 门面，常用方法：

```python
# 发群消息（文本或消息段列表）
lumen.QClient.send_group_msg(group_id, "Hello")
lumen.QClient.send_group_msg(group_id, [
    lumen.msgbuilder.at(123456),
    lumen.msgbuilder.text(" 你好"),
])

# 发私聊消息
lumen.QClient.send_private_msg(user_id, "Hello")

# 通用 OneBot action（带回调）
lumen.call_action("get_group_member_info", {"group_id": gid, "user_id": uid},
                  callback=lambda data: lumen.logger.info(data))
```

消息段构造与全部 action 见[消息与动作](messaging.md)。

## Minecraft 桥（mc）

```python
# 监听游戏事件（别名 onJoin/onLeft/onChat/onDeath，或任意 Endstone 事件类名）
lumen.mc.listen("onJoin", lambda player: lumen.logger.info(f"{player.name} 进服"))
lumen.mc.listen("PlayerInteractEvent", lambda event: ...)   # 反射任意 Endstone 事件

# 执行服务器命令（主线程执行，阻塞等结果，最多 5 秒）
ok = lumen.mc.runcmd("say hello")            # 返回 dispatch 成功与否
result = lumen.mc.runcmdEx("list", timeout=5)  # {"success": bool, "output": "..."}

# 全服广播
lumen.mc.broadcast("§a服务器公告")

# 在线玩家名列表
players = lumen.mc.online_players
```

注意事项：

- `runcmd / runcmdEx / online_players` 会**阻塞等待主线程**，**不要在主线程回调里调用**（如 Endstone 事件回调、命令 handler）——主线程上「调度回主线程再等待」会自死锁。主线程场景直接用 `lumen.server` 原生 API
- 任意 Endstone 事件监听：同一事件类只创建一个原生 listener，回调挂可变分发表；上下文清理时清空分发表即停止分发
- 别名事件（onJoin 等）经内部总线转发，同样自动清理

## 注册游戏命令

```python
def on_load(lumen):
    def handler(sender, args):
        sender.send_message("点歌: " + " ".join(args))
        return True

    lumen.register_command(
        "diange",                     # 命令名统一按小写处理 → /diange
        handler,
        description="点歌命令",
        usages=["/diange [args: message]"],
    )
```

工作机制（**两段式注册**）：

- BDS 命令表在插件加载时冻结，运行期无法注册新命令，LumenBridge 用「启动面板 + 运行期绑定」解决
- `data/command_palette.json` 是启动命令面板：插件模块导入期把面板并入 LumenBridge 类级 `commands` 预声明；面板里**已有**的命令本次启动即可用
- 运行期注册**面板中没有**的新命令名：写入面板并提示「下次重启生效」，同时立即绑定 handler（命令本身要重启后才出现在命令表）
- 命令名仅允许小写字母数字下划线连字符；非法名、非可调用 handler、被其他子插件占用的名字返回 `False`
- usage 语法：必须以 `/名字` 开头，其余只能是参数 token（`<参数>` / `[参数]` / `(a|b)` 枚举），可带类型如 `[args: message]`；非法 usage 会被清洗剔除，全无效时回退默认 `/名字 [args: message]`
- 卸载时解除 handler 绑定，面板声明保留（重启后可重新绑定）

也可以经 `lumen.plugin.register_command(...)` 注册（兼容入口，加载期间自动归属到当前子插件上下文）。

## 注册正则动作

```python
def on_load(lumen):
    def weather_handler(params, pack, context):
        return {"weather": "晴 25℃"}   # 返回 dict → 合入规则变量；返回其他 → $result

    lumen.register_regex_action("weather", weather_handler)
```

之后在 WebUI 正则规则里用 `{"type": "callPluginCommand", "params": "weather,城市"}` 调用。详见[正则触发引擎](../guide/regex-engine.md)。卸载时自动注销注册的动作类型。

## 环境变量池（env）

跨子插件共享数据的线程安全键值池：

```python
lumen.env.set("sign_count", 42)
count = lumen.env.get("sign_count", 0)
```

- `set` 会广播 `env.update.<key>` 与 `env.update` 事件，其他子插件可监听
- `main_group` 有特殊语义：**事件回调中返回当前来源群**（线程本地），其他场景返回配置主群——兼容旧插件 `if gid == env.get("main_group")` 风格；广播全部主群用 `env.get("main_groups")`（列表）
- `admin_qq` 返回全部适配器管理员并集

## 私有存储（storage）

子插件目录下的 JSON 键值存储（`plugins/lumenbridge/plugins/<名字>/data/`），线程安全、原子写：

```python
cfg = lumen.storage.read("config.json", {"welcome": True})   # 读取，缺失写默认值
lumen.storage.write("config.json", {**cfg, "welcome": False})
path = lumen.storage.path("config.json")                      # 绝对路径
```

- 读损坏文件：先备份为 `.corrupt-<时间戳>` 再返回默认值（不会静默覆盖用户数据）
- 路径穿越防护：拒绝绝对路径与 `../` 逃逸

## 主线程调度

子插件回调大多运行在事件线程/HTTP 线程，触碰游戏世界必须回主线程：

```python
# 异步调度（不等待结果）
lumen.run_on_main(lambda: lumen.server.broadcast_message("hi"))

# 同步桥（阻塞拿返回值；主线程内自动同步执行）
online = lumen.call_on_main(lambda: [p.name for p in lumen.server.online_players],
                            timeout=5, default=[])
```

- `call_on_main` 是后台线程触碰 Endstone API 的推荐方式（自动处理死锁：检测到已在主线程则直接执行）
- 线程模型详见[架构总览](architecture.md#线程模型)

## 任务调度器（scheduler）

Endstone 调度器的**记录型代理**，用法与原生一致：

```python
lumen.scheduler.run_task(plugin_obj, callback)          # 同步任务
lumen.scheduler.run_task(plugin_obj, callback, delay=20)  # 延迟
lumen.scheduler.run_task(plugin_obj, callback, period=40) # 周期
```

经 `lumen.scheduler` 创建的任务会被登记，**卸载时统一 cancel**，防止热重载后旧定时任务重复执行。注意：框架更新不能经调度器任务执行（reload 清理调度器任务会悬垂心跳迭代）。

## Endstone 全 API 直达

```python
player = lumen.get_player("Steve")       # 在线玩家对象（Player 全 API）
lumen.server.dispatch_command(...)       # Server 全 API
import endstone                          # lumen.endstone / lumen.import_module("endstone.event")
```

- `lumen.server` 在后台线程直接触碰可能崩溃，配合 `call_on_main` 使用
- `lumen.plugin` 可注册原生事件监听器（`plugin.register_events`）等

## 白名单辅助

```python
domain = lumen.domain_of(pack)            # "official" 或 "qq"
xbox = lumen.get_xbox_by_pack(pack)       # 按事件包发送者查绑定（自动按域路由）
xbox = lumen.get_xbox_by_qq(123456)       # 按 QQ 号查
xbox = lumen.get_xbox_by_qq(openid, domain="official")  # 官方域按 openid 查
```

查绑定前务必用 `domain_of` 选域，跨域查询会误报「未绑定」。

## Web 扩展（web）

注册 WebUI 配置表单、REST API 与自定义页面：

```python
lumen.web.registerApi("GET", "/status", lambda: {"ok": True})
lumen.web.registerPage("我的页面", "index.html", tab=False, icon="star")
```

详见[Web 扩展开发](web-extensions.md)。

## 卸载语义（on_unload 与自动清理）

```python
def on_unload(lumen):
    # 释放非托管资源（数据库连接、外部线程等）
    ...
```

`on_unload` 之外，框架自动清理：

| 资源 | 清理方式 |
|------|----------|
| `on/once` 事件监听 | 全部解绑 |
| `register_regex_action` 注册的动作 | 全部注销 |
| `register_command` 绑定的 handler | 解除绑定（面板声明保留） |
| `lumen.scheduler` 创建的任务 | 全部 cancel |
| `lumen.web` 注册的 API/页面/配置表单 | 全部撤销 |
| `mc.listen` 的 Endstone 原生监听 | 分发表清空（listener 保留但 no-op） |

需要手工释放的只有：自建线程、数据库连接、文件句柄等外部资源——在 `on_unload` 里处理。
