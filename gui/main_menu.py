# gui/main_menu.py —— 客户端系统托盘（核心逻辑）
import tkinter as tk
import threading
import time
import requests
import pyperclip
import json
import logging
from pathlib import Path

logging.basicConfig(
    filename="client.log",
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

class SyncClient:
    """剪贴板同步客户端"""

    def __init__(self, config):
        self.server_url = f"http://{config['server_host']}:{config['server_port']}"
        self.key = config["key"]
        self.local_name = config["local_name"]
        self.last_text = ""
        self.running = False
        self.sent_texts = set()  # 已发送的文本去重

    def start(self):
        """启动剪贴板监听"""
        self.running = True
        self.last_text = pyperclip.paste()
        logging.info("客户端剪贴板监听启动")

        while self.running:
            try:
                text = pyperclip.paste()
                if text and text != self.last_text:
                    self.last_text = text
                    self.push_text(text)
            except Exception as e:
                logging.error(f"监听异常: {e}")
            time.sleep(0.5)

    def push_text(self, text):
        """推送文本到服务端"""
        if text in self.sent_texts:
            return  # 已发送过，避免重复

        try:
            resp = requests.post(
                f"{self.server_url}/text_sync",
                json={
                    "key": self.key,
                    "content": text,
                    "source": self.local_name
                },
                timeout=5
            )
            if resp.status_code == 200:
                self.sent_texts.add(text)
                logging.info(f"推送成功: {text[:50]}...")
            else:
                logging.warning(f"推送失败: {resp.status_code} {resp.text}")
        except requests.exceptions.RequestException as e:
            logging.error(f"连接服务端失败: {e}")

    def pull_text(self):
        """从服务端拉取最新文本（可选扩展）"""
        # 当前服务端没有提供拉取接口，可后续扩展
        pass

    def stop(self):
        self.running = False


def start_client_gui(config):
    """启动客户端 GUI"""
    client = SyncClient(config)

    # 启动监听线程
    watcher_thread = threading.Thread(target=client.start, daemon=True)
    watcher_thread.start()

    # 创建系统托盘
    import pystray
    from PIL import Image

    def on_quit(icon, item):
        client.stop()
        icon.stop()

    # 这里简化处理，实际可用 pystray 做系统托盘
    # 完整代码需要 pystray + PIL 库

    # 简易方案：直接进入 Tkinter 主循环（隐藏窗口 + 托盘）
    root = tk.Tk()
    root.withdraw()

    # 托盘菜单（简化版）
    menu = tk.Menu(root, tearoff=0)
    menu.add_command(label="设置", command=lambda: open_client_settings(config))
    menu.add_command(label="退出", command=lambda: (client.stop(), root.quit()))

    # 实际项目建议用 pystray 替代，这里先用 tkinter 演示逻辑
    root.mainloop()


def open_client_settings(config):
    """打开客户端设置"""
    import json
    from tkinter import simpledialog, messagebox

    root = tk.Tk()
    root.withdraw()

    host = simpledialog.askstring("设置", "服务器地址", initialvalue=config.get("server_host", "127.0.0.1"))
    port = simpledialog.askinteger("设置", "服务器端口", initialvalue=config.get("server_port", 8000))
    key = simpledialog.askstring("设置", "密钥", initialvalue=config.get("key", ""))
    local_name = simpledialog.askstring("设置", "本机名称", initialvalue=config.get("local_name", "PC-02"))

    if all([host, port, key, local_name]):
        config.update({
            "server_host": host,
            "server_port": port,
            "key": key,
            "local_name": local_name
        })
        with open("client_config.json", "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
        messagebox.showinfo("保存", "配置已保存，重启后生效")