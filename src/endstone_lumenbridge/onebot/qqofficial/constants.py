"""QQ 官方机器人协议常量。

集中管理网关协议（OP 码 / 会话原因 / 关闭码）、事件订阅位（Intents）、
被动回复窗口与限额、发送重试矩阵等数值，便于对照官方文档统一维护：
https://bot.q.qq.com/wiki/develop/api-v2/
"""

from __future__ import annotations

# ---------------------------------------------------------------- REST 接入
# 官方统一请求地址：https://api.bot.qq.com（旧域名 api.sgroup.qq.com 已弃用）
API_DOMAIN = "api.bot.qq.com"
SANDBOX_DOMAIN = "sandbox.api.bot.qq.com"
TOKEN_URL = "https://api.bot.qq.com/app/getAppAccessToken"
HTTP_TIMEOUT = 20.0

# ---------------------------------------------------------------- 断线重连
RECONNECT_BASE_DELAY = 2.0
RECONNECT_MAX_DELAY = 60.0
# 连接间隔默认值（毫秒）：两次网关连接尝试之间的最小等待时间
DEFAULT_CONNECT_INTERVAL = 60000
# WS 关闭码语义（参考 qq-botpy）：4004 鉴权失败需重取 token；
# 4006 Resume seq 无效 / 4009 会话过期 / 9001/9005 不可恢复会话，需全新 Identify
AUTH_FAIL_CODES = {4004}
SESSION_RESET_CODES = {4006, 4009, 9001, 9005}

# 网关会话结束原因（_gateway_session 返回值）：
# ended=正常断开（连接关闭/接收结束）；reconnect=服务端要求重连（OP_RECONNECT）；
# invalid=会话无效（OP_INVALID_SESSION，已重置会话状态待全新 Identify）
SESSION_ENDED = "ended"
SESSION_RECONNECT = "reconnect"
SESSION_INVALID = "invalid"

# 网关 OP 码
OP_DISPATCH = 0
OP_HEARTBEAT = 1
OP_IDENTIFY = 2
OP_RESUME = 6
OP_RECONNECT = 7
OP_INVALID_SESSION = 9
OP_HELLO = 10
OP_HEARTBEAT_ACK = 11

# ---------------------------------------------------------------- Intents
# Intents（官方文档「事件订阅」）：默认订阅群聊+C2C、群成员事件、公域频道@、
# 频道私信、频道变更（GUILDS）、互动事件（INTERACTION）。
#
# 注意 GROUP_MEMBER_ADD/REMOVE 的订阅位官方文档存在双版本（CDN 不一致）：
#   - 事件订阅总表与多数事件页：GROUP_MEMBER_EVENT (1<<24)
#   - 部分 autogen 事件页（7/21 更新）：GROUP_AND_C2C_EVENT (1<<25)
# 稳妥起见默认同时订阅 1<<24|1<<25；若机器人无 1<<24 权限导致网关拒连，
# 适配器在连续 Identify 失败后自动摘除 1<<24 降级重连（自愈，见 adapter）。
# 其余域按需通过连接配置 extra_intents 叠加（无权限的位订阅会导致网关拒连，
# 故不默认开启）：
#   1<<1  GUILD_MEMBERS        频道成员变更（需官方开通权限后才可订阅）
#   1<<9  GUILD_MESSAGES       私域频道消息（MESSAGE_CREATE / MESSAGE_DELETE，
#                              官方 Intents 总表确认仅私域机器人可订阅）
#   1<<10 GUILD_MESSAGE_REACTIONS 表情表态（MESSAGE_REACTION_*）
#   1<<27 MESSAGE_AUDIT        消息审核（MESSAGE_AUDIT_PASS/REJECT）
#   1<<28 FORUMS_EVENT         论坛事件（FORUM_*，旧名 OPEN_FORUM_*）
#   1<<29 AUDIO_ACTION         语音房事件（AUDIO_*，官方 Intents 总表位号）
INTENT_GUILDS = 1 << 0
INTENT_GROUP_MEMBER = 1 << 24
INTENT_PUBLIC_MESSAGES = 1 << 25
INTENT_INTERACTION = 1 << 26
INTENT_PUBLIC_GUILD_MESSAGES = 1 << 30
INTENT_DIRECT_MESSAGE = 1 << 12
DEFAULT_INTENTS = (
    INTENT_PUBLIC_MESSAGES
    | INTENT_GROUP_MEMBER
    | INTENT_PUBLIC_GUILD_MESSAGES
    | INTENT_DIRECT_MESSAGE
    | INTENT_GUILDS
    | INTENT_INTERACTION
)
# Identify 连续失败多少次后自动摘除 GROUP_MEMBER 位（1<<24）降级重连
INTENT_FALLBACK_THRESHOLD = 2

