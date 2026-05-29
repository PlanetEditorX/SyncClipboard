import logging
from PIL import Image
import pystray
from pathlib import Path
from common.utils import BASE_DIR
from gui.config_manager import ConfigManager
from gui.service_manager import ServiceManager
from gui.file_handler import FileHandler
from gui.clipboard_handler import ClipboardHandler
from gui.file_watcher_handler import FileWatcherHandler
from gui.tray_menu import TrayMenu

logger = logging.getLogger("gui")


class TrayManager:
    """托盘管理器 - 核心调度"""
    def __init__(self):
        self.config = ConfigManager()
        self.services = ServiceManager(self.config)
        self.file_handler = FileHandler(self.config)
        self.clipboard_handler = ClipboardHandler(self.config, self)
        self.watcher = FileWatcherHandler(self.clipboard_handler, self.config)
        self.menu_builder = TrayMenu(self)
        self.icon = None

        # 加载状态和配置
        self.config.load_state()

    def update_icon(self):
        """更新托盘图标"""
        if not self.icon:
            return
        active = self.services.server_running or self.services.client_running
        icon_name = "icon-active.png" if active else "icon-stop.png"
        icon_path = BASE_DIR / "gui" / "icon" / icon_name
        if icon_path.exists():
            self.icon.icon = Image.open(icon_path)

    def on_left_click(self, icon):
        """左键点击"""
        self.file_handler.fetch_file()

    def quit_app(self, icon):
        """退出程序"""
        keep_server = self.services.server_running
        keep_client = self.services.client_running

        self.services.stop_server()
        self.services.stop_client()

        self.config.server_running = keep_server
        self.config.client_running = keep_client
        self.config.save_state()

        icon.stop()
        import os
        os._exit(0)

    def run(self):
        """运行托盘"""
        # 加载客户端配置
        if not self.config.load_client_config():
            return

        # 设置图标
        active = self.services.server_running or self.services.client_running
        icon_name = "icon-active.png" if active else "icon-stop.png"
        icon_path = BASE_DIR / "gui" / "icon" / icon_name

        if icon_path.exists():
            image = Image.open(icon_path)
        else:
            image = Image.new("RGB", (64, 64), "blue")

        # 恢复服务状态
        logger.info(f"状态恢复: server={self.config.server_running}, client={self.config.client_running}")
        if self.config.server_running:
            self.services.start_server()
        if self.config.client_running:
            self.services.start_client()

        # 启动文件监控
        self.watcher.start()

        # 启动托盘
        self.icon = pystray.Icon("SyncClipboard", image, "SyncClipboard", self.menu_builder.create())
        self.icon.on_click = self.on_left_click
        self.update_icon()
        self.icon.run()

        # 托盘退出后清理
        self.watcher.stop()