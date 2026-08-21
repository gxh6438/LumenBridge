"""正则触发引擎：基于 rules.json 规则库对群消息与游戏事件做正则匹配并执行动作。"""

from __future__ import annotations

import json
import re
import shutil
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from ..onebot.message import at as at_segment
from ..onebot.message import image as image_segment
from ..i18n import t as _t

if TYPE_CHECKING:
    from ..plugin import LumenBridgePlugin

# 规则动作去抖：同一规则对同一事件指纹在窗口内只执行一次动作，
# 防止上游事件重复（多链路上报 / 协议端重发）导致回复发送两次
_ACTION_DEDUP_WINDOW = 5.0
_ACTION_DEDUP_MAX = 512


DEFAULT_RULES: list[dict[str, Any]] = [
    {
        "id": "rule_query_whitelist",
        "name": "查白名单",
        "enabled": True,
        "triggerType": "message",
        "pattern": "^查白名单",
        "flags": "i",
        "eventType": "",
        "conditions": [],
        "actions": [
            {"type": "callPluginCommand", "params": "getXboxID,$at"},
            {"type": "replyText", "params": "查询结果：$xbox"},
        ],
        "block": True,
    },
    {
        "id": "rule_query_server",
        "name": "查服",
        "enabled": True,
        "triggerType": "message",
        "pattern": "^查服$",
        "flags": "i",
        "eventType": "",
        "conditions": [],
        "actions": [
            {"type": "executeCommand", "params": "list"},
            {"type": "replyText", "params": "$result"},
        ],
        "block": True,
    },
    {
        "id": "rule_admin_execute",
        "name": "管理员执行命令",
        "enabled": True,
        "triggerType": "message",
        "pattern": "^执行(.+)",
        "flags": "",
        "eventType": "",
        "conditions": [
            {"field": "userRole", "operator": "==", "value": "sparkadmin"}
        ],
        "actions": [
            {"type": "executeCommand", "params": "$1"},
            {"type": "replyText", "params": "执行结果：$result"},
        ],
        "block": True,
    },
    {
        "id": "rule_welcome",
        "name": "入群欢迎",
        "enabled": True,
        "triggerType": "event",
        "pattern": "",
        "flags": "",
        "eventType": "group.member_join",
        "conditions": [],
        "actions": [
            # replyAtText：先真实 @ 进群成员再发文本
            {"type": "replyAtText", "params": " 欢迎新成员！发送 绑定白名单<你的游戏ID> 即可进服游玩"},
        ],
        "block": False,
    },
]

# 旧版内置欢迎动作（纯文本、无 @）：仅当存量 rules.json 仍是该原样默认时
# 自动升级为 replyAtText 版本；被用户改过的规则绝不触碰
_LEGACY_WELCOME_ACTIONS = [
    {"type": "replyText", "params": "欢迎新成员！发送 绑定白名单<你的游戏ID> 即可进服游玩"},
]

_JS_FLAG_MAP = {"i": re.IGNORECASE, "m": re.MULTILINE, "s": re.DOTALL}

# 内置命令：/get openid [@成员|QQ号]
_OPENID_CMD_RE = re.compile(r"^/get\s+openid(?:\s+(.*))?$", re.IGNORECASE)

# ReDoS 防护：规则 pattern 来自可被网页编辑的 rules.json，恶意或失误的
# 灾难性回溯 pattern（嵌套量词 / 超大重复下界）会长时间阻塞匹配线程甚至主线程。
_MAX_PATTERN_LEN = 512        # pattern 最大长度
_MAX_MATCH_TEXT_LEN = 2000    # 参与匹配的消息文本最大长度
# 危险重复限定：{4位数及以上} 的有界重复（如 a{100000}）或嵌套量词（如 (a+)+）
_HUGE_REPEAT_RE = re.compile(r"\{\d{4,}")
# 量词 + 组结束 + 量词，如 (a+)+ / (a*)?{2} / (?:a+)*
_NESTED_QUANT_RE = re.compile(r"[+*}]\s*\)\s*[+*{]")