# OneBot v11 无对应语义、以官方事件名（小写）原样转发给子插件的官方事件：
# 互动回调（1<<26）、消息审核（1<<27）、论坛（1<<28）、语音房（1<<29）、
# 表情表态（1<<10）、频道/子频道变更（1<<0）等。
# 子插件订阅方式：ctx.on("notice.interaction_create", handler)，raw 字段为官方原始载荷。
OFFICIAL_RAW_EVENTS = frozenset({
    # 互动事件：按钮 / 组件回调（群聊卡片消息、频道消息均可能触发）
    "INTERACTION_CREATE",
    # 消息审核结果（主动消息审核场景）
    "MESSAGE_AUDIT_PASS",
    "MESSAGE_AUDIT_REJECT",
    # 论坛事件（1<<28，需官方开通论坛权限；2026-08 文档新名 FORUM_*，保留旧名兼容）
    "FORUM_THREAD_CREATE",
    "FORUM_THREAD_UPDATE",
    "FORUM_THREAD_DELETE",
    "FORUM_POST_CREATE",
    "FORUM_POST_DELETE",
    "FORUM_REPLY_CREATE",
    "FORUM_REPLY_DELETE",
    "FORUM_PUBLISH_AUDIT_RESULT",
    "OPEN_FORUM_THREAD_CREATE",  # 官方旧名
    "OPEN_FORUM_THREAD_UPDATE",
    "OPEN_FORUM_THREAD_DELETE",
    "OPEN_FORUM_POST_DELETE",
    "OPEN_FORUM_REPLY_CREATE",
    "OPEN_FORUM_REPLY_DELETE",
    # 语音房事件（1<<29）
    "AUDIO_START",
    "AUDIO_FINISH",
    "AUDIO_ON_MIC",
    "AUDIO_OFF_MIC",
    # 表情表态（1<<10）
    "MESSAGE_REACTION_ADD",  # 兼容官方旧名
    "MESSAGE_REACTION_REMOVE",
    "ADD_REACTION",
    "DELETE_REACTION",
    # 频道 / 子频道变更（1<<0）
    "GUILD_CREATE",
    "GUILD_UPDATE",
    "GUILD_DELETE",
    "CHANNEL_CREATE",
    "CHANNEL_UPDATE",
    "CHANNEL_DELETE",
    # 频道成员变更（1<<1，需官方开通权限；ADD/REMOVE 有语义翻译，UPDATE raw 转发）
    "GUILD_MEMBER_ADD",
    "GUILD_MEMBER_REMOVE",
    "GUILD_MEMBER_UPDATE",
    # 注：私域频道消息 MESSAGE_CREATE / MESSAGE_DELETE（1<<9）已有语义翻译
    # （频道群消息 / group_recall，见 translate.py），不在此 raw 转发集内
})

# ---------------------------------------------------------------- 被动回复
# 被动回复窗口与每条消息可回复次数（官方 2026-06 文档「消息收发概述」）：
#   群聊：5 分钟内最多回复 5 次；单聊：60 分钟内最多回复 4 次（2026-01-10 起）。
# 各留时间余量避免边界失效。
PASSIVE_WINDOW_GROUP = 4.5 * 60.0
PASSIVE_WINDOW_C2C = 55.0 * 60.0
PASSIVE_MAX_SEQ_GROUP = 5
PASSIVE_MAX_SEQ_C2C = 4
# 被动凭据池（借鉴 Gensokyo 懒池）：每目标保留的 msg_id 条目上限
PASSIVE_POOL_MAX = 8
# 机器人入群事件 event_id 可作被动回复凭据（不消耗主动额度）的窗口
EVENT_ID_WINDOW = 30 * 60.0

# ---------------------------------------------------------------- 发送可靠性
# 主动消息被拒（22009）补发栈：每目标上限 / 每次借道补发条数
# （过多会挤占正常回复额度，Gensokyo 建议不超过 3）
ACTIVE_STACK_MAX = 5
ACTIVE_STACK_FLUSH = 3
# 发送重试矩阵：最大尝试次数与重试间隔（富媒体链路更长）
SEND_RETRY_MAX = 3
SEND_RETRY_DELAY_TEXT = 1.0
SEND_RETRY_DELAY_MEDIA = 3.0
# 合并转发降级模拟：逐条发送的节点上限（官方无 forward 接口，额度宝贵）
FORWARD_NODE_LIMIT = 3

# 官方业务错误码（HTTP body 内 err_code / code 字段）：
BIZ_ACTIVE_REJECTED = 22009       # 主动消息被拒（额度用尽 / 用户关闭开关）
BIZ_EVENT_ID_INVALID = 40034025   # event_id 无效
BIZ_MSG_ID_EXPIRED = 40034005     # 被动回复 msg_id 已过期

# ---------------------------------------------------------------- 富媒体
# 富媒体：OneBot 段类型 → 官方 file_type（1 图 2 视频 3 语音）
MEDIA_FILE_TYPE = {"image": 1, "video": 2, "record": 3}
# 富媒体：官方附件 content_type → OneBot 段类型。
# 前缀匹配 image/ video/ audio/；精确匹配 voice（官方语音附件无斜杠前缀）
CT_SEGMENT_PREFIX = (("image/", "image"), ("video/", "video"), ("audio/", "record"))
CT_SEGMENT_EXACT = {"voice": "record", "file": ""}
# 官方 media 接口仅支持 image / video / file 三类
CT_MEDIA_API = {"image": "image", "video": "video", "record": "file"}
# 本地文件 base64 上传上限（官方限制约 10MB，留余量）
LOCAL_MEDIA_MAX = 8 * 1024 * 1024
