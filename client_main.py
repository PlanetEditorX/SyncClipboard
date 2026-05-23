# client_main.py
import json
import sys
import os
import signal
import threading

sys.path.insert(0, os.path.dirname(__file__))

from gui.main_menu import start_client_gui

CONFIG_FILE = "client_config.json"

def load_config():
    if not os.path.exists(CONFIG_FILE):
        default_config = {
            "server_host": "127.0.0.1",
            "server_port": 8000,
            "key": "123456",
            "local_name": "PC-01"
        }
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(default_config, f, ensure_ascii=False, indent=2)
        return default_config
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def main():
    config = load_config()
    client = start_client_gui(config)

    # 注册信号处理，以便在终端按 Ctrl+C 时优雅退出
    def graceful_exit(signum, frame):
        print("\n正在关闭客户端...")
        client.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, graceful_exit)
    signal.signal(signal.SIGTERM, graceful_exit)

    # 保持主线程存活，等待信号
    try:
        while client.running:
            threading.Event().wait(1)
    except KeyboardInterrupt:
        graceful_exit(None, None)

if __name__ == "__main__":
    main()