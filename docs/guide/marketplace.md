# 插件市场与更新

LumenBridge 内置插件市场客户端与框架自更新机制，配置在主配置的 `marketplace` 与 `updates` 两节。

## 插件市场

WebUI「插件市场」页浏览市场在售/免费的 LumenBridge 子插件，一键安装、更新、点赞与举报。

### 配置

```json
"marketplace": {
    "enable": true,
    "api_url": "https://market.mxcraft.vip",
    "allow_http": false,
    "timeout": 30,
    "max_download_bytes": 67108864,
    "check_on_start": true,
    "check_interval_seconds": 21600
}
```

| 字段 | 默认 | 说明 |
|------|------|------|
| `enable` | `true` | 市场功能总开关 |
| `api_url` | 见上 | 市场站点根地址，只填根地址自动补全 `/api/v1` 前缀 |
| `allow_http` | `false` | 是否允许非 HTTPS 站点；生产环境应保持 `false`，仅本地调试可显式开启 |
| `timeout` | `30` | 市场 API 请求超时（秒） |
| `max_download_bytes` | 64 MB | 下载包大小上限 |
| `check_on_start` | `true` | 启动时检查更新 |
| `check_interval_seconds` | `21600` | 定期检查间隔（默认 6 小时） |

### 安装方式

三种入口，最终都走同一套安全安装流程：

1. **市场页一键安装**：按市场返回的下载地址与 SHA-256 安装
2. **上传 zip**：子插件页本地上传
3. **URL 安装**：子插件页填直链下载

### 安装安全设计

- **不信任网络下载**：市场插件必须核对发布记录中的 **SHA-256**，校验失败拒绝安装
- **ZIP 路径穿越防护**：解包前检查每个条目路径，拒绝绝对路径与 `../` 逃逸条目
- **SSRF 防护**：URL 下载仅允许白名单同主机 + 重定向主机固定 + 大小/类型限制
- **依赖自动安装**：安装前 dry-run 检查 pip 依赖冲突，无法验证时绝不继续
- **防并发更新**：框架更新任务持专用锁，避免 wheel 互相覆盖、备份错乱

### 插件级依赖（requires）自动安装

市场安装不只处理 pip 依赖，还处理子插件 lumen.json 声明的插件级依赖 `requires`：

- 缺失的 Endstone 插件：收集进结果提示用户手动安装
- 缺失的市场子插件依赖：像 pip 依赖一样**自动递归下载安装**（递归深度上限 8 层，超过视为循环依赖拒绝）

### 访客互动

点赞/举报等匿名写操作通过市场会话完成（CSRF 校验）；访客身份 token 持久化在数据目录，避免每次换新身份导致点赞无法取消。

## 子插件更新

两种方式：

```
/lumen update <子插件名>     # 更新单个
/lumen update -A             # 更新全部市场来源的子插件
```

或在 WebUI 子插件页/市场页操作。仅市场来源（lumen.json 运行期标记 `_market.source == "marketplace"`）的子插件支持命令更新；版本比较按补 0 对齐（`1.2` == `1.2.0`）。

## 框架自更新（updates）

LumenBridge 自身支持在线更新，配置：

```json
"updates": {
    "enable": true,
    "api_url": "https://market.mxcraft.vip",
    "timeout": 30,
    "auto_update": true
}
```

| 字段 | 默认 | 说明 |
|------|------|------|
| `enable` | `true` | 框架更新功能开关 |
| `api_url` | 见上 | 更新源站点根地址，自动补全 `/api/v1/updates/lumenbridge` |
| `timeout` | `30` | 请求超时（秒） |
| `auto_update` | `true` | 发现新版本**自动下载校验并暂存**，但**不自动生效** |

### 两阶段更新流程

框架更新采用**暂存 → 确认**两阶段，避免热重载影响全服插件：

1. **暂存（stage）**：启动检查/手动检查发现新版本后，后台下载 wheel 并校验，暂存到数据目录，日志与 WebUI 提示有可用更新
2. **确认（apply）**：管理员显式触发才生效——

   ```
   /lumen update framework
   ```

   或 WebUI 更新提示弹窗确认。应用时自动备份旧版本，更新完成后按提示重启服务器。

> 框架更新**不能**在运行期经任务调度自动执行（会导致调度器悬垂崩溃），因此被设计为只能在命令/WebUI 显式触发。

## pip 包管理

`pip` 配置节：

```json
"pip": {
    "enable": true,
    "index_url": "",
    "timeout": 300
}
```

| 字段 | 默认 | 说明 |
|------|------|------|
| `enable` | `true` | pip 功能总开关 |
| `index_url` | 见下 | 镜像源，留空用官方 PyPI |
| `timeout` | `300` | pip 操作超时（秒，10–3600） |

`index_url` 智能默认值：首次生成配置时，界面语言为中文自动填腾讯云镜像源（`https://mirrors.cloud.tencent.com/pypi/simple`），英文留空用官方源。**一旦手动设置过（哪怕设为空字符串）就不再被自动覆盖**。

### 受保护包

pip 卸载与安装冲突检查使用**受保护包名单**——Endstone 运行时依赖闭包（32 项）+ 基础设施：

```
endstone、aiohttp、certifi、numpy、psutil、rich、requests、yaml、
pip、setuptools、wheel、websockets、endstone-lumenbridge …
```

- 卸载受保护包直接拒绝
- 安装前 dry-run 发现与受保护包冲突即中止，防止子插件依赖把运行时顶掉
- 卸载后自动清理指向已删除文件的失效模块缓存

### 操作入口

- WebUI「包管理」页：列表/安装/卸载/镜像源配置
- 游戏命令 `/lumen pip install|list|uninstall`，详见[命令系统](commands.md#pip-python-包管理)

## 相关文档

- **下一步**：[常见问题](faq.md)
- [常见问题](faq.md)——下一步：排障
- [清单与依赖](../development/manifest-and-deps.md)——开发自己的子插件
