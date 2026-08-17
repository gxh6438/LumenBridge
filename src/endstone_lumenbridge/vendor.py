"""内置第三方库加载器：websockets 内嵌在 lib/ 随插件分发，避免手动安装依赖。

M25：lib/ 目录改为追加到 sys.path 尾部（append 而非 insert(0)）——磁盘上
已安装的合格 websockets（>= 14）优先解析，内嵌版本仅在环境中没有任何可用
websockets 时兜底命中，不抢占系统/其他插件的版本解析优先级。
"""

import logging
import sys
from pathlib import Path
from typing import Any

_LOG = logging.getLogger(__name__)


def setup_lib_path() -> None:
    """将插件内置的 lib 目录追加到模块搜索路径尾部（M25：append 而非 insert(0)）。

    追加到尾部意味着 site-packages 等既有路径中的已安装版本优先解析，
    内嵌版本只作"磁盘上没有 websockets"时的兜底，不影响他方版本。
    """
    lib_path = str(Path(__file__).parent / "lib")
    if lib_path not in sys.path:
        sys.path.append(lib_path)


def _is_websockets_compatible(module: Any) -> bool:
    """检测已导入的 websockets 是否满足 onebot 适配器的 API 需求。

    onebot/adapter.py 与 qqofficial_adapter.py 使用新式 asyncio 接口：
    websockets.connect(..., additional_headers=...) 与 websockets.serve(...)，
    其中 additional_headers 参数要求 websockets >= 14；更旧版本（legacy 客户端
    extra_headers 时代）会在建立连接时抛 TypeError。
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
    return parts >= (14, 0)


def import_websockets() -> Any:
    """导入 websockets：优先复用进程中已加载且合格的版本（不破坏其他插件）。

    M25 行为约定（返回值语义不变：成功返回模块，失败抛 ImportError）：

    1. sys.modules 已有合格 websockets（版本 >= 14，或已由他方提供且 API
       齐全）→ 直接复用，不再注入任何路径；
    2. 否则把内置 lib/ 追加到 sys.path 尾部后再导入——磁盘上已安装的合格
       版本位于更前的搜索路径会优先命中，内嵌版本仅兜底；
    3. 不再 purge sys.modules 中的 websockets*（旧实现的全量清理会破坏进程
       内其他已依赖该版本的插件）；若最终命中的是过旧版本，仅告警提示连接
       时可能因缺少 additional_headers 报错，按现状返回该模块。
    """
    cached = sys.modules.get("websockets")
    if cached is not None and _is_websockets_compatible(cached):
        # 已由本插件或他方加载且合格：直接复用，不动 sys.path
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
