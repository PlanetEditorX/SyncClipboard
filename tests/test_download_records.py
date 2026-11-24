import os
import unittest
from unittest.mock import MagicMock, patch


class TestHumanBytes(unittest.TestCase):
    def test_bytes_and_kilobytes_have_no_decimals(self):
        from gui.qt_download_records import human_bytes

        self.assertEqual(human_bytes(0), "0 B")
        self.assertEqual(human_bytes(512), "512 B")
        self.assertEqual(human_bytes(2048), "2 KB")

    def test_larger_units_have_one_decimal(self):
        from gui.qt_download_records import human_bytes

        self.assertEqual(human_bytes(1024 * 1024), "1.0 MB")
        self.assertEqual(human_bytes(int(1.5 * 1024 ** 3)), "1.5 GB")

    def test_none_is_zero(self):
        from gui.qt_download_records import human_bytes

        self.assertEqual(human_bytes(None), "0 B")


class TestDownloadRecordsDialog(unittest.TestCase):
    """真实窗口的接线测试，用 offscreen 平台跑，不需要显示器。"""

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance()
        if cls.app is None:
            try:
                cls.app = QApplication([])
            except Exception as exc:  # noqa: BLE001 - 环境缺少平台插件时跳过
                raise unittest.SkipTest(f"无法创建 QApplication: {exc}")

    def _dialog(self):
        from gui.qt_download_records import DownloadRecordsDialog

        dialog = DownloadRecordsDialog(None, port=8899, key="k")
        # 测试里手动刷新，别让真实的 1 秒定时器掺和进来
        dialog._timer.stop()
        self.addCleanup(dialog.deleteLater)
        return dialog

    @staticmethod
    def _payload():
        return {
            "limit": 2,
            "records": [
                {
                    "time": "2026-11-24 00:00:00",
                    "name": "ROM",
                    "kind": "folder",
                    "client": "Mi 10",
                    "event": "started",
                    "count": 1,
                    "bytes": 1024,
                    "detail": "开始下载",
                }
            ],
            "sessions": [
                {
                    "file_id": "d1",
                    "client": "Mi 10",
                    "name": "ROM",
                    "sent": 512,
                    "total": 1024,
                    "percent": 50,
                    "parts": 1,
                    "files": 2,
                    "state": "running",
                }
            ],
            "bytes_sent_total": 2048,
            "completed_downloads": 1,
        }

    @staticmethod
    def _response(payload):
        response = MagicMock()
        response.json.return_value = payload
        response.raise_for_status.return_value = None
        return response

    def test_refresh_fills_both_tables(self):
        dialog = self._dialog()
        with patch(
            "gui.qt_download_records.requests.get",
            return_value=self._response(self._payload()),
        ):
            dialog.refresh_now()

        self.assertEqual(dialog.progress_table.rowCount(), 1)
        self.assertEqual(dialog.progress_table.item(0, 3).text(), "50%")
        self.assertEqual(dialog.progress_table.item(0, 5).text(), "进行中")
        self.assertEqual(dialog.event_table.rowCount(), 1)
        self.assertEqual(dialog.event_table.item(0, 4).text(), "开始下载")
        self.assertIn("2 次完整下载", dialog._status.text())

    def test_refresh_keeps_selected_row(self):
        dialog = self._dialog()
        payload = self._payload()
        payload["sessions"] = payload["sessions"] * 3
        with patch(
            "gui.qt_download_records.requests.get",
            return_value=self._response(payload),
        ):
            dialog.refresh_now()
            dialog.progress_table.selectRow(1)
            dialog.refresh_now()

        self.assertEqual(dialog.progress_table.currentRow(), 1)

    def test_fetch_error_is_shown_with_reason(self):
        dialog = self._dialog()
        with patch(
            "gui.qt_download_records.requests.get",
            side_effect=RuntimeError("connection refused"),
        ):
            dialog.refresh_now()

        self.assertIn("connection refused", dialog._status.text())

    def test_pause_stops_polling(self):
        dialog = self._dialog()
        dialog._pause_button.setChecked(True)
        with patch("gui.qt_download_records.requests.get") as get:
            dialog.refresh_now()
        get.assert_not_called()

    def test_timer_interval_is_one_second(self):
        dialog = self._dialog()
        self.assertEqual(dialog._timer.interval(), 1000)

    def test_endpoint_can_be_updated_in_place(self):
        dialog = self._dialog()
        dialog.set_endpoint(port=9999, key="new")
        self.assertEqual(dialog._port, 9999)
        self.assertEqual(dialog._key, "new")


