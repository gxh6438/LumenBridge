# LumenBridge

**Endstone 群服互通框架 · OneBot v11 双向消息同步 · QQ 官方机器人 · 全平台中继**

[插件市场](https://market.mxcraft.vip/) · [文档中心](https://gxh6438.github.io/LumenBridge/) · [Apache-2.0 License](#许可)

---

## 致谢与开源声明

LumenBridge 的诞生离不开开源社区的滋养，我们要在一切之前，向以下项目郑重致谢：

### ⭐ 特别感谢 [SparkBridge3](https://github.com/SparkBridge/SparkBridge3)

**LumenBridge 的部分核心模块衍生自 SparkBridge3**（LeviLamina 平台的 BDS QQ 机器人框架，Apache-2.0 许可证）。在 LumenBridge 立项之初，我们让 AI 完整阅读了 SparkBridge3 的源码并复刻了部分功能设计——具体而言：

- **正则触发引擎**：`rules.json` 规则数据模型的字段结构（`triggerType` / `pattern` / `eventType` / `conditions` / `actions`）、条件三元组模型、内置动作命名与默认规则场景，源自 SparkBridge3 的 `sb3_regex` 插件；
- **群服互通**：同步配置键名与默认消息模板文案（`[玩家] %s 进服` / `[群聊] %s: %s` 等），源自 SparkBridge3 的 `sb3_mc` 插件；
- **事件命名体系**（`message.group.normal` 等）沿用 SparkBridge3 的命名方式。

**我们由衷感谢 [SparkBridge3](https://github.com/SparkBridge/SparkBridge3) 与 [endstone-qqsync-plugin](https://github.com/yuexps/endstone-qqsync-plugin) 这两个开源项目**——SparkBridge3 以 Apache-2.0 协议开放了清晰的源码与设计，LumenBridge 的早期开发直接受益于此；endstone-qqsync-plugin 的线程模型设计也给了我们很大启发。请大家也去给这两个项目点一个 Star。

完整的衍生声明、修改清单与许可证义务履行情况，见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

### 其他致谢

- [websockets](https://github.com/python-websockets/websockets)（BSD-3-Clause）— 已内置打包于本插件
- [Prism](https://prismjs.com/)（MIT）与 [qrcode-generator](https://github.com/kazuhikoarase/qrcode-generator)（MIT）— WebUI 内置组件

此外，本插件部分代码由 AI 协助编写。

---

## 项目简介

LumenBridge（流明桥）是一款面向 **Endstone**（Minecraft 基岩版专用服务器插件框架）的 QQ 群服互通插件，使用纯 Python 编写。它将游戏内聊天与 QQ 群消息打通，让管理员在群里就能管理服务器，让玩家不用进游戏就能收到进退服、死亡和开关服通知。

插件同时支持三种连接方式：通过 **OneBot v11 协议**直连 NapCat / Lagrange / LLOneBot / go-cqhttp 等个人号机器人框架；通过 **QQ 开放平台 API** 接入官方机器人；以及通过 **AstrBot 中继**扩展到 Telegram / Discord / 微信 / 飞书等全平台。三种适配器可同时运行，各自维护独立的管理员、群列表和互通配置，互不干扰。

整个插件**零外部依赖**（`websockets` 库已内置打包），放进 `plugins/` 目录即可使用。

---

## 技术架构

### 多适配器并行引擎

LumenBridge 的核心是一个**多适配器并行引擎**。每个适配器实例独立维护 WebSocket 连接、事件分发、消息队列与重连机制，通过统一的 `AdapterHub` 进行生命周期管理和差量热重载。当配置变更时，Hub 只重建受影响的适配器，其他连接不受干扰——保存即生效，无需重启服务器。

| 适配器类型 | 协议 | 适用场景 |
| --- | --- | --- |
| WebSocket（正向 / 反向） | OneBot v11 | NapCat / Lagrange / LLOneBot / go-cqhttp |
| QQ 官方机器人 | QQ 开放平台 API | 无需部署协议端，扫码即用 |
| AstrBot | OneBot v11 中继 | Telegram / Discord / 微信 / 飞书等全平台 |

### QQ 官方机器人全覆盖

原生实现 QQ 开放平台网关协议，**46 种事件类型全覆盖**——群消息、私聊、频道消息、全部 notice 与扩展事件，无一遗漏。内置被动凭据池管理、主动消息补发机制、发送重试矩阵与按钮键盘互动消息。扫码授权后 AppID 与 AppSecret 自动填入，无需手动复制粘贴。

### 事件翻译层

QQ 官方机器人的事件在内部统一翻译为 OneBot v11 语义，子插件无需感知消息来自个人号还是官方机器人——同一套代码同时服务两个域。

---

## 核心功能

### 群服双向互通

游戏聊天与 QQ 群消息实时双向同步，消息段完整格式化（文本 / 图片 / 表情 / @ / 回复 / 合并转发）。玩家进退服、死亡、服务器开关自动通知到群。出站广播按每个适配器各自的配置与群列表发送，多群多适配器互不串扰。

### 白名单自动绑定

群内发送指令即可绑定 Xbox ID 并自动执行 `allowlist add`，退群自动移除白名单。绑定 / 解绑触发词可自定义，支持手动管理与查看绑定关系。

### 正则触发引擎

基于 `rules.json` 的可编程消息 / 事件触发器，支持条件校验、变量替换、多动作序列、自定义动作类型。内置 10 个消息变量（`$userId` / `$groupId` / `$nickname` / `$rawMessage` / `$card` / `$role` / `$domain` 等），另有正则捕获组（`$1`…）与动作上下文变量（`$result` 等）。规则支持图形编辑与 JSON 编辑两种模式，**保存即热加载**。规则按优先级从上到下执行，支持「阻断后续」短路逻辑，并内置 ReDoS 防护。

### Web 管理面板

浏览器可视化管理全部功能，采用毛玻璃（Glassmorphism）设计语言，三语界面自动检测服务器语言：

| 页面 | 功能 |
| --- | --- |
| **总览** | 适配器连接状态、CPU / 内存实时环形仪表盘、在线玩家、白名单 / 正则 / 子插件计数、运行环境信息 |
| **核心配置** | 基础配置表单编辑 + JSON 双模式、连接适配器卡片管理、背景图上传、语言设置 |
| **正则** | 规则增删改、事件类型选择、条件配置、动作序列编辑、规则优先级排序 |
| **子插件** | 安装 / 启用 / 禁用 / 热重载 / 卸载、在线编辑源码、依赖一键补全 |
| **插件市场** | 在线浏览、搜索、安装子插件，SHA-256 完整性校验 |
| **包管理** | 查看 / 安装 / 卸载 pip 依赖（内置 32 项受保护包名单，防止破坏服务器本体） |
| **实时日志** | 级别筛选、关键词过滤与命中高亮、暂停 / 继续、连接状态指示 |

安全设计：PBKDF2 密码哈希、版本化可撤销 Token、登录防爆破、Host 校验。

### 子插件体系

完整的 Python 子插件 API，支持热重载、第三方 pip 依赖声明（含 Endstone 插件级依赖与版本比较）、i18n 国际化、自定义 Web 页面注册。已实现 OneBot v11 协议穿透——子插件可接收 OneBot v11 全部事件、直达 Endstone 所有 API；官方机器人事件经翻译层自动适配，OneBot 生态插件接入无缝衔接。

### 插件市场

内置插件市场，也可直接访问 [market.mxcraft.vip](https://market.mxcraft.vip/) 浏览和管理：

- **下载插件**：在 WebUI 或网页端浏览、搜索并安装，安装时自动校验 SHA-256 完整性
- **发布插件**：使用 GitHub 账号登录；注册满一年的账号可直接发布，未满一年的发布后需管理员审核通过

---

## 快速开始

### 环境要求

- Endstone 0.11+（Minecraft 基岩版专用服务器）
- Python 3.11+（Endstone 内置）
- 一个 OneBot v11 协议端（NapCat / Lagrange / LLOneBot / go-cqhttp）或 QQ 官方机器人凭据

### 安装

从 [Releases](https://github.com/gxh6438/LumenBridge/releases) 下载最新 `endstone_lumenbridge-x.y.z-py3-none-any.whl`，放入 Endstone 的 `plugins/` 目录，启动服务器即可。插件无任何外部 pip 依赖。

### 三步配置

1. **部署协议端**：以 NapCat 为例，添加正向 WebSocket 服务端，端口 `3001`
2. **启动服务器**：whl 放入 `plugins/` 后启动 Endstone，自动生成默认配置
3. **配置连接**：打开 WebUI（默认 `http://127.0.0.1:8765`，密码见控制台），在「连接配置」页填写信息

```text
plugins/
├── endstone_lumenbridge-x.y.z-py3-none-any.whl
└── lumenbridge/
    ├── config.json          # 基础配置
    ├── connections.json     # 连接配置（适配器卡片）
    ├── rules.json           # 正则规则库
    └── whitelist.json       # 白名单绑定数据
```

---

## 子插件开发

LumenBridge 提供完整的子插件开发 API：

```python
lumen = None

def on_load(ctx):
    global lumen
    lumen = ctx
    lumen.on("message.group.normal", on_group_message)

def on_group_message(pack, reply):
    reply("收到！")
```

子插件清单文件 `lumen.json`：

```json
{
    "name": "my_plugin",
    "version": "1.0.0",
    "author": "你的名字",
    "desc": "插件描述",
    "load": true,
    "priority": "post",
    "min_v": "1.0.0",
    "dependencies": []
}
```

详见 [子插件快速入门](docs/development/subplugin-getting-started.md) 与 [Context API 参考](docs/development/context-api.md)。

---

## 文档

完整文档位于 [`docs/`](docs/index.md)（[在线版](https://gxh6438.github.io/LumenBridge/)，由 VitePress 构建），按「入门 → 使用 → 开发」三层组织，每篇独立成文：

| 分区 | 内容 |
| --- | --- |
| [入门 getting-started/](docs/getting-started/introduction.md) | 项目介绍、安装与升级、三步快速上手 |
| [使用指南 guide/](docs/guide/configuration.md) | 配置详解、适配器、命令、白名单、聊天互通、WebUI、正则引擎、插件市场、FAQ |
| [开发文档 development/](docs/development/architecture.md) | 架构总览、子插件开发（入门 / API / 事件 / 消息 / Web 扩展 / 清单 / 生命周期 / i18n / 官方机器人）、项目结构、测试指南 |

---

## 技术规格

| 项目 | 规格 |
| --- | --- |
| 版本 | 1.0.14 |
| 许可证 | Apache-2.0（衍生自 SparkBridge3 的部分同样遵循 Apache-2.0） |
| 语言 | Python 3.11+ |
| 框架 | Endstone 0.11+ |
| 协议 | OneBot v11 / QQ 开放平台 API |
| 外部依赖 | 零（websockets 已内置） |
| 国际化 | 简体中文 / 繁体中文 / 英文 |
| 测试覆盖 | 37 个测试文件、454 项测试用例 |

---

## 许可

本项目采用 **Apache License 2.0**（完整文本见 [LICENSE](LICENSE)）。部分模块衍生自 [SparkBridge3](https://github.com/SparkBridge/SparkBridge3)（Apache-2.0），衍生关系与修改清单详见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)，该声明随 wheel 产物一同分发。

使用本插件前请阅读[用户使用条例与免责声明](docs/guide/terms.md)。
