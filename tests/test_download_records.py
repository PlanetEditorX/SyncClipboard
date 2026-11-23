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


if __name__ == "__main__":
    unittest.main()
