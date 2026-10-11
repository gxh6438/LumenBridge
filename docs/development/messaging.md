# 消息与动作（OneBot 层）

本文介绍 LumenBridge 与 QQ 协议端之间的消息构造与 OneBot v11 动作调用，是子插件收发消息的底层机制。Context 层的封装见 [Context API 参考](context-api.md)。

核心模块：

| 模块 | 职责 |
|------|------|
| `onebot/message.py` | 消息段构建器 + 合并转发构建器 + `format_message` 归一化 |
| `onebot/packets.py` | OneBot v11 动作数据包构建器 |
| `onebot/adapter.py` | 单适配器：连接维持、发送队列、API 回执 |
| `onebot/hub.py` | 多适配器枢纽：智能路由 + 兼容单适配器的 API 门面 |

## 消息段构建器

`lumen.msgbuilder`（即 `onebot.message`）提供 OneBot v11 消息段的快速构造函数：

| 函数 | 消息段 | 说明 |
|------|--------|------|
| `text(raw)` | `{"type": "text", ...}` | 纯文本 |
| `at(qq)` | `at` | @ 某人，传 `"all"` @ 全体（需权限） |
| `face(face_id)` | `face` | QQ 表情 |
| `image(file, sub_type=0)` | `image` | 图片，见下方说明 |
| `reply(message_id)` | `reply` | 引用回复，通常插在消息段列表首位 |
| `poke(qq)` | `poke` | 戳一戳 |
| `video(file)` | `video` | 视频 |
| `record(file)` | `record` | 语音 |

`image()` 的 `file` 参数支持四种形态：

- **URL 字符串**：直接透传协议端下载；
- **bytes**：自动转 `base64://` 编码内联发送；
- **本地路径（白名单内）**：自动读取并转 `base64://`；
- **本地路径（白名单外）**：按原始字符串透传，协议端按其规则处理。

本地文件读取受**根目录白名单**保护（`set_local_image_roots`，由主程序初始化时注入），防止用户可控的变量拼出任意路径把服务器文件外发；单图限制 10MB，超限回退为原始字符串。

```python
from endstone_lumenbridge.onebot import message as msg

lumen.QClient.send_group_msg(123456, [
    msg.at(10086),
    msg.text(" 你在吗？"),
    msg.image("https://example.com/cat.jpg"),
])
```

### format_message 归一化

所有发送接口在底层都会经过 `format_message(msg)`：

- 字符串 → 自动包装为 `text` 段；
- 单个 dict 段 → 自动包装为列表；
- `None` / 非法类型段 → 直接丢弃（避免序列化出 `null` 段被协议端拒绝）。

因此发送消息时可以直接传字符串、段、或两者的混合列表，无需手动构造完整结构。

### decode_cq_entities

`decode_cq_entities(raw)` 解码 OneBot `raw_message` 中的 HTML 实体（`&#91;` `&#93;` `&#44;` `&amp;`），供聊天转发链路还原原始文本。

## 合并转发消息

`ForwardMessageBuilder` 链式构建合并转发节点：

```python
fwd = (
    msg.ForwardMessageBuilder()
    .add_custom_message("玩家A", 10086, "服务器人满了吗？")
    .add_custom_message("玩家B", 10087, [msg.text("满了，排队中")])
    .build()
)
lumen.QClient.send_group_forward_msg(123456, fwd)
```

| 方法 | 说明 |
|------|------|
| `add_message_by_id(message_id)` | 引用已有消息作为节点 |
| `add_custom_message(name, uin, content)` | 自定义昵称/QQ号的虚构节点，`content` 走 `format_message` |
| `build()` | 返回节点列表（浅拷贝） |

对应的动作包：`send_group_forward_msg`（群）/ `send_private_forward_msg`（私聊）。

## 动作包构建器

