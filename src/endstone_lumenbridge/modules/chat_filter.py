"""聊天屏蔽词过滤模块：双向转发敏感词过滤（打码 / 拦截）。

存储布局（独立子目录，见 migrate_storage.py 分类规范）::

    data/chat_filter/words.json     词条与配置（原子写）
    data/chat_filter/wordbanks/     词库目录：*.txt（每行一词，# 注释）
                                    自动发现，WebUI 一键导入

匹配策略：
- 普通词条经全角→半角 + 大小写归一化后 re.escape 拼接为单条合并正则，
  万级词条单次匹配 O(len(text))，避免逐词 str.find 的 O(n*m)；
- 正则词条单独逐条匹配（数量少，通常个位数）；
- 模式：mask（命中替换 ***，消息继续转发）/ block（整条丢弃）；
- 方向：game_to_qq / qq_to_game 独立开关；
- 豁免：玩家名 / QQ 号命中时跳过过滤。

线程模型：词条只在 WebUI 保存时变化（HTTP 线程），匹配在事件线程/
主线程执行；编译后的 pattern 以不可变元组整体替换（原子引用置换），
读侧无锁。
"""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Any, TYPE_CHECKING

from ..i18n import t as _t

if TYPE_CHECKING:
    from ..plugin import LumenBridgePlugin

# 合并正则分片上限：超长单正则某些引擎回溯退化，分片保持稳定延迟
_MERGED_CHUNK = 400
# 单个正则词条编译超时保护（ReDoS）：仅允许安全子集的简单启发式，
# 与 regex_engine 相比这里词条由管理员维护，风险面小，仍做长度上限
_MAX_REGEX_LEN = 128
# 全角数字/字母/标点 → 半角（含全角空格）
_FULLWIDTH_MAP = {i: i - 0xFEE0 for i in range(0xFF01, 0xFF5F)}
_FULLWIDTH_MAP[0x3000] = 0x20  # 全角空格

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "mode": "mask",            # mask | block
    "game_to_qq": True,
    "qq_to_game": True,
    "mask_text": "*",
    "exempt_players": [],       # 玩家名（不区分大小写）
    "exempt_qq": [],            # QQ 号
}

# 词条条目：{"word": str, "type": "plain"|"regex", "source": str}
# source 为词库文件名 / "custom"（WebUI 手动添加）


def normalize_text(text: str) -> str:
    """全角→半角 + ASCII 大写→小写。用于普通词条匹配前的归一化。"""
    if not text:
        return ""
    half = text.translate(_FULLWIDTH_MAP)
    return half.lower()


