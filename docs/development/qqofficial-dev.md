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
- 频道撤回 → `group_recall`（私域私信 → `friend_recall`）；
- **无 OneBot 语义的官方事件原样转发**为 `notice.<事件名小写>`（如 `notice.interaction_create`、`notice.forum_thread_create`），`raw` 字段为官方原始载荷——子插件可 `lumen.on("notice.interaction_create", handler)` 监听按钮回调。

### 接收侧翻译能力

入站消息的官方载荷元素与 OneBot 消息段映射：

| 官方载荷 | OneBot 段 | 说明 |
|----------|-----------|------|
| `content` 文本 | `text` | `@机器人` 前缀剥离并置 `at_bot` 标记，`@成员` 就地转 `at` 段 |
| `mentions` 数组 | `at` 段增强 | 补昵称、补齐 content 已剥离的 @ |
| 引用消息（`reply` 载荷） | `reply` 段 | 附 `id` / `text` / `sender` / `user_id`（go-cqhttp 惯例字段，子插件可直接读） |
| 聊天记录（`forward` 载荷） | `forward` 段 | 前置段形式 |
| 图片 / 视频附件 | `image` / `video` 段 | 附件自带 `url`；缺失时经 media 接口补取带时效下载地址 |
| 语音附件 | `record` 段 | 附 `wav_url`（WAV 直链，SILK 原始链多数播放器不可播）与 ASR 识别文本——识别文本同时追加一条 `[语音] 文本` text 段，游戏聊天侧唯一可读形态 |

## 被动回复窗口（核心限制）

官方平台的回复配额（各留时间余量防边界失效）：

| 场景 | 窗口 | 每条消息可回复 |
|------|------|----------------|
| 群聊 | 5 分钟（实际用 4.5 分钟） | 5 次 |
| 单聊 C2C | 60 分钟（实际用 55 分钟） | 4 次 |

实现机制：

- **被动凭据池**：每个目标保留最近 8 条收到的 `msg_id`，回复时取最新可用凭据；
- **event_id 凭据**：机器人入群事件、按钮/快捷菜单点击事件（`INTERACTION_CREATE`）的 `event_id` 在 30 分钟内可作被动回复凭据（不消耗主动额度）；
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
| 按钮键盘段 | `msg_type=2` markdown 载体 + `keyboard` 字段（见下节） |
| 图片/视频/语音 | 先调官方 media 接口拿 file_info，再发富媒体消息 |

容错与降级：

- markdown 载体被拒（机器人未开通 markdown 能力）→ 自动降级纯文本重发（@ 不渲染，还原可读文本）；
- 发送失败重试 3 次：文本间隔 1s，富媒体间隔 3s；
- 合并转发：官方无 forward 接口，降级为**逐条发送模拟**（节点上限 3，额度宝贵）。

富媒体限制：官方 media 接口仅支持 image/video/file 三类（语音映射 file）；本地文件 base64 上传上限 8MB（官方限制约 10MB 留余量）。

## 按钮键盘互动消息

