# QQ 官方机器人适配器（开发向）

本文面向二次开发与协议排障，介绍 QQ 官方机器人适配器的实现：网关会话、鉴权、事件翻译、被动回复窗口与发送链路。使用向文档见 [连接与适配器](../guide/adapters.md)。

## 定位与代码结构

对接 QQ 开放平台机器人 API（`api.bot.qq.com`），WebSocket 网关模式，**零外部依赖**：REST 走 `urllib`（`asyncio.to_thread`），WS 走内嵌 websockets。

```
onebot/qqofficial_adapter.py   适配器本体：网关会话、鉴权、REST 通道、生命周期、OneBot 兼容接口
onebot/qqofficial/
    constants.py               协议常量：OP 码、Intents、被动窗口、重试矩阵
    credentials.py             access_token 凭据池（过期自动刷新、并发去重）
    translate.py               官方事件 → OneBot v11 事件翻译
    sender.py                  发送队列与重试（markdown 降级、错误码分类）
    utils.py                   工具：HTTP 错误、payload 提取、markdown 工具
```

官方文档对照：<https://bot.q.qq.com/wiki/develop/api-v2/>

## 网关会话

标准 QQ 机器人 WS 网关协议（OP 码）：

| OP | 含义 | 处理 |
|----|------|------|
| 10 Hello | 连接欢迎 | 取心跳间隔 |
| 2 Identify | 鉴权登录 | 携带 token + Intents |
| 6 Resume | 断线恢复 | 携带 session_id + seq |
| 1 / 11 | 心跳 / ACK | 保活 |
| 0 Dispatch | 事件派发 | 交 `EventTranslator` 翻译 |
| 7 Reconnect | 服务端要求重连 | 重连（尽量 Resume） |
| 9 Invalid Session | 会话无效 | 重置会话状态后全新 Identify |

WS 关闭码语义：`4004` 鉴权失败（重取 token）；`4006` / `4009` / `9001` / `9005` 会话需重置（全新 Identify）。

重连策略：指数退避（2s 起，上限 60s）；两次网关连接尝试之间强制最小间隔（`connect_interval`，默认 60 秒，防止触发官方频控）。

## 鉴权

- 通过 `POST /app/getAppAccessToken` 获取短期 `access_token`，凭据池管理过期刷新；
- API 域名：正式 `api.bot.qq.com`，沙箱 `sandbox.api.bot.qq.com`（卡片 `sandbox: true`）。

## 事件订阅（Intents）

默认订阅（`DEFAULT_INTENTS`）：

| 位 | Intent | 覆盖 |
|----|--------|------|
| 1<<25 | GROUP_AND_C2C_EVENT | 群聊 + 单聊消息 |
| 1<<24 | GROUP_MEMBER_EVENT | 群成员增减 |
| 1<<30 | PUBLIC_GUILD_MESSAGES | 公域频道 @ 消息 |
| 1<<12 | DIRECT_MESSAGE | 频道私信 |
| 1<<0 | GUILDS | 频道/子频道变更 |
| 1<<26 | INTERACTION | 互动回调（按钮等） |

> `1<<24` 在官方文档存在双版本（总表 1<<24，部分 autogen 页 1<<25），适配器**同时订阅**两者；若机器人无 `1<<24` 权限导致网关拒连，连续 2 次 Identify 失败后自动摘除该位降级重连（自愈）。

其余特权域（频道成员 1<<1、私域消息 1<<9、表情表态 1<<10、审核 1<<27、论坛 1<<28、语音房 1<<29）默认不订阅（无权限会拒连），按需通过连接卡片 `extra_intents` 叠加。

## 双域与事件翻译

官方事件全部翻译为 OneBot v11 格式后经 `bus.emit("onebot.pack", ...)` 派发，下游（dispatcher、子插件、白名单等）与个人号域**共用同一套处理链**：

- 事件包携带 `domain="official"`（频道为 `"guild"`），个人号域为 `"qq"`；
- 群标识为 `group_openid`（字符串，非数字），`user_id` 为 openid；
- 群消息 → `message.group.normal`；单聊 → `message.private.friend`；
- 成员进出 → `notice.group_increase/decrease`；机器人被移出群 → `notice.group_decrease`（kick_me）；
- 入群申请 → `request.group.add`；
- **无 OneBot 语义的官方事件原样转发**为 `notice.<事件名小写>`（如 `notice.interaction_create`、`notice.forum_thread_create`），`raw` 字段为官方原始载荷——子插件可 `lumen.on("notice.interaction_create", handler)` 监听按钮回调。

