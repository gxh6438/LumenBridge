"""内置第三方库加载器：websockets 内嵌在 lib/ 随插件分发，避免手动安装依赖。

lib/ 目录追加到 sys.path 尾部（append 而非 insert(0)）：磁盘上已安装的合格
websockets（>= 14）优先解析，内嵌版本仅在环境中没有任何可用 websockets 时
兜底命中，不抢占系统/其他插件的版本解析优先级。
"""

import logging
import sys
from pathlib import Path
from typing import Any

_LOG = logging.getLogger(__name__)


def setup_lib_path() -> None:
    """将插件内置的 lib 目录追加到模块搜索路径尾部（append 而非 insert(0)）。"""
    lib_path = str(Path(__file__).parent / "lib")
    if lib_path not in sys.path:
        sys.path.append(lib_path)


def _is_websockets_compatible(module: Any) -> bool:
    """检测已导入的 websockets 是否满足适配器需求。

    onebot/qqofficial 适配器使用新式接口（additional_headers 参数要求 >= 14）；
    更旧版本建连时会抛 TypeError。
    """
    if not (
        callable(getattr(module, "connect", None))
        and callable(getattr(module, "serve", None))
    ):
        return False
    version = str(getattr(module, "__version__", "") or "")
    try:
        parts = tuple(int(p) for p in version.split(".")[:2])
    except ValueError:
        # 版本号无法解析（如开发版）；所需 API 齐全则视为可用
        return True
    # 单段版本号（如 "14"）补齐为 (14, 0)：否则 (14,) < (14, 0) 会误判不兼容
    parts += (0,) * (2 - len(parts))
    return parts >= (14, 0)


def import_websockets() -> Any:
    """导入 websockets（成功返回模块，失败抛 ImportError）。

    1. sys.modules 已有合格版本（>= 14 或 API 齐全）→ 直接复用，不动 sys.path；
    2. 否则 append 内置 lib/ 后导入：磁盘合格版本优先命中，内嵌仅兜底；
    3. 不 purge sys.modules 的 websockets*（避免破坏其他插件）；过旧仅告警并按现状返回。
    """
    cached = sys.modules.get("websockets")
    if cached is not None and _is_websockets_compatible(cached):
        return cached
    setup_lib_path()
    try:
        import websockets
    except ImportError as e:  # pragma: no cover
        raise ImportError(f"无法导入 websockets 库: {e}")
    if not _is_websockets_compatible(websockets):
        _LOG.warning(
            "当前环境解析到的 websockets %s 版本过旧（需要 >= 14），连接适配器时"
            "可能因缺少 additional_headers 报 TypeError；为不破坏其他插件已加载的"
            "版本，不再强制切换内嵌版本",
            getattr(websockets, "__version__", "?"),
        )
    return websockets


setup_lib_path()
