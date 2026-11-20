"""PySide6 主控制台窗口。

职责与原 CustomTkinter 版一致：单页面板（状态 / 服务开关 / 快捷操作 /
连接设置 / 更新），业务逻辑（config、service、file、clipboard）完全不碰。

关键差别在于渲染：Qt 有窗口级 backing store，拖动缩放时不会像 Tk 那样
先暴露未绘制区域（也就是原来那 0.5 秒花屏），重绘耗时也从 ~730ms 降到
~60ms。QSS 让圆角卡片、焦点态、悬停态都能一次统一。
"""

import logging
import os
import shutil
import tempfile
import threading
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from common.log_files import DEFAULT_SOURCE, LOG_SOURCES
from common.update_apply import (
    UpdateError,
    ensure_writable,
    extract_package,
    is_self_update_available,
    launch_updater,
    resolve_app_dir,
)
from common.updater import APP_VERSION, check_for_update, download_file
from common.utils import BASE_DIR, post_to_main_thread_no_wait
from gui import qt_dialogs
from gui.main_window import validate_client_settings
from gui.qt_log_view import LogViewer
from gui.qt_theme import (
    APPEARANCE_LABELS,
    APPEARANCE_MODES,
    ToggleSwitch,
    app_icon,
    apply_theme,
    set_badge,
)

logger = logging.getLogger("gui")


def _section_title(text):
    label = QLabel(text)
    label.setObjectName("SectionTitle")
    return label


def _set_color(widget, color, extra=""):
    """只改颜色，保留全局 QSS 里的字号等属性。"""
    widget.setStyleSheet(f"color: {color}; {extra}".strip())


class ServiceCard(QFrame):
    """一张服务卡片：标记、标题、开关、描述、状态徽章。"""

    def __init__(self, title, description, mark, palette, parent=None):
        super().__init__(parent)
        self.setObjectName("Card")
        self.setMinimumHeight(148)
        self._palette = palette

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(9)

        top = QHBoxLayout()
        top.setSpacing(11)
        self.mark = QLabel(mark)
        self.mark.setObjectName("CardMark")
        self.mark.setFixedSize(38, 38)
        self.mark.setAlignment(Qt.AlignCenter)

        self.title = QLabel(title)
        self.title.setStyleSheet("font-size: 15px; font-weight: 600;")

        self.switch = ToggleSwitch(palette=palette)

        top.addWidget(self.mark)
        top.addWidget(self.title)
        top.addStretch(1)
        top.addWidget(self.switch, 0, Qt.AlignVCenter)
        root.addLayout(top)

        self.description = QLabel(description)
        self.description.setObjectName("Hint")
        root.addWidget(self.description)

        self.status = QLabel()
        self.status.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)
        set_badge(self.status, "已停止", palette["border"], palette["muted"])
        row = QHBoxLayout()
        row.addWidget(self.status)
        row.addStretch(1)
        root.addLayout(row)
        root.addStretch(1)

    def set_state(self, enabled, alive, busy, palette):
        self._palette = palette
        self.switch.setPaletteColors(palette)
        self.switch.setChecked(enabled, animate=False)
        self.switch.setEnabled(not busy)

        if busy:
            set_badge(self.status, "正在更新…", palette["warning"])
        elif enabled and alive:
            set_badge(self.status, "运行正常", palette["success"])
        elif enabled:
            set_badge(self.status, "已启用 · 进程未运行", palette["danger"])
        elif alive:
            set_badge(self.status, "已关闭 · 进程未能停止", palette["danger"])
        else:
            set_badge(self.status, "已停止", palette["border"], palette["muted"])


