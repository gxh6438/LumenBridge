"""OneBot v11 协议层：适配器、适配器枢纽、消息段构建、数据包构建、事件分发"""

from . import message, packets
from .adapter import OneBotAdapter
from .dispatcher import EventDispatcher
from .hub import AdapterHub

__all__ = ["OneBotAdapter", "AdapterHub", "EventDispatcher", "message", "packets"]
