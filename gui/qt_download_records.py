"""下载记录窗口：每秒轮询本机文件服务，实时显示进度与事件。

原来是一次性快照 + 纯文本：想看进度得反复点按钮重新打开。这里改成
一个常驻窗口，打开后自己按秒刷新，两块内容分开看：
- 传输进度：每个「共享 + 设备」一行，显示已发出字节 / 总量 / 文件数；
- 最近事件：开始下载、取清单、完成、中断、被拒绝、次数用尽、取消共享。

刷新时保留滚动位置和当前行 —— 每秒重建表格却不跳回顶部，才谈得上"实时"。
数据只在客户端内存里，这里不做任何缓存：每次都是现场问客户端要，客户端
没起来就如实显示读取失败，不展示过期数据。

进度表最后一列是「操作」：传输中的行可以就地取消（停发 + 撤销该设备的
访问），已完成的行可以撤销共享。取消请求发给本机文件服务，由发送端执行。
"""

import time

import requests
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
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
    "manual_cancel": "手动取消",
}

# 进度表列序：前 6 列是数据，最后一列放「取消」按钮
PROGRESS_HEADERS = ("客户端", "共享", "已发出 / 总量", "进度", "文件数", "状态", "操作")
# 共享名最长，让它吃掉多余宽度；「状态」「操作」挨在一起收在最右侧
SHARE_COLUMN = PROGRESS_HEADERS.index("共享")
STATUS_COLUMN = PROGRESS_HEADERS.index("状态")
ACTION_COLUMN = PROGRESS_HEADERS.index("操作")
# 「操作」列给固定宽度：靠 ResizeToContents 会踩到「按钮先建、文字后填」的
# 时序 —— 列宽按空按钮量出来，等填上「取消传输」就被裁掉半个字
ACTION_COLUMN_WIDTH = 112
ACTION_BUTTON_WIDTH = 92
# 行高要按按钮让位：QTableWidget 不会因为 setCellWidget 把行撑高，默认行高 30
# 比「按钮 + 上下留白」矮，按钮底部就会被单元格裁掉一条边（看着像按钮缺了下
# 边框）。这里显式算行高，跟客户端名单窗口保持同样的 40。
ACTION_BUTTON_HEIGHT = 30
ACTION_ROW_PADDING = 5
ACTION_ROW_HEIGHT = ACTION_BUTTON_HEIGHT + ACTION_ROW_PADDING * 2


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
        # 进度表按「共享 + 设备」维护行身份：身份不变就原地改文字，不重建行，
        # 否则每秒重建会把正在点的「取消」按钮销毁掉，点击就丢了
        self._progress_keys = []
        self._progress_rows = []
        self._progress_buttons = []

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

        self.progress_table = self._make_table(PROGRESS_HEADERS, SHARE_COLUMN)
        # 按钮列固定宽度，避免列宽跟着按钮文字量出来又被裁
        self.progress_table.horizontalHeader().setSectionResizeMode(
            ACTION_COLUMN, QHeaderView.Fixed
        )
        self.progress_table.setColumnWidth(ACTION_COLUMN, ACTION_COLUMN_WIDTH)
        # 行高给「操作」列的按钮留够位置，否则按钮底部会被行边界裁掉
        self.progress_table.verticalHeader().setDefaultSectionSize(ACTION_ROW_HEIGHT)
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
    def _make_table(headers, stretch_column=None):
        table = QTableWidget(0, len(headers))
        table.setHorizontalHeaderLabels(list(headers))
        table.setEditTriggers(QTableWidget.NoEditTriggers)
        table.setSelectionBehavior(QTableWidget.SelectRows)
        table.setAlternatingRowColors(True)
        table.verticalHeader().setVisible(False)
        header = table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeToContents)
        # 默认让最后一列（说明 / 状态）吃掉剩余宽度，避免长文本被截成省略号；
        # 进度表最后一列是按钮，撑宽了按钮会变成一条，所以改由「状态」列吃掉
        target = len(headers) - 1 if stretch_column is None else stretch_column
        header.setSectionResizeMode(target, QHeaderView.Stretch)
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
        keys = [
            (str(session.get("file_id", "")), str(session.get("client", "")))
            for session in sessions
        ]
        # 行身份没变就只改文字：按钮是 QWidget，重建会把正在进行的点击吞掉
        if keys == self._progress_keys:
            self._update_progress(sessions)
        else:
            self._rebuild_progress(sessions, keys)
        self._restore_scroll(table, scroll)

    def _rebuild_progress(self, sessions, keys):
        table = self.progress_table
        table.setRowCount(len(sessions))
        self._progress_keys = list(keys)
        self._progress_rows = []
        self._progress_buttons = []
        row_height = ACTION_ROW_HEIGHT
        for row, session in enumerate(sessions):
            for column in range(ACTION_COLUMN):
                table.setItem(row, column, QTableWidgetItem(""))
            # 文字一开始就填好：先建空按钮再补文字，列宽会按空按钮量
            button = QPushButton(
                "取消共享" if session.get("state") == "completed" else "取消传输"
            )
            button.setCursor(Qt.PointingHandCursor)
            # 只覆盖 padding：全局 QSS 那套 8px 上下内边距塞进 30px 高的按钮里，
            # 内容区只剩几个像素，文字会被压扁
            button.setStyleSheet("padding: 4px 8px;")
            # 先让样式生效，sizeHint 才会把上面的 padding 算进去
            button.ensurePolished()
            button_height = max(ACTION_BUTTON_HEIGHT, button.sizeHint().height())
            button.setFixedHeight(button_height)
            button.setFixedWidth(ACTION_BUTTON_WIDTH)
            button.clicked.connect(
                lambda _checked=False, index=row: self._on_cancel_row(index)
            )
            # 套一层留白：直接塞进单元格的按钮会贴着格线，看起来像又一个格子
            holder = QWidget()
            holder_layout = QHBoxLayout(holder)
            holder_layout.setContentsMargins(6, ACTION_ROW_PADDING, 6, ACTION_ROW_PADDING)
            holder_layout.addWidget(button)
            table.setCellWidget(row, ACTION_COLUMN, holder)
            self._progress_buttons.append(button)
            row_height = max(row_height, button_height + ACTION_ROW_PADDING * 2)
            self._progress_rows.append({})
        # 逐行落到实际行高：只设 defaultSectionSize 的话，已有行不一定会跟着变
        table.verticalHeader().setDefaultSectionSize(row_height)
        for index in range(table.rowCount()):
            table.setRowHeight(index, row_height)
        self._update_progress(sessions)

    def action_button(self, row):
        """第 row 行的「取消」按钮；没有该行时返回 None（测试与调试用）。"""
        if 0 <= row < len(self._progress_buttons):
            return self._progress_buttons[row]
        return None

    def _update_progress(self, sessions):
        """按行刷新文字与按钮文案；行数/顺序不变时走这里，不碰控件本身。"""
        table = self.progress_table
        for row, session in enumerate(sessions):
            if row >= len(self._progress_rows):
                break
            completed = session.get("state") == "completed"
            values = (
                str(session.get("client", "")),
                str(session.get("name", "")),
                f"{human_bytes(session.get('sent'))} / {human_bytes(session.get('total'))}",
                f"{session.get('percent', 0)}%",
                f"{session.get('parts', 0)}/{session.get('files', 0)}",
                "已完成" if completed else "进行中",
            )
            for column, text in enumerate(values):
                item = table.item(row, column)
                if item is None:
                    table.setItem(row, column, QTableWidgetItem(text))
                elif item.text() != text:
                    item.setText(text)
            self._progress_rows[row] = {
                "file_id": str(session.get("file_id", "")),
                "client": str(session.get("client", "")),
                "name": str(session.get("name", "")),
                "completed": completed,
            }
            button = self.action_button(row)
            if button is not None:
                label = "取消共享" if completed else "取消传输"
                if button.text() != label:
                    button.setText(label)
                # 没有 file_id 的旧客户端记录点不了，别给个假按钮
                button.setEnabled(bool(session.get("file_id")))

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

    # ---------------------------------------------------------------- 取消操作

    def _on_cancel_row(self, row):
        """「操作」列按钮：先确认，再让发送端停发并撤销访问。"""
        if not 0 <= row < len(self._progress_rows):
            return
        info = self._progress_rows[row]
        file_id = info.get("file_id") or ""
        client_id = info.get("client") or ""
        name = info.get("name") or file_id or "该共享"
        if not file_id:
            return

        if info.get("completed"):
            question = (
                f"取消「{name}」的共享？\n\n"
                "已完成的下载记录会保留；之后设备再拿旧链接会 404，"
                "需要重新复制一次。"
            )
        else:
            question = (
                f"中断「{name}」向 {client_id or '该设备'} 的传输？\n\n"
                "发送端会立刻停止继续发送并撤销该设备的访问，"
                "对方拿到的文件会不完整。"
            )
        answer = QMessageBox.question(
            self, "取消共享", question,
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return

        ok, error = self._post_cancel(file_id, client_id)
        if not ok:
            QMessageBox.warning(
                self, "取消失败", f"无法通知客户端执行取消：{error}"
            )
            return
        self.refresh_now()

    def _post_cancel(self, file_id, client_id):
        url = f"http://{self._host}:{self._port}/downloads/cancel"
        try:
            response = requests.post(
                url,
                json={"file_id": file_id, "client": client_id},
                headers={"key": str(self._key)},
                timeout=REQUEST_TIMEOUT,
            )
            response.raise_for_status()
            return True, ""
        except Exception as exc:  # noqa: BLE001 - 原因要原样告诉用户
            return False, str(exc)

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