class MainWindow(QMainWindow):
    """单页控制台。"""

    def __init__(self, manager, parent=None):
        super().__init__(parent)
        self.manager = manager
        self.config = manager.config
        self._palette = apply_theme(QApplication.instance(), self.config.appearance_mode)
        self._centered = False
        self._secret_visible = False
        self._allow_close = False

        self._update_state = "idle"
        self._update_result = None
        self._pending_payload = None
        self._pending_work_dir = None
        self._last_download_percent = -1
        self._log_viewer = None

        self.setWindowTitle("SyncClipboard")
        self.setWindowIcon(app_icon("active"))
        self.setMinimumSize(780, 560)

        self._build()
        self.load_settings()
        self.refresh_status()

        self._status_timer = QTimer(self)
        self._status_timer.setInterval(1000)
        self._status_timer.timeout.connect(self.refresh_status)
        self._status_timer.start()

    # ------------------------------------------------------------------ 构建

    def _build(self):
        page = QWidget()
        page.setObjectName("Page")
        self.setCentralWidget(page)

        outer = QVBoxLayout(page)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self._build_header())

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        content = QWidget()
        content.setObjectName("Page")
        self.body = QVBoxLayout(content)
        self.body.setContentsMargins(22, 20, 22, 22)
        self.body.setSpacing(14)
        scroll.setWidget(content)
        outer.addWidget(scroll, 1)

        self._build_hero()
        self._build_service_cards()
        self._build_quick_actions()
        self._build_logs()
        self._build_settings()
        self._build_footer()

    def _build_header(self):
        header = QFrame()
        header.setObjectName("Header")
        header.setFixedHeight(84)

        lay = QHBoxLayout(header)
        lay.setContentsMargins(22, 0, 22, 0)
        lay.setSpacing(13)

        mark = QLabel("SC")
        mark.setObjectName("AppMark")
        mark.setFixedSize(44, 44)
        mark.setAlignment(Qt.AlignCenter)

        titles = QVBoxLayout()
        titles.setSpacing(1)
        name = QLabel("SyncClipboard")
        name.setObjectName("Title")
        subtitle = QLabel("跨设备剪贴板同步中心")
        subtitle.setObjectName("Subtitle")
        titles.addWidget(name)
        titles.addWidget(subtitle)

        lay.addWidget(mark)
        lay.addLayout(titles)
        lay.addStretch(1)
        lay.addWidget(self._build_appearance_selector(), 0, Qt.AlignVCenter)
        return header

    def _build_appearance_selector(self):
        bar = QFrame()
        bar.setObjectName("SegmentBar")
        # 放进 QHBoxLayout 时默认会被纵向撑满 header，这里限死高度
        bar.setFixedHeight(36)
        bl = QHBoxLayout(bar)
        bl.setContentsMargins(3, 3, 3, 3)
        bl.setSpacing(2)

        self.appearance_group = QButtonGroup(self)
        self.appearance_group.setExclusive(True)
        self.appearance_buttons = {}
        for label in APPEARANCE_LABELS.values():
            button = QPushButton(label)
            button.setObjectName("Segment")
            button.setCheckable(True)
            button.setCursor(Qt.PointingHandCursor)
            button.setMinimumWidth(66)
            button.setFixedHeight(28)
            button.clicked.connect(lambda _checked=False, text=label: self._change_appearance(text))
            self.appearance_group.addButton(button)
            bl.addWidget(button)
            self.appearance_buttons[label] = button
        return bar

    def _build_hero(self):
        hero = QFrame()
        hero.setObjectName("Hero")
        lay = QHBoxLayout(hero)
        lay.setContentsMargins(22, 17, 22, 17)
        lay.setSpacing(16)

        box = QVBoxLayout()
        box.setSpacing(4)
        self.welcome_title = QLabel("设备同步已准备就绪")
        self.welcome_title.setObjectName("HeroTitle")
        self.welcome_detail = QLabel("正在读取服务状态…")
        self.welcome_detail.setObjectName("Subtitle")
        box.addWidget(self.welcome_title)
        box.addWidget(self.welcome_detail)

        self.overall_status = QLabel()
        self.overall_status.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)
        set_badge(self.overall_status, "检查中", self._palette["warning"])

        lay.addLayout(box, 1)
        lay.addWidget(self.overall_status, 0, Qt.AlignVCenter)
        self.body.addWidget(hero)

    def _build_service_cards(self):
        row = QHBoxLayout()
        row.setSpacing(14)

        self.server_card = ServiceCard(
            "本机服务器", "为局域网设备中转剪贴板内容", "S", self._palette
        )
        self.client_card = ServiceCard(
            "同步客户端", "监听并同步本机剪贴板变化", "C", self._palette
        )
        self.server_card.switch.toggled.connect(self._on_server_toggled)
        self.client_card.switch.toggled.connect(self._on_client_toggled)

        row.addWidget(self.server_card, 1)
        row.addWidget(self.client_card, 1)
        self.body.addLayout(row)

    def _build_quick_actions(self):
        card = QFrame()
        card.setObjectName("Card")
        lay = QVBoxLayout(card)
        lay.setContentsMargins(18, 16, 18, 16)
        lay.setSpacing(12)
        lay.addWidget(_section_title("快速操作"))

        row = QHBoxLayout()
        row.setSpacing(10)

        self.fetch_button = QPushButton("获取共享文件")
        self.fetch_button.setObjectName("Primary")
        self.fetch_button.clicked.connect(self._fetch_file)

        self.restart_button = QPushButton("重启已启用服务")
        self.restart_button.clicked.connect(self._restart_services)

        self.folder_button = QPushButton("打开下载目录")
        self.folder_button.clicked.connect(self._open_download_folder)

        for button in (self.fetch_button, self.restart_button, self.folder_button):
            button.setMinimumHeight(38)
            button.setCursor(Qt.PointingHandCursor)
            row.addWidget(button, 1)

        lay.addLayout(row)
        self.body.addWidget(card)

    def _build_logs(self):
        """日志入口卡片：三个来源各一个按钮，点哪个读哪个。"""
        card = QFrame()
        card.setObjectName("Card")
        lay = QVBoxLayout(card)
        lay.setContentsMargins(18, 16, 18, 16)
        lay.setSpacing(12)

        head = QHBoxLayout()
        head.addWidget(_section_title("运行日志"))
        head.addStretch(1)
        folder = QPushButton("打开日志目录")
        folder.setCursor(Qt.PointingHandCursor)
        folder.setFixedWidth(120)
        folder.setMinimumHeight(32)
        folder.clicked.connect(self._open_log_folder)
        head.addWidget(folder)
        lay.addLayout(head)

        hint = QLabel(
            "服务端、客户端与界面各写一份日志，点哪个读哪个；窗口关闭后不再读取文件"
        )
        hint.setObjectName("Hint")
        hint.setWordWrap(True)
        lay.addWidget(hint)

        row = QHBoxLayout()
        row.setSpacing(10)
        self.log_buttons = {}
        for source, (title, file_name, description) in LOG_SOURCES.items():
            button = QPushButton(f"查看{title}")
            button.setCursor(Qt.PointingHandCursor)
            button.setMinimumHeight(38)
            button.setToolTip(f"{description} · log/{file_name}")
            button.clicked.connect(
                lambda _checked=False, value=source: self.open_log_view(value)
            )
            row.addWidget(button, 1)
            self.log_buttons[source] = button
        lay.addLayout(row)

        self.body.addWidget(card)

    def _build_settings(self):
        card = QFrame()
        card.setObjectName("Card")
        lay = QVBoxLayout(card)
        lay.setContentsMargins(20, 18, 20, 18)
        lay.setSpacing(14)

        head = QHBoxLayout()
        head.addWidget(_section_title("连接设置"))
        head.addStretch(1)
        hint = QLabel("保存后，正在运行的客户端会自动重启并应用配置")
        hint.setObjectName("Hint")
        head.addWidget(hint)
        lay.addLayout(head)

        grid = QGridLayout()
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(12)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)

        self.server_host_edit = self._field(grid, 0, 0, "服务器地址", "例如 192.168.1.10")
        self.server_port_edit = self._field(grid, 0, 1, "服务器端口", "8000")
        self.local_name_edit = self._field(grid, 1, 0, "本机名称", "用于识别此设备")
        self.file_port_edit = self._field(grid, 1, 1, "文件服务端口", "8899")
        self.key_edit = self._field(grid, 2, 0, "共享密钥", "访问服务器所需密钥", password=True)
        self.save_path_edit = self._directory_field(grid, 2, 1)

        self.secret_toggle = QPushButton("显示密钥")
        self.secret_toggle.setCursor(Qt.PointingHandCursor)
        self.secret_toggle.setFixedWidth(96)
        self.secret_toggle.setMinimumHeight(32)
        self.secret_toggle.clicked.connect(self._toggle_secret)
        grid.addWidget(self.secret_toggle, 3, 0, Qt.AlignLeft)

        lay.addLayout(grid)

        preference = QFrame()
        preference.setObjectName("Preference")
        pref_lay = QHBoxLayout(preference)
        pref_lay.setContentsMargins(16, 9, 16, 9)
        pref_label = QLabel("开机后自动在后台启动")
        pref_label.setStyleSheet("font-weight: 600;")
        self.autostart_switch = ToggleSwitch(palette=self._palette)
        self.autostart_switch.toggled.connect(self._toggle_autostart)
        pref_lay.addWidget(pref_label)
        pref_lay.addStretch(1)
        pref_lay.addWidget(self.autostart_switch)
        lay.addWidget(preference)

        actions = QHBoxLayout()
        actions.setSpacing(10)
        self.settings_message = QLabel("")
        self.settings_message.setObjectName("Hint")
        self.settings_message.setWordWrap(True)

        advanced = QPushButton("打开高级配置")
        advanced.setCursor(Qt.PointingHandCursor)
        advanced.setFixedWidth(120)
        advanced.setMinimumHeight(38)
        advanced.clicked.connect(self._open_config_folder)

        self.save_button = QPushButton("保存并应用")
        self.save_button.setObjectName("Primary")
        self.save_button.setCursor(Qt.PointingHandCursor)
        self.save_button.setFixedWidth(126)
        self.save_button.setMinimumHeight(38)
        self.save_button.clicked.connect(self.save_settings)

        actions.addWidget(self.settings_message, 1)
        actions.addWidget(advanced)
        actions.addWidget(self.save_button)
        lay.addLayout(actions)

        self.body.addWidget(card)

    def _field(self, grid, row, column, label, placeholder, password=False):
        box = QWidget()
        lay = QVBoxLayout(box)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)

        caption = QLabel(label)
        caption.setObjectName("FieldLabel")
        edit = QLineEdit()
        edit.setPlaceholderText(placeholder)
        edit.setMinimumHeight(38)
        if password:
            edit.setEchoMode(QLineEdit.Password)

        lay.addWidget(caption)
        lay.addWidget(edit)
        grid.addWidget(box, row, column)
        return edit

    def _directory_field(self, grid, row, column):
        box = QWidget()
        lay = QVBoxLayout(box)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)

        caption = QLabel("文件保存目录")
        caption.setObjectName("FieldLabel")

        line = QHBoxLayout()
        line.setSpacing(8)
        edit = QLineEdit()
        edit.setMinimumHeight(38)
        choose = QPushButton("选择")
        choose.setCursor(Qt.PointingHandCursor)
        choose.setFixedWidth(72)
        choose.setMinimumHeight(38)
        choose.clicked.connect(self._choose_directory)
        line.addWidget(edit, 1)
        line.addWidget(choose)

        lay.addWidget(caption)
        lay.addLayout(line)
        grid.addWidget(box, row, column)
        return edit

    def _build_footer(self):
        footer = QWidget()
        lay = QVBoxLayout(footer)
        lay.setContentsMargins(0, 2, 0, 0)
        lay.setSpacing(6)

        row = QHBoxLayout()
        row.setSpacing(12)
        hint = QLabel("关闭窗口后应用仍会在系统托盘中运行")
        hint.setObjectName("Hint")

        self.update_button = QPushButton("检查更新")
        self.update_button.setCursor(Qt.PointingHandCursor)
        self.update_button.setFixedWidth(120)
        self.update_button.setMinimumHeight(32)
        self.update_button.clicked.connect(self.check_for_updates)

        self.version_label = QLabel(f"当前版本 v{APP_VERSION}")
        self.version_label.setObjectName("Hint")

        row.addWidget(hint)
        row.addStretch(1)
        row.addWidget(self.update_button)
        row.addWidget(self.version_label)
        lay.addLayout(row)

        self.update_message = QLabel("")
        self.update_message.setObjectName("Hint")
        self.update_message.setAlignment(Qt.AlignRight)
        self.update_message.setWordWrap(True)
        lay.addWidget(self.update_message)

        self.body.addWidget(footer)

    # ------------------------------------------------------- 设置读写与状态

    def load_settings(self):
        # setText 后光标停在末尾，长路径会从中间截断显示，这里统一回到开头
        for edit, value in (
            (self.server_host_edit, self.config.server_host),
            (self.server_port_edit, self.config.server_port),
            (self.local_name_edit, self.config.local_name),
            (self.file_port_edit, self.config.file_server_port),
            (self.key_edit, self.config.key),
            (self.save_path_edit, self.config.save_path),
        ):
            edit.setText("" if value is None else str(value))
            edit.setCursorPosition(0)

        mode = getattr(self.config, "appearance_mode", "System")
        label = APPEARANCE_LABELS.get(mode, "跟随系统")
        button = self.appearance_buttons.get(label)
        if button is not None:
            button.setChecked(True)

        self.autostart_switch.setChecked(self.config.is_autostart_enabled(), animate=False)

    def save_settings(self):
        try:
            values = validate_client_settings(
                self.server_host_edit.text(),
                self.server_port_edit.text(),
                self.local_name_edit.text(),
                self.file_port_edit.text(),
                self.key_edit.text(),
                self.save_path_edit.text(),
            )
        except ValueError as exc:
            self._set_settings_message(str(exc), self._palette["danger"])
            return

        if not self.config.update_client_config(values):
            self._set_settings_message("配置保存失败，请查看日志", self._palette["danger"])
            return

        self._set_settings_message("配置已保存", self._palette["success"])
        self.welcome_detail.setText(
            f"连接到 {values['server_host']}:{values['server_port']} · 设备 {values['local_name']}"
        )
        if self.manager.get_service_state()["client_enabled"]:
            if self.manager.restart_client_async():
                self._set_settings_message("配置已保存，正在重启客户端…", self._palette["warning"])
            else:
                self._set_settings_message(
                    "配置已保存；服务正忙，请稍后手动重启客户端", self._palette["warning"]
                )

    def _set_settings_message(self, text, color):
        self.settings_message.setText(text)
        _set_color(self.settings_message, color)

    def _set_update_message(self, text, color):
        self.update_message.setText(text)
        _set_color(self.update_message, color)

    def refresh_status(self):
        state = self.manager.get_service_state()
        palette = self._palette

        self.server_card.set_state(
            state["server_enabled"], state["server_alive"], state["busy"], palette
        )
        self.client_card.set_state(
            state["client_enabled"], state["client_alive"], state["busy"], palette
        )

        busy = state["busy"]
        self.restart_button.setEnabled(not busy)
        self.save_button.setEnabled(not busy)

        unhealthy = []
        if state["server_enabled"] != state["server_alive"]:
            unhealthy.append(
                "服务器进程未运行" if state["server_enabled"] else "服务器进程未能停止"
            )
        if state["client_enabled"] != state["client_alive"]:
            unhealthy.append(
                "客户端进程未运行" if state["client_enabled"] else "客户端进程未能停止"
            )
        healthy = state["client_alive"] or state["server_alive"]

        if busy:
            set_badge(self.overall_status, "处理中", palette["warning"])
            self.welcome_title.setText("正在更新服务状态")
        elif unhealthy:
            set_badge(self.overall_status, "需检查", palette["danger"])
            self.welcome_title.setText("、".join(unhealthy))
        elif healthy:
            set_badge(self.overall_status, "运行中", palette["success"])
            self.welcome_title.setText("设备同步已准备就绪")
        else:
            set_badge(self.overall_status, "已暂停", palette["border"], palette["muted"])
            self.welcome_title.setText("同步服务当前已暂停")

        self.welcome_detail.setText(self._connection_summary())

        if not busy and self.settings_message.text() == "配置已保存，正在重启客户端…":
            if state["client_alive"]:
                self._set_settings_message("配置已应用", palette["success"])
            else:
                self._set_settings_message("配置已保存，但客户端启动失败", palette["danger"])

    def _connection_summary(self):
        """未配置时不要显示 "None:None"。"""
        host = self.config.server_host or "未配置服务器"
        port = self.config.server_port or "-"
        name = self.config.local_name or "未命名设备"
        return f"连接到 {host}:{port} · 设备 {name}"

    # ---------------------------------------------------------------- 交互

    def _on_server_toggled(self, checked):
        if not self.manager.request_service_state("server", checked):
            self.refresh_status()

    def _on_client_toggled(self, checked):
        if not self.manager.request_service_state("client", checked):
            self.refresh_status()

    def _restart_services(self):
        if not self.manager.restart_services_async():
            self._set_settings_message("服务正忙，请稍后再试", self._palette["warning"])

    def _fetch_file(self):
        threading.Thread(
            target=self.manager.file_handler.fetch_file_with_progress,
            name="fetch-shared-file",
            daemon=True,
        ).start()

    def _open_download_folder(self):
        path = Path(self.save_path_edit.text() or self.config.save_path)
        try:
            path.mkdir(parents=True, exist_ok=True)
            os.startfile(path)
        except Exception as exc:  # noqa: BLE001 - 需要把失败告知用户
            qt_dialogs.info_message("无法打开目录", str(exc))

    def _open_config_folder(self):
        config_dir = BASE_DIR / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.startfile(config_dir)
        except Exception as exc:  # noqa: BLE001
            qt_dialogs.info_message("无法打开目录", str(exc))

    # -------------------------------------------------------------- 运行日志

    def open_log_view(self, source=DEFAULT_SOURCE):
        """日志查看的唯一入口：点开时才创建窗口并读一次文件。

        查看器本身不带定时器，所以关掉窗口之后不会再有磁盘读取；
        再次点击会复用同一个窗口并重新读一次。
        """
        if self._log_viewer is None:
            self._log_viewer = LogViewer(self, self._palette)
        self._log_viewer.set_palette(self._palette)
        self._log_viewer.open_source(source)

    def _open_log_folder(self):
        log_dir = BASE_DIR / "log"
        log_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.startfile(log_dir)
        except Exception as exc:  # noqa: BLE001
            qt_dialogs.info_message("无法打开日志目录", str(exc))

    def _choose_directory(self):
        selected = qt_dialogs.choose_directory(
            self.save_path_edit.text() or self.config.save_path,
            "选择文件保存目录",
        )
        if selected:
            self.save_path_edit.setText(selected)

    def _toggle_secret(self):
        self._secret_visible = not self._secret_visible
        self.key_edit.setEchoMode(
            QLineEdit.Normal if self._secret_visible else QLineEdit.Password
        )
        self.secret_toggle.setText("隐藏密钥" if self._secret_visible else "显示密钥")

    def _toggle_autostart(self, checked):
        if not self.config.toggle_autostart(checked):
            self.autostart_switch.setChecked(not checked, animate=False)
            self._set_settings_message("开机启动设置失败", self._palette["danger"])
        else:
            self._set_settings_message(
                "已开启开机后台启动" if checked else "已关闭开机启动",
                self._palette["success"],
            )

    def _change_appearance(self, label):
        mode = APPEARANCE_MODES.get(label, "System")
        self._palette = apply_theme(QApplication.instance(), mode)
        for switch in (
            self.server_card.switch,
            self.client_card.switch,
            self.autostart_switch,
        ):
            switch.setPaletteColors(self._palette)
        if self._log_viewer is not None:
            self._log_viewer.set_palette(self._palette)
        self.config.appearance_mode = mode
        if not self.config.save_state():
            self._set_settings_message("外观偏好保存失败", self._palette["danger"])
        self.refresh_status()

    # ------------------------------------------------------- 更新流程

    def check_for_updates(self):
        """按钮唯一入口：按当前状态决定检查、下载还是重启更新。"""
        if self._update_state in {"checking", "downloading"}:
            return
        if self._update_state == "available":
            self._start_update_download()
            return
        if self._update_state == "ready":
            self._apply_update()
            return
        self._set_update_state(
            "checking", "正在检查更新…", self._palette["muted"], enabled=False
        )
        threading.Thread(target=self._run_update_check, name="update-check", daemon=True).start()

    def _set_update_state(self, state, text, color, button_text=None, enabled=True):
        self._update_state = state
        self._set_update_message(text, color)
        self.update_button.setText(button_text or "检查更新")
        self.update_button.setEnabled(enabled)

    def _run_update_check(self):
        result = check_for_update()
        post_to_main_thread_no_wait(self._render_update_result, result)

    def _render_update_result(self, result):
        self._update_result = result
        if result.error:
            self._set_update_state("idle", result.error, self._palette["danger"])
        elif result.has_update and result.download_url and is_self_update_available():
            self._set_update_state(
                "available",
                f"发现新版本 v{result.latest_version}（当前 v{result.current_version}）",
                self._palette["success"],
                button_text=f"更新到 v{result.latest_version}",
            )
        elif result.has_update and not is_self_update_available():
            self._set_update_state(
                "idle",
                f"发现新版本 v{result.latest_version}，当前为源码运行，请前往 Releases 下载",
                self._palette["warning"],
            )
        elif result.has_update:
            self._set_update_state(
                "idle",
                f"发现新版本 v{result.latest_version}，但未找到 Windows 安装包",
                self._palette["warning"],
            )
        else:
            self._set_update_state(
                "idle", f"已是最新版本 v{result.current_version}", self._palette["muted"]
            )

    def _start_update_download(self):
        self._last_download_percent = -1
        self._set_update_state(
            "downloading",
            "正在下载更新…",
            self._palette["warning"],
            button_text="下载中…",
            enabled=False,
        )
        threading.Thread(
            target=self._download_update, name="update-download", daemon=True
        ).start()

    def _download_update(self):
        work_dir = Path(tempfile.mkdtemp(prefix="syncclipboard_update_"))
        try:
            zip_path = work_dir / "update.zip"
            download_file(
                self._update_result.download_url,
                zip_path,
                progress=self._on_download_progress,
            )
            payload = extract_package(zip_path, work_dir / "payload")
        except Exception as exc:  # noqa: BLE001
            logger.warning("下载更新失败: %s", exc)
            shutil.rmtree(work_dir, ignore_errors=True)
            post_to_main_thread_no_wait(
                self._set_update_state, "idle", f"更新失败：{exc}", self._palette["danger"]
            )
            return
        post_to_main_thread_no_wait(self._finish_download, payload, work_dir)

    def _on_download_progress(self, downloaded, total):
        if not total:
            return
        percent = int(downloaded * 100 / total)
        if percent == self._last_download_percent:
            return
        self._last_download_percent = percent
        post_to_main_thread_no_wait(
            self._set_update_message, f"正在下载更新… {percent}%", self._palette["warning"]
        )

    def _finish_download(self, payload, work_dir):
        self._pending_payload = payload
        self._pending_work_dir = work_dir
        self._set_update_state(
            "ready",
            f"更新 v{self._update_result.latest_version} 已就绪，重启后生效",
            self._palette["success"],
            button_text="重启并更新",
        )

    def _apply_update(self):
        if not qt_dialogs.ask_yes_no(
            "更新 SyncClipboard", "将关闭程序、替换文件并重新启动，是否继续？"
        ):
            return
        try:
            app_dir = resolve_app_dir()
            ensure_writable(app_dir)
            launch_updater(app_dir, self._pending_payload, self._pending_work_dir)
        except UpdateError as exc:
            self._set_update_state("idle", f"更新失败：{exc}", self._palette["danger"])
            return
        self._set_update_state(
            "ready", "正在重启以完成更新…", self._palette["warning"], enabled=False
        )
        self.manager.quit_app()

    # -------------------------------------------------------- 显示与关闭

    def show_window(self):
        if self.isMinimized():
            self.showNormal()
        first_show = not self._centered
        if first_show:
            # 必须在布局建立后再定尺寸：构造期 resize 会被 sizeHint 盖掉，
            # 结果窗口停在 minimumSize（打包后实际只显示 780x560）。
            width, height = self._initial_size()
            self.resize(width, height)
        self.show()
        if first_show:
            self._center_once()
            self._centered = True
            self._log_environment()
        self.raise_()
        self.activateWindow()

    def _log_environment(self):
        """显示后再记录缩放信息——窗口未关联屏幕时 devicePixelRatio 不准。"""
        screen = self.screen()
        geometry = screen.availableGeometry() if screen is not None else None
        logger.info(
            "界面环境: 可用区 %sx%s, 设备像素比 %.2f, 显示器 %s",
            geometry.width() if geometry else 0,
            geometry.height() if geometry else 0,
            self.devicePixelRatioF(),
            screen.name() if screen is not None else "?",
        )

    def _initial_size(self):
        """按屏幕可用区域挑一个合适的初始大小（对齐原 Tk 版的比例）。"""
        screen = self.screen() or QGuiApplication.primaryScreen()
        if screen is None:
            return 980, 760
        geometry = screen.availableGeometry()
        width = max(780, min(1000, int(geometry.width() * 0.88)))
        height = max(560, min(780, int(geometry.height() * 0.9)))
        return width, height

    def hide_window(self):
        # 日志窗跟着主窗口一起收起：界面不见了就不该有窗口还在读文件
        if self._log_viewer is not None and self._log_viewer.isVisible():
            self._log_viewer.hide()
        self.hide()

    def _center_once(self):
        screen = self.screen() or QGuiApplication.primaryScreen()
        if screen is None:
            return
        geometry = screen.availableGeometry()
        frame = self.frameGeometry()
        frame.moveCenter(geometry.center())
        self.move(frame.topLeft())

    def closeEvent(self, event):  # noqa: N802 - Qt 命名
        if self._allow_close:
            event.accept()
            return
        event.ignore()
        self.hide()

    def allow_close(self):
        self._allow_close = True
