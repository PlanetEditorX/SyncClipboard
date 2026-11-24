"""PySide6 主控制台窗口。

职责与原 CustomTkinter 版一致：单页面板（状态 / 服务开关 / 快捷操作 /
连接设置 / 更新），业务逻辑（config、service、file、clipboard）完全不碰。

关键差别在于渲染：Qt 有窗口级 backing store，拖动缩放时不会像 Tk 那样
先暴露未绘制区域（也就是原来那 0.5 秒花屏），重绘耗时也从 ~730ms 降到
~60ms。QSS 让圆角卡片、焦点态、悬停态都能一次统一。
"""

import logging
import shutil
import tempfile
import threading
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QComboBox,
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
from common.sync_modes import MODE_BOTH, MODE_LABELS, VALID_MODES
from common.update_apply import (
    UpdateError,
    ensure_writable,
    extract_package,
    is_self_update_available,
    launch_updater,
    resolve_app_dir,
)
from common.updater import (
    APP_VERSION,
    UPDATE_MIRROR_PRESETS,
    UPDATE_SOURCE_DIRECT,
    UPDATE_SOURCE_MANUAL,
    UPDATE_SOURCE_MIRROR,
    UpdateCancelled,
    UpdateSource,
    check_for_update,
    download_file,
)
from common.utils import BASE_DIR, open_path, post_to_main_thread_no_wait
from gui import qt_dialogs
from gui.config_manager import LOCAL_SERVER_HOST
from gui.main_window import (
    DEFAULT_FILE_SHARE_TTL_MINUTES,
    DEFAULT_FOLDER_CONFIRM_TIMEOUT_SECONDS,
    DEFAULT_FOLDER_FILE_COUNT_CONFIRM,
    DEFAULT_FOLDER_ZIP_CONFIRM_GB,
    DEFAULT_SHARE_DOWNLOAD_LIMIT,
    DEFAULT_UPDATE_MIRROR_PREFIX,
    MAX_SHARE_DOWNLOAD_LIMIT,
    validate_client_settings,
)
from gui.qt_clients import ClientsDialog
from gui.qt_download_records import DownloadRecordsDialog
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

# 哪些「服务器地址」算作本机：只有连本机时，面板密钥才等同于本机服务器的密钥
LOCAL_HOST_ALIASES = {"127.0.0.1", "localhost", "::1", "[::1]"}


