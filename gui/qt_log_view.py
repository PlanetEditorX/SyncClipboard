"""日志查看器（打开即自动刷新，关闭即停）。

资源边界很明确，全部围绕"窗口可见"这一件事：

1. 构造函数不碰磁盘，只有用户点开某个日志（`open_source`）时才读第一次；
2. 自动刷新由 2 秒的 QTimer 驱动，但定时器**只在窗口显示期间运行**：
   `showEvent` 启动，`hideEvent` / `closeEvent` 停止；
3. `refresh()` 自己也先查 `isVisible()`，隐藏状态下即使有人误触发也不产生 I/O；
4. 内容没变化时不重设正文，避免打断用户选中文本和滚动位置。

服务日志 / 客户端日志 / 界面日志是三份独立文件，切换分段即切换来源，
切过去才会去读对应文件，没点过的文件全程不会被打开。
"""

import logging
import os
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QRegularExpression, QTimer, Qt
from PySide6.QtGui import QColor, QGuiApplication, QSyntaxHighlighter, QTextCharFormat
from PySide6.QtWidgets import (
    QButtonGroup,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
)

from common.log_files import (
    DEFAULT_SOURCE,
    LOG_SOURCES,
    LogSnapshot,
    format_size,
    read_log,
)
from common.utils import BASE_DIR
from gui.qt_dialogs import info_message
from gui.qt_theme import LIGHT, app_icon

logger = logging.getLogger("gui")

# 只给异常级别上色，INFO 保持正文色，避免整屏花掉
LEVEL_RULES = (
    (r"\[(?:ERROR|CRITICAL)\]", "danger"),
    (r"\[WARNING\]", "warning"),
    (r"\[DEBUG\]", "muted"),
)


class LogLevelHighlighter(QSyntaxHighlighter):
    """按日志级别着色。只在内容变化时工作，没有额外定时器。"""

    def __init__(self, document, palette):
        super().__init__(document)
        self._rules = []
        self.set_palette_colors(palette)

    def set_palette_colors(self, palette):
        palette = palette or LIGHT
        self._rules = []
        for pattern, key in LEVEL_RULES:
            fmt = QTextCharFormat()
            fmt.setForeground(QColor(palette[key]))
            self._rules.append((QRegularExpression(pattern), fmt))
        self.rehighlight()

    def highlightBlock(self, text):  # noqa: N802 - Qt 命名
        for expression, fmt in self._rules:
            iterator = expression.globalMatch(text)
            while iterator.hasNext():
                match = iterator.next()
                self.setFormat(match.capturedStart(), match.capturedLength(), fmt)


AUTO_REFRESH_MS = 2000


