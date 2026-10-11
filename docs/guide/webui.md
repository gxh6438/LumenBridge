# Web 管理面板

LumenBridge 内置一个纯标准库实现的 Web 管理面板（无任何第三方 Web 框架依赖），默认监听 `http://127.0.0.1:8300`。

## 访问与登录

- **地址**：`http://<服务器IP>:8300`（默认 `127.0.0.1` 仅本机可访问）
- **密码**：默认配置 `webui.password = "*"`，首次启动时自动生成 8 位随机密码并打印到控制台：

  ```
  [WebUI] 管理面板密码: ab3Xk9Zq
  ```

- **公开接口**：登录接口、健康检查 `/api/health`、界面语言接口、登录页背景图接口以外，全部 API 要求携带有效 token。

开放外网访问：

```
/lumen webui-host public
```

或把主配置 `webui.host` 改为 `0.0.0.0`。开放前务必确认已设置强密码。

## 页面功能

| 页面 | 功能 |
|------|------|
| 总览 | 适配器连接状态、服务器 CPU/内存占用、在线玩家、机器人资料；适配器卡片的增删改与群绑定密钥签发入口 |
| 核心配置 | `config.json` 全部字段的表单化编辑（含聊天转发策略、白名单、正则引擎、WebUI、pip、市场等分节）；聊天屏蔽词过滤管理也在配置区 |
| 正则 | 正则触发规则的增删改查与测试，详见[正则触发引擎](regex-engine.md) |
| 白名单 | QQ↔Xbox 绑定列表（合并双域展示）、添加/解绑 |
| 子插件 | 已装子插件列表、启用/禁用/热重载、上传 zip 或从 URL 安装、依赖安装，详见[子插件](../development/subplugin-getting-started.md) |
| 包管理 | pip 包列表、安装/卸载、镜像源配置 |
| 实时日志 | 服务端日志实时流（SSE），支持按级别过滤与搜索 |
| 插件市场 | 浏览/搜索市场插件、一键安装/更新、QQ 官方扫码登录辅助，详见[插件市场与更新](marketplace.md) |

## 鉴权机制

### 密码存储

- 管理员密码以 **PBKDF2-SHA256**（24 万次迭代 + 随机盐）哈希后落盘，格式 `pbkdf2_sha256$<迭代数>$<盐>$<摘要>`
- 旧版明文密码兼容：登录校验走恒时比较（`hmac.compare_digest`），明文值不自动迁移（避免误改配置）
- 迭代数设有上限（200 万），防止配置被人为塞入天文数字造成每次登录的 DoS

### 登录 token

- 登录成功签发 HMAC-SHA256 签名 token（`payload.signature`，payload 含签发与过期时间），有效期 **24 小时**
- token 携带**版本号**：修改密码、`/lumen password`、密钥变更都会使版本 +1，所有已签发 token 立即失效
- 签名密钥 `webui.secret` 留空时自动生成随机密钥

## 主要 API 一览

全部 JSON 接口位于 `/api/` 前缀（需登录，除标注公开的接口）：

| 接口 | 方法 | 说明 |
|------|------|------|
| `/api/health` | GET | 健康检查（公开） |
| `/api/auth/login` | POST | 登录换取 token（公开） |
| `/api/overview` | GET | 总览数据（适配器状态、玩家、机器人资料） |
| `/api/server/metrics` | GET | 服务器 CPU/内存指标 |
| `/api/config` | GET/PUT | 主配置读写（PUT 后自动热生效） |
| `/api/connections` | GET/POST/PUT/DELETE | 适配器卡片 CRUD |
| `/api/connections/reload` | POST | 重载连接 |
| `/api/connections/bindkey` | POST | 签发群绑定密钥 |
| `/api/qqofficial/qr/create`、`/qr/poll` | POST | QQ 官方扫码获取凭证辅助 |
| `/api/rules` | GET/PUT | 正则规则读写 |
| `/api/whitelist` | GET/POST/DELETE | 白名单绑定管理（`/domains` 查双域） |
| `/api/chat_filter` | GET/PUT | 聊天屏蔽词配置与词条（`/import` 导入词库） |
| `/api/subplugins` | GET | 子插件列表；`/reload` 热重载；`/install/upload`、`/install/url` 安装 |
| `/api/pip/*` | GET/POST | pip 列表/安装/卸载/配置 |
| `/api/market/*` | GET/POST | 市场列表/安装/检查更新/更新全部/点赞/上报 |
| `/api/updates/*` | GET/POST | 框架更新的检查/暂存/应用 |
| `/api/reload` | POST | 全量重载（等价 `/lumen reload`） |
| `/api/logs` | GET | 历史日志缓冲 |
| `/api/logs/stream` | GET | 实时日志 SSE 流 |

## 服务器指标采集

内置指标采集器每 3 秒采样一次 CPU 与内存：

- 装有 `psutil` 时走 psutil；未安装时 Linux 直接读 `/proc`，Windows 走系统 API，均不依赖第三方包
- 无法采集时（平台不支持）返回原因说明，面板显示「不可用」而不是报错

## 安全设计

- **登录失败无提示差异**：统一错误响应，不泄露用户名/密码是否存在
- **目录穿越防护**：静态文件服务以 `relative_to` 校验路径，拒绝越界读取
- **密钥掩码**：适配器 `access_token` / `app_secret` 读取时以等长 `*` 掩码返回，提交掩码自动保留原值；需查看明文走单独的 reveal 接口
- **SSE 日志隔离**：随机密码等敏感信息只走 stdout，不进入面板可回看的日志流
- **SIGPIPE 屏蔽**：仅屏蔽本线程的 SIGPIPE（客户端中途断开不崩服务端）
- **线程模型**：HTTP 服务运行在独立线程，所有游戏世界操作经 `run_on_main` 调度回主线程，后台线程绝不直接触碰 Level/PluginManager

## 常见问题

**忘了密码？** 控制台执行 `/lumen password`（无参数生成随机密码）或 `/lumen password 新密码`。

**面板打不开？** 确认 `webui.enable` 为 true、端口未被占用、防火墙放行；远程访问需先切换 host（见上文）。

**登录后一直跳回登录页？** 服务器重启后 `webui.secret` 若为空会重新生成，旧 token 全部失效，重新登录即可；修改过密码也会使旧 token 失效。