def points_to_local_server(host):
    """连接的服务器地址是不是本机。"""
    return str(host or "").strip().lower() in LOCAL_HOST_ALIASES


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

        # 下载记录窗口复用同一个实例：关掉再开不叠窗口，定时器也随窗口停止
        self._download_records_dialog = None
        self._clients_dialog = None
        self._update_state = "idle"
        self._update_result = None
        self._pending_payload = None
        self._pending_work_dir = None
        self._last_download_percent = -1
        # 下载中时非空：置位即请求中断，下载线程负责清理临时目录
        self._update_cancel_event = None
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
        self._build_sync_controls()
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
            "本机服务器", "为局域网中转剪贴板，开启后自动连回本机", "S", self._palette
        )
        self.server_card.setToolTip(
            f"开启后自动把「服务器地址」改为 {LOCAL_SERVER_HOST}，并重启正在运行的客户端"
        )
        self.client_card = ServiceCard(
            "同步客户端", "监听并同步本机剪贴板变化", "C", self._palette
        )
        self.server_card.switch.toggled.connect(self._on_server_toggled)
        self.client_card.switch.toggled.connect(self._on_client_toggled)

        row.addWidget(self.server_card, 1)
        row.addWidget(self.client_card, 1)
        self.body.addLayout(row)

    def _build_sync_controls(self):
        """同步控制：文字/文件的收发方向、目录共享开关与确认阈值、共享存活时间。"""
        card = QFrame()
        card.setObjectName("Card")
        lay = QVBoxLayout(card)
        lay.setContentsMargins(20, 18, 20, 18)
        lay.setSpacing(14)

        head = QHBoxLayout()
        head.addWidget(_section_title("同步控制"))
        head.addStretch(1)
        hint = QLabel("保存后客户端会自动重启并应用")
        hint.setObjectName("Hint")
        head.addWidget(hint)
        lay.addLayout(head)

        grid = QGridLayout()
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(10)
        grid.setColumnStretch(1, 1)

        text_caption = QLabel("文字同步")
        text_caption.setObjectName("FieldLabel")
        self.text_mode_bar, self.text_mode_group, self.text_mode_buttons = (
            self._sync_mode_selector()
        )
        grid.addWidget(text_caption, 0, 0)
        grid.addWidget(self.text_mode_bar, 0, 1, Qt.AlignLeft)

        file_caption = QLabel("文件同步")
        file_caption.setObjectName("FieldLabel")
        self.file_mode_bar, self.file_mode_group, self.file_mode_buttons = (
            self._sync_mode_selector()
        )
        grid.addWidget(file_caption, 1, 0)
        grid.addWidget(self.file_mode_bar, 1, 1, Qt.AlignLeft)
        lay.addLayout(grid)

        preference = QFrame()
        preference.setObjectName("Preference")
        pref_lay = QHBoxLayout(preference)
        pref_lay.setContentsMargins(16, 9, 16, 9)

        pref_box = QVBoxLayout()
        pref_box.setSpacing(2)
        pref_label = QLabel("同步复制的文件夹")
        pref_label.setStyleSheet("font-weight: 600;")
        pref_hint = QLabel(
            "关闭后复制文件夹不会被同步；开启时总大小或文件数超过阈值，"
            "会在右下角弹通知确认"
        )
        pref_hint.setObjectName("Hint")
        # 长提示不换行会把整张卡片的最小宽度顶到窗口最小宽度以上，
        # 而滚动区关掉了横向滚动条，超出部分直接被裁掉
        pref_hint.setWordWrap(True)
        pref_box.addWidget(pref_label)
        pref_box.addWidget(pref_hint)

        self.folder_sync_switch = ToggleSwitch(palette=self._palette)
        self.folder_sync_switch.toggled.connect(self._on_folder_sync_toggled)
        pref_lay.addLayout(pref_box, 1)
        pref_lay.addWidget(self.folder_sync_switch, 0, Qt.AlignVCenter)
        lay.addWidget(preference)

        # 四个数字框排两列两行：一行放四个会在最小宽度（780）下横向溢出，
        # 而滚动区刻意关掉了横向滚动条，溢出部分会被直接裁掉。
        numbers = QGridLayout()
        numbers.setHorizontalSpacing(24)
        numbers.setVerticalSpacing(8)
        limit_box, self.folder_limit_edit = self._number_input("目录大小阈值", "GB")
        count_box, self.folder_count_edit = self._number_input("目录文件数阈值", "个")
        confirm_box, self.folder_confirm_edit = self._number_input(
            "确认弹窗持续时间", "秒"
        )
        self.folder_confirm_edit.setToolTip(
            "目录超出阈值时在右下角弹出通知，这么多秒内没点「允许共享」就不共享"
        )
        ttl_box, self.share_ttl_edit = self._number_input("共享存活时间", "分钟")
        numbers.addWidget(limit_box, 0, 0)
        numbers.addWidget(count_box, 0, 1)
        numbers.addWidget(confirm_box, 1, 0)
        numbers.addWidget(ttl_box, 1, 1)
        numbers.setColumnStretch(2, 1)
        lay.addLayout(numbers)

        cancel_card = QFrame()
        cancel_card.setObjectName("Preference")
        cancel_lay = QHBoxLayout(cancel_card)
        cancel_lay.setContentsMargins(16, 9, 16, 9)
        cancel_box = QVBoxLayout()
        cancel_box.setSpacing(2)
        cancel_label = QLabel("下载完成后自动取消共享")
        cancel_label.setStyleSheet("font-weight: 600;")
        cancel_hint = QLabel(
            "关闭 = 存活期内所有已配对设备都能获取；开启 = 完成指定次数的完整下载后自动取消共享"
        )
        cancel_hint.setObjectName("Hint")
        cancel_hint.setWordWrap(True)
        cancel_box.addWidget(cancel_label)
        cancel_box.addWidget(cancel_hint)
        self.share_auto_cancel_switch = ToggleSwitch(palette=self._palette)
        self.share_auto_cancel_switch.toggled.connect(self._on_auto_cancel_toggled)
        limit_box, self.share_limit_edit = self._number_input("可下载次数", "次")
        self.share_limit_edit.setToolTip(
            f"1 到 {MAX_SHARE_DOWNLOAD_LIMIT}；按设备计次，"
            "以实际发出的字节达到共享总大小判定「下载完成」，重试不重复计数"
        )
        cancel_lay.addLayout(cancel_box, 1)
        cancel_lay.addWidget(limit_box, 0, Qt.AlignVCenter)
        cancel_lay.addWidget(self.share_auto_cancel_switch, 0, Qt.AlignVCenter)
        lay.addWidget(cancel_card)

        notify_card = QFrame()
        notify_card.setObjectName("Preference")
        notify_lay = QHBoxLayout(notify_card)
        notify_lay.setContentsMargins(16, 9, 16, 9)
        notify_box = QVBoxLayout()
        notify_box.setSpacing(2)
        notify_label = QLabel("服务器派发时也弹通知")
        notify_label.setStyleSheet("font-weight: 600;")
        notify_hint = QLabel(
            "手机推给本机服务器时会弹窗；开启后，服务器再把内容派发到这台电脑的"
            "客户端时同样提醒。关闭后仍然同步，只是不弹窗"
        )
        notify_hint.setObjectName("Hint")
        notify_hint.setWordWrap(True)
        notify_box.addWidget(notify_label)
        notify_box.addWidget(notify_hint)
        self.notify_remote_push_switch = ToggleSwitch(palette=self._palette)
        notify_lay.addLayout(notify_box, 1)
        notify_lay.addWidget(self.notify_remote_push_switch, 0, Qt.AlignVCenter)
        lay.addWidget(notify_card)

        tip = QLabel(
            "超过阈值时在右下角弹出通知，点「允许共享」才登记共享；"
            "到「确认弹窗持续时间」还没点就按不共享处理。开始下载后每次成功请求自动续期。"
            "「下载完成后自动取消共享」按设备计次：发送端累计实际发出的字节，"
            "普通文件发满整个文件、目录清单里的文件全部发完才算一次下载；"
            "次数用尽后自动取消登记并拒绝新设备。到期或次数用尽只取消登记，不会删除原文件。"
        )
        tip.setObjectName("Hint")
        tip.setWordWrap(True)
        lay.addWidget(tip)

        self.body.addWidget(card)

    def _sync_mode_selector(self):
        """三段式选择：双向 / 只发送 / 只接收。"""
        bar = QFrame()
        bar.setObjectName("SegmentBar")
        bar.setFixedHeight(36)
        bl = QHBoxLayout(bar)
        bl.setContentsMargins(3, 3, 3, 3)
        bl.setSpacing(2)

        group = QButtonGroup(self)
        group.setExclusive(True)
        buttons = {}
        for mode in VALID_MODES:
            button = QPushButton(MODE_LABELS[mode])
            button.setObjectName("Segment")
            button.setCheckable(True)
            button.setCursor(Qt.PointingHandCursor)
            button.setMinimumWidth(74)
            button.setFixedHeight(28)
            group.addButton(button)
            bl.addWidget(button)
            buttons[mode] = button
        return bar, group, buttons

    def _number_input(self, label, unit):
        box = QWidget()
        row = QHBoxLayout(box)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)

        caption = QLabel(label)
        caption.setObjectName("FieldLabel")
        edit = QLineEdit()
        edit.setFixedWidth(74)
        edit.setMinimumHeight(34)
        edit.setAlignment(Qt.AlignCenter)
        suffix = QLabel(unit)
        suffix.setObjectName("Hint")

        row.addWidget(caption)
        row.addWidget(edit)
        row.addWidget(suffix)
        return box, edit

    def _on_folder_sync_toggled(self, checked):
        for name in ("folder_limit_edit", "folder_count_edit", "folder_confirm_edit"):
            edit = getattr(self, name, None)
            if edit is not None:
                edit.setEnabled(bool(checked))

    def _on_auto_cancel_toggled(self, checked):
        edit = getattr(self, "share_limit_edit", None)
        if edit is not None:
            edit.setEnabled(bool(checked))

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

        row2 = QHBoxLayout()
        row2.setSpacing(10)
        self.records_button = QPushButton("查看下载记录")
        self.records_button.setMinimumHeight(38)
        self.records_button.setCursor(Qt.PointingHandCursor)
        self.records_button.clicked.connect(self._show_download_records)

        self.clients_button = QPushButton("客户端管理")
        self.clients_button.setMinimumHeight(38)
        self.clients_button.setCursor(Qt.PointingHandCursor)
        self.clients_button.setToolTip("查看已登记的设备、各自的推送次数与送达情况，可移除")
        self.clients_button.clicked.connect(self._show_clients)

        records_hint = QLabel(
            "下载记录：谁在取、已经取走多少字节；客户端管理：推过几次、送达多少"
        )
        records_hint.setObjectName("Hint")
        records_hint.setWordWrap(True)

        # 托盘菜单在 Windows 11 的溢出区里偶尔会「点不动」，界面里留一个
        # 一定可用的退出入口
        self.quit_button = QPushButton("退出程序")
        self.quit_button.setMinimumHeight(38)
        self.quit_button.setCursor(Qt.PointingHandCursor)
        self.quit_button.setToolTip("停止本机服务器与同步客户端后退出，下次启动按原开关恢复")
        self.quit_button.clicked.connect(self._quit_app)

        row2.addWidget(self.records_button, 1)
        row2.addWidget(self.clients_button, 1)
        row2.addWidget(self.quit_button, 1)
        row2.addWidget(records_hint, 2)
        lay.addLayout(row2)
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
            "服务端、客户端与界面各写一份日志，点哪个读哪个；打开后每 2 秒自动刷新，"
            "窗口关闭即停止读取"
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

        # 密钥到底改的是谁：连远端时只影响客户端，连本机时同时决定本机服务器
        self.key_hint = QLabel("")
        self.key_hint.setObjectName("Hint")
        self.key_hint.setWordWrap(True)
        self.server_host_edit.textChanged.connect(self._refresh_key_hint)

        lay.addLayout(grid)
        lay.addWidget(self.key_hint)

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

        advanced = QPushButton("打开配置目录")
        advanced.setCursor(Qt.PointingHandCursor)
        # 按钮点了就是开资源管理器里的 config 目录，文案要跟行为对得上；
        # 六个字比原来的四个字宽，固定宽度留够
        advanced.setFixedWidth(132)
        advanced.setMinimumHeight(38)
        advanced.setToolTip(
            "在资源管理器里打开配置目录（config/），"
            "里面的 client_config.json、server_config.json 可直接编辑"
        )
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

        lay.addLayout(self._build_update_source_row())

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

    def _build_update_source_row(self):
        """更新源的网络路径：直连 / 镜像加速 / 手动代理。

        三档共用一行：分段按钮选档位，右侧同一个位置按档位显示「镜像前缀」
        下拉或「代理地址」输入框，避免面板纵向再长一截。
        """
        source_row = QHBoxLayout()
        source_row.setSpacing(10)

        caption = QLabel("更新源")
        caption.setObjectName("FieldLabel")

        bar = QFrame()
        bar.setObjectName("SegmentBar")
        bar.setFixedHeight(34)
        bar_lay = QHBoxLayout(bar)
        bar_lay.setContentsMargins(3, 3, 3, 3)
        bar_lay.setSpacing(2)

        group = QButtonGroup(self)
        group.setExclusive(True)
        self.update_source_buttons = {}
        for mode, text in (
            (UPDATE_SOURCE_DIRECT, "GitHub 直连"),
            (UPDATE_SOURCE_MIRROR, "镜像加速"),
            (UPDATE_SOURCE_MANUAL, "手动代理"),
        ):
            button = QPushButton(text)
            button.setObjectName("Segment")
            button.setCheckable(True)
            button.setCursor(Qt.PointingHandCursor)
            button.setMinimumWidth(76)
            button.setFixedHeight(26)
            group.addButton(button)
            bar_lay.addWidget(button)
            self.update_source_buttons[mode] = button
            button.clicked.connect(
                lambda _checked=False, mode=mode: self._on_update_source_changed(mode)
            )
        self.update_source_group = group

        self.mirror_combo = QComboBox()
        self.mirror_combo.setEditable(True)
        self.mirror_combo.addItems(UPDATE_MIRROR_PRESETS)
        self.mirror_combo.setMinimumHeight(34)
        self.mirror_combo.setMinimumWidth(168)
        self.mirror_combo.setToolTip(
            "镜像地址前缀：请求会被改写成「前缀 + 原始地址」。"
            "这些都是第三方服务，随时可能失效，也可以自己输入一个。"
        )

        self.proxy_edit = QLineEdit()
        self.proxy_edit.setPlaceholderText("http://127.0.0.1:7890")
        self.proxy_edit.setMinimumHeight(34)
        self.proxy_edit.setMinimumWidth(168)
        self.proxy_edit.setToolTip("只支持 HTTP/HTTPS 代理，不支持 SOCKS5")

        source_row.addWidget(caption)
        source_row.addWidget(bar)
        source_row.addWidget(self.mirror_combo, 1)
        source_row.addWidget(self.proxy_edit, 1)

        hint = QLabel("")
        hint.setObjectName("Hint")
        hint.setWordWrap(True)
        self.update_source_hint = hint

        box = QVBoxLayout()
        box.setSpacing(4)
        box.addLayout(source_row)
        box.addWidget(hint)
        return box

    def _on_update_source_changed(self, mode):
        self.mirror_combo.setVisible(mode == UPDATE_SOURCE_MIRROR)
        self.proxy_edit.setVisible(mode == UPDATE_SOURCE_MANUAL)
        hints = {
            UPDATE_SOURCE_DIRECT: "直连 GitHub 官方地址；国内网络可能很慢或直接超时。",
            UPDATE_SOURCE_MIRROR: (
                "检查与下载都改走镜像前缀，镜像失效会自动退回直连一次。"
                "这些是第三方公共服务，随时可能失效，选不到就自己填一个。"
            ),
            UPDATE_SOURCE_MANUAL: (
                "只给请求挂 HTTP/HTTPS 代理（本地代理软件一般同时开 127.0.0.1:7890）。"
                "不支持 SOCKS5 —— 那要额外装依赖，包体积会变大。"
            ),
        }
        self.update_source_hint.setText(hints.get(mode, ""))

    def _collect_update_source(self):
        """读出更新源设置；控件缺失（裸窗口的测试）时退回配置值。"""
        mode = getattr(self.config, "update_source", UPDATE_SOURCE_DIRECT)
        for key, button in (getattr(self, "update_source_buttons", None) or {}).items():
            if button.isChecked():
                mode = key
                break

        combo = getattr(self, "mirror_combo", None)
        prefix = (
            combo.currentText() if combo is not None
            else getattr(self.config, "update_mirror_prefix", DEFAULT_UPDATE_MIRROR_PREFIX)
        )
        proxy_edit = getattr(self, "proxy_edit", None)
        proxy = (
            proxy_edit.text() if proxy_edit is not None
            else getattr(self.config, "update_proxy", "")
        )
        return {
            "update_source": mode,
            "update_mirror_prefix": prefix,
            "update_proxy": proxy,
        }

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

        # 同步方向：文字 / 文件各一组分段按钮
        for buttons, saved_mode in (
            (self.text_mode_buttons, getattr(self.config, "text_sync_mode", MODE_BOTH)),
            (self.file_mode_buttons, getattr(self.config, "file_sync_mode", MODE_BOTH)),
        ):
            target = buttons.get(saved_mode) or buttons.get(MODE_BOTH)
            if target is not None:
                target.setChecked(True)

        folder_enabled = bool(getattr(self.config, "folder_sync_enabled", True))
        self.folder_sync_switch.setChecked(folder_enabled, animate=False)
        self.folder_limit_edit.setText(
            self._format_number(
                getattr(self.config, "folder_zip_confirm_gb", DEFAULT_FOLDER_ZIP_CONFIRM_GB)
            )
        )
        self.folder_count_edit.setText(
            str(int(getattr(self.config, "folder_file_count_confirm",
                            DEFAULT_FOLDER_FILE_COUNT_CONFIRM)))
        )
        # setChecked 不发出 toggled，关联控件的启用状态要单独同步
        self._on_folder_sync_toggled(folder_enabled)
        confirm_edit = getattr(self, "folder_confirm_edit", None)
        if confirm_edit is not None:
            confirm_edit.setText(
                str(
                    int(
                        getattr(
                            self.config,
                            "folder_confirm_timeout_seconds",
                            DEFAULT_FOLDER_CONFIRM_TIMEOUT_SECONDS,
                        )
                    )
                )
            )
        self.share_ttl_edit.setText(
            str(
                int(
                    getattr(
                        self.config,
                        "file_share_ttl_minutes",
                        DEFAULT_FILE_SHARE_TTL_MINUTES,
                    )
                )
            )
        )

        saved_limit = int(
            getattr(self.config, "share_download_limit", DEFAULT_SHARE_DOWNLOAD_LIMIT) or 0
        )
        cancel_switch = getattr(self, "share_auto_cancel_switch", None)
        if cancel_switch is not None:
            cancel_switch.setChecked(saved_limit > 0, animate=False)
        limit_edit = getattr(self, "share_limit_edit", None)
        if limit_edit is not None:
            limit_edit.setText(str(saved_limit if saved_limit > 0 else 1))
            limit_edit.setEnabled(saved_limit > 0)

        notify_switch = getattr(self, "notify_remote_push_switch", None)
        if notify_switch is not None:
            notify_switch.setChecked(
                bool(getattr(self.config, "notify_remote_push", True)), animate=False
            )

        # 更新源：三档分段 + 镜像下拉 + 代理输入框
        source_mode = getattr(self.config, "update_source", UPDATE_SOURCE_DIRECT)
        buttons = getattr(self, "update_source_buttons", None) or {}
        target = buttons.get(source_mode) or buttons.get(UPDATE_SOURCE_DIRECT)
        if target is not None:
            target.setChecked(True)
        combo = getattr(self, "mirror_combo", None)
        if combo is not None:
            combo.setCurrentText(
                getattr(
                    self.config, "update_mirror_prefix", DEFAULT_UPDATE_MIRROR_PREFIX
                )
            )
        proxy_edit = getattr(self, "proxy_edit", None)
        if proxy_edit is not None:
            proxy_edit.setText(getattr(self.config, "update_proxy", ""))
        if buttons:
            self._on_update_source_changed(source_mode)

        self.autostart_switch.setChecked(self.config.is_autostart_enabled(), animate=False)
        self._refresh_key_hint()

    def _refresh_key_hint(self, *_args):
        """说明「共享密钥」这一项到底改的是谁，跟着服务器地址实时变。

        只开客户端时它只是访问对方服务器的凭证；连本机或本机服务器开着时
        它同时是本机服务器（server_config.json）的密钥，保存会两边一起改 ——
        不写清楚很容易搞混这把钥匙到底开哪扇门。
        """
        hint = getattr(self, "key_hint", None)
        if hint is None:
            return
        host = self.server_host_edit.text().strip()
        local_host = points_to_local_server(host)
        server_on = self._server_is_enabled()
        if local_host and server_on:
            hint.setText(
                "当前连接指向本机，且本机服务器已开启：保存时这个密钥会同时写入"
                "客户端和本机服务器，服务随即重启；手机等其他设备也要改成同一个密钥。"
            )
        elif local_host:
            hint.setText(
                "当前连接指向本机：保存时这个密钥会同时写入客户端和本机服务器配置，"
                "下次启动本机服务器即生效。"
            )
        elif server_on:
            hint.setText(
                "本机服务器已开启：保存时这个密钥会同时写入客户端和本机服务器，"
                "服务随即重启；手机等其他设备也要改成同一个密钥。"
            )
        else:
            hint.setText(
                "这是连接 %s 所需的密钥，只写入客户端配置；本机服务器未开启，"
                "它自己的密钥在 server_config.json 里。" % (host or "服务器")
            )

    def _server_is_enabled(self):
        """本机服务器是否开着（面板状态查询失败时按关着处理，不多改配置）。"""
        try:
            manager = getattr(self, "manager", None)
            if manager is None:
                return False
            return bool(manager.get_service_state()["server_enabled"])
        except Exception:                                  # noqa: BLE001
            return False

    def _should_sync_key_to_server(self, host):
        """连本机、或本机服务器开着时，面板密钥同时是本机服务器的密钥。

        用户心智是「只开客户端 → 密钥是对方的；同时开服务器 → 两边都改」，
        所以服务状态也算一个条件：本机服务器给手机用时，即使这台电脑的客户端
        暂时连的是别台机器，面板密钥仍按本机服务器的密钥来改。
        """
        return points_to_local_server(host) or self._server_is_enabled()

    @staticmethod
    def _format_number(value):
        """整数值不带小数点（10 而不是 10.0），小数保留一位。"""
        try:
            number = float(value)
        except (TypeError, ValueError):
            return str(value)
        return f"{number:g}"

    def _selected_mode(self, buttons, fallback=MODE_BOTH):
        if not buttons:
            return fallback
        for mode, button in buttons.items():
            if button.isChecked():
                return mode
        return fallback

    def _collect_sync_settings(self):
        """读取同步控制区的当前值；控件缺失（旧测试的裸窗口）时退回配置值。"""
        switch = getattr(self, "folder_sync_switch", None)
        folder_enabled = (
            bool(switch.isChecked()) if switch is not None
            else bool(getattr(self.config, "folder_sync_enabled", True))
        )

        limit_edit = getattr(self, "folder_limit_edit", None)
        limit_gb = (
            limit_edit.text() if limit_edit is not None
            else getattr(self.config, "folder_zip_confirm_gb", DEFAULT_FOLDER_ZIP_CONFIRM_GB)
        )

        count_edit = getattr(self, "folder_count_edit", None)
        limit_count = (
            count_edit.text() if count_edit is not None
            else getattr(self.config, "folder_file_count_confirm",
                         DEFAULT_FOLDER_FILE_COUNT_CONFIRM)
        )

        confirm_edit = getattr(self, "folder_confirm_edit", None)
        confirm_timeout = (
            confirm_edit.text() if confirm_edit is not None
            else getattr(self.config, "folder_confirm_timeout_seconds",
                         DEFAULT_FOLDER_CONFIRM_TIMEOUT_SECONDS)
        )

        ttl_edit = getattr(self, "share_ttl_edit", None)
        ttl_minutes = (
            ttl_edit.text() if ttl_edit is not None
            else getattr(self.config, "file_share_ttl_minutes", DEFAULT_FILE_SHARE_TTL_MINUTES)
        )

        # 开关关闭 = 0（不限制）；开启时读数字框，0 会被校验拦下
        cancel_switch = getattr(self, "share_auto_cancel_switch", None)
        if cancel_switch is not None and not cancel_switch.isChecked():
            download_limit = 0
        else:
            limit_edit = getattr(self, "share_limit_edit", None)
            download_limit = (
                limit_edit.text() if limit_edit is not None
                else getattr(self.config, "share_download_limit",
                             DEFAULT_SHARE_DOWNLOAD_LIMIT)
            )

        notify_switch = getattr(self, "notify_remote_push_switch", None)
        notify_remote_push = (
            bool(notify_switch.isChecked()) if notify_switch is not None
            else bool(getattr(self.config, "notify_remote_push", True))
        )

        # 更新源控件可能不存在（裸窗口的测试只关心同步项），缺了就用配置值
        source_values = {}
        if hasattr(self, "_collect_update_source"):
            source_values = self._collect_update_source()

        return {
            "text_sync_mode": self._selected_mode(
                getattr(self, "text_mode_buttons", None),
                getattr(self.config, "text_sync_mode", MODE_BOTH),
            ),
            "file_sync_mode": self._selected_mode(
                getattr(self, "file_mode_buttons", None),
                getattr(self.config, "file_sync_mode", MODE_BOTH),
            ),
            "folder_sync_enabled": folder_enabled,
            "folder_zip_confirm_gb": limit_gb,
            "folder_file_count_confirm": limit_count,
            "folder_confirm_timeout_seconds": confirm_timeout,
            "file_share_ttl_minutes": ttl_minutes,
            "share_download_limit": download_limit,
            "notify_remote_push": notify_remote_push,
            **source_values,
        }

    def save_settings(self):
        try:
            values = validate_client_settings(
                self.server_host_edit.text(),
                self.server_port_edit.text(),
                self.local_name_edit.text(),
                self.file_port_edit.text(),
                self.key_edit.text(),
                self.save_path_edit.text(),
                **self._collect_sync_settings(),
            )
        except ValueError as exc:
            self._set_settings_message(str(exc), self._palette["danger"])
            return

        previous_ttl = getattr(self.config, "file_share_ttl_minutes", None)
        previous_key = getattr(self.config, "key", "")
        if not self.config.update_client_config(values):
            self._set_settings_message("配置保存失败，请查看日志", self._palette["danger"])
            return

        # 共享存活时间同时决定服务端何时丢弃文件记录，必须一起改
        ttl_changed = previous_ttl != values["file_share_ttl_minutes"]
        if ttl_changed:
            self.config.apply_file_share_ttl_to_server_config()

        # 本机服务器参与同步时，面板密钥就是它的密钥：只改客户端会让
        # 客户端拿新密钥去敲一个还认旧密钥的服务器，同步直接 401。
        key_synced = False
        key_changed = previous_key != values["key"]
        if key_changed and self._should_sync_key_to_server(values["server_host"]):
            key_synced = self.config.apply_key_to_server_config()

        self._refresh_key_hint()
        self._set_settings_message("配置已保存", self._palette["success"])
        self.welcome_detail.setText(
            f"连接到 {values['server_host']}:{values['server_port']} · 设备 {values['local_name']}"
        )
        state = self.manager.get_service_state()
        needs_restart = ttl_changed or key_synced
        if needs_restart and state["server_enabled"]:
            # 服务端按共享存活时间丢弃文件记录、按密钥校验请求，改了都得重新加载配置；
            # restart_services 会把已启用的客户端一起重启，不必再单独调一次。
            if self.manager.restart_services_async():
                self._set_settings_message("配置已保存，正在重启服务…", self._palette["warning"])
            else:
                self._set_settings_message(
                    "配置已保存；服务正忙，请稍后手动重启", self._palette["warning"]
                )
        elif state["client_enabled"]:
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

        if not busy and self.settings_message.text() in (
            "配置已保存，正在重启客户端…",
            "配置已保存，正在重启服务…",
        ):
            if state["client_alive"] or state["server_alive"]:
                self._set_settings_message("配置已应用", palette["success"])
            else:
                self._set_settings_message("配置已保存，但客户端启动失败", palette["danger"])

        # 服务开关改变后，密钥是「只给客户端」还是「两边共用」也会变
        self._refresh_key_hint()

    def _connection_summary(self):
        """未配置时不要显示 "None:None"。"""
        host = self.config.server_host or "未配置服务器"
        port = self.config.server_port or "-"
        name = self.config.local_name or "未命名设备"
        return f"连接到 {host}:{port} · 设备 {name}"

    def on_local_server_host_applied(self, host=LOCAL_SERVER_HOST):
        """本机服务器开启后，连接地址已被自动改写；同步面板输入框与提示。"""
        edit = getattr(self, "server_host_edit", None)
        if edit is not None:
            edit.setText(host)
            edit.setCursorPosition(0)
        self.welcome_detail.setText(self._connection_summary())
        self._set_settings_message(
            f"已切换为连接本机服务器（{host}）", self._palette["success"]
        )

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

    def _quit_app(self):
        """界面里的退出入口：确认后走与托盘完全相同的退出流程。"""
        if not qt_dialogs.ask_yes_no(
            "退出 SyncClipboard",
            "将停止本机服务器与同步客户端并退出程序。\n"
            "下次启动会按当前开关状态恢复服务，是否继续？",
        ):
            return
        self.manager.quit_app()

    def _fetch_file(self):
        threading.Thread(
            target=self.manager.file_handler.fetch_file_with_progress,
            name="fetch-shared-file",
            daemon=True,
        ).start()

    def _open_download_folder(self):
        path = Path(self.save_path_edit.text() or self.config.save_path)
        path.mkdir(parents=True, exist_ok=True)
        ok, error = open_path(path, is_directory=True)
        if not ok:
            qt_dialogs.info_message("无法打开目录", error)

    def _show_download_records(self):
        """下载记录窗口：打开后每秒刷新，实时看进度与事件。

        窗口是非模态的，可以一边开着一边操作主界面；重复点击只是把它提到
        前面，不会叠出第二个。端口 / 密钥改过之后按新值重新拉取。
        """
        port = getattr(self.config, "file_server_port", 8899)
        key = getattr(self.config, "key", "")
        if self._download_records_dialog is None:
            self._download_records_dialog = DownloadRecordsDialog(
                self, port=port, key=key
            )
        else:
            self._download_records_dialog.set_endpoint(port=port, key=key)
        self._download_records_dialog.show()
        self._download_records_dialog.raise_()
        self._download_records_dialog.activateWindow()

    def _show_clients(self):
        """客户端管理窗口：看设备、推送次数与送达情况，可逐条移除。

        名单存在**服务端**进程里，所以问的是当前连的那台服务器：连本机就是
        127.0.0.1，连别人家就用配置里的地址。客户端自己存不了一份「谁来过」，
        也没有意义 —— 换台机器看到的就是另一批设备。

        `local_name` 传的是**看窗口的这台电脑**，用来标「本机」；服务端下发
        的 `is_server` 另算，只标「服务器」，不能混。
        """
        host = str(getattr(self.config, "server_host", "") or "").strip()
        if points_to_local_server(host) or not host:
            host = "127.0.0.1"
        port = getattr(self.config, "server_port", 8000)
        key = getattr(self.config, "key", "")
        local_name = str(getattr(self.config, "local_name", "") or "").strip()
        if self._clients_dialog is None:
            self._clients_dialog = ClientsDialog(
                self, host=host, port=port, key=key, local_name=local_name
            )
        else:
            self._clients_dialog.set_endpoint(
                host=host, port=port, key=key, local_name=local_name
            )
        self._clients_dialog.show()
        self._clients_dialog.raise_()
        self._clients_dialog.activateWindow()

    def _open_config_folder(self):
        config_dir = BASE_DIR / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        ok, error = open_path(config_dir, is_directory=True)
        if not ok:
            qt_dialogs.info_message("无法打开目录", error)

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
        ok, error = open_path(log_dir, is_directory=True)
        if not ok:
            qt_dialogs.info_message("无法打开日志目录", error)

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
        """按钮唯一入口：按当前状态决定检查、下载、取消还是重启更新。"""
        if self._update_state == "checking":
            return
        if self._update_state in {"downloading", "cancelling"}:
            self._cancel_update()
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

    def _current_update_source(self):
        """把面板里选的三档转成 updater 认识的对象。"""
        return UpdateSource(
            mode=getattr(self.config, "update_source", UPDATE_SOURCE_DIRECT),
            mirror_prefix=getattr(
                self.config, "update_mirror_prefix", DEFAULT_UPDATE_MIRROR_PREFIX
            ),
            proxy=getattr(self.config, "update_proxy", ""),
        )

    def _run_update_check(self):
        result = check_for_update(source=self._current_update_source())
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
        # 每次下载一个新的取消令牌；取消后这个 Event 就作废
        self._update_cancel_event = threading.Event()
        self._set_update_state(
            "downloading",
            "正在下载更新…",
            self._palette["warning"],
            button_text="取消下载",
            enabled=True,
        )
        threading.Thread(
            target=self._download_update, name="update-download", daemon=True
        ).start()

    def _cancel_update(self):
        """取消正在进行的下载：置位令牌，下载线程会抛 UpdateCancelled。

        临时目录的清理放在下载线程的 except 分支里做 —— 只有那边知道
        work_dir 的确切路径，也只有那边能确认文件句柄已经关掉。
        """
        event = getattr(self, "_update_cancel_event", None)
        if event is None or event.is_set():
            return
        event.set()
        self._set_update_state(
            "cancelling",
            "正在取消下载…",
            self._palette["warning"],
            button_text="取消中…",
            enabled=False,
        )

    def _download_update(self):
        work_dir = Path(tempfile.mkdtemp(prefix="syncclipboard_update_"))
        cancel_event = getattr(self, "_update_cancel_event", None)
        try:
            zip_path = work_dir / "update.zip"
            download_file(
                self._update_result.download_url,
                zip_path,
                progress=self._on_download_progress,
                source=self._current_update_source(),
                cancel_event=cancel_event,
            )
            payload = extract_package(zip_path, work_dir / "payload")
        except UpdateCancelled:
            logger.info("用户取消更新下载，清理临时目录: %s", work_dir)
            shutil.rmtree(work_dir, ignore_errors=True)
            post_to_main_thread_no_wait(self._on_update_cancelled)
            return
        except Exception as exc:  # noqa: BLE001
            logger.warning("下载更新失败: %s", exc)
            shutil.rmtree(work_dir, ignore_errors=True)
            post_to_main_thread_no_wait(self._on_update_failed, str(exc))
            return
        post_to_main_thread_no_wait(self._finish_download, payload, work_dir)

    def _on_update_cancelled(self):
        self._update_cancel_event = None
        self._last_download_percent = -1
        latest = getattr(self._update_result, "latest_version", "") or ""
        self._set_update_state(
            "available",
            f"已取消下载，临时文件已清理；可重新下载 v{latest}",
            self._palette["muted"],
            button_text=f"更新到 v{latest}" if latest else "重新下载",
        )

    def _on_update_failed(self, message):
        self._update_cancel_event = None
        self._set_update_state(
            "idle", f"更新失败：{message}", self._palette["danger"]
        )

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
