"""QQ <-> XboxID 白名单绑定管理（双域存储）。

- 个人号域（domain="qq"）：绑定存于 whitelist.json，qid 为 QQ 号；
- QQ 官方域（domain="official"）：绑定存于 whitelist_official.json，qid 为 openid；
游戏侧命令成功后才提交本地数据，避免"提示成功但玩家仍在白名单"的错配。
"""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from ..i18n import t as _t

if TYPE_CHECKING:
    from ..plugin import LumenBridgePlugin

CommandResult = dict[str, Any]

# BDS 白名单命令输出特征：新版本 BDS 直接输出本地化 key（如 commands.allowlist.add.failed，
# 含义为"玩家已在白名单中"），旧版本 / 已装语言包时输出对应文本。两类都要识别。
_ADD_DUPLICATE_MARKERS = (
    "commands.allowlist.add.failed",
    "already in allow list",
    "already in the allow list",
    "already in allowlist",
    "already allowlisted",
    "已在白名单",
    "已在白名單",
    "已经在白名单",
    "重複的白名單",
    "重复的白名单",
)
_REMOVE_MISSING_MARKERS = (
    "commands.allowlist.remove.failed",
    "not in allow list",
    "not in the allow list",
    "not in allowlist",
    "not on the whitelist",
    "not in the whitelist",
    "不在白名单",
    "不在白名單",
    "未在白名单",
    "未在白名單",
)
_ADD_SUCCESS_MARKERS = (
    "commands.allowlist.add.success",
    "added",
    "已添加",
    "添加到白名单",
    "加入白名单",
)
_REMOVE_SUCCESS_MARKERS = (
    "commands.allowlist.remove.success",
    "removed",
    "已移除",
    "已从白名单",
    "已從白名單",
)


