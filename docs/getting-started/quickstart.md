# 快速上手

本文以最常见的 **NapCat 正向 WebSocket** 为例，三步完成从零到群服互通。官方机器人和 AstrBot 的快速路径见文末。

## 第一步：部署协议端

在运行 QQ 机器人的机器上启动 NapCat 并登录机器人 QQ 号，进入网络配置页面：

1. 添加一个「正向 WebSocket 服务端」，监听端口设为 `3001`。
2. 如配置了 Access Token，记下来，稍后在 LumenBridge 侧填一样的值。
3. 确认 NapCat 显示机器人在线、WebSocket 服务已启动。

Lagrange / LLOneBot 配置方式类似，保证有一个正向 WebSocket 端口对外开放即可；go-cqhttp 已停止更新但仍然兼容，配置文件中的 `ws` 节点同样适用。

## 第二步：启动服务器

把 whl 放进 `plugins/` 目录后启动 Endstone。插件自动创建数据目录和默认配置，并预置两张适配器卡片（WebSocket 与 QQ 官方机器人），都处于未启用状态。控制台会输出：

```text
[LumenBridge] WebUI 已启动: http://127.0.0.1:8300
[LumenBridge] WebUI 登录密码: aB3xK9mN
```

`config.json` 中 `webui.password` 的默认值是 `*`，表示每次启动随机生成密码并打印到控制台（见上）。固定密码可在 WebUI 或配置文件中设置。

## 第三步：配置连接

1. 浏览器打开 WebUI（默认 `http://127.0.0.1:8300`），输入控制台打印的密码登录。
2. 进入「配置 → 连接配置」页签，展开 WebSocket 卡片的设置。
3. 连接方式选「正向 WS」，目标地址填 `ws://127.0.0.1:3001`。
4. 展开「权限与身份设置」，填入机器人 QQ 号、管理员 QQ 号和主群号。
5. 打开「启用此适配器」，保存。

返回服务器，日志出现「适配器已启动」且卡片右下角显示绿色「已连接」即打通成功。在群里发一条消息，游戏内会出现对应内容；游戏内说话，群里也能收到。

> 提示：不想手抄群号和管理员 QQ 号，可以用[群绑定密钥](../guide/adapters.md#群绑定密钥免手填)功能——WebUI 签发密钥，在目标群里发一条指令即可自动完成绑定。

## 验证与常用后续操作

| 操作 | 方式 |
|------|------|
| 查看连接状态 | WebUI 总览页，或游戏内 `/lumen status` |
| 绑定白名单 | 群内发送 `绑定白名单<XboxID>`，详见[白名单](../guide/whitelist.md) |
| 查服 | 群内发送 `查服`（内置正则规则） |
| 安装子插件 | WebUI 插件市场页，详见[插件市场](../guide/marketplace.md) |
| 调整消息格式 | 连接配置卡片中的「群服互通」设置，详见[适配器配置](../guide/adapters.md#群服互通-sync-配置) |

## 官方机器人快速路径

无需部署协议端，适合合规运营：

1. 在 [QQ 开放平台](https://q.qq.com) 创建机器人，拿到 AppID 和 AppSecret。
2. WebUI「配置 → 连接配置」页找到 QQ 官方机器人卡片，点「扫码授权」——扫码成功后 AppID 与 AppSecret 自动填入。
3. 填入管理员 openid 与主群 group_openid（不知道可在群里发送 `/get openid` 查询），启用并保存。

未上线前可先用沙箱环境测试：把卡片中的 `sandbox` 打开，请求走 `sandbox.api.bot.qq.com`。更多细节见 [QQ 官方机器人开发专题](../development/qqofficial-dev.md)。

## AstrBot 全平台快速路径

把群服互通扩展到 Telegram / Discord / 微信 / 飞书等平台：

1. 安装 AstrBot 插件端 `astrbot_plugin_lumenbridge`，重启 AstrBot。
2. LumenBridge 连接配置页添加 `astrbot` 类型卡片，目标地址填 `ws://<AstrBot机器IP>:6200`，两端 Access Token 一致。
3. 在 AstrBot 插件端的 `umo_list` 中添加要桥接的会话（群聊或私聊）。

消息链路：`游戏 ⇄ LumenBridge ⇄ (OneBot v11 / WS) ⇄ astrbot_plugin_lumenbridge ⇄ AstrBot ⇄ 各平台`。平台用户自动分配虚拟 QQ 号并持久化，重启不漂移。详见[连接与适配器](../guide/adapters.md#astrbot-类型)。