## 被动回复窗口（核心限制）

官方平台的回复配额（各留时间余量防边界失效）：

| 场景 | 窗口 | 每条消息可回复 |
|------|------|----------------|
| 群聊 | 5 分钟（实际用 4.5 分钟） | 5 次 |
| 单聊 C2C | 60 分钟（实际用 55 分钟） | 4 次 |

实现机制：

- **被动凭据池**：每个目标保留最近 8 条收到的 `msg_id`，回复时取最新可用凭据；
- **入群 event_id**：机器人入群事件的 `event_id` 在 30 分钟内可作被动回复凭据（不消耗主动额度）；
- 凭据耗尽或过期（业务码 `40034005`）后转走**主动消息**通道。

## 主动消息与补发栈

- 主动消息被拒（业务码 `22009`：额度用尽/用户关闭接收）时进入**补发栈**：每目标最多缓存 5 条，后续收到该目标任何消息（可被动回复）时借道补发，每次最多 3 条（过多挤占正常回复额度）；
- `event_id` 无效（`40034025`）按发送失败处理。

## 发送链路

`MessageSender` 维护独立发送队列，出站消息构造规则：

| 消息形态 | 载体 |
|----------|------|
| 纯文本 | `msg_type=0`（content） |
| 含 @ 或 markdown 段 | `msg_type=2` markdown 载体（@ 转官方文本标记） |
| 图片/视频/语音 | 先调官方 media 接口拿 file_info，再发富媒体消息 |

容错与降级：

- markdown 载体被拒（机器人未开通 markdown 能力）→ 自动降级纯文本重发（@ 不渲染，还原可读文本）；
- 发送失败重试 3 次：文本间隔 1s，富媒体间隔 3s；
- 合并转发：官方无 forward 接口，降级为**逐条发送模拟**（节点上限 3，额度宝贵）。

富媒体限制：官方 media 接口仅支持 image/video/file 三类（语音映射 file）；本地文件 base64 上传上限 8MB（官方限制约 10MB 留余量）。

## 适配器特有 API

| 方法 | 说明 |
|------|------|
| `get_group_restrict_chat_setting` | 查群禁聊/开关状态 |
| `get_group_bot_state` | 查机器人在群内的状态 |
| `get_group_join_request_list` | 拉取入群申请列表 |
| `set_group_add_request(flag, approve)` | 审批入群申请（对应 `request.group.add` 的 `flag`） |
| `delete_msg(...)` | 官方域撤回（受平台限制） |
| `remember_group / forget_group` | 运行期维护已知群列表 |
| `broadcast_groups` | 全部已知群 openid 列表 |

查询类方法（`get_login_info`、`get_group_list`、`get_group_info`）以本地缓存/凭据合成 OneBot 兼容响应，不走 OneBot 协议端。

## 连接卡片配置

```json
{
    "id": "official-main",
    "name": "QQ 官方机器人",
    "type": "qqofficial",
    "enabled": true,
    "app_id": "102xxxxxx",
    "app_secret": "xxxx",
    "sandbox": false,
    "bot_qq": 0,
    "main_group": ["ABCDEF...openid"],
    "connect_interval": 60000,
    "extra_intents": 0,
    "suppress_connection_log": true
}
```

`main_group` 填群 openid（宽松解析，支持数组/逗号分隔字符串）。`bot_qq` 用于展示（官方 API 不回传机器人 QQ 号）。

## 排障要点

- **网关拒连**：多半是 Intents 无权限，看日志是否触发 `1<<24` 自动降级；`extra_intents` 里的位无权限同样拒连；
- **收不到群消息**：确认机器人已在群内、群消息开关开启（`get_group_bot_state` 可查）；
- **回复失败 22009**：被动凭据过期后走了主动消息，等用户再发一条消息即可借道补发；
- **连接间隔**：官方频控限制连接频率，`connect_interval` 不要调小。
