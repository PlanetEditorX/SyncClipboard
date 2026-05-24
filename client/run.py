# client/run.py
import json
import os
import sys
import signal
import time
from pathlib import Path
from client.main_menu import SyncClient
from common.path import BASE_DIR

CONFIG_FILE = BASE_DIR / "config" / "client_config.json"
CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)

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
    client = SyncClient(config)

    def graceful_exit(signum, frame):
        print("\n正在关闭客户端...")
        client.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, graceful_exit)
    signal.signal(signal.SIGTERM, graceful_exit)

    client.start()

    # 保持进程存活，等待信号
    try:
        while client.running:
            time.sleep(1)
    except KeyboardInterrupt:
        graceful_exit(None, None)

if __name__ == "__main__":
    main()