`lumen.packbuilder`（即 `onebot.packets`）把 OneBot 动作封装为函数，返回 `{"action", "params", "echo"}` 结构。**这些函数只是参数拼装的便捷封装——OneBot v11 协议端的全部能力（含协议端私有扩展）都可直接调用，不需要 LumenBridge 逐个适配**，完整动作清单见 [OneBot v11 标准](https://github.com/botuniverse/onebot-11/blob/master/api/public.md) 及所用协议端（NapCat / Lagrange 等）的扩展文档。

### 发送类（最常用）

| 函数 | 动作 | 说明 |
|------|------|------|
| `group_message(group_id, message)` | `send_group_msg` | 群消息 |
| `private_message(user_id, message)` | `send_private_msg` | 私聊消息 |
| `group_forward_message(group_id, messages)` | `send_group_forward_msg` | 群合并转发 |
| `private_forward_message(user_id, messages)` | `send_private_forward_msg` | 私聊合并转发 |
| `delete_message(message_id)` | `delete_msg` | 撤回消息 |
| `get_message(message_id)` | `get_msg` | 取消息详情 |
| `forward_message(message_id)` | `get_forward_msg` | 取合并转发内容 |

其余封装（群管理 `group_ban` / `group_kick`、查询 `group_member_list` / `friend_list`、群文件、加群审批等）命名规律一致：函数名 = 动作名转下划线，参数与 OneBot 规范一一对应，详见 `onebot/packets.py`。

### 通用透传（任意动作）

```python
pack = packets.build("set_input_status", {"user_id": 10086})
lumen.QClient.call_api(pack)
```

`build(action, params, echo=None)` 直接构造任意动作包。协议端新增的私有 API（如 NapCat 的 `send_poke`）无需等待 LumenBridge 更新封装，直接透传即可——**这是个人号域调用协议端全量能力的标准入口**。

> **个人号域与官方域的根本差异**：个人号域动作是「拼包→WS 发给协议端→协议端实现」，LumenBridge 只做路由不做翻译；官方域没有 OneBot 协议端，所有动作由适配器**逐个翻译**为 QQ 开放平台 REST 接口——因此官方域只有已翻译的动作可用（清单见下节），未翻译动作会告警并回调 `None`。

## 适配器发送接口

`OneBotAdapter` 的发送链路基于**异步发送队列**：`send_pack` 把包入队，常驻协程在连接就绪后逐个发出；断线期间队列挂起，重连后自动补发；队列满时淘汰最旧包，不可序列化的毒包直接丢弃。

| 方法 | 说明 |
|------|------|
| `send_pack(pack)` | 发送原始动作包（fire-and-forget），线程安全，外部线程可直接调用 |
| `call_api(pack, callback=None, timeout=10)` | 带 echo 的 API 调用，回执到达后在 WS 线程回调 `callback(data)` |
| `call_action(action, params=None, callback=None, timeout=10)` | 通用动作入口，内部构造包后走 `call_api` |
| `send_group_msg(group_id, message)` | 群消息快捷封装（内容自动 `format_message`） |
| `send_private_msg(user_id, message)` | 私聊快捷封装 |
| `send_group_forward_msg(group_id, messages)` | 群合并转发快捷封装 |
| 各 `set_*` / `get_*` 封装 | 与 packets.py 一一对应，见 `adapter.py` 方法列表 |

要点：

- **callback 回调发生在 WS 线程**，不要在回调里直接操作 Endstone 主线程对象，需要时用 `lumen.call_on_main(...)` 切换；
- `call_api` 无 callback 时不生成 echo，避免 Future 泄漏；断线时立即 `callback(None)`，不挂满超时；
- `echo` 字段用于回执配对，timeout 内未收到回执按失败处理。

## 多适配器路由（AdapterHub）

配置多个适配器时，`lumen.QClient` 拿到的是 `AdapterHub` 门面——调用方式与单适配器完全一致，路由在内部完成：

### 路由规则

1. **群定向方法**（`send_group_msg`、`set_group_ban`、`get_group_member_list` 等首参为群号的方法）按群路由：
   - 群号命中某适配器的专属群列表 → 只发这些适配器；
   - 未命中时按**双域**判定：纯数字群号 → 个人号域适配器；openid 字符串 → QQ 官方域适配器；
   - `get_*` 查询只取第一个目标，避免同一 callback 被触发多次；
2. **查询/通用方法**（`get_msg`、`get_login_info`、`call_api` 等）路由到**主适配器**（优先已连接的 websocket 实例，其次任意已连接实例）；
3. **其余发送/设置类方法**广播到全部已连接适配器；
4. **私聊**：数字 QQ 号走个人号域，openid 走官方域。

### 常用 Hub API

| 方法 | 说明 |
|------|------|
| `get(adapter_id)` | 按 ID 取适配器实例 |
| `all()` / `connected()` | 全部 / 已连接适配器列表 |
| `primary()` | 主适配器 |
| `send_group_msg` / `send_private_msg` | 按域路由发送 |
| `broadcast_group(group_id, message)` | 与 `send_group_msg` 路由一致（旧入口保留） |
| `status()` | 供 WebUI/命令展示的运行状态列表 |

Hub 通过 `__getattr__` 把未显式实现的方法按上述语义代理到适配器集合，因此**全部 OneBot API 在多适配器下均可直接使用**，子插件无感知。

## 双域与官方域差异

QQ 官方机器人适配器（`qqofficial`）没有 OneBot 协议端：**事件被翻译成 OneBot v11 格式输入，动作被翻译成开放平台 REST 接口输出**。子插件面向 OneBot 语义编程，跨域自动生效，但两域能力面不同：

| 差异点 | 个人号域 | 官方域 |
|--------|----------|--------|
| 目标标识 | QQ 号（数字） | openid（字符串） |
| 消息发送 | `send_group_msg` / `send_private_msg` | 同名方法可用（自动选被动 / 主动凭据，markdown @ 渲染） |
| @ 段 | 任意成员 | 有限（markdown 载体渲染真实 @；`@全体成员` 丢弃） |
| 引用回复 | `reply` 段构造 | **接收**翻译为 `reply` 段；**发送** `reply` 段被忽略，被动回复（msg_id 凭据）在客户端天然呈引用样式，主动消息无法构造引用 |
| 撤回 | `delete_msg`（权限内任意） | **支持** `delete_msg`：群聊（管理员可撤群员消息）+ 单聊（仅撤机器人自己发出的），均限 2 分钟内 |
| 禁言 | `set_group_ban` / `set_group_whole_ban` | **支持** `set_group_ban`（0=解除，最长 30 天，仅普通成员，需群管理员）+ `set_group_ban_batch` 批量（≤10 人）；全体禁言不支持 |
| 群管理 | 踢人 / 设管 / 改群名 / 群名片等全套 | 仅入群审批 `set_group_add_request` + 申请列表查询；踢人 / 设管等不支持 |
| 主动私聊 | 支持 | 受官方额度限制（被拒自动入补发栈，借下次被动回复补发） |
| 合并转发 | `send_group_forward_msg` 原生支持 | 无官方接口，降级为逐条发送模拟（节点上限 3） |
| 图片/视频/语音 | URL / base64 / 本地路径 | URL 或 base64 上传（单条消息单个媒体，语音映射 file） |
| 未翻译动作 | 任意动作透传（`build` + `call_api`） | 告警并回调 `None`（`call_api` 不透传，官方域无 OneBot 协议） |

官方域已翻译动作的完整清单（端点、参数语义、回调返回、平台限制）见 [QQ 官方机器人开发专题](qqofficial-dev.md)的「API 翻译参考」一节；连接配置见 [连接与适配器](../guide/adapters.md)。

## 子插件中的推荐用法

```python
def on_load(lumen):
    # 快捷发送（自动归一化 + 智能路由）
    lumen.QClient.send_group_msg(123456, "hello")

    # 带回执的查询
    def on_member_list(data):
        if data is None:
            return
        lumen.logger.info(f"群成员 {len(data.get('data', {}).get('members', []))} 人")

    lumen.call_action(
        "get_group_member_list",
        {"group_id": 123456},
        callback=on_member_list,
    )

    # 构造复杂消息
    from endstone_lumenbridge.onebot import message as msg
    lumen.QClient.send_group_msg(123456, [
        msg.at(10086), msg.text(" 检查一下"), msg.image("https://pic.example.com/map.png"),
    ])
```

## 相关文档

- **下一步**：[QQ 官方机器人开发专题](qqofficial-dev.md)——官方域全量动作的端点与限制
- 消息接收与 `reply` 快捷回复：[事件系统](events.md)
- 存储与调度：[Context API 参考](context-api.md)
- [Web 扩展](web-extensions.md)：扩展到面板
