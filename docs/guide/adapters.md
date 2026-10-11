# 连接与适配器

LumenBridge 通过**适配器卡片**对接 QQ。每张卡片是一份独立的身份配置：机器人账号、管理员、群列表、群服互通设置互不干扰，支持多张卡片同时在线并行工作。

- 支持三种类型：**OneBot（个人号）**、**AstrBot**、**QQ 官方机器人**
- 支持任意数量的适配器并行连接，各自独立收发消息
- 配置存放在 `plugins/lumenbridge/connections/` 目录，与主配置分离

## 适配器类型

### OneBot（`websocket`）

对接实现了 OneBot v11 协议的 QQ 协议端（NapCat、Lagrange、LLOneBot 等）。支持两种连接方向：

| 方向 | `ws_type` | 说明 |
|------|-----------|------|
| 正向 WS | `0` | LumenBridge 作为 **WS 客户端**主动连接协议端的 `target` 地址（`ws://` 或 `wss://`） |
| 反向 WS | `1` | LumenBridge 作为 **WS 服务端**监听 `listen_host:listen_port`，等待协议端来连 |

 NapCat 侧通常配置「WebSocket 服务器」（即 LumenBridge 反向监听），或「WebSocket 客户端」（即 LumenBridge 正向连接），二选一即可。

### AstrBot（`astrbot`）

对接 AstrBot 平台，同样走 OneBot v11 消息段，连接参数与 OneBot 相同（`ws_type` / `target` / `listen_port` / `access_token`），默认监听端口 `6200`。

### QQ 官方机器人（`qqofficial`）

对接 QQ 开放平台的**官方机器人**（q.qq.com）。不经过任何协议端，由 LumenBridge 直接实现官方 WebSocket 网关协议，身份从 QQ 号换成 `AppID` + `AppSecret`，群号换成 `group_openid`。

官方机器人无需挂第三方协议端，账号安全性更高；但受官方接口限制：群聊场景必须 @ 机器人才有消息事件、主动消息有额度限制。详见[QQ 官方机器人](../development/qqofficial-dev.md)。

## 卡片字段详解

