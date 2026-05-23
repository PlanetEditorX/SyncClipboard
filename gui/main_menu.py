# gui/main_menu.py
import threading
import time
import requests
import pyperclip
import json
import logging
from datetime import datetime
import sys
import os
from tkinter import Tk, filedialog
import win32clipboard

# 配置日志
logging.basicConfig(
    filename="client.log",
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

class SyncClient:
    """剪贴板同步客户端：推送本地变化 + 拉取远程最新"""
    def __init__(self, config):
        self.server_url = f"http://{config['server_host']}:{config['server_port']}"
        self.key = config["key"]
        self.local_name = config["local_name"]
        self.last_text = ""
        self.running = False
        self.last_remote_id = None          # 已拉取的远程条目id
        self._last_remote_content = None    # 最近一次远程设置的内容（用于防回传）
        self.push_thread = None
        self.pull_thread = None
        self.tray_icon = None

    def start(self):
        """启动推送、拉取和文件任务线程"""
        self.running = True
        self.last_text = pyperclip.paste()
        logging.info("客户端剪贴板监听启动")

        self.push_thread = threading.Thread(target=self._push_loop, daemon=True)
        self.push_thread.start()

        self.pull_thread = threading.Thread(target=self._pull_loop, daemon=True)
        self.pull_thread.start()

    def _push_loop(self):
        self.last_text = pyperclip.paste() or ""
        self.last_file_set = None   # 用于判断文件是否变化
        while self.running:
            try:
                # 1. 优先检测文件剪贴板
                files = get_clipboard_files()
                if files:
                    # 比较文件集合是否与上次一样
                    current_set = frozenset(files)
                    if current_set != self.last_file_set:
                        self.last_file_set = current_set
                        self._push_latest_file(files)
                    time.sleep(0.5)
                    continue  # 跳过文本处理

                # 2. 没有文件 → 处理文本
                self.last_file_set = None  # 清除文件状态，确保下次文件变化能被检测
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
        """定期从服务端拉取全局最新"""
        while self.running:
            try:
                resp = requests.get(
                    f"{self.server_url}/latest",
                    headers={"key": self.key},   # 将 key 放入请求头
                    timeout=5
                )
                if resp.status_code == 200:
                    data = resp.json()
                    latest = data.get("latest_global")
                    if latest and latest.get("source") != self.local_name:
                        if latest["id"] != self.last_remote_id:
                            # 更新本地剪贴板
                            pyperclip.copy(latest["content"])
                            self.last_remote_id = latest["id"]
                            self._last_remote_content = latest["content"]
                            self.last_text = latest["content"]
                            logging.info(f"拉取并更新剪贴板: {latest['content'][:50]} (来自 {latest['source']})")
                            # # 向服务端报告“已粘贴”
                            # try:
                            #     requests.post(
                            #         f"{self.server_url}/mark_pasted",
                            #         json={
                            #             "key": self.key,
                            #             "source": self.local_name,
                            #             "id": latest["id"],
                            #             "content": latest["content"],
                            #             "original_source": latest["source"]
                            #         },
                            #         timeout=5
                            #     )
                            #     logging.info(f"已上报粘贴: {latest['content'][:50]} (原始来源: {latest['source']})")
                            # except Exception as e:
                            #     logging.error(f"上报粘贴失败: {e}")
            except Exception as e:
                logging.error(f"拉取失败: {e}")
            time.sleep(5)  # 每5秒拉取一次

    def _push_latest_file(self, file_paths):
        """取第一个文件，推送元数据到服务端"""
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

    def _notify_user(self, message):
        """简单日志和托盘通知（需 pystray）"""
        logging.info(message)
        # 如果有托盘图标，可以调用 notify
        if self.tray_icon and hasattr(self.tray_icon, 'notify'):
            self.tray_icon.notify(message)

    def stop(self):
        """停止所有线程和托盘"""
        self.running = False
        if self.tray_icon:
            self.tray_icon.stop()
        logging.info("客户端已停止")

    def create_tray(self):
        """创建系统托盘图标（需要 pystray 和 Pillow）"""
        try:
            import pystray
            from PIL import Image, ImageDraw
        except ImportError:
            logging.warning("pystray 或 Pillow 未安装，托盘功能不可用。")
            print("提示：如需系统托盘，请执行 pip install pystray Pillow")
            return

        # 生成一个简单的托盘图标
        image = Image.new("RGB", (64, 64), "black")
        dc = ImageDraw.Draw(image)
        dc.rectangle((16, 16, 48, 48), fill="white")

        menu = pystray.Menu(
            pystray.MenuItem("退出", lambda icon, item: self.stop())
        )
        self.tray_icon = pystray.Icon("clipboard_sync", image, "剪贴板同步", menu)
        # 在独立线程中运行托盘（避免阻塞主线程）
        threading.Thread(target=self.tray_icon.run, daemon=True).start()

def get_clipboard_files():
    """返回剪贴板中的文件路径列表，如果不是文件则返回 None"""
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

def start_client_gui(config):
    """客户端入口：创建 SyncClient 实例并启动"""
    client = SyncClient(config)
    client.start()
    client.create_tray()  # 如果有依赖则显示托盘，否则仅后台运行
    return client