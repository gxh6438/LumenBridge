# 正则触发引擎

正则触发引擎让你**不写代码**就能配置自动化：群消息或服务器事件命中正则表达式后，按顺序执行一组动作（回复、执行命令、禁言、撤回……）。规则库存放在 `plugins/lumenbridge/data/rules.json`，通过 WebUI「正则」页可视化管理。

## 规则结构

```json
{
    "id": "rule_query_server",
    "name": "查服",
    "enabled": true,
    "triggerType": "message",
    "pattern": "^查服$",
    "flags": "i",
    "eventType": "",
    "conditions": [],
    "actions": [
        {"type": "executeCommand", "params": "list"},
        {"type": "replyText", "params": "$result"}
    ],
    "block": true
}
```

| 字段 | 说明 |
|------|------|
| `id` | 规则唯一标识（建议 `rule_` 前缀） |
| `name` | 规则名（日志与调试显示） |
| `enabled` | 是否启用 |
| `triggerType` | `message` 群消息触发 / `event` 事件触发 |
| `pattern` | 正则表达式；消息规则空 pattern 会匹配任意消息，**显式跳过**；事件规则空 pattern 表示匹配该事件的全部发生 |
| `flags` | JS 风格标志字符串：`i` 忽略大小写 / `m` 多行 / `s` 点号匹配换行 |
| `eventType` | `triggerType=event` 时的事件名（见下表） |
| `conditions` | 附加条件（见下文） |
| `actions` | 动作列表，按顺序执行（见下文） |
| `block` | 命中后是否停止匹配后续规则 |

## 触发源

### message — 群消息

监听所有已绑定群的普通消息（受 `only_on_main` 配置限制，见下文），对消息文本执行 `pattern.search()`。捕获组按 `$0`（整体）、`$1`、`$2`… 注入变量。

### event — 事件

| 事件名 | 来源 | pattern 匹配对象 |
|--------|------|------------------|
| `group.member_join` | QQ 群成员进群 | `user_id` |
| `group.member_leave` | QQ 群成员退群 | `user_id` |
| `server.player_join` | 玩家进服 | 玩家名 |
| `server.player_left` | 玩家退服 | 玩家名 |
| `server.player_chat` | 玩家聊天 | 消息内容 |

## 触发范围 only_on_main

主配置 `regex_engine.only_on_main`（默认 `true`）：

- `true`：仅**主群**（适配器 `main_group` 列表的第一个群）触发规则
- `false`：所有已绑定群都触发

内置命令 `/get openid`（见下文）不受此限制，任何绑定群都可用。

## 条件 conditions

全部条件都满足才执行动作。`field` / `operator` / `value` 三元组：

| field | 说明 |
|-------|------|
| `userRole` | 发送者角色：`owner` / `admin` / `member`，LumenBridge 管理员额外视为 `admin`（兼容旧配置的 `sparkadmin`） |
| `userId` | 发送者 QQ 号/openid/玩家名 |
| `groupId` | 群号 |
| 其他字段 | 直接取事件包同名字段（字符串比较） |

| operator | 说明 |
|----------|------|
| `==` / `!=` | 相等 / 不等（字符串比较） |
| `includes` | 包含子串 |
| `matches` | 正则匹配（忽略大小写，同样受 ReDoS 防护） |

## 动作 actions

| 动作 | params | 说明 |
|------|--------|------|
| `replyText` | 文本模板 | 回复文本，支持变量替换 |
| `replyAtText` | 文本模板 | 先真实 @ 事件主体再发文本（进群欢迎默认动作），模板支持变量替换 |
| `replyImage` | 图片 URL | 发送图片，URL 支持变量替换（如接 `$result` 取命令输出中的链接） |
| `executeCommand` | 命令 | 在游戏主线程执行 BDS 命令（无需 `/` 前缀，自动剥除），命令支持变量替换，输出捕获并翻译后写入 `$result` 变量 |
| `callPluginCommand` | `命令名,参数1,参数2…` | 调用注册到引擎的自定义动作；参数逐段支持变量替换；返回 dict 时合并进变量，返回其他值写入 `$result` |
| `deleteMessage` | 无 | 撤回触发消息（需协议端支持） |
| `muteUser` | 秒数 | 禁言触发者，秒数支持变量替换（如 `$1` 取捕获组），解析失败默认 600 秒 |

