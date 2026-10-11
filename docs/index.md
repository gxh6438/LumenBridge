---
layout: home

hero:
  name: LumenBridge
  text: Endstone 群服互通框架
  tagline: OneBot v11 双向消息同步 · QQ 官方机器人 · 全平台中继 · 零外部依赖
  image:
    src: /logo-512.png
    alt: LumenBridge
  actions:
    - theme: brand
      text: 快速上手
      link: /getting-started/quickstart
    - theme: alt
      text: 安装插件
      link: /getting-started/installation
    - theme: alt
      text: GitHub
      link: https://github.com/gxh6438/LumenBridge

features:
  - title: 群服双向互通
    details: 游戏内聊天与 QQ 群消息实时同步，进退服、死亡与开关服通知自动推送，管理员在群里即可管理服务器。
  - title: 多适配器并行
    details: OneBot v11 个人号、QQ 官方机器人、AstrBot 中继可同时运行，各自维护独立的群列表与管理员，保存即热重载。
  - title: 子插件生态
    details: 动态加载、热重载、插件市场一键安装，Context API、事件总线、Web 扩展（配置表单 / REST / 自定义页面）齐备。
  - title: WebUI 管理面板
    details: 纯标准库实现的管理面板，浏览器内完成连接、白名单、正则引擎、插件市场全部管理。
  - title: 安全设计
    details: PBKDF2 密码哈希、路径穿越防护、本地图片白名单、受保护 pip 包名单、反向 WS 无鉴权监听告警。
  - title: 零外部依赖
    details: websockets 库已内置打包，放进 plugins/ 目录即可使用，不与服务器已有包冲突。
---

# LumenBridge 文档中心

> 版本 1.0.12 · MIT License · 面向 Endstone（Minecraft 基岩版专用服务器插件框架）

欢迎来到 LumenBridge（流明桥）官方文档。文档按「入门 → 使用 → 开发」三层组织，每篇独立成文，方便按需查阅与维护。

## 文档地图

文档按「入门 → 使用 → 开发」三层组织，**各层内部按建议阅读顺序排列**（编号即顺序），层层递进：入门三篇走完即可跑通；使用指南沿「配置 → 连接 → 面板 → 核心功能 → 进阶」的用户旅程推进；开发文档沿「全局观 → 上手 → 核心 API → 深入 → 发布」的学习曲线推进。

### 入门（getting-started/）

| # | 文档 | 内容 |
|---|------|------|
| 1 | [项目介绍与核心概念](getting-started/introduction.md) | LumenBridge 是什么、能做什么、适配器/域/事件包/子插件等核心概念 |
| 2 | [安装与升级](getting-started/installation.md) | 环境要求、安装步骤、目录布局、旧版数据迁移、升级与卸载 |
| 3 | [快速上手](getting-started/quickstart.md) | 以 NapCat 为例三步打通群服互通，附官方机器人 / AstrBot 快速路径 |

### 使用指南（guide/）

| # | 文档 | 内容 |
|---|------|------|
| 1 | [配置详解](guide/configuration.md) | `config.json` 全部字段说明、校验规则、外部修改热生效 |
| 2 | [连接与适配器](guide/adapters.md) | 三类适配器卡片字段详解、群服互通 sync 配置、双域路由、群绑定密钥、批量添加管理员 |
| 3 | [Web 管理面板](guide/webui.md) | 登录鉴权、各功能页面、安全建议 |
| 4 | [群服互通与聊天](guide/chat.md) | 双向同步链路、消息段格式化、聊天转发策略、聊天屏蔽词 |
| 5 | [白名单绑定](guide/whitelist.md) | 群内绑定 / 解绑 XboxID、双域存储、退群自动移除 |
| 6 | [游戏内命令](guide/commands.md) | `/lumen` 全部子命令与权限模型 |
| 7 | [正则触发引擎](guide/regex-engine.md) | 规则结构、条件、变量替换、动作类型、内置规则 |
| 8 | [插件市场与更新](guide/marketplace.md) | 子插件安装 / 更新 / 发布、框架更新、pip 包管理 |
| 9 | [常见问题](guide/faq.md) | 连接失败、消息不通、白名单异常等高频问题排查 |
| 10 | [用户使用条例与免责声明](guide/terms.md) | 使用前提、账号风险、第三方内容、责任边界 |

### 开发文档（development/）

| # | 文档 | 内容 |
|---|------|------|
| 1 | [架构总览](development/architecture.md) | 模块划分、线程模型、事件流、安全设计——建立全局观 |
| 2 | [子插件快速入门](development/subplugin-getting-started.md) | 十分钟写出第一个子插件 |
| 3 | [LumenContext API 参考](development/context-api.md) | 子插件上下文对象完整 API |
| 4 | [事件系统参考](development/events.md) | OneBot 事件、官方扩展事件、游戏事件的订阅名与包结构 |
| 5 | [消息收发](development/messaging.md) | 消息段构建器、动作包透传、双域路由与能力差异 |
| 6 | [QQ 官方机器人开发专题](development/qqofficial-dev.md) | 网关协议、事件翻译、API 翻译参考（官方域全量动作）、被动 / 主动消息机制、错误码 |
| 7 | [Web 扩展](development/web-extensions.md) | 配置表单、REST API、自定义页面（iframe） |
| 8 | [清单与依赖](development/manifest-and-deps.md) | `lumen.json` 全字段、pip 依赖、`requires` 强制依赖 |
| 9 | [国际化](development/i18n.md) | 多语言机制与子插件翻译注册 |
| 10 | [生命周期与热重载](development/lifecycle.md) | 主插件启停、子插件加载 / 卸载语义、框架自动清理、热重载 |
| 11 | [项目结构](development/project-structure.md) | 源码目录与存储文件速查 |
| 12 | [测试指南](development/testing.md) | 回归测试运行方式与编写约定 |

## 文档约定

- 路径如 `plugins/lumenbridge/` 指 Endstone 服务器数据目录下的插件数据目录。
- 配置键使用点号路径表示，如 `webui.password` 即 `config.json` 中 `webui` 对象内的 `password` 字段。
- 「个人号域」指通过 OneBot v11 协议端接入的 QQ 个人号机器人；「官方域」指通过 QQ 开放平台 API 接入的官方机器人。
- 代码示例默认 Python 3.11+。

## 快速链接

- 插件市场：<https://market.mxcraft.vip/>
- 在线文档：<https://lumen.mxcraft.vip/>
- 问题反馈：项目仓库 Issues
