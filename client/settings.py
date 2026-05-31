# client/settings.py —— 客户端设置界面
import json
import tkinter as tk
from pathlib import Path
from common.utils import BASE_DIR, get_tk_root
from tkinter import simpledialog, messagebox

try:
    import customtkinter as ctk
except ImportError:
    ctk = None

CLIENT_CONFIG_FILE = BASE_DIR / "config" / "client_config.json"
CLIENT_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)

def load_client_config():
    with open(CLIENT_CONFIG_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_client_config(cfg):
    with open(CLIENT_CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    messagebox.showinfo("保存", "配置已保存")

def open_client_settings():
    root = get_tk_root()
    if root is None:
        if ctk is not None and hasattr(ctk, 'CTk'):
            temp_root = ctk.CTk()
        else:
            temp_root = tk.Tk()
        temp_root.withdraw()
        master = temp_root
    else:
        master = root
    cfg = load_client_config()
    # 使用 simpledialog 并传入 parent
    host = simpledialog.askstring("设置", "服务器地址", parent=master, initialvalue=cfg.get("server_host", ""))
    port = simpledialog.askinteger("设置", "服务器端口", parent=master, initialvalue=cfg.get("server_port", 8000))
    key = simpledialog.askstring("设置", "密钥", parent=master, initialvalue=cfg.get("key", ""))
    local_name = simpledialog.askstring("设置", "本机名称", parent=master, initialvalue=cfg.get("local_name", "PC-02"))

    if host and port and key and local_name:
        cfg.update({
            "server_host": host,
            "server_port": port,
            "key": key,
            "local_name": local_name
        })
        save_client_config(cfg)