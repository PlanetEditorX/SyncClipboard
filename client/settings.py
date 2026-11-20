"""客户端配置文件读写。

这里只保留纯 IO 逻辑：客户端设置界面已经由 ``gui.qt_main_window`` 提供，
原来的 tkinter / CustomTkinter 弹窗（open_client_settings）没有任何调用方，
随界面迁移一并移除。
"""

import json

from common.utils import BASE_DIR

CLIENT_CONFIG_FILE = BASE_DIR / "config" / "client_config.json"
CLIENT_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)


def load_client_config():
    with open(CLIENT_CONFIG_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_client_config(cfg):
    with open(CLIENT_CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
