from __future__ import annotations

import signal
import sys
import threading
import time
from pathlib import Path

# 演练服务必须优先加载当前工作树，不能误用环境中较旧的已安装 wheel。
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from endstone_lumenbridge.config import ConfigManager
from endstone_lumenbridge.connections import ConnectionManager
from endstone_lumenbridge.event_bus import EventBus
from endstone_lumenbridge.onebot import AdapterHub
from endstone_lumenbridge.webui.logbuffer import LogBuffer
from endstone_lumenbridge.webui.server import WebUIServer


class Logger:
    def info(self, message: object) -> None:
        print(f"INFO {message}", flush=True)

    def warning(self, message: object) -> None:
        print(f"WARN {message}", flush=True)

    def error(self, message: object) -> None:
        print(f"ERROR {message}", flush=True)

    def debug(self, message: object) -> None:
        print(f"DEBUG {message}", flush=True)


class Adapter:
    ws_type = 0
    mode_name = "Forward WebSocket"
    is_connected = True


class Server:
    online_players: list[object] = []


class Plugin:
    VERSION = "1.1.0-test"

    def __init__(self, data_folder: Path) -> None:
        self.logger = Logger()
        self._tee_logger = self.logger
        self.data_folder = data_folder
        self.config_manager = ConfigManager(data_folder, self.logger)
        self.config_manager.apply_patch({
            "webui": {"host": "127.0.0.1", "port": 18300, "password": "webui-test-password", "secret": "test-secret"},
            "connection": {"access_token": "test-onebot-token"},
        })
        self.log_buffer = LogBuffer()
        self.bus = EventBus(self.logger)
        # v1.2.0 连接配置：提供 connections + hub，供 /api/connections 浏览器演练
        self.connections = ConnectionManager(data_folder, self.logger)
        self.hub = AdapterHub(self.logger, self.bus, self.connections)
        self.adapter = self.hub
        self.server = Server()
        self.whitelist_module = None
        self.regex_module = None
        self.subplugin_manager = None
        self.language = "en"
        self._pip_serial_lock = threading.RLock()
        self._pip_manager = None

    def reload_onebot_connection(self) -> None:
        """浏览器演练用的空实现（不真正建立 WS 连接）"""
        return None

    def run_on_main(self, callback, delay: int = 1) -> None:
        callback()

    def bot_profile_snapshot(self) -> dict[str, object]:
        return {"qq": 12345, "nickname": "Lumen Test Bot", "avatar_url": ""}


def main() -> None:
    root = Path("/tmp/lumenbridge_webui_browser_test")
    root.mkdir(parents=True, exist_ok=True)
    plugin = Plugin(root)
    webui = WebUIServer(plugin)
    plugin.webui = webui
    webui.start()
    plugin.log_buffer.push("info", "LumenBridge", "WebUI browser regression server is ready")
    print("WEBUI_READY http://127.0.0.1:18300 password=webui-test-password", flush=True)

    stopped = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_args: stopped.set())
    signal.signal(signal.SIGINT, lambda *_args: stopped.set())
    while not stopped.wait(0.5):
        pass
    webui.stop()


if __name__ == "__main__":
    main()
