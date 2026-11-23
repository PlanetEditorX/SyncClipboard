"""下载记录窗口：每秒轮询本机文件服务，实时显示进度与事件。

原来是一次性快照 + 纯文本：想看进度得反复点按钮重新打开。这里改成
一个常驻窗口，打开后自己按秒刷新，两块内容分开看：
- 传输进度：每个「共享 + 设备」一行，显示已发出字节 / 总量 / 文件数；
- 最近事件：开始下载、取清单、完成、中断、被拒绝、次数用尽、取消共享。

刷新时保留滚动位置和当前行 —— 每秒重建表格却不跳回顶部，才谈得上"实时"。
数据只在客户端内存里，这里不做任何缓存：每次都是现场问客户端要，客户端
没起来就如实显示读取失败，不展示过期数据。
"""

import time

import requests
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

# 轮询间隔：本机请求，1 秒足够跟手，又不会把客户端日志刷爆
REFRESH_INTERVAL_MS = 1000
# 连接本机文件服务要快失败：客户端没起来时应立刻提示，而不是卡住界面
REQUEST_TIMEOUT = (1, 2)

EVENT_LABELS = {
    "started": "开始下载",
    "manifest": "取清单",
    "completed": "下载完成",
    "interrupted": "传输中断",
    "blocked": "被拒绝",
    "exhausted": "次数用尽",
    "cancelled": "取消共享",
}


