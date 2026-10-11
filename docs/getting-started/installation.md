# 安装与升级

## 环境要求

| 项目 | 要求 | 说明 |
|------|------|------|
| Endstone | 0.11 及以上 | Minecraft 基岩版专用服务器插件框架 |
| Python | 3.11 及以上 | Endstone 内置，通常无需单独安装 |
| 协议端 | 任选其一 | NapCat / Lagrange / LLOneBot / go-cqhttp，或 QQ 官方机器人凭据 |

不需要安装任何额外的 Python 包。插件用到的 `websockets` 库已打包在 whl 内部，不会与服务器上已有的包冲突。访问插件市场、检查更新和安装 pip 依赖时会自动尊重系统代理变量（`HTTP_PROXY` / `HTTPS_PROXY`）。

## 安装步骤

1. 下载 `endstone_lumenbridge-1.0.11-py3-none-any.whl`。
2. 把 whl 文件放进 BDS 服务器的 `plugins/` 目录。
3. 启动服务器，Endstone 会自动加载插件并在 `plugins/lumenbridge/` 下生成默认配置。

首次启动后，控制台会输出 LumenBridge 的启动横幅、WebUI 访问地址和登录密码。

## 数据目录布局

启动后生成的目录结构：

```text
plugins/
├── endstone_lumenbridge-1.0.11-py3-none-any.whl
└── lumenbridge/
    ├── config.json          # 基础配置（语言、白名单、正则引擎、WebUI 等）
    ├── connections/         # 连接配置（适配器卡片，按类型分文件）
    │   ├── websocket.json   #   QQ 个人号（OneBot WebSocket）卡片
    │   ├── qqofficial.json  #   QQ 官方机器人卡片
    │   └── astrbot.json     #   AstrBot 中继卡片
    ├── subplugins/          # 已安装的子插件
    │   └── <插件名>/
    │       ├── lumen.json   #   子插件清单
    │       ├── main.py      #   入口文件
    │       └── ...          #   子插件自带的数据 / 资源文件
    └── data/                # 运行数据
        ├── rules.json       #   正则规则库
        ├── whitelist.json   #   白名单绑定数据（个人号域）
        ├── whitelist_official.json  # 白名单绑定数据（官方域）
        └── chat_filter/     #   聊天屏蔽词数据
            ├── words.json   #     词条与配置
            └── wordbanks/   #     词库文件（*.txt，每行一词）
```

所有 JSON 配置均为原子写入（先写临时文件再替换），进程中断不会留下半截文件。

## 从旧版本迁移

### v1.0.2 之前的单文件连接配置

插件首次写盘时会自动把旧的单文件 `connections.json` 拆分为 `connections/` 目录分文件存储（原文件改名为 `connections.json.migrated` 保留备份），连接不中断，无需手动操作。

### 旧版平铺数据文件搬入 data/

如需把 `rules.json`、`whitelist.json` 等旧版平铺在数据根目录的文件搬进 `data/`，使用独立迁移脚本：

```bash
# 把 scripts/migrate_storage.py 复制到 plugins/lumenbridge/ 目录下执行
python migrate_storage.py --dry   # 先预览，不落盘
python migrate_storage.py         # 确认无误后执行迁移
```

迁移前会自动把旧文件备份到 `legacy_backup/` 目录。

## 升级

### 手动升级

下载新版 whl，覆盖 `plugins/` 目录中的旧文件，重启服务器。配置文件、规则库、白名单数据和子插件全部保留。

### 通过市场在线更新

WebUI 更新页可检查并下载框架新版本。新版本下载校验后**原子暂存**（不自动生效），需要管理员在 WebUI 确认弹窗或控制台执行 `/lumen update framework -y` 触发热重载生效：

- 热重载等同 Endstone `/reload`，会重载服务器内**全部插件**，面板短暂下线后自动恢复。
- 新版本启动失败会自动回滚旧版本（自动备份最近 3 份旧 wheel）。
- 详见[插件市场与更新](../guide/marketplace.md#框架在线更新)。

## 卸载

1. 删除 `plugins/` 目录下的 whl 文件。
2. 如需彻底清除数据，删除 `plugins/lumenbridge/` 目录（含全部配置、白名单和子插件）。

卸载前建议备份 `config.json`、`connections/` 和 `data/whitelist*.json`。
