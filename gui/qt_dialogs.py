"""Qt 原生对话框封装：消息框与目录选择。

common 层不依赖图形库，所以这里把 Qt 实现注入 ui_bridge，
`show_message()` 才能在后台线程调用时安全地弹出窗口。
"""

import logging
from pathlib import Path

from PySide6.QtWidgets import QFileDialog, QMessageBox

from common import ui_bridge
from gui.qt_theme import app_icon

logger = logging.getLogger("gui")


def _parent():
    window = ui_bridge.get_main_window()
    # 点 Windows 通知时主窗口往往收在托盘里。拿一个不可见的窗口当模态框
    # parent，弹窗可能跟着隐藏或落到别的屏幕后面，用户关不掉、任务也卡住。
    return window if window is not None and window.isVisible() else None


def info_message(title, message):
    """信息提示框（主线程）。"""
    box = QMessageBox(_parent())
    box.setWindowTitle(title)
    box.setText(message)
    box.setIcon(QMessageBox.Information)
    box.setStandardButtons(QMessageBox.Ok)
    box.setWindowIcon(app_icon())
    box.exec()


def ask_yes_no(title, message):
    """确认框，返回 True 表示用户点了「是」。"""
    box = QMessageBox(_parent())
    box.setWindowTitle(title)
    box.setText(message)
    box.setIcon(QMessageBox.Question)
    box.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
    box.setDefaultButton(QMessageBox.No)
    box.setWindowIcon(app_icon())
    return box.exec() == QMessageBox.Yes


def choose_directory(current=None, title="选择文件夹"):
    """目录选择框，返回选中的绝对路径；取消时返回空字符串。"""
    start = str(current) if current else ""
    if start and not Path(start).is_dir():
        start = ""
    selected = QFileDialog.getExistingDirectory(_parent(), title, start)
    return selected or ""


def ask_save_file(current_dir=None, filename="", title="保存文件"):
    """保存文件对话框，返回完整路径；取消时返回空字符串。"""
    start_dir = str(current_dir) if current_dir else ""
    if start_dir and not Path(start_dir).is_dir():
        start_dir = ""
    initial = str(Path(start_dir) / filename) if start_dir and filename else ""
    selected, _ = QFileDialog.getSaveFileName(
        _parent(), title, initial, "所有文件 (*.*)"
    )
    return selected or ""


def register():
    """把消息框实现注入 ui_bridge。"""
    ui_bridge.set_message_handler(info_message)