单条动作失败只记日志，不中断后续动作；单条规则异常不影响其余规则。

### 变量替换

文本与命令参数中的 `$变量` 会替换，未知变量原样保留：

**事件包变量**（来自触发消息/事件本身）：

| 变量 | 值 |
|------|----|
| `$userId` | 发送者 QQ 号/openid/玩家名 |
| `$groupId` | 群号 |
| `$at` | 消息中第一个被 @ 的人（无则退回发送者） |
| `$nickname` | 发送者昵称 |
| `$card` | 发送者群名片，未设置时回退昵称（官方域恒回退昵称） |
| `$role` | 发送者群身份：`owner` / `admin` / `member`（官方域为 `member`） |
| `$rawMessage` | 触发消息的原始全文 |
| `$messageId` | 触发消息 ID |
| `$selfId` | 机器人自身 QQ 号/AppID |
| `$domain` | 消息所属域：`qq`（个人号）/ `official`（官方机器人）/ `guild`（频道） |

**上下文变量**（由正则匹配或前序动作产生）：

| 变量 | 值 |
|------|----|
| `$0` `$1` `$2`… | 正则捕获组（`$0` 为整体匹配） |
| `$result` | 最近一个 `executeCommand` / `callPluginCommand` 的输出 |
| `$xbox` 等 | 自定义动作返回 dict 时合并写入的任意变量 |

## 内置动作与命令

- **`getXboxID`**（内置 callPluginCommand 动作）：按发送者（或 `$at` 指定者）查询白名单绑定，把 Xbox 玩家名写入 `$xbox` 变量。默认规则「查白名单」即由此实现：

  ```
  动作1：callPluginCommand → getXboxID,$at
  动作2：replyText → 查询结果：$xbox
  ```

- **`/get openid`**（内置群命令）：查询群 ID / 成员 openid，便于抄录进适配器配置。无参数返回本群与发送者 ID；@ 成员返回该成员 openid（个人号域为 QQ 号）；官方域不支持 QQ 号查询（平台不提供映射）。

## 子插件注册自定义动作

子插件可向引擎注册新动作类型，供规则以 `callPluginCommand` 调用：

```python
lumen.regex_module.register_action("weather", my_handler)
```

详见[子插件 Context API](../development/context-api.md#注册正则动作)。

## 性能与安全防护

正则引擎运行在**消息热路径**（服务器主线程），风险 pattern 的灾难性回溯会直接冻结服务器，因此内置多层防护：

- pattern 长度上限 512 字符，参与匹配的文本上限 2000 字符（超出截断）
- **ReDoS 启发式**：有界大重复（`a{100000}`）与嵌套量词（`(a+)+`）直接判为风险，拒编译（缓存后零重复扫描，日志只打一次）
- pattern 编译缓存，聊天匹配零重复编译
- **动作去抖**：同一规则对同一事件指纹（消息按 message_id，事件按类型+用户+群+时间）5 秒窗口内只执行一次，防协议端重复上报；指纹缓存上限 512 条
- `command_timeout`（默认 5 秒）限制 `executeCommand` 等待时长
- 主线程触发的命令同步执行（调度回主线程再阻塞等待会自死锁）
- 命令输出自动剥除 `§` 颜色码；`Translatable` 输出翻译失败时稳定兜底，`stop` 命令不误报失败

## 默认规则

首次启动写入四条示例规则，可直接修改或删除：

| 规则 | 触发 | 效果 |
|------|------|------|
| 查白名单 | `^查白名单` | 查询 @ 者的 Xbox 绑定 |
| 查服 | `^查服$` | 执行 `list` 并回显在线人数 |
| 管理员执行命令 | `^执行(.+)` + 管理员条件 | 管理员在群里远程执行服务器命令 |
| 入群欢迎 | `group.member_join` | @ 新成员发欢迎语 |
