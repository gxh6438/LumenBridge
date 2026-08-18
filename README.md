# LumenBridge

**Endstone 群服互通框架 · OneBot v11 双向消息同步 · QQ 官方机器人 · 全平台中继**

[插件市场](https://market.mxcraft.vip/) · [使用文档](https://lumen.mxcraft.vip/getting-started/usage) · [开发文档](https://lumen.mxcraft.vip/developers/plugin-development) · [MIT License](#许可)

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

原生实现 QQ 开放平台网关协议，**46 种事件类型全覆盖**——群消息、私聊、频道消息、全部 notice 与扩展事件，无一遗漏。内置被动凭据池管理、主动消息补发机制与发送重试矩阵。扫码授权后 AppID 与 AppSecret 自动填入，无需手动复制粘贴。

## 核心功能

### 群服双向互通

游戏聊天与 QQ 群消息实时双向同步，消息段完整格式化（文本 / 图片 / 表情 / @ / 回复 / 合并转发）。玩家进退服、死亡、服务器开关自动通知到群。出站广播按每个适配器各自的配置与群列表发送，多群多适配器互不串扰。

### 白名单自动绑定

群内发送指令即可绑定 Xbox ID 并自动执行 `allowlist add`，退群自动移除白名单。绑定 / 解绑触发词可自定义，支持手动管理与查看绑定关系。

### 正则触发引擎

基于 `rules.json` 的可编程消息 / 事件触发器，支持条件校验、变量替换（`$userId` / `$groupId` / `$nickname`）、多动作序列、自定义动作类型。规则支持图形编辑与 JSON 编辑两种模式，**保存即热加载**，无需重启。规则按优先级从上到下执行，支持"阻断后续"短路逻辑。

### Web 管理面板

浏览器可视化管理全部功能，采用毛玻璃（Glassmorphism）设计语言，三语界面自动检测服务器语言：

| 页面 | 功能 |
| --- | --- |
| **总览** | 适配器连接状态、CPU / 内存实时环形仪表盘、在线玩家、白名单 / 正则 / 子插件计数、运行环境信息 |
| **核心配置** | 基础配置表单编辑 + JSON 双模式、连接适配器卡片管理、背景图上传、语言设置 |
| **正则** | 规则增删改、事件类型选择、条件配置、动作序列编辑、规则优先级排序 |
| **子插件** | 安装 / 启用 / 禁用 / 热重载 / 卸载、在线编辑源码、依赖管理 |
| **插件市场** | 在线浏览、搜索、安装子插件，SHA-256 完整性校验 |
| **包管理** | 查看 / 安装 / 卸载 pip 依赖 |
| **实时日志** | 服务器控制台日志实时流 |

### 子插件体系

完整的 Python 子插件 API，支持热重载、第三方 pip 依赖声明、i18n 国际化。已实现OneBot v11协议穿透，子插件可以获取OneBot v11所有事件！以及Endstone所有API！同时，LumenBridge内置官Bot事件翻译，OneBot插件接入官Bot无缝衔接！

### 插件市场

内置插件市场，也可直接访问 [market.mxcraft.vip](https://market.mxcraft.vip/) 浏览和管理：

- **下载插件**：在 WebUI 或网页端浏览、搜索并安装，安装时自动校验 SHA-256 完整性
- **发布插件**：使用 GitHub 账号登录；注册满一年的账号可直接发布，未满一年的发布后需管理员审核通过

---

## 快速开始

### 环境要求

- Endstone 0.11+（Minecraft 基岩版专用服务器）
- Python 3.10+（Endstone 内置）
- 一个 OneBot v11 协议端（NapCat / Lagrange / LLOneBot / go-cqhttp）或 QQ 官方机器人凭据

### 安装

将 `endstone_lumenbridge-1.0.0-py3-none-any.whl` 放入 Endstone 的 `plugins/` 目录，启动服务器即可。插件无任何外部 pip 依赖。

### 三步配置

1. **部署协议端**：以 NapCat 为例，添加正向 WebSocket 服务端，端口 `3001`
2. **启动服务器**：whl 放入 `plugins/` 后启动 Endstone，自动生成默认配置
3. **配置连接**：打开 WebUI（默认 `http://127.0.0.1:8765`，密码见控制台），在「连接配置」页填写信息

```text
plugins/
├── endstone_lumenbridge-1.0.0-py3-none-any.whl
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

详见 [子插件开发文档](https://lumen.mxcraft.vip/developers/plugin-development)。

---

## 文档

| 文档 | 说明 |
| --- | --- |
| [使用文档](https://lumen.mxcraft.vip/getting-started/usage) | 安装、配置、日常使用全指南 |
| [子插件开发文档](https://lumen.mxcraft.vip/developers/plugin-development) | 子插件 API 完整参考与教程 |
| [QQ 官方机器人适配指南](https://lumen.mxcraft.vip/qq-bot/adapter-guide) | QQ 官方 bot 配置与事件详解 |
| [QQ 官方机器人 API 参考](https://lumen.mxcraft.vip/qq-bot/api-reference) | QQ 官方 bot 子插件 API 速查 |

---

## 技术规格

| 项目 | 规格 |
| --- | --- |
| 版本 | 1.0.0 |
| 许可证 | MIT |
| 语言 | Python 3.10+ |
| 框架 | Endstone 0.11+ |
| 协议 | OneBot v11 / QQ 开放平台 API |
| 外部依赖 | 零（websockets 已内置） |
| 国际化 | 简体中文 / 繁体中文 / 英文 |
| 测试覆盖 | 23 个测试文件、172+ 项回归测试 |

---

## 特别鸣谢

功能设计参考了以下开源项目：

- [SparkBridge3](https://github.com/SparkBridge/SparkBridge3) (Apache-2.0) — 群服互通功能设计参考
- [endstone-qqsync-plugin](https://github.com/yuexps/endstone-qqsync-plugin) (MIT) — 线程模型参考

本插件部分代码由 AI 协助编写。

---

## 许可

本项目采用 **MIT License**。
