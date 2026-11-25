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
import subprocess
import sys
import threading
import time

from PySide6.QtCore import QObject, QTimer
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from common import ui_bridge
from common.single_instance import start_show_watcher
from common.ui_bridge import post_to_main_thread_no_wait, request_ui_shutdown
from common.updater import PROJECT_PAGE
from common.utils import BASE_DIR, open_path, open_url
from gui import qt_dialogs
from gui.clipboard_handler import ClipboardHandler
from gui.config_manager import LOCAL_SERVER_HOST, ConfigManager
from gui.file_handler import FileHandler
from gui.file_watcher_handler import FileWatcherHandler
from gui.icon_theme import icon_path, is_active_state
from gui.qt_main_window import MainWindow
from gui.qt_theme import app_icon, apply_theme
from gui.service_manager import ServiceManager
from gui.service_queue import ServiceActionQueue
from server.core.file_latest import set_file_share_ttl

logger = logging.getLogger("gui")

# 点「退出」到「进程必须消失」的宽限时间，是看门狗的兜底期限
QUIT_GRACE_SECONDS = 15.0

# 重启时传给新实例的参数：让它等旧实例释放单实例互斥量后再接管
RESTART_TAKEOVER_FLAG = "--takeover"


def build_restart_command(minimized=False, frozen=None, executable=None):
    """构造「重启程序」要执行的命令行。

    新实例带 ``--takeover``，否则它启动时看到旧实例还占着单实例互斥量，会被
    当成「重复启动」直接退出——用户看到的就是托盘没了、程序也没回来。

    源码运行用 ``-m gui.run``（与开机启动项一致），不依赖相对路径或当前目录；
    打包版直接重启 exe。
    """
    frozen = getattr(sys, "frozen", False) if frozen is None else frozen
    executable = sys.executable if executable is None else executable
    args = ["--minimized"] if minimized else []
    args.append(RESTART_TAKEOVER_FLAG)
    if frozen:
        return [executable, *args]
    return [executable, "-m", "gui.run", *args]


