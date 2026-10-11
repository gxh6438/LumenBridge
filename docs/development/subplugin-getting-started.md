# 子插件开发入门

LumenBridge 子插件是运行在 LumenBridge 之上的 Python 插件，可获得 QQ 事件、发消息、执行游戏命令，且支持**不重启热重载**。本文带你从零写一个最小子插件。

## 目录结构

子插件放在 LumenBridge 数据目录的 `plugins/` 子目录下，每个子插件一个文件夹：

```
plugins/lumenbridge/plugins/
└── hello/
    ├── lumen.json      # 清单（缺失时首次扫描自动生成默认值）
    └── main.py         # 入口（必须）
```

- 文件夹名仅允许字母数字下划线连字符（`.` `..` 等路径字符会被拒绝）
- 以 `.` 或 `_` 开头的文件夹被忽略
- `main.py` 必须存在，否则只告警不加载

## lumen.json 清单

```json
{
    "name": "hello",
    "version": "1.0.0",
    "author": "you",
    "desc": "我的第一个子插件",
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

| 字段 | 默认 | 说明 |
|------|------|------|
| `name` | 文件夹名 | 子插件唯一名（非法字符回退文件夹名） |
| `version` | `"1.0.0"` | 版本号 |
| `author` / `desc` | - | 作者与描述（`/lumen plugins` 与 WebUI 展示） |
| `load` | `true` | 是否加载；`false` 表示禁用（保留目录不执行代码） |
| `priority` | `"main"` | 加载顺序段：`pre` → `main` → `post` |
| `min_v` | `""` | 最低 LumenBridge 版本要求，如 `"1.0.4"`；不满足拒绝加载 |
| `dependencies` | `[]` | **pip 依赖**列表，如 `["openai>=1.0.0", "pillow"]`；缺失时拒绝加载并提示，可用 `/lumen pip install <子插件名>` 一键安装 |
| `requires` | 空 | **插件级依赖**，详见[manifest 与依赖](manifest-and-deps.md) |

清单损坏（JSON 语法错误等）时**拒绝执行其代码**（绝不回退默认 `load=true`），避免坏清单意外激活插件。

## main.py 入口

必须暴露 `on_load(lumen)`，可选暴露 `on_unload(lumen)`。参数 `lumen` 是子插件的 [Context API](context-api.md) 实例：

```python
# plugins/lumenbridge/plugins/hello/main.py

def on_load(lumen):
    lumen.logger.info("hello 插件已加载")

    def on_group_msg(pack, reply):
        text = str(pack.get("raw_message", ""))
        if text == "你好":
            reply("你好！我是由子插件回复的~", True)

    lumen.bus.on("message.group.normal", on_group_msg)


def on_unload(lumen):
    lumen.logger.info("hello 插件已卸载")
```

把服务器跑起来，QQ 群里发送「你好」即可收到回复。

### 最小例子的拆解

- `pack`：OneBot v11 事件包（dict），字段见[事件参考](events.md)
- `reply(text, at)`：便捷回复函数，第二个参数表示是否 @ 触发者
- 事件监听通过 `lumen.bus`（事件总线）注册，详见[事件系统](events.md)

## 进阶示例：绑定游戏命令

```python
def on_load(lumen):
    def cmd_hello(sender, args):
        sender.send_message("Hello from subplugin!")
        return True

    lumen.plugin.register_command(
        "hello",
        cmd_hello,
        description="子插件示例命令",
        usages=["/hello [args: message]"],
    )
```

游戏内执行 `/hello` 即可。命令注册的规则与限制（BDS 命令表冻结、usage 语法）详见[Context API · 注册游戏命令](context-api.md#注册游戏命令)。

## 加载流程

启动/重载时按以下顺序处理：

1. **发现**：扫描 `plugins/` 目录，补全缺失的 lumen.json（自动生成默认清单）
2. **排序**：按 `priority`（pre → main → post）+ 依赖拓扑排序（被依赖的先加载）
3. **同名冲突检查**：两个目录声明同一个 `name` → 拒绝后加载者（防止旧目录模块成为孤儿，回调持续触发且无卸载入口）
4. **pip 依赖检查**：`dependencies` 有缺失 → 拒绝加载，`sp.missing_modules` 记录缺失模块名
5. **min_v 版本闸**：LumenBridge 版本不满足 → 拒绝加载
6. **requires 检查**：插件级依赖不满足 → 拒绝加载
7. **执行入口**：以独立模块名（`lumenbridge_sub_<文件夹名>`）导入 `main.py`，调用 `on_load(context)`
8. **失败清理**：`on_load` 抛异常时自动清理已注册的监听器/命令（`context._cleanup()`），模块从 `sys.modules` 移除

加载状态可在 `/lumen plugins` 与 WebUI 子插件页查看（含失败原因、缺失依赖列表）。

## 热重载

三种触发方式：

```
/lumen reload                     # 全量重载（含配置、连接、规则）
```

或 WebUI 子插件页单点「重载」。重载是**差量**的：

- 只重建有变更的子插件，其余保持运行
- 加载前清理 `__pycache__` 旧字节码，加载中禁写新缓存（pyc 时间戳粒度可能只有 1 秒，覆盖源码后旧缓存有被错误复用的风险）
- 先调用旧实例的 `on_unload`，再执行新代码的 `on_load`
- 事件监听器注册有去重保护（同一 handler 不会重复注册）

开发建议：改完代码直接 WebUI 点重载，秒级生效。

## 依赖管理

### pip 依赖

lumen.json 里声明，两种安装方式：

```
/lumen pip install hello          # 按子插件名自动装 dependencies 列表
/lumen pip install openai>=1.0.0  # 手动装包
```

安装成功后自动热重载对应子插件。受保护包（endstone 运行时闭包等）不可安装/卸载，详见[插件市场与更新 · pip](../guide/marketplace.md#pip-包管理)。

### 插件级依赖

依赖其它子插件或 Endstone 插件时用 `requires` 声明，市场安装时会自动递归补齐市场在售的子插件依赖。详见[manifest 与依赖](manifest-and-deps.md)。

## 调试

- `lumen.logger` 输出到服务器日志（WebUI 实时日志页可见）
- 加载失败时 `sp.error` 保存人类可读错误（自动识别 ModuleNotFoundError 并列出缺失模块）
- `python` 用法与普通 Python 一致；标准库之外依赖一律走 `dependencies` 声明

## 相关文档

- **下一步**：[LumenContext API 参考](context-api.md)
- [事件系统参考](events.md)——如何响应消息
