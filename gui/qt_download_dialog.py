"""Qt 版下载进度对话框。

接口与原 Tk 版保持一致（reset / update_progress / cancel / close /
is_cancelled），file_handler 只需要换 import，调用方式不变。

下载线程会频繁调用 update_progress，所以这里所有改动都必须经由主线程
（file_handler 已经用 post_to_main_thread 包了一层），窗口内部不再做加锁。
"""

import logging

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
)

from common import ui_bridge
from gui.qt_theme import app_icon

logger = logging.getLogger("gui")


class DownloadProgressDialog(QDialog):
    """下载进度窗口（非模态，可取消）。"""

    def __init__(self, title="下载进度", parent=None):
        super().__init__(parent or ui_bridge.get_main_window())
        self.cancelled = False
        self._closed = False

        self.setWindowTitle(title)
        self.setWindowIcon(app_icon("active"))
        self.setModal(False)
        self.setMinimumWidth(440)
        self.setWindowFlag(Qt.WindowContextHelpButtonHint, False)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(22, 20, 22, 18)
        lay.setSpacing(12)

        self.title_label = QLabel("正在下载文件…")
        self.title_label.setStyleSheet("font-size: 15px; font-weight: 600;")
        lay.addWidget(self.title_label)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(12)
        lay.addWidget(self.progress)

        self.detail_label = QLabel("准备下载…")
        self.detail_label.setObjectName("Hint")
        lay.addWidget(self.detail_label)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.cancel_button = QPushButton("取消下载")
        self.cancel_button.setCursor(Qt.PointingHandCursor)
        self.cancel_button.setMinimumHeight(34)
        self.cancel_button.setFixedWidth(110)
        self.cancel_button.clicked.connect(self.cancel)
        buttons.addWidget(self.cancel_button)
        lay.addLayout(buttons)

        self._center()
        self.show()

    def _center(self):
        screen = self.screen() or QGuiApplication.primaryScreen()
        if screen is None:
            return
        geometry = screen.availableGeometry()
        self.adjustSize()
        frame = self.frameGeometry()
        frame.moveCenter(geometry.center())
        self.move(frame.topLeft())

    def reset(self, filename, total_size=0):
        """重新开始一次下载（窗口复用）。"""
        self.cancelled = False
        self._closed = False
        self.title_label.setText(f"正在下载：{filename}")
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.detail_label.setText("准备下载…")
        self.show()
        self.raise_()

    def set_busy(self, text="正在打包…"):
        """不确定进度阶段（例如等发送端压缩目录）：进度条走循环动画。"""
        if self._closed:
            return
        self.progress.setRange(0, 0)
        self.detail_label.setText(text)

    def update_progress(self, percentage, downloaded_mb, total_mb):
        if self._closed:
            return
        self.progress.setValue(int(percentage))
        if total_mb > 0:
            self.detail_label.setText(
                f"下载进度：{percentage}% ({downloaded_mb:.1f} MB / {total_mb:.1f} MB)"
            )
        else:
            self.detail_label.setText(f"已下载：{downloaded_mb:.1f} MB")

    def _hide(self):
        self._closed = True
        self.hide()

    def cancel(self):
        """用户主动取消。"""
        self.cancelled = True
        self._hide()

    def close(self):  # noqa: A003 - 对齐原接口命名
        """下载结束后的关闭；不视为取消。"""
        self._hide()

    def closeEvent(self, event):  # noqa: N802 - Qt 命名
        # 点右上角 X 等同于取消下载
        if not self._closed:
            self.cancelled = True
            self._closed = True
        super().closeEvent(event)

    def is_cancelled(self):
        return self.cancelled