def _is_risky_pattern(pattern: str) -> bool:
    """启发式检测灾难性回溯风险 pattern。"""
    if _HUGE_REPEAT_RE.search(pattern):
        return True
    if _NESTED_QUANT_RE.search(pattern):
        return True
    return False


def compile_pattern(pattern: str, flags: str) -> re.Pattern:
    """将 JS 风格 flags 字符串转换为 Python re 标志并编译"""
    py_flags = 0
    for ch in flags or "":
        py_flags |= _JS_FLAG_MAP.get(ch, 0)
    return re.compile(pattern, py_flags)


class RegexEngineModule:
    """消息 / 事件正则触发引擎"""

    def __init__(self, plugin: "LumenBridgePlugin") -> None:
        self.plugin = plugin
        self.logger = getattr(plugin, "_tee_logger", None) or plugin.logger
        self.bus = plugin.bus
        self.adapter = plugin.adapter

        # 运行数据统一存放于 data/ 子目录（见迁移脚本 migrate_storage.py）
        self.path = Path(plugin.data_folder) / "data" / "rules.json"
        self.rules: list[dict[str, Any]] = self._load_rules()
        self.custom_actions: dict[str, Callable[..., Any]] = {}
        # 已编译 pattern 缓存：key = flags + pattern；非法 / 高风险 pattern
        # 缓存为 None（负缓存）。玩家聊天事件在主线程匹配，逐条重复编译 +
        # ReDoS 风险扫描会直接消耗 TPS；规则仅在 load/reload/save 时变化。
        self._pattern_cache: dict[str, "re.Pattern[str] | None"] = {}

        self._action_dedup: OrderedDict[tuple[str, ...], float] = OrderedDict()
        self._action_dedup_lock = threading.Lock()

        self.register_action("getXboxID", self._action_get_xbox_id)

        # 无条件注册事件，enable 在 handler 内实时检查（handle_message /
        # handle_event 开头均有开关判断）：/lumen reload 重载配置后立即生效
        self.bus.on("message.group.normal", self._on_group_message)
        self.bus.on("notice.group_increase", self._on_group_increase)
        self.bus.on("notice.group_decrease", self._on_group_decrease)

    @property
    def conf(self) -> dict[str, Any]:
        """引擎配置（实时读取：config_manager.load() 会整体替换 data
        dict，缓存旧引用会导致 /lumen reload 后配置永不生效）。"""
        return self.plugin.config_manager.regex_engine

    @staticmethod
    def _pack_fingerprint(trigger: str, event_type: str, pack: dict[str, Any]) -> tuple[str, ...]:
        """触发源指纹：消息按 message_id（缺失时按用户+内容+时间），事件按类型+用户+群+时间。"""
        if trigger == "message":
            mid = pack.get("message_id")
            if mid is not None:
                return ("m", str(mid), str(pack.get("self_id", "")))
            return (
                "m",
                str(pack.get("user_id", "")),
                str(pack.get("group_id", "")),
                str(pack.get("raw_message", ""))[:128],
                str(pack.get("time", "")),
            )
        return (
            "e",
            str(event_type),
            str(pack.get("user_id", "")),
            str(pack.get("group_id", "")),
            # 聊天类事件纳入内容，避免同一玩家短窗口内的不同发言被误判为重复
            str(pack.get("raw_message", ""))[:128],
            str(pack.get("time", "")),
        )

    def _skip_duplicated(self, rule: dict[str, Any], fp: tuple[str, ...]) -> bool:
        """同一规则对同一指纹在窗口内重复触发时返回 True（跳过动作执行）。"""
        key = (str(rule.get("id", "")),) + fp
        now = time.monotonic()
        with self._action_dedup_lock:
            while len(self._action_dedup) > _ACTION_DEDUP_MAX:
                self._action_dedup.popitem(last=False)
            seen = self._action_dedup.get(key)
            if seen is not None and now - seen < _ACTION_DEDUP_WINDOW:
                return True
            self._action_dedup[key] = now
            return False

    def _write_rules_atomic(self, rules: list[dict[str, Any]]) -> None:
        """tmp 文件 + 原子替换写 rules.json，防止进程中断留下半个文件（参考 whitelist._save_list_locked）。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_name(self.path.name + ".tmp")
        temp.write_text(
            json.dumps(rules, ensure_ascii=False, indent=4), encoding="utf-8"
        )
        temp.replace(self.path)

    def _load_rules(self) -> list[dict[str, Any]]:
        if self.path.is_file():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    # 过滤非 dict 元素（手改文件 / API 提交畸形数组）：
                    # 畸形元素会让热路径的 rule.get() 抛 AttributeError
                    cleaned = [r for r in data if isinstance(r, dict)]
                    if len(cleaned) != len(data):
                        self.logger.warning(_t("regex_engine.log.rules_invalid_entries"))
                    migrated = self._migrate_legacy_welcome(cleaned)
                    if migrated is not cleaned:
                        self._write_rules_atomic(migrated)
                        self.logger.info(_t("regex_engine.log.welcome_upgraded"))
                    return migrated
            except (json.JSONDecodeError, UnicodeDecodeError, OSError):
                # 损坏时先把原文件备份为 .corrupt 再回退默认：
                # 直接覆写会让用户全部自定义规则永久丢失
                try:
                    shutil.copy2(self.path, self.path.with_name(self.path.name + ".corrupt"))
                except OSError:
                    pass
                self.logger.error(_t("regex_engine.log.rules_parse_failed"))
        self._write_rules_atomic(DEFAULT_RULES)
        return json.loads(json.dumps(DEFAULT_RULES))

    @staticmethod
    def _migrate_legacy_welcome(rules: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """存量 rules.json 的旧版欢迎规则（纯文本）→ replyAtText（真实 @）。

        仅当 rule_welcome 的 actions 与旧内置默认完全一致（用户未改过）才
        升级；任何自定义内容保持原样。返回原列表（无变化）或新列表。
        """
        for rule in rules:
            if (
                isinstance(rule, dict)
                and rule.get("id") == "rule_welcome"
                and rule.get("actions") == _LEGACY_WELCOME_ACTIONS
            ):
                updated = json.loads(json.dumps(rules))
                for item in updated:
                    if item.get("id") == "rule_welcome":
                        item["actions"] = json.loads(json.dumps(
                            next(r for r in DEFAULT_RULES if r.get("id") == "rule_welcome")["actions"]
                        ))
                        break
                return updated
        return rules

    def reload_rules(self) -> int:
        """重载规则库，返回规则数量"""
        self.rules = self._load_rules()
        getattr(self, "_pattern_cache", None) and self._pattern_cache.clear()
        return len(self.rules)

    def save_rules(self, rules: list[dict[str, Any]]) -> None:
        """保存规则库并立即热加载（供 WebUI 调用）"""
        # 过滤非 dict 元素：畸形元素会让热路径 rule.get() 在 try 外抛
        # AttributeError，导致后续所有规则被跳过
        rules = [r for r in rules if isinstance(r, dict)]
        self._write_rules_atomic(rules)
        self.rules = rules
        getattr(self, "_pattern_cache", None) and self._pattern_cache.clear()
        self.logger.info(_t("regex_engine.log.rules_saved", count=len(rules)))

    def _compile_cached(self, pattern: str, flags: str, rule_name: Any) -> "re.Pattern[str] | None":
        """带缓存编译规则 pattern；非法 / 超长 / 高风险 pattern 返回 None。

        命中缓存时零编译成本（含负缓存），异常日志也只打印一次而不是每条消息刷屏。
        """
        # getattr 兜底：测试桩经 __new__ 构造时不走 __init__
        cache = getattr(self, "_pattern_cache", None)
        if cache is None:
            cache = self._pattern_cache = {}
        key = f"{flags}\x00{pattern}"
        if key in cache:
            return cache[key]
        regex: "re.Pattern[str] | None" = None
        if pattern and len(pattern) <= _MAX_PATTERN_LEN and not _is_risky_pattern(pattern):
            try:
                regex = compile_pattern(pattern, flags)
            except re.error:
                regex = None
        if regex is None:
            self.logger.error(_t("regex_engine.log.pattern_risky", name=rule_name))
        cache[key] = regex
        return regex

    def register_action(self, action_type: str, handler: Callable[..., Any]) -> None:
        if callable(handler):
            self.custom_actions[action_type] = handler
            self.logger.info(_t("regex_engine.log.action_registered", action_type=action_type))

    def _action_get_xbox_id(
        self, params: list[str], pack: dict[str, Any], context: dict[str, Any]
    ) -> dict[str, Any]:
        """内置动作：按 QQ 号/openid 查询绑定的 XboxID，结果写入 $xbox 变量。

        域感知：官方域事件包的 user_id / at 段 id 为 openid，绑定存于
        official 域；个人号域为 QQ 号，存于 qq 域。按事件包所属域路由
        查询，否则官 bot 下永远查不到绑定（表现为"未绑定"误报）。
        """
        qq = params[0] if params else str(pack.get("user_id", ""))
        domain = "official" if str(pack.get("domain", "")) == "official" else "qq"
        wl = self.plugin.whitelist_module
        entry = wl.get_binding_by_qq(qq, domain) if wl else None
        return {"xbox": entry["xbox"] if entry else _t("regex_engine.reply.not_bound")}

    @staticmethod
    def _get_at(message: Any) -> str | None:
        if isinstance(message, list):
            for seg in message:
                # 协议端畸形消息段（字符串 / null / data 非字典）跳过，防止 .get 抛异常
                if isinstance(seg, dict) and seg.get("type") == "at":
                    data = seg.get("data")
                    return str(data.get("qq", "")) if isinstance(data, dict) else ""
        return None

    @staticmethod
    def _get_at_excluding(message: Any, exclude: set[str]) -> str | None:
        """取第一个 @ 目标，跳过指定 id（如官方域 @机器人 自身的 AppID）。"""
        if isinstance(message, list):
            for seg in message:
                if isinstance(seg, dict) and seg.get("type") == "at":
                    qq = str((seg.get("data") or {}).get("qq", "") or "")
                    if qq and qq not in exclude:
                        return qq
        return None

    def _handle_get_openid(self, pack: dict[str, Any], msg_text: str) -> bool:
        """内置命令 /get openid：查询群 / 成员 openid，便于抄录进适配器配置。

        - 无参数：返回本群 ID 与发送者 ID；
        - @成员：返回成员 openid（个人号域为 QQ 号）；
        - QQ 号：官方域不支持（平台不提供 QQ 号↔openid 映射），个人号域即用户 ID。
        """
        match = _OPENID_CMD_RE.match(msg_text.strip())
        if not match:
            return False
        group_id = str(pack.get("group_id", "") or "")
        is_official = str(pack.get("domain", "")) == "official"
        # 官方域 @机器人 的 at 段 id 为 AppID，查询成员时需排除
        at_id = self._get_at_excluding(
            pack.get("message"), {str(pack.get("self_id", "") or "")}
        )
        arg = (match.group(1) or "").strip()
        if at_id:
            self.adapter.send_group_msg(
                group_id,
                _t("openid.member_openid" if is_official else "openid.member_qq", id=at_id),
            )
        elif not arg:
            self.adapter.send_group_msg(
                group_id,
                "\n".join(
                    [
                        _t("openid.group", id=group_id),
                        _t("openid.self", id=pack.get("user_id", "")),
                    ]
                ),
            )
        elif arg.isdigit():
            self.adapter.send_group_msg(
                group_id,
                _t("openid.qq_unsupported" if is_official else "openid.qq_domain", id=arg),
            )
        else:
            self.adapter.send_group_msg(group_id, _t("openid.usage"))
        return True

    def _parse_variables(
        self, text: str, pack: dict[str, Any], context: dict[str, Any]
    ) -> str:
        if not text:
            return ""

        def _sub(match: re.Match) -> str:
            var = match.group(1)
            if var == "userId":
                return str(pack.get("user_id", ""))
            if var == "groupId":
                return str(pack.get("group_id", ""))
            if var == "at":
                at_qq = self._get_at(pack.get("message"))
                return at_qq if at_qq else str(pack.get("user_id", ""))
            if var == "nickname":
                return str((pack.get("sender") or {}).get("nickname", ""))
            if var in context:
                return str(context[var])
            return match.group(0)

        return re.sub(r"\$([a-zA-Z0-9_]+)", _sub, text)

    def _check_conditions(
        self, conditions: list[dict[str, Any]] | None, pack: dict[str, Any]
    ) -> bool:
        if not conditions:
            return True

        for cond in conditions:
            field = cond.get("field")
            operator = cond.get("operator")
            value = cond.get("value")

            if field == "userRole":
                roles = [(pack.get("sender") or {}).get("role") or "member"]
                # 宽松字符串比较：兼容 QQ 官方域的 openid 管理员
                if str(pack.get("user_id", "")) in self.plugin.config_manager.admin_keys:
                    roles.append("admin")
                    # 向后兼容：旧配置可能使用 "sparkadmin"
                    roles.append("sparkadmin")
                ok = value in roles if operator == "==" else (
                    value not in roles if operator == "!=" else False
                )
                if not ok:
                    return False
                continue

            if field == "userId":
                actual = str(pack.get("user_id", ""))
            elif field == "groupId":
                actual = str(pack.get("group_id", ""))
            else:
                actual = str(pack.get(field, "")) if pack.get(field) is not None else ""

            if operator == "==":
                ok = actual == str(value)
            elif operator == "!=":
                ok = actual != str(value)
            elif operator == "includes":
                ok = str(value) in actual
            elif operator == "matches":
                # ReDoS 防护：与规则 pattern 同款限制——超长 / 高风险 pattern 直接
                # 判不匹配；匹配文本截断；re.error 同样返回 False
                pattern_val = str(value)
                if len(pattern_val) > _MAX_PATTERN_LEN or _is_risky_pattern(pattern_val):
                    ok = False
                else:
                    try:
                        ok = re.search(
                            pattern_val, actual[:_MAX_MATCH_TEXT_LEN], re.IGNORECASE
                        ) is not None
                    except re.error:
                        ok = False
            else:
                ok = False
            if not ok:
                return False
        return True

    def _execute_actions(
        self,
        actions: list[dict[str, Any]],
        pack: dict[str, Any],
        match_groups: list[str],
    ) -> None:
        context: dict[str, Any] = {"result": ""}
        for idx, val in enumerate(match_groups):
            context[str(idx)] = val if val is not None else ""

        group_id = pack.get("group_id") or self.plugin.config_manager.main_group

        for action in actions or []:
            # 非法动作元素（字符串 / null 等）跳过，防 .get 抛异常中断后续动作
            if not isinstance(action, dict):
                continue
            action_type = action.get("type")
            params = action.get("params", "")
            try:
                if action_type == "replyText":
                    content = self._parse_variables(params, pack, context)
                    self.adapter.send_group_msg(group_id, content)

                elif action_type == "replyAtText":
                    # 真实 @ 事件主体（如进群成员）再发文本：官方域经
                    # extract_payload 渲染为 <@openid>，个人号域发原生 at 段
                    content = self._parse_variables(params, pack, context)
                    target = pack.get("user_id")
                    if target is not None and str(target):
                        self.adapter.send_group_msg(
                            group_id, [at_segment(target), content]
                        )
                    else:
                        self.adapter.send_group_msg(group_id, content)

                elif action_type == "replyImage":
                    parsed = self._parse_variables(params, pack, context)
                    self.adapter.send_group_msg(group_id, image_segment(parsed))

                elif action_type == "deleteMessage":
                    if pack.get("message_id") is not None:
                        self.adapter.delete_msg(pack["message_id"])

                elif action_type == "muteUser":
                    parsed = self._parse_variables(str(params), pack, context)
                    try:
                        duration = int(parsed)
                    except ValueError:
                        duration = 600
                    if pack.get("user_id"):
                        self.adapter.set_group_ban(group_id, pack["user_id"], duration)

                elif action_type == "executeCommand":
                    cmd = self._parse_variables(params, pack, context)
                    context["result"] = self._run_command_capture(cmd)

                elif action_type == "callPluginCommand":
                    parts = str(params).split(",")
                    command = parts[0].strip()
                    handler = self.custom_actions.get(command)
                    if handler is None:
                        self.logger.error(_t("regex_engine.log.action_not_found", command=command))
                        continue
                    parsed = self._parse_variables(params, pack, context)
                    command_params = [p.strip() for p in parsed.split(",")[1:]]
                    ret = handler(command_params, pack, context)
                    if isinstance(ret, dict):
                        context.update(ret)
                    elif ret is not None:
                        context["result"] = str(ret)

                else:
                    self.logger.error(_t("regex_engine.log.unknown_action_type", action_type=action_type))
            except Exception as e:
                self.logger.error(_t("regex_engine.log.action_failed", action_type=action_type, error=e))

    def _command_message_text(self, message: Any) -> str:
        """把 Endstone ``Translatable`` 输出转换为可直接发送到 QQ 的文本。"""
        if isinstance(message, str):
            return message

        key = str(getattr(message, "text", "") or "")
        params = [str(value) for value in (getattr(message, "params", None) or [])]
        translated = ""
        try:
            translated = str(self.plugin.server.language.translate(message))
        except Exception:
            try:
                translated = str(self.plugin.server.language.translate(key, params))
            except Exception:
                pass

        # 某些 BDS/Endstone 组合缺少服务端语言资源，translate 仍会返回原键。
        # 对用户实际遇到的 list 两段输出提供稳定且与语言无关的兜底。
        if translated and translated not in {key, str(message)} and not translated.startswith("commands."):
            return translated
        if key == "commands.players.list":
            if len(params) >= 2:
                return _t("regex_engine.reply.online_players_count", a=params[0], b=params[1])
            return _t("regex_engine.reply.online_players_list")
        if key == "commands.players.list.names":
            names = " ".join(params).strip()
            return _t("regex_engine.reply.player_names", names=names) if names else _t("regex_engine.reply.no_online_players")
        if params:
            return _t("regex_engine.reply.key_value", key=key, value=' '.join(params)) if key else " ".join(params)
        return key or str(message)

    def _run_command_capture(self, cmd: str) -> str:
        """在游戏主线程执行命令，捕获并翻译命令输出。"""
        cmd = cmd.strip().lstrip("/")
        if not cmd:
            return _t("regex_engine.reply.empty_command")

        outputs: list[str] = []
        errors: list[str] = []
        done = threading.Event()
        result: dict[str, Any] = {"success": False, "exception": ""}

        def run() -> None:
            try:
                def capture(msg: Any) -> None:
                    try:
                        text = self._command_message_text(msg).strip()
                        if text:
                            outputs.append(text)
                    except Exception:
                        pass

                def capture_error(msg: Any) -> None:
                    try:
                        text = self._command_message_text(msg).strip()
                        if text:
                            errors.append(text)
                    except Exception:
                        pass

                from endstone.command import CommandSenderWrapper

                # Endstone 0.11：on_message / on_error 双回调捕获正常与错误输出
                sender = CommandSenderWrapper(
                    self.plugin.server.command_sender,
                    on_message=capture,
                    on_error=capture_error,
                )
                result["success"] = bool(self.plugin.server.dispatch_command(sender, cmd))
            except Exception as exc:
                result["exception"] = str(exc)
            finally:
                done.set()

        try:
            # 防自死锁：事件类规则（进服/发言等）在主线程触发，若再把 run 调度
            # 回主线程并阻塞等待，任务永远无法执行（主线程正被本调用占用），
            # 只能等超时。主线程上直接同步执行。
            # getattr 兼容测试桩等未实现该方法的插件替身对象。
            _on_main = getattr(self.plugin, "is_on_main_thread", None)
            if callable(_on_main) and _on_main():
                run()
            else:
                self.plugin.run_on_main(run)
        except Exception as exc:
            return _t("regex_engine.reply.dispatch_failed", error=exc)

        timeout = max(0.1, float(self.conf.get("command_timeout", 5.0)))
        if not done.wait(timeout=timeout):
            return _t("regex_engine.reply.wait_timeout", timeout=timeout)

        clean_errors = re.sub(r"§.", "", "\n".join(errors)).strip()
        clean_outputs = re.sub(r"§.", "", "\n".join(outputs)).strip()
        if result.get("exception"):
            return _t("regex_engine.reply.exec_exception", error=result['exception'])
        if clean_errors:
            return clean_errors

        command_name = cmd.split(None, 1)[0].casefold()
        # BDS 的 stop 会立即进入关服流程，dispatch_command 在部分版本返回 False，
        # 但没有异常或错误输出时命令已经成功提交，不能向 QQ 误报“执行失败”。
        if command_name == "stop":
            return clean_outputs or _t("regex_engine.reply.stop_executed")
        if clean_outputs:
            return clean_outputs
        return _t("regex_engine.reply.exec_success") if result.get("success") else _t("regex_engine.reply.exec_failed")

    @staticmethod
    def _rebuild_raw_message(message: Any) -> str:
        if isinstance(message, list):
            parts: list[str] = []
            for seg in message:
                if not isinstance(seg, dict) or seg.get("type") != "text":
                    continue
                data = seg.get("data")
                if isinstance(data, dict):
                    parts.append(str(data.get("text", "")))
            return "".join(parts)
        return str(message or "")

    def _on_group_message(self, pack: dict[str, Any], _reply: Any) -> None:
        if not self.conf.get("enable", True):
            return

        msg_text = self._rebuild_raw_message(pack.get("message", pack.get("raw_message", "")))
        # ReDoS 防护：截断超长文本，限制最坏回溯规模
        if len(msg_text) > _MAX_MATCH_TEXT_LEN:
            msg_text = msg_text[:_MAX_MATCH_TEXT_LEN]

        # 内置命令 /get openid：配置辅助工具，不受 only_on_main 限制
        if self._handle_get_openid(pack, msg_text):
            return

        if self.conf.get("only_on_main", True) and not self.plugin.group_allowed(pack):
            return

        for rule in self.rules:
            if not rule.get("enabled") or rule.get("triggerType") != "message":
                continue
            try:
                pattern = (rule.get("pattern") or "").strip()
                # 空 pattern 在 re.compile("") 时会匹配任意位置，
                # 导致所有消息都被命中，因此显式跳过空 pattern 的规则
                if not pattern:
                    continue
                # ReDoS 防护：超长 / 高风险 pattern 直接跳过（缓存后零重复扫描）
                regex = self._compile_cached(pattern, rule.get("flags", ""), rule.get("name"))
                if regex is None:
                    continue
                match = regex.search(msg_text)
                if not match:
                    continue
                if not self._check_conditions(rule.get("conditions"), pack):
                    continue
                if self.conf.get("admin_debug"):
                    self.logger.info(_t("regex_engine.log.rule_matched", name=rule.get('name')))
                groups = [match.group(0)] + [g or "" for g in match.groups()]
                if self._skip_duplicated(rule, self._pack_fingerprint("message", "", pack)):
                    if rule.get("block"):
                        break
                    continue
                self._execute_actions(rule.get("actions", []), pack, groups)
                if rule.get("block"):
                    break
            except Exception as e:
                # 单条规则失败只记日志，不中断后续规则执行（与 handle_event 一致）
                self.logger.error(_t("regex_engine.log.rule_exec_failed", name=rule.get('name'), error=e))

    def handle_event(self, event_type: str, pack: dict[str, Any]) -> None:
        if not self.conf.get("enable", True):
            return
        if not pack.get("sender"):
            # 注意不能原地写入共享 pack：同一事件包会被多个 handler 消费
            # （如 whitelist 的退群昵称解析依赖 sender 缺失时回退查询群名片），
            # 注入假 sender 会污染其他 handler 的判断。浅拷贝后注入。
            pack = {
                **pack,
                "sender": {
                    "role": "member",
                    "nickname": str(pack.get("user_id", _t("regex_engine.reply.unknown_user"))),
                },
            }

        for rule in self.rules:
            if (
                not rule.get("enabled")
                or rule.get("triggerType") != "event"
                or rule.get("eventType") != event_type
            ):
                continue
            try:
                pattern = (rule.get("pattern") or "").strip()
                # 玩家发言事件匹配消息内容；其他事件匹配 user_id（玩家名）
                if event_type == "server.player_chat":
                    target = str(pack.get("raw_message", ""))
                else:
                    target = str(pack.get("user_id", ""))
                # ReDoS 防护：截断 + 高风险 pattern 跳过（事件规则在主线程执行，
                # 灾难性回溯会直接冻结服务器）；编译走缓存避免逐消息重复编译
                if len(target) > _MAX_MATCH_TEXT_LEN:
                    target = target[:_MAX_MATCH_TEXT_LEN]
                if pattern:
                    regex = self._compile_cached(pattern, rule.get("flags", "i"), rule.get("name"))
                    if regex is None:
                        continue
                    match = regex.search(target)
                    if not match:
                        continue
                    groups = [match.group(0)] + [g or "" for g in match.groups()]
                else:
                    # 事件规则空 pattern 表示匹配该事件全部发生；与消息规则跳过空 pattern 不同。
                    groups = [target]

                if not self._check_conditions(rule.get("conditions"), pack):
                    continue
                if self._skip_duplicated(rule, self._pack_fingerprint("event", event_type, pack)):
                    if rule.get("block"):
                        break
                    continue
                self._execute_actions(rule.get("actions", []), pack, groups)
                if rule.get("block"):
                    break
            except Exception as e:
                self.logger.error(_t("regex_engine.log.rule_exec_failed", name=rule.get('name'), error=e))

    def _on_group_increase(self, pack: dict[str, Any]) -> None:
        if self.conf.get("only_on_main", True) and not self.plugin.group_allowed(pack):
            return
        self.handle_event("group.member_join", pack)

    def _on_group_decrease(self, pack: dict[str, Any]) -> None:
        if self.conf.get("only_on_main", True) and not self.plugin.group_allowed(pack):
            return
        self.handle_event("group.member_leave", pack)

    def _mc_event_target_group(self) -> Any:
        """MC 事件规则的回复目标群。

        main_group 只汇总个人号域数字群号（connections.all_groups 仅遍历
        websocket 适配器）；纯官方机器人部署下为 0，回复发往群 0 会被
        hub 按数字路由到个人号域后静默丢弃。此时回退到已连接适配器的
        广播目标（配置群或官方侧动态发现的群 openid），与 chat_sync
        的广播口径一致。
        """
        main = self.plugin.config_manager.main_group
        if main:
            return main
        try:
            hub = self.adapter
            for adapter in hub.connected() if hasattr(hub, "connected") else []:
                groups = list(getattr(adapter, "groups", None) or [])
                if groups:
                    return groups[0]
                broadcast = getattr(adapter, "broadcast_groups", None)
                if callable(broadcast):
                    discovered = list(broadcast())
                    if discovered:
                        return discovered[0]
        except Exception:
            pass
        return main

    def _mock_pack(self, player_name: str) -> dict[str, Any]:
        # time 参与 _pack_fingerprint 指纹：缺失时同一玩家 5 秒内（去重窗口）
        # 重复的相同发言会被误判为上游重复上报而吞掉第二次的动作
        return {
            "user_id": player_name,
            "group_id": self._mc_event_target_group(),
            "time": int(time.time()),
            "sender": {"nickname": player_name, "role": "member"},
        }

    def on_mc_player_join(self, player_name: str) -> None:
        self.handle_event("server.player_join", self._mock_pack(player_name))

    def on_mc_player_left(self, player_name: str) -> None:
        self.handle_event("server.player_left", self._mock_pack(player_name))

    def on_mc_player_chat(self, player_name: str, message: str) -> None:
        pack = self._mock_pack(player_name)
        pack["raw_message"] = message
        self.handle_event("server.player_chat", pack)
