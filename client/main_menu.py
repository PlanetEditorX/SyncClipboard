# client/main_menu.py
import os
import sys
import uuid
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
from common.tools import BASE_DIR, SAFE_POST
from server.services.client_tracker import ClientTracker

# ---------- 日志配置 ----------
LOG_FILE = BASE_DIR / "log" / "client.log"
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

handler = RotatingFileHandler(LOG_FILE, maxBytes=1*1024*1024, backupCount=1, encoding='utf-8')
handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))

logger = logging.getLogger()
logger.setLevel(logging.INFO)
logger.addHandler(handler)

class SyncClient:
    """剪贴板同步客户端：推送本地变化 + 拉取远程最新（纯后台版本）"""
    def __init__(self, config, file_server=None):
        self.server_url = f"http://{config['server_host']}:{config['server_port']}"
        self.key = config["key"]
        self.local_name = config["local_name"]
        self.last_text = ""
        self.running = False
        self.last_remote_id = None
        self._last_remote_content = None
        self.push_thread = None
        self.pull_thread = None
        # 文件服务
        self.file_server = file_server
        # 全局锁，避免同时读写剪贴板
        self.clipboard_lock = threading.Lock()
        self.tracker = ClientTracker()

    def safe_paste(self, retries=5):
        for _ in range(retries):
            try:
                return pyperclip.paste()
            except Exception:
                time.sleep(0.05)
        return ""

    def start(self):
        self.running = True
        self.last_text = self.safe_paste()
        logging.info("客户端剪贴板监听启动")

        # 推送线程
        self.push_thread = threading.Thread(target=self._push_loop, daemon=True)
        self.push_thread.start()

        # self.pull_thread = threading.Thread(target=self._pull_loop, daemon=True)
        # self.pull_thread.start()

    def _push_loop(self):
        self.last_text = self.safe_paste() or ""
        self.last_file_set = None
        while self.running:
            try:
                files = get_clipboard_files()
                if files:
                    current_set = frozenset(files)
                    if current_set != self.last_file_set:
                        self.last_file_set = current_set
                        self._push_latest_file(files)
                    time.sleep(1)
                    continue

                self.last_file_set = None
                with self.clipboard_lock:
                    text = self.safe_paste()
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
            latest_global = self.tracker.get_global_latest()
            if latest_global == None or latest_global["content"] != text:
                resp = SAFE_POST(
                    f"{self.server_url}/text_sync",
                    json={
                        "key": self.key,
                        "content": text,
                        "source": self.local_name
                    },
                    timeout=30
                )
                if resp.status_code == 200:
                    logging.info(f"推送成功: {text[:50]}...")
                else:
                    logging.warning(f"推送失败: {resp.status_code} {resp.text}")
        except Exception:
            logger.exception("连接服务端失败")

    def _push_latest_file(self, file_paths):
        """
        推送最新的复制文件
        """
        if not file_paths:
            return
        path = file_paths[0]
        if not os.path.isfile(path):
            return
        name = os.path.basename(path)
        size = os.path.getsize(path)
        file_id = str(uuid.uuid4())
        try:
            # 注册到 FileServer
            if self.file_server:
                self.file_server.register_file(
                    file_id,
                    path
                )
            resp = requests.post(
                f"{self.server_url}/file_sync",
                headers={"key": self.key},
                json={
                    "file_id": file_id,
                    "path": path,
                    "name": name,
                    "size": size,
                    "source": self.local_name,
                    "port": self.file_server.port
                },
                timeout=5
            )
            if resp.status_code == 200:
                logging.info(f"文件路径已同步: {name} ({size} bytes)")
        except Exception as e:
            logging.error(f"同步文件路径失败: {e}")

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
                            with self.clipboard_lock:
                                pyperclip.copy(latest["content"])
                            self.last_remote_id = latest["id"]
                            self._last_remote_content = latest["content"]
                            self.last_text = latest["content"]
                            logging.info(f"拉取并更新剪贴板: {latest['content'][:50]} (来自 {latest['source']})")
            except Exception as e:
                logging.error(f"拉取失败: {e} 等待10秒后重试")
                time.sleep(7)
            time.sleep(3)

    def stop(self):
        self.running = False
        logging.info("客户端已停止")

def get_clipboard_files():
    """
    获取剪贴板中的文件路径列表。
    如果剪贴板中包含从资源管理器复制的文件，则返回文件路径列表；
    否则返回 None。
    """
    try:
        # 打开剪贴板
        win32clipboard.OpenClipboard()
        if win32clipboard.IsClipboardFormatAvailable(
                win32clipboard.CF_HDROP):
            return list(
                win32clipboard.GetClipboardData(
                    win32clipboard.CF_HDROP
                )
            )
        return None
    except Exception:
        return None
    finally:
        try:
            # 关闭剪贴板，释放系统资源
            win32clipboard.CloseClipboard()
        except:
            pass