# 项目结构

本文是 LumenBridge 代码库的地图，标注每个目录/模块的职责与相互关系，配合 [架构总览](architecture.md) 阅读。

## 顶层布局

```
Lumenbridge/
├── pyproject.toml            # 打包配置（endstone 插件入口点）
├── README.md                 # 项目首页（链接到 docs/）
├── CHANGELOG.md              # 版本变更记录
├── LICENSE
├── docs/                     # 本文档站（VitePress 源码 + md 内容）
├── src/endstone_lumenbridge/ # 插件源码（核心）
├── tests/                    # pytest 测试套件
├── scripts/                  # 运维与调试脚本
├── examples_plugins/         # 子插件示例（example_greeter / os）
├── package.json              # VitePress 文档站（npm run docs:build）
└── .github/workflows/        # CI（build.yml 构建 Release / docs.yml 部署文档站）
```

文档站本地预览：`npm install && npm run docs:dev`；配置在 `docs/.vitepress/config.mts`（导航、侧边栏、本地搜索），`docs/index.md` 为站点首页。

## src/endstone_lumenbridge/

### 顶层模块

| 文件 | 职责 |
|------|------|
| `plugin.py` | 插件主类 `LumenBridgePlugin`：on_enable/on_disable 编排、命令注册、跨模块粘合 |
| `config.py` | `ConfigManager`：config.json 读写、原子写入、外部修改检测 |
| `connections.py` | `ConnectionManager`：connections.json 多适配器卡片的增删改查 |
| `event_bus.py` | `EventBus`：线程安全事件总线（on/once/emit/off） |
| `i18n.py` | 国际化：三语语言包、locale 规范化、子插件命名空间 |
| `marketplace.py` | 插件市场：远端索引、安装/更新、框架两阶段更新 |
| `pip_manager.py` | pip 依赖隔离安装、受保护包名单、依赖缺失检测 |
| `vendor.py` | 内嵌依赖加载器（websockets 等按需 import） |

### onebot/ —— QQ 连接层

| 文件 | 职责 |
|------|------|
| `adapter.py` | `OneBotAdapter`：OneBot v11 WS/反向 WS 连接、发送队列、API 回执 |
| `hub.py` | `AdapterHub`：多适配器管理、双域路由、统一门面 |
| `dispatcher.py` | `EventDispatcher`：原始包 → 分层事件、去重、reply 注入 |
| `message.py` | 消息段构建器、`format_message`、合并转发构建器 |
| `packets.py` | OneBot v11 动作包构建器 |
| `qqofficial_adapter.py` | QQ 官方机器人适配器本体（网关会话/鉴权/REST） |
| `qqofficial_bind.py` | 官方域 openid 绑定辅助 |
| `qqofficial/`（子包） | constants / credentials / translate / sender / utils |

### modules/ —— 功能模块

| 文件 | 职责 | 文档 |
|------|------|------|
| `chat_sync.py` | 群 ↔ 游戏聊天同步主链路 | [群服互通](../guide/chat.md) |
| `chat_probe.py` | 聊天吸收探针（美化插件兼容） | 同上 |
| `chat_filter.py` | 双向屏蔽词过滤（wordbanks/） | 同上 |
| `whitelist.py` | 白名单自助绑定、退群自动移除 | [白名单绑定](../guide/whitelist.md) |
| `group_bind.py` | 群绑定密钥管理 | [连接与适配器](../guide/adapters.md) |
| `regex_engine.py` | 正则触发引擎 | [正则触发引擎](../guide/regex-engine.md) |
| `admin_command.py` | `/lumen` 管理命令实现 | [命令系统](../guide/commands.md) |

### subplugin/ —— 子插件运行时

| 文件 | 职责 | 文档 |
|------|------|------|
| `loader.py` | 子插件发现/加载/卸载/热重载 | [清单与依赖](manifest-and-deps.md) |
| `context.py` | `LumenContext` 子插件上下文 + 命令面板 | [Context API](context-api.md) |
| `requires.py` | requires 依赖声明解析与检查 | 同上 |

### webui/ —— 管理面板

| 文件 | 职责 |
|------|------|
| `server.py` | ThreadingHTTPServer 实现：路由、静态文件、REST API |
| `auth.py` | token 鉴权、登录限速 |
| `configform.py` | 配置表单构建器（子插件 Web 扩展） |
| `logbuffer.py` | 日志环形缓冲（面板实时日志） |
| `metrics.py` | 运行指标采集 |
| `static/` | 前端静态资源（HTML/CSS/JS） |

### 资源目录

| 目录 | 内容 |
|------|------|
| `locales/` | 语言包 `en.json` / `zh_CN.json` / `zh_TW.json` |
| `wordbanks/` | `profanity.txt` 屏蔽词库、`ads.txt` 广告词库 |
| `lib/websockets/` | 内嵌 websockets 库（避免运行期 pip 安装） |

## tests/

pytest 套件，按主题拆分（`test_onebot_*`、`test_subplugin_*`、`test_webui*`、`test_qqofficial*` 等；禁止版本号命名，见 [测试指南](testing.md)）。运行方式与编写约定见 [测试指南](testing.md)。

## scripts/

| 脚本 | 用途 |
|------|------|
| `migrate_storage.py` | 存储格式迁移 |
| `serve_test_webui.py` | 本地起 WebUI 预览（脱离服务器环境调试面板） |
| `audit_config_i18n.py` | 审计配置项与 i18n 键完整性 |
| `update_marketplace_i18n.py` | 同步市场 i18n 词条 |
| `endstone_*_probe.py` | Endstone API 兼容性探测（适配新版本） |

## CI 自动构建发版（.github/workflows/）

**build.yml** —— wheel 构建与 Release，两种触发方式：

| 触发方式 | 行为 |
|------|------|
| 推送 `v*` tag（如 `git tag v1.0.12 && git push origin v1.0.12`） | 构建 wheel → 创建对应 tag 的 Release |
| Actions 页面手动触发（workflow_dispatch，输入 tag） | 构建 wheel → Release 已存在则覆盖产物（`--clobber`）；不存在则创建（tag 缺失时在当前提交上建 tag） |

Release 正文自动取 CHANGELOG.md 中该版本段落（`## [版本号]` 到下一个版本标题之间），CHANGELOG 无对应段落时回退 GitHub 自动生成。因此发版前只需：更新 `pyproject.toml` 版本号 + CHANGELOG 追加段落 + 打 tag 推送，其余交给 CI。

**docs.yml** —— VitePress 文档站部署：`docs/**`、`package*.json` 变更推送到 main 时自动构建并发布到 GitHub Pages，也可手动触发。

## 运行期数据（服务器侧）

安装到服务器后产生，不在代码库内：

```
plugins/lumenbridge/
├── config.json               # 主配置
├── connections.json          # 适配器卡片
├── data/
│   ├── command_palette.json  # 子插件命令面板
│   └── <子插件名>/            # 各子插件 storage 数据
├── plugins/                  # 子插件目录（每插件一个子目录 + lumen.json）
└── cache/                    # 市场缓存等
```

## 相关文档

- [架构总览](architecture.md)：分层与线程模型
- [测试指南](testing.md)：如何运行与编写测试