class WhitelistModule:
    """QQ <-> XboxID 白名单绑定管理。"""

    def __init__(self, plugin: "LumenBridgePlugin") -> None:
        self.plugin = plugin
        self.logger = getattr(plugin, "_tee_logger", None) or plugin.logger
        self.bus = plugin.bus
        self.adapter = plugin.adapter

        # 双域存储：个人号域 whitelist.json / 官方域 whitelist_official.json
        self.path = Path(plugin.data_folder) / "whitelist.json"
        self.path_official = Path(plugin.data_folder) / "whitelist_official.json"
        self._data_lock = threading.RLock()
        self._pending_qq: set[str] = set()
        self._pending_xbox: set[str] = set()
        self.bindings: list[dict[str, Any]] = self._load(self.path)
        self.bindings_official: list[dict[str, Any]] = self._load(self.path_official)

        # 无条件注册事件，enable / remove_on_leave 在 handler 内实时检查：
        # /lumen reload 重载配置后开关立即生效，无需重新挂载监听
        self.bus.on("message.group.normal", self._on_group_message)
        self.bus.on("notice.group_decrease", self._on_group_decrease)

    @property
    def conf(self) -> dict[str, Any]:
        """白名单配置（实时读取：config_manager.load() 会整体替换 data
        dict，缓存旧引用会导致 /lumen reload 后配置永不生效）。"""
        return self.plugin.config_manager.whitelist

    @staticmethod
    def _domain_of(pack: dict[str, Any]) -> str:
        """事件包所属域：官方适配器标记 domain="official"，个人号域缺省 "qq"。"""
        return "official" if str(pack.get("domain", "")) == "official" else "qq"

    @staticmethod
    def _normalize_xbox(xbox: Any) -> str:
        """去掉用户输入的外层引号并保留玩家名内部空格。"""
        name = str(xbox or "").strip()
        if len(name) >= 2 and name[0] == name[-1] == '"':
            name = name[1:-1].strip()
        return name

    def _load(self, path: Path) -> list[dict[str, Any]]:
        if not path.is_file():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            self.logger.error(_t("whitelist.log.parse_error"))
            return []
        if not isinstance(data, list):
            self.logger.error(_t("whitelist.log.not_array"))
            return []

        cleaned: list[dict[str, Any]] = []
        seen_qq: set[str] = set()
        seen_xbox: set[str] = set()
        for item in data:
            if not isinstance(item, dict):
                continue
            qid = str(item.get("qid", "")).strip()
            xbox = self._normalize_xbox(item.get("xbox"))
            xbox_key = xbox.casefold()
            if not qid or not xbox or qid in seen_qq or xbox_key in seen_xbox:
                continue
            cleaned.append({"qid": qid, "xbox": xbox})
            seen_qq.add(qid)
            seen_xbox.add(xbox_key)
        return cleaned

    def _save_list_locked(self, path: Path, bindings: list[dict[str, Any]]) -> None:
        """在持锁状态下原子替换 JSON，防止进程中断留下半个文件。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(path.name + ".tmp")
        temp.write_text(
            json.dumps(bindings, ensure_ascii=False, indent=4),
            encoding="utf-8",
        )
        temp.replace(path)

    def _store(self, domain: str) -> tuple[Path, list[dict[str, Any]]]:
        """按域取 (存储路径, 绑定列表)。"""
        if domain == "official":
            return self.path_official, self.bindings_official
        return self.path, self.bindings

    def get_binding_by_qq(self, qq: int | str, domain: str = "qq") -> dict[str, Any] | None:
        qid = str(qq)
        with self._data_lock:
            _path, bindings = self._store(domain)
            entry = next((b for b in bindings if str(b.get("qid")) == qid), None)
            return dict(entry) if entry else None

    def snapshot(self, domain: str | None = None) -> list[dict[str, Any]]:
        """返回绑定列表的线程安全副本；domain=None 时合并双域并附 domain 字段。"""
        with self._data_lock:
            if domain is not None:
                _path, bindings = self._store(domain)
                return [dict(b) for b in bindings]
            merged = [dict(b, domain="qq") for b in self.bindings]
            merged += [dict(b, domain="official") for b in self.bindings_official]
            return merged

    def get_qq_by_xbox(self, xbox: str, domain: str | None = None) -> str | None:
        """按玩家名查绑定标识；domain=None 时跨双域查询。"""
        xbox_key = self._normalize_xbox(xbox).casefold()
        with self._data_lock:
            stores = [self._store(domain)] if domain else [self._store("qq"), self._store("official")]
            for _path, bindings in stores:
                entry = next(
                    (b for b in bindings if str(b.get("xbox", "")).casefold() == xbox_key),
                    None,
                )
                if entry:
                    return str(entry["qid"])
        return None

    def xbox_exists(self, xbox: str) -> bool:
        return self.get_qq_by_xbox(xbox) is not None

    def add_binding(self, qq: int | str, xbox: str, domain: str = "qq") -> bool:
        qid = str(qq)
        name = self._normalize_xbox(xbox)
        if not qid or not name:
            return False
        with self._data_lock:
            path, bindings = self._store(domain)
            if any(str(b.get("qid")) == qid for b in bindings):
                return False
            # xbox 全域唯一：同一玩家名不允许在两个域重复绑定
            for other in (*self.bindings, *self.bindings_official):
                if str(other.get("xbox", "")).casefold() == name.casefold():
                    return False
            bindings.append({"qid": qid, "xbox": name})
            self._save_list_locked(path, bindings)
            return True

    def remove_binding_by_qq(self, qq: int | str, domain: str = "qq") -> dict[str, Any] | None:
        qid = str(qq)
        with self._data_lock:
            path, bindings = self._store(domain)
            entry = next((b for b in bindings if str(b.get("qid")) == qid), None)
            if not entry:
                return None
            bindings.remove(entry)
            self._save_list_locked(path, bindings)
            return dict(entry)

    def remove_binding_by_xbox(self, xbox: str) -> dict[str, Any] | None:
        """跨域按玩家名解绑：优先个人号域，再查官方域。"""
        for domain in ("qq", "official"):
            qid = self.get_qq_by_xbox(xbox, domain)
            if qid:
                return self.remove_binding_by_qq(qid, domain)
        return None

    def _begin_operation(self, qq: int | str, xbox: str) -> bool:
        qid = str(qq)
        xbox_key = self._normalize_xbox(xbox).casefold()
        with self._data_lock:
            if qid in self._pending_qq or xbox_key in self._pending_xbox:
                return False
            self._pending_qq.add(qid)
            self._pending_xbox.add(xbox_key)
            return True

    def _end_operation(self, qq: int | str, xbox: str) -> None:
        with self._data_lock:
            self._pending_qq.discard(str(qq))
            self._pending_xbox.discard(self._normalize_xbox(xbox).casefold())

    @staticmethod
    def _classify_allowlist_output(action: str, output: str) -> str:
        """识别 BDS 白名单命令输出。

        返回 ``success`` / ``duplicate``（add：玩家已在白名单）/ ``missing``
        （remove：玩家不在白名单）/ 空字符串（无法识别，回退通用启发式）。
        """
        normalized = re.sub(r"§.", "", str(output or "")).casefold()
        if not normalized:
            return ""
        if action == "add":
            if any(marker in normalized for marker in _ADD_DUPLICATE_MARKERS):
                return "duplicate"
            if any(marker in normalized for marker in _ADD_SUCCESS_MARKERS):
                return "success"
        else:
            if any(marker in normalized for marker in _REMOVE_MISSING_MARKERS):
                return "missing"
            if any(marker in normalized for marker in _REMOVE_SUCCESS_MARKERS):
                return "success"
        return ""

    @classmethod
    def _command_succeeded(cls, dispatched: bool, output: str, action: str = "add") -> bool:
        outcome = cls._classify_allowlist_output(action, output)
        if outcome == "success":
            return True
        if outcome == "duplicate":
            return False
        if outcome == "missing":
            # remove 语义是"让玩家不在白名单中"：本就不在时视为幂等成功
            return True
        normalized = output.casefold()
        failure_markers = (
            "failed", "failure", "could not", "cannot", "not found",
            "not on the whitelist", "not in the whitelist",
            "失败", "失敗", "无法", "無法", "不能", "不存在", "未在白名单", "未在白名單",
        )
        if any(marker in normalized for marker in failure_markers):
            return False
        success_markers = (
            "success", "added", "removed",
            "成功", "已添加", "已移除",
        )
        if any(marker in normalized for marker in success_markers):
            return True
        return bool(dispatched)

    @staticmethod
    def _format_command_error(result: CommandResult) -> str:
        outcome = str(result.get("outcome") or "")
        if outcome == "duplicate":
            return _t("whitelist.reply.duplicate_detail")
        if outcome == "missing":
            return _t("whitelist.reply.missing_detail")
        detail = str(result.get("output") or result.get("error") or _t("whitelist.log.server_rejected"))
        detail = re.sub(r"§.", "", detail).strip().replace("\n", "；")
        return detail[:300]

    def _run_allowlist_command(
        self,
        action: str,
        xbox: str,
        callback: Callable[[CommandResult], None] | None = None,
    ) -> tuple[threading.Event, CommandResult]:
        """调度 ``/whitelist add|remove`` 并返回可等待的结果容器。"""
        if action not in {"add", "remove"}:
            raise ValueError(_t("whitelist.log.unsupported_action", action=action))
        name = self._normalize_xbox(xbox)
        # JSON 的字符串转义与 Bedrock 双引号参数语法兼容，可安全覆盖空格名称。
        target = json.dumps(name, ensure_ascii=False) if any(ch.isspace() for ch in name) else name
        cmd = f"whitelist {action} {target}"
        done = threading.Event()
        result: CommandResult = {
            "success": False,
            "dispatched": False,
            "output": "",
            "error": "",
            "outcome": "",
            "command": cmd,
            "xbox": name,
        }

        def finish() -> None:
            done.set()
            if callback:
                try:
                    callback(dict(result))
                except Exception as exc:
                    self.logger.error(_t("whitelist.log.command_error", exc=exc))

        def run() -> None:
            outputs: list[str] = []
            try:
                def capture(msg: Any) -> None:
                    try:
                        value = msg if isinstance(msg, str) else getattr(msg, "text", str(msg))
                        outputs.append(str(value))
                    except Exception:
                        pass

                from endstone.command import CommandSenderWrapper

                # Endstone 0.11：on_message / on_error 双回调捕获正常与错误输出
                sender = CommandSenderWrapper(
                    self.plugin.server.command_sender,
                    on_message=capture,
                    on_error=capture,
                )
                dispatched = bool(self.plugin.server.dispatch_command(sender, cmd))
                output = re.sub(r"§.", "", "\n".join(outputs)).strip()
                outcome = self._classify_allowlist_output(action, output)
                result["dispatched"] = dispatched
                result["output"] = output
                result["outcome"] = outcome
                result["success"] = self._command_succeeded(dispatched, output, action)
                if not result["success"]:
                    self.logger.warning(
                        _t("whitelist.log.command_failed", cmd=cmd, output=(output or _t("whitelist.log.no_output")))
                    )
            except Exception as exc:
                result["error"] = str(exc)
                self.logger.error(_t("whitelist.log.command_exception", cmd=cmd, exc=exc))
            finally:
                finish()

        try:
            self.plugin.run_on_main(run)
        except Exception as exc:
            result["error"] = str(exc)
            finish()
        return done, result

    def run_allowlist_command_wait(
        self, action: str, xbox: str, timeout: float = 6.0
    ) -> CommandResult:
        """供 Web 请求线程使用：等待命令完成，但不阻塞游戏主线程。"""
        done, result = self._run_allowlist_command(action, xbox)
        if not done.wait(timeout=max(0.1, timeout)):
            return {
                **result,
                "success": False,
                "error": _t("whitelist.log.wait_timeout", timeout=timeout),
            }
        return dict(result)

    def unbind_sync(
        self, qq: int | str, timeout: float = 6.0, domain: str = "qq"
    ) -> tuple[bool, str, dict[str, Any] | None]:
        """Web/API 使用的同步解绑事务；游戏侧成功后才删除本地绑定。"""
        entry = self.get_binding_by_qq(qq, domain)
        if not entry:
            return False, _t("whitelist.log.record_not_exist"), None
        xbox = entry["xbox"]
        if not self._begin_operation(qq, xbox):
            return False, _t("whitelist.reply.operation_in_progress"), entry
        try:
            result = self.run_allowlist_command_wait("remove", xbox, timeout)
            if not result.get("success"):
                return False, self._format_command_error(result), entry
            current = self.get_binding_by_qq(qq, domain)
            if not current or current.get("xbox") != xbox:
                return False, _t("whitelist.log.record_modified"), entry
            self.remove_binding_by_qq(qq, domain)
            self.logger.info(_t("whitelist.log.webui_unbind", qq=qq, xbox=xbox))
            return True, _t("whitelist.log.unbind_sync_success", xbox=xbox), entry
        finally:
            self._end_operation(qq, xbox)

    def _on_group_message(self, pack: dict[str, Any], reply: Any) -> None:
        if not self.conf.get("enable", True):
            return
        if not self.plugin.group_allowed(pack):
            return

        domain = self._domain_of(pack)
        raw = str(pack.get("raw_message", "")).strip()
        user_id = (pack.get("sender") or {}).get("user_id") or pack.get("user_id")
        # 关键词为空字符串时 startswith("") 恒为 True、所有消息都会进入绑定分支，
        # 统一回退默认关键词
        bind_kw = str(self.conf.get("bind_keyword") or "绑定白名单")
        unbind_kw = str(self.conf.get("unbind_keyword") or "解绑白名单")

        if raw.startswith(bind_kw):
            existing = self.get_binding_by_qq(user_id, domain)
            if existing:
                reply(_t("whitelist.reply.already_bound", xbox=existing['xbox']), True)
                return
            xbox = self._normalize_xbox(raw[len(bind_kw):])
            if not xbox:
                reply(_t("whitelist.reply.invalid_format", keyword=bind_kw), True)
                return
            if self.xbox_exists(xbox):
                reply(_t("whitelist.reply.xbox_taken"), True)
                return
            if not self._begin_operation(user_id, xbox):
                reply(_t("whitelist.reply.operation_in_progress"), True)
                return

            def complete_bind(result: CommandResult) -> None:
                try:
                    if not result.get("success"):
                        if str(result.get("outcome") or "") == "duplicate":
                            reply(_t("whitelist.reply.bind_failed_duplicate"), True)
                        else:
                            reply(_t("whitelist.reply.bind_failed_server", detail=self._format_command_error(result)), True)
                        return
                    if not self.add_binding(user_id, xbox, domain):
                        # 极少数并发冲突：回滚刚刚添加的游戏白名单，避免孤儿记录。
                        self._run_allowlist_command("remove", xbox)
                        reply(_t("whitelist.reply.bind_failed_busy"), True)
                        return
                    reply(_t("whitelist.reply.bind_success", xbox=xbox), True)
                    self.logger.info(_t("whitelist.log.bind_success", user_id=user_id, xbox=xbox))
                finally:
                    self._end_operation(user_id, xbox)

            if self.conf.get("auto_add", True):
                self._run_allowlist_command("add", xbox, complete_bind)
            else:
                complete_bind({"success": True, "output": ""})
            return

        if raw == unbind_kw:
            entry = self.get_binding_by_qq(user_id, domain)
            if not entry:
                reply(_t("whitelist.reply.not_bound"), True)
                return
            xbox = entry["xbox"]
            if not self._begin_operation(user_id, xbox):
                reply(_t("whitelist.reply.unbind_in_progress"), True)
                return

            def complete_unbind(result: CommandResult) -> None:
                try:
                    if not result.get("success"):
                        reply(
                            _t("whitelist.reply.unbind_failed", detail=self._format_command_error(result)),
                            True,
                        )
                        return
                    current = self.get_binding_by_qq(user_id, domain)
                    if not current or current.get("xbox") != xbox:
                        reply(_t("whitelist.reply.unbind_failed_modified"), True)
                        return
                    self.remove_binding_by_qq(user_id, domain)
                    reply(_t("whitelist.reply.unbind_success", xbox=xbox), True)
                    self.logger.info(_t("whitelist.log.unbind_success", user_id=user_id, xbox=xbox))
                finally:
                    self._end_operation(user_id, xbox)

            if self.conf.get("auto_add", True):
                self._run_allowlist_command("remove", xbox, complete_unbind)
            else:
                complete_unbind({"success": True, "output": ""})

    def _resolve_member_name(self, pack: dict[str, Any], fallback: str, then: Any) -> None:
        """解析群成员显示名：QQ 昵称（个人号域可查）→ 白名单名兜底。

        官方域 user_id 是 member_openid，官方无按 openid 反查昵称的接口，
        直接用白名单名。个人号域经 get_stranger_info 异步查询（超时/失败
        回调 None，同样兜底），回调保证触发（adapter.call_api 语义）。
        """
        sender = pack.get("sender") or {}
        inline = str(sender.get("card") or sender.get("nickname") or "").strip()
        if inline:
            then(inline)
            return
        if self._domain_of(pack) == "official":
            then(fallback)
            return
        try:
            def _cb(info: Any) -> None:
                name = ""
                if isinstance(info, dict):
                    name = str(info.get("card") or info.get("nickname") or "").strip()
                then(name or fallback)

            self.adapter.get_stranger_info(pack.get("user_id"), _cb)
        except Exception:
            then(fallback)

    def _on_group_decrease(self, pack: dict[str, Any]) -> None:
        if not self.conf.get("enable", True) or not self.conf.get("remove_on_leave", True):
            return
        group_id = pack.get("group_id")
        user_id = pack.get("user_id")
        self_id = pack.get("self_id")
        if not self.plugin.group_allowed(pack) or str(user_id) == str(self_id):
            return

        domain = self._domain_of(pack)
        entry = self.get_binding_by_qq(user_id, domain)
        if not entry:
            return
        xbox = entry["xbox"]
        if not self._begin_operation(user_id, xbox):
            return

        def complete(result: CommandResult) -> None:
            try:
                if result.get("success"):
                    self.remove_binding_by_qq(user_id, domain)

                    def announce(name: str) -> None:
                        self.adapter.send_group_msg(
                            group_id, _t("whitelist.reply.left_group_removed", name=name, xbox=xbox)
                        )

                    self._resolve_member_name(pack, xbox, announce)
                    self.logger.info(_t("whitelist.log.left_group_removed", user_id=user_id, xbox=xbox))
                else:
                    detail = self._format_command_error(result)

                    def announce_failed(name: str) -> None:
                        self.adapter.send_group_msg(
                            group_id,
                            _t("whitelist.reply.left_group_remove_failed", name=name, xbox=xbox, detail=detail),
                        )

                    self._resolve_member_name(pack, xbox, announce_failed)
                    self.logger.warning(_t("whitelist.log.left_group_remove_failed", user_id=user_id, xbox=xbox, detail=detail))
            finally:
                self._end_operation(user_id, xbox)

        self._run_allowlist_command("remove", xbox, complete)
