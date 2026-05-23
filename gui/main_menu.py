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

        # 修正：正确创建并启动文件任务线程
        self.file_task_thread = threading.Thread(target=self._file_task_worker, daemon=True)
        self.file_task_thread.start()

    def _push_loop(self):
        self.last_text = pyperclip.paste()
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
                    self.last_text = pyperclip.paste()  # 更新文本状态，防止后面误判
                    continue

                # 2. 没有文件，检测文本
                text = pyperclip.paste()
                if text and text != self.last_text:
                    if text != self._last_remote_content:
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

    def _file_task_worker(self):
        """轮询上传任务（手机请求的文件）和下载任务（手机主动发来的文件）"""
        while self.running:
            try:
                # 1. 查询待上传任务（手机请求了我们的文件）
                resp = requests.get(
                    f"{self.server_url}/pending_uploads",
                    headers={"key": self.key},
                    params={"client": self.local_name},
                    timeout=5
                )
                if resp.status_code == 200:
                    tasks = resp.json().get("tasks", [])
                    for task in tasks:
                        # 根据 file_name 找到本地文件路径
                        # 这里需要一个用户交互：让用户选择文件。简化做法：直接根据文件名在常用目录搜索
                        # 实际项目中可以弹出文件选择对话框或预先配置共享文件夹
                        local_path = self._find_local_file(task["file_name"])
                        if local_path:
                            self._upload_file_for_task(local_path, task["token"])
                        else:
                            logging.warning(f"未找到文件: {task['file_name']}")

                # 2. 查询待下载任务（手机发来的文件）
                resp2 = requests.get(
                    f"{self.server_url}/pending_downloads",
                    headers={"key": self.key},
                    params={"client": self.local_name},
                    timeout=5
                )
                if resp2.status_code == 200:
                    tasks = resp2.json().get("tasks", [])
                    for task in tasks:
                        # 通知用户有新文件，让用户决定是否下载
                        # 这里可以弹出通知或托盘菜单项
                        self._notify_user(f"收到文件 {task['file_name']}，来自 {task['uploaded_by']}，token={task['token']}")
            except Exception as e:
                logging.error(f"文件任务轮询异常: {e}")
            time.sleep(5)   # 每5秒轮询一次

    def _find_local_file(self, filename):
        # 先尝试预定义目录
        search_dirs = [
            os.path.expanduser("~/Desktop"),
            os.path.expanduser("~/Downloads"),
            # 可添加其他固定目录
        ]
        # 如果 config 中有额外目录，也加入
        extra_dirs = getattr(self, 'file_search_dirs', [])
        for d in search_dirs + extra_dirs:
            candidate = os.path.join(d, filename)
            if os.path.isfile(candidate):
                return candidate
        # 弹窗让用户选择
        root = Tk()
        root.withdraw()
        filepath = filedialog.askopenfilename(title=f"请选择文件: {filename}")
        root.destroy()
        return filepath if filepath else None

    def _upload_file_for_task(self, filepath, token):
        try:
            with open(filepath, "rb") as f:
                resp = requests.post(
                    f"{self.server_url}/upload_file",
                    headers={"key": self.key},
                    files={"file": f},
                    data={
                        "token": token,
                        "source": self.local_name
                    },
                    timeout=30
                )
            if resp.status_code == 200:
                logging.info(f"文件上传成功: {filepath}")
        except Exception as e:
            logging.error(f"文件上传失败: {e}")

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