官方域支持**消息按钮**（官方「消息按钮」协议）：markdown 消息底部挂载键盘，用户点击回调按钮触发 `INTERACTION_CREATE` 互动事件。消息段构造见 [消息收发 · 按钮键盘](messaging.md#按钮键盘官方域扩展)，本节讲收发全链路。

### 发送侧

- 键盘仅官方域渲染：`msg_type=2`（markdown 载体）+ `keyboard` 字段（自定义按钮 `keyboard.content` 或后台模板 `keyboard.id`）；
- 官方必填字段（`visited_label` / `data` / `permission` / `unsupport_tips`）由 `button()` 缺省自动补全，避免 `630001 param invalid`；
- markdown 能力未开通降级纯文本时键盘一并丢弃（按钮依赖 markdown 载体）；
- 单条消息键盘与富媒体互斥（首个命中生效）。

### 点击回调（入站）

点击回调按钮触发 `notice.interaction_create`（1<<26 互动事件），`raw` 为官方原始载荷：

```python
def on_interaction(event):
    raw = event["raw"]
    # 群聊点击：raw.group_openid / raw.group_member_openid / raw.data.resolved.button_data
    # 单聊点击：raw.user_openid / raw.data.resolved.button_data
    if raw["data"]["resolved"]["button_data"] == "ans_creeper":
        event.ctx.send_group_msg(raw["group_openid"], "回答正确，验证通过！")

lumen.on("notice.interaction_create", on_interaction)
```

官方载荷关键字段（type=11 消息按钮）：

| 字段 | 说明 |
|------|------|
| `id` | 互动事件 ID（被动回复凭据 + 回应接口参数） |
| `type` | 互动类型：11 消息按钮 / 12 快捷菜单 / 13 消息反馈 / 14 清空会话 / 18 用户授权 / 19 群授权 等 |
| `scene` / `chat_type` | c2c=单聊 / group=群聊 / guild=频道 |
| `group_openid` / `group_member_openid` | 群聊场景的群与点击者 |
| `user_openid` | 单聊场景的点击者 |
| `data.resolved.button_data` | 按钮的 `data` 字段原样回传（答题答案判定靠它） |
| `data.resolved.button_id` | 按钮的 `id` 字段（发送时设置才有） |

### 回应机制（防客户端转圈）

官方要求：**type=11/12 的点击必须在事件有效期内调 `PUT /interactions/{interaction_id}` 回应，否则客户端一直 loading 到超时；同一 interaction_id 仅可回应一次**。适配器处理：

- 收到 type=11/12 点击 → 调度 2.5 秒兜底自动回应 `code=0`（子插件无感，客户端永不转圈）；
- 子插件可在宽限期内抢先自定义回应（抢占后兜底自动跳过）：

```python
lumen.QClient.call_action(
    "respond_interaction",
    {"interaction_id": raw["id"], "code": 5},   # 5=仅管理员操作：非管理员点「跳过验证」给失败提示
    callback=lambda r: None,
)
```

  `code`：0 成功 / 1 操作失败 / 2 操作频繁 / 3 重复操作 / 4 没有权限 / 5 仅管理员操作。

### 被动回复凭据

点击事件 `id` 会注册为该目标的 `event_id` 被动凭据（与入群 event_id 同机制）：点击后的回复消息（如「验证通过」）自动借道被动通道发送，**不消耗主动额度**。

## API 翻译参考（官方域全量动作）

官方域没有 OneBot 协议端，**所有动作由适配器逐个翻译**为开放平台 REST 接口。以下是适配器已实现的全部动作——OneBot 语义调用、参数语义、回调返回与平台限制。子插件按 OneBot 语义编程，路由层按 openid 自动送达官方适配器（见 [消息收发](messaging.md)的路由规则）。

### 消息发送

**`send_group_msg(group_openid, message)` / `send_private_msg(openid, message)`**
→ `POST /v2/groups/{openid}/messages` / `POST /v2/users/{openid}/messages`

- 消息经 `extract_payload` 归一为 `(文本, 富媒体)`：`text` 段拼接、`at` 段转官方 `<qqbot-at-user id="..." />` 文本链（`@全体成员` 丢弃）、首个 `image` / `video` / `record` 段作富媒体（`reply` 段忽略）；文本截断 2000 字符；
- 载体自动选择：纯文本 `msg_type=0`；含 @ 走 `msg_type=2` markdown（实测唯一可渲染真实 @ 的组合）；富媒体 `msg_type=7`；
- 富媒体先 `POST /v2/{groups|users}/{openid}/files` 上传（URL 直传或本地 base64，上限 8MB）取 `file_info` 再发送，失败降级纯文本；
- 凭据三级：被动 `msg_id`+`msg_seq`（不耗主动额度）→ `event_id`（入群 / 按钮点击事件）→ 主动消息（可能被拒 22009 入补发栈）。

### 互动事件回应

**`respond_interaction(interaction_id, code, callback)`**（`call_action("respond_interaction", ...)`）
→ `PUT /interactions/{interaction_id}`，body `{"code": int}`

- 官方要求按钮（type=11）/快捷菜单（type=12）点击必须回应，否则客户端 loading 到超时；适配器已内置 2.5 秒兜底自动回应（`code=0`），子插件仅在有自定义提示需求时抢先调用（如非管理员点管理员按钮回 `code=5`）；
- 同一 `interaction_id` 仅可回应一次（重复回调 `{"ok": False, "error": ...}`）；`interaction_id` 取互动事件 `raw.id`（适配器自动剥离 `INTERACTION_CREATE:` 前缀）；
- `code`：0 成功 / 1 操作失败 / 2 操作频繁 / 3 重复操作 / 4 没有权限 / 5 仅管理员操作。

### 消息撤回

**`delete_msg(message_id, callback)`**
→ `DELETE /v2/groups/{g}/messages/{id}` / `DELETE /v2/users/{u}/messages/{id}`

- 群聊：机器人可撤自己的消息，管理员身份可撤群员消息；单聊：仅可撤机器人发给该用户自己的消息；
- **2 分钟时限**，超时官方拒绝；
- `message_id` 须为本适配器记录过的 id（发送回执 id 或收到的消息 id）——适配器维护 `消息 id → (kind, target)` 映射用于反查端点，未知 id 回调 `{"ok": False, "error": ...}`；
- 回调返回 `{"ok": True}` 或 `{"ok": False, "error": "撤回失败：..."}`。

### 禁言

**`set_group_ban(group_openid, member_openid, duration, callback)`**
→ `POST /v2/groups/{g}/restrict_chat_setting`，body `{"members": [{"op": "add"|"del", "member_openid", "mute_expire_at"}]}`

- `duration=0` 解除禁言（op=del），`>0` 禁言至 `now+duration`；
- 平台限制：最长 **30 天**（2592000 秒，超限自动截断并告警）、仅能禁言**普通成员**、需群管理员；
- 参数非法（空 openid / 负数）直接回调 `None`；成功回调 `{"ok": True}`。

**`set_group_ban_batch(group_openid, member_openids, duration, callback)`**（官方扩展）
同上端点，`members` 数组一次提交：

- 单次最多 **10 人**（超限截断并告警）；`duration` 必须 > 0；
- 成功回调 `{"ok": True, "count": n}`。

**`get_group_restrict_chat_setting(group_openid, callback)`**（官方扩展）
→ `GET /v2/groups/{g}/restrict_chat_setting`，返回全员禁言规则 + 当前禁言成员列表。

### 入群审批（群管理）

**`set_group_add_request(flag, sub_type, approve, reason, callback)`**
→ `POST /v2/groups/{g}/approval_join_request/{member_openid}`，body `{"op": "approve"|"decline", "join_request_id", "reject_reason"?}`

- `flag` 即 `request.group.add` 事件下发的 `join_request_id`（适配器维护 flag → 群/成员映射）；
- 需群管理员；拒绝理由截断 255 字符；
- 回调 `{"ok": True}` / `{"ok": False, "error": ...}`。

**`get_group_join_request_list(group_openid, cursor="", limit=20, callback)`**（官方扩展）
→ `GET /v2/groups/{g}/join_request_list`（分页参数走 GET + JSON body），需群管理员。

### 查询类

| 方法 | 端点 / 实现 | 说明 |
|------|-------------|------|
| `get_group_info(group_openid, callback)` | `GET /v2/groups/{g}/info`（白名单接口） | 字段映射：`group_openid→group_id`、`group_name`、`group_member_num→member_count`、`group_finger_memo→group_memo`；无权限（常态）回退本地兜底 |
| `get_group_list(callback)` | 本地合成 | 官方无群列表接口；配置 `main_group` + 动态发现（流入事件学习）的 openid 汇总 |
| `get_login_info(callback)` | 本地合成 | `{user_id: bot_qq, nickname: READY 昵称, app_id}`——官方 API 不回传机器人 QQ 号 |
| `get_group_bot_state(group_openid, callback)` | `GET /v2/groups/{g}/bot_state`（官方扩展） | 机器人群内角色 / 主动消息开关 / 收消息设置 |

本地兜底语义：配置了 `main_group` 时，列表内的 openid 视为已互通群；未配置时对所有已发现的群生效。

### 不支持的动作

- **`call_api(pack)`**：官方域无 OneBot action 协议可透传，直接回调 `None`（个人号域的通用透传入口在官方域不可用）；
- **`send_pack(pack)`**：OneBot 原始包与官方协议不兼容，忽略；
- **其余未实现方法**（`__getattr__` 统一降级）：记 warning「不支持的官方域动作」并以 `None` 回调（末参为回调时）——不会抛 `AttributeError`，但功能不生效。典型不支持：踢人（`set_group_kick`）、设管（`set_group_admin`）、全体禁言（`set_group_whole_ban`）、好友点赞、群文件等。

### 调用示例

```python
def on_load(lumen):
    # 禁言 10 分钟（openid 域自动路由到官方适配器）
    lumen.QClient.set_group_ban("ABCDEF...", "UVWXYZ...", 600)

    # 撤回自己 2 分钟内发的消息（需持有发送回执 id）
    lumen.QClient.delete_msg(sent_message_id, callback=lambda r: lumen.logger.info(r))

    # 审批入群申请
    lumen.QClient.set_group_add_request(flag, approve=True)
```

除上表外的**任何** OneBot 动作在官方域都会降级——子插件如需判断当前域，用事件包的 `domain` 字段（`"qq"` / `"official"` / `"guild"`）或在动作回调里检查 `None`。

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

## 相关文档

- **下一步**：[Web 扩展](web-extensions.md)
- [消息收发](messaging.md)——双域差异总览
