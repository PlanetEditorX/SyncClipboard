# client_main.py —— 客户端入口
import json
import sys
import os

# 确保能找到 gui 目录
sys.path.insert(0, os.path.dirname(__file__))

from gui.main_menu import start_client_gui

CONFIG_FILE = "client_config.json"

def load_config():
    if not os.path.exists(CONFIG_FILE):
        # 首次运行创建默认配置
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

if __name__ == "__main__":
    config = load_config()
    start_client_gui(config)