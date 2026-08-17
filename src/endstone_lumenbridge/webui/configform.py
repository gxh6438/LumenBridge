"""配置表单构建器：Fluent API 声明 Schema，保存触发 config.update.<name> 事件。"""

from __future__ import annotations

import copy
from typing import Any, Callable


class ConfigFormBuilder:
    """链式配置表单构建器"""

    def __init__(self, name: str, on_register: Callable[["ConfigFormBuilder"], None]) -> None:
        self.name = name
        self.items: list[dict[str, Any]] = []
        self._on_register = on_register

    def _add(self, item_type: str, key: str, val: Any, desc: str, label: str = "", **extra: Any) -> "ConfigFormBuilder":
        item = {"type": item_type, "key": key, "val": val, "desc": desc, "label": label or key}
        item.update(extra)
        self.items.append(item)
        return self

    def text(self, key: str, val: str = "", desc: str = "", label: str = "") -> "ConfigFormBuilder":
        return self._add("text", key, val, desc, label)

    def number(self, key: str, val: float = 0, desc: str = "", label: str = "") -> "ConfigFormBuilder":
        return self._add("number", key, val, desc, label)

    def switch(self, key: str, val: bool = False, desc: str = "", label: str = "") -> "ConfigFormBuilder":
        return self._add("switch", key, val, desc, label)

    def select(self, key: str, val: Any, options: list[Any], desc: str = "", label: str = "") -> "ConfigFormBuilder":
        """下拉选择字段；options 支持纯值列表或 {"value","label"} dict 列表（混用统一规范化为 dict）。"""
        normalized: list[dict[str, Any]] = []
        for o in options:
            if isinstance(o, dict) and "value" in o:
                normalized.append({"value": o["value"], "label": str(o.get("label", o["value"]))})
            else:
                normalized.append({"value": o, "label": str(o)})
        return self._add("select", key, val, desc, label, options=normalized)

    def array(self, key: str, val: list[Any] | None = None, desc: str = "", label: str = "") -> "ConfigFormBuilder":
        return self._add("array", key, val if val is not None else [], desc, label)

    def textarea(self, key: str, val: str = "", desc: str = "", label: str = "") -> "ConfigFormBuilder":
        return self._add("textarea", key, val, desc, label)

    def file(self, key: str, val: str = "", desc: str = "", label: str = "",
             accept: str = "image/*", upload_url: str = "") -> "ConfigFormBuilder":
        """文件上传字段：前端渲染为带预览的上传组件，选中文件后自动上传到 upload_url。

        - accept: 限制可选文件类型（如 "image/png, image/jpeg"）；
        - upload_url: 文件上传目标端点（如 "/api/plugin/picserver_rank3/background"）；
        - val: 当前状态提示文本（如 "已设置背景图" / ""）；
        - 配置保存（JSON POST）时跳过 file 类型字段，上传由独立端点处理。
        """
        return self._add("file", key, val, desc, label, accept=accept, upload_url=upload_url)

    def register(self) -> None:
        """完成构建并注册到 WebUI"""
        self._on_register(self)

    def to_schema(self) -> dict[str, Any]:
        """返回 schema 深拷贝：外部（含注册表）修改 schema 不回写构建器，反之亦然。

        必须用 deepcopy：select 的 options 是 dict 列表，浅拷贝（dict(item)）
        仍与构建器共享内层引用，外部改动会污染后续签发的所有表单。
        """
        return {"name": self.name, "items": copy.deepcopy(self.items)}
