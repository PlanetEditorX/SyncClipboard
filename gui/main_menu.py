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
        """启动推送和拉取线程"""
        self.running = True
        self.last_text = pyperclip.paste()
        logging.info("客户端剪贴板监听启动")

        self.push_thread = threading.Thread(target=self._push_loop, daemon=True)
        self.push_thread.start()

        self.pull_thread = threading.Thread(target=self._pull_loop, daemon=True)
        self.pull_thread.start()

    def _push_loop(self):
        while self.running:
            try:
                text = pyperclip.paste()
                if text and text != self.last_text:
                    # 如果是远程同步来的内容，不要回传
                    if text != self._last_remote_content:
                        self.push_text(text)
                    self.last_text = text
            except Exception as e:
                logging.error(f"剪贴板监听异常: {e}")
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
            time.sleep(2)  # 每2秒拉取一次

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


def start_client_gui(config):
    """客户端入口：创建 SyncClient 实例并启动"""
    client = SyncClient(config)
    client.start()
    client.create_tray()  # 如果有依赖则显示托盘，否则仅后台运行
    return client