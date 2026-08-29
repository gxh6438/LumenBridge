"""LumenBridge 国际化（i18n）模块。

加载内置语言包（en / zh_CN / zh_TW），自动检测 Endstone 服务器语言（server.properties
-> endstone API -> 兜底 zh_CN），提供 t(key, **kwargs) 翻译函数与 locale 规范化。
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

# 支持的语言代码 -> 显示名
SUPPORTED_LANGUAGES: dict[str, str] = {
    "en": "English",
    "zh_CN": "简体中文",
    "zh_TW": "繁體中文",
}

DEFAULT_LANGUAGE = "zh_CN"
AUTO_DETECT = "auto"

# locale 别名映射：连字符 -> 下划线，脚本子标签（Hans/Hant）优先于地区子标签判定繁简，
# 纯中文 zh / chinese -> zh_CN；不支持的语言回退到 DEFAULT_LANGUAGE
_LOCALE_ALIASES: dict[str, str] = {
    "zh": "zh_CN",
    "zh-hans": "zh_CN",
    "zh-hans-cn": "zh_CN",
    "zh-hans-sg": "zh_CN",
    "zh-hans-hk": "zh_CN",
    "zh-hans-tw": "zh_CN",
    "zh-hans-mo": "zh_CN",
    "zh-cn": "zh_CN",
    "zh_cn": "zh_CN",
    "zh-sg": "zh_CN",
    "zh_sg": "zh_CN",
    "chinese": "zh_CN",
    "zh-hant": "zh_TW",
    "zh-hant-tw": "zh_TW",
    "zh-hant-hk": "zh_TW",
    "zh-hant-mo": "zh_TW",
    "zh-hant-cn": "zh_TW",
    "zh-tw": "zh_TW",
    "zh_tw": "zh_TW",
    "zh-hk": "zh_TW",
    "zh_hk": "zh_TW",
    "zh-mo": "zh_TW",
    "zh_mo": "zh_TW",
    "en-us": "en",
    "en_us": "en",
    "en-gb": "en",
    "en_gb": "en",
    "en-au": "en",
    "en-ca": "en",
    "english": "en",
    "en": "en",
}

LOCALES_DIR = Path(__file__).parent / "locales"


def normalize_locale(locale: str) -> str:
    """将各种 locale 写法规范化为支持的代码之一。

    >>> normalize_locale("zh-CN")
    'zh_CN'
    >>> normalize_locale("zh_Hans_CN")
    'zh_CN'
    >>> normalize_locale("zh_Hant_TW")
    'zh_TW'
    >>> normalize_locale("en_US")
    'en'
    >>> normalize_locale("fr_FR")
    'zh_CN'  # 不支持的语言回退到默认语言
    """
    if not locale:
        return DEFAULT_LANGUAGE
    raw = str(locale).strip()
    key = raw.lower()
    if key in _LOCALE_ALIASES:
        return _LOCALE_ALIASES[key]
    # 下划线变体归一到连字符形式再查一次：别名表的扩展形式（脚本子标签等）
    # 只登记了连字符键，否则 zh_Hant / zh_hant 这类写法会漏过快速路径，
    # 走到语言级兜底被误判为 zh_CN（繁体服务器整套文案变简体）
    hyphen_key = key.replace("_", "-")
    if hyphen_key != key and hyphen_key in _LOCALE_ALIASES:
        return _LOCALE_ALIASES[hyphen_key]
    normalized = raw.replace("-", "_")
    parts = [p for p in normalized.split("_") if p]
    # 三段式：语言_脚本_国家（如 zh_Hans_CN / zh_Hant_TW）
    # 脚本子标签特征：4 字符且首字母大写（Hans/Hant/Cyrl/Latn）
    if len(parts) >= 3 and len(parts[1]) == 4 and parts[1][0].isupper():
        lang = parts[0].lower()
        script = parts[1]
        country = parts[2].upper()
        script_lower = script.lower()
        candidate_script = f"{lang}-{script_lower}"
        if candidate_script in _LOCALE_ALIASES:
            return _LOCALE_ALIASES[candidate_script]
        candidate_full = f"{lang}-{script_lower}-{country.lower()}"
        if candidate_full in _LOCALE_ALIASES:
            return _LOCALE_ALIASES[candidate_full]
        if lang in _LOCALE_ALIASES:
            return _LOCALE_ALIASES[lang]
    # 两段式：语言_国家（如 zh_CN / en_US）
    if len(parts) >= 2:
        lang = parts[0].lower()
        # 跳过脚本子标签（如 zh_Hans 形式）
        if len(parts[1]) == 4 and parts[1][0].isupper():
            country = parts[2].upper() if len(parts) >= 3 else ""
        else:
            country = parts[1].upper()
        candidate = f"{lang}_{country}" if country else lang
        if candidate.lower() in _LOCALE_ALIASES:
            return _LOCALE_ALIASES[candidate.lower()]
        if candidate in SUPPORTED_LANGUAGES:
            return candidate
        # 同语言不同国家回退到语言主代码（如 zh_HK -> 查 zh 系列别名）
        lang_key = lang.lower()
        if lang_key in _LOCALE_ALIASES:
            return _LOCALE_ALIASES[lang_key]
    elif len(parts) == 1:
        lang_key = parts[0].lower()
        if lang_key in _LOCALE_ALIASES:
            return _LOCALE_ALIASES[lang_key]
        if lang_key in SUPPORTED_LANGUAGES:
            return lang_key
    return DEFAULT_LANGUAGE


def detect_endstone_language(server: Any = None, data_folder: Any = None) -> str:
    """检测 Endstone 服务器语言，返回规范化的 locale 代码。

    优先级：server.properties 的 language 字段 -> endstone API server.language.locale
    -> 兜底 zh_CN。
    """
    if data_folder is not None:
        try:
            server_properties_path = Path(data_folder).parent.parent / "server.properties"
            locale = _read_server_properties_language(server_properties_path)
            if locale:
                return normalize_locale(locale)
        except Exception:
            pass
    if server is not None:
        try:
            lang_obj = getattr(server, "language", None)
            if lang_obj is not None:
                locale = getattr(lang_obj, "locale", None)
                if locale:
                    return normalize_locale(locale)
        except Exception:
            pass
    return DEFAULT_LANGUAGE


def _read_server_properties_language(properties_path: Path) -> str:
    """解析 server.properties 文件，返回 language 字段原始字符串（未规范化）。

    server.properties 是标准 Java properties 格式（key=value，UTF-8，# 注释）。
    """
    if not properties_path.is_file():
        return ""
    try:
        text = properties_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("!"):
            continue
        m = re.match(r"^language\s*=\s*(.+?)\s*$", line)
        if m:
            return m.group(1).strip()
    return ""


class I18n:
    """国际化管理器：加载语言包、提供翻译函数，并支持子插件注册自定义翻译。"""

    def __init__(self, language: str = DEFAULT_LANGUAGE) -> None:
        self._language: str = DEFAULT_LANGUAGE
        self._cache: dict[str, dict[str, Any]] = {}
        # 子插件命名空间翻译：{namespace: {lang: {key: value}}}
        self._namespaces: dict[str, dict[str, dict[str, str]]] = {}
        self.set_language(language)

    @property
    def language(self) -> str:
        """当前语言代码（已规范化）。"""
        return self._language

    @property
    def lang(self) -> str:
        """当前语言代码（别名，兼容子插件习惯写法）。"""
        return self._language

    def set_language(self, language: str) -> str:
        """设置当前语言，返回实际生效的语言代码（已规范化）。

        若 language 为 "auto"，保持当前语言不变（由 plugin 调用
        detect_endstone_language 后再 set_language 实际值）。
        """
        if language == AUTO_DETECT:
            return self._language
        normalized = normalize_locale(language) if language else DEFAULT_LANGUAGE
        self._language = normalized if normalized in SUPPORTED_LANGUAGES else DEFAULT_LANGUAGE
        return self._language

    def _load(self, lang: str) -> dict[str, Any]:
        """加载指定语言的翻译字典（带缓存）。"""
        if lang in self._cache:
            return self._cache[lang]
        path = LOCALES_DIR / f"{lang}.json"
        if not path.is_file():
            self._cache[lang] = {}
            return self._cache[lang]
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                data = {}
        except (json.JSONDecodeError, UnicodeDecodeError, OSError):
            data = {}
        self._cache[lang] = data
        return data

    def _lookup(self, key: str, lang: str) -> str | None:
        """在指定语言包中查找键，返回字符串值或 None。"""
        data = self._load(lang)
        parts = key.split(".")
        cur: Any = data
        for p in parts:
            if isinstance(cur, dict) and p in cur:
                cur = cur[p]
            else:
                return None
        return cur if isinstance(cur, str) else None

    def t(self, key: str, **kwargs: Any) -> str:
        """翻译键，支持 {placeholder} 占位符。查找顺序：当前语言 -> en -> key 本身。"""
        val = self._lookup(key, self._language)
        if val is None and self._language != "en":
            val = self._lookup(key, "en")
        if val is None:
            return key
        if kwargs:
            try:
                return val.format(**kwargs)
            except (KeyError, IndexError, ValueError, TypeError):
                return val
        return val

    def register_namespace(self, namespace: str, translations: dict[str, dict[str, str]]) -> None:
        """子插件注册自己的翻译字典：{语言代码: {key: 翻译文本}}。

        子插件可只提供一种语言，t() 在缺失时回退到该命名空间下第一个可用语言，再回退到 key。
        """
        if not namespace or not isinstance(translations, dict):
            return
        normalized: dict[str, dict[str, str]] = {}
        for lang, kvs in translations.items():
            if not isinstance(kvs, dict):
                continue
            normalized[lang] = {str(k): str(v) for k, v in kvs.items()}
        self._namespaces[namespace] = normalized

    def unregister_namespace(self, namespace: str) -> None:
        """子插件卸载时清理其翻译（热重载时避免泄漏）。"""
        self._namespaces.pop(namespace, None)

    def tn(self, namespace: str, key: str, **kwargs: Any) -> str:
        """翻译子插件命名空间下的键。

        查找顺序：命名空间下当前语言 -> en -> 第一个可用语言 -> key 本身。
        """
        ns = self._namespaces.get(namespace)
        if not ns:
            return key
        val = ns.get(self._language, {}).get(key)
        if val is None and self._language != "en":
            val = ns.get("en", {}).get(key)
        # 子插件单语言场景：回退到第一个可用语言
        if val is None:
            for lang_kvs in ns.values():
                if key in lang_kvs:
                    val = lang_kvs[key]
                    break
        if val is None:
            return key
        if kwargs:
            try:
                return val.format(**kwargs)
            except (KeyError, IndexError, ValueError, TypeError):
                return val
        return val

    def available_languages(self) -> dict[str, str]:
        """返回所有可用语言 {code: native_name}。"""
        result: dict[str, str] = {}
        for code in SUPPORTED_LANGUAGES:
            meta = self._load(code).get("_meta", {})
            result[code] = str(meta.get("native_name", SUPPORTED_LANGUAGES[code]))
        return result

    def export(self, lang: str | None = None) -> dict[str, Any]:
        """导出指定语言的完整翻译字典（供 WebUI 前端使用）。lang 为 None 则导出当前语言。"""
        target = lang or self._language
        normalized = normalize_locale(target) if target else self._language
        # 始终深拷贝再合并：直接返回 _load 的缓存引用（尤其 en 分支）会把
        # 缓存字典暴露给调用方，外部改写会污染后续所有翻译
        data = copy.deepcopy(self._load(normalized))
        # 该语言缺失的键用 en 补全，前端无需处理回退
        if normalized != "en":
            en_data = self._load("en")
            data = _deep_merge_translation(en_data, data)
        return data


def _deep_merge_translation(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """递归合并翻译字典：override 覆盖 base，base 补全缺失键。"""
    result: dict[str, Any] = copy.deepcopy(override)
    for key, value in base.items():
        if key not in result:
            result[key] = copy.deepcopy(value)
        elif isinstance(value, dict) and isinstance(result[key], dict):
            result[key] = _deep_merge_translation(value, result[key])
    return result


# 全局单例（plugin 启动时调用 set_language 配置）
_i18n = I18n(DEFAULT_LANGUAGE)


def get_i18n() -> I18n:
    """获取全局 I18n 实例。"""
    return _i18n


def t(key: str, **kwargs: Any) -> str:
    """全局翻译快捷函数。"""
    return _i18n.t(key, **kwargs)
