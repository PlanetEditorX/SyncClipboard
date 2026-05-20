import tkinter as tk
from tkinter import simpledialog, messagebox
import json

CONFIG_FILE = "config.json"

def load_config():
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_config(cfg):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
        messagebox.showinfo("保存", "配置已保存")

def open_settings():
    cfg = load_config()
    root = tk.Tk()
    root.withdraw()

    key = simpledialog.askstring("设置", "请输入密钥", initialvalue=cfg.get("key",""))
    save_path = simpledialog.askstring("设置", "文件保存路径", initialvalue=cfg.get("save_path",""))
    port = simpledialog.askinteger("设置", "端口号", initialvalue=cfg.get("port",8000))
    local_name = simpledialog.askstring("设置", "本机名称", initialvalue=cfg.get("local_name","PC-01"))

    if key and save_path and port and local_name:
        cfg.update({"key": key, "save_path": save_path, "port": port, "local_name": local_name})
        save_config(cfg)