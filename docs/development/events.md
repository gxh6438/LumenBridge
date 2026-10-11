# 事件系统

LumenBridge 内部用一条线程安全的**事件总线**（`EventBus`）串联所有消息流。子插件、功能模块都通过 `on/once/off/emit` 挂载与触发事件。

## 总线 API

| 方法 | 说明 |
|------|------|
| `on(event, handler)` | 注册持久监听器，返回 handler；同一 handler 重复注册自动去重（热重载安全） |
| `once(event, handler)` | 注册一次性监听器，触发一次后自动移除 |
| `off(event, handler)` | 移除监听器 |
| `emit(event, *args, **kwargs)` | 触发事件，逐个调用监听器；单个监听器异常被捕获记日志，不影响其他监听器 |
| `remove_all(event=None)` | 移除某事件（或全部）的所有监听器 |
| `listener_count(event)` | 查询监听器数量 |

子插件应通过 Context 使用（自动登记、卸载时自动解绑）：

```python
lumen.on("message.group.normal", handler)
lumen.once("bot.online", lambda adapter: ...)
```

## 事件目录

### OneBot 消息事件 `message.*`

监听 QQ 消息的主入口，事件名格式 `message.<message_type>.<sub_type>`：

| 事件名 | 触发 |
|--------|------|
| `message.group.normal` | 群普通消息（最常用） |
| `message.group.anonymous` | 群匿名消息 |
| `message.group.notice` | 群系统提示（如入群公告） |
| `message.private.friend` | 好友私聊 |
| `message.private.group` | 群临时会话 |

handler 签名：`handler(pack, reply)`。

```python
def on_load(lumen):
    def handler(pack, reply):
        if pack["raw_message"] == "ping":
            reply("pong", True)      # True = 引用并 @ 触发者

    lumen.on("message.group.normal", handler)
```

### 通知事件 `notice.*`

| 事件名 | 说明 |
|--------|------|
| `notice.group_increase` | 群成员增加（含机器人自身进群） |
| `notice.group_decrease` | 群成员减少（退群/踢出；白名单退群自动移除监听此事件） |
| `notice.group_admin` | 群管理员变动 |
| `notice.group_ban` | 群禁言 |
| `notice.group_recall` | 群消息撤回 |
| `notice.friend_recall` | 好友消息撤回 |
| `notice.group_upload` | 群文件上传 |
| `notice.notify` | 戳一戳等 honor/notify 事件（依协议端） |

handler 签名：`handler(pack)`（无 reply 参数）。

### 请求事件 `request.*`

| 事件名 | 说明 |
|--------|------|
| `request.friend` | 好友添加请求 |
| `request.group` | 群添加/邀请请求 |

handler 签名：`handler(pack)`。

### 元事件 `meta_event.*`

| 事件名 | 说明 |
|--------|------|
| `meta_event.heartbeat` | 协议端心跳 |
| `meta_event.lifecycle` | 生命周期（connect 等） |

### 连接事件

| 事件名 | 参数 | 说明 |
|--------|------|------|
| `bot.online` | adapter 实例 | 某适配器连接就绪（含 RESUMED 会话恢复） |
| `bot.offline` | adapter 实例 | 某适配器断开 |

### 底层事件 `onebot.pack`

所有 OneBot 原始事件包在**分层派发之前**先以单参 `(pack,)` 派发到 `onebot.pack`（dispatcher 自己也监听它）。仅在需要监听分层体系外的原始载荷时使用。

### 动作回执 `packid_<echo>`

`call_action(..., callback=...)` 之外的底层回执通道：回执到达后以 `emit(f"packid_{echo}", data)` 派发。一般用 callback 而非监听它。

## Minecraft 事件 `mc.*`

Endstone 服务器事件经主插件转发到总线（在主线程 emit）：

| 事件名 | 参数 | 说明 |
|--------|------|------|
| `mc.player_join` | player_name | 玩家进服 |
| `mc.player_left` | player_name | 玩家退服 |
| `mc.player_chat` | player_name, message | 玩家聊天 |
| `mc.player_death` | player_name, death_message | 玩家死亡 |

QQ 官方适配器同时把这类事件注册为正则引擎的 `server.player_*` 规则触发源（见[正则触发引擎](../guide/regex-engine.md#event-事件)）。

需要更多 Endstone 事件时用 `lumen.mc.listen`（反射任意事件类）或 `lumen.plugin.register_events`（原生监听），见[Context API · Minecraft 桥](context-api.md#minecraft-桥-mc)。

## 框架内部事件

| 事件名 | 参数 | 说明 |
|--------|------|------|
| `env.update.<key>` | value | `lumen.env.set` 时广播 |
| `env.update` | key, value | 同上的通用形式 |
| `config.update.core` | config body | WebUI 保存主配置后广播 |
| `config.update.<name>` | key, value | WebUI 保存子插件配置表单后广播 |

## 事件包（pack）字段

消息/通知事件包是 OneBot v11 标准字段 + LumenBridge 扩展：

| 字段 | 说明 |
|------|------|
| `post_type` | `message` / `notice` / `request` / `meta_event` |
| `message_type` / `sub_type` | 消息类型（group/private）与子类型 |
| `message_id` | 消息 ID（回复/撤回用） |
| `user_id` | 发送者 QQ 号（官方域为 openid） |
| `group_id` | 群号（官方域为 group_openid） |
| `message` | 消息段数组（OneBot v11 格式） |
| `raw_message` | CQ 码字符串（HTML 转义实体已解码） |
| `sender` | `{user_id, nickname, card, role, level...}` |
| `self_id` | 机器人自身 ID |
| `time` | 事件时间戳 |
| `_lumen_adapter_id` | **扩展**：来源适配器卡片 id |
| `domain` | **扩展**：`official` = QQ 官方机器人；`guild` = 官方频道；缺省 = 个人号域 |

> 官方域事件由翻译层统一转成 OneBot v11 形态（@ 提及转 at 段、官方原始载荷放在扩展事件名小写的 notice 里并附 `raw` 字段），下游无感。详见[QQ 官方机器人开发](qqofficial-dev.md)。

## reply 快捷回复

消息事件的第二参数 `reply(msg, quote=False)`：

- 自动路由到**来源适配器**（多适配器并行时绝不串线），适配器断线时发送挂起待重连补发
- `msg` 可为字符串或消息段数组（自动经 `format_message` 归一化）
- `quote=True` 时在最前面插入引用段（需要 `message_id`）

```python
reply("文本")                     # 纯文本
reply([at_segment(uid), " 你好"]) # 消息段
reply("引用回复", quote=True)     # 带引用
```

## 事件过滤与去重

- **群过滤**：部分框架模块（聊天同步、白名单、正则）按适配器群列表过滤事件；子插件监听器**不过滤**，自行判定 `pack["group_id"]` 是否需要处理（可用 `env.get("main_group")` / `lumen.env.get("main_groups")` 辅助）
- **notice/request 去重**：同一事件 5 秒窗口内重复到达只处理第一次（多链路重复上报/协议端重发防护）；message 事件不去重
- **`/get openid`、群绑定、添加管理员**等内置群命令不叠加群过滤，任何可收到的群都响应

## 线程模型

监听器运行在**适配器接收线程**（`mc.*` 事件在主线程 emit）。监听器里：

- 可以：发 QQ 消息（适配器线程安全）、读写 storage/env、`lumen.run_on_main` / `call_on_main` 调度
- 不可以：直接触碰玩家/世界对象（用 `call_on_main` 桥接）、执行阻塞过久（阻塞会拖慢同队列的其他事件）