class LogViewer(QDialog):
    """日志窗口。单实例复用，关掉只是隐藏，隐藏时自动刷新随之停止。"""

    def __init__(self, parent=None, palette=None, base_dir=None):
        super().__init__(parent)
        self._palette = palette or dict(LIGHT)
        self._base_dir = Path(base_dir) if base_dir is not None else BASE_DIR
        self._source = DEFAULT_SOURCE
        self._has_content = False

        self.setWindowTitle("运行日志")
        self.setWindowIcon(app_icon("active"))
        self.setMinimumSize(640, 420)
        self.resize(860, 580)

        self._build()
        self._update_path_label()

        # 自动刷新定时器：构造时不启动，等窗口真正显示才跑（见 showEvent）
        self._auto_timer = QTimer(self)
        self._auto_timer.setInterval(AUTO_REFRESH_MS)
        self._auto_timer.timeout.connect(self.refresh)

    # ------------------------------------------------------------------ 构建

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(11)

        head = QHBoxLayout()
        head.setSpacing(9)
        head.addWidget(self._build_selector(), 0, Qt.AlignVCenter)
        head.addStretch(1)

        self.copy_button = self._small_button("复制全文", self._copy_all)
        self.folder_button = self._small_button("打开日志目录", self._open_folder)
        self.refresh_button = self._small_button("刷新", self.refresh, primary=True)
        for button in (self.copy_button, self.folder_button, self.refresh_button):
            head.addWidget(button, 0, Qt.AlignVCenter)
        root.addLayout(head)

        info = QHBoxLayout()
        info.setSpacing(10)
        self.path_label = QLabel()
        self.path_label.setObjectName("Hint")
        self.path_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.time_label = QLabel()
        self.time_label.setObjectName("Hint")
        info.addWidget(self.path_label, 1)
        info.addWidget(self.time_label, 0)
        root.addLayout(info)

        self.text = QPlainTextEdit()
        self.text.setObjectName("LogText")
        self.text.setReadOnly(True)
        self.text.setUndoRedoEnabled(False)  # 只读日志不需要撤销栈，省内存
        self.text.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.text.setPlaceholderText("正在读取日志…")
        self.highlighter = LogLevelHighlighter(self.text.document(), self._palette)
        root.addWidget(self.text, 1)

        self.status_label = QLabel("未加载")
        self.status_label.setObjectName("Hint")
        self.status_label.setWordWrap(True)
        self._set_status("未加载", self._palette["muted"])
        root.addWidget(self.status_label)

    def _build_selector(self):
        bar = QFrame()
        bar.setObjectName("SegmentBar")
        bar.setFixedHeight(34)
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(3, 3, 3, 3)
        layout.setSpacing(2)

        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self._buttons = {}
        for source, (title, _file_name, _description) in LOG_SOURCES.items():
            button = QPushButton(title)
            button.setObjectName("Segment")
            button.setCheckable(True)
            button.setCursor(Qt.PointingHandCursor)
            button.setFixedHeight(26)
            button.setMinimumWidth(88)
            button.clicked.connect(
                lambda _checked=False, value=source: self._on_source_clicked(value)
            )
            self._group.addButton(button)
            layout.addWidget(button)
            self._buttons[source] = button
        return bar

    def _small_button(self, text, handler, primary=False):
        button = QPushButton(text)
        if primary:
            button.setObjectName("Primary")
        button.setCursor(Qt.PointingHandCursor)
        button.setMinimumHeight(32)
        button.setMinimumWidth(92)
        button.clicked.connect(lambda _checked=False: handler())
        return button

    # -------------------------------------------------------------- 对外接口

    def open_source(self, source=DEFAULT_SOURCE):
        """切换来源并读取一次；这是「用户点了某个日志」的唯一入口。"""
        self._select_source(source)
        if not self.isVisible():
            self.show()
        self.raise_()
        self.activateWindow()
        self.refresh()

    def refresh(self):
        """读一次日志。窗口不可见时直接返回——隐藏状态下不该产生磁盘 I/O。"""
        if not self.isVisible():
            return
        snapshot = read_log(self._source, self._base_dir)
        self._render(snapshot)

    def set_palette(self, palette):
        """外观切换时同步高亮配色（QSS 由全局负责）。"""
        self._palette = palette or dict(LIGHT)
        highlighter = getattr(self, "highlighter", None)
        if highlighter is not None:
            highlighter.set_palette_colors(self._palette)
        self.text.viewport().update()

    # ---------------------------------------------------------------- 内部

    def _on_source_clicked(self, source):
        self._select_source(source)
        self.refresh()

    def _select_source(self, source):
        if source not in LOG_SOURCES:
            source = DEFAULT_SOURCE
        self._source = source
        button = self._buttons.get(source)
        if button is not None and not button.isChecked():
            button.setChecked(True)
        self._update_path_label()

    def _update_path_label(self, snapshot=None):
        title, file_name, description = LOG_SOURCES[self._source]
        path = snapshot.path if snapshot is not None else (self._base_dir / "log" / file_name)
        self.path_label.setText(f"{title} · {description} · {path}")

    def _render(self, snapshot: LogSnapshot):
        self._update_path_label(snapshot)

        if snapshot.error:
            # 读取失败（含文件尚未生成）时不动正文，避免把已看到的内容清空
            self._set_status(snapshot.error, self._palette["danger"])
            self.time_label.setText("")
            return

        scroll = self.text.verticalScrollBar()
        stick_to_bottom = (not self._has_content) or scroll.value() >= scroll.maximum() - 4
        kept_position = scroll.value()

        # 自动刷新每 2 秒跑一次，内容没变就别重设正文：
        # setPlainText 会清掉选中区并让滚动条跳位，平白打断正在看日志的人。
        if snapshot.text != self.text.toPlainText():
            self.text.setPlainText(snapshot.text)
            if stick_to_bottom:
                scroll.setValue(scroll.maximum())
            else:
                scroll.setValue(min(kept_position, scroll.maximum()))
        self._has_content = bool(snapshot.text)

        detail = f"共 {snapshot.line_count} 行 · {format_size(snapshot.file_size)}"
        if snapshot.truncated:
            detail += " · 已截断，仅显示尾部"
        if not snapshot.text:
            detail += " · 日志内容为空"
        self._set_status(detail, self._palette["muted"])
        self.time_label.setText(
            f"读取于 {datetime.now():%H:%M:%S} · 每 {AUTO_REFRESH_MS // 1000} 秒自动刷新"
        )

    def _set_status(self, text, color):
        self.status_label.setText(text)
        self.status_label.setStyleSheet(f"color: {color}; font-size: 11px;")

    def _copy_all(self):
        content = self.text.toPlainText()
        if not content:
            self._set_status("当前没有可复制的内容", self._palette["warning"])
            return
        QGuiApplication.clipboard().setText(content)
        self._set_status(
            f"已复制 {content.count(chr(10)) + 1} 行到剪贴板", self._palette["success"]
        )

    def _open_folder(self):
        folder = self._base_dir / "log"
        try:
            folder.mkdir(parents=True, exist_ok=True)
            os.startfile(folder)
        except Exception as exc:  # noqa: BLE001 - 需要告知用户
            info_message("无法打开日志目录", str(exc))

    def showEvent(self, event):  # noqa: N802 - Qt 命名
        """窗口显示后才开始自动刷新，这样隐藏期间不存在任何周期任务。"""
        super().showEvent(event)
        if not self._auto_timer.isActive():
            self._auto_timer.start()

    def hideEvent(self, event):  # noqa: N802 - Qt 命名
        # 最小化、被主窗口收起、关闭都会走到这里：一律停表
        self._auto_timer.stop()
        super().hideEvent(event)

    def closeEvent(self, event):  # noqa: N802 - Qt 命名
        # 关闭即停止自动刷新；正文保留，下次打开会重新读一次并滚到底部
        self._auto_timer.stop()
        self._has_content = False
        super().closeEvent(event)
