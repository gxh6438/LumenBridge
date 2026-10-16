# 第三方声明（Third-Party Notices）

本项目（LumenBridge）的部分模块衍生自以下开源项目，并遵循其原始许可证分发。
本项目整体以 Apache License 2.0 授权（见 [LICENSE](LICENSE)）。

## SparkBridge3

- 项目地址：<https://github.com/SparkBridge/SparkBridge3>
- 许可证：Apache License 2.0
- 上游仓库未附带版权声明行与 NOTICE 文件，特此注明来源以示尊重。

### 衍生部分（已由 JavaScript 移植为 Python，适配 Endstone 平台并做修改与扩展）

- 正则触发引擎（`src/endstone_lumenbridge/modules/regex_engine.py`）的规则数据模型：
  `rules.json` 的字段结构（`triggerType` / `pattern` / `flags` / `eventType` /
  `conditions` / `actions`）、条件三元组模型、内置动作命名
  （`callPluginCommand` / `executeCommand` / `replyText` 等）
  与默认规则场景，源自 SparkBridge3 的 `plugins/sb3_regex`。
- 群服互通（`src/endstone_lumenbridge/modules/chat_sync.py`、
  `src/endstone_lumenbridge/connections.py`）的同步配置键名
  （`chat_to_server_enable` 等）与默认消息模板文案
  （`[玩家] %s 进服` / `[群聊] %s: %s` / `[表情]` / `[图片]` 等），
  源自 SparkBridge3 的 `plugins/sb3_mc`。
- 事件命名体系（`message.group.normal` 等）沿用 SparkBridge3 的命名方式
  （本项目子插件为 Python 编写，SparkBridge3 的 JavaScript 插件无法直接运行）。

### 主要修改

- 由 Node.js/JavaScript 实现移植为 Python 实现，运行平台由 LeviLamina/BDS
  迁移至 Endstone；
- 正则引擎新增规则优先级、阻断（`block`）短路、ReDoS 防护、额外内置变量
  （`$rawMessage` / `$card` / `$role` / `$messageId` / `$selfId` / `$domain`）与
  热重载；
- 管理员角色体系独立实现（后台管理员角色为 `lumenadmin`，无迁移逻辑）；
- 多适配器并行引擎、QQ 官方机器人适配器、AstrBot 中继、WebUI、子插件体系、
  插件市场、pip 包管理等为 LumenBridge 原创实现。

## endstone-qqsync-plugin

- 项目地址：<https://github.com/yuexps/endstone-qqsync-plugin>
- 许可证：README 声明 MIT（截至本文撰写时仓库内暂缺 LICENSE 文件；本项目未复制其代码，仅参考不受版权保护的设计思路）
- 参考范围（均为设计思路，未复制代码）：
  - 在 Endstone 插件中以独立线程运行 asyncio 事件循环、经
    `run_coroutine_threadsafe` 桥接主线程的线程模型；
  - 有界发送队列与断线暂存、重连补发的发送缓冲思路；
  - 指数退避加随机抖动的重连策略；
  - 将 websockets 库内置打包以实现零 pip 依赖的分发方式
    （双方各自独立内置不同版本，均来自上游 python-websockets）。
- 本项目相关模块为独立实现，类 / 方法命名、代码结构与注释均不相同。

## 内置第三方组件

- `src/endstone_lumenbridge/lib/websockets/`：内置打包的
  [websockets](https://github.com/python-websockets/websockets) 库（BSD-3-Clause），
  见该目录内 `version.py` 与上游仓库。
- `src/endstone_lumenbridge/webui/static/prism/`：[Prism](https://prismjs.com/)
  语法高亮（MIT）。
- `src/endstone_lumenbridge/webui/static/qrcode.min.js`：qrcode-generator
  （MIT, Kazuhiko Arase）。
