# 架构总览

本文描述 LumenBridge 的整体架构、分层职责与线程模型，是开发文档的总纲。

## 分层结构

```
┌─────────────────────────────────────────────────────┐
│                    Endstone 服务端                    │
├─────────────────────────────────────────────────────┤
│  plugin.py  插件入口（生命周期 / 命令 / 调度 / 模块装配） │
├────────────────┬────────────────────┬───────────────┤
│  连接层         │      功能模块层     │    扩展层      │
│  connections   │  modules/chat_sync │  subplugins/  │
│  onebot/hub    │  modules/whitelist │   (子插件)     │
│  onebot/adapter│  modules/regex_*   │  webui/       │
│  onebot/       │  modules/chat_*    │  marketplace  │
│   qqofficial/  │  modules/group_bind│  pip_manager  │
├────────────────┴────────────────────┴───────────────┤
│  event_bus（线程安全事件枢纽） · config · i18n · vendor │
└─────────────────────────────────────────────────────┘
```

## 模块清单

| 文件/目录 | 职责 |
|-----------|------|
| [plugin.py](https://github.com/gxh6438/LumenBridge/blob/main/src/endstone_lumenbridge/plugin.py) | 插件入口：生命周期、`/lumen` 命令、`run_on_main` 调度、模块装配、聊天探针接线 |
| [config.py](https://github.com/gxh6438/LumenBridge/blob/main/src/endstone_lumenbridge/config.py) | 主配置加载/校验/原子写入/外部修改检测 |
| [connections.py](https://github.com/gxh6438/LumenBridge/blob/main/src/endstone_lumenbridge/connections.py) | 适配器卡片管理（CRUD、校验、按类型分文件持久化、群/管理员并集缓存） |
| [event_bus.py](https://github.com/gxh6438/LumenBridge/blob/main/src/endstone_lumenbridge/event_bus.py) | 线程安全事件总线（on/once/emit/off） |
| [i18n.py](https://github.com/gxh6438/LumenBridge/blob/main/src/endstone_lumenbridge/i18n.py) | 国际化：locale 规范化、语言检测、`t()` 翻译 |
| [onebot/](https://github.com/gxh6438/LumenBridge/blob/main/src/endstone_lumenbridge/onebot/) | OneBot v11 适配器体系（见下文） |
| [modules/](https://github.com/gxh6438/LumenBridge/blob/main/src/endstone_lumenbridge/modules/) | 七个功能模块（见下文） |
| [subplugin/](https://github.com/gxh6438/LumenBridge/blob/main/src/endstone_lumenbridge/subplugin/) | 子插件加载器、Context API、依赖检查 |
| [webui/](https://github.com/gxh6438/LumenBridge/blob/main/src/endstone_lumenbridge/webui/) | 纯标准库 Web 管理面板 |
| [marketplace.py](https://github.com/gxh6438/LumenBridge/blob/main/src/endstone_lumenbridge/marketplace.py) | 插件市场与更新客户端 |
| [pip_manager.py](https://github.com/gxh6438/LumenBridge/blob/main/src/endstone_lumenbridge/pip_manager.py) | pip 依赖隔离与受保护包机制 |
| [vendor.py](https://github.com/gxh6438/LumenBridge/blob/main/src/endstone_lumenbridge/vendor.py) | 内置 vendored 依赖装载（websockets） |

## OneBot 适配器体系

```
onebot/
├── hub.py                 # AdapterHub：多适配器门面（统一收发入口）
├── adapter.py             # OneBotAdapter：WS 正向/反向适配器（个人号域/AstrBot 共用）
├── dispatcher.py          # 事件分发：原始上报 → 事件包 → 总线
├── message.py             # OneBot 消息段构造器（at/image/text…）
├── packets.py             # 动作包构造器（send_group_msg / set_group_ban…）
├── qqofficial_adapter.py  # QQ 官方网关适配器
└── qqofficial/            # 官方协议实现（网关/凭据/签名/事件翻译/发送器）
    ├── credentials.py     # AppID/Secret 凭据、扫码辅助
    ├── translate.py       # 官方事件 → OneBot v11 事件包翻译
    ├── sender.py          # 官方 API 发送器（主动消息额度/被动回复序号管理）
    ├── utils.py           # 官方消息段解析
    └── constants.py       # Intents、被动消息窗口等协议常量
```

**双域设计**：QQ 官方事件由翻译层统一转为 OneBot v11 形态后进入同一事件通道，并打上 `domain="official"` 标记（频道事件走 `guild` 域隔离）。下游模块（白名单、正则、聊天同步）按域选择身份标识（QQ 号 vs openid），其余逻辑完全复用。

## 功能模块

| 模块 | 文件 | 说明 |
|------|------|------|
| 聊天同步 | `chat_sync.py` | 双向消息同步 + 服务器事件通知（详见[群服互通](../guide/chat.md)） |
| 白名单 | `whitelist.py` | QQ↔Xbox 绑定，事务式游戏命令（详见[白名单](../guide/whitelist.md)） |
| 正则引擎 | `regex_engine.py` | 规则匹配与动作执行（详见[正则引擎](../guide/regex-engine.md)） |
| 聊天过滤 | `chat_filter.py` | 双向敏感词过滤（mask/block） |
| 聊天探针 | `chat_probe.py` | 识别美化插件取消聊天后重发的场景 |
| 群绑定 | `group_bind.py` | WebUI 签发一次性密钥 + 群聊消费 |
| 管理员命令 | `admin_command.py` | 群内 `/添加管理员 @…` |

模块在插件启动时统一装配进 `plugin`，互相通过 `plugin.xxx_module` 与事件总线协作，不存在模块间直接 import 依赖。

## 事件流

### 入站（QQ → 游戏）

```
协议端/官方网关 WS 消息
  → Adapter（接收线程，asyncio/线程桥接）
  → Dispatcher（解析为事件包 pack，附 _lumen_adapter_id / domain）
  → EventBus.emit("message.group.normal", pack, reply)
      ├─ ChatSyncModule     转发进游戏（run_on_main 广播）
      ├─ WhitelistModule    关键词绑定
      ├─ RegexEngineModule  规则匹配
      ├─ ChatFilterModule / GroupBindModule / AdminCommandModule
      └─ 子插件监听器
```

### 出站（游戏 → QQ）

```
Endstone 事件（玩家聊天/进退服/死亡）或模块动作
  → ChatSyncModule._broadcast / adapter.send_group_msg
  → 各启用适配器按自己的群列表发送
```

## 线程模型

LumenBridge 是多线程系统，理解线程边界是安全开发的前提：

| 线程 | 职责 |
|------|------|
| **主线程** | 游戏世界：Endstone 事件、命令执行、广播。**唯一允许**触碰 Level/Player/PluginManager 的线程 |
| 适配器接收线程 | WS 收包与解析，事件经总线分发（监听器运行在事件线程） |
| HTTP 线程池 | WebUI 请求处理 |
| 后台线程 | 市场检查、pip 安装、框架更新下载 |

**跨线程规则**：

- 一切游戏世界操作必须经 `plugin.run_on_main(task)` 调度回主线程；主线程上调用「调度 + 阻塞等待」会**自死锁**，代码里须直接同步执行（框架内已有该防护，见 [lifecycle](lifecycle.md)）
- 后台线程输出日志必须走 tee logger（replxx 控制台非主线程写入会崩溃服务端），用 `plugin._cmd_log()`
- 共享状态（配置、连接、规则、白名单）均以锁保护；热路径用不可变快照/引用置换避免读侧持锁（`_compiled`、`get_view` 等）

## 存储布局

```
plugins/lumenbridge/
├── config.json                  # 主配置
├── connections/                 # 适配器卡片（按类型分文件）
│   ├── websocket.json
│   ├── qqofficial.json
│   └── astrbot.json
├── plugins/<name>/              # 子插件目录（main.py + lumen.json）
├── data/
│   ├── rules.json               # 正则规则
│   ├── whitelist.json           # 个人号域白名单
│   ├── whitelist_official.json  # 官方域白名单
│   ├── command_palette.json     # 子插件启动命令面板
│   ├── chat_filter/             # 聊天过滤词条与词库
│   ├── marketplace/             # 市场缓存与暂存
│   └── pip/                     # pip 环境记录
└── webui/                       # （若启用）
```

所有 JSON 写入均为「临时文件 + `os.replace`」原子替换；损坏文件自动备份 `.corrupt` / `.bak`。

## 安全边界速览

| 边界 | 机制 |
|------|------|
| WebUI | PBKDF2 密码哈希 + HMAC token（版本号失效）+ 静态文件目录穿越防护 |
| 子插件 zip | 路径穿越检查 + SHA-256 校验（市场来源）+ SSRF 防护 |
| pip | 受保护包名单 + dry-run 冲突检测 + 安装路径串行锁 |
| 正则 | ReDoS 启发式 + 长度截断 + pattern 负缓存 |
| 群绑定密钥 | 内存态 + 一次性消费 + 5 分钟 TTL + 恒时比较 |

## 相关文档

- **下一步**：[子插件快速入门](subplugin-getting-started.md)
- [项目结构](project-structure.md)——代码库地图
