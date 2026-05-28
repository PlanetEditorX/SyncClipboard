# client/settings.py —— 客户端设置界面
import tkinter as tk
from tkinter import simpledialog, messagebox
import json
from pathlib import Path
from common.tools import BASE_DIR

CONFIG_FILE = BASE_DIR / "config" / "client_config.json"
CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)

def load_client_config():
    with open(CLIENT_CONFIG_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_client_config(cfg):
    with open(CLIENT_CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    messagebox.showinfo("保存", "配置已保存")

def open_client_settings():
    cfg = load_client_config()
    root = tk.Tk()
    root.withdraw()

    host = simpledialog.askstring("设置", "服务器地址", initialvalue=cfg.get("server_host", ""))
    port = simpledialog.askinteger("设置", "服务器端口", initialvalue=cfg.get("server_port", 8000))
    key = simpledialog.askstring("设置", "密钥", initialvalue=cfg.get("key", ""))
    local_name = simpledialog.askstring("设置", "本机名称", initialvalue=cfg.get("local_name", "PC-02"))

    if all([host, port, key, local_name]):
        cfg.update({
            "server_host": host,
            "server_port": port,
            "key": key,
            "local_name": local_name
        })
        save_client_config(cfg)