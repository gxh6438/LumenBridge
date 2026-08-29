# LumenBridge 使用文档

> 版本 1.0.0 · MIT License · 面向 Endstone（Minecraft 基岩版专用服务器插件框架）

---

## 目录

- [开篇介绍](#开篇介绍)
- [环境要求与安装](#环境要求与安装)
- [快速开始](#快速开始)
- [连接类型详解](#连接类型详解)
- [群服互通配置](#群服互通配置)
- [Web 管理面板](#web-管理面板)
- [白名单功能](#白名单功能)
- [正则触发引擎](#正则触发引擎)
- [游戏内命令](#游戏内命令)
- [多适配器与双域路由](#多适配器与双域路由)
- [QQ 官方机器人](#qq-官方机器人)
- [AstrBot 全平台中继](#astrbot-全平台中继)
- [国际化](#国际化)
- [插件市场](#插件市场)
- [插件市场配置项](#插件市场配置项)
- [pip 包管理配置项](#pip-包管理配置项)
- [背景图配置](#背景图配置)
- [更新检查配置](#更新检查配置)
- [热重载](#热重载)
- [常见问题](#常见问题)

---

## 开篇介绍

LumenBridge（流明桥）是一款面向 Endstone 框架的 QQ 群服互通插件，用纯 Python 编写，运行在 Minecraft 基岩版专用服务器（BDS）之上。它把游戏聊天与 QQ 群消息打通，让管理员不用进游戏就能在群里管理服务器，让玩家不用加群就能收到进退服、死亡和开关服通知。除了基本的消息同步，它还提供白名单自动绑定、可编程的正则触发引擎、可视化的 Web 管理面板、子插件体系和在线插件市场，几乎覆盖了一个群服社区日常运营的全部需求。

在协议层，LumenBridge 同时支持三种连接方式：通过 OneBot v11 协议直连 NapCat、Lagrange、LLOneBot、go-cqhttp 等个人号机器人框架；通过 QQ 开放平台 API 接入 QQ 官方机器人；以及通过 astrbot_plugin_lumenbridge 扩展到 Telegram、Discord、微信、飞书等全平台。三种适配器可以同时运行，各自维护独立的管理员、群列表和互通配置，互不干扰。整个插件无任何外部 pip 依赖（websockets 库已内置），放进 plugins 目录即可使用。

---

## 环境要求与安装

### 环境要求

LumenBridge 的运行环境非常轻量，只需要满足以下条件即可：

| 项目 | 要求 | 说明 |
|------|------|------|
| Endstone | 0.11 及以上 | Minecraft 基岩版专用服务器插件框架 |
| Python | 3.10 及以上 | Endstone 内置，通常无需单独安装 |
| 协议端 | 任选其一 | NapCat / Lagrange / LLOneBot / go-cqhttp，或 QQ 官方机器人凭据 |

不需要安装任何额外的 Python 包。插件用到的 `websockets` 库已经打包在 whl 文件内部，不会与你服务器上已有的包产生冲突。如果你的网络环境需要代理，LumenBridge 在访问插件市场、检查更新和安装 pip 依赖时会自动尊重系统代理变量。

### 安装方式

安装过程只需要两步：

1. 下载 `endstone_lumenbridge-1.0.0-py3-none-any.whl` 文件。
2. 把这个 whl 文件放进 BDS 服务器的 `plugins/` 目录。

启动服务器时，Endstone 会自动加载这个 whl 并在 `plugins/lumenbridge/` 目录下生成默认配置文件。首次启动后你会在控制台看到 LumenBridge 的启动横幅和 WebUI 地址。整个过程不需要手动解压、不需要 pip install、不需要修改任何系统环境变量。

```text
plugins/
└── endstone_lumenbridge-1.0.0-py3-none-any.whl
```

---

## 快速开始

下面以 NapCat 正向 WebSocket（端口 3001）为例，三步完成从零到互通的全部流程。

### 第一步：部署协议端

先在你打算运行 QQ 机器人的机器上启动 NapCat 并登录机器人 QQ 号。进入 NapCat 的网络配置页面，添加一个「正向 WebSocket 服务端」，把监听端口设为 `3001`。如果你使用 Access Token 鉴权，记下这个 Token，稍后在 LumenBridge 里也要填一样的。确认 NapCat 显示机器人在线、WebSocket 服务已启动后，这一步就完成了。

如果你用的是 Lagrange 或 LLOneBot，配置方式类似，只要保证有一个正向 WebSocket 端口对外开放即可。go-cqhttp 虽然已经停止更新，但仍然兼容，配置文件里的 `ws` 节点同样适用。

### 第二步：启动服务器

把 whl 文件放进 `plugins/` 目录后启动 Endstone 服务器。插件会自动创建数据目录和默认配置：

```text
plugins/lumenbridge/
├── config.json          # 基础配置（语言、白名单、正则引擎、WebUI 等）
├── connections/         # 连接配置（适配器卡片，按类型分文件）
│   ├── websocket.json   #   QQ 个人号（OneBot WebSocket）卡片
│   ├── qqofficial.json  #   QQ 官方机器人卡片
│   └── astrbot.json     #   AstrBot 中继卡片
└── data/                # 运行数据
    ├── rules.json       #   正则规则库
    ├── whitelist.json   #   白名单绑定数据（个人号域）
    └── whitelist_official.json  # 白名单绑定数据（官方机器人域）
```

从旧版本升级的用户：插件首次写盘时会把旧的单文件 `connections.json` 自动切换为新结构（原文件改名为 `connections.json.migrated`，连接不中断）。如需把 `rules.json`、`whitelist.json` 等旧版平铺在根目录的数据文件也搬进 `data/`，请使用独立迁移脚本——把 `scripts/migrate_storage.py` 复制到 `plugins/lumenbridge/` 目录下，先运行 `python migrate_storage.py --dry` 预览，确认无误后去掉 `--dry` 执行迁移。迁移前会自动备份旧文件到 `legacy_backup/`。

此时控制台会输出一段启动横幅，并打印 WebUI 的访问地址和登录密码。默认情况下插件会预置两张适配器卡片（WebSocket 和 QQ 官方机器人），都处于未启用状态，等待你在下一步填写信息后开启。

### 第三步：配置连接

打开浏览器访问 WebUI（默认地址 `http://127.0.0.1:8765`）。登录密码在服务器控制台输出中可以找到——当 `config.json` 里 `webui.password` 的值是 `*` 时，插件会在每次启动时随机生成一个密码并打印出来：

```text
[LumenBridge] WebUI 已启动: http://127.0.0.1:8765
[LumenBridge] WebUI 登录密码: aB3xK9mN
```

进入 WebUI 后，打开「配置 → 连接配置」页签，找到预置的 WebSocket 卡片。点右上角的齿轮图标展开设置：把连接方式选为「正向 WS」，目标地址填 `ws://127.0.0.1:3001`，展开「权限与身份设置」填入机器人 QQ 号、管理员 QQ 号和主群号，最后打开「启用此适配器」并保存。返回服务器看到日志出现「适配器 WebSocket 已启动」并且卡片右下角显示绿色「已连接」时，说明打通成功。这时在群里发一条消息，游戏内就会出现对应的群聊内容；在游戏内说一句话，群里也能收到。

---

## 连接类型详解

LumenBridge 支持三种适配器类型：`websocket`（QQ 个人号直连协议端）、`astrbot`（AstrBot 全平台中继）、`qqofficial`（QQ 官方机器人）。连接配置按类型分文件存放在 `plugins/lumenbridge/connections/` 目录下（`websocket.json` / `qqofficial.json` / `astrbot.json`），以「适配器卡片」的形式管理。你可以在 WebUI 的连接配置页直接增删改这些卡片，也可以手动编辑对应的 JSON 文件。

每种适配器卡片都有自己专属的字段集，下面分别说明。

### websocket 类型

这是最常用的类型，用来直连基于 OneBot v11 协议的 QQ 个人号机器人框架（NapCat、Lagrange、LLOneBot、go-cqhttp）。

| 字段名 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| id | string | 自动生成 | 卡片唯一标识，字母数字下划线短横，最长 64 字符 |
| type | string | `websocket` | 适配器类型，固定为 `websocket` |
| name | string | `WebSocket` | 卡片显示名称，1 至 64 字符，重名自动追加序号 |
| enabled | bool | false | 是否启用此适配器，未启用的卡片不参与连接 |
| ws_type | int | 0 | 连接方向：0 = 正向 WS（本插件主动连对方），1 = 反向 WS（对方连过来） |
| target | string | `""` | 正向 WS 时对方的 WS 地址，如 `ws://127.0.0.1:3001` |
| listen_host | string | `0.0.0.0` | 反向 WS 时的本地监听地址 |
| listen_port | int | 3002 | 反向 WS 时的本地监听端口，多卡片之间不能冲突 |
| access_token | string | `""` | 鉴权 Token，需与对端一致，可留空 |
| bot_qq | int | 0 | 机器人 QQ 号，用于识别自身消息避免自回复 |
| admin_qq | list/int | `[]` | 该适配器的管理员 QQ 列表 |
| main_group | list/int | `""` | 该适配器生效的群号列表，支持逗号分隔字符串 |
| sync | object | 见后文 | 该适配器独立的群服互通配置 |

配置示例（正向 WS 连接 NapCat）：

```json
{
    "id": "ws_main",
    "type": "websocket",
    "name": "主机器人",
    "enabled": true,
    "ws_type": 0,
    "target": "ws://127.0.0.1:3001",
    "listen_host": "0.0.0.0",
    "listen_port": 3002,
    "access_token": "",
    "bot_qq": 10001,
    "admin_qq": [123456789],
    "main_group": [987654321],
    "sync": {
        "chat_to_server_enable": true,
        "chat_to_group_enable": true,
        "join_to_group_enable": true,
        "leave_to_group_enable": true,
        "death_to_group_enable": true,
        "server_start_to_group": true,
        "server_stop_to_group": true,
        "chat_to_server_format": "[群聊] %s: %s",
        "chat_to_group_format": "[玩家] %s: %s",
        "max_message_length": 256
    }
}
```

反向 WS 的配置只需要把 `ws_type` 改为 `1`，然后把 NapCat 配成反向 WebSocket 客户端、地址指向 `ws://<BDS机器IP>:3002` 即可，`target` 字段在反向模式下留空。

### astrbot 类型

AstrBot 类型用来连接 AstrBot 插件端（`astrbot_plugin_lumenbridge`），把群服互通扩展到 Telegram、Discord、微信、飞书等 AstrBot 支持的全部平台。它的字段集与 websocket 完全一致，只是默认监听端口不同。

| 字段名 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| id | string | 自动生成 | 卡片唯一标识 |
| type | string | `astrbot` | 适配器类型，固定为 `astrbot` |
| name | string | `AstrBot` | 卡片显示名称 |
| enabled | bool | false | 是否启用 |
| ws_type | int | 0 | 连接方向：0 = 正向，1 = 反向 |
| target | string | `""` | 正向 WS 时 AstrBot 插件端的地址 |
| listen_host | string | `0.0.0.0` | 反向 WS 监听地址 |
| listen_port | int | 6200 | 反向 WS 监听端口，默认 6200 |
| access_token | string | `""` | 鉴权 Token |
| bot_qq | int | 0 | 虚拟机器人号，填任意值即可 |
| admin_qq | list | `[]` | 管理员列表 |
| main_group | list | `""` | 无需填写，会话在 AstrBot 插件端配置 |
| sync | object | 见后文 | 群服互通配置 |

配置示例（正向 WS 连接 AstrBot 插件端）：

```json
{
    "id": "astrbot_main",
    "type": "astrbot",
    "name": "AstrBot",
    "enabled": true,
    "ws_type": 0,
    "target": "ws://127.0.0.1:6200",
    "listen_host": "0.0.0.0",
    "listen_port": 6200,
    "access_token": "my_secret",
    "bot_qq": 10000,
    "admin_qq": [],
    "main_group": "",
    "sync": {}
}
```

AstrBot 卡片不需要在 `main_group` 里填群号，因为实际的桥接会话在 AstrBot 插件端的 `umo_list` 里配置，LumenBridge 会自动接收到虚拟群号。

### qqofficial 类型

QQ 官方机器人类型通过 QQ 开放平台 API 接入官方机器人，不需要部署任何第三方协议端。它的字段集与前两种不同，使用 AppID 和 AppSecret 鉴权，管理员和群都用 openid 而非数字 QQ 号。

| 字段名 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| id | string | 自动生成 | 卡片唯一标识 |
| type | string | `qqofficial` | 适配器类型，固定为 `qqofficial` |
| name | string | `QQ 官方机器人` | 卡片显示名称 |
| enabled | bool | false | 是否启用 |
| app_id | string | `""` | QQ 开放平台 AppID，不超过 32 位字母数字 |
| app_secret | string | `""` | QQ 开放平台 AppSecret，最长 128 字符 |
| sandbox | bool | false | 是否使用沙箱环境（沙箱域名 sandbox.api.bot.qq.com） |
| suppress_connection_log | bool | true | 后台静默日志（默认开启）：开启时不打印连接/断连/重连、无凭据降级主动发送、重试与补发提示等运行类日志；关闭时打印全部日志便于排障。发送失败等异常日志不受影响 |
| connect_interval | int | 60000 | 连接间隔（毫秒），两次网关连接尝试的最小等待，0 表示指数退避自动重连 |
| extra_intents | int | 0 | 附加事件订阅位，按位或叠加到默认 Intents |
| bot_qq | int | 0 | 机器人标识，通常填 0 |
| admin_qq | list | `[]` | 管理员 openid 列表（字符串标识） |
| main_group | list | `""` | 主群 group_openid 列表（逗号分隔字符串） |
| sync | object | 见后文 | 群服互通配置 |

配置示例：

```json
{
    "id": "qo_main",
    "type": "qqofficial",
    "name": "官方机器人",
    "enabled": true,
    "app_id": "102345678",
    "app_secret": "your_app_secret_here",
    "sandbox": false,
    "suppress_connection_log": true,
    "connect_interval": 60000,
    "extra_intents": 0,
    "bot_qq": 0,
    "admin_qq": ["ABCDEFG123456"],
    "main_group": "GROUP_OPENID_XXX",
    "sync": {}
}
```

如果你还不知道自己的 openid 或 group_openid，可以在群里发送 `/get openid` 指令，LumenBridge 会把当前群和发送者的标识回复出来，方便你抄录进配置。

---

## 群服互通配置

每个适配器卡片都拥有独立的 `sync` 配置块，用来控制这个适配器对应的群与服务器之间的消息同步行为。这意味着如果你同时运行两个机器人，可以给它们分别设置不同的消息格式和开关。入站消息（群→游戏）按消息来源适配器的配置渲染，出站广播（游戏→群）则按每个适配器各自的配置与群列表发送。

### 字段说明

| 字段名 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| chat_to_server_enable | bool | true | 是否把群消息转发到游戏内 |
| chat_to_group_enable | bool | true | 是否把游戏内聊天转发到群 |
| join_to_group_enable | bool | true | 玩家进服时是否通知群 |
| leave_to_group_enable | bool | true | 玩家退服时是否通知群 |
| death_to_group_enable | bool | true | 玩家死亡时是否通知群 |
| server_start_to_group | bool | true | 服务器启动时是否通知群 |
| server_stop_to_group | bool | true | 服务器关闭时是否通知群 |
| text_format | string | `%s` | 纯文本消息段的渲染格式 |
| face_format | string | `[表情]` | QQ 表情消息段的渲染格式 |
| image_format | string | `[图片]` | 图片消息段的渲染格式 |
| at_format | string | `@%s` | @ 消息段的渲染格式，`%s` 为被@的人 |
| reply_format | string | `[回复]` | 回复消息段的渲染格式 |
| forward_format | string | `[合并转发]` | 合并转发消息段的渲染格式 |
| join_format | string | `[玩家] %s 进服` | 玩家进服通知格式，`%s` 为玩家名 |
| leave_format | string | `[玩家] %s 退服` | 玩家退服通知格式，`%s` 为玩家名 |
| death_format | string | `[死亡] %s` | 玩家死亡通知格式，`%s` 为死亡信息 |
| chat_to_group_format | string | `[玩家] %s: %s` | 游戏聊天转发到群的格式，前 `%s` 为玩家名，后 `%s` 为消息 |
| chat_to_server_format | string | `[群聊] %s: %s` | 群消息转发到游戏的格式，前 `%s` 为昵称，后 `%s` 为消息 |
| server_start_format | string | `[服务器] 已启动` | 服务器启动通知格式 |
| server_stop_format | string | `[服务器] 已关闭` | 服务器关闭通知格式 |
| max_message_length | int | 256 | 单条转发消息的最大长度，超出截断，范围 1 至 4096 |

### 占位符用法

所有 `*_format` 字段都使用 `%s` 作为占位符。不同字段的占位符含义取决于上下文：

- `chat_to_group_format` 有两个 `%s`：第一个替换为玩家名，第二个替换为消息内容。
- `chat_to_server_format` 有两个 `%s`：第一个替换为发送者昵称，第二个替换为消息内容。
- `join_format`、`leave_format`、`death_format` 各有一个 `%s`，分别替换为玩家名、玩家名、死亡信息。
- `at_format` 有一个 `%s`，替换为被 @ 的人的 QQ 号或昵称。
- `text_format` 有一个 `%s`，替换为文本内容。

例如，想把群消息在游戏内显示得更醒目，可以这样改：

```json
"chat_to_server_format": "§b[QQ] §e%s §7: §f%s"
```

这样群聊消息在游戏内会显示为蓝色的 `[QQ]` 标签、黄色的昵称和白色的内容。如果你不希望玩家死亡时通知群，把 `death_to_group_enable` 改为 `false` 即可，无需删除 `death_format`。

### 完整示例

下面是一个开启了全部同步、使用了自定义格式的完整 sync 配置：

```json
"sync": {
    "chat_to_server_enable": true,
    "chat_to_group_enable": true,
    "join_to_group_enable": true,
    "leave_to_group_enable": true,
    "death_to_group_enable": true,
    "server_start_to_group": true,
    "server_stop_to_group": true,
    "text_format": "%s",
    "face_format": "[表情]",
    "image_format": "[图片]",
    "at_format": "@%s",
    "reply_format": "[回复]",
    "forward_format": "[合并转发]",
    "join_format": "§a[进服] §e%s §a加入了游戏",
    "leave_format": "§c[退服] §e%s §c离开了游戏",
    "death_format": "§4[死亡] %s",
    "chat_to_group_format": "[游戏] %s: %s",
    "chat_to_server_format": "§b[群聊] §e%s§r: %s",
    "server_start_format": "§a服务器已启动",
    "server_stop_format": "§c服务器已关闭",
    "max_message_length": 256
}
```

---

## Web 管理面板

LumenBridge 内置了一个完整的 Web 管理面板，通过浏览器就能管理全部功能，不用反复改文件再重启。面板采用响应式布局，手机竖屏会自动切换为底部导航栏，桌面端则是侧边导航。

### 访问方式

浏览器打开 `http://服务器IP:8765`，输入密码登录。密码的默认值是 `*`，表示每次启动服务器时随机生成并打印到控制台。你也可以在 `config.json` 的 `webui.password` 字段里固定一个密码。出于安全考虑，建议把监听地址保持在 `127.0.0.1`，需要外网访问时配合反向代理和 IP 白名单使用。

### WebUI 配置项

| 字段名 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| enable | bool | true | 是否启用 Web 管理面板 |
| host | string | `127.0.0.1` | 监听地址，`0.0.0.0` 允许外网访问（注意安全） |
| port | int | 8765 | WebUI 端口，范围 1 至 65535 |
| password | string | `*` | 登录密码，`*` 表示启动时随机生成并打印到控制台 |
| secret | string | `""` | Token 签名密钥，留空时自动生成 |

### 功能页面

面板包含以下页面：

**总览页**。展示所有适配器的连接状态（已连接/已断开/已禁用）、规则数、白名单数、子插件数和 WebUI 地址。页面右侧是服务器资源仪表盘，包含 CPU 占用和内存占用的环形图表，每 3 秒自动刷新一次。CPU 占用达到 70% 时仪表盘变橙色，达到 90% 变红色，方便你一眼发现服务器压力。仪表盘只在总览页激活时轮询，切到其他页面会自动停止，不浪费资源。

**配置页**。分为「基础配置」和「连接配置」两个子页签。基础配置可以直接在线编辑 `config.json` 的全部内容，保存后自动热重载，不需要重启服务器。连接配置以适配器卡片的形式管理全部连接，每张卡片可以展开设置连接方式、身份权限和群服互通，增删改即时生效。

**白名单页**。以表格形式展示当前所有 QQ 与 XboxID 的绑定关系，支持搜索、手动添加和删除。手动添加的绑定会自动同步到游戏白名单。

**正则规则页**。以列表形式展示 `rules.json` 里的全部规则，支持在线增删改。每条规则可以设置事件类型、触发条件、动作序列，内置 JSON 编辑器方便高级编辑。

**子插件页**。展示已安装的子插件列表，支持上传 zip 包安装、启用、禁用、重载、卸载。子插件通过 `registerPage` 注册的自定义页面也会出现在导航栏里。

**插件市场页**。在线浏览、搜索和安装市场里的子插件，安装时自动校验 SHA-256 完整性。详见后文「插件市场」章节。

**pip 管理页**。查看服务器当前已安装的 Python 包列表，安装新的依赖。安装操作会尊重 `pip.index_url` 配置的镜像源。

---

## 白名单功能

白名单功能让玩家在 QQ 群里发一条指令就能完成游戏白名单的绑定，不用管理员手动一个个加。绑定关系存储在 `plugins/lumenbridge/data/whitelist.json`（个人号域）与 `data/whitelist_official.json`（官方机器人域）里，可以在 WebUI 的白名单页可视化管理。

### 绑定与解绑指令

玩家在主群里发送以下指令即可完成操作：

| 指令 | 说明 |
|------|------|
| `绑定白名单<XboxID>` | 把当前 QQ 绑定到指定 XboxID，自动执行游戏 `allowlist add` |
| `解绑白名单` | 解除当前 QQ 的绑定，自动从游戏白名单移除 |

例如玩家发送 `绑定白名单 Steve`，LumenBridge 会在游戏主线程执行 `allowlist add Steve`，成功后把 `QQ → Steve` 写入 `whitelist.json`，并在群里回复「绑定成功，你已绑定：Steve」。整个流程是事务性的：如果游戏侧命令执行失败，本地记录不会被修改，避免出现「提示成功但玩家仍被挡在门外」的情况。

如果 XboxID 里包含空格，需要用引号包裹，例如 `绑定白名单 "Player One"`。一个 QQ 只能绑定一个 XboxID，反之亦然，重复绑定会被拒绝。玩家退群时，如果开启了 `remove_on_leave`，LumenBridge 会自动解绑并从游戏白名单移除该玩家。

### 配置项

白名单功能在 `config.json` 的 `whitelist` 块中配置：

| 字段名 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| enable | bool | true | 是否启用白名单功能 |
| auto_add | bool | true | 绑定后是否自动执行游戏 `allowlist add`，关闭则只记录不进游戏 |
| bind_keyword | string | `绑定白名单` | 群内触发绑定的关键字 |
| unbind_keyword | string | `解绑白名单` | 群内触发解绑的关键字 |
| remove_on_leave | bool | true | 玩家退群时是否自动移除其游戏白名单 |

配置示例：

```json
"whitelist": {
    "enable": true,
    "auto_add": true,
    "bind_keyword": "绑定白名单",
    "unbind_keyword": "解绑白名单",
    "remove_on_leave": true
}
```

如果你想改用别的指令词，比如「绑定」和「解绑」，只需要修改 `bind_keyword` 和 `unbind_keyword` 两个字段即可。注意服务器需要先开启白名单（`allowlist on`）才能让绑定真正生效，否则 `allowlist add` 虽然执行了但玩家仍然能进服。

---

## 正则触发引擎

正则触发引擎是 LumenBridge 的核心可编程模块，它让你用一组规则来自动响应群消息和游戏事件。规则库存放在 `plugins/lumenbridge/data/rules.json` 里，每条规则定义一个触发条件和一个动作序列。引擎内置了 ReDoS 防护，会自动跳过可能导致灾难性回溯的危险正则表达式，避免恶意或失误的规则拖垮服务器。

### 规则结构

每条规则是一个 JSON 对象，结构如下：

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
        {"type": "reply", "params": "$result"}
    ],
    "block": true
}
```

| 字段名 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| id | string | 必填 | 规则唯一标识 |
| name | string | 必填 | 规则名称，用于展示 |
| enabled | bool | true | 是否启用此规则 |
| triggerType | string | `message` | 触发类型：`message` = 群消息触发，`event` = 事件触发 |
| pattern | string | `""` | 正则表达式（Python 语法），事件触发时空值表示匹配全部 |
| flags | string | `""` | 正则标志：`i` 忽略大小写 / `m` 多行 / `s` 单行 |
| eventType | string | `""` | 事件触发时的事件类型，如 `group.member_join` |
| conditions | list | `[]` | 附加条件数组，全部满足才执行动作 |
| actions | list | `[]` | 动作序列，按顺序执行 |
| block | bool | false | 命中后是否阻断后续规则 |

### 条件字段

条件数组里每个对象包含 `field`、`operator`、`value` 三个字段。支持的字段和取值如下：

| 条件字段 | 取值 | 说明 |
|----------|------|------|
| userRole | member / admin / owner | 用户角色，`admin` 表示超级管理员。`sparkadmin` 为向后兼容别名，等同于 `admin` |
| userId | QQ号/openid | 触发者的用户标识 |
| groupId | 群号 | 触发消息所在的群号 |

支持的 operator（运算符）：

| 运算符 | 说明 |
|--------|------|
| `==` | 等于 |
| `!=` | 不等于 |
| `includes` | 包含（子串匹配） |
| `matches` | 正则匹配 |

例如，限制只有管理员才能触发某条规则：

```json
"conditions": [
    {"field": "userRole", "operator": "==", "value": "admin"}
]
```

### 变量替换

动作的 `params` 字段支持变量替换，引擎会在执行前把变量替换为实际值：

| 变量 | 说明 |
|------|------|
| `$userId` | 触发者的 QQ 号（或官方域的 openid） |
| `$groupId` | 消息所在群号 |
| `$at` | 被 @ 的人的 QQ 号，无 @ 时为触发者自己 |
| `$nickname` | 触发者昵称 |
| `$result` | 上一个 `executeCommand` 动作的输出 |
| `$0` | 正则完整匹配 |
| `$1` `$2` ... `$n` | 正则捕获组，按顺序对应 |

### 动作类型

动作序列按顺序执行，每个动作是一个包含 `type` 和 `params` 的对象：

| 动作类型 | params 说明 | 作用 |
|----------|------------|------|
| reply | 文本内容 | 在当前群回复文本，支持变量替换 |
| sendGroup | `群号,文本` | 向指定群发送文本 |
| sendPrivate | `QQ号,文本` | 向指定用户发送私聊 |
| executeCommand | 命令字符串 | 在服务器执行命令，输出存入 `$result` |
| callPluginCommand | `命令名,参数1,参数2` | 调用子插件注册的自定义动作 |
| wait | 秒数 | 等待指定秒数后继续执行后续动作 |
| setVar | `变量名,值` | 设置自定义变量，供后续动作引用 |

### 内置默认规则

LumenBridge 首次启动时会生成四条内置规则：

| 规则 | 触发 | 作用 |
|------|------|------|
| 查服 | 群消息 `查服` | 执行 `/list` 并回复在线玩家列表 |
| 查白名单 | 群消息 `查白名单 @某人` | 查询被@者绑定的 XboxID |
| 管理员执行命令 | 群消息 `执行<命令>` | 仅管理员可用，执行任意服务器命令并回复结果 |
| 入群欢迎 | 新成员入群事件 | 自动 @ 新成员并发送欢迎语和绑定提示 |

### 引擎配置项

正则引擎在 `config.json` 的 `regex_engine` 块中配置：

| 字段名 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| enable | bool | true | 是否启用正则引擎 |
| only_on_main | bool | true | 是否仅在主群触发规则，关闭则所有群都触发 |
| admin_debug | bool | false | 调试模式，命中规则时在日志打印规则名 |
| command_timeout | float | 5.0 | `executeCommand` 动作的超时时间，范围 0.1 至 60 秒 |

配置示例：

```json
"regex_engine": {
    "enable": true,
    "only_on_main": true,
    "admin_debug": false,
    "command_timeout": 5.0
}
```

### 完整规则示例

下面是一个完整的规则示例：当管理员在群里发送「封禁 玩家名」时，执行游戏封禁命令并回复结果：

```json
{
    "id": "rule_ban_player",
    "name": "封禁玩家",
    "enabled": true,
    "triggerType": "message",
    "pattern": "^封禁\\s+(.+)",
    "flags": "",
    "eventType": "",
    "conditions": [
        {"field": "userRole", "operator": "==", "value": "admin"}
    ],
    "actions": [
        {"type": "executeCommand", "params": "ban $1"},
        {"type": "reply", "params": "已封禁玩家 $1\n执行结果：$result"}
    ],
    "block": true
}
```

这条规则用 `^封禁\s+(.+)` 匹配「封禁」加空格加玩家名，捕获组 `$1` 就是玩家名。条件限定只有管理员能触发，动作序列先执行游戏 `ban` 命令，再用 `$result` 把命令输出回复到群里。`block` 设为 true 表示命中后不再匹配后续规则。

事件触发的规则则不需要 pattern，靠 `eventType` 匹配。例如监听玩家进服事件并发送欢迎语：

```json
{
    "id": "rule_welcome_player",
    "name": "玩家进服欢迎",
    "enabled": true,
    "triggerType": "event",
    "pattern": "",
    "flags": "",
    "eventType": "server.player_join",
    "conditions": [],
    "actions": [
        {"type": "reply", "params": "$0 加入了服务器"}
    ],
    "block": false
}
```

事件规则可选的 `eventType` 包括：`group.member_join`（新成员入群）、`group.member_leave`（成员退群）、`server.player_join`（玩家进服）、`server.player_left`（玩家退服）、`server.player_chat`（玩家游戏内聊天）。

---

## 游戏内命令

LumenBridge 注册了一个 `/lumen` 主命令，在游戏内或服务器控制台都可以使用。命令的权限默认只开放给 OP（控制台天然拥有全部权限），可以通过配置放开给普通玩家。

### 命令一览

| 命令 | 说明 |
|------|------|
| `/lumen status` | 查看 LumenBridge 运行状态（适配器连接、主群、规则数、白名单数、子插件数、WebUI 地址） |
| `/lumen reload` | 热重载配置、规则库、子插件、连接适配器和 WebUI |
| `/lumen say <消息>` | 以服务器身份向所有主群发送消息 |
| `/lumen plugins` | 查看子插件列表与加载状态 |
| `/lumen pip install <包名>` | 安装 Python 依赖（可传子插件名自动安装其声明的依赖） |
| `/lumen pip list` | 查看已安装的 Python 包 |
| `/lumen pip uninstall <包名>` | 卸载 Python 包 |

### 命令权限配置

命令权限在 `config.json` 的 `commands` 块中配置。出于安全考虑，默认情况下游戏内玩家无法执行任何 `/lumen` 子命令，只有控制台和 OP 可以使用。

| 字段名 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| allow_in_game | bool | false | 是否允许游戏内玩家使用 `/lumen` 命令（总开关） |
| status.allow_player | bool | false | 是否允许非 OP 玩家执行 `/lumen status` |
| reload.allow_player | bool | false | 是否允许非 OP 玩家执行 `/lumen reload` |
| say.allow_player | bool | false | 是否允许非 OP 玩家执行 `/lumen say` |
| plugins.allow_player | bool | false | 是否允许非 OP 玩家执行 `/lumen plugins` |
| pip.allow_in_game | bool | false | 是否允许游戏内使用 `/lumen pip`（即使开启也仅限 OP） |
| pip.allow_player | bool | false | 是否允许非 OP 玩家使用 pip（实际仍受限） |

配置示例：

```json
"commands": {
    "allow_in_game": true,
    "status": {"allow_player": true},
    "reload": {"allow_player": false},
    "say": {"allow_player": false},
    "plugins": {"allow_player": true},
    "pip": {
        "allow_in_game": false,
        "allow_player": false
    }
}
```

上面的配置允许普通玩家查看状态和子插件列表，但重载、发言和 pip 管理仍然只有 OP 能用。`/lumen pip` 是一个特殊命令：即使 `allow_in_game` 和 `allow_player` 都设为 true，它也只接受 OP 执行，因为安装第三方包涉及服务器安全。

---

## 多适配器与双域路由

LumenBridge 的多适配器能力是它区别于普通群服插件的核心特性之一。你可以在同一台服务器上同时运行多个适配器，它们各自独立工作又互相协作。

### 多适配器并行

每个适配器卡片都有自己独立的管理员列表、群列表和群服互通配置。这意味着你可以同时连接两个 QQ 个人号机器人，各管各的群；或者一个个人号机器人加一个 QQ 官方机器人并行工作；甚至再加上一个 AstrBot 中继把消息桥接到 Telegram。只要反向 WS 的监听端口不冲突，适配器数量没有上限。

在 WebUI 的连接配置页点击「添加适配器」即可创建新卡片，选择类型后填写信息并启用。新创建的卡片默认启用状态，但在填写完整连接信息之前不会真正建连。多适配器场景下，游戏事件（进退服、死亡、开关服）会广播到所有已连接适配器的主群，玩家聊天也会同步到全部主群。

### 双域自动路由

当多个适配器同时在线时，LumenBridge 会在发送消息时自动判断目标属于哪个域。这套路由机制对用户完全透明，你不需要手动指定发给哪个适配器：

- **按目标标识类型路由**：openid 字符串（官方域）只发给 QQ 官方适配器，数字 QQ 号（个人号域）只发给个人号协议端适配器。
- **按群号路由**：如果某个群号明确属于某个适配器的主群列表，消息就只发给那个适配器。对于不属于任何适配器的未知群，消息会广播到全部已连接适配器，确保不遗漏。
- **查询类调用路由到主适配器**：像获取群列表、获取成员信息这类查询操作，会路由到主适配器（首个已连接的 WebSocket 实例，其次任意已连接实例，最后首个适配器）。

这种设计的好处是，你可以让个人号机器人负责日常群聊和游戏内管理，让官方机器人负责合规的消息推送，两者各司其职又互不干扰。子插件在调用 API 时也不用关心消息最终走哪条链路，适配器层会自动处理。

---

## QQ 官方机器人

QQ 官方机器人适配器通过 QQ 开放平台 API 直接接入，不需要部署任何第三方协议端。它适合需要合规运营、不愿意使用个人号协议端风险的场景。

### 前置准备

使用官方机器人前，你需要先在 QQ 开放平台（[q.qq.com](https://q.qq.com)）注册并创建一个机器人应用，获取到 AppID 和 AppSecret。这两个值填入适配器卡片的 `app_id` 和 `app_secret` 字段即可。如果你还没有上线，可以先用沙箱环境测试：把 `sandbox` 设为 true，请求会走沙箱域名 `sandbox.api.bot.qq.com`。

### 事件订阅

官方适配器原生支持 46 种网关事件，覆盖群消息、私聊、频道、群成员进退群、互动回调、消息审核、论坛、语音房、表情表态等全部官方事件类型。默认已经订阅了群聊消息、C2C 消息、群成员事件、互动事件等核心 Intents。

如果你需要订阅额外事件，通过 `extra_intents` 字段按位或叠加订阅位。常用的订阅位如下：

| 订阅位 | 值 | 事件类型 |
|--------|-----|---------|
| 频道变更 | 1 << 0 | GUILD_CREATE / UPDATE / DELETE |
| 频道成员 | 1 << 1 | GUILD_MEMBER_ADD / REMOVE / UPDATE（需官方开通权限） |
| 私域频道消息 | 1 << 9 | MESSAGE_CREATE / DELETE（仅私域机器人可订阅） |
| 表情表态 | 1 << 10 | MESSAGE_REACTION_ADD / REMOVE |
| 群成员进退群 | 1 << 24 | GROUP_MEMBER_ADD / REMOVE |
| 互动事件 | 1 << 26 | INTERACTION_CREATE |
| 消息审核 | 1 << 27 | MESSAGE_AUDIT_PASS / REJECT |
| 论坛事件 | 1 << 28 | FORUM_THREAD_CREATE 等 |
| 语音房 | 1 << 29 | AUDIO_START / FINISH / ON_MIC / OFF_MIC |

注意：没有权限的订阅位会导致网关拒连。如果机器人没有群成员权限而订阅了 1<<24，适配器在连续 Identify 失败后会自动摘除该位并降级重连，实现自愈。

### 连接日志与重连

官方网关在连接不稳定时可能频繁断连重连，产生大量日志。后台静默日志开关 `suppress_connection_log` **默认开启（true）**——连接/断连/重连、无被动回复凭据降级主动发送、发送重试与补发提示等运行类日志默认不打印，后台保持清爽。排查问题时把它设为 false，即可查看上述全部日志；发送失败等异常日志始终正常输出，不受此开关影响。

`connect_interval` 控制两次网关连接尝试之间的最小等待时间（毫秒），默认 60000（60 秒）。设为 0 则采用指数退避策略自动重连，从 2 秒起步逐步延长到 60 秒上限。

### 管理员与群标识

官方域使用 openid 而非数字 QQ 号来标识用户和群。因此官方适配器卡片的 `admin_qq` 填的是管理员的 openid（一串字母数字字符串），`main_group` 填的是群的 group_openid。如果你不知道自己的 openid，可以在群里发送 `/get openid` 指令查询。子插件在官方域收到的事件包里 `user_id` 和 `group_id` 也是 openid 格式，需要按域区分处理。

---

## AstrBot 全平台中继

AstrBot 是一个支持多平台（QQ、Telegram、Discord、微信、飞书等）的聊天机器人框架。LumenBridge 通过 `astrbot_plugin_lumenbridge` 这个 AstrBot 插件端与 AstrBot 对接，把群服互通的能力扩展到 AstrBot 支持的全部平台。消息流如下：

```text
游戏 ⇄ LumenBridge ⇄ (OneBot v11 / WS) ⇄ astrbot_plugin_lumenbridge ⇄ AstrBot ⇄ 各平台会话
```

### 部署步骤

第一步，把 `astrbot_plugin_lumenbridge` 放入 AstrBot 的 `data/plugins/` 目录（或通过 AstrBot WebUI 插件市场安装），重启 AstrBot。第二步，在 LumenBridge 的连接配置页添加一张 `astrbot` 类型的适配器卡片，填写连接方向。推荐使用正向 WS：AstrBot 插件端的 `ws_mode` 保持 `server`（默认），监听端口默认 6200，LumenBridge 卡片的目标地址填 `ws://<AstrBot机器IP>:6200`，两端 Access Token 保持一致。

第三步，在 AstrBot 插件端的 `umo_list` 里添加要桥接的会话，每项是一个 `unified_msg_origin`（统一消息来源）字符串。例如：

```text
["aiocqhttp:GroupMessage:123456", "telegram:GroupMessage:-100123", "telegram:FriendMessage:555"]
```

群聊和私聊都可以添加，私聊还需打开 `private_bridge`。可以用 `=>数字` 显式指定虚拟群号，不指定则自动分配。虚拟群号会同步显示在 LumenBridge 的群列表中，子插件可以按群号路由。

### 验证与说明

配置完成后，LumenBridge 的 AstrBot 卡片显示绿色「已连接」即表示打通。在 Telegram 或 Discord 等桥接会话发言，游戏内就会出现对应消息；游戏内聊天也会推送到全部桥接会话。平台用户会自动分配虚拟 QQ 号并持久化，重启不会漂移。AstrBot 插件端已实现 OneBot 常用 API（发群聊、私聊、合并转发、群列表等），未支持的 API 会按空数据成功应答，不影响 LumenBridge 运行。

---

## 国际化

LumenBridge 支持三种界面语言：简体中文（`zh_CN`）、繁体中文（`zh_TW`）和英文（`en`）。语言设置影响控制台日志、游戏内命令反馈和 WebUI 界面的显示语言，不影响群消息内容（群消息始终按你配置的格式发送）。

在 `config.json` 的 `language` 字段设置界面语言：

| 值 | 说明 |
|------|------|
| `auto` | 自动检测服务器语言（读取 `server.properties` 或 Endstone API），默认值 |
| `zh_CN` | 简体中文 |
| `zh_TW` | 繁体中文 |
| `en` | 英文 |

配置示例：

```json
"language": "auto"
```

设为 `auto` 时，插件启动会先尝试读取 BDS 的 `server.properties` 中的语言设置，如果读取不到则回退到 Endstone API 检测，最后回退到简体中文。语言文件存放在 whl 内部的 `locales/` 目录下，包含 `en.json`、`zh_CN.json`、`zh_TW.json` 三个文件。子插件也可以通过 API 注册自己的多语言文案，与核心语言设置保持一致。

---

## 插件市场

LumenBridge 内置了一个在线插件市场，方便你浏览、搜索和安装社区开发的子插件。市场网址是 [market.mxcraft.vip](https://market.mxcraft.vip/)，你可以直接在网页端浏览，也可以在 WebUI 的「插件市场」页面里操作。

### 安装插件

在 WebUI 的插件市场页，你可以按名称搜索插件、查看插件详情和版本历史，点击安装即可。安装时插件会自动下载 zip 包并校验 SHA-256 完整性，确保下载的文件与市场发布记录完全一致。校验通过后，zip 包会经过路径穿越防护流程解压到子插件目录，同名插件按版本号比较决定是升级还是跳过。

安装完成后，新插件默认处于已加载状态，可以直接使用。如果插件声明了第三方 pip 依赖，LumenBridge 会自动帮你安装这些依赖（需要 `pip.enable` 为 true）。

### 发布插件

如果你开发了子插件想分享给社区，可以在市场网站上发布。发布流程使用 GitHub 账号登录，无需单独注册账号。发布规则如下：

- GitHub 账号注册满一年的用户，发布后直接上架，无需审核。
- GitHub 账号注册未满一年的用户，发布后需要等待管理员审核通过才能上架。

这一规则是为了平衡开放性和安全性，既鼓励社区贡献，又能对新账号进行基本的防滥用筛查。审核通常会在较短时间内完成。

---

## 插件市场配置项

插件市场相关配置在 `config.json` 的 `marketplace` 块中：

| 字段名 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| enable | bool | true | 是否启用插件市场功能 |
| api_url | string | `https://market.mxcraft.vip` | 市场站点根地址，后台自动补全 `/api/v1` |
| allow_http | bool | false | 是否允许 HTTP（非加密）访问市场，生产环境应保持 false |
| timeout | int | 30 | 请求超时时间，范围 5 至 120 秒 |
| max_download_bytes | int | 67108864 | 单个插件包最大下载字节数，默认 64 MiB，范围 1 至 128 MiB |
| check_on_start | bool | true | 是否在服务器启动时检查插件更新 |
| check_interval_seconds | int | 21600 | 定期检查更新的间隔，默认 6 小时，范围 60 秒至 7 天 |
| report_api_key | string | `""` | 可选的上报 API Key，与市场站点的 webui_report_api_key 一致 |

配置示例：

```json
"marketplace": {
    "enable": true,
    "api_url": "https://market.mxcraft.vip",
    "allow_http": false,
    "timeout": 30,
    "max_download_bytes": 67108864,
    "check_on_start": true,
    "check_interval_seconds": 21600,
    "report_api_key": ""
}
```

`api_url` 只需要填站点根地址，LumenBridge 会自动在后面拼接 `/api/v1` 路径前缀。如果你自建了市场站点，把这里改成你的地址即可。`allow_http` 在生产环境务必保持 false，HTTP 明文传输存在被中间人篡改的风险，仅本地调试时可以临时开启。

---

## pip 包管理配置项

子插件可能依赖第三方 Python 包（比如 requests、Pillow 等）。LumenBridge 内置了 pip 包管理功能，可以在游戏内或 WebUI 中安装这些依赖，不用手动登录服务器执行命令。

pip 管理相关配置在 `config.json` 的 `pip` 块中：

| 字段名 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| enable | bool | true | 是否启用 pip 包管理功能 |
| index_url | string | `""` | pip 镜像源地址，留空表示使用官方 PyPI |
| timeout | int | 300 | pip 操作超时时间，范围 10 至 3600 秒 |

配置示例：

```json
"pip": {
    "enable": true,
    "index_url": "https://mirrors.cloud.tencent.com/pypi/simple",
    "timeout": 300
}
```

`index_url` 有一个智能默认值机制：当界面语言为简体中文或繁体中文时，首次生成配置会自动填入腾讯云镜像源以加速下载；语言为英文时则留空使用官方 PyPI。一旦你手动设置过这个字段（哪怕设为空字符串），就不会再被自动覆盖。国内服务器推荐使用镜像源，否则 pip 下载可能非常缓慢甚至超时。

pip 安装操作是串行执行的（通过内部锁保证），避免 WebUI 安装、插件市场依赖安装和 `/lumen pip install` 并发执行导致 site-packages 元数据损坏。核心包（endstone 等关键依赖）受到保护，不会被误卸载。

---

## 背景图配置

WebUI 的登录页支持自定义背景图，可以对接任意「直接返回图片」的随机图片 API，让管理面板的视觉效果更丰富。背景图相关配置在 `config.json` 的 `background` 块中：

| 字段名 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| enable | bool | true | 是否启用自定义背景图 |
| api_url | string | `https://t.alcy.cc/fj` | 随机图片 API 地址，需返回图片内容 |
| blur_strength | int | 0 | 毛玻璃模糊强度，0 为不模糊，范围 0 至 100 |
| fallback_to_default | bool | true | 图片获取失败时是否回退到默认背景 |
| cache_seconds | int | 600 | 背景图缓存时间，范围 30 至 86400 秒 |

配置示例：

```json
"background": {
    "enable": true,
    "api_url": "https://t.alcy.cc/fj",
    "blur_strength": 0,
    "fallback_to_default": true,
    "cache_seconds": 600
}
```

`api_url` 必须是有效的 `http://` 或 `https://` 地址，接口应直接返回图片二进制内容（而非 JSON 包装）。`blur_strength` 控制背景图的毛玻璃模糊效果，数值越大越模糊，适合在背景图过于花哨影响文字可读性时使用。如果 API 请求失败，`fallback_to_default` 为 true 时会显示内置的默认背景，为 false 则不显示背景图。

---

## 更新检查配置

LumenBridge 会定期检查自身框架是否有新版本发布，并在 WebUI 总览页提示你更新。更新检查相关配置在 `config.json` 的 `updates` 块中：

| 字段名 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| enable | bool | true | 是否启用更新检查 |
| api_url | string | `https://market.mxcraft.vip` | 检查更新的 API 地址，自动补全 `/api/v1/updates/lumenbridge` |
| timeout | int | 30 | 请求超时时间，范围 5 至 120 秒 |

配置示例：

```json
"updates": {
    "enable": true,
    "api_url": "https://market.mxcraft.vip",
    "timeout": 30
}
```

和插件市场一样，`api_url` 只需要填站点根地址。更新检查不会自动下载或安装新版本，只会在检测到新版本时通知你。你可以通过插件市场的 WebUI 页面手动触发框架更新，更新前会自动备份当前版本。

---

## 热重载

热重载是 LumenBridge 日常使用中非常重要的一个特性，它让你在修改配置后不用重启整个服务器就能让改动生效。执行 `/lumen reload` 命令（控制台或游戏内 OP 均可）会依次完成以下操作：

1. 重新加载 `config.json`，合并缺失字段并校验跨字段约束。
2. 重新初始化国际化语言设置。
3. 重建连接适配器（差量重建：只重建有变更的适配器，未变更的保持连接不断开）。
4. 重载 WebUI 配置（host/port/password/secret/enable 变化立即生效）。
5. 失效 pip manager 缓存，使新的 pip 配置生效。
6. 重新加载 `rules.json` 规则库。
7. 热重载全部子插件。

热重载是事务性的：如果某个环节出错（比如配置校验失败），会输出错误信息但不影响其他环节。WebUI 保存配置时也会自动触发对应的热重载流程，所以你在 WebUI 改完配置点保存就能立即生效，不需要再手动执行 reload 命令。

需要注意的是，连接参数的改动（比如改了 WebSocket 目标地址或端口）在旧版本中需要重启服务器，但现在的热重载已经支持差量重建适配器，只有真正发生变更的适配器会被断开重连，其他适配器保持原状。这使得在线调整多适配器配置变得非常平滑。

---

## 常见问题

### 启动后日志显示连接失败怎么办

首先确认协议端（NapCat 等）已经启动并且机器人 QQ 已登录。然后检查 LumenBridge 连接卡片里的 `target` 地址和端口是否与协议端一致，同机用 `127.0.0.1`，跨机用实际 IP。如果配置了 `access_token`，两端必须完全一致。最后确认 BDS 服务器能访问到协议端，可以用 `curl` 或 `telnet` 测试端口连通性。

### 群消息能到游戏，但游戏消息到不了群

检查对应适配器的 `sync.chat_to_group_enable` 是否为 true。然后确认机器人 QQ 号确实在目标群里（不在群里自然发不出去）。在控制台执行 `/lumen status` 确认适配器显示已连接。如果以上都没问题，查看控制台是否有发送失败的错误日志。

### 白名单绑定提示「服务器未能添加白名单」

确认服务器已经开启了白名单（执行 `allowlist on`）。检查 XboxID 拼写是否正确，注意 XboxID 区分大小写。如果 ID 里包含空格，需要用引号包裹，例如 `绑定白名单 "Player One"`。查看控制台是否有 `allowlist add` 的错误输出，常见原因是 ID 已存在或格式不合法。

### WebUI 打不开

确认 `webui.enable` 为 true，并且端口没有被其他程序占用。如果需要从外网访问，把 `webui.host` 改为 `0.0.0.0` 并在防火墙放行端口。安全建议：WebUI 暴露到公网会招来扫描器骚扰，最好用 `127.0.0.1` 配合反向代理和 IP 白名单使用。如果端口被占用，换个端口重试。

### 改了配置不生效

大部分配置可以用 `/lumen reload` 热重载，包括基础配置、规则库、子插件和连接适配器。WebUI 修改配置会自动保存并热重载。如果热重载后仍然不生效，检查配置文件是否有语法错误（JSON 格式问题），控制台会在加载时报错。极少数情况下（如 Endstone 版本兼容性问题）可能需要完整重启服务器。

### 服务器日志频繁出现 ConnectionResetError

这通常是外网扫描器连上 WebUI 端口后立刻断开导致的。LumenBridge 已经修复了由此导致的崩溃问题，如果仍然看到少量日志，把 WebUI 监听地址改为 `127.0.0.1` 或者在防火墙限制 WebUI 端口的访问 IP 即可消除。

### 如何更新到新版本

下载新版 whl 文件，覆盖 `plugins/` 目录里的旧文件，重启服务器即可。配置文件、规则库、白名单数据和子插件都会保留。如果你通过插件市场的 WebUI 页面触发框架更新，系统会自动备份当前版本再替换。更新后如果有破坏性变更，控制台会输出迁移提示，按照提示处理即可。

### QQ 官方机器人连接被拒

检查 AppID 和 AppSecret 是否正确，确认机器人已经在开放平台通过审核并发布。如果订阅了 `extra_intents` 里的特权事件（如频道成员 1<<1），确认官方已经为你的机器人开通了对应权限，否则网关会拒连。沙箱环境下测试时记得把 `sandbox` 设为 true。如果连续断连，把 `suppress_connection_log` 设为 true 减少日志干扰，然后观察是否有具体错误码（如 4004 表示鉴权失败需要重新获取 token）。