class ChatFilterModule:
    """聊天屏蔽词过滤（双向 + 双模式 + 豁免）"""

    def __init__(self, plugin: "LumenBridgePlugin") -> None:
        self.plugin = plugin
        self.logger = getattr(plugin, "_tee_logger", None) or plugin.logger
        self._dir = Path(plugin.data_folder) / "data" / "chat_filter"
        self._words_path = self._dir / "words.json"
        self._wordbanks_dir = self._dir / "wordbanks"
        self._lock = threading.Lock()

        self._config: dict[str, Any] = dict(DEFAULTS)
        self._words: list[dict[str, Any]] = []
        # 编译产物：((merged_pattern_tuple), (regex_pattern_tuple), exempt_set)
        self._compiled: tuple[tuple[Any, ...], tuple[Any, ...], frozenset[str]] = ((), (), frozenset())
        self._load()
        self._recompile()

    # ------------------------------------------------------------------ 加载
    def _load(self) -> None:
        """读取 words.json（不存在则落盘默认结构）。"""
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            self._wordbanks_dir.mkdir(parents=True, exist_ok=True)
            self._seed_builtin_wordbanks()
            if self._words_path.exists():
                raw = json.loads(self._words_path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    cfg = raw.get("config")
                    words = raw.get("words")
                    if isinstance(cfg, dict):
                        self._config = {**DEFAULTS, **cfg}
                    if isinstance(words, list):
                        self._words = [w for w in words if isinstance(w, dict) and w.get("word")]
            else:
                self._save_locked()
        except (OSError, json.JSONDecodeError) as e:
            self.logger.warning(_t("chatfilter.load_failed_log", error=e))
            # 损坏文件备份后重置（参考 command_palette 处理）
            try:
                if self._words_path.exists():
                    self._words_path.replace(self._words_path.with_suffix(".json.corrupt"))
            except OSError:
                pass
            self._config = dict(DEFAULTS)
            self._words = []

    def _seed_builtin_wordbanks(self) -> None:
        """首次运行时把内置示例词库播种到 data 目录（不覆盖同名文件）。"""
        builtin = Path(__file__).resolve().parent.parent / "wordbanks"
        try:
            if not builtin.is_dir():
                return
            for src in sorted(builtin.glob("*.txt")):
                dst = self._wordbanks_dir / src.name
                if not dst.exists():
                    dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
        except OSError as e:
            self.logger.warning(_t("chatfilter.seed_failed", error=e))

    def _save_locked(self) -> None:
        """原子写 words.json（调用方持锁或单线程初始化路径）。"""
        self._dir.mkdir(parents=True, exist_ok=True)
        temp = self._words_path.with_name(self._words_path.name + ".tmp")
        temp.write_text(
            json.dumps({"config": self._config, "words": self._words}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temp.replace(self._words_path)

    # ------------------------------------------------------------------ 编译
    def _recompile(self) -> None:
        """把词条编译为合并正则元组；异常词条跳过并记日志。"""
        plains: list[str] = []
        regexes: list[Any] = []
        for entry in self._words:
            word = str(entry.get("word") or "")
            if not word:
                continue
            if entry.get("type") == "regex":
                if len(word) > _MAX_REGEX_LEN:
                    continue
                try:
                    regexes.append(re.compile(word, re.IGNORECASE))
                except re.error as e:
                    self.logger.warning(_t("chatfilter.regex_invalid", word=word, error=e))
            else:
                # 普通词条按归一化形式匹配（全角/大小写变形词一并命中）
                plains.append(normalize_text(word))
        # 合并正则分片：每片 _MERGED_CHUNK 个词条
        merged: list[Any] = []
        for i in range(0, len(plains), _MERGED_CHUNK):
            chunk = plains[i : i + _MERGED_CHUNK]
            merged.append(re.compile("|".join(re.escape(p) for p in chunk if p)))
        exempt = frozenset(
            normalize_text(str(p)) for p in self._config.get("exempt_players", [])
        ) | frozenset(str(q) for q in self._config.get("exempt_qq", []))
        # 原子引用置换：读侧永不持锁
        self._compiled = (tuple(merged), tuple(regexes), exempt)

    # ------------------------------------------------------------------ 过滤
    def check(
        self, text: str, *, direction: str, player: str = "", qq: str = ""
    ) -> tuple[str, bool]:
        """过滤一段文本。

        :param direction: "game_to_qq" | "qq_to_game"
        :param player: 玩家名（游戏→QQ 方向的豁免判定）
        :param qq: 发送者 QQ 号（QQ→游戏方向的豁免判定）
        :return: (处理后的文本, 是否命中)——block 模式命中时返回 ("", True)
        """
        if not isinstance(text, str) or not text:
            return text, False
        cfg = self._config
        if not cfg.get("enabled", True):
            return text, False
        if not cfg.get(direction, True):
            return text, False
        merged, regexes, exempt = self._compiled
        # 豁免判定（按方向取对应身份）
        identity = normalize_text(player) if direction == "game_to_qq" else str(qq or "")
        if identity and identity in exempt:
            return text, False

        normalized = normalize_text(text)
        hit = False
        for pat in merged:
            if pat.search(normalized):
                hit = True
                break
        if not hit:
            for pat in regexes:
                if pat.search(text):
                    hit = True
                    break
        if not hit:
            return text, False

        if cfg.get("mode", "mask") == "block":
            return "", True
        mask = str(cfg.get("mask_text", "*") or "*")
        # 打码：普通词条在归一化文本上替换（等长映射回原文会因长度
        # 不一致错位，故直接对原文按各词条原文替换；正则词条同样
        # 对原文替换）。逐词条替换保证变形词（全角）也能替换原文。
        result = text
        for entry in self._words:
            word = str(entry.get("word") or "")
            if not word:
                continue
            if entry.get("type") == "regex":
                try:
                    pat = re.compile(word, re.IGNORECASE)
                    result = pat.sub(mask, result)
                except re.error:
                    continue
            else:
                # 构造忽略全角/大小写差异的替换：按归一化等价类逐段替换
                result = _replace_normalized(result, word, mask)
        return result, True

    # ------------------------------------------------------------------ 词库
    def list_wordbanks(self) -> list[dict[str, Any]]:
        """发现 wordbanks/ 下的 *.txt 词库（名称、词条数、已导入状态）。"""
        banks: list[dict[str, Any]] = []
        if not self._wordbanks_dir.is_dir():
            return banks
        imported_sources = {str(w.get("source") or "") for w in self._words}
        for f in sorted(self._wordbanks_dir.glob("*.txt")):
            try:
                count = sum(
                    1
                    for line in f.read_text(encoding="utf-8", errors="replace").splitlines()
                    if line.strip() and not line.strip().startswith("#")
                )
            except OSError:
                continue
            banks.append({
                "name": f.stem,
                "file": f.name,
                "count": count,
                "imported": f.name in imported_sources,
            })
        return banks

    def import_wordbank(self, filename: str) -> int:
        """导入指定词库文件（去重合并），返回新增词条数。"""
        safe = Path(filename).name  # 防路径穿越
        path = self._wordbanks_dir / safe
        if not path.is_file():
            raise FileNotFoundError(safe)
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as e:
            raise OSError(str(e)) from e
        new_words = [
            ln.strip() for ln in lines
            if ln.strip() and not ln.strip().startswith("#")
        ]
        if not new_words:
            return 0
        with self._lock:
            existing = {normalize_text(str(w.get("word") or "")) for w in self._words}
            added = 0
            for w in new_words:
                if normalize_text(w) not in existing:
                    self._words.append({"word": w, "type": "plain", "source": safe})
                    existing.add(normalize_text(w))
                    added += 1
            if added:
                self._save_locked()
                self._recompile()
        return added

    # ------------------------------------------------------------------ WebUI
    def snapshot(self) -> dict[str, Any]:
        """GET /api/chat_filter 的数据快照。"""
        with self._lock:
            return {
                "config": dict(self._config),
                "words": [dict(w) for w in self._words],
                "wordbanks": self.list_wordbanks(),
            }

    def update(self, payload: dict[str, Any]) -> None:
        """PUT /api/chat_filter：整体替换配置与词条（原子写 + 重编译）。"""
        cfg_in = payload.get("config")
        words_in = payload.get("words")
        with self._lock:
            if isinstance(cfg_in, dict):
                merged = {**DEFAULTS, **{
                    k: v for k, v in cfg_in.items() if k in DEFAULTS
                }}
                # 枚举校验
                if merged["mode"] not in ("mask", "block"):
                    merged["mode"] = "mask"
                merged["exempt_players"] = [str(p) for p in (merged.get("exempt_players") or [])][:200]
                merged["exempt_qq"] = [str(q) for q in (merged.get("exempt_qq") or [])][:200]
                self._config = merged
            if isinstance(words_in, list):
                seen: set[str] = set()
                cleaned: list[dict[str, Any]] = []
                for w in words_in:
                    if not isinstance(w, dict):
                        continue
                    word = str(w.get("word") or "").strip()
                    if not word or len(word) > _MAX_REGEX_LEN:
                        continue
                    key = normalize_text(word)
                    if key in seen:
                        continue
                    seen.add(key)
                    cleaned.append({
                        "word": word,
                        "type": "regex" if w.get("type") == "regex" else "plain",
                        "source": str(w.get("source") or "custom")[:64],
                    })
                self._words = cleaned
            self._save_locked()
            self._recompile()


def _replace_normalized(text: str, word: str, mask: str) -> str:
    """在原文上替换与 word 归一化等价的片段（处理全角/大小写变形）。"""
    norm_word = normalize_text(word)
    if not norm_word:
        return text
    norm_text = normalize_text(text)
    if norm_word not in norm_text:
        return text
    # 归一化保持等长（translate 逐字符 1:1），可按索引切片回原文
    out: list[str] = []
    i = 0
    n = len(norm_word)
    while i <= len(norm_text) - n:
        if norm_text[i : i + n] == norm_word:
            out.append(mask)
            i += n
        else:
            out.append(text[i])
            i += 1
    out.append(text[i:])
    return "".join(out)
