"""子插件强制依赖（requires）声明解析与检查。

``lumen.json`` 在 pip 依赖（``dependencies``）之外，可声明对其它
LumenBridge 子插件与 Endstone 插件的强制依赖::

    "requires": {
        "subplugins": ["economy>=1.2.0", "chat-helpers"],
        "endstone": ["endstone-some-plugin>=2.0"]
    }

宽容格式：``requires`` 直接给数组时视为 ``{"subplugins": [...]}``。
单条约束支持六种比较符（``>= <= > < == !=``），仅写名称时只要求存在。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

# 单条约束：名称 + 可选比较符 + 可选版本号
_REQ_RE = re.compile(
    r"^\s*([A-Za-z0-9_\-]{1,64})\s*(==|!=|>=|<=|>|<)?\s*([0-9A-Za-z.+-]{1,64})?\s*$"
)

def version_tuple(value: Any) -> tuple[int, ...]:
    """宽松版本比较元组：仅去 v/V 前缀，每段取前导数字（与 min_v 口径一致）。"""
    parts: list[int] = []
    for segment in str(value or "0").lstrip("vV").split("."):
        match = re.match(r"(\d+)", segment)
        parts.append(int(match.group(1)) if match else 0)
    return tuple(parts or [0])


def version_cmp(a: Any, b: Any) -> int:
    """比较两个版本字符串，返回 -1/0/1。

    段数不同时右侧补 0 对齐：1.2 与 1.2.0 视为相等（语义化版本惯例）。
    裸元组比较中 (1, 2) < (1, 2, 0)，会把相等版本误判为不满足
    （如 ==1.2 对已装 1.2.0、min_v 1.2.0 对宿主 1.2）。
    """
    ta, tb = version_tuple(a), version_tuple(b)
    n = max(len(ta), len(tb))
    pa = ta + (0,) * (n - len(ta))
    pb = tb + (0,) * (n - len(tb))
    return (pa > pb) - (pa < pb)


_OPS: dict[str, Callable[[Any, Any], bool]] = {
    ">=": lambda a, b: version_cmp(a, b) >= 0,
    "<=": lambda a, b: version_cmp(a, b) <= 0,
    ">": lambda a, b: version_cmp(a, b) > 0,
    "<": lambda a, b: version_cmp(a, b) < 0,
    "==": lambda a, b: version_cmp(a, b) == 0,
    "!=": lambda a, b: version_cmp(a, b) != 0,
}


@dataclass
class PluginRequirement:
    """单条插件依赖约束，如 ``economy>=1.2.0``。"""

    name: str
    op: str = ""  # 空 = 仅要求存在
    version: str = ""
    raw: str = ""

    def display(self) -> str:
        if self.raw:
            return self.raw
        return self.name + (f"{self.op}{self.version}" if self.op else "")

    def satisfied_by(self, actual_version: Any) -> bool:
        if not self.op:
            return True
        return _OPS[self.op](actual_version, self.version)

    def describe_unmet(self, actual: str = "") -> str:
        """生成“缺什么/差多少”的一句话描述（不带 i18n，供日志拼接）。"""
        if not actual:
            return self.display()
        return f"{self.display()} (当前 v{actual})"


@dataclass
class RequiresDeclaration:
    """一个子插件声明的全部插件级依赖。"""

    subplugins: list[PluginRequirement] = field(default_factory=list)
    endstone: list[PluginRequirement] = field(default_factory=list)
    # 无效声明项（格式非法）收集于此，供日志提示作者修正
    invalid: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not (self.subplugins or self.endstone)

    def subplugin_names(self) -> set[str]:
        return {r.name for r in self.subplugins}


def parse_requirement(spec: Any) -> PluginRequirement | None:
    """解析单条约束；格式非法返回 None（绝不抛异常）。"""
    if not isinstance(spec, str):
        return None
    match = _REQ_RE.match(spec)
    if not match:
        return None
    name, op, version = match.group(1), match.group(2) or "", match.group(3) or ""
    if op and not version:
        # "name>=" 这类写了比较符却没给版本：视为非法
        return None
    if version and not op:
        # "has space" 会被误拆成 name="has" + version="space"：
        # 无比较符却带版本 = 非法（版本必须跟在比较符后）
        return None
    return PluginRequirement(name=name, op=op, version=version, raw=spec.strip())


def _parse_list(raw: Any, invalid: list[str]) -> list[PluginRequirement]:
    out: list[PluginRequirement] = []
    if raw is None:
        return out
    if not isinstance(raw, list):
        invalid.append(str(raw)[:80])
        return out
    for item in raw:
        req = parse_requirement(item)
        if req is not None:
            out.append(req)
        else:
            invalid.append(str(item)[:80])
    return out


def parse_requires(raw: Any) -> RequiresDeclaration:
    """解析 manifest 的 requires 字段，任何畸形输入都收敛为“部分/空声明”。

    - 缺失 / None / 非法类型 → 空声明
    - 数组 → 视为 subplugins
    - 对象 → 读取 subplugins / endstone 两键（其余键忽略）
    - 单项非法 → 跳过并记入 invalid（与 dependencies 非 list 的宽容口径一致）
    """
    declaration = RequiresDeclaration()
    if raw is None:
        return declaration
    if isinstance(raw, list):
        declaration.subplugins = _parse_list(raw, declaration.invalid)
        return declaration
    if isinstance(raw, dict):
        declaration.subplugins = _parse_list(raw.get("subplugins"), declaration.invalid)
        declaration.endstone = _parse_list(raw.get("endstone"), declaration.invalid)
        return declaration
    declaration.invalid.append(str(raw)[:80])
    return declaration


def parse_requires_from_manifest(manifest: Any) -> RequiresDeclaration:
    """从 manifest dict 提取 requires（manifest 非 dict 时返回空声明）。"""
    if isinstance(manifest, dict):
        return parse_requires(manifest.get("requires"))
    return RequiresDeclaration()


def check_endstone_requirements(
    requirements: Iterable[PluginRequirement],
    installed: dict[str, str],
) -> list[tuple[PluginRequirement, str]]:
    """检查 Endstone 插件依赖。

    ``installed`` 为已加载 Endstone 插件名（小写）→ 版本 的映射。
    返回不满足的 (约束, 当前版本) 列表；当前版本为空表示未安装。
    仅要求存在的约束：插件在列表中即满足（版本未知也算已安装）。
    """
    unmet: list[tuple[PluginRequirement, str]] = []
    for req in requirements:
        actual = installed.get(req.name.lower(), "")
        if req.name.lower() not in installed:
            unmet.append((req, ""))
        elif req.op and not req.satisfied_by(actual):
            unmet.append((req, actual))
    return unmet
