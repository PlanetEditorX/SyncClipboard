import json
import logging
import sys
import os
import subprocess
import time
from pathlib import Path

import pystray
from pystray import MenuItem, Menu
from PIL import Image
import winreg   # 仅 Windows，若需跨平台请自行替换
import multiprocessing
from server.run import main as server_main
from client.run import main as client_main

# ---------- 路径配置 ----------
# 项目根目录
from common.path import BASE_DIR

CLIENT_CONFIG = BASE_DIR / "config" / "client_config.json"
SERVER_CONFIG = BASE_DIR / "config" / "server_config.json"
STATE_FILE = BASE_DIR / "config" / "gui_state.json"
CLIENT_CONFIG.parent.mkdir(parents=True, exist_ok=True)

logger = logging.getLogger("gui")

class TrayManager:
    def __init__(self):
        self.icon = None
        self.server_process = None
        self.client_process = None
        self.server_running = False
        self.client_running = False
        # 加载上次运行状态
        self.load_state()

    # -------- 状态持久化 --------
    def load_state(self):
        if STATE_FILE.exists():
            try:
                with open(STATE_FILE, 'r', encoding='utf-8') as f:
                    s = json.load(f)
                self.server_running = s.get('server_running', False)
                self.client_running = s.get('client_running', False)
            except Exception:
                pass

    def save_state(self):
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(STATE_FILE, 'w', encoding='utf-8') as f:
            json.dump({
                'server_running': self.server_running,
                'client_running': self.client_running
            }, f)

    # -------- 图标 --------
    def update_icon(self):
        """根据运行状态更新托盘图标"""
        if not self.icon:
            return
        active = self.server_running or self.client_running
        icon_name = "icon-active.png" if active else "icon-stop.png"
        icon_path = BASE_DIR / "gui" / "icon" / icon_name

        if icon_path.exists():
            self.icon.icon = Image.open(icon_path)

    # -------- 开机启动（Windows）--------
    def _autostart_key(self):
        return r"Software\Microsoft\Windows\CurrentVersion\Run", "SyncClipboardTray"

    def is_autostart_enabled(self):
        try:
            key_path, name = self._autostart_key()
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_READ) as key:
                winreg.QueryValueEx(key, name)
                return True
        except FileNotFoundError:
            return False

    def toggle_autostart(self, icon, item):
        key_path, name = self._autostart_key()

        try:
            if item.checked:
                with winreg.OpenKey(
                    winreg.HKEY_CURRENT_USER,
                    key_path,
                    0,
                    winreg.KEY_SET_VALUE
                ) as key:
                    winreg.DeleteValue(key, name)

            else:
                if getattr(sys, "frozen", False):
                    cmd = f'"{sys.executable}"'
                else:
                    cmd = f'"{sys.executable}" -m gui.run'

                with winreg.OpenKey(
                    winreg.HKEY_CURRENT_USER,
                    key_path,
                    0,
                    winreg.KEY_SET_VALUE
                ) as key:
                    winreg.SetValueEx(
                        key,
                        name,
                        0,
                        winreg.REG_SZ,
                        cmd
                    )

        except Exception as e:
            logger.error(f"开机启动操作失败: {e}")

    # -------- 服务器启停 --------
    def start_server(self, icon=None, item=None):
        logger.info(f"尝试启动服务器，当前状态: running={self.server_running}")
        # 如果已有活着的进程，直接更新状态
        if self.server_process and self.server_process.is_alive():
            self.server_running = True
            if icon: icon.update_menu()
            return

        try:
            self.server_process = multiprocessing.Process(
                target=server_main,
                daemon=True   # 主进程退出时自动终止
            )
            self.server_process.start()
            self.server_running = True
            self.save_state()
            self.update_icon()
            logger.info("服务器已启动")
        except Exception as e:
            logger.error(f"服务器启动失败: {e}")
            self.server_running = False
        if icon:
            icon.update_menu()

    def stop_server(self):
        if self.server_process and self.server_process.is_alive():
            self.server_process.terminate()
            self.server_process.join(timeout=5)
            if self.server_process.is_alive():
                self.server_process.kill()
            self.server_process = None
        self.server_running = False
        self.save_state()
        self.update_icon()
        logger.info("服务器已停止")

    def toggle_server(self, icon, item):
        if self.server_running:
            self.stop_server()
        else:
            self.start_server(icon, item)
        icon.update_menu()

    # -------- 客户端启停 --------
    def start_client(self, icon=None, item=None):
        logger.info(f"尝试启动客户端，当前状态: running={self.client_running}")
        if self.client_process and self.client_process.is_alive():
            self.client_running = True
            if icon: icon.update_menu()
            return
        try:
            self.client_process = multiprocessing.Process(
                target=client_main,
                daemon=True
            )
            self.client_process.start()
            self.client_running = True
            self.save_state()
            self.update_icon()
            logger.info("客户端已启动")
        except Exception as e:
            logger.error(f"客户端启动失败: {e}")
            self.client_running = False
        if icon:
            icon.update_menu()

    def stop_client(self):
        if self.client_process and self.client_process.is_alive():
            self.client_process.terminate()
            self.client_process.join(timeout=5)
            if self.client_process.is_alive():
                self.client_process.kill()
            self.client_process = None
        self.client_running = False
        self.save_state()
        self.update_icon()
        logger.info("客户端已停止")

    def toggle_client(self, icon, item):
        if self.client_running:
            self.stop_client()
        else:
            self.start_client(icon, item)
        icon.update_menu()

    # -------- 重启服务 --------
    def restart_services(self):
        logger.info("正在重启服务...")
        if self.server_running:
            self.stop_server()
        if self.client_running:
            self.stop_client()
        time.sleep(1)
        self.start_server()
        self.start_client()
        if self.icon:
            self.icon.update_menu()

    # -------- 编辑配置文件 --------
    def edit_client_config(self):
        if CLIENT_CONFIG.exists():
            os.startfile(CLIENT_CONFIG)
        else:
            logger.warning("客户端配置文件不存在")

    def edit_server_config(self):
        if SERVER_CONFIG.exists():
            os.startfile(SERVER_CONFIG)
        else:
            logger.warning("服务器配置文件不存在")

    # -------- 退出程序 --------
    def quit_app(self, icon):
        # 1. 记住退出前的运行状态
        keep_server = self.server_running
        keep_client = self.client_running

        # 2. 停止所有服务（这会修改 self.server_running = False 并保存）
        self.stop_server()
        self.stop_client()

        # 3. 恢复真实的意图状态，并保存到文件
        self.server_running = keep_server
        self.client_running = keep_client
        self.save_state()

        # 4. 退出托盘
        icon.stop()
        os._exit(0)

    # -------- 状态查询（供菜单 checked 使用）--------
    def is_server_running(self):
        return self.server_running

    def is_client_running(self):
        return self.client_running

    # -------- 菜单构建 --------
    def create_menu(self):
        return Menu(
            MenuItem(
                '开机启动',
                self.toggle_autostart,
                checked=lambda item: self.is_autostart_enabled()
            ),
            Menu.SEPARATOR,
            MenuItem(
                '启动服务器',
                self.toggle_server,
                checked=lambda item: self.is_server_running()
            ),
            MenuItem(
                '启动客户端',
                self.toggle_client,
                checked=lambda item: self.is_client_running()
            ),
            Menu.SEPARATOR,
            MenuItem('修改客户端配置', self.edit_client_config),
            MenuItem('修改服务器配置', self.edit_server_config),
            Menu.SEPARATOR,
            MenuItem('重启服务', self.restart_services),
            Menu.SEPARATOR,
            MenuItem('退出', self.quit_app)
        )

    # -------- 运行托盘 --------
    def run(self):
        active = self.server_running or self.client_running

        icon_name = "icon-active.png" if active else "icon-stop.png"
        icon_path = BASE_DIR / "gui" / "icon" / icon_name

        if icon_path.exists():
            image = Image.open(icon_path)
        else:
            image = Image.new("RGB", (64, 64), "blue")

        # 根据历史状态自动恢复服务
        logger.info(f"状态恢复: server={self.server_running}, client={self.client_running}")
        if self.server_running:
            self.start_server()   # 不带 icon，容错
        if self.client_running:
            self.start_client()

        self.icon = pystray.Icon("SyncClipboard", image, "SyncClipboard", self.create_menu())
        self.update_icon()
        self.icon.run()