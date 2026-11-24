"""Qt 版下载进度对话框。

接口与原 Tk 版保持一致（reset / update_progress / cancel / close /
is_cancelled），file_handler 的调用方式不变。

**线程约束**：下载跑在 worker 线程，Qt 控件只能在 GUI 线程碰。早先这里
只有「构造窗口」经过 ui_bridge 投递，之后 reset / update_progress /
close 全是 worker 线程直连 QWidget 方法 —— 在 Windows 上 raise_()/hide()
会走到窗口管理器调用，非 GUI 线程执行时可能把主线程一起卡住，表现就是
主窗口和进度窗口同时「未响应」且进度窗一片空白。

现在对外方法只做两件事：读写线程安全的取消标志、emit 信号。所有控件
操作都放在信号连到的槽里，由 Qt 的 AutoConnection 自动排到 GUI 线程。
"""

import logging
import threading

from PySide6.QtCore import Qt, Signal
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

# GUI 线程持有的强引用。下载跑在 worker 线程，dialog 是那边的局部变量；
# 若不做这件事，worker 一结束 Python 就回收它，QWidget 会被**在非 GUI 线程**
# 析构 —— 实测直接 segfault。这里留一份引用，改由 _apply_hide 里的
# deleteLater() 在 GUI 线程销毁。
_alive_dialogs = set()


class DownloadProgressDialog(QDialog):
    """下载进度窗口（非模态，可取消）。"""

    # worker 线程 emit → AutoConnection 排进 GUI 线程事件队列。
    _reset_signal = Signal(str, int)
    _busy_signal = Signal(str)
    _progress_signal = Signal(int, float, float)
    _hide_signal = Signal()

    def __init__(self, title="下载进度", parent=None):
        super().__init__(parent or ui_bridge.get_main_window())
        self.cancelled = False
        self._closed = False
        # 取消状态是跨线程读的，用 Event 而不是让 worker 去问 QWidget
        self._cancel_event = threading.Event()

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

        self._reset_signal.connect(self._apply_reset)
        self._busy_signal.connect(self._apply_busy)
        self._progress_signal.connect(self._apply_progress)
        self._hide_signal.connect(self._apply_hide)

        _alive_dialogs.add(self)
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

    # ------------------------------------------------------------ 对外接口
    # 这几个方法都可能由 worker 线程调用，一律只动标志 / emit 信号。

    def reset(self, filename, total_size=0):
        """重新开始一次下载（窗口复用）。"""
        self._cancel_event.clear()
        self.cancelled = False
        self._closed = False
        self._reset_signal.emit(str(filename), int(total_size or 0))

    def set_busy(self, text="正在同步…"):
        """不确定进度阶段（例如等发送端压缩目录）：进度条走循环动画。"""
        if self._closed:
            return
        self._busy_signal.emit(str(text))

    def update_progress(self, percentage, downloaded_mb, total_mb):
        if self._closed:
            return
        self._progress_signal.emit(
            int(percentage), float(downloaded_mb), float(total_mb)
        )

    def cancel(self):
        """用户主动取消（按钮点击，运行在 GUI 线程）。"""
        self.cancelled = True
        self._cancel_event.set()
        self._apply_hide()

    def close(self):  # noqa: A003 - 对齐原接口命名
        """下载结束后的关闭；不视为取消。"""
        self._hide_signal.emit()

    def is_cancelled(self):
        return self._cancel_event.is_set()

    # ------------------------------------------------------------ GUI 线程槽
    def _apply_reset(self, filename, _total_size):
        self.title_label.setText(f"正在下载：{filename}")
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.detail_label.setText("准备下载…")
        self._closed = False
        self.show()
        self.raise_()

    def _apply_busy(self, text):
        if self._closed:
            return
        self.progress.setRange(0, 0)
        self.detail_label.setText(text)

    def _apply_progress(self, percentage, downloaded_mb, total_mb):
        if self._closed:
            return
        self.progress.setValue(percentage)
        if total_mb > 0:
            self.detail_label.setText(
                f"下载进度：{percentage}% ({downloaded_mb:.1f} MB / {total_mb:.1f} MB)"
            )
        else:
            self.detail_label.setText(f"已下载：{downloaded_mb:.1f} MB")

    def _apply_hide(self):
        self._closed = True
        self.hide()
        # deleteLater 是线程安全的，但真正销毁发生在 GUI 线程 —— 绝不能
        # 让 worker 线程末尾的 GC 去析构 QWidget。
        _alive_dialogs.discard(self)
        self.deleteLater()

    def closeEvent(self, event):  # noqa: N802 - Qt 命名
        # 点右上角 X 等同于取消下载
        if not self._closed:
            self.cancelled = True
            self._cancel_event.set()
            self._apply_hide()
        super().closeEvent(event)
