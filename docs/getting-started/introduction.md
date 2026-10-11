# 项目介绍与核心概念

## LumenBridge 是什么

LumenBridge（流明桥）是一款面向 **Endstone**（Minecraft 基岩版专用服务器插件框架）的 QQ 群服互通插件，使用纯 Python 编写，运行在 Minecraft 基岩版专用服务器（BDS）之上。

它把游戏内聊天与 QQ 群消息打通，让管理员不用进游戏就能在群里管理服务器，让玩家不用进游戏就能收到进退服、死亡和开关服通知。除基本消息同步外，还提供白名单自动绑定、可编程的正则触发引擎、可视化 Web 管理面板、子插件体系和在线插件市场，覆盖群服社区日常运营的全部需求。

## 核心特性

| 特性 | 说明 |
|------|------|
| 三协议并行 | OneBot v11（NapCat / Lagrange / LLOneBot / go-cqhttp）、QQ 开放平台官方机器人、AstrBot 全平台中继，可同时运行 |
| 多适配器 | 每个适配器独立的管理员、群列表与互通配置，差量热重载，保存即生效 |
| 官方机器人全覆盖 | 原生实现 QQ 开放平台网关协议，46 种事件类型全覆盖，被动凭据池 + 主动消息补发 + 发送重试矩阵 |
| 白名单自动绑定 | 群内指令绑定 XboxID，自动执行 `allowlist add`，退群自动移除 |
| 正则触发引擎 | 基于 `rules.json` 的可编程触发器，保存即热加载，内置 ReDoS 防护 |
| Web 管理面板 | 纯标准库实现（零 Web 框架依赖），毛玻璃设计，三语界面 |
| 子插件体系 | 完整 Python API、热重载、pip 依赖声明、WebUI 扩展、Endstone 全 API 直达 |
| 插件市场 | 在线浏览 / 搜索 / 安装，SHA-256 完整性校验，依赖自动补装 |
| 零外部依赖 | `websockets` 已内置打包，放进 `plugins/` 即用 |

## 三种连接方式

| 适配器类型 | 协议 | 适用场景 | 标识体系 |
|-----------|------|---------|---------|
| `websocket` | OneBot v11 | 直连 NapCat / Lagrange / LLOneBot / go-cqhttp 等个人号协议端 | 数字 QQ 号 |
| `qqofficial` | QQ 开放平台 API | 无需部署协议端，扫码即用，合规运营 | openid 字符串 |
| `astrbot` | OneBot v11 中继 | 经 AstrBot 扩展到 Telegram / Discord / 微信 / 飞书等全平台 | 虚拟 QQ 号 |

三种适配器可同时运行，各自维护独立的管理员列表、群列表和互通配置，互不干扰。

## 核心概念

### 适配器（Adapter）

一个适配器 = 一条到聊天平台的连接 + 一套独立的身份配置（机器人号、管理员列表、主群列表）+ 一套独立的群服互通设置。适配器以「卡片」形式存放在 `plugins/lumenbridge/connections/` 目录，可在 WebUI 中增删改，保存后由 AdapterHub **差量重建**——只有配置发生变化的适配器会重连，其余连接不受干扰。

### 域（Domain）

用户和群在不同连接方式下的标识体系不同：

- **个人号域（`qq`）**：用户是数字 QQ 号，群是数字群号。
- **官方域（`official`）**：用户是 openid 字符串，群是 group_openid 字符串。

LumenBridge 在发送消息时按目标标识自动路由（数字走个人号域、openid 走官方域），子插件也可以通过事件包的 `domain` 字段判断消息来源域。

### 事件包（Pack）

所有聊天平台的事件（消息、通知、请求）都会被统一翻译成 **OneBot v11 格式的 JSON 数据包**（简称 pack）后在事件总线上分发。子插件监听事件时拿到的就是这个 pack。QQ 官方机器人的原生事件同样被翻译成 OneBot 语义（无对应语义的以扩展事件原样转发），因此子插件无需关心底层是哪种连接方式。

### 主群（main_group）

每个适配器可配置若干主群。主群是白名单绑定、正则引擎（`only_on_main` 开启时）、游戏事件通知等功能的生效范围。游戏事件会广播到所有已连接适配器的全部主群。

### 子插件（SubPlugin）

运行在 `plugins/lumenbridge/subplugins/` 目录下的 Python 插件，通过 `on_load(lumen)` 拿到一个 `LumenContext` 上下文对象，即可订阅事件、收发消息、调用 Endstone API、注册 WebUI 页面。支持热重载与第三方 pip 依赖声明。

## 总体架构一览

```text
                    ┌─────────────────────────────────────┐
                    │           Endstone 服务器            │
                    │  （主线程：游戏逻辑 / 命令 / 事件）      │
                    └──────────────┬──────────────────────┘
                                   │ run_on_main
┌──────────────┐    WebSocket   ┌──┴───────────────┐   ┌──────────────┐
│ NapCat 等协议端│◄──────────────►│                  │◄──┤  QQ 开放平台   │
└──────────────┘                │   LumenBridge    │   └──────────────┘
┌──────────────┐                │      插件本体      │   ┌──────────────┐
│   AstrBot    │◄──────────────►│                  │◄──┤  Web 管理面板  │
└──────────────┘                └──┬───────────────┘   └──────────────┘
                                   │ 事件总线 EventBus
                    ┌──────────────┼──────────────────┐
                    │  chat_sync   │ regex_engine     │  子插件 × N
                    │  whitelist   │ chat_filter      │  （LumenContext）
                    │  group_bind  │ chat_probe …     │
                    └──────────────┴──────────────────┘
```

## 下一步

- [安装 LumenBridge](installation.md)
- [三步打通群服互通](quickstart.md)
- [了解全部配置项](../guide/configuration.md)
- [开发第一个子插件](../development/subplugin-getting-started.md)
