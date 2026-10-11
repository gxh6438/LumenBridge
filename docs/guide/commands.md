# 命令系统

LumenBridge 的命令分两层：**游戏内管理命令**（`/lumen` 系列，Endstone 命令）与 **QQ 群内命令**（聊天关键词/前缀触发）。

## /lumen 主命令

```
/lumen (status|reload|say|plugins|pip|update|password|webui-host)<action> [message]
```

权限 `lumenbridge.command.lumen` 默认仅 OP；`password` 与 `webui-host` 两个子命令**仅限服务器控制台**执行（见下文安全设计）。

### status — 状态总览

```
/lumen status
```

输出所有适配器的连接状态（已连接/未连接/未启用 + 主群列表）、正则规则数、白名单绑定数、子插件加载数与 WebUI 访问地址。不带参数的 `/lumen` 默认执行 status。

### reload — 全量重载

```
/lumen reload
```

依次完成：重读 `config.json` → 重初始化语言 → 重连 OneBot 适配器 → 重启 WebUI → 失效 pip 缓存 → 重载正则规则 → 差量热重载全部子插件 → 按配置决定是否启动市场检查线程。成功后汇报重载的规则数与子插件数。

配置文件手动编辑后，无需重启服务器，执行 reload 即可生效（WebUI 修改则会自动热生效）。

### say — 服→群广播

```
/lumen say 大家好
```

把消息广播到**所有适配器**的全部已绑定群，格式为 `[发送者] 消息`。机器人离线或没有任何已绑定群时报错。

### plugins — 子插件列表

```
/lumen plugins
```

列出全部子插件及状态（已加载/加载失败/禁用等）。

### pip — Python 包管理

```
/lumen pip list                 # 列出已安装包（最多显示 50 个）
/lumen pip install <包名|子插件名>
/lumen pip uninstall <包名>
```

- `install` 支持**直接填子插件名**：自动按该子插件 lumen.json 声明的 `dependencies` 安装依赖包
- 安装/卸载在后台线程执行，日志逐行回显，完成后结果回发；安装成功后自动热重载对应子插件
- 与 WebUI/市场的安装路径共用同一把串行锁，不会并发损坏环境
- 受 `pip.enable` 配置控制，关闭时提示不可用

### update — 子插件/框架更新

```
/lumen update <子插件名>     # 更新单个市场安装的子插件
/lumen update -A             # 更新全部市场安装的子插件
/lumen update framework      # 更新 LumenBridge 框架自身
```

详见[插件市场与更新](marketplace.md)。非市场来源的子插件无法通过此命令更新。

### password — 重置 WebUI 密码（仅控制台）

```
/lumen password 新密码
/lumen password              # 清除并生成随机密码，打印到控制台
```

- 仅限控制台执行：被盗 OP 不得借此接管 WebUI（可安装子插件 = 任意代码执行）
- 两种路径均使**已签发的全部登录 token 失效**
- 随机密码只走 stdout 输出，不会被 WebUI 日志/SSE 回看捕获
- WebUI 未启用时仅写入配置，下次启动生效

### webui-host — 切换监听地址（仅控制台）

```
/lumen webui-host            # 查看当前监听地址
/lumen webui-host local      # 127.0.0.1（仅本机访问）
/lumen webui-host public     # 0.0.0.0（所有网卡，含外网）
/lumen webui-host 192.168.1.10
```

写入配置立即生效：运行中重启监听 socket，未运行则供下次启动使用。切到 `0.0.0.0` 会提示安全注意。

## 游戏内命令权限控制

`commands` 配置节精细控制每个子命令能否在游戏内由玩家执行：

```json
"commands": {
    "allow_in_game": false,
    "status":   { "allow_player": false },
    "reload":   { "allow_player": false },
    "say":      { "allow_player": false },
    "plugins":  { "allow_player": false },
    "update":   { "allow_player": false },
    "pip":      { "allow_in_game": false, "allow_player": false }
}
```

| 规则 | 说明 |
|------|------|
| 控制台 | 永远放行 |
| `allow_in_game: false` | 所有 `/lumen` 子命令禁止玩家执行（默认） |
| `allow_player: true` | 允许玩家执行该子命令；仍需 OP 权限，除非额外放行 |
| `pip` | 即使 `allow_in_game` 开启，pip 子命令仍**仅限 OP**（可安装任意包 = 任意代码执行） |

## QQ 群内命令

群内命令监听已绑定群的消息，按前缀/关键词触发：

| 命令 | 说明 | 详见 |
|------|------|------|
| `绑定白名单 <Xbox名>` | 自助申请游戏白名单 | [白名单绑定](whitelist.md) |
| `解绑白名单` | 解除自己的绑定并移除白名单 | [白名单绑定](whitelist.md) |
| `/lumen绑定 <密钥>` | 消费 WebUI 签发的群绑定密钥 | [连接与适配器](adapters.md#群绑定密钥) |
| `/添加管理员 @甲 @乙` | 批量添加适配器管理员（发送者须已是管理员） | [连接与适配器](adapters.md#批量添加管理员) |

关键词（绑定白名单/解绑白名单）可在主配置中自定义；`/` 前缀命令接受简繁英多语言变体。

此外，[正则触发引擎](regex-engine.md)可以配置任意自定义的群内指令（如签到、查询），无需写代码。

## 子插件注册游戏命令

子插件可以通过 Context API 注册自定义游戏内命令（如 `/点歌`、`/绑定`），原理与限制详见[子插件命令注册](../development/context-api.md#注册游戏命令)：

- BDS 命令表在插件加载时冻结，子插件命令由 LumenBridge 的**启动命令面板**统一并入预声明，运行期绑定 handler
- 子插件热重载时命令自动重新绑定

## 相关文档

- **下一步**：[正则触发引擎](regex-engine.md)——免代码的消息自动化
- [Web 管理面板](webui.md)——浏览器侧管理
