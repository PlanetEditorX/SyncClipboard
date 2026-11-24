"""客户端管理窗口：服务端登记了哪些设备、各自收过多少次内容、可随时移除。

数据全在服务端进程里（`config/client_ip.json`），这里只做两件事：读
`GET /clients` 画表、点「移除」时发 `POST /clients/delete`。窗口本身不缓存
任何东西 —— 关掉服务端就如实显示读取失败，不拿上一轮的数字糊弄人。

为什么不做成主面板的一块：这台机器可能只跑客户端、连的是别人家的服务端，
名单要按「当前连的服务器」查；独立窗口还能一边开着一边翻别的地方，跟下载
记录窗口的交互习惯保持一致。

刷新间隔取 3 秒而不是 1 秒：`probe=1` 会从服务端对每台设备发一次 /ping
（结果在服务端缓存 5 秒），1 秒一刷等于让局域网一直在被探测，而设备上下线
本来也不是秒级的事情。
"""

from datetime import datetime, timedelta

import requests
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor
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

REFRESH_INTERVAL_MS = 3000
# 服务端要等 /ping 回来（1.2 秒超时）才响应，读超时得比它宽一点
REQUEST_TIMEOUT = (2, 6)

HEADERS = (
    "客户端",
    "系统",
    "IP",
    "推送端口",
    "推送次数",
    "成功",
    "推送率",
    "上次推送",
    "上次连接",
    "状态",
    "操作",
)
NAME_COLUMN = HEADERS.index("客户端")
RATE_COLUMN = HEADERS.index("推送率")
STATUS_COLUMN = HEADERS.index("状态")
ACTION_COLUMN = HEADERS.index("操作")
# 「操作」列放两个按钮：移除（暂时清出派发列表）+ 黑名单 / 解除（持久拦截）
ACTION_COLUMN_WIDTH = 192
ACTION_BUTTON_WIDTH = 84
# 行高必须给按钮让位。QTableWidget 不会因为 setCellWidget 把行撑高，默认行高
# 只有 30 —— 而按钮本身就要 30，加上下留白（3+3）一共 36，多出来的部分直接被
# 单元格裁掉，表现就是「移除」按钮只剩上半截、下边框不见。所以行高按「按钮
# 高度 + 上下留白」算，不再用默认值。
ACTION_BUTTON_HEIGHT = 30
ACTION_ROW_PADDING = 5
ACTION_ROW_HEIGHT = ACTION_BUTTON_HEIGHT + ACTION_ROW_PADDING * 2

EMPTY = "—"


def human_time(value, now=None):
    """把 `YYYY-MM-DD HH:MM:SS` 写成「今天 15:10」/「昨天 09:02」/「11-20 09:02」。

    面板上看的是「多久以前」，完整日期只在悬停时给 —— 一列里塞
    `2026-11-24 15:10:33` 会让整张表挤在一起，还看不出新鲜度。
    """
    if not value:
        return EMPTY
    try:
        stamp = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return str(value)
    now = now or datetime.now()
    if stamp.date() == now.date():
        return f"今天 {stamp.strftime('%H:%M')}"
    if stamp.date() == (now - timedelta(days=1)).date():
        return f"昨天 {stamp.strftime('%H:%M')}"
    if stamp.year == now.year:
        return stamp.strftime("%m-%d %H:%M")
    return stamp.strftime("%Y-%m-%d")


def idle_text(seconds):
    """距上次活跃多久：刚连上的设备显示「刚刚」。"""
    if seconds is None:
        return ""
    seconds = int(seconds)
    if seconds < 60:
        return "刚刚"
    if seconds < 3600:
        return f"{seconds // 60} 分钟前"
    if seconds < 86400:
        return f"{seconds // 3600} 小时前"
    return f"{seconds // 86400} 天前"


def rate_text(row):
    """推送率：一次都没推过时给「—」，不要编一个 0% 出来。"""
    rate = row.get("push_rate")
    if rate is None:
        return EMPTY
    return f"{rate:g}%"


def status_text(row):
    """在线 / 离线 / 探不出来。

    `None` 表示服务端没法主动探测这台设备：iOS 没有推送端口，别的系统
    也可能是还没上报端口。这类设备只能靠它自己来请求才拿到内容，所以一律
    写「轮询」—— 比「未知」准确，也不会让人以为出了什么错。
    """
    online = row.get("online")
    if online is True:
        return "在线"
    if online is False:
        return "离线"
    return "轮询"


