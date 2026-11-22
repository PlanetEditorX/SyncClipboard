"""托盘与桌面窗口编排（Qt 版）。

沿用原 Tk 版的职责划分：这里只负责「把各部分接起来」——
配置、服务进程、文件监控、剪贴板回调、单实例唤出、托盘菜单。

有两点与 Tk 版不同：
1. 托盘从 pystray 换成 QSystemTrayIcon，不再需要「在子线程里跑另一个
   消息循环」，避免和 Qt 事件循环抢 Win32 消息；
2. UI 投递仍走 common/ui_bridge 的队列，由 QTimer 周期抽干，所以
   file_handler 等调用方无需改动。
"""

import logging
import os
import sys
import threading

from PySide6.QtCore import QObject, QTimer
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from common import ui_bridge
from common.single_instance import start_show_watcher
from common.ui_bridge import post_to_main_thread_no_wait, request_ui_shutdown
from common.utils import BASE_DIR
from gui import qt_dialogs
from gui.clipboard_handler import ClipboardHandler
from gui.config_manager import ConfigManager
from gui.file_handler import FileHandler
from gui.file_watcher_handler import FileWatcherHandler
from gui.icon_theme import icon_path, is_active_state
from gui.qt_main_window import MainWindow
from gui.qt_theme import app_icon, apply_theme
from gui.service_manager import ServiceManager
from gui.service_queue import ServiceActionQueue
from server.core.file_latest import set_file_share_ttl

logger = logging.getLogger("gui")


def build_shutdown_handler(services, config, root):
    """退出时停止子进程、保留「启用意图」、最后请求 UI 退出。

    独立成函数是为了能脱离 QObject 单独测试：这里只有纯 Python 逻辑。
    """

    def shutdown():
        try:
            keep_server = bool(config.server_running)
            keep_client = bool(config.client_running)
            services.stop_server(preserve_intent=True)
            services.stop_client(preserve_intent=True)
            # stop_* 会把意图改成 False，这里恢复成用户原本的选择，
            # 下次启动才能按上次的开关状态恢复服务。
            config.server_running = keep_server
            config.client_running = keep_client
            config.save_state()
        except Exception:
            logger.exception("退出时停止服务失败")
        finally:
            request_ui_shutdown(root)

    return shutdown


