# 常见问题（FAQ）

## 连接问题

### 机器人连不上？

按顺序排查：

1. `/lumen status` 查看适配器状态（已连接 / 未连接 / 未启用）
2. **OneBot 正向 WS**：确认协议端（NapCat 等）的 WS 服务器地址与端口能被服务器访问，`target` 以 `ws://` 或 `wss://` 开头
3. **OneBot 反向 WS**：确认 LumenBridge `listen_port` 与协议端配置的「反向 WS 上报地址」一致；反向适配器之间端口不能冲突
4. `access_token` 两端一致；不用 token 时两边都留空
5. **QQ 官方**：AppID/AppSecret 无空格；沙箱机器人要开 `sandbox`；群里必须先添加机器人为群机器人
6. 连接日志被静默了？把适配器 `suppress_connection_log` 设为 `false` 看全部日志

### NapCat 该配正向还是反向？

任选其一：

- NapCat 开「WebSocket 服务器」→ LumenBridge 用**正向**（`ws_type: 0`，target 填 NapCat 地址）
- NapCat 开「WebSocket 客户端」→ LumenBridge 用**反向**（`ws_type: 1`，NapCat 反向地址填 `ws://127.0.0.1:3002`，端口对应 `listen_port`）

### QQ 官方机器人在群里没反应？

官方平台限制：**群聊里必须 @ 机器人**才会推送消息事件（`GROUP_AT_MESSAGE_CREATE`）。另外检查：

- 群管理员是否开启了「接收主动消息」（关闭后机器人无法主动发言，会有 `GROUP_MSG_REJECT` 事件）
- 主动消息有官方额度限制
- `main_group` 填的是 **group_openid**（可用群内 `/get openid` 查询），不是群号

### 怎么查群号 / openid？

群里发送：

```
/get openid
```

返回本群 ID 与发送者 ID；@ 某成员再发送则返回该成员 ID（个人号域为 QQ 号）。

## 消息同步问题

### 群消息没进游戏？

1. 来源群在适配器 `main_group` 列表里吗？
2. `sync.chat_to_server_enable` 开了吗？
3. 被聊天屏蔽词 `block` 模式拦了？看 WebUI 聊天过滤页
4. 超过 `max_message_length`（256）会被截断而不是丢弃，长消息看尾部 `...`

### 游戏聊天没进群？（装了聊天美化插件时）

聊天美化插件会**取消**原生聊天事件再重发格式化文本。默认 `chat.forward_cancelled = "auto"` 下，LumenBridge 通过广播回溯探针自动识别重发型插件并照常转发；若你的美化插件把消息打散变形（逐字插颜色码）或用 `send_message` 逐玩家重发，探针识别不到，改为主配置：

```json
"chat": { "forward_cancelled": "always" }
```

代价是被禁言/屏蔽的消息也会转发。详见[群服互通与聊天](chat.md#聊天转发策略-forward_cancelled)。

### @ 机器人显示 @某某 而不是玩家名？

`at_format` 中被 @ 者只有在**白名单绑定过**时才显示 Xbox 玩家名，未绑定显示 QQ 号/openid。

## 白名单问题

### 绑定白名单提示失败？

- 玩家名含非法字符（仅允许字母/数字/空格/下划线/连字符/点号，1–32 位）
- 玩家名已被其他人绑定（Xbox 名全域唯一）
- BDS 返回「已在白名单」→ 说明游戏侧早已有此玩家，直接绑定失败；先在服务器控制台 `allowlist remove` 再试
- 回复详情里含服务器原始输出，按提示处理

### 退群没自动移除白名单？

- `whitelist.remove_on_leave` 开了吗？
- 退群事件来自哪个适配器：绑定记录分域存储，QQ 群退群只匹配个人号域（QQ 号）记录，官方域（openid）记录要官方群踢人才触发
- 机器人需要有权限收到退群通知（OneBot 协议端需开启群事件上报）

## WebUI 问题

见[Web 管理面板](webui.md#常见问题)：忘记密码、面板打不开、登录失效。

## 子插件问题

### 子插件没加载？

1. `/lumen plugins` 看列表状态
2. 目录结构对吗？`plugins/lumenbridge/plugins/<名字>/main.py` + `lumen.json`，详见[子插件开发入门](../development/subplugin-getting-started.md)
3. `lumen.json` JSON 语法错误 / 缺 `name` 字段都会拒绝加载，看启动日志
4. 依赖缺失？`/lumen pip install <子插件名>` 按 lumen.json 自动装依赖

### 改了子插件代码怎么生效？

不用重启：

```
/lumen reload
```

或 WebUI 子插件页单点热重载。差量热重载：只重建有变更的子插件，其余保持运行。注意同名冲突会被拒绝（防止旧目录模块成为孤儿）。

### 子插件声明依赖其它 Endstone 插件，警告「无法获取服务器插件列表」？

v1.0.4 已修复此问题。若仍出现，确认使用的是最新版 LumenBridge。详见[manifest 与依赖](../development/manifest-and-deps.md)。

## pip 问题

### 安装依赖提示受保护包冲突？

子插件依赖与 Endstone 运行时闭包（如 `aiohttp`、`numpy`）或基础设施（`pip`、`setuptools`）冲突时拒绝安装——这些包被卸载/顶替会破坏服务器本体。联系子插件作者换依赖。

### pip 装包很慢 / 超时？

中文环境首次生成配置时自动填了腾讯镜像源；可自行更换 `pip.index_url`，或调大 `pip.timeout`（默认 300 秒）。

## 更新问题

### 框架更新怎么生效？

自动更新只**暂存**（下载 + 校验），需显式确认：

```
/lumen update framework
```

更新完成后按提示重启服务器。详见[插件市场与更新](marketplace.md#框架自更新-updates)。

### 为什么不自动生效？

框架热更新会重建全服插件运行时，风险高，设计为只允许管理员在确认时机触发。

## 调试手段

- `debug: true`（主配置）输出聊天链路诊断日志（`[chatdbg]`，含探针窗口快照）
- 正则引擎 `admin_debug: true` 在命中规则时打印规则名
- WebUI 实时日志页（SSE）观察全链路
- 配置/规则/白名单文件写坏会被自动备份为 `.corrupt` / `.bak`，可人工恢复
