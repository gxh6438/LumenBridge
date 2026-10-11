# 配置详解（config.json）

配置文件位于 `plugins/lumenbridge/config.json`，存放全局基础配置。连接相关的配置（适配器、管理员、群列表、群服互通）**不在**这个文件里，而是按适配器卡片存放在 `connections/` 目录，详见[连接与适配器](adapters.md)。

## 加载与校验机制

了解以下机制有助于安全地手动编辑配置：

- **缺失键自动补全**：加载时递归合并默认配置，旧版本配置文件不会因为新增字段而失效。
- **未知键拒绝**：写入时校验补丁形状，未知键或类型错误会报错拒绝，不会落盘。
- **跨字段校验**：端口范围、超时范围等在加载与保存时校验，非法值回退默认并输出错误。
- **损坏自保护**：手动编辑出现 JSON 语法错误时，坏文件自动备份为 `config.json.corrupt`，内存现值保留；校验失败回退默认前自动备份为 `config.json.bak`。
- **外部修改检测**：插件按 `(mtime, size)` 指纹检测 `config.json` 是否被手动修改，WebUI 会自动热生效并刷新页面，无需重启。
- **原子写入**：先写临时文件再 `os.replace`，进程中断不留半截文件。

## 全部字段

### 顶层字段

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `debug` | bool | `false` | 调试模式，开启后输出调试日志（含聊天链路诊断 `[chatdbg]`） |
| `language` | string | `"auto"` | 界面语言：`auto` 自动检测 / `zh_CN` / `zh_TW` / `en`，详见[国际化](../development/i18n.md) |
| `chat.forward_cancelled` | string | `"auto"` | 被其他插件取消的聊天事件如何转发到群，详见[群服互通与聊天](chat.md#聊天转发策略-forward_cancelled) |

### whitelist（白名单）

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `enable` | bool | `true` | 是否启用白名单功能 |
| `auto_add` | bool | `true` | 绑定后是否自动执行游戏 `allowlist add`，关闭则只记录 |
| `bind_keyword` | string | `"绑定白名单"` | 群内触发绑定的关键字 |
| `unbind_keyword` | string | `"解绑白名单"` | 群内触发解绑的关键字 |
| `remove_on_leave` | bool | `true` | 玩家退群时是否自动移除其游戏白名单 |

详见[白名单绑定](whitelist.md)。

### regex_engine（正则引擎）

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `enable` | bool | `true` | 是否启用正则引擎 |
| `only_on_main` | bool | `true` | 仅在主群触发规则，关闭则所有群都触发 |
| `admin_debug` | bool | `false` | 调试模式，命中规则时在日志打印规则名 |
| `command_timeout` | float | `5.0` | `executeCommand` 动作超时，范围 0.1–60 秒 |

详见[正则触发引擎](regex-engine.md)。

### webui（Web 管理面板）

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `enable` | bool | `true` | 是否启用 WebUI |
| `host` | string | `"127.0.0.1"` | 监听地址；`0.0.0.0` 允许外网访问（注意安全），也可用 `/lumen webui-host` 切换 |
| `port` | int | `8300` | 监听端口，范围 1–65535 |
| `password` | string | `"*"` | 登录密码；`*` 表示启动时随机生成并打印到控制台 |
| `secret` | string | `""` | Token 签名密钥，留空自动生成 |

详见[Web 管理面板](webui.md)。

### background（登录页背景图）

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `enable` | bool | `true` | 是否启用自定义背景图 |
| `api_url` | string | `"https://t.alcy.cc/fj"` | 随机图片 API，需直接返回图片内容（非 JSON 包装） |
| `blur_strength` | int | `0` | 毛玻璃模糊强度 0–100，0 不模糊 |
| `fallback_to_default` | bool | `true` | 获取失败时是否回退内置默认背景 |
| `cache_seconds` | int | `600` | 背景图缓存时间，范围 30–86400 秒 |

### pip（Python 包管理）

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `enable` | bool | `true` | 是否启用 pip 包管理 |
| `index_url` | string | 见下 | pip 镜像源，留空用官方 PyPI |
| `timeout` | int | `300` | pip 操作超时，范围 10–3600 秒 |

`index_url` 有智能默认值：首次生成配置时，界面语言为中文自动填腾讯云镜像源（`https://mirrors.cloud.tencent.com/pypi/simple`），英文留空用官方源。**一旦手动设置过（哪怕设为空字符串）就不再被自动覆盖**。详见[插件市场与更新](marketplace.md#pip-包管理)。

### marketplace（插件市场）

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `enable` | bool | `true` | 是否启用插件市场 |
| `api_url` | string | `"https://market.mxcraft.vip"` | 市场站点根地址，自动补全 `/api/v1` |
| `allow_http` | bool | `false` | 是否允许 HTTP 明文访问市场，生产环境务必保持 `false` |
| `timeout` | int | `30` | 请求超时，范围 5–120 秒 |
| `max_download_bytes` | int | `67108864` | 单个插件包最大下载体积，范围 1–128 MiB |
| `check_on_start` | bool | `true` | 启动时检查子插件更新 |
| `check_interval_seconds` | int | `21600` | 定期检查间隔，范围 60 秒–7 天 |

### updates（框架更新检查）

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `enable` | bool | `true` | 是否启用更新检查 |
| `api_url` | string | `"https://market.mxcraft.vip"` | 检查 API 根地址，自动补全 `/api/v1/updates/lumenbridge` |
| `timeout` | int | `30` | 请求超时，范围 5–120 秒 |
| `auto_update` | bool | `true` | 发现新版本自动下载校验并**暂存**；不会自动生效——热重载影响全服插件，必须由管理员经 `/lumen update framework -y` 或 WebUI 确认触发 |

### commands（命令权限）

控制游戏内玩家使用 `/lumen` 命令的权限。控制台与 OP 天然拥有全部权限，以下开关只影响普通玩家：

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `allow_in_game` | bool | `false` | 允许游戏内玩家使用 `/lumen`（总开关） |
| `status.allow_player` | bool | `false` | 允许非 OP 玩家执行 `/lumen status` |
| `reload.allow_player` | bool | `false` | 允许非 OP 玩家执行 `/lumen reload` |
| `say.allow_player` | bool | `false` | 允许非 OP 玩家执行 `/lumen say` |
| `plugins.allow_player` | bool | `false` | 允许非 OP 玩家执行 `/lumen plugins` |
| `update.allow_player` | bool | `false` | 允许非 OP 玩家执行 `/lumen update` |
| `pip.allow_in_game` | bool | `false` | 允许游戏内使用 `/lumen pip`（即使开启也仅限 OP） |
| `pip.allow_player` | bool | `false` | pip 的玩家开关（实际仍受 OP 限制） |

`/lumen password` 与 `/lumen webui-host` 仅限控制台执行，无玩家开关。详见[游戏内命令](commands.md)。

## 完整默认配置

```json
{
    "debug": false,
    "chat": { "forward_cancelled": "auto" },
    "language": "auto",
    "whitelist": {
        "enable": true,
        "auto_add": true,
        "bind_keyword": "绑定白名单",
        "unbind_keyword": "解绑白名单",
        "remove_on_leave": true
    },
    "regex_engine": {
        "enable": true,
        "only_on_main": true,
        "admin_debug": false,
        "command_timeout": 5.0
    },
    "webui": {
        "enable": true,
        "host": "127.0.0.1",
        "port": 8300,
        "password": "*",
        "secret": ""
    },
    "background": {
        "enable": true,
        "api_url": "https://t.alcy.cc/fj",
        "blur_strength": 0,
        "fallback_to_default": true,
        "cache_seconds": 600
    },
    "pip": { "enable": true, "index_url": "", "timeout": 300 },
    "marketplace": {
        "enable": true,
        "api_url": "https://market.mxcraft.vip",
        "allow_http": false,
        "timeout": 30,
        "max_download_bytes": 67108864,
        "check_on_start": true,
        "check_interval_seconds": 21600
    },
    "updates": {
        "enable": true,
        "api_url": "https://market.mxcraft.vip",
        "timeout": 30,
        "auto_update": true
    },
    "commands": {
        "allow_in_game": false,
        "status": { "allow_player": false },
        "reload": { "allow_player": false },
        "say": { "allow_player": false },
        "plugins": { "allow_player": false },
        "update": { "allow_player": false },
        "pip": { "allow_in_game": false, "allow_player": false }
    }
}
```

## 相关文档

- **下一步**：[连接与适配器](adapters.md)——把机器人连进服务器
- [游戏内命令](commands.md)——命令权限的实际效果
- [热重载](../development/lifecycle.md)——配置修改如何生效
