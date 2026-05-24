# client/main_menu.py
import os
import sys
import time
import json
import logging
import requests
import threading
import pyperclip
import win32clipboard
from pathlib import Path
from datetime import datetime
from logging.handlers import RotatingFileHandler

# ---------- 日志配置 ----------
LOG_FILE = Path("log/client.log")
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

handler = RotatingFileHandler(LOG_FILE, maxBytes=1*1024*1024, backupCount=1, encoding='utf-8')
handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))

logger = logging.getLogger()
logger.setLevel(logging.INFO)
logger.addHandler(handler)

class SyncClient:
    """剪贴板同步客户端：推送本地变化 + 拉取远程最新（纯后台版本）"""
    def __init__(self, config):
        self.server_url = f"http://{config['server_host']}:{config['server_port']}"
        self.key = config["key"]
        self.local_name = config["local_name"]
        self.last_text = ""
        self.running = False
        self.last_remote_id = None
        self._last_remote_content = None
        self.push_thread = None
        self.pull_thread = None

    def start(self):
        self.running = True
        self.last_text = pyperclip.paste()
        logging.info("客户端剪贴板监听启动")

        self.push_thread = threading.Thread(target=self._push_loop, daemon=True)
        self.push_thread.start()

        self.pull_thread = threading.Thread(target=self._pull_loop, daemon=True)
        self.pull_thread.start()

    def _push_loop(self):
        self.last_text = pyperclip.paste() or ""
        self.last_file_set = None
        while self.running:
            try:
                files = get_clipboard_files()
                if files:
                    current_set = frozenset(files)
                    if current_set != self.last_file_set:
                        self.last_file_set = current_set
                        self._push_latest_file(files)
                    time.sleep(0.5)
                    continue

                self.last_file_set = None
                text = pyperclip.paste()
                if text is None:
                    text = ""
                if text != self.last_text:
                    if text and text != self._last_remote_content:
                        self.push_text(text)
                    self.last_text = text
            except Exception as e:
                logging.error(f"监听异常: {e}")
            time.sleep(0.5)

    def push_text(self, text):
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
                logging.info(f"推送成功: {text[:50]}...")
            else:
                logging.warning(f"推送失败: {resp.status_code} {resp.text}")
        except Exception as e:
            logging.error(f"连接服务端失败: {e}")

    def _pull_loop(self):
        while self.running:
            try:
                resp = requests.get(
                    f"{self.server_url}/latest?source={self.local_name}",
                    headers={
                        "key": self.key
                    },
                    timeout=5
                )
                if resp.status_code == 200:
                    data = resp.json()
                    latest = data.get("latest_global")
                    if latest and latest.get("source") != self.local_name:
                        if latest["id"] != self.last_remote_id:
                            pyperclip.copy(latest["content"])
                            self.last_remote_id = latest["id"]
                            self._last_remote_content = latest["content"]
                            self.last_text = latest["content"]
                            logging.info(f"拉取并更新剪贴板: {latest['content'][:50]} (来自 {latest['source']})")
            except Exception as e:
                logging.error(f"拉取失败: {e}")
            time.sleep(3)

    def _push_latest_file(self, file_paths):
        if not file_paths:
            return
        path = file_paths[0]
        if not os.path.isfile(path):
            return
        name = os.path.basename(path)
        size = os.path.getsize(path)
        try:
            resp = requests.post(
                f"{self.server_url}/file_sync",
                headers={"key": self.key},
                json={
                    "path": path,
                    "name": name,
                    "size": size,
                    "source": self.local_name
                },
                timeout=5
            )
            if resp.status_code == 200:
                logging.info(f"文件路径已同步: {name} ({size} bytes)")
        except Exception as e:
            logging.error(f"同步文件路径失败: {e}")

    def stop(self):
        self.running = False
        logging.info("客户端已停止")

def get_clipboard_files():
    try:
        win32clipboard.OpenClipboard()
        if win32clipboard.IsClipboardFormatAvailable(win32clipboard.CF_HDROP):
            files = win32clipboard.GetClipboardData(win32clipboard.CF_HDROP)
            win32clipboard.CloseClipboard()
            return list(files)
        win32clipboard.CloseClipboard()
        return None
    except Exception:
        return None