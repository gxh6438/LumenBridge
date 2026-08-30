# QQ 官方机器人子插件 API 参考

> 适用版本：LumenBridge 1.0.0（2026-08 事件全覆盖版）
> 本文是**官方 bot（qqofficial 适配器）子插件 API 的完整参考**：事件订阅 + 主动调用 + 消息段 + 额度机制，按「OneBot v11 标准有的」和「官方独有（OneBot 没有的）」分类整理。
> 入门教程与原理讲解见同目录《QQ官方机器人适配器与子插件开发指南.md》。

---

## 目录

- [1. 三十分钟速览（必读）](#1-三十分钟速览必读)
- [2. 子插件骨架](#2-子插件骨架)
- [3. LumenContext（lumen 对象）完整 API](#3-lumencontextlumen-对象完整-api)
- [4. 事件订阅 API](#4-事件订阅-api)
  - [4.1 消息事件（OneBot 标准）](#41-消息事件onebot-标准)
  - [4.2 通知事件（OneBot 标准）](#42-通知事件onebot-标准)
  - [4.3 请求事件（OneBot 标准）](#43-请求事件onebot-标准)
  - [4.4 元事件（OneBot 标准）](#44-元事件onebot-标准)
  - [4.5 官方扩展事件（OneBot 没有 ★官方独有）](#45-官方扩展事件onebot-没有官方独有)
  - [4.6 官方 bot 永远不会触发的事件（协议限制）](#46-官方-bot-永远不会触发的事件协议限制)
- [5. 主动调用 API（发消息 / 审批 / 撤回 / 查询）](#5-主动调用-api发消息--审批--撤回--查询)
- [6. 消息段支持矩阵（msgbuilder）](#6-消息段支持矩阵msgbuilder)
- [7. 发送额度与凭据机制](#7-发送额度与凭据机制)
- [8. 事件订阅位（Intents）总表](#8-事件订阅位intents总表)
- [9. 事件包字段速查](#9-事件包字段速查)
- [10. 完整示例](#10-完整示例)

---

## 1. 三十分钟速览（必读）

官方 bot 子插件开发只有 4 个核心概念：

| 概念 | 一句话 |
|---|---|
| **域（domain）** | 官方群聊/单聊事件 `domain="official"`，频道事件 `domain="guild"`，个人号 `"qq"`；用它过滤来源 |
| **openid** | 官方体系下群/用户都是字符串标识（`group_openid` / `member_openid` / `user_openid`），**不是 QQ 号**，统一按字符串处理 |
| **事件翻译** | 官方 46 种网关事件全部翻译为 OneBot v11 事件包；OneBot 有对应语义的走标准事件，没有的以 `notice.官方事件名小写` 扩展转发（`raw` 字段保留官方原始载荷） |
| **被动凭据** | 官方 bot 发消息受限：回复刚收到的消息用被动凭据（群 5 分钟 5 次 / 单聊 60 分钟 4 次），主动消息有月度额度且用户可关闭——**能用 `reply` 就用 `reply`** |

## 2. 子插件骨架

```
subplugins/my_plugin/
├── lumen.json     # 清单（缺失时自动生成模板）
└── main.py        # 入口（必须叫这个名）
```

`lumen.json` 字段：

```json
{
    "name": "my_plugin",
    "version": "1.0.0",
    "author": "you",
    "desc": "插件简介",
    "load": true,           // false = 跳过加载
    "priority": "main",     // pre / main / post 三段加载顺序
    "min_v": ""             // 最低 LumenBridge 版本要求（可选）
}
```

```python
# main.py —— 最小骨架
lumen = None

def on_load(ctx):                       # 必须
    global lumen
    lumen = ctx
    lumen.on("message.group.normal", on_msg)

def on_unload(ctx):                     # 可选（监听自动清理，只做额外收尾）
    ctx.logger.info("bye")

def on_msg(pack, reply):
    reply("hello")
```

---

## 3. LumenContext（lumen 对象）完整 API

### 3.1 事件

| API | 说明 |
|---|---|
| `lumen.on(event, cb)` | 订阅事件 |
| `lumen.once(event, cb)` | 订阅一次，触发后自动注销 |
| `lumen.off(event, cb)` | 注销 |
| `lumen.emit(event, *args)` | 主动发事件 |
| `lumen.register_regex_action(type, handler)` | 向正则引擎注册自定义动作 |
| `lumen.register_command(命令名, handler, description="", aliases=None, usages=None)` | 注册 Endstone 服务器命令。`handler(sender, args) -> bool`（主线程执行，args 不含命令名）；命令名非法或被占用返回 False。首次注册写入 `command_palette.json`，**重启服务器后**命令生效；卸载子插件仅解除 handler 绑定（面板声明保留）。`lumen.plugin.register_command(...)` 为等效兼容入口 |

### 3.2 消息发送

| API | 说明 |
|---|---|
| `lumen.QClient` | 适配器中枢（AdapterHub），双域自动路由：目标为 openid → 官方适配器；数字 QQ 号 → 个人号适配器 |
| `lumen.QClient.send_group_msg(群标识, 消息)` | 官方域群标识 = `group_openid` 字符串 |
| `lumen.QClient.send_private_msg(用户标识, 消息)` | 官方域 = `user_openid` 字符串 |
| `lumen.msgbuilder` | 消息段构建器（见第 6 节） |
| `lumen.packbuilder` | 数据包构建器 |
| `lumen.call_action(action, params, callback)` | 通用 OneBot action（官方适配器不支持的回调 `None`） |
| 事件回调自带 `reply(消息, quote=False)` | **首选发送方式**：自动路由回来源适配器 + 自动携带被动凭据（不耗主动额度）；`quote=True` 引用原消息 |

### 3.3 数据与环境

| API | 说明 |
|---|---|
| `lumen.storage.read(文件名, 默认值)` / `.write(...)` / `.path(...)` | 子插件私有 JSON 存储（原子写入、自动加锁、损坏自动备份） |
| `lumen.env.get(键)` / `.set(键, 值)` | 全局共享池；`main_group` 事件回调中返回当前来源群；`main_groups` / `admin_qq` |
| `lumen.logger.info/warning/error/debug(...)` | 带插件名前缀日志 |
| `lumen.debug` | 调试模式布尔值 |
| `lumen.i18n` | 多语言实例 |

### 3.4 Minecraft

| API | 说明 |
|---|---|
| `lumen.mc.listen(事件名, cb)` | 别名 `onJoin/onLeft/onChat/onDeath` 或任意 Endstone 事件类名 |
| `lumen.mc.runcmd(命令)` | 异步执行（不等输出） |
| `lumen.mc.runcmdEx(命令, timeout=5)` | 执行并捕获输出 `{"success", "output"}`；勿在主线程调用 |
| `lumen.mc.broadcast(消息)` | 全服广播 |
| `lumen.mc.online_players` | 在线玩家名列表快照 |

### 3.5 Web 与 Endstone 直达

| API | 说明 |
|---|---|
| `lumen.web.createConfig()` / `registerApi(method, path, handler, need_auth=True)` / `registerPage(标题, 路径)` | WebUI 配置表单 / REST API / 自定义页面 |
| `lumen.plugin` / `lumen.server` / `lumen.scheduler` / `lumen.endstone` / `lumen.import_module(name)` / `lumen.get_player(...)` / `lumen.run_on_main(fn, delay=1)` | Endstone 全能力直达 |

---

## 4. 事件订阅 API

### 4.1 消息事件（OneBot 标准）

回调签名：`cb(pack, reply)`。

| 订阅名 | 官方事件 | 订阅位 | 说明 |
|---|---|---|---|
| `message.group.normal` | `GROUP_MESSAGE_CREATE` | 1<<25 | 全量群消息（无需@机器人） |
| `message.group.normal` | `GROUP_AT_MESSAGE_CREATE` | 1<<25 | @机器人的群消息（共用处理） |
| `message.private.friend` | `C2C_MESSAGE_CREATE` | 1<<25 | 官方单聊 |
| `message.group.normal` | `AT_MESSAGE_CREATE` | 1<<30 | 公域频道@消息，`domain="guild"`，`group_id=channel_id` |
| `message.group.normal` | `MESSAGE_CREATE` | 1<<9（私域） | 私域频道全量消息（载荷同上） |
| `message.private.friend` | `DIRECT_MESSAGE_CREATE` | 1<<12 | 频道私信，`domain="guild"` |

> `message.private` 也可用（粗粒度订阅）；`sub_type` 官方域恒为 `normal` / `friend`（官方无匿名/临时会话概念）。

### 4.2 通知事件（OneBot 标准）

回调签名：`cb(pack)`。

| 订阅名 | 官方事件 | 订阅位 | 字段说明 |
|---|---|---|---|
| `notice.group_increase` | `GROUP_ADD_ROBOT` | 1<<25 | 机器人入群。`user_id=self_id`（AppID）、`operator_id=操作人 openid`；日志打印 group_openid 供抄录；`event_id` 自动入凭据池 |
| `notice.group_increase` | `GROUP_MEMBER_ADD` | 1<<24（默认双订） | 成员入群。`user_id=成员 openid`、`sub_type="approve"`（官方不区分入群方式，取规范默认值），附 `raw` 原文 |
| `notice.group_decrease` | `GROUP_DEL_ROBOT` | 1<<25 | 机器人被移出群。`sub_type="kick_me"` |
| `notice.group_decrease` | `GROUP_MEMBER_REMOVE` | 1<<24（默认双订） | 成员退群。`sub_type="leave"`，附 `raw` |
| `notice.friend_add` | `FRIEND_ADD` | 1<<25 | 官方单聊维度添加好友 |
| `notice.friend_del` | `FRIEND_DEL` | 1<<25 | 官方单聊维度删除好友（OneBot 无此标准事件，LumenBridge 扩展语义） |
| `notice.group_recall` | `PUBLIC_MESSAGE_DELETE` | 1<<30 | 频道消息撤回。`user_id=消息发送者`、`operator_id=撤回操作者`、`domain="guild"` |
| `notice.group_recall` | `MESSAGE_DELETE` | 1<<9（私域） | 私域频道消息撤回（同上） |
| `notice.friend_recall` | `DIRECT_MESSAGE_DELETE` | 1<<12 | 频道私信撤回。`user_id=好友` |
| `notice.group_increase` | `GUILD_MEMBER_ADD` | 1<<1（需权限） | 频道成员加入，`group_id=guild_id` |
| `notice.group_decrease` | `GUILD_MEMBER_REMOVE` | 1<<1（需权限） | 频道成员移除 |

> 官方 bot **没有**群消息主动撤回推送事件——QQ 群里撤回消息官方网关不会通知（上表撤回均为频道域）。

### 4.3 请求事件（OneBot 标准）

回调签名：`cb(pack)`。

| 订阅名 | 官方事件 | 订阅位 | 说明 |
|---|---|---|---|
| `request.group` | `GROUP_JOIN_REQUEST` | 1<<25 | 用户申请加群（**机器人须为群管理员才能收到**）。`flag=join_request_id`（审批回传用）、`user_id=申请者 openid`、`group_id=group_openid`、`comment="用户名 \| 邀请人 \| 验证问答"` 拼接 |

> OneBot 的 `request.friend`（加好友请求）官方协议不存在——官方只有事后 `FRIEND_ADD` 通知，无法审批。`request.group` 的 OneBot `sub_type=invite`（邀请机器人入群）与官方 `apply_source=invited`（邀请用户入群）语义不同，故官方域恒翻译为 `sub_type="add"`。

### 4.4 元事件（OneBot 标准）

| 订阅名 | 触发时机 |
|---|---|
| `meta_event.lifecycle` | 官方网关 READY 时合成 `lifecycle.connect`（与个人号行为一致，`sub_type="connect"`、`domain="official"`） |
| `meta_event.heartbeat` | 官方适配器不产生（仅个人号协议端推送） |

### 4.5 官方扩展事件（OneBot 没有 ★官方独有）

OneBot v11 协议**不存在**对应语义的官方事件，以 `notice.官方事件名小写` 转发，`raw` 字段保留官方原始载荷（**权威数据源，字段名与官方文档一致**），`official_event` 字段为大写原名。**只有官方适配器会产生，个人号永不触发同名事件，可放心订阅。**

| 订阅名 | 官方事件 | 订阅位 | 触发场景 |
|---|---|---|---|
| `notice.interaction_create` | `INTERACTION_CREATE` | 1<<26 | 用户点击机器人发的按钮/组件（群聊卡片、频道消息） |
| `notice.message_audit_pass` | `MESSAGE_AUDIT_PASS` | 1<<27 | 主动消息审核通过 |
| `notice.message_audit_reject` | `MESSAGE_AUDIT_REJECT` | 1<<27 | 主动消息审核被拒 |
| `notice.forum_thread_create/update/delete` | `FORUM_THREAD_*` | 1<<28 | 论坛主题创建/更新/删除（官方 2026-08 新名） |
| `notice.forum_post_create/delete` | `FORUM_POST_*` | 1<<28 | 论坛帖子（新名） |
| `notice.forum_reply_create/delete` | `FORUM_REPLY_*` | 1<<28 | 论坛回帖（新名） |
| `notice.forum_publish_audit_result` | `FORUM_PUBLISH_AUDIT_RESULT` | 1<<28 | 论坛发布审核结果 |
| `notice.open_forum_*` | `OPEN_FORUM_*` | 1<<28 | 论坛旧名（兼容保留） |
| `notice.audio_start/finish/on_mic/off_mic` | `AUDIO_*` | 1<<29 | 语音房开播/结束/上麦/下麦 |
| `notice.add_reaction` / `notice.message_reaction_add` | `ADD_REACTION` / `MESSAGE_REACTION_ADD` | 1<<10 | 表情表态添加（新旧名兼容） |
| `notice.delete_reaction` / `notice.message_reaction_remove` | `DELETE_REACTION` / `MESSAGE_REACTION_REMOVE` | 1<<10 | 表情表态删除 |
| `notice.guild_create/update/delete` | `GUILD_*` | 1<<0 | 机器人加入/离开/频道资料更新 |
| `notice.channel_create/update/delete` | `CHANNEL_*` | 1<<0 | 子频道创建/更新/删除 |
| `notice.guild_member_update` | `GUILD_MEMBER_UPDATE` | 1<<1 | 频道成员资料变更（ADD/REMOVE 有语义翻译，见 4.2） |
| `notice.group_msg_switch` | `GROUP_MSG_REJECT` / `GROUP_MSG_RECEIVE` | 1<<25 | 群管理员关闭/开启「接收机器人主动消息」。`sub_type="reject"/"receive"` |
| `notice.friend_msg_switch` | `C2C_MSG_REJECT` / `C2C_MSG_RECEIVE` | 1<<25 | 用户关闭/开启机器人主动推送。`sub_type="reject"/"receive"` |
| `notice.<任何新事件名小写>` | 未枚举事件 | — | **兜底机制**：官方未来新增事件自动转发，日志 DEBUG 提示 |

> `group_msg_switch` / `friend_msg_switch` 为自定义语义扩展（非官方名小写）：OneBot 无「主动消息开关」概念，强行映射 `group_ban` 会让下游误判为禁言。

### 4.6 官方 bot 永远不会触发的事件（协议限制）

以下 OneBot 标准事件官方协议**不存在对应能力**，子插件在官方域订阅它们永远收不到（个人号域正常）。写双域插件时不要依赖它们在官方域触发：

| 事件 | 原因 |
|---|---|
| `notice.notify.poke/lucky_king/honor` | 官方无戳一戳/红包/荣誉推送 |
| `notice.group_upload` | 官方群文件经消息 attachments 附件走，无独立事件 |
| `notice.group_ban` / `group_admin` | 官方无禁言/管理员变动推送 |
| `notice.group_recall`（**QQ 群**域） | 官方群撤回无推送（频道域有，见 4.2） |
| `request.friend` | 官方无加好友审批 |
| `message.group.anonymous` / `message.private.group` | 官方无匿名/临时会话 |
| `meta_event.heartbeat` / `enable` / `disable` | 官方网关用心跳保活但不推送该元事件 |

---

## 5. 主动调用 API（发消息 / 审批 / 撤回 / 查询）

`lumen.QClient`（AdapterHub）自动按目标路由：**openid 字符串 → 官方适配器，数字 → 个人号适配器**。以下均为官方适配器真实实现的能力：

### 5.1 发送消息

```python
lumen.QClient.send_group_msg(group_openid, 消息)     # 群聊（主动或借被动凭据）
lumen.QClient.send_private_msg(user_openid, 消息)    # 单聊
reply(消息, quote=False)                             # 消息回调内首选（被动凭据）
```

`消息` 支持纯字符串 / 消息段数组（见第 6 节）。

### 5.2 入群审批 ★官方独有能力

```python
# 处理 request.group 事件里的申请；机器人须为群管理员
lumen.QClient.set_group_add_request(
    flag,              # 事件包里的 flag（= join_request_id）
    sub_type="add",    # 兼容 OneBot 签名，官方域忽略
    approve=True,      # True=同意 / False=拒绝
    reason="",         # 拒绝理由（≤255字，官方字段 reject_reason）
)
```

内部调用官方 `POST /v2/groups/{group_openid}/approval_join_request/{member_openid}`。flag 与群/成员的映射由适配器在收到 `GROUP_JOIN_REQUEST` 时自动缓存（512 条，重启后失效——重启前的旧 flag 会告警"无对应记录"）。

### 5.3 撤回群消息

```python
lumen.QClient.delete_msg(message_id)
```

内部调用官方 `DELETE /v2/groups/{group_openid}/messages/{message_id}`。规则：

- **机器人自己发的群消息**：2 分钟时限内可撤回；
- **机器人是群管理员**：可撤普通群员消息（`message_id` 取自收到的消息事件包）；
- 私聊消息：官方无 C2C 撤回接口，告警跳过；
- `message_id` 归属自动追踪：适配器收到/发出的每条群消息都会记录 id → 群映射（1024 条），直接传事件包里的 `message_id` 即可。

### 5.4 查询类（本地兜底实现）

| API | 行为 |
|---|---|
| `get_login_info(cb)` | `{user_id: 配置的bot_qq, nickname: READY上报名, app_id}`（AppID 不是 QQ 号，昵称取自网关 READY） |
| `get_group_list(cb)` | 官方无群列表接口 → 返回配置的 group_openid 列表本地兜底 |
| `get_group_info(群标识, cb)` | 本地兜底：在配置群列表内（或未配置任何群=全部群生效）返回 `{group_id, group_name, member_count:0, max_member_count:0}`，否则 `None` |

### 5.5 不支持的方法与降级行为

官方协议没有的能力（禁言、踢人、取群成员列表、设置群名片、戳一戳、发送合并转发、好友操作等 OneBot action）统一**安全降级**，不会抛异常：

- `call_action(action, ...)` → 回调 `None`；
- 任意未实现的 `get_*` 方法 → 末参为回调时回调 `None`；
- 写操作（`set_*` / `delete_*` 等官方不支持的方法）→ 记 warning 日志后跳过。

调用方务必判空。

---

## 6. 消息段支持矩阵（msgbuilder）

官方域发消息时各消息段的行为：

| 构建函数 | 官方域行为 |
|---|---|
| `text(str)` | ✅ 纯文本，单条最长 2000 字符自动截断 |
| `image(url / 本地路径 / base64)` | ✅ 经官方 files 接口上传后发送；本地文件 ≤8MB（base64） |
| `video(...)` | ✅ 同上 |
| `record(...)` | ✅ 同上（官方 file_type=3） |
| `at(qq)` | ⚠️ 官方群聊仅支持 @ 机器人（`<@tiny_id>`），@ 普通成员不可达，文本原样输出 |
| `reply(message_id)` | ⚠️ 官方无引用回复接口，降级为普通文本（引用段被丢弃） |
| `face` / `poke` / `forward` / `json` / `xml` / ... | ❌ 官方无对应能力，忽略或降级 |

约束：**官方单条消息只支持 1 个富媒体段**（取第一个，多余忽略）；富媒体上传失败自动降级为纯文本发送。

---

## 7. 发送额度与凭据机制

| 机制 | 数值 |
|---|---|
| 被动回复窗口 | 群 5 分钟 / 单聊 60 分钟（内部留 0.5/5 分钟余量） |
| 每条消息可被动回复次数 | 群 5 次 / 单聊 4 次（2026-01-10 起官方调整，msg_seq 递增） |
| 被动凭据池 | 每目标缓存 8 条消息凭据（懒池策略：优先消耗未用过的最新凭据） |
| 入群 event_id 凭据 | 机器人入群事件自带，30 分钟内可作被动回复凭据、不耗主动额度（入群欢迎借此发送） |
| 主动消息 | 月度额度（腾讯侧控制）；用户/群管理员可关闭（错误码 22009） |
| 补发栈（AtoP） | 主动被拒暂存（每目标 5 条），下次被动回复成功后每次最多借额度补发 3 条 |
| 发送重试 | 超时/网络错误重试 3 次（文本 1s / 媒体 3s 间隔，重试递增 msg_seq）；22009 不重试进栈 |
| 队列限速 | 每条消息间隔 0.2s，队列满 100 条丢最旧并告警 |

---

## 8. 事件订阅位（Intents）总表

**默认订阅**（开箱即用，无需配置）：

| 位 | 含义 | 覆盖事件 |
|---|---|---|
| 1<<25 | 群聊 + C2C | 群消息、单聊、机器人进退群、好友增删、**入群申请**、主动消息开关 |
| 1<<24 | 群成员进退群 | GROUP_MEMBER_ADD / REMOVE（与 1<<25 双订防文档 CDN 双版本；**连续 Identify 失败 2 次自动摘除降级重连**，事件改由 1<<25 推送） |
| 1<<30 | 公域频道@ | AT_MESSAGE_CREATE |
| 1<<12 | 频道私信 | DIRECT_MESSAGE_CREATE / DELETE |
| 1<<0 | 频道变更 | GUILD_* / CHANNEL_* |
| 1<<26 | 互动事件 | INTERACTION_CREATE |

**按需订阅**（连接配置 `extra_intents`，多位按位或相加）：

| 位 | 十进制 | 含义 | 事件 |
|---|---|---|---|
| 1<<1 | 2 | 频道成员变更（需官方开通权限） | GUILD_MEMBER_* |
| 1<<9 | 512 | 私域频道消息（仅私域机器人） | MESSAGE_CREATE / DELETE |
| 1<<10 | 1024 | 表情表态 | *_REACTION / ADD_REACTION / DELETE_REACTION |
| 1<<27 | 134217728 | 消息审核 | MESSAGE_AUDIT_PASS / REJECT |
| 1<<28 | 268435456 | 论坛 | FORUM_*（OPEN_FORUM_*） |
| 1<<29 | 536870912 | 语音房 | AUDIO_* |

> ⚠️ 订阅**无权限**的位会导致网关拒连。排查方法：`extra_intents` 归零确认能连，再逐位添加。1<<24 例外——默认订阅且带自动降级，无需手动处理。

---

## 9. 事件包字段速查

标准事件包（消息类）：

```python
{
    "post_type": "message", "message_type": "group", "sub_type": "normal",
    "message_id": "官方msg_id字符串",
    "group_id": "group_openid",           # 频道域为 channel_id
    "user_id": "member_openid",
    "message": [{"type": "text", "data": {...}}, ...],
    "raw_message": "纯文本",
    "sender": {"nickname": ..., "user_id": ..., "card": ""},
    "anonymous": None, "font": 0,
    "time": 1786853582, "self_id": "AppID",
    "domain": "official",                 # ★ official=官方群聊单聊 / guild=频道 / qq=个人号
    "guild_id": "G1",                     # 仅频道域附带
    "_lumen_adapter_id": "适配器id",
}
```

扩展事件包：

```python
{
    "post_type": "notice",
    "notice_type": "interaction_create",  # 官方事件名小写
    "sub_type": "",
    "user_id": "尽力提取的openid",
    "group_id": "载荷里有群/频道标识时附带",
    "official_event": "INTERACTION_CREATE",  # ★ 官方原名（大写）
    "raw": {...官方原始载荷，权威数据源...},
    "time": ..., "self_id": ..., "domain": "official", "_lumen_adapter_id": ...,
}
```

---

## 10. 完整示例

覆盖消息回复、入群审批、撤回、成员进退群、扩展事件：

```python
"""官方 bot 子插件 API 全演示"""

lumen = None

def on_load(ctx):
    global lumen
    lumen = ctx

    lumen.on("message.group.normal", on_group_msg)      # 群消息（默认订阅）
    lumen.on("message.private.friend", on_private_msg)  # 单聊（默认订阅）
    lumen.on("request.group", on_join_request)          # 入群申请（默认订阅）
    lumen.on("notice.group_increase", on_member_join)   # 成员进群（默认订阅）
    lumen.on("notice.group_decrease", on_member_left)   # 成员退群（默认订阅）
    lumen.on("notice.interaction_create", on_button)    # 按钮回调（默认订阅）
    lumen.on("notice.forum_thread_create", on_forum)    # 论坛（需 extra_intents 1<<28）
    lumen.logger.info("官方 bot 全 API 演示已加载")


def on_group_msg(pack, reply):
    if pack.get("domain") not in ("official", "guild"):
        return                                  # 过滤：只处理官方 bot 来源
    text = pack.get("raw_message", "").strip()

    if text == "状态":
        result = lumen.mc.runcmdEx("list")
        reply(f"在线：{result['output']}", quote=True)   # reply 自动带被动凭据

    elif text == "撤回我":
        # 机器人是群管理员时可撤普通成员消息
        lumen.QClient.delete_msg(pack["message_id"])


def on_private_msg(pack, reply):
    reply("收到私聊：你发的 id 是 " + str(pack["message_id"]))


def on_join_request(pack):
    """入群申请审批：黑名单拒绝、白名单直接过"""
    flag = pack["flag"]
    user = pack["user_id"]
    if user in lumen.storage.read("blacklist.json", []):
        lumen.QClient.set_group_add_request(flag, approve=False, reason="黑名单")
    else:
        lumen.QClient.set_group_add_request(flag, approve=True)


def on_member_join(pack):
    gid = pack["group_id"]                      # group_openid
    who = pack["user_id"]                       # member_openid（机器人自己入群时=AppID）
    lumen.QClient.send_group_msg(gid, f"欢迎 {who}！")

def on_member_left(pack):
    lumen.logger.info(f"{pack['user_id']} 离开了 {pack['group_id']}")

def on_button(pack):
    raw = pack.get("raw", {})                   # 官方原始载荷（权威）
    lumen.logger.info(f"按钮回调 {pack.get('official_event')}: {raw}")

def on_forum(pack):
    lumen.logger.info(f"论坛新主题：{pack['raw'].get('title')}")


def on_unload(ctx):
    ctx.logger.info("演示插件卸载")
```

---

## 附：能力边界一览

| 能力 | 官方 bot | 个人号 |
|---|---|---|
| 收发群聊/私聊文本 | ✅（受额度） | ✅ |
| 图片/视频/语音 | ✅（单条 1 个媒体） | ✅ |
| 入群申请审批 | ✅ `set_group_add_request` | 视协议端 |
| 撤回群消息 | ✅ `delete_msg`（自己 2 分钟内 / 管理员撤成员） | ✅ |
| 撤回私聊消息 | ❌ 官方无接口 | ✅ |
| 禁言/踢人/群管理 | ❌ | ✅ |
| 按钮互动回调 | ✅ `notice.interaction_create` | ❌ |
| 论坛/语音房/表情表态 | ✅ 扩展事件 | ❌ |
| 群成员列表/群文件 | ❌ | ✅ |
| 合并转发 | ❌ | ✅ |

*本文与代码同步维护；事件映射规则的权威实现见 [translate.py](../src/endstone_lumenbridge/onebot/qqofficial/translate.py)。*