class TestCancelFromStatusColumn(unittest.TestCase):
    """状态下方的「操作」列：传输中可取消，已完成可撤销共享。"""

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance()
        if cls.app is None:
            try:
                cls.app = QApplication([])
            except Exception as exc:  # noqa: BLE001
                raise unittest.SkipTest(f"无法创建 QApplication: {exc}")

    def setUp(self):
        from gui.qt_download_records import DownloadRecordsDialog

        self.dialog = DownloadRecordsDialog(None, port=8899, key="k")
        self.dialog._timer.stop()
        self.addCleanup(self.dialog.deleteLater)

    @staticmethod
    def _payload(state="running"):
        from tests.test_download_records import TestDownloadRecordsDialog

        payload = TestDownloadRecordsDialog._payload()
        payload["sessions"][0]["state"] = state
        return payload

    def _fill(self, payload):
        from tests.test_download_records import TestDownloadRecordsDialog

        with patch(
            "gui.qt_download_records.requests.get",
            return_value=TestDownloadRecordsDialog._response(payload),
        ):
            self.dialog.refresh_now()

    def _button(self, row=0):
        return self.dialog.action_button(row)

    def test_running_row_offers_stop(self):
        self._fill(self._payload("running"))
        button = self._button()
        self.assertIsNotNone(button)
        self.assertEqual(button.text(), "取消传输")
        self.assertTrue(button.isEnabled())

    def test_action_column_wide_enough_for_its_buttons(self):
        """列宽/按钮宽不能被文字裁掉：真机上是「取消传输」只显示一半。"""
        from gui.qt_download_records import ACTION_COLUMN, ACTION_COLUMN_WIDTH

        self._fill(self._payload("running"))
        table = self.dialog.progress_table
        self.assertGreaterEqual(table.columnWidth(ACTION_COLUMN), ACTION_COLUMN_WIDTH)
        button = self._button()
        # 左右各 6px 内边距 + 一点余量
        needed = button.fontMetrics().horizontalAdvance(button.text()) + 24
        self.assertLessEqual(needed, button.width())
        self.assertGreaterEqual(
            table.columnWidth(ACTION_COLUMN), button.width() + 12
        )

    def test_row_height_roomy_enough_for_its_buttons(self):
        """行高要容得下按钮 + 上下留白：默认 30 的行高会把按钮下边框裁掉。"""
        from gui.qt_download_records import (
            ACTION_BUTTON_HEIGHT,
            ACTION_COLUMN,
            ACTION_ROW_HEIGHT,
            ACTION_ROW_PADDING,
        )
        from tests.test_download_records import TestDownloadRecordsDialog

        payload = self._payload("running")
        self._fill(payload)
        with patch(
            "gui.qt_download_records.requests.get",
            return_value=TestDownloadRecordsDialog._response(payload),
        ):
            self.dialog.resize(1080, 460)
            self.dialog.show()
            self.app.processEvents()

        table = self.dialog.progress_table
        button = self._button()
        holder = table.cellWidget(0, ACTION_COLUMN)
        self.assertGreaterEqual(table.rowHeight(0), ACTION_ROW_HEIGHT)
        self.assertGreaterEqual(button.height(), ACTION_BUTTON_HEIGHT)
        # 按钮整块落在容器里，底部不会被行边界裁掉
        self.assertGreaterEqual(holder.height(), button.height() + ACTION_ROW_PADDING)
        self.assertLessEqual(button.y() + button.height(), holder.height())

    def test_completed_row_offers_share_cancel(self):
        self._fill(self._payload("completed"))
        self.assertEqual(self._button().text(), "取消共享")

    def test_rows_are_kept_alive_across_refresh(self):
        """每秒刷新不能重建行：否则正在点的按钮会被销毁，点击丢失。"""
        payload = self._payload()
        self._fill(payload)
        first = self._button()
        self._fill(payload)
        self.assertIs(self._button(), first)

    def test_confirm_posts_file_id_and_client(self):
        from PySide6.QtWidgets import QMessageBox

        self._fill(self._payload("running"))
        with patch(
            "gui.qt_download_records.QMessageBox.question",
            return_value=QMessageBox.Yes,
        ), patch("gui.qt_download_records.requests.post") as post, patch(
            "gui.qt_download_records.requests.get",
            side_effect=RuntimeError("client gone"),
        ):
            self.dialog._on_cancel_row(0)

        post.assert_called_once()
        kwargs = post.call_args.kwargs
        self.assertTrue(post.call_args.args[0].endswith("/downloads/cancel"))
        self.assertEqual(kwargs["json"], {"file_id": "d1", "client": "Mi 10"})
        self.assertEqual(kwargs["headers"]["key"], "k")

    def test_declining_confirmation_does_not_post(self):
        from PySide6.QtWidgets import QMessageBox

        self._fill(self._payload("running"))
        with patch(
            "gui.qt_download_records.QMessageBox.question",
            return_value=QMessageBox.No,
        ), patch("gui.qt_download_records.requests.post") as post:
            self.dialog._on_cancel_row(0)

        post.assert_not_called()

    def test_failure_is_reported(self):
        from PySide6.QtWidgets import QMessageBox

        self._fill(self._payload("running"))
        with patch(
            "gui.qt_download_records.QMessageBox.question",
            return_value=QMessageBox.Yes,
        ), patch(
            "gui.qt_download_records.requests.post",
            side_effect=RuntimeError("connection refused"),
        ), patch(
            "gui.qt_download_records.QMessageBox.warning"
        ) as warning:
            self.dialog._on_cancel_row(0)

        warning.assert_called_once()
        self.assertIn("connection refused", warning.call_args.args[2])


if __name__ == "__main__":
    unittest.main()