def human_bytes(value):
    """把字节数写成 12.3 MB 这种；未知或 0 保持 0 B。"""
    number = float(value or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if number < 1024 or unit == "TB":
            return f"{number:.0f} {unit}" if unit in ("B", "KB") else f"{number:.1f} {unit}"
        number /= 1024
    return f"{number:.1f} TB"


class DownloadRecordsDialog(QDialog):
    """非模态窗口：打开后每秒刷新，关闭即停止轮询。"""

    def __init__(self, parent=None, host="127.0.0.1", port=8899, key=""):
        super().__init__(parent)
        self.setWindowTitle("下载记录")
        self.resize(1080, 620)
        self.setModal(False)

        self._host = host
        self._port = port
        self._key = key
        self._paused = False

        palette = getattr(parent, "_palette", None) or {}
        self._muted_color = palette.get("muted", "#6b7280")
        self._danger_color = palette.get("danger", "#c0392b")

        self._build_ui()

        self._timer = QTimer(self)
        self._timer.setInterval(REFRESH_INTERVAL_MS)
        self._timer.timeout.connect(self.refresh_now)
        self._timer.start()

    # ---------------------------------------------------------------- 界面

    def _build_ui(self):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(10)

        self._status = QLabel("正在读取…")
        self._status.setObjectName("Hint")
        lay.addWidget(self._status)

        self.progress_table = self._make_table(
            ("客户端", "共享", "已发出 / 总量", "进度", "文件数", "状态")
        )
        self.event_table = self._make_table(
            ("时间", "共享", "类型", "客户端", "事件", "已完成次数", "本次字节", "说明")
        )

        lay.addWidget(self._section_label("传输进度（按设备累计实际发出的字节）"))
        lay.addWidget(self.progress_table, 2)
        lay.addWidget(self._section_label("最近事件"))
        lay.addWidget(self.event_table, 3)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        self._pause_button = QPushButton("暂停刷新")
        self._pause_button.setCheckable(True)
        self._pause_button.setCursor(Qt.PointingHandCursor)
        self._pause_button.setMinimumHeight(32)
        self._pause_button.toggled.connect(self._on_pause_toggled)

        self._refresh_button = QPushButton("立即刷新")
        self._refresh_button.setCursor(Qt.PointingHandCursor)
        self._refresh_button.setMinimumHeight(32)
        self._refresh_button.clicked.connect(self.refresh_now)

        close_button = QPushButton("关闭")
        close_button.setCursor(Qt.PointingHandCursor)
        close_button.setMinimumHeight(32)
        close_button.clicked.connect(self.close)

        buttons.addWidget(self._pause_button)
        buttons.addWidget(self._refresh_button)
        buttons.addStretch(1)
        buttons.addWidget(close_button)
        lay.addLayout(buttons)

    @staticmethod
    def _section_label(text):
        label = QLabel(text)
        label.setObjectName("FieldLabel")
        return label

    @staticmethod
    def _make_table(headers):
        table = QTableWidget(0, len(headers))
        table.setHorizontalHeaderLabels(list(headers))
        table.setEditTriggers(QTableWidget.NoEditTriggers)
        table.setSelectionBehavior(QTableWidget.SelectRows)
        table.setAlternatingRowColors(True)
        table.verticalHeader().setVisible(False)
        header = table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeToContents)
        # 最后一列（说明 / 状态）吃掉剩余宽度，避免长文本被截成省略号
        header.setSectionResizeMode(len(headers) - 1, QHeaderView.Stretch)
        return table

    # ---------------------------------------------------------------- 交互

    def set_endpoint(self, host=None, port=None, key=None):
        """配置改了端口或密钥时更新目标，下一次刷新就用新值。"""
        if host is not None:
            self._host = host
        if port is not None:
            self._port = port
        if key is not None:
            self._key = key

    def _on_pause_toggled(self, checked):
        self._paused = bool(checked)
        self._pause_button.setText("继续刷新" if self._paused else "暂停刷新")
        if not self._paused:
            self.refresh_now()

    def closeEvent(self, event):  # noqa: N802 - Qt 规定的方法名
        self._timer.stop()
        super().closeEvent(event)

    def showEvent(self, event):  # noqa: N802
        # 窗口再次打开时恢复轮询并立刻拉一次，别先显示上一轮的旧数据
        if not self._paused and not self._timer.isActive():
            self._timer.start()
        self.refresh_now()
        super().showEvent(event)

    # ---------------------------------------------------------------- 数据

    def _fetch(self):
        url = f"http://{self._host}:{self._port}/downloads"
        try:
            response = requests.get(
                url, headers={"key": str(self._key)}, timeout=REQUEST_TIMEOUT
            )
            response.raise_for_status()
            return response.json(), None
        except Exception as exc:  # noqa: BLE001 - 要把原因原样显示给用户
            return None, str(exc)

    def refresh_now(self):
        if self._paused:
            return
        payload, error = self._fetch()
        if error:
            self._status.setText(f"无法读取下载记录（客户端未运行？）：{error}")
            self._status.setStyleSheet(f"color: {self._danger_color};")
            return
        self._render(payload)

    def _render(self, payload):
        limit = payload.get("limit", 0)
        records = payload.get("records") or []
        sessions = payload.get("sessions") or []
        sent_total = human_bytes(payload.get("bytes_sent_total", 0))
        completed = payload.get("completed_downloads", 0)

        policy = "不限制（存活期内所有设备可获取）" if not limit else f"{limit} 次完整下载"
        self._status.setText(
            f"当前策略：{policy}　|　本次运行累计发出 {sent_total}"
            f"　|　已完成 {completed} 次　|　每 {REFRESH_INTERVAL_MS // 1000} 秒自动刷新"
            f"　|　{time.strftime('%H:%M:%S')}"
        )
        self._status.setStyleSheet(f"color: {self._muted_color};")

        self._fill_progress(sessions)
        self._fill_events(records)

    def _fill_progress(self, sessions):
        table = self.progress_table
        scroll = self._scroll_state(table)

        table.setRowCount(len(sessions))
        for row, session in enumerate(sessions):
            state = "已完成" if session.get("state") == "completed" else "进行中"
            values = (
                str(session.get("client", "")),
                str(session.get("name", "")),
                f"{human_bytes(session.get('sent'))} / {human_bytes(session.get('total'))}",
                f"{session.get('percent', 0)}%",
                f"{session.get('parts', 0)}/{session.get('files', 0)}",
                state,
            )
            for column, text in enumerate(values):
                table.setItem(row, column, QTableWidgetItem(text))

        self._restore_scroll(table, scroll)

    def _fill_events(self, records):
        table = self.event_table
        scroll = self._scroll_state(table)

        table.setRowCount(len(records))
        for row, item in enumerate(records):
            values = (
                str(item.get("time", "")),
                str(item.get("name", "")),
                "目录" if item.get("kind") == "folder" else "文件",
                str(item.get("client", "")),
                EVENT_LABELS.get(item.get("event"), str(item.get("event", ""))),
                str(item.get("count", "")),
                human_bytes(item.get("bytes")),
                str(item.get("detail", "")),
            )
            for column, text in enumerate(values):
                table.setItem(row, column, QTableWidgetItem(text))

        self._restore_scroll(table, scroll)

    @staticmethod
    def _scroll_state(table):
        bar = table.verticalScrollBar()
        return bar.value(), bar.maximum(), table.currentRow()

    @staticmethod
    def _restore_scroll(table, state):
        """重建表格后把滚动位置和当前行放回去。

        原来贴在底部就继续贴底（看最新事件最常用的姿势），否则回到原位置。
        """
        value, maximum, current_row = state
        bar = table.verticalScrollBar()
        if maximum and value >= maximum:
            bar.setValue(bar.maximum())
        else:
            bar.setValue(min(value, bar.maximum()))
        if 0 <= current_row < table.rowCount():
            table.selectRow(current_row)
