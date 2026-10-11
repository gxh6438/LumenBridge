# 测试指南

LumenBridge 使用 pytest 测试套件，可在无 Endstone 服务器、无网络的离线环境运行。本文介绍如何运行测试、测试基建原理与编写约定。

## 运行测试

```bash
# 全量
python -m pytest tests/ -q

# 单个文件 / 单个用例
python -m pytest tests/test_onebot_core.py -q
python -m pytest tests/test_subplugin.py::test_xxx -q

# 按关键字筛选
python -m pytest tests/ -q -k "whitelist"
```

环境要求：Python ≥ 3.11 + pytest，无需真实 endstone 包（见下文 stub 机制）。

## 测试基建

### conftest.py 的 endstone stub

离线沙箱无法安装 endstone，`tests/conftest.py` 在收集任何测试前注入**最小 endstone 哑模块**（仅当真实 endstone 不可导入时，真实环境不生效）：

- `endstone.ColorFormat`：任意颜色属性返回空串的哑对象，不污染拼接文本；
- `endstone.command`：`Command` / `CommandSender` / `CommandSenderWrapper` 等哑类；
- `endstone.event`：全部事件类 + 与真实实现一致的 `event_handler` 装饰器（打 `_is_event_handler` / `_priority` / `_ignore_cancelled` 标记，供 `register_events` 反射扫描）；
- `endstone.plugin.Plugin`、`endstone.logger.Logger` 基类。

因此 plugin.py / whitelist.py / regex_engine.py / subplugin 等模块都能被正常导入与收集。测试需要具体行为时，在用例内**整体替换为自己的记录型替身**（fake server、fake player 等），stub 只保证「可导入」。

### tmp fixture

`conftest.py` 提供 `tmp` fixture（映射到 pytest 的 `tmp_path`），供脚本风格测试隔离读写文件系统。

## 测试文件地图

| 文件 | 覆盖 |
|------|------|
| `test_import.py` | 全模块导入冒烟 |
| `test_onebot_core.py` | 消息段构建、动作包构建器、事件分发 |
| `test_onebot_adapter.py` | 适配器收发、队列、高层 API 透传 |
| `test_qqofficial.py` | 官方适配器网关/翻译/被动凭据/绑定加密 |
| `test_qqofficial_suppress_log.py` | 官方适配器连接日志静默开关 |
| `test_event_dedup.py` | 事件去重窗口 |
| `test_subplugin.py` | 子插件 e2e 与 Context API 透传 |
| `test_subplugin_loader.py` | 子插件加载/ZIP 生命周期/热重载/开关 |
| `test_subplugin_requires.py` | requires 依赖检查（子插件 + Endstone 插件依赖） |
| `test_subplugin_reload_workflow.py` / `test_reload_broadcast.py` | 重载工作流与广播语义 |
| `test_config_and_pip.py` | 配置校验/原子写入/坏文件备份、pip 命令与管理 |
| `test_chat_absorption.py` | 聊天吸收探针 |
| `test_whitelist`（test_group_bind.py / test_join_request.py） | 白名单绑定、入群审批 |
| `test_regex_and_storage.py` | 正则引擎、变量替换、/get openid、子插件存储 |
| `test_webui.py` | WebUI e2e、连接密钥掩码、overview 域回退 |
| `test_webui_http.py` / `test_webui_design_contract.py` | WebUI HTTP 路由与设计契约 |
| `test_webui_persistence.py` | WebUI 总览/语言热生效/开关持久化/文案 |
| `test_webui_host_and_config.py` | webui-host 指令、外部手改配置热重载 |
| `test_webui_subplugin_api.py` | WebUI 子插件管理接口与配置表单标签 |
| `test_marketplace_framework_update.py` | 市场与框架两阶段更新、清单原子写 |
| `test_storage_migration.py` | 存储迁移 |
| `test_security*.py` | 鉴权、路径穿越、本地图片白名单等安全项 |
| `test_bugfixes.py` / `test_regression_fixes.py` | 历史修复回归集（按主题外的杂项） |
| `test_docs_alignment.py` | **文档与代码对齐检查**（文档中的路径/键名与实现一致） |
| `test_thread_safe_logger.py` | LoggerTee 线程安全 |

> 命名约定：测试文件一律按**主题**命名（模块/功能域），禁止按版本号追加命名（如 `test_v130_*`）——历史版本号文件已全部按主题拆分合并。

## 编写约定

1. **测试替身**：不依赖真实服务器/网络。服务器用记录型 fake（方法调用记入列表断言），网络用 monkeypatch 替换 HTTP/WS；
2. **隔离文件系统**：涉及配置/数据文件的用例一律用 `tmp` fixture，禁止写仓库目录；
3. **异步代码**：适配器的 asyncio 循环在测试中以独立线程运行，用事件/轮询等待状态而非固定 sleep（`pytest-timeout` 兜底防挂起）；
4. **回归测试**：修 bug 时先补一条复现用例再加修复，用例命名指向行为（`test_xxx_does_yyy`）；
5. **stub 兼容**：新代码引用 endstone 符号时，若 conftest stub 未覆盖会导致离线收集失败——需要同步扩充 stub（保持与真实 API 同名同签名）。

## 文档对齐测试

`test_docs_alignment.py` 校验 docs/ 中的关键事实（目录名、清单文件名、配置键、命令名等）与源码一致。修改涉及用户可见名称的功能时：

1. 先改代码与既有测试；
2. 同步更新 docs/ 对应章节；
3. 跑 `python -m pytest tests/test_docs_alignment.py -q` 确认对齐。

## 相关文档

- [文档中心首页](../index.md)：全部文档地图（本篇为开发文档末篇）
- [项目结构](project-structure.md)：tests/ 与 scripts/ 清单
- [架构总览](architecture.md)：被测模块的职责边界
