# 子插件清单与依赖声明

每个子插件通过目录内的 `lumen.json` 清单描述自身元数据与依赖。本文完整定义清单字段、依赖检查流程与加载顺序，供插件作者与维护者参考。目录结构与最小示例见 [子插件开发入门](subplugin-getting-started.md)。

## 清单字段

```json
{
    "name": "my_plugin",
    "version": "1.0.0",
    "author": "you",
    "desc": "示例子插件",
    "load": true,
    "priority": "main",
    "min_v": "",
    "dependencies": [],
    "requires": {
        "subplugins": [],
        "endstone": []
    }
}
```

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `name` | string | 是 | 子插件名；缺省时以目录名自动生成（并原子写回清单） |
| `version` | string | 否 | 版本号，默认 `1.0.0`；参与依赖版本比较与市场展示 |
| `author` | string | 否 | 作者 |
| `desc` | string | 否 | 一句话描述，显示在日志与 WebUI |
| `load` | bool | 否 | `false` 时跳过加载（WebUI 的启用/禁用开关就是写这个字段，原子写回） |
| `priority` | string | 否 | `pre` / `main` / `post` 三段加载顺序，默认 `main` |
| `min_v` | string | 否 | 最低 LumenBridge 版本要求；不满足拒绝加载 |
| `dependencies` | string[] | 否 | 第三方 pip 依赖，如 `["openai>=1.0.0", "pillow"]` |
| `requires` | object | 否 | 插件级强制依赖，见下文 |

### 清单容错

- 清单损坏（非法 JSON / 非 UTF-8 / 非对象）返回空清单并记错误日志，**绝不阻断其他子插件加载**；
- 清单缺失时按默认清单生成并原子写回（tmp + `os.replace`），防进程中断留下截断文件；
- 子插件名仅允许 `A-Za-z0-9_-`，防止路径穿越。

## 依赖声明体系

子插件有三层依赖声明，检查时机与失败行为各不相同：

| 层级 | 字段 | 依赖对象 | 缺失时 |
|------|------|----------|--------|
| pip 依赖 | `dependencies` | Python 包 | 拒绝加载，报缺失清单（市场安装流程会自动装齐） |
| 框架版本 | `min_v` | LumenBridge 本体 | 拒绝加载，报最低版本要求 |
| 插件级依赖 | `requires` | 其它子插件 / Endstone 插件 | 子插件缺失或不满足版本 → 拒绝加载；Endstone 插件缺失 → 拒绝加载并提示安装 |

### requires 语法

```json
"requires": {
    "subplugins": ["economy>=1.2.0", "chat-helpers"],
    "endstone": ["endstone-some-plugin>=2.0"]
}
```

- 支持六种比较符：`>=` `<=` `>` `<` `==` `!=`；仅写名称时只要求存在；
- **宽容格式**：`requires` 直接给数组时视为 `{"subplugins": [...]}`；
- 单项格式非法（如写了比较符没写版本、带空格等）时跳过该项并记警告日志，不影响其他项；
- 版本比较为**宽松口径**：仅去 `v/V` 前缀，每段取前导数字（`1.2` 与 `1.2.0` 不视为相等，段数多者更大）。

### 依赖检查流程

子插件每次加载（启动、热重载、市场安装后）按固定顺序检查：

1. 刷新 importlib 依赖缓存（新装的 pip 包立即可见）；
2. `dependencies` → `pip_manager.missing_dependencies()` 检查，缺一则拒绝加载；
3. `min_v` → 与当前 LumenBridge 版本比较，不满足拒绝加载；
4. `requires` → 两类检查：
   - **Endstone 插件**：读取 `server.plugin_manager.plugins` 构建「插件名（小写）→ 版本」映射，缺失或版本不符 → 拒绝加载并提示安装。服务器对象不可用（如极早期加载阶段）时记「无法获取服务器插件列表，本次跳过该项检查」警告、不阻断——这是唯一的非阻断分支；
   - **子插件**：要求被依赖插件**已加载**且版本满足；声明依赖自己时跳过；
5. 全部通过后创建 `LumenContext`、清缓存并导入 `main.py`，调用 `on_load(lumen)`。

检查失败时子插件仍会记录在管理列表中（`loaded=false` + `error`），WebUI 可展示失败原因与「装依赖」入口。

## 依赖补全与 requirements.txt

`requires` 缺失导致加载失败时，WebUI 子插件卡片会出现「补全依赖」入口。点击后自动探测当前可用的补全方式，弹窗按依赖类型分组展示。

子插件可在自己目录内提供可选的 `requirements.txt`，为每个依赖声明下载直链（名称大小写不敏感，链接可留空，支持 `#` 注释与空行）：

```ini
[subplugins]
Economy = https://cdn.example.com/economy.zip
helper =

[endstone]
HomeAPI = https://cdn.example.com/homeapi.whl
```

两类依赖的补全方式与完成行为不同：

| 依赖类型 | 可选方式 | 安装位置 | 完成后 |
|----------|----------|----------|--------|
| 子插件依赖 | 市场安装（已在市场发布才显示）· 链接下载（requirements.txt 提供才显示）· 手动上传 .zip | `plugins/lumenbridge/plugins/` | 自动热重载该子插件 |
| Endstone 插件依赖 | 链接下载（requirements.txt 提供才显示）· 手动上传 .whl / .zip | 服务器根目录 `plugins/`（非 LumenBridge 子插件目录） | 询问是否全服热重载（等同 `/reload`，期间网页短暂离线后自动刷新；也可暂不重载，稍后手动执行） |

- 文件损坏或缺失时按无链接处理，不影响其它补全方式；
- 链接下载沿用 WebUI 安装链接的安全约束：初始链接需在 `webui.install_url_allow_hosts` 白名单内（未配置则不限制），可选 sha256 校验；链接 30x 跳转到其它地址（如 CDN）不受初始白名单限制；
- 市场方式会补装**全部**可从市场获取的缺失依赖；个别依赖装不齐时任务结果会如实标注缺失清单，不会假报成功。

## 加载顺序

1. 扫描 `plugins/lumenbridge/plugins/` 下全部含 `lumen.json` 的目录；
2. 按 `priority` 分 `pre` → `main` → `post` 三段；
3. 段内按 `requires.subplugins` 做**稳定拓扑排序**，被依赖者先加载；
4. 循环依赖时剩余插件按原顺序追加加载，由各自的检查步骤暴露循环错误。

依赖声明只影响**同批次**（同 priority 段）的排序；跨段依赖请把被依赖者放到更早的段（如 `pre`）。

## 安装包安全约束

市场安装与本地导入 ZIP 时强制执行（详见 [插件市场](../guide/marketplace.md)）：

- 目录名合法性校验（`_NAME_RE`），解析路径必须在子插件根目录内（防 `..` 与符号链接穿越）；
- ZIP 炸弹防护：条目数 ≤ 2000、单文件 ≤ 64MB、总解压体积 ≤ 64MB。

## 相关文档

- **下一步**：[国际化](i18n.md)——多语言打磨
- [子插件开发入门](subplugin-getting-started.md)：目录结构与最小示例
- [Context API 参考](context-api.md)：`on_load(lumen)` 可用的全部接口
- [生命周期与热重载](lifecycle.md)：`on_load` / `on_unload` 调用时机与清理语义