### 公共字段（三种类型通用）

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | string | 卡片唯一标识，创建时自动生成（如 `ws_a1b2c3d4`），不可修改 |
| `type` | string | 卡片类型：`websocket` / `astrbot` / `qqofficial`，创建后不可改 |
| `name` | string | 卡片显示名（1–64 字符），全局限重，重名自动追加序号 |
| `enabled` | bool | 是否启用；WebUI 新建的卡片默认启用（连接信息不完整时不会实际建连） |
| `bot_qq` | int | 机器人自身号码。个人号域填 QQ 号；官方域填机器人在管理端的编号（用于日志显示，鉴权不依赖它） |
| `admin_qq` | list | 管理员列表。个人号域为 QQ 号；官方域为**成员 openid**（可在群内 @ 用户执行 `/添加管理员` 自动写入）。最多 100 个 |
| `main_group` | list | 绑定的群列表。个人号域为群号；官方域为 **group_openid**（逗号分隔）。最多 100 个。`regex_engine.only_on_main = true` 时，列表中第一个群视为「主群」 |
| `sync` | object | 群服互通配置（消息格式、转发开关），详见[群服互通与聊天](chat.md#sync-全部字段) |

### WebSocket / AstrBot 专属字段

| 字段 | 类型 | 说明 |
|------|------|------|
| `ws_type` | int | `0` 正向（客户端）/ `1` 反向（服务端） |
| `target` | string | 正向连接的目标地址，必须以 `ws://` 或 `wss://` 开头 |
| `listen_host` | string | 反向模式的监听地址，默认 `0.0.0.0` |
| `listen_port` | int | 反向模式的监听端口（1–65535），反向适配器之间**不允许端口冲突** |
| `access_token` | string | OneBot access token，留空表示不鉴权 |

### QQ 官方专属字段

| 字段 | 类型 | 说明 |
|------|------|------|
| `app_id` | string | 机器人 AppID（≤32 位字母数字） |
| `app_secret` | string | 机器人 AppSecret（≤128 字符）。WebUI 中以等长 `*` 掩码显示，提交掩码自动保留原值 |
| `sandbox` | bool | 是否连接沙箱环境 |
| `suppress_connection_log` | bool | 静默连接/断连等高频运行日志（默认 `true` 防刷屏）；关闭后打印全部日志便于排障 |
| `connect_interval` | int | 两次网关连接尝试的最小间隔（毫秒，0–86400000），`0` 表示按指数退避自动重连 |
| `extra_intents` | int | 附加事件订阅位（按位或叠加到默认 Intents）。填非法位会被官方网关拒绝连接 |

`extra_intents` 位定义：

| 位 | 事件 |
|----|------|
| `1 << 0` | 频道变更 |
| `1 << 1` | 频道成员 |
| `1 << 3` | 语音房 |
| `1 << 9` | 私域频道消息 |
| `1 << 10` | 表情表态 |
| `1 << 24` | 群成员进退群 |
| `1 << 26` | 互动 |
| `1 << 27` | 审核 |
| `1 << 28` | 论坛 |

## 存储布局

适配器卡片按类型分文件存放，写入均为原子替换（临时文件 + `os.replace`）：

```
plugins/lumenbridge/
├── config.json              # 主配置
├── connections/
│   ├── websocket.json       # 所有 OneBot 卡片
│   ├── qqofficial.json      # 所有 QQ 官方卡片
│   └── astrbot.json         # 所有 AstrBot 卡片
└── data/
```

每个文件格式为 `{"version": 1, "adapters": [...]}`。

**兼容旧版**：旧版单文件 `connections.json` 仍然兼容——目录不存在时自动回退读取，下次写盘自动切换为新结构并把旧文件改名为 `connections.json.migrated`。旧版 `config.json` 中的 `connection` / `admin_qq` / `main_group` / `sync` 字段首次加载时会自动迁移进首张 WebSocket 卡片并自动启用。

**损坏自保护**：存储文件损坏（JSON 语法错误）时，坏文件自动备份为 `.json.bak`，按空列表处理；加载时校验不通过的卡片仅告警跳过（同样先备份来源文件），不影响其余卡片。

## 群绑定密钥

把群号/`group_openid` 手动填进配置容易出错，LumenBridge 提供**群绑定密钥**机制免手填：

1. 在 WebUI 适配器编辑页点击「群绑定」，为该适配器签发一次性密钥（12 位十六进制，如 `a3f9c2e81b04`）。
2. 管理员在目标群发送：

   ```
   /lumen绑定 a3f9c2e81b04
   ```

   （@ 与不 @ 机器人均可；繁体 `/lumen綁定`、英文 `/lumen bind` 同样接受）
3. 验证通过后，当前群与发送者自动写入该适配器的身份配置（群列表 + 管理员列表，自动去重）。

安全边界：

- 密钥**只存内存不落盘**，进程重启自然清空
- **一次性消费**，绑定成功即销毁；比对采用 `hmac.compare_digest` 防时序侧信道
- **5 分钟有效期**，到期自动销毁；签发新钥立即焚毁旧钥
- 比对失败**不会**销毁当前密钥（打错字不连带焚毁有效密钥）

## 批量添加管理员

群内发送：

```
/添加管理员 @甲 @乙 @丙
```

（繁体 `/添加管理員`、英文 `/addadmin` 同样接受）

- 发送者必须**已是该适配器的管理员**，普通群员无法自我提权
- 被 @ 的用户写入**来源适配器**的管理员列表（自动去重）：个人号域写入 QQ 号，官方域写入成员 openid
- 写入即时生效，无需重载连接；上限与合法性由配置层校验，超限时报错原样回执

## 多适配器并行与双域路由

多张卡片可以同时启用，事件按来源适配器隔离处理：

- 每条入站消息都会带上 `_lumen_adapter_id` 标记，所有响应（回群、白名单、正则规则动作）都走**来源适配器**回发
- 事件名带域后缀：个人号域为 `message.group.normal` / `message.private`，官方域事件由翻译层统一转为 OneBot v11 形态后同样进入个人号域通道（频道事件除外，走 `guild` 域隔离）。子插件无需关心消息来自哪种适配器
- 群/管理员判定使用**全适配器并集**（内部有 O(1) 缓存）：任意一个适配器绑定的群都算「已绑定群」，任何适配器的管理员都拥有管理员权限
- 玩家进服/退服/死亡等服务器事件会**广播给所有启用的适配器**的主群

## 配置变更

三种方式修改适配器配置：

1. **WebUI**（推荐）：可视化增删改卡片、签发群绑定密钥，密钥掩码自动回填。
2. **游戏内命令**：`/lumen` 系列命令，详见[命令系统](commands.md)。
3. **手动编辑**：直接改 `connections/*.json`。注意与 `config.json` 不同，适配器文件**没有**外部修改热检测，手动改完需在 WebUI 点击重载连接或重启服务器。

校验失败的常见提示：

| 提示 | 原因 |
|------|------|
| `adapter.type 只能为 websocket、astrbot 或 qqofficial` | 类型字段拼写错误 |
| `adapter.target 必须是有效的 ws:// 或 wss:// 地址` | 正向连接地址缺协议头 |
| `adapter.listen_port 必须位于 1 至 65535 之间` | 端口越界 |
| `监听端口与适配器「xx」冲突` | 两个反向 WS 适配器用了同一端口 |
| `adapter.main_group 中包含无效的 openid` | 官方卡片里填了 QQ 号而非 group_openid |