class ClientsDialog(QDialog):
    """非模态窗口：打开后每 3 秒拉一次名单，关闭即停止轮询。"""

    def __init__(self, parent=None, host="127.0.0.1", port=8000, key="", local_name=""):
        super().__init__(parent)
        self.setWindowTitle("客户端管理")
        self.resize(1120, 560)
        self.setModal(False)

        self._host = host
        self._port = port
        self._key = key
        # 「本机」是看这个窗口的这台电脑，不是服务端自己。服务端下发的
        # is_server 只能标「服务器」，否则在别的电脑上打开会把服务器误认成
        # 本机（见 _client_role）。
        self._local_name = str(local_name or "").strip()
        self._paused = False
        # 行身份 = (名字, IP)：身份不变就只改文字，不重建行，免得正在点的
        # 「移除」按钮被每秒的刷新销毁掉（同下载记录窗口的处理）
        self._row_keys = []
        self._row_data = []
        self._row_buttons = []
        self._block_buttons = []

        palette = getattr(parent, "_palette", None) or {}
        self._muted = palette.get("muted", "#64748B")
        self._danger = palette.get("danger", "#EF4444")
        self._success = palette.get("success", "#22C55E")
        self._warning = palette.get("warning", "#F59E0B")

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

        self.table = QTableWidget(0, len(HEADERS))
        self.table.setHorizontalHeaderLabels(list(HEADERS))
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setAlternatingRowColors(True)
        # 关掉自动换行：某台设备的系统标签多几个字就把行撑高，整张表会跳
        self.table.setWordWrap(False)
        self.table.verticalHeader().setVisible(False)
        # 行高给「操作」列的按钮留够位置，否则按钮底部会被行边界裁掉
        self.table.verticalHeader().setDefaultSectionSize(ACTION_ROW_HEIGHT)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeToContents)
        # 客户端名最长，让它吃掉多余宽度；「操作」固定宽度，别跟着按钮文字量
        header.setSectionResizeMode(NAME_COLUMN, QHeaderView.Stretch)
        header.setSectionResizeMode(ACTION_COLUMN, QHeaderView.Fixed)
        self.table.setColumnWidth(ACTION_COLUMN, ACTION_COLUMN_WIDTH)
        lay.addWidget(self.table, 1)

        hint = QLabel(
            "「移除」只从派发列表删掉这条记录：设备下次请求会自动回来、又开始收推送。"
            "要真正拦住它请点「黑名单」。"
        )
        hint.setObjectName("Hint")
        hint.setWordWrap(True)
        lay.addWidget(hint)

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

        self._clear_button = QPushButton("清空列表")
        self._clear_button.setCursor(Qt.PointingHandCursor)
        self._clear_button.setMinimumHeight(32)
        self._clear_button.clicked.connect(self._on_clear_all)

        close_button = QPushButton("关闭")
        close_button.setCursor(Qt.PointingHandCursor)
        close_button.setMinimumHeight(32)
        close_button.clicked.connect(self.close)

        buttons.addWidget(self._pause_button)
        buttons.addWidget(self._refresh_button)
        buttons.addStretch(1)
        buttons.addWidget(self._clear_button)
        buttons.addWidget(close_button)
        lay.addLayout(buttons)

    # ---------------------------------------------------------------- 交互

    def set_endpoint(self, host=None, port=None, key=None, local_name=None):
        """配置里改了服务器地址 / 端口 / 密钥时更新目标，下一次刷新即生效。"""
        if host is not None:
            self._host = host
        if port is not None:
            self._port = port
        if key is not None:
            self._key = key
        if local_name is not None:
            self._local_name = str(local_name or "").strip()

    def _on_pause_toggled(self, checked):
        self._paused = bool(checked)
        self._pause_button.setText("继续刷新" if self._paused else "暂停刷新")
        if not self._paused:
            self.refresh_now()

    def closeEvent(self, event):  # noqa: N802 - Qt 规定的方法名
        self._timer.stop()
        super().closeEvent(event)

    def showEvent(self, event):  # noqa: N802
        if not self._paused and not self._timer.isActive():
            self._timer.start()
        self.refresh_now()
        super().showEvent(event)

    # ---------------------------------------------------------------- 数据

    def _base_url(self):
        return f"http://{self._host}:{self._port}"

    def _fetch(self):
        try:
            response = requests.get(
                f"{self._base_url()}/clients",
                params={"probe": 1},
                headers={"key": str(self._key)},
                timeout=REQUEST_TIMEOUT,
            )
            response.raise_for_status()
            return response.json(), None
        except Exception as exc:  # noqa: BLE001 - 原因要原样显示给用户
            return None, str(exc)

    def refresh_now(self):
        if self._paused:
            return
        payload, error = self._fetch()
        if error:
            self._status.setText(f"无法读取客户端管理数据（服务端未运行？）：{error}")
            self._status.setStyleSheet(f"color: {self._danger};")
            return
        self._render(payload)

    def _render(self, payload):
        rows = payload.get("clients") or []
        summary = payload.get("summary") or {}
        self._status.setText(self._summary_text(payload, summary, rows))
        self._status.setStyleSheet(f"color: {self._muted};")
        self._fill(rows)
        self._clear_button.setEnabled(bool(rows))

    def _summary_text(self, payload, summary, rows):
        if not rows:
            return (
                "还没有客户端登记：任何带密钥的请求都会自动登记设备。"
                f"　|　服务器 {payload.get('server_name') or self._host}"
            )
        parts = [
            f"共 {summary.get('total', len(rows))} 台",
            f"在线 {summary.get('online', 0)}",
            f"离线 {summary.get('offline', 0)}",
        ]
        if summary.get("unknown"):
            parts.append(f"未知 {summary['unknown']}")
        if summary.get("blacklisted"):
            parts.append(f"黑名单 {summary['blacklisted']}")
        total = summary.get("push_total", 0)
        ok = summary.get("push_ok", 0)
        if total:
            rate = summary.get("push_rate")
            parts.append(f"累计推送 {total} 次 / 成功 {ok} 次（成功率 {rate:g}%）")
        else:
            parts.append("还没有推送记录")
        parts.append(f"每 {REFRESH_INTERVAL_MS // 1000} 秒自动刷新")
        return "　|　".join(parts)

    @staticmethod
    def _action_button(text, tooltip, callback):
        """操作列的小按钮：只覆盖 padding，别用全局那套会把它压扁的 8px。"""
        button = QPushButton(text)
        button.setCursor(Qt.PointingHandCursor)
        button.setStyleSheet("padding: 4px 8px;")
        # 先让样式生效，sizeHint 才会把上面的 padding 算进去
        button.ensurePolished()
        # 高度按字体实际需要给：DPI 或主题字体变大时不会把字挤扁，
        # 行高也跟着一起长，按钮不会被裁
        button.setFixedHeight(
            max(ACTION_BUTTON_HEIGHT, button.sizeHint().height())
        )
        button.setFixedWidth(ACTION_BUTTON_WIDTH)
        button.setToolTip(tooltip)
        button.clicked.connect(callback)
        return button

    def _fill(self, rows):
        keys = [(str(row.get("local_name", "")), str(row.get("ip", ""))) for row in rows]
        if keys == self._row_keys and len(keys) == self.table.rowCount():
            self._update_rows(rows)
            return

        scroll = self._scroll_state()
        self.table.setRowCount(len(rows))
        self._row_keys = list(keys)
        self._row_data = []
        self._row_buttons = []
        self._block_buttons = []
        row_height = ACTION_ROW_HEIGHT
        for index in range(len(rows)):
            button = self._action_button(
                "移除",
                "从派发列表移除；设备下次请求会自动回来",
                lambda _checked=False, row=index: self._on_remove_row(row),
            )
            block_button = self._action_button(
                "黑名单",
                "拦住这台设备：之后它注册、同步都不再收",
                lambda _checked=False, row=index: self._on_blacklist_row(row),
            )
            # 套一层留白，不让按钮贴着格线；两个按钮并排，中间留一点缝
            holder = QWidget()
            holder_layout = QHBoxLayout(holder)
            holder_layout.setContentsMargins(6, ACTION_ROW_PADDING, 6, ACTION_ROW_PADDING)
            holder_layout.setSpacing(6)
            holder_layout.addWidget(button)
            holder_layout.addWidget(block_button)
            self.table.setCellWidget(index, ACTION_COLUMN, holder)
            self._row_buttons.append(button)
            self._block_buttons.append(block_button)
            row_height = max(
                row_height, button.height() + ACTION_ROW_PADDING * 2
            )
            for column in range(ACTION_COLUMN):
                self.table.setItem(index, column, QTableWidgetItem(""))
            self._row_data.append({})
        # 逐行落到实际行高：只设 defaultSectionSize 的话，已有行不一定会跟着变
        self.table.verticalHeader().setDefaultSectionSize(row_height)
        for index in range(self.table.rowCount()):
            self.table.setRowHeight(index, row_height)
        self._update_rows(rows)
        self._restore_scroll(scroll)

    def _update_rows(self, rows):
        for index, row in enumerate(rows):
            if index >= len(self._row_data):
                break
            name = str(row.get("local_name", ""))
            os_name = str(row.get("os", ""))
            port = row.get("port") or 0
            os_text = os_name or EMPTY
            values = (
                self._client_role(row, name),
                os_text,
                str(row.get("ip") or EMPTY),
                str(port) if port else EMPTY,
                str(row.get("push_total", 0)),
                str(row.get("push_ok", 0)),
                rate_text(row),
                human_time(row.get("last_push")),
                human_time(row.get("last_alive")),
                "已拉黑" if row.get("blacklisted") else status_text(row),
            )
            for column, text in enumerate(values):
                item = self.table.item(index, column)
                if item is None:
                    self.table.setItem(index, column, QTableWidgetItem(text))
                elif item.text() != text:
                    item.setText(text)
                self._decorate(item, column, row, name)

            # 行复用时也要跟着黑名单状态更新按钮（不能只在建行时设一次）
            blocked = bool(row.get("blacklisted"))
            if index < len(self._row_buttons):
                remove_button = self._row_buttons[index]
                remove_button.setEnabled(not blocked)
                remove_button.setToolTip(
                    "已在黑名单，先解除再操作"
                    if blocked else "从派发列表移除；设备下次请求会自动回来"
                )
            if index < len(self._block_buttons):
                block_button = self._block_buttons[index]
                block_button.setText("解除" if blocked else "黑名单")
                block_button.setToolTip(
                    "解除黑名单：它下次请求就会重新登记并收到推送"
                    if blocked else "加入黑名单：之后注册、同步都不再收"
                )
            self._row_data[index] = row

    def _client_role(self, row, name):
        """名字后挂角色：本机 / 服务器（两边都占就都标出来）。

        「本机」只能由看窗口的这端判断，服务端下发的 `is_server` 说的是
        「服务器那台机器」，两件事不能混为一谈。
        """
        roles = []
        if self._local_name and name and name.casefold() == self._local_name.casefold():
            roles.append("本机")
        if row.get("is_server"):
            roles.append("服务器")
        return f"{name}（{'·'.join(roles)}）" if roles else name

    def _decorate(self, item, column, row, name):
        """给单元格上色 / 加悬停说明；颜色只决定「看着像不像有问题」。"""
        if item is None:
            return
        if column == NAME_COLUMN:
            item.setToolTip(self._tooltip(row, name))
        elif column == RATE_COLUMN:
            # 刻意不按阈值上色：复制是高频操作，同步要等那台设备醒来才发生，
            # 用久了几乎必然全红 —— 那是使用节奏，不是故障。健康度看「状态」
            # 和「上次连接」这两列，推送率只当参考数字。
            if row.get("push_rate") is None:
                item.setForeground(QColor(self._muted))
            item.setToolTip(
                "尝试推送 / 真正送达。设备关机、休眠也算一次尝试，"
                "数字低通常只是它当时没开着，不代表同步坏了。"
            )
        elif column == STATUS_COLUMN:
            if row.get("blacklisted"):
                color = self._danger
            else:
                online = row.get("online")
                color = self._success if online is True else (
                    self._danger if online is False else self._muted
                )
            item.setForeground(QColor(color))

    @staticmethod
    def _tooltip(row, name):
        lines = [f"{name} · {row.get('os') or 'Unknown'}"]
        if row.get("first_seen"):
            lines.append(f"首次登记：{row['first_seen']}")
        if row.get("last_seen"):
            lines.append(f"上次请求：{row['last_seen']}")
        if row.get("last_delivery"):
            lines.append(f"上次送达：{row['last_delivery']}")
        if row.get("last_push"):
            result = "送达" if row.get("last_push_ok") else "未送达"
            lines.append(f"上次推送：{row['last_push']}（{result}）")
        if row.get("push_fail"):
            lines.append(f"累计未送达：{row['push_fail']} 次")
        if row.get("pull_ok"):
            lines.append(f"轮询取走：{row['pull_ok']} 次")
        idle = idle_text(row.get("idle_seconds"))
        if idle:
            lines.append(f"活跃于：{idle}")
        return "\n".join(lines)

    def action_button(self, row):
        """第 row 行的「移除」按钮；没有该行时返回 None（测试与调试用）。"""
        if 0 <= row < len(self._row_buttons):
            return self._row_buttons[row]
        return None

    def blacklist_button(self, row):
        """第 row 行的「黑名单 / 解除」按钮。"""
        if 0 <= row < len(self._block_buttons):
            return self._block_buttons[row]
        return None

    # ---------------------------------------------------------------- 移除

    def _on_remove_row(self, index):
        if not 0 <= index < len(self._row_data):
            return
        row = self._row_data[index]
        name = str(row.get("local_name") or "")
        if not name:
            return
        online = row.get("online")
        if online is True:
            tail = (
                "\n\n这台设备当前在线：删掉之后它下一次请求（通常几秒内）就会"
                "重新登记，等于只清空了统计。"
            )
        else:
            tail = "\n\n之后它再连上来仍会被自动登记。"

        answer = QMessageBox.question(
            self,
            "移除客户端",
            f"从名单中移除「{name}」？\n\n会同时删除它的推送次数 / 成功率记录。{tail}",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return

        ok, error, removed = self._post_delete({"local_name": name})
        if not ok:
            QMessageBox.warning(self, "移除失败", f"无法通知服务端：{error}")
            return
        if not removed:
            QMessageBox.information(self, "没有移除", "服务端里已经没有这条记录。")
        self.refresh_now()

    def _on_blacklist_row(self, index):
        if not 0 <= index < len(self._row_data):
            return
        row = self._row_data[index]
        name = str(row.get("local_name") or "")
        if not name:
            return
        blocked = bool(row.get("blacklisted"))
        if blocked:
            title = "解除黑名单"
            message = (
                f"解除「{name}」的黑名单？\n\n"
                "解除后它下次请求就会重新登记，又开始收推送。"
            )
        else:
            title = "加入黑名单"
            message = (
                f"把「{name}」加入黑名单？\n\n"
                "之后它的注册请求会被拒绝、同步不再发给它；面板上会保留一条"
                "「已拉黑」记录，随时可以解除。"
            )
        answer = QMessageBox.question(
            self, title, message, QMessageBox.Yes | QMessageBox.No, QMessageBox.No
        )
        if answer != QMessageBox.Yes:
            return

        ok, error = self._post_blacklist(
            {"local_name": name, "block": not blocked}
        )
        if not ok:
            QMessageBox.warning(self, f"{title}失败", f"无法通知服务端：{error}")
            return
        self.refresh_now()

    def _on_clear_all(self):
        count = len(self._row_data)
        if not count:
            return
        answer = QMessageBox.question(
            self,
            "清空名单",
            f"清空全部 {count} 台客户端的登记与推送统计？\n\n"
            "还在运行的设备会陆续重新登记，统计从 0 开始。",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        ok, error, _removed = self._post_delete({"all": True})
        if not ok:
            QMessageBox.warning(self, "清空失败", f"无法通知服务端：{error}")
            return
        self.refresh_now()

    def _post_delete(self, payload):
        try:
            response = requests.post(
                f"{self._base_url()}/clients/delete",
                json=payload,
                headers={"key": str(self._key)},
                timeout=REQUEST_TIMEOUT,
            )
            response.raise_for_status()
            data = response.json()
            return True, "", int(data.get("removed", 0))
        except Exception as exc:  # noqa: BLE001 - 原因要原样告诉用户
            return False, str(exc), 0

    def _post_blacklist(self, payload):
        try:
            response = requests.post(
                f"{self._base_url()}/clients/blacklist",
                json=payload,
                headers={"key": str(self._key)},
                timeout=REQUEST_TIMEOUT,
            )
            response.raise_for_status()
            return True, ""
        except Exception as exc:  # noqa: BLE001 - 原因要原样告诉用户
            return False, str(exc)

    # ---------------------------------------------------------------- 滚动

    def _scroll_state(self):
        bar = self.table.verticalScrollBar()
        return bar.value(), bar.maximum(), self.table.currentRow()

    def _restore_scroll(self, state):
        """重建表格后把滚动位置和当前行放回去，别每次都跳回顶部。"""
        value, maximum, current_row = state
        bar = self.table.verticalScrollBar()
        if maximum and value >= maximum:
            bar.setValue(bar.maximum())
        else:
            bar.setValue(min(value, bar.maximum()))
        if 0 <= current_row < self.table.rowCount():
            self.table.selectRow(current_row)