def spawn_restarted_instance(command=None, cwd=None):
    """后台拉起新实例，返回 Popen；失败返回 None。

    不能用 DETACHED_PROCESS（见 common/update_apply 的教训：子进程会拿不到
    标准句柄而卡住）；``CREATE_NO_WINDOW`` + 标准流接 DEVNULL 就够了，父进程
    退出后子进程照旧存活。
    """
    command = build_restart_command() if command is None else list(command)
    cwd = BASE_DIR if cwd is None else cwd
    creationflags = 0
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        creationflags |= subprocess.CREATE_NO_WINDOW
    if hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
        creationflags |= subprocess.CREATE_NEW_PROCESS_GROUP
    try:
        process = subprocess.Popen(
            command,
            cwd=str(cwd),
            creationflags=creationflags,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
    except OSError:
        logger.exception("拉起重启实例失败")
        return None
    logger.info("已拉起重启实例 pid=%s：%s", process.pid, " ".join(command))
    return process


def build_shutdown_handler(services, config, root):
    """退出时停止子进程、保留「启用意图」、最后请求 UI 退出。

    独立成函数是为了能脱离 QObject 单独测试：这里只有纯 Python 逻辑。
    「请求 UI 退出」放在最后一步，保证界面消失时子进程已经停干净。
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
            if not request_ui_shutdown(root):
                logger.warning("界面已不在运行，退出请求无需投递")

    return shutdown


def stop_leftover_processes(services):
    """把仍存活的子进程立即结束，用于退出收尾。"""
    if services is None:
        return
    for name in ("server_process", "client_process"):
        process = getattr(services, name, None)
        if process is None:
            continue
        try:
            if process.is_alive():
                logger.warning("退出前仍有 %s 在运行，强制结束", name)
                process.kill()
                process.join(1)
        except (AssertionError, ValueError, OSError):
            logger.debug("强制结束 %s 失败", name, exc_info=True)


def force_exit(services=None, reason=""):
    """最后手段：收掉残留子进程后直接结束进程。

    正常路径不走这里——``run()`` 收好界面后交给解释器自然退出。只有真的卡住
    时才用到：退出动作排在服务队列后面被挡住、停子进程不返回、或者界面退了
    但进程留在任务管理器里。

    结束方式用 TerminateProcess 而不是 ``os._exit``：后者仍会跑一遍 DLL 卸载，
    PySide 的托盘/插件对象在那个阶段会 access violation（实测稳定复现），
    虽然进程确实死了，但会平白留下一条崩溃记录。
    """
    if reason:
        logger.warning("强制结束进程：%s", reason)
    stop_leftover_processes(services)
    try:
        logging.shutdown()
    except Exception:  # noqa: BLE001 - 关日志失败也要保证进程退出
        pass
    _terminate_current_process()


def _terminate_current_process(code=0):
    """内核级结束当前进程；拿不到 Win32 入口时退回 os._exit。"""
    if _terminate_via_win32(code):
        return
    logger.error("TerminateProcess 未生效，退回 os._exit")
    os._exit(code)


def _terminate_via_win32(code=0):
    """TerminateProcess(GetCurrentProcess(), code)：不跑 DLL 卸载，直接消失。"""
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.TerminateProcess.restype = wintypes.BOOL
        kernel32.TerminateProcess.argtypes = (wintypes.HANDLE, wintypes.UINT)
        return bool(kernel32.TerminateProcess(kernel32.GetCurrentProcess(), code))
    except Exception:  # noqa: BLE001 - 非 Windows / 调用受限都退回 os._exit
        logger.debug("TerminateProcess 调用失败", exc_info=True)
        return False


def force_exit_after(delay, services=None, reason=""):
    """延时后强制结束进程，作为退出流程的看门狗。

    进程正常退出时这个守护线程会随进程一起消失，不会真的动手；只有到了期限
    还活着，才说明某一段卡住了。
    """
    time.sleep(delay)
    force_exit(services, reason or f"{delay:.0f} 秒内没有退出")


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

        # 退出相关状态：只发起一次，随后由看门狗保证进程不会残留
        self._quit_requested = False

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

        # 开启「本机服务器」时，客户端就该连回本机，而不是继续连着旧的中心服务器
        switch_to_local = service == "server" and enabled

        def action():
            # 配置必须先落盘再启停：客户端进程只在启动时读一次配置。
            host_changed = switch_to_local and self.config.use_local_server()
            if service == "server":
                success = (
                    self.services.start_server() if enabled
                    else self.services.stop_server()
                )
            else:
                success = (
                    self.services.start_client() if enabled
                    else self.services.stop_client()
                )
            if host_changed and success:
                self._restart_running_client()
                self._notify_local_server_host()

        return self._actions.submit(action)

    def _restart_running_client(self):
        """客户端只在启动时读配置，连接地址改了必须重启进程。"""
        if not self.config.client_running:
            return
        if self.services.stop_client(preserve_intent=True):
            self.services.start_client()

    def _notify_local_server_host(self):
        """把自动改写的连接地址同步回面板（Qt 控件只能在主线程碰）。"""
        window = self.window
        if window is not None:
            post_to_main_thread_no_wait(
                window.on_local_server_host_applied, LOCAL_SERVER_HOST
            )

    def restart_services_async(self):
        return self._actions.submit(self.services.restart_services)

    def restart_client_async(self):
        return self._actions.submit(self._restart_running_client)

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
        # 「重启程序」不受服务队列忙闲影响：队列卡住时正是需要重开的时候
        for action in (self.action_client, self.action_server):
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

        self.action_restart = menu.addAction("重启程序")
        self.action_restart.triggered.connect(self.restart_app)
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

        self.action_repo = menu.addAction("访问项目仓库")
        # QAction.triggered 会带一个 checked 参数，这里包一层省得签名对不上
        self.action_repo.triggered.connect(lambda: self.open_repository())
        menu.addSeparator()

        self.action_quit = menu.addAction("退出")
        self.action_quit.triggered.connect(self.quit_app)
        # 诊断用：Windows 11 的托盘溢出区偶尔会把点击吞掉，菜单展开了却没触发
        # 动作。记一笔「展开」与「点了退出」，事后能分清是哪一段没走通。
        menu.aboutToShow.connect(lambda: logger.info("托盘菜单已展开"))
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
        """打开某个配置文件。

        原来只有 ``os.startfile``：Windows 上 ``.json`` 没有文件关联时会抛
        ``WinError -2147221003``，异常被吞进日志，点菜单就是「毫无反应」。
        这里改成 open_path 兜底（退到记事本），文件还没生成也明确说一声。
        """
        path = BASE_DIR / "config" / file_name
        if not path.exists():
            qt_dialogs.info_message(
                "配置文件尚未生成",
                f"{path} 还不存在。\n"
                "启动对应的服务（客户端 / 本机服务器）后会自动创建默认配置，"
                "也可以先点一次「保存并应用」。",
            )
            return
        ok, error = open_path(path)
        if not ok:
            qt_dialogs.info_message("无法打开配置文件", error)

    @staticmethod
    def open_repository():
        """用系统默认浏览器打开代码仓库（与面板底部「查看仓库」同一个入口）。"""
        ok, error = open_url(PROJECT_PAGE)
        if not ok:
            qt_dialogs.info_message("无法打开浏览器", f"{PROJECT_PAGE}\n\n{error}")

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

    # ---------------------------------------------------------------- 重启

    def restart_app(self, *_args):
        """重启程序：先拉起新实例，再走与「退出」完全相同的收尾流程。

        顺序不能反：先退出的话进程一没就没人再拉起了。新实例带着 ``--takeover``
        等本进程释放单实例互斥量，接管之后按上次的开关状态重新拉起服务。

        窗口状态跟着走：现在是收在托盘里的，重启后也留在托盘，不会突然弹出来。
        """
        if self._quit_requested:
            logger.info("退出/重启流程已在执行，忽略重复的重启请求")
            return

        minimized = self.window is None or self.window.isHidden()
        process = spawn_restarted_instance(
            build_restart_command(minimized=minimized)
        )
        if process is None:
            # 新实例没起来就退出，等于「点了重启、程序却彻底没了」，宁可不动
            ui_bridge.show_message(
                "重启失败",
                "无法启动新的程序实例，本次保持运行。\n"
                "请从开始菜单或快捷方式重新打开 SyncClipboard。",
            )
            return

        logger.info("收到重启请求，先拉起新实例再退出")
        self.quit_app()

    # ---------------------------------------------------------------- 退出

    def quit_app(self, *_args):
        """发起退出：停子进程 → 请求界面退出 → 看门狗兜底强制结束。

        这里只「发起」，真正停服务仍排在服务队列后面执行，避免和正在跑的
        启停操作抢同一批子进程；但因为退出是唯一必须成功的操作，另外挂一个
        看门狗：无论队列、子进程回收还是事件循环哪一环卡住，进程最后都会
        被强制结束——不允许出现「托盘没了、任务管理器里还有进程」。
        """
        if self._quit_requested:
            logger.info("退出流程已在执行，忽略重复的退出请求")
            return

        self._quit_requested = True
        logger.info("收到退出请求，开始停止服务")
        if not self._actions.shutdown(
            build_shutdown_handler(self.services, self.config, self.root)
        ):
            # 队列已进入退出状态却仍有界面在跑：至少把界面退出推进下去
            logger.warning("退出动作未能进入服务队列，直接请求界面退出")
            request_ui_shutdown(self.root)

        threading.Thread(
            target=self._quit_watchdog, name="quit-watchdog", daemon=True
        ).start()
        self.refresh_ui()

    def _quit_watchdog(self):
        """退出宽限期到了还活着，就强制结束进程。

        看门狗不看界面是否已经退出：界面退了但解释器卡在收尾阶段（多进程
        子进程回收、非守护线程）同样是「任务管理器里还有进程」，一样要收掉。
        """
        force_exit_after(QUIT_GRACE_SECONDS, self.services)

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
            getattr(self.config, "file_share_ttl_minutes", 30) * 60
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
            # 走到这里说明退出请求已经发过（它排在停服务之后）。收好界面后返回，
            # 让解释器正常退出；万一收尾阶段卡住，quit-watchdog 会兜底结束进程。
            self.watcher.stop()
            self._pump.stop()
            if self.tray is not None:
                self.tray.hide()
            ui_bridge.clear_ui_root(self.root)
            if self._quit_requested:
                logger.info("界面主循环已退出")