class TrayManager(QObject):
    """协调托盘、主窗口与子服务。"""

    def __init__(self):
        super().__init__()
        self.config = ConfigManager()
        self.config.load_state()

        # QApplication 必须在任何 QWidget 之前创建，且全进程唯一
        self.root = QApplication.instance() or QApplication(sys.argv)
        self.root.setApplicationName("SyncClipboard")
        self.root.setApplicationDisplayName("SyncClipboard")
        self.root.setWindowIcon(app_icon("active"))
        # 关闭窗口只是收进托盘，不能退出进程
        self.root.setQuitOnLastWindowClosed(False)
        apply_theme(self.root, self.config.appearance_mode)

        ui_bridge.set_ui_root(self.root)
        qt_dialogs.register()

        # 定期抽干 UI 投递队列（替代 Tk 的 root.after 轮询）
        self._pump = QTimer(self)
        self._pump.setInterval(ui_bridge.DRAIN_INTERVAL_MS)
        self._pump.timeout.connect(ui_bridge.process_ui_queue)
        self._pump.start()

        self.services = ServiceManager(self.config)
        self.file_handler = FileHandler(self.config)
        self.clipboard_handler = ClipboardHandler(self.config, self)
        self.watcher = FileWatcherHandler(self.clipboard_handler, self.config)

        self.window = None
        self.tray = None
        self.menu = None
        self._actions = ServiceActionQueue(on_change=self._on_service_change)

    def _on_service_change(self):
        """队列状态变化时刷新界面；后台线程调用会被转发到主线程。"""
        self.refresh_ui()

    # ------------------------------------------------------------ 服务状态

    @staticmethod
    def _process_alive(process):
        if process is None:
            return False
        try:
            return process.is_alive()
        except (AssertionError, ValueError):
            return False

    def get_service_state(self):
        """持久化的「启用意图」与观测到的进程健康度分开返回。"""
        return {
            "server_enabled": bool(self.config.server_running),
            "client_enabled": bool(self.config.client_running),
            "server_alive": self._process_alive(self.services.server_process),
            "client_alive": self._process_alive(self.services.client_process),
            "busy": self._actions.busy,
        }

    def request_service_state(self, service, enabled):
        """在串行的后台线程上启动或停止某个服务。"""
        if service not in {"server", "client"}:
            raise ValueError(f"未知服务: {service}")

        def action():
            if service == "server":
                (self.services.start_server if enabled else self.services.stop_server)()
            else:
                (self.services.start_client if enabled else self.services.stop_client)()

        return self._actions.submit(action)

    def restart_services_async(self):
        return self._actions.submit(self.services.restart_services)

    def restart_client_async(self):
        def action():
            if not self.config.client_running:
                return
            if self.services.stop_client(preserve_intent=True):
                self.services.start_client()

        return self._actions.submit(action)

    # ------------------------------------------------------------ 刷新界面

    def refresh_ui(self):
        """服务状态变化后刷新所有表面。"""
        if threading.current_thread() is not threading.main_thread():
            post_to_main_thread_no_wait(self.refresh_ui)
            return
        if self.window is not None:
            try:
                self.window.refresh_status()
            except Exception:
                logger.exception("刷新主窗口失败")
        try:
            self.update_icon()
        except Exception:
            logger.exception("刷新托盘图标失败")
        try:
            self._refresh_menu()
        except Exception:
            logger.debug("刷新托盘菜单失败", exc_info=True)

    def _load_icon(self):
        state = is_active_state(self.get_service_state())
        path = icon_path(state)
        return QIcon(str(path)) if path is not None else app_icon("active")

    def update_icon(self):
        if self.tray is not None:
            self.tray.setIcon(self._load_icon())

    def _refresh_menu(self):
        if self.menu is None:
            return
        state = self.get_service_state()
        busy = state["busy"]
        self.action_client.setChecked(state["client_enabled"])
        self.action_server.setChecked(state["server_enabled"])
        for action in (self.action_client, self.action_server, self.action_restart):
            action.setEnabled(not busy)
        self.action_autostart.setChecked(self.config.is_autostart_enabled())

    # ---------------------------------------------------------------- 托盘

    def _build_menu(self):
        menu = QMenu()

        self.action_open = menu.addAction("打开 SyncClipboard")
        self.action_open.triggered.connect(self.show_main_window)
        self.action_fetch = menu.addAction("获取共享文件")
        self.action_fetch.triggered.connect(self.fetch_shared_file)
        menu.addSeparator()

        self.action_client = menu.addAction("同步客户端")
        self.action_client.setCheckable(True)
        self.action_client.triggered.connect(self._toggle_client)

        self.action_server = menu.addAction("本机服务器")
        self.action_server.setCheckable(True)
        self.action_server.triggered.connect(self._toggle_server)

        self.action_restart = menu.addAction("重启已启用服务")
        self.action_restart.triggered.connect(lambda: self.restart_services_async())
        menu.addSeparator()

        self.action_autostart = menu.addAction("开机启动")
        self.action_autostart.setCheckable(True)
        self.action_autostart.triggered.connect(self._toggle_autostart)

        self.action_client_config = menu.addAction("打开客户端配置")
        self.action_client_config.triggered.connect(
            lambda: self._open_config("client_config.json")
        )
        self.action_server_config = menu.addAction("打开服务器配置")
        self.action_server_config.triggered.connect(
            lambda: self._open_config("server_config.json")
        )

        self.action_logs = menu.addAction("查看运行日志")
        self.action_logs.triggered.connect(lambda: self.show_log_view("server"))
        menu.addSeparator()

        self.action_quit = menu.addAction("退出")
        self.action_quit.triggered.connect(self.quit_app)
        return menu

    def _create_tray(self):
        if not QSystemTrayIcon.isSystemTrayAvailable():
            logger.warning("系统托盘不可用，界面仍可通过主窗口操作")
            return
        self.menu = self._build_menu()
        self.tray = QSystemTrayIcon(self._load_icon(), self)
        self.tray.setToolTip("SyncClipboard")
        self.tray.setContextMenu(self.menu)
        self.tray.activated.connect(self._on_tray_activated)
        self.tray.show()

    def _on_tray_activated(self, reason):
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            self.show_main_window()

    def _toggle_server(self, _checked=False):
        state = self.get_service_state()
        self.request_service_state("server", not state["server_enabled"])

    def _toggle_client(self, _checked=False):
        state = self.get_service_state()
        self.request_service_state("client", not state["client_enabled"])

    def _toggle_autostart(self, _checked=False):
        current = self.config.is_autostart_enabled()
        if self.config.toggle_autostart(not current):
            self._refresh_menu()

    @staticmethod
    def _open_config(file_name):
        path = BASE_DIR / "config" / file_name
        if not path.exists():
            return
        try:
            os.startfile(path)
        except Exception:
            logger.exception("打开配置文件失败: %s", path)

    # ------------------------------------------------------------ 窗口控制

    def show_main_window(self, *_args):
        if self.window is None:
            return
        if threading.current_thread() is threading.main_thread():
            self.window.show_window()
        else:
            post_to_main_thread_no_wait(self.window.show_window)

    def hide_main_window(self, *_args):
        if self.window is None:
            return
        if threading.current_thread() is threading.main_thread():
            self.window.hide_window()
        else:
            post_to_main_thread_no_wait(self.window.hide_window)

    def show_log_view(self, source="server"):
        """从托盘直接打开某个日志：先把主窗口叫出来，再按要求读一次。"""
        if self.window is None:
            return

        def action():
            self.window.show_window()
            self.window.open_log_view(source)

        if threading.current_thread() is threading.main_thread():
            action()
        else:
            post_to_main_thread_no_wait(action)

    def fetch_shared_file(self, *_args):
        threading.Thread(
            target=self.file_handler.fetch_file_with_progress,
            name="fetch-shared-file",
            daemon=True,
        ).start()

    # ---------------------------------------------------------------- 退出

    def quit_app(self, *_args):
        """在后台线程停止子进程，保留启用意图。"""
        if not self._actions.shutdown(
            build_shutdown_handler(self.services, self.config, self.root)
        ):
            return
        self.refresh_ui()

    # ---------------------------------------------------------------- 启动

    def run(self):
        """启动服务、托盘与 Qt 事件循环。"""
        if not self.config.load_client_config():
            logger.error("客户端配置加载失败，界面未启动")
            ui_bridge.show_message(
                "配置加载失败", "无法读取客户端配置，请检查 config/client_config.json"
            )
            ui_bridge.clear_ui_root(self.root)
            return

        # GUI 也要用同一套共享存活时间判断「文件记录是否已过期」
        set_file_share_ttl(
            getattr(self.config, "file_share_ttl_minutes", 10) * 60
        )

        self.window = MainWindow(self)
        ui_bridge.set_main_window(self.window)

        restore_server = bool(self.config.server_running)
        restore_client = bool(self.config.client_running)
        logger.info("状态恢复: server=%s, client=%s", restore_server, restore_client)
        if restore_server:
            self.services.start_server()
        if restore_client:
            self.services.start_client()

        self.watcher.start()
        self._create_tray()

        self.refresh_ui()
        # 界面与服务就绪后开始响应重复启动的唤出请求
        start_show_watcher(self.show_main_window)
        if "--minimized" not in sys.argv:
            self.window.show_window()

        try:
            self.root.exec()
        finally:
            self.watcher.stop()
            self._pump.stop()
            if self.tray is not None:
                self.tray.hide()
            ui_bridge.clear_ui_root(self.root)
