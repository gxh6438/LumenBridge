# 生命周期与热重载

本文描述 LumenBridge 主插件与子插件从启动到停用的完整生命周期，包括热重载机制与框架自动清理语义。

## 主插件生命周期

### on_enable 初始化顺序

Endstone 调用 `LumenBridgePlugin.on_enable()` 后按固定顺序初始化（模块间存在依赖，顺序不可调换）：

1. 记录主线程 ID、注入本地图片白名单（仅允许读插件数据目录）；
2. 日志三通（`LoggerTee`）：后台线程日志调度回主线程输出，避免与 Windows replxx 竞争崩溃；
3. `ConfigManager` → i18n → `EventBus`；
4. `ConnectionManager`（连接卡片）→ `AdapterHub` → `EventDispatcher`；
5. 功能模块：白名单、聊天同步、正则引擎、聊天过滤、群绑定、管理命令；
6. `EnvPool` + `SubPluginManager.load_all()` —— **子插件在这里加载**；
7. `MarketplaceClient` + 市场检查/自动更新线程（不在主线程做网络）；
8. WebUI 启动（先补注册子插件暂存的 Web 扩展）；
9. `hub.sync_from_manager()` 启动适配器连接。

### on_disable 清理顺序

按相反依赖方向逐项清理，**每步独立 try/except**，单步异常不阻断后续清理：

1. 停市场检查线程；
2. WebUI 停止；
3. `subplugin_manager.unload_all()` —— 子插件全部卸载；
4. 聊天同步 `on_server_stop()`（重载 pending 时跳过）；
5. `hub.stop_all()` 断开全部适配器；
6. `bus.remove_all()` 清空事件总线。

## 子插件生命周期

### 入口函数

| 函数 | 必需性 | 调用时机 |
|------|--------|----------|
| `on_load(lumen)` | **必须** | 加载的最后一步；抛异常即加载失败并回滚该插件已注册的资源 |
| `on_unload(lumen)` | 可选 | 卸载第一步；异常仅记警告，不阻断清理 |

```python
def on_load(lumen):
    lumen.on("message.group.normal", handler)

def on_unload(lumen):
    pass  # 常规资源已由框架自动清理，这里只处理特殊外部资源
```

### 加载流程

```
发现目录 → 读 lumen.json（损坏不阻断他人）
  → priority 三段排序 + requires 拓扑排序
  → 依赖检查（pip → min_v → requires，见 manifest-and-deps.md）
  → 创建 LumenContext
  → 清 sys.modules 缓存与 __pycache__（防新旧模块并存）
  → 导入 main.py → on_load(lumen)
```

加载成功后进入管理列表（`loaded=true`）；失败时保留记录（`loaded=false` + `error`），WebUI 可见原因。

### 卸载流程

```
on_unload(lumen)          ← 可选的作者自定义清理
context._cleanup()        ← 框架自动清理（见下）
sys.modules 移除模块及子模块
从管理列表移除
```

## 框架自动清理清单

`context._cleanup()` 在**每次卸载**（包括加载失败回滚）时执行，子插件作者无需重复实现：

| 清理项 | 语义 |
|--------|------|
| 事件监听器 | `lumen.on/once` 注册的全部 handler 从总线移除 |
| 正则自定义动作 | `lumen.register_regex_action` 注册的动作类型注销 |
| MC 桥内部监听 | `mc.on` 挂到总线的 handler 移除 |
| 服务器命令 | 命令 handler 解绑；**面板声明保留**（BDS 命令表属启动期，重启后可重新绑定） |
| 全局命令登记 | 从全局命令名列表移除本插件登记 |
| 定时任务 | 经 `lumen.scheduler` 注册的任务逐个 cancel（`task.cancel()` 失败退回 `cancel_task(task_id)`） |
| Endstone 原生监听 | 分发表清空 + listener 置 `_lumen_active=False` 双保险，防热重载后回调重复触发 |
| Web 扩展 | 配置表单 / REST API / 自定义页面全部撤销 |
| i18n 翻译 | `unregister_namespace` 移除本插件命名空间 |
| context 置废 | `_disposed=True`，此后再注册事件直接抛 `RuntimeError`（防旧 context 泄漏 handler） |

> 提示：线程对象是唯一需要作者自行管理的东西——框架无法终止 Python 线程。子插件自建线程时请在 `on_unload` 里发停止信号并 `join`，或改用 `lumen.scheduler` 定时任务。

## 热重载

### 触发入口

- WebUI「子插件」页的重载按钮（单个 / 全部）；
- `/lumen` 游戏内命令与群内管理命令；
- 市场安装 / 更新成功后自动重载对应子插件。

### reload_all

全程持锁（避免中间态被并发观察）：

1. 逐个 `_unload_one`；
2. 重新 `discover()` 扫描目录；
3. 重名冲突检测（不同目录同名清单）→ 跳过并告警；
4. `load=false` 的插件记为禁用（不加载、无错误）；
5. 按依赖拓扑排序逐个加载，返回加载成功数量。

### reload_one（单个重载）

采用**先校验后卸载**策略，任何校验失败直接返回，运行中实例不受影响：

1. 校验目录存在（支持按清单名反查目录）、路径安全（防符号链接穿越）、`main.py` 存在；
2. 清单损坏 → 拒绝重载（绝不回退默认配置）；
3. 清单 `load=false` → 拒绝重载并提示已禁用；
4. 校验通过才卸载旧实例，然后以新清单加载。

### 为什么热重载是安全的

- 模块加载**禁止写字节码**且加载前清空 `sys.modules` 中的旧模块，新旧模块不会并存；
- 加载抛异常时自动执行 `context._cleanup()` 回滚已注册资源；
- 所有管理操作持锁，WebUI 的状态展示不会观察到中间态。

## 框架两阶段更新

LumenBridge 本体的市场更新采用两阶段：先下载落盘，提示「下次重启生效」。子插件更新则下载后直接热重载生效。详见 [插件市场与更新](../guide/marketplace.md)。

## 相关文档

- **下一步**：[项目结构](project-structure.md)——代码库地图
- [子插件清单与依赖声明](manifest-and-deps.md)：加载检查顺序
- [Context API 参考](context-api.md)：`on_load` 可用接口与自动清理语义
- [架构总览](architecture.md)：线程模型与模块边界
