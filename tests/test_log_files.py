import inspect
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from common.log_files import LOG_SOURCES, LogSnapshot, log_path, read_log


class TestLogSources(unittest.TestCase):
    def test_server_and_client_use_separate_files(self):
        """需求基线：服务日志与客户端日志必须落在两个文件里。"""
        self.assertEqual(LOG_SOURCES["server"][1], "server.log")
        self.assertEqual(LOG_SOURCES["client"][1], "client.log")
        self.assertNotEqual(LOG_SOURCES["server"][1], LOG_SOURCES["client"][1])

    def test_unknown_source_is_rejected(self):
        with self.assertRaises(ValueError):
            log_path(".", "nope")


class TestReadLog(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.base = Path(self._temp.name)
        (self.base / "log").mkdir()

    def tearDown(self):
        self._temp.cleanup()

    def _write(self, source, text):
        path = log_path(self.base, source)
        path.write_text(text, encoding="utf-8")
        return path

    def test_reads_both_sources_independently(self):
        self._write("server", "2026-11-20 [INFO] 服务启动\n")
        self._write("client", "2026-11-20 [INFO] 客户端启动\n")

        server = read_log("server", self.base)
        client = read_log("client", self.base)

        self.assertTrue(server.ok)
        self.assertTrue(client.ok)
        self.assertIn("服务启动", server.text)
        self.assertNotIn("客户端启动", server.text)
        self.assertIn("客户端启动", client.text)
        self.assertEqual(server.line_count, 1)

    def test_missing_file_is_reported_without_raising(self):
        snapshot = read_log("server", self.base)

        self.assertFalse(snapshot.ok)
        self.assertEqual(snapshot.text, "")
        self.assertIn("尚未生成", snapshot.error)
        self.assertTrue(snapshot.error.endswith("server.log"))

    def test_empty_file_is_not_an_error(self):
        self._write("client", "")
        snapshot = read_log("client", self.base)

        self.assertTrue(snapshot.ok)
        self.assertEqual(snapshot.text, "")
        self.assertEqual(snapshot.file_size, 0)

    def test_large_file_keeps_only_tail_and_starts_on_a_whole_line(self):
        """截断必须丢半行，否则界面第一行是残句容易误导。"""
        lines = [f"line-{index:04d}" for index in range(5000)]
        self._write("server", "\n".join(lines) + "\n")

        snapshot = read_log("server", self.base, max_bytes=1000)

        self.assertTrue(snapshot.truncated)
        first_line = snapshot.text.splitlines()[0]
        self.assertRegex(first_line, r"^line-\d{4}$")
        self.assertEqual(snapshot.text.splitlines()[-1], "line-4999")
        self.assertEqual(snapshot.line_count, 5000 - lines.index(first_line))

    def test_line_limit_keeps_the_newest_lines(self):
        lines = [f"line-{index:02d}" for index in range(50)]
        self._write("gui", "\n".join(lines) + "\n")

        snapshot = read_log("gui", self.base, max_lines=10)

        self.assertTrue(snapshot.truncated)
        self.assertEqual(snapshot.line_count, 10)
        self.assertEqual(snapshot.text.splitlines()[0], "line-40")
        self.assertEqual(snapshot.text.splitlines()[-1], "line-49")

    def test_invalid_utf8_bytes_do_not_break_the_viewer(self):
        log_path(self.base, "client").write_bytes(b"ok\n\xff\xfe broken\n")
        snapshot = read_log("client", self.base)

        self.assertTrue(snapshot.ok)
        self.assertIn("broken", snapshot.text)

    def test_ansi_escape_sequences_are_stripped(self):
        self._write("server", "\x1b[31m红色错误\x1b[0m\n")
        snapshot = read_log("server", self.base)

        self.assertNotIn("\x1b", snapshot.text)
        self.assertIn("红色错误", snapshot.text)

    def test_chinese_content_survives(self):
        self._write("server", "2026-11-20 13:00:00 [INFO] 剪贴板同步完成\n")
        snapshot = read_log("server", self.base)

        self.assertIn("剪贴板同步完成", snapshot.text)


class TestLazyLoading(unittest.TestCase):
    """自动刷新只在窗口可见时跑，隐藏状态下一律不碰磁盘。"""

    def test_hidden_viewer_never_reads_the_file(self):
        from gui.qt_log_view import LogViewer

        viewer = LogViewer.__new__(LogViewer)
        viewer.isVisible = lambda: False

        with patch("gui.qt_log_view.read_log") as read_log_mock:
            LogViewer.refresh(viewer)

        read_log_mock.assert_not_called()


class TestLogViewerWiring(unittest.TestCase):
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

    def _build_window(self):
        from gui.qt_main_window import MainWindow

        config = SimpleNamespace(
            server_host="192.0.2.10",
            server_port=8000,
            local_name="Test-PC",
            file_server_port=8899,
            key="secret",
            save_path=str(Path(tempfile.gettempdir())),
            appearance_mode="Light",
            is_autostart_enabled=lambda: False,
        )
        manager = SimpleNamespace(config=config, file_handler=None)
        manager.get_service_state = lambda: {
            "server_enabled": False,
            "client_enabled": False,
            "server_alive": False,
            "client_alive": False,
            "busy": False,
        }
        manager.restart_services_async = lambda: True
        manager.restart_client_async = lambda: True

        window = MainWindow(manager)
        window._status_timer.stop()
        self.addCleanup(window.deleteLater)
        return window

    def test_building_the_window_does_not_read_any_log(self):
        """打开界面本身不能碰日志文件——只有点了按钮才读。"""
        with patch("gui.qt_log_view.read_log") as read_log_mock:
            window = self._build_window()
            window.show_window()
            self.app.processEvents()

        read_log_mock.assert_not_called()

    def test_clicking_an_entry_reads_that_source_exactly_once(self):
        window = self._build_window()
        window.show_window()
        self.app.processEvents()

        with patch("gui.qt_log_view.read_log") as read_log_mock:
            read_log_mock.side_effect = lambda source, *_a, **_k: LogSnapshot(
                source=source, path=Path("client.log"), text="一行日志", line_count=1
            )
            window.open_log_view("client")
            self.app.processEvents()

        self.assertEqual(read_log_mock.call_count, 1)
        self.assertEqual(read_log_mock.call_args.args[0], "client")
        self.assertTrue(window._log_viewer.isVisible())
        self.assertEqual(window._log_viewer.text.toPlainText(), "一行日志")

    def test_closing_the_ui_stops_all_log_reading(self):
        window = self._build_window()
        window.show_window()
        window.open_log_view("server")
        self.app.processEvents()

        window.hide_window()
        self.app.processEvents()
        self.assertFalse(window._log_viewer.isVisible())

        with patch("gui.qt_log_view.read_log") as read_log_mock:
            window._log_viewer.refresh()
        read_log_mock.assert_not_called()

    def test_auto_refresh_ticks_only_while_the_window_is_open(self):
        """打开后自动刷新，关闭（含收进托盘）后必须停表，不留周期任务。"""
        from gui.qt_log_view import AUTO_REFRESH_MS

        window = self._build_window()
        window.show_window()
        self.app.processEvents()

        snapshot = LogSnapshot(
            source="server", path=Path("server.log"), text="一行日志", line_count=1
        )
        with patch("gui.qt_log_view.read_log", return_value=snapshot) as read_log_mock:
            window.open_log_view("server")
            self.app.processEvents()

            viewer = window._log_viewer
            self.assertTrue(viewer.isVisible())
            self.assertTrue(viewer._auto_timer.isActive())
            self.assertEqual(viewer._auto_timer.interval(), AUTO_REFRESH_MS)
            self.assertEqual(read_log_mock.call_count, 1)

            # 定时器到点必须真的再读一次
            viewer._auto_timer.timeout.emit()
            self.assertEqual(read_log_mock.call_count, 2)

            window.hide_window()
            self.app.processEvents()
            self.assertFalse(viewer._auto_timer.isActive())

            # 隐藏后即使有人手动触发 refresh 也不该产生 I/O
            viewer.refresh()
            self.assertEqual(read_log_mock.call_count, 2)


if __name__ == "__main__":
    unittest.main()
