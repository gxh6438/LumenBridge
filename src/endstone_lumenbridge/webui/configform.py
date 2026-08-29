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

    def _add(
        self,
        item_type: str,
        key: str,
        val: Any,
        desc: str,
        label: str = "",
        *,
        default: Any = None,
        **extra: Any,
    ) -> "ConfigFormBuilder":
        item = {"type": item_type, "key": key, "val": val, "desc": desc, "label": label or key}
        # 恢复默认按钮的数据源：未显式声明 default 时以注册时的初始值为默认值
        item["default"] = copy.deepcopy(val) if default is None else copy.deepcopy(default)
        item.update(extra)
        self.items.append(item)
        return self

    def section(self, title: str, desc: str = "") -> "ConfigFormBuilder":
        """分组标题：声明一个分组卡片，后续字段归属该组直到下一个 section。

        分组仅影响 WebUI 渲染布局，不参与配置读写（key 为空、保存时自动跳过）。
        """
        self.items.append({
            "type": "section", "key": "", "val": "", "desc": desc,
            "label": title, "title": title, "default": "",
        })
        return self

    def text(
        self, key: str, val: str = "", desc: str = "", label: str = "",
        *, secret: bool = False, default: Any = None,
        obvious_hint: bool = False, show_key: bool = False,
    ) -> "ConfigFormBuilder":
        """文本输入。secret=True 渲染为密码框（带明文切换）。"""
        return self._add("text", key, val, desc, label, default=default,
                         secret=secret, obvious_hint=obvious_hint, show_key=show_key)

    def number(
        self, key: str, val: float = 0, desc: str = "", label: str = "",
        *, minimum: float | None = None, maximum: float | None = None,
        step: float | None = None, default: Any = None,
        obvious_hint: bool = False, show_key: bool = False,
    ) -> "ConfigFormBuilder":
        """数字输入。同时声明 minimum/maximum 时前端渲染滑块+数字框联动。"""
        extra: dict[str, Any] = {"obvious_hint": obvious_hint, "show_key": show_key}
        if minimum is not None:
            extra["min"] = minimum
        if maximum is not None:
            extra["max"] = maximum
        if step is not None:
            extra["step"] = step
        return self._add("number", key, val, desc, label, default=default, **extra)

    def switch(
        self, key: str, val: bool = False, desc: str = "", label: str = "",
        *, default: Any = None, obvious_hint: bool = False, show_key: bool = False,
    ) -> "ConfigFormBuilder":
        return self._add("switch", key, val, desc, label, default=default,
                         obvious_hint=obvious_hint, show_key=show_key)

    def select(
        self, key: str, val: Any, options: list[Any], desc: str = "", label: str = "",
        *, default: Any = None, obvious_hint: bool = False, show_key: bool = False,
    ) -> "ConfigFormBuilder":
        """下拉选择字段；options 支持纯值列表或 {"value","label"} dict 列表（混用统一规范化为 dict）。"""
        normalized: list[dict[str, Any]] = []
        for o in options:
            if isinstance(o, dict) and "value" in o:
                normalized.append({"value": o["value"], "label": str(o.get("label", o["value"]))})
            else:
                normalized.append({"value": o, "label": str(o)})
        return self._add("select", key, val, desc, label, options=normalized, default=default,
                         obvious_hint=obvious_hint, show_key=show_key)

    def multiselect(
        self, key: str, val: list[Any] | None, options: list[Any], desc: str = "", label: str = "",
        *, default: Any = None, obvious_hint: bool = False, show_key: bool = False,
    ) -> "ConfigFormBuilder":
        """多选字段：渲染为复选框组，val 为已选值列表；options 规范化规则同 select。"""
        normalized: list[dict[str, Any]] = []
        for o in options:
            if isinstance(o, dict) and "value" in o:
                normalized.append({"value": o["value"], "label": str(o.get("label", o["value"]))})
            else:
                normalized.append({"value": o, "label": str(o)})
        return self._add("multiselect", key, val if val is not None else [], desc, label,
                         options=normalized, default=default,
                         obvious_hint=obvious_hint, show_key=show_key)

    def array(
        self, key: str, val: list[Any] | None = None, desc: str = "", label: str = "",
        *, default: Any = None, obvious_hint: bool = False, show_key: bool = False,
    ) -> "ConfigFormBuilder":
        """数组字段：渲染为 chips 列表编辑器（支持逐项增删与批量导入）。"""
        return self._add("array", key, val if val is not None else [], desc, label, default=default,
                         obvious_hint=obvious_hint, show_key=show_key)

    def textarea(
        self, key: str, val: str = "", desc: str = "", label: str = "",
        *, default: Any = None, obvious_hint: bool = False, show_key: bool = False,
    ) -> "ConfigFormBuilder":
        """多行文本：右上角提供全屏编辑入口。"""
        return self._add("textarea", key, val, desc, label, default=default,
                         obvious_hint=obvious_hint, show_key=show_key)

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
