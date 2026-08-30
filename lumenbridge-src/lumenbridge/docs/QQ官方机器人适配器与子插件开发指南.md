# LumenBridge QQ 官方机器人适配器 与 子插件 开发指南

> 适用版本：LumenBridge 1.0.0+
> 本文档覆盖本轮开发完成的两大核心内容：**QQ 官方机器人适配器**（全事件支持 + 模块化架构）与**子插件事件订阅体系**。
> 面向零基础读者，所有概念均从零讲起；有经验的开发者可直接跳到对应章节查表，或查阅同目录**《QQ官方机器人子插件API参考.md》**（纯 API 速查版，含 OneBot 有/没有的完整对照）。

---

## 目录

- [第一部分：基础概念（先读懂这个再往下看）](#第一部分基础概念)
- [第二部分：QQ 官方机器人适配器详解](#第二部分qq-官方机器人适配器详解)
- [第三部分：子插件开发完全指南](#第三部分子插件开发完全指南)
- [第四部分：常见问题与排错（FAQ）](#第四部分常见问题与排错faq)
- [第五部分：术语表](#第五部分术语表)

---

# 第一部分：基础概念

## 1.1 LumenBridge 是什么？

LumenBridge 是一个运行在 Minecraft BDS 服务器（基于 Endstone 插件加载器）上的**消息互通桥**。它能把 QQ 群 / QQ 官方机器人的消息和 Minecraft 服务器打通，并且提供一套**子插件系统**，让你可以像写普通机器人插件一样扩展功能。

一句话理解：

```
QQ（个人号 或 官方机器人） ⇄ LumenBridge（跑在MC服务器里） ⇄ Minecraft 服务器
```

## 1.2 什么是"适配器"（Adapter）？

适配器就是 LumenBridge 与某个"聊天平台连接方式"的**翻译官**。每种连接方式一个适配器，可以同时启用多个。

LumenBridge 目前内置 3 种适配器类型（见 [connections.py](../src/endstone_lumenbridge/connections.py) 中的 `ADAPTER_TYPES`）：

| 类型 | 说明 | 连接方式 |
|---|---|---|
| `websocket` | QQ 个人号（经 NapCat / Lagrange 等 OneBot 实现端） | 正向 / 反向 WebSocket |
| `astrbot` | 经由 AstrBot 中转的连接 | WebSocket |
| `qqofficial` | **QQ 官方机器人**（本轮开发重点） | 官方 WebSocket 网关 + REST API |

### 为什么个人号和官方机器人要分成两种适配器？

因为它们是**两套完全不同的体系**：

| 对比项 | QQ 个人号（websocket） | QQ 官方机器人（qqofficial） |
|---|---|---|
| 账号 | 普通 QQ 号，任何人可用 | 在 [QQ 开放平台](https://q.qq.com/) 注册的机器人 AppID |
| 协议 | OneBot v11（社区事实标准） | 腾讯官方网关协议（自研） |
| 群标识 | 数字群号（如 `123456789`） | `group_openid` 字符串（如 `AAAA BBBB CCCC`） |
| 用户标识 | 数字 QQ 号 | `member_openid` / `user_openid` 字符串 |
| 发消息 | 基本无限制 | 严格受限（见 2.7 节"发送额度"） |
| 能收到的消息 | 常规群聊/私聊全部 | 需订阅"事件位"，且分被动/主动 |

**一句话总结**：个人号是"模拟人"，官方机器人是"正规军"。官方机器人规矩多，但不会被封号、有腾讯官方支持。

## 1.3 什么是"域"（domain）？

因为官方机器人的群标识（openid）和个人号的数字群号长得完全不一样，LumenBridge 用 `domain` 字段区分事件来自哪个体系：

| domain 值 | 含义 | 哪些适配器产生 |
|---|---|---|
| `"qq"`（缺省） | QQ 个人号域 | websocket / astrbot |
| `"official"` | QQ 官方机器人域（群聊 + C2C 私聊） | qqofficial |
| `"guild"` | QQ 频道域（官方机器人所在频道） | qqofficial |

每个事件包（后面会讲什么是事件包）都带 `domain` 字段，子插件可以用它过滤"我只想处理个人号的消息"或"我只想处理官方机器人的消息"。

## 1.4 什么是"事件包"（OneBot pack）？

不管底层协议差异多大，LumenBridge 内部统一用 **OneBot v11 格式的事件包**流转。一个事件包就是一个 Python 字典（dict），长这样：

```python
{
    "post_type": "message",          # 事件大类：message(消息) / notice(通知) / request(请求) / meta_event(元事件)
    "message_type": "group",         # 消息类型：group(群聊) / private(私聊)
    "sub_type": "normal",            # 子类型
    "message_id": "MSG1",            # 消息 ID（官方域为官方 msg_id 字符串）
    "group_id": "GRPOPENID1",        # 群标识（官方域为 group_openid；个人号域为数字群号）
    "user_id": "MEMBER1",            # 发送者标识（官方域为 member_openid）
    "message": [...],                # 消息段数组（见 3.6 节）
    "raw_message": "你好服务器",      # 纯文本形式
    "sender": {"nickname": "Steve", "user_id": "MEMBER1", "card": ""},
    "time": 1786853582,              # 事件时间戳
    "self_id": "102000001",          # 机器人自身标识（官方域为 AppID）
    "domain": "official",            # ★ 域标记（见 1.3）
    "_lumen_adapter_id": "qo_test",  # ★ 来源适配器 ID（内部字段，用于回路由）
}
```

**翻译流程**（官方机器人方向）：

```
腾讯网关推送原始事件（如 GROUP_MESSAGE_CREATE）
        │
        ▼
qqofficial 适配器收到 → translate.py 翻译成上面的 OneBot 格式事件包
        │
        ▼
bus.emit("onebot.pack", pack)  →  分发器（dispatcher.py）按 post_type 拆分
        │
        ▼
子插件用 lumen.on("message.group.normal", 回调) 收到 (pack, reply)
```

## 1.5 本轮开发完成了什么？（总览）

1. **QQ 官方机器人全事件支持**：对照[官方文档](https://bot.q.qq.com/wiki/develop/api-v2/)，支持官方全部事件 API——群聊、C2C 私聊、频道、成员进退群、互动回调、消息审核、论坛、语音房、表情表态等 20+ 类事件。
2. **扩展事件转发机制**：OneBot 协议里没有对应语义的事件（如按钮点击回调），以 `notice.官方事件名小写` 的形式原样转发给子插件，**只有官方适配器会触发**，个人号适配器不受影响。
3. **可配置事件订阅（extra_intents）**：默认订阅群聊+C2C、群成员进退群（1<<24，无权限自动降级）、频道@、频道私信、频道变更、互动事件；特权事件（频道成员、私域频道消息、表态、审核、论坛、语音房）按需在连接配置里叠加订阅位。
4. **发送可靠性体系**（借鉴开源项目 Gensokyo 的设计并按现行官方文档修正）：
   - 被动回复凭据池（懒池）：充分利用每条消息的多次回复额度（群 5 次 / 单聊 4 次）；
   - 主动消息补发栈：主动消息被拒（错误码 22009）后暂存，下次被动回复时借额度补发；
   - 发送重试矩阵：按错误码决定重试策略。
5. **模块分离重构**：把原先 1500+ 行的单文件适配器拆成 5 个职责单一的模块，便于长期维护。

---

# 第二部分：QQ 官方机器人适配器详解

## 2.1 准备工作：你需要什么？

1. 到 [QQ 开放平台](https://q.qq.com/) 创建一个机器人，拿到 **AppID** 和 **AppSecret**；
2. 机器人需有对应的权限（群聊 / C2C / 频道，按你实际使用的场景在平台侧配置）；
3. 把机器人拉进你的 QQ 群（或让用户添加机器人为好友）。

## 2.2 模块结构（模块分离后的架构）

适配器代码位于 `src/endstone_lumenbridge/onebot/` 下：

```
onebot/
├── qqofficial_adapter.py      ← 组合入口（适配器本体，约 700 行）
└── qqofficial/                ← 子包（按职责拆分的 5 个模块）
    ├── __init__.py
    ├── constants.py           ← 协议常量
    ├── utils.py               ← 工具函数
    ├── credentials.py         ← 回复凭据管理
    ├── translate.py           ← 事件翻译器
    └── sender.py              ← 消息发送器
```

### 每个模块负责什么？（为什么这样拆？）

| 模块 | 文件 | 职责 | 什么时候需要改它？ |
|---|---|---|---|
| **适配器本体** | [qqofficial_adapter.py](../src/endstone_lumenbridge/onebot/qqofficial_adapter.py) | 网关会话（连接/鉴权/心跳/断线重连）、access_token 管理、REST 请求通道、生命周期、OneBot 兼容接口 | 改连接逻辑、鉴权方式时 |
| **常量表** | [constants.py](../src/endstone_lumenbridge/onebot/qqofficial/constants.py) | OP 码、事件订阅位（Intents）、被动回复窗口时长、错误码等所有"魔法数字" | 官方调整规则（如回复次数、窗口时长）时只需改这一处 |
| **工具函数** | [utils.py](../src/endstone_lumenbridge/onebot/qqofficial/utils.py) | HTTP 错误封装、从错误响应里提取业务错误码、清理消息文本（去 @机器人 标记/表情标记）、OneBot 消息段 ↔ 官方格式互转 | 消息格式解析规则变化时 |
| **凭据管理** | [credentials.py](../src/endstone_lumenbridge/onebot/qqofficial/credentials.py) | 被动凭据池、入群 event_id 缓存、主动补发栈（三个"存钱罐"） | 官方调整发送额度规则时 |
| **事件翻译** | [translate.py](../src/endstone_lumenbridge/onebot/qqofficial/translate.py) | 官方事件 → OneBot 事件包的**全部映射规则**（本文件是事件支持的核心） | 官方新增事件、想改映射关系时 |
| **消息发送** | [sender.py](../src/endstone_lumenbridge/onebot/qqofficial/sender.py) | 发送队列、富媒体上传、重试矩阵、补发栈执行 | 调整发送策略时 |

**组合方式**：适配器本体持有三个子模块实例（`self.credentials` / `self.translator` / `self.sender`），子模块通过 `self.ad` 引用回适配器（拿 logger、REST 通道等），没有循环导入，也没有继承纠缠：

```python
# qqofficial_adapter.py 中的组合（简化示意）
class QQOfficialAdapter:
    def __init__(...):
        self.credentials = CredentialsStore()      # 凭据存钱罐
        self.translator = EventTranslator(self)    # 事件翻译官
        self.sender = MessageSender(self)          # 消息邮递员
```

> 兼容性说明：旧版本从主文件导入的那些私有名字（如 `_PASSIVE_MAX_SEQ`、`_cache_passive`）在主文件里全部保留为**别名/委托方法**，老代码和测试不用改。

## 2.3 工作原理：适配器是怎么连上腾讯服务器的？

QQ 官方机器人使用 **WebSocket 网关** 推送事件，流程如下（全部自动完成，无需干预，但排错时需要了解）：

```
1. 获取 access_token（鉴权令牌）
   POST https://api.bot.qq.com/app/getAppAccessToken（用 AppID + AppSecret 换取）
   令牌有有效期，适配器会自动提前 2 分钟刷新；失效（HTTP 401）会强制重取。

2. 获取网关地址
   GET /gateway/bot → 拿到 wss://xxxx 地址

3. 建立 WebSocket 连接，进入握手协议：
   服务器 → Hello (op=10)            ：告知心跳间隔
   客户端 → Identify (op=2)          ：出示 token + 事件订阅位(intents)
              或 Resume (op=6)       ：断线重连时恢复会话（不丢事件）
   服务器 → READY                    ：会话建立成功，返回 session_id
   客户端 → 心跳 (op=1)              ：按间隔持续心跳，服务器回 ACK (op=11)
   服务器 → 事件推送 (op=0)          ：所有订阅的事件从这里流下来

4. 特殊指令：
   服务器 → RECONNECT (op=7)         ：服务端要求重连（保留 session_id 走 Resume）
   服务器 → INVALID_SESSION (op=9)   ：会话失效（清空 session_id 重新 Identify）
```

**可靠性设计**：

- 断线后指数退避重连（2s 起，最长 60s），可配置最小重连间隔（`connect_interval`）；
- WS 关闭码 `4004` = 鉴权失败（自动重取 token）、`9001/9005` = 会话不可恢复（自动全新 Identify）；
- 服务端连发 RECONNECT 不会造成重连风暴（退避计数不重置）。

## 2.4 配置项详解（connections/qqofficial.json）

适配器配置按类型分文件存放在插件数据目录的 `connections/` 下，官方机器人卡片在 `connections/qqofficial.json`，通常通过 WebUI 的"适配器卡片"编辑。qqofficial 类型的完整字段（**不含** WebSocket 专属字段 `ws_type` / `target` / `listen_host` / `listen_port` / `access_token`——官方域走网关鉴权，无此概念）：

```json
{
    "id": "qqofficial_default",   // 适配器唯一 ID（字母数字下划线横线，≤64字符）
    "type": "qqofficial",         // ★ 类型必须是 qqofficial
    "name": "QQ 官方机器人",       // 显示名（任意，≤64字符）
    "enabled": true,              // ★ 启用开关
    "app_id": "102000001",        // ★ 开放平台 AppID（≤32位字母数字）
    "app_secret": "xxxxxxxx",     // ★ 开放平台 AppSecret（≤128字符）
    "sandbox": false,             // 沙箱模式（联调时用，走 sandbox.api.bot.qq.com）
    "suppress_connection_log": true, // 后台静默日志（默认开启）：连接/断连/重连、凭据降级、重试与补发提示等运行类日志不打印；false 打印全部便于排障
    "connect_interval": 60000,    // 两次网关连接尝试的最小间隔（毫秒），0=纯指数退避
    "extra_intents": 0,           // ★ 附加事件订阅位（重点，见 2.5 节）
    "bot_qq": 0,                  // 机器人的真实 QQ 号（仅用于显示昵称头像；AppID 不是 QQ 号）
    "admin_qq": [],               // 管理员列表
    "main_group": "AAAA,BBBB",    // 主群列表：官方域填 group_openid（逗号分隔）
    "sync": { ... }               // 群服互通设置（同个人号适配器）
}
```

**校验规则**（[connections.py](../src/endstone_lumenbridge/connections.py) `_validate_adapter`）：

- `extra_intents` 必须是 **0 ~ 2^31-1 之间的整数**（bool 会被拒绝）；
- `connect_interval` 必须是 0 ~ 86400000 毫秒数；
- `app_id` 只能是 ≤32 位字母数字；`sandbox` 必须是布尔值。

> 小知识：怎么拿到自己群的 `group_openid`？——把机器人拉进群，它收到 `GROUP_ADD_ROBOT`（机器人入群）事件时，会在控制台日志里打印 group_openid，照抄进配置即可。

## 2.5 事件订阅位（Intents）——决定你能收到哪些事件

官方网关用**二进制位**控制事件订阅，每一位代表一类事件。想收某类事件，就把对应的位"点亮"（置 1）。

**默认订阅**（无需配置，开箱即用）：

| 位 | 含义 | 包含的事件 |
|---|---|---|
| `1<<25` | 群聊 + C2C 消息 | 群消息、私聊消息、机器人进退群、好友增删、主动消息开关、**入群申请**等 |
| `1<<24` | QQ 群成员进退群 | GROUP_MEMBER_ADD / GROUP_MEMBER_REMOVE（与 1<<25 双订；无权限自动降级，见下） |
| `1<<30` | 公域频道 @ 消息 | `AT_MESSAGE_CREATE` |
| `1<<12` | 频道私信 | `DIRECT_MESSAGE_CREATE` |
| `1<<0` | 频道变更 | GUILD_CREATE / UPDATE / DELETE、CHANNEL_* |
| `1<<26` | 互动事件 | INTERACTION_CREATE（按钮/组件回调） |

> **1<<24 自动降级**：官方文档对该订阅位存在 CDN 双版本（1<<24 与 1<<25 说法不一），故默认双订。若机器人无 1<<24 权限导致网关拒连，适配器在**连续 2 次 Identify 失败后自动摘除该位降级重连**（自愈，日志有提示），群成员进退群事件改由 1<<25 推送。

**按需订阅**（在 `extra_intents` 里配置，多位用"按位或"相加）：

| 位 | 十进制值 | 含义 | 触发的事件 |
|---|---|---|---|
| `1<<1` | 2 | 频道成员变更（**需官方开通权限**） | GUILD_MEMBER_ADD / UPDATE / REMOVE |
| `1<<9` | 512 | 私域频道消息 | MESSAGE_CREATE / MESSAGE_DELETE（仅私域机器人可订阅） |
| `1<<10` | 1024 | 表情表态 | MESSAGE_REACTION_* / ADD_REACTION / DELETE_REACTION |
| `1<<27` | 134217728 | **消息审核** | MESSAGE_AUDIT_PASS / MESSAGE_AUDIT_REJECT |
| `1<<28` | 268435456 | 论坛事件 | FORUM_*（官方 2026-08 新名，兼容旧名 OPEN_FORUM_*） |
| `1<<29` | 536870912 | 语音房 | AUDIO_START / FINISH / ON_MIC / OFF_MIC |

**怎么算 extra_intents 的值？** 把你要的位相加即可。例如同时想要"消息审核 + 论坛事件"：

```
(1<<27) + (1<<28) = 134217728 + 268435456 = 402653184
```

那就填 `"extra_intents": 402653184`。

> ⚠️ **重要提醒**：
> 1. 订阅机器人**没有权限**的位会导致网关**拒绝连接**！比如没开"频道成员变更"权限就不要填 `1<<1`。
> 2. 不确定就先填 0，一个功能一个功能地加，每次加完观察日志有没有拒连。
> 3. 群成员进退群（1<<24）与互动事件（1<<26）已默认订阅，无需配置。常用值速查：`0`（默认）｜`134217728`（消息审核）｜`268435456`（论坛）｜`402653184`（审核+论坛）。

## 2.6 事件支持总表（官方事件 → 子插件订阅名）★核心速查表

适配器把官方事件翻译成 OneBot 事件包后，子插件用 `lumen.on("事件名", 回调)` 订阅。**这张表是从子插件视角看的完整对照表**，建议收藏。

### 2.6.1 消息类事件（有 OneBot 对应语义）

| 官方事件 | 订阅位 | 翻译结果 | 子插件订阅名 | 说明 |
|---|---|---|---|---|
| `GROUP_MESSAGE_CREATE` | 1<<25 | 群消息 | `message.group.normal` | 全量群消息（无需@机器人） |
| `GROUP_AT_MESSAGE_CREATE` | 1<<25 | 群消息 | `message.group.normal` | @机器人的群消息（载荷同上，共用处理） |
| `C2C_MESSAGE_CREATE` | 1<<25 | 私聊消息 | `message.private.friend` | 单聊消息 |
| `AT_MESSAGE_CREATE` | 1<<30 | 群消息（domain=guild） | `message.group.normal` | 频道@消息，group_id=channel_id |
| `MESSAGE_CREATE` | 1<<9（私域，需配置） | 群消息（domain=guild） | `message.group.normal` | 私域频道全量消息（载荷同上） |
| `DIRECT_MESSAGE_CREATE` | 1<<12 | 私聊消息（domain=guild） | `message.private.friend` | 频道私信 |

> 消息类事件的回调签名是 `回调(pack, reply)`，`reply` 是快捷回复函数（见 3.5 节）。

### 2.6.2 通知类事件（有 OneBot 对应语义）

| 官方事件 | 订阅位 | 翻译结果 | 子插件订阅名 | 说明 |
|---|---|---|---|---|
| `GROUP_ADD_ROBOT` | 1<<25 | 机器人入群 | `notice.group_increase` | 日志会打印 group_openid；event_id 会存入凭据池 |
| `GROUP_DEL_ROBOT` | 1<<25 | 机器人被移出群 | `notice.group_decrease` | sub_type=kick_me |
| `GROUP_MEMBER_ADD` | 1<<24（默认订阅） | 群成员入群 | `notice.group_increase` | user_id=进群成员 openid，附 raw 原文 |
| `GROUP_MEMBER_REMOVE` | 1<<24（默认订阅） | 群成员退群 | `notice.group_decrease` | 同上 |
| `FRIEND_ADD` | 1<<25 | 好友添加 | `notice.friend_add` | C2C 维度 |
| `FRIEND_DEL` | 1<<25 | 好友删除 | `notice.friend_del` | C2C 维度 |
| `PUBLIC_MESSAGE_DELETE` | 1<<30 | 频道消息撤回 | `notice.group_recall` | domain=guild；user_id=发送者、operator_id=操作者 |
| `MESSAGE_DELETE` | 1<<9（私域，需配置） | 私域频道消息撤回 | `notice.group_recall` | 同上 |
| `DIRECT_MESSAGE_DELETE` | 1<<12 | 频道私信撤回 | `notice.friend_recall` | domain=guild；user_id=好友 |
| `GUILD_MEMBER_ADD` | 1<<1（需权限） | 频道成员加入 | `notice.group_increase` | group_id=guild_id |
| `GUILD_MEMBER_REMOVE` | 1<<1（需权限） | 频道成员移除 | `notice.group_decrease` | 同上 |

> 通知类事件回调签名是 `回调(pack)`（没有 reply）。
> 官方 bot **没有** QQ 群消息撤回的**推送**事件（撤回能力经 `delete_msg` 主动调用，见 3.4.2 节）；上表撤回均为频道域。

### 2.6.2a 请求类事件（有 OneBot 对应语义）

| 官方事件 | 订阅位 | 翻译结果 | 子插件订阅名 | 说明 |
|---|---|---|---|---|
| `GROUP_JOIN_REQUEST` | 1<<25（默认订阅） | 入群申请 | `request.group` | **机器人须为群管理员才能收到**；flag=join_request_id 供 `set_group_add_request` 审批回传；comment=用户名/邀请人/问答拼接；OneBot 的 sub_type 恒为 add（官方 apply_source=invited 是"邀请用户入群"，与 OneBot invite"邀请机器人入群"语义不同） |

> 请求类事件回调签名是 `回调(pack)`。审批方法见 3.4.2 节 `set_group_add_request`。

### 2.6.3 扩展事件（OneBot 没有对应语义 → 原样转发）★本轮新增

以下事件在 OneBot v11 协议里**不存在**对应概念。适配器把它们包装成"扩展 notice"转发：`notice_type` = 官方事件名**小写**，`raw` 字段保留**官方原始载荷**（原封不动的 dict）。

**关键特性：这些事件只有官方适配器会产生**，个人号适配器永远不会触发同名事件，所以子插件可以放心订阅，不会和现有逻辑冲突。不订阅的插件完全不受影响。

| 官方事件 | 订阅位 | 子插件订阅名 | 触发场景 |
|---|---|---|---|
| `INTERACTION_CREATE` | 1<<26（默认订阅） | `notice.interaction_create` | 用户点击机器人发的**按钮/组件** |
| `MESSAGE_AUDIT_PASS` | 1<<27（需配置） | `notice.message_audit_pass` | 主动消息**审核通过** |
| `MESSAGE_AUDIT_REJECT` | 1<<27（需配置） | `notice.message_audit_reject` | 主动消息**审核被拒** |
| `FORUM_THREAD_CREATE` / `UPDATE` / `DELETE` | 1<<28（需配置） | `notice.forum_thread_create` 等 | 论坛主题创建/更新/删除（官方 2026-08 新名） |
| `FORUM_POST_CREATE` / `DELETE` | 1<<28（需配置） | `notice.forum_post_create` 等 | 论坛帖子创建/删除 |
| `FORUM_REPLY_CREATE` / `DELETE` | 1<<28（需配置） | `notice.forum_reply_create` 等 | 论坛回帖创建/删除 |
| `FORUM_PUBLISH_AUDIT_RESULT` | 1<<28（需配置） | `notice.forum_publish_audit_result` | 论坛发布审核结果 |
| `OPEN_FORUM_*` | 1<<28（需配置） | `notice.open_forum_*` | 论坛旧事件名（兼容保留） |
| `AUDIO_START` | 1<<29（需配置） | `notice.audio_start` | 语音房开播 |
| `AUDIO_FINISH` | 1<<29（需配置） | `notice.audio_finish` | 语音房结束 |
| `AUDIO_ON_MIC` | 1<<29（需配置） | `notice.audio_on_mic` | 上麦 |
| `AUDIO_OFF_MIC` | 1<<29（需配置） | `notice.audio_off_mic` | 下麦 |
| `MESSAGE_REACTION_ADD` / `ADD_REACTION` | 1<<10（需配置） | `notice.add_reaction` 等 | 表情表态添加（新旧事件名都兼容） |
| `MESSAGE_REACTION_REMOVE` / `DELETE_REACTION` | 1<<10（需配置） | `notice.delete_reaction` 等 | 表情表态删除 |
| `GUILD_CREATE` / `UPDATE` / `DELETE` | 1<<0（默认订阅） | `notice.guild_create` 等 | 机器人加入/更新/离开频道 |
| `CHANNEL_CREATE` / `UPDATE` / `DELETE` | 1<<0（默认订阅） | `notice.channel_create` 等 | 子频道创建/更新/删除 |
| `GUILD_MEMBER_UPDATE` | 1<<1（需权限） | `notice.guild_member_update` | 频道成员资料变更 |
| `GROUP_MSG_REJECT` | 1<<25 | `notice.group_msg_switch` | 群管理员**关闭**了"接收机器人主动消息" |
| `GROUP_MSG_RECEIVE` | 1<<25 | `notice.group_msg_switch` | 群管理员**开启**了主动消息 |
| `C2C_MSG_REJECT` | 1<<25 | `notice.friend_msg_switch` | 用户**关闭**了机器人的主动推送 |
| `C2C_MSG_RECEIVE` | 1<<25 | `notice.friend_msg_switch` | 用户**开启**了主动推送 |
| （任何未枚举的新事件） | — | `notice.事件名小写` | **兜底机制**：官方未来新增的事件也会自动转发，日志打 DEBUG 提示 |

> **关于 `group_msg_switch` / `friend_msg_switch` 的设计说明**：OneBot 标准里没有"主动消息开关"这个概念，最接近的 `group_ban`（禁言）语义完全不符——如果强行映射，下游会把"管理员关闭主动消息"误判为"机器人被禁言"。所以这里用了自定义扩展类型，`sub_type` 为 `"reject"`（关闭）或 `"receive"`（开启），不识别它的插件会安全忽略。

### 2.6.4 扩展事件包长什么样？（信封翻译、载荷穿透）

扩展事件采用**半翻译**设计：外层"信封"包装成标准 OneBot notice 结构（以便混入统一事件流、复用订阅/去抖/域隔离机制），内层"载荷"（官方原始数据）**一字不动**放进 `raw`：

```python
{
    "post_type": "notice",
    "notice_type": "interaction_create",   # 官方事件名小写
    "sub_type": "",
    "user_id": "USER_OPENID",              # 从载荷里尽力提取的操作者
    "self_id": "102000001",
    "time": 1786853582,
    "domain": "official",
    "official_event": "INTERACTION_CREATE",# ★ 官方原始事件名（大写），便于精确分发
    "raw": { ... 官方原始载荷，原封不动 ... },  # ★ 想用什么字段自己取
    "group_id": "GRPOPENID1",              # 载荷里有群/频道标识时附带
    "_lumen_adapter_id": "qo_xxx",
}
```

> ⚠️ **重要约定：`raw` 是权威数据源**。`user_id` / `group_id` 等便捷字段只是"尽力提取"——载荷里没有对应字段时可能为空或不全。写正式功能请一律从 `raw` 取值（字段名与官方文档完全一致，照着官方文档取即可）；便捷字段只适合打日志这类容错场景。

---

## 2.7 发送机制详解（最重要的"规矩"部分）

官方机器人发消息**不是想发就发**，理解这一节才能解释"机器人为什么有时候不说话"。

### 2.7.1 两种消息：被动 vs 主动

| | 被动回复 | 主动消息 |
|---|---|---|
| 触发条件 | 用户/群里**先发了消息**，机器人回复 | 机器人**主动**发起（如开服通知） |
| 额度 | 群：每条收到的消息可回复 **5 次**；单聊：**4 次** | 每月每用户/群有**条数上限**（腾讯侧控制） |
| 时间窗 | 群：**5 分钟**内；单聊：**60 分钟**内（以官方文档为准） | 无时间窗，但有月度额度 |
| 用户可否关闭 | 不可 | 可（关闭后发送会收到错误码 22009） |

### 2.7.2 被动凭据池（credentials.py 中的"懒池"）

每收到一条消息，适配器就把它的 `msg_id` 存进**凭据池**（按目标分组），并记录过期时间。发消息时从池里取凭据：

- **优先取没用过的最新凭据**（摊平额度，不会一条消息用光 5 次而别的消息浪费）；
- 用过的凭据配**递增的 msg_seq**（第 1 次回复 seq=1，第 2 次 seq=2……），同一 msg_id 可回复 5 次（群）/ 4 次（单聊）；
- 全部用尽或过期 → 自动降级为主动发送；
- 每个目标最多缓存 8 条消息的凭据（`PASSIVE_POOL_MAX = 8`），自动清理过期项。

> 设计借鉴了开源项目 Gensokyo 的"懒池"，但按现行官方文档修正了数值：**C2C 单聊是 4 次而不是 Gensokyo 时代的 5 次**（2026-01-10 起官方调整），群聊仍是 5 次。两种场景的限额在凭据入池时分别记录（见 [credentials.py](../src/endstone_lumenbridge/onebot/qqofficial/credentials.py) 的 `cache_passive` 的 `max_seq` 参数）。

### 2.7.3 入群 event_id 凭据

机器人被拉进群的瞬间（`GROUP_ADD_ROBOT` 事件），事件自带一个 `event_id`，**可以在 30 分钟内当作被动回复凭据用，且不消耗主动额度**。适配器会自动缓存它——机器人刚入群时的欢迎消息就是借这个发的，不受主动消息频次限制。

### 2.7.4 主动消息补发栈（AtoP：Active to Passive）

主动消息被拒（错误码 **22009**，额度用尽或用户关闭了开关）时：

1. 消息进入该目标的**补发栈**暂存（每目标最多 5 条，栈满丢新的）；
2. 下次该目标来消息、机器人被动回复**发送成功**后，借凭据池的**剩余额度**把栈里的消息补发出去；
3. 每次最多补 3 条（`ACTIVE_STACK_FLUSH = 3`，太多会挤占正常回复额度），凭据耗尽就停，剩余的下次再补；
4. 补发失败（比如凭据也过期了）条目留在栈里等下次机会，不会丢。

### 2.7.5 发送重试矩阵（sender.py）

发送失败不是无脑重试，而是**看错误码下菜**：

| 情况 | 策略 |
|---|---|
| 超时 / 网络错误 | 重试，最多 3 次；文本间隔 1s、富媒体 3s；**每次重试递增 msg_seq**（官方按 (msg_id, msg_seq) 去重，不递增会被判定重复消息而静默丢弃） |
| 错误码 22009（主动被拒） | **不重试**，进补发栈 |
| 错误码 40034025（event_id 无效） | 从请求体删掉 event_id 后**立即重发一次** |
| 其他业务错误 | 按普通重试处理 |

### 2.7.6 发送优先级总结（一条消息的完整旅程）

```
子插件调用 send_group_msg / send_private_msg
        │
        ▼
进入发送队列（先进先出，满 100 条丢最旧的并告警）
        │
        ▼
取出一条消息：
  ① 有富媒体？→ 先上传到官方 files 接口（失败则降级为纯文本，不浪费凭据）
  ② 凭据池里有该目标的被动凭据？→ 有：带上 msg_id + msg_seq（不受主动频次限制）
                                   └ 无：有入群 event_id？→ 带上（不消耗主动额度）
                                        └ 都没有：裸发（消耗主动额度）
  ③ 发送 → 成功？→ 是被动回复且成功 → 顺手执行补发栈 flush
                  └ 被拒(22009) 且是主动 → 入补发栈，等下次机会
  ④ 每条消息之间限速 0.2s（避免触发平台 QPS 限制）
```

### 2.7.7 官方业务错误码速查

| 错误码 | 含义 | 适配器的处理 |
|---|---|---|
| 22009 | 主动消息被拒（额度尽/用户关闭） | 入补发栈 |
| 40034025 | event_id 无效 | 删 event_id 重发一次 |
| 40034005 | 被动回复 msg_id 已过期 | 按普通重试（凭据池会自动淘汰过期项） |
| HTTP 401 | access_token 失效 | 强制刷新 token 重试一次 |
| WS 关闭码 4004 | 网关鉴权失败 | 重取 token 重连 |
| WS 关闭码 9001/9005 | 会话不可恢复 | 清空会话状态，全新 Identify |

## 2.8 富媒体（图片/视频/语音）

**发送**：消息段里的第一个富媒体段会被提取（官方单条消息只支持一个媒体），先经 `/v2/{groups|users}/{target}/files` 上传（URL 直接传；本地文件走 base64，上限约 8MB 留余量），然后以 `msg_type=7` 发送。

**接收**：官方附件按 content_type 映射为 OneBot 段：`image/*` → image、`video/*` → video、`audio/*` 或 `voice` → record。附件自带 URL 直接用；没有 URL 时自动调 media 接口换取带时效的下载地址。

## 2.9 已知限制（诚实说明）

- **大部分 OneBot action 不支持**：官方机器人没有"禁言、踢人、取群成员列表"这类接口。调用不支持的方法会安全降级（回调 None 并打 warning），不会崩。**已实现的主动能力**：发消息、入群审批（`set_group_add_request`）、群消息撤回（`delete_msg`）、基础查询。
- **撤回限制**：机器人自己发的群消息限 2 分钟内；私聊消息官方无撤回接口（告警跳过）；QQ 群撤回无推送事件（主动调用才有）。
- **群列表接口不存在**：`get_group_list` 用配置里的 openid 列表本地兜底。
- **合并转发**：官方无对应接口（常量 `FORWARD_NODE_LIMIT` 已预留降级模拟的参数，功能待实现）。
- **markdown / 按钮消息发送**：接收回调已支持（`notice.interaction_create`），主动发送待实现。
- **@ 普通群成员 / 引用回复**：官方群聊仅支持 @ 机器人，reply 段降级为纯文本。

---

# 第三部分：子插件开发完全指南

## 3.1 子插件是什么？

子插件是放在 LumenBridge 的 `subplugins/` 目录下的**独立小插件**。它不用碰 LumenBridge 的内部代码，通过一个注入的**上下文对象**（惯例命名 `lumen`）获得全部能力：收发 QQ 消息、监听游戏事件、读写私有存储、执行服务器命令……

可以把它理解为：**LumenBridge 是插座，子插件是电器**。

## 3.2 目录结构与清单文件

一个最小的子插件长这样：

```
subplugins/
└── my_plugin/              ← 文件夹名 = 插件名（字母数字下划线）
    ├── lumen.json          ← 清单（没有的话 LumenBridge 会自动生成模板）
    └── main.py             ← 入口（必须叫这个名）
```

**lumen.json 字段说明**：

```json
{
    "name": "my_plugin",     // 插件名（缺省取文件夹名）
    "version": "1.0.0",      // 版本
    "author": "你的名字",     // 作者
    "desc": "插件简介",       // 描述
    "load": true,            // ★ false = 跳过加载（临时禁用插件用）
    "priority": "main",      // 加载顺序：first(最先) / main(默认) / final(最后)
    "min_v": ""              // 最低 LumenBridge 版本要求（留空不检查）
}
```

## 3.3 生命周期：on_load 与 on_unload

`main.py` **必须**暴露 `on_load(ctx)` 函数，**可选**暴露 `on_unload(ctx)`：

```python
lumen = None  # 模块级变量保存上下文，供各回调使用

def on_load(ctx):
    """加载时被调用，ctx 就是 lumen 上下文对象"""
    global lumen
    lumen = ctx
    lumen.logger.info("我的插件加载了！")

def on_unload(ctx):
    """热重载/关服时被调用（事件监听会被自动清理，这里只做额外收尾）"""
    ctx.logger.info("我的插件卸载了")
```

> 你不需要手动注销事件监听——`ctx._cleanup()` 会自动把本插件注册的所有监听、正则动作、翻译全部清理掉，热重载不会产生重复触发。

## 3.4 lumen 上下文对象完整参考

`on_load(ctx)` 拿到的 `ctx`（类型 [LumenContext](../src/endstone_lumenbridge/subplugin/context.py)，版本 "1.0.0"）包含以下成员：

### 3.4.1 事件相关

| API | 说明 |
|---|---|
| `lumen.on(事件名, 回调)` | 订阅事件（见 3.5 节完整事件表） |
| `lumen.once(事件名, 回调)` | 订阅一次，触发后自动注销 |
| `lumen.off(事件名, 回调)` | 手动注销 |
| `lumen.emit(事件名, *args)` | 主动发出一个事件 |

### 3.4.2 消息发送

| API | 说明 |
|---|---|
| `lumen.QClient` | 适配器中枢（AdapterHub），发消息的主要入口（双域自动路由：openid → 官方、数字 → 个人号） |
| `lumen.QClient.send_group_msg(群标识, 消息)` | 发群消息（官方域群标识 = group_openid 字符串） |
| `lumen.QClient.send_private_msg(用户标识, 消息)` | 发私聊（官方域 = user_openid 字符串） |
| `lumen.QClient.set_group_add_request(flag, approve=True, reason="")` | ★官方独有：入群申请审批。flag 取 `request.group` 事件包；机器人须为群管理员；拒绝时可带 reason（≤255字） |
| `lumen.QClient.delete_msg(message_id)` | 撤回群消息：机器人自己的限 2 分钟内；机器人是管理员可撤普通成员消息。message_id 直接取事件包；私聊消息官方无接口会告警跳过 |
| `lumen.call_action(action, params, callback)` | 通用 OneBot action 调用（官方适配器对不支持的 action 回调 None） |
| `lumen.msgbuilder` | 消息段构建器（见 3.6 节） |
| `lumen.packbuilder` | 数据包构建器 |

> 提示：在**消息事件回调**里尽量用自带的 `reply` 函数（见 3.5.1），它会自动路由回来源适配器、自动携带被动凭据，最省额度。

### 3.4.3 数据与环境

| API | 说明 |
|---|---|
| `lumen.storage.read(文件名, 默认值)` | 读私有 JSON 存储（不存在/损坏时写默认值并返回；损坏文件自动备份为 `.corrupt-时间戳`） |
| `lumen.storage.write(文件名, 数据)` | 写私有 JSON 存储（原子写入，防断电损坏；自动加锁防并发） |
| `lumen.storage.path(文件名)` | 取存储文件的绝对路径 |
| `lumen.env.get(键)` / `lumen.env.set(键, 值)` | 全局共享变量池（`main_group` 在事件回调中返回**当前来源群**，兼容旧插件过滤写法） |
| `lumen.logger.info/warning/error/debug(...)` | 带插件名前缀的日志 |
| `lumen.debug` | 是否处于 debug 模式（布尔） |

### 3.4.4 Minecraft 交互（mc 桥）

| API | 说明 |
|---|---|
| `lumen.mc.listen(事件名, 回调)` | 监听游戏事件。兼容别名：`onJoin`/`onLeft`/`onChat`/`onDeath`；或任意 Endstone 事件类名（回调收原生事件对象） |
| `lumen.mc.runcmd(命令)` | 异步执行命令（不关心输出） |
| `lumen.mc.runcmdEx(命令, timeout=5)` | 执行命令并捕获输出，返回 `{"success": bool, "output": str}`。⚠️ 阻塞调用线程，**不要在游戏主线程调用**（会死锁） |
| `lumen.mc.broadcast(消息)` | 全服广播 |
| `lumen.mc.online_players` | 在线玩家名列表（线程安全快照，最多阻塞主线程 2 秒） |

### 3.4.5 Web 扩展（web 桥）

| API | 说明 |
|---|---|
| `lumen.web.createConfig()` | 创建 WebUI 配置表单构建器 |
| `lumen.web.registerApi(method, path, handler, need_auth=True)` | 注册 REST API 到 WebUI |
| `lumen.web.registerPage(标题, 相对路径)` | 注册自定义 Web 页面 |

### 3.4.6 Endstone 直达（进阶）

| API | 说明 |
|---|---|
| `lumen.plugin` | LumenBridge 插件实例（endstone.plugin.Plugin 全部能力） |
| `lumen.server` | Endstone Server 对象（后台线程请配合 `run_on_main`） |
| `lumen.scheduler` | Endstone 任务调度器 |
| `lumen.endstone` | endstone 顶级模块透传（随版本升级自动获得新 API） |
| `lumen.import_module(名字)` | 按需导入任意 endstone 子模块 |
| `lumen.get_player(名字或UUID)` | 获取在线玩家对象 |
| `lumen.run_on_main(函数, delay=1)` | 把函数调度到游戏主线程执行 |
| `lumen.register_regex_action(类型名, 处理函数)` | 向正则引擎注册自定义动作（规则里 `callPluginCommand` 可调用） |
| `lumen.register_command(命令名, handler, description="")` | 注册 Endstone 服务器命令：`handler(sender, args) -> bool`（主线程执行，args 不含命令名）；命令名非法或已被占用返回 False。首次注册写入 `command_palette.json`，重启服务器后命令生效；卸载子插件仅解除 handler 绑定。`lumen.plugin.register_command(...)` 为等效兼容入口 |

## 3.5 事件订阅完全指南 ★

### 3.5.1 消息事件：回调拿 (pack, reply)

```python
def on_group_message(pack, reply):
    # pack: 3.1.4 节介绍的事件包
    # reply(消息, quote=False): 快捷回复，自动路由回来源适配器；
    #   quote=True 时引用原消息
    if pack.get("domain") not in ("official", "qq"):  # 过滤来源域
        return
    text = pack.get("raw_message", "")
    if text == "你好":
        reply("你好呀！", quote=True)

lumen.on("message.group.normal", on_group_message)     # 群消息
lumen.on("message.private.friend", on_private_message) # 私聊
```

**事件名的构成规则**：`message.{message_type}.{sub_type}`。常用的就是上面两个。

### 3.5.2 通知事件：回调拿 (pack)

```python
def on_member_join(pack):
    gid = pack.get("group_id")     # 官方域为 group_openid
    uid = pack.get("user_id")      # 进群成员的 openid
    lumen.logger.info(f"群 {gid} 新成员：{uid}")

lumen.on("notice.group_increase", on_member_join)   # 成员/机器人入群
lumen.on("notice.group_decrease", on_member_leave)  # 成员/机器人退群
lumen.on("notice.friend_add", on_friend_add)
lumen.on("notice.friend_del", on_friend_del)
lumen.on("notice.group_recall", on_group_recall)    # 撤回（频道域）
```

### 3.5.2a 请求事件：回调拿 (pack)

```python
def on_join_request(pack):
    flag = pack["flag"]            # join_request_id，审批回传用
    user = pack["user_id"]         # 申请者 openid
    ok = user not in lumen.storage.read("blacklist.json", [])
    lumen.QClient.set_group_add_request(flag, approve=ok, reason="黑名单")

lumen.on("request.group", on_join_request)   # 入群申请（机器人须为群管理员）
```

### 3.5.3 官方扩展事件（本轮核心新增）：回调拿 (pack)

订阅名 = `notice.` + 官方事件名小写。官方原始数据在 `pack["raw"]`，官方事件原名在 `pack["official_event"]`：

```python
def on_button_click(pack):
    raw = pack.get("raw", {})          # 官方原始载荷（字典）
    data = raw.get("data", {})         # 按官方文档取字段，例如：
    #   data.get("resolved") 里是按钮交互数据
    lumen.logger.info(f"按钮回调：{pack.get('official_event')} 来自 {pack.get('user_id')}")

def on_audit_result(pack):
    raw = pack.get("raw", {})
    lumen.logger.info(f"审核结果事件：{pack.get('notice_type')}")

lumen.on("notice.interaction_create", on_button_click)   # 按钮/组件回调
lumen.on("notice.message_audit_pass", on_audit_result)   # 审核通过
lumen.on("notice.message_audit_reject", on_audit_result) # 审核被拒
lumen.on("notice.add_reaction", on_reaction)             # 表情表态
```

> ⚠️ 记得检查 2.5 节：扩展事件大多需要在适配器配置里设置对应的 `extra_intents` 订阅位，否则根本收不到事件！

### 3.5.4 完整事件名速查

| 分类 | 事件名 |
|---|---|
| 消息 | `message.group.normal`、`message.private.friend` |
| 群通知 | `notice.group_increase`、`notice.group_decrease`、`notice.group_recall`、`notice.group_msg_switch`（主动消息开关） |
| 好友通知 | `notice.friend_add`、`notice.friend_del`、`notice.friend_recall`、`notice.friend_msg_switch` |
| 请求 | `request.group`（入群申请，官方 bot 默认订阅） |
| 官方扩展 | `notice.interaction_create`、`notice.message_audit_pass/reject`、`notice.forum_*`（新名）/ `notice.open_forum_*`（旧名）、`notice.audio_*`、`notice.add_reaction`、`notice.delete_reaction`、`notice.guild_*`、`notice.channel_*`、`notice.guild_member_update` |
| 游戏 | `mc.player_join`、`mc.player_left`、`mc.player_chat`、`mc.player_death`（mc.listen 的别名内部走这些） |
| 元事件 | `meta_event.lifecycle`（官方 bot READY 时合成 connect；heartbeat 仅个人号协议端推送） |

## 3.6 消息格式与 msgbuilder

发消息时，`消息` 参数支持三种写法：

```python
# ① 纯字符串（最简单）
lumen.QClient.send_group_msg(group_id, "Hello!")

# ② 消息段数组（可混合文本与媒体）
lumen.QClient.send_group_msg(group_id, [
    lumen.msgbuilder.text("看这张图："),
    lumen.msgbuilder.image("https://example.com/pic.jpg"),  # URL
    # 或本地文件路径 / base64（官方域走 files 接口上传）
])

# ③ reply 快捷回复（消息事件回调里）
reply(["文字和", lumen.msgbuilder.text("消息段可以混用")], quote=True)
```

常用消息段类型（官方域的注意点）：

| 段类型 | 说明 | 官方域注意 |
|---|---|---|
| `text` | 文本 | 单条最长 2000 字符（超长自动截断） |
| `image` / `video` / `record` | 图片/视频/语音 | 官方**单条消息只支持 1 个媒体**，多余的会被忽略；本地文件 ≤8MB |

## 3.7 完整实战示例

下面这个示例覆盖了本轮开发的全部重点能力（完整可运行，放进去就能用）：

```python
"""官方机器人全能演示子插件：事件订阅 + 扩展事件 + 凭据友好回复"""

lumen = None

DEFAULT_CONFIG = {
    "watch_group": "",        # 留空 = 跟随主群
    "audit_notify": True,     # 审核结果要不要通知管理员
}

def on_load(ctx):
    global lumen
    lumen = ctx
    conf = lumen.storage.read("config.json", DEFAULT_CONFIG)

    # ---- 基础消息 ----
    lumen.on("message.group.normal", on_group_message)

    # ---- 群成员进退群（需 extra_intents 含 1<<24 = 16777216）----
    lumen.on("notice.group_increase", on_member_change)
    lumen.on("notice.group_decrease", on_member_change)

    # ---- 官方扩展事件（需对应订阅位，见文档 2.5 节）----
    lumen.on("notice.interaction_create", on_interaction)      # 1<<26
    lumen.on("notice.message_audit_pass", on_audit)            # 1<<27
    lumen.on("notice.message_audit_reject", on_audit)          # 1<<27
    lumen.on("notice.group_msg_switch", on_switch)             # 默认订阅即有

    lumen.logger.info("官方机器人演示插件已加载")


def on_group_message(pack, reply):
    """被动回复：reply 自动携带被动凭据，不受主动额度限制"""
    if pack.get("domain") != "official":
        return  # 只处理官方机器人事件（可选；个人号 domain == "qq"）
    text = pack.get("raw_message", "").strip()
    if text == "状态":
        result = lumen.mc.runcmdEx("list")
        reply(f"服务器状态：{result['output'] or '查询失败'}", quote=True)


def on_member_change(pack):
    """群成员进退群通知到主群"""
    joined = pack["notice_type"] == "group_increase"
    who = pack.get("user_id", "?")
    lumen.QClient.send_group_msg(
        lumen.env.get("main_group"),
        [lumen.msgbuilder.text(f"有成员{'加入' if joined else '离开'}了：{who}")],
    )


def on_interaction(pack):
    """按钮点击回调：官方原始载荷在 pack['raw']"""
    raw = pack.get("raw", {})
    lumen.logger.info(f"收到互动回调，原始数据：{raw}")


def on_audit(pack):
    """主动消息审核结果"""
    passed = pack["notice_type"] == "message_audit_pass"
    lumen.logger.info(f"主动消息审核{'通过' if passed else '被拒'}")


def on_switch(pack):
    """群管理员开关了主动消息（OneBot 无此语义，扩展事件）"""
    rejected = pack.get("sub_type") == "reject"
    lumen.logger.warning(
        f"群 {pack.get('group_id')} {'关闭' if rejected else '开启'}了主动消息接收"
    )
```

## 3.8 双域插件的最佳实践

如果你的子插件要**同时兼容个人号和官方机器人**：

1. **过滤看 domain**：`pack.get("domain")` 区分来源；
2. **标识别假设类型**：`group_id` / `user_id` 官方域是字符串、个人号域可能是数字，**统一 `str()` 处理**；
3. **回复用 reply**：快捷回复自动路由回来源适配器，无需关心标识类型；
4. **能力探测**：官方机器人不支持 action（撤回/禁言等），`call_action` 会回调 None，记得判空；
5. **扩展事件随便订**：个人号适配器永远不会触发 `notice.interaction_create` 这类事件，订阅了也不会收到多余调用——只连个人号时这部分功能自动"休眠"，其余功能不受影响（详见 FAQ Q9）。

**探测官方 bot 是否在线**（适合在指令入口给用户友好提示，而不是让按钮功能静默失效）：

```python
def has_official_online() -> bool:
    return any(
        a.adapter_type == "qqofficial" and a.is_connected
        for a in lumen.QClient.connected()
    )

# 用法：官方 bot 不在线时，按钮类指令给出明确提示
def on_group_message(pack, reply):
    if pack.get("raw_message") == "签到按钮":
        if not has_official_online():
            reply("该功能需要 QQ 官方机器人在线，当前仅个人号接入")
            return
        ...
```

---

# 第四部分：常见问题与排错（FAQ）

**Q1：机器人连上了，但收不到群消息？**
→ 检查三点：① 群消息属于默认订阅（1<<25），应该能收到；② `main_group` 配置的 group_openid 是否正确（日志里机器人入群时会打印）；③ WebUI 适配器卡片是否 enabled。

**Q2：配置了 extra_intents 后网关连不上了？**
→ 大概率订阅了**没有权限**的位（最常见是 `1<<1` 频道成员）。逐位排查：先把 extra_intents 改回 0 确认能连，再一个位一个位地加。

**Q3：机器人过一会儿就不回消息了？**
→ 被动凭据用完了（群 5 次/单聊 4 次，且有时间窗），之后的回复要走主动额度；主动额度用尽会收到 22009，消息进补发栈，等下次有人说话时补发。这是官方平台的硬限制，不是 bug。可观察日志中的 `active_queued` / `active_flushed` 条目理解流程。

**Q4：日志出现 "收到未枚举的官方事件 XXX"？**
→ 官方新增了事件（本适配器的兜底机制已自动转发给子插件，订阅名 `notice.xxx小写`）。这是 DEBUG 级日志，无害；也可以反馈给开发者补充正式映射。

**Q5：子插件订阅了 `notice.interaction_create` 但没反应？**
→ 互动事件需要订阅位 `1<<26`。确认 `extra_intents` 已包含 67108864，且机器人在开放平台侧有互动组件权限。

**Q6：`runcmdEx` 卡死/超时？**
→ 不要在游戏主线程调用它（文档 3.4.4 有警告）。在事件回调（工作线程）里用是安全的。

**Q7：怎么区分"机器人自己入群"和"普通成员入群"？**
→ 都是 `notice.group_increase`。机器人自己入群时 `user_id == self_id`（即 AppID）；普通成员入群（需 1<<24）时 `user_id` 是成员 openid，且事件带 `raw` 原文。

**Q8：想给官方机器人的群发定时消息（如开服公告）？**
→ 直接 `send_group_msg` 即可。没有被动凭据时会走主动额度；被拒会自动进补发栈，下次群里有人说话时借额度补发。

**Q9：子插件大部分功能用普通 OneBot 事件，只有一小部分订阅了官方扩展事件。只连个人号时，整个插件会失效吗？**
→ **不会，只有那一小部分功能"休眠"。** `lumen.on()` 注册回调时不校验"是否存在能产生该事件的适配器"——只连个人号时，标准事件（`message.*` / `notice.group_increase` 等）照常触发，官方扩展事件（`notice.interaction_create` 等）的回调只是永远不被调用：不报错、不告警、不影响其他回调。等官方 bot 接入后自动"唤醒"。同一子插件可放心混用两域事件，零成本。

---

# 第五部分：术语表

| 术语 | 解释 |
|---|---|
| **适配器（Adapter）** | LumenBridge 与某种聊天平台连接方式的翻译官 |
| **域（domain）** | 事件来源体系标记：qq（个人号）/ official（官方群聊·单聊）/ guild（频道） |
| **OneBot v11** | 社区制定的 QQ 机器人协议标准，LumenBridge 内部统一事件格式 |
| **事件包（pack）** | OneBot 格式的事件字典，子插件回调的第一个参数 |
| **AppID / AppSecret** | QQ 开放平台发放的机器人身份凭证 |
| **openid**（group_/member_/user_） | 官方体系下群/成员/用户的字符串标识，与 QQ 号无对应关系 |
| **网关（Gateway）** | 腾讯官方推送事件的 WebSocket 通道 |
| **Intents（订阅位）** | 用二进制位告诉网关"我要订阅哪些事件"的机制 |
| **被动回复** | 收到消息后的回复，带 msg_id 凭据，额度宽松（群 5 次/单聊 4 次） |
| **主动消息** | 机器人主动发起，月度额度限制，用户可关闭 |
| **msg_id / msg_seq** | 被动回复凭据对：同一 msg_id 配递增 seq 可多次回复 |
| **凭据池（懒池）** | 缓存多条消息的 msg_id 并智能分配回复额度的机制 |
| **补发栈（AtoP）** | 主动消息被拒后暂存、借被动额度补发的机制 |
| **event_id 凭据** | 机器人入群事件自带的可回复凭据（30 分钟，不耗主动额度） |
| **22009** | 官方错误码：主动消息被拒 |
| **扩展事件** | OneBot 无对应语义的官方事件，以 `notice.小写事件名` 原样转发，raw 保留原始载荷 |
| **子插件（Subplugin）** | 放在 subplugins/ 目录、通过 lumen 上下文获得能力的小插件 |
| **lumen.json** | 子插件的清单文件 |
| **懒池（Lazy Pool）** | Gensokyo 项目首创的凭据分配策略：优先消耗未用过的最新凭据 |

---

## 附录：源码导航

| 想看/想改什么 | 去哪里 |
|---|---|
| 事件映射规则（某事件翻译成什么） | [translate.py](../src/endstone_lumenbridge/onebot/qqofficial/translate.py) `on_dispatch` |
| 某常量的值（窗口时长/次数/错误码） | [constants.py](../src/endstone_lumenbridge/onebot/qqofficial/constants.py) |
| 凭据池/补发栈逻辑 | [credentials.py](../src/endstone_lumenbridge/onebot/qqofficial/credentials.py) |
| 重试策略/富媒体上传 | [sender.py](../src/endstone_lumenbridge/onebot/qqofficial/sender.py) |
| 网关连接/鉴权 | [qqofficial_adapter.py](../src/endstone_lumenbridge/onebot/qqofficial_adapter.py) |
| 适配器配置校验 | [connections.py](../src/endstone_lumenbridge/connections.py) `_validate_adapter` |
| 子插件上下文 API | [context.py](../src/endstone_lumenbridge/subplugin/context.py) |
| 子插件加载器 | [loader.py](../src/endstone_lumenbridge/subplugin/loader.py) |
| 事件分层派发 | [dispatcher.py](../src/endstone_lumenbridge/onebot/dispatcher.py) |
| 官方协议文档 | https://bot.q.qq.com/wiki/develop/api-v2/ |

---

*文档完。如有疑问请结合源码注释阅读，所有模块均有详尽的中文 docstring。*
