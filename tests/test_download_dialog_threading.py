"""下载进度窗口的线程约束回归测试。

用户报过「点击通知下载后，主窗口和进度窗口一起未响应、进度窗一片空白」。
根因是 worker 线程直接调用 QWidget 方法（reset 里还有 raise_()）。这些测试
锁住新的约束：worker 只 emit 信号 / 读写线程安全标志，控件操作留在 GUI 线程。
"""

import os
import queue
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


class TestDownloadDialogThreading(unittest.TestCase):
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

    def setUp(self):
        from gui.qt_download_dialog import DownloadProgressDialog

        self.dialog = DownloadProgressDialog("测试进度")
        self.errors = []

    def tearDown(self):
        try:
            self.dialog.close()
            self.app.processEvents()
            self.dialog.deleteLater()
            self.app.processEvents()
        except RuntimeError:
            # close() 已经走 deleteLater，C++ 对象可能已经销毁；Python 侧
            # 再碰一次就会报 shiboken 的 "already deleted"，这里不需要处理。
            pass

    def _run_worker(self, func):
        thread = threading.Thread(target=func, name="download-worker")
        thread.start()
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive(), "worker 线程被进度窗口卡住")
        self.assertEqual(self.errors, [], f"worker 线程调用出错: {self.errors}")

    def test_worker_updates_reach_widgets_after_gui_event_loop(self):
        dialog = self.dialog

        def worker():
            try:
                dialog.reset("a.txt")
                dialog.update_progress(42, 1.5, 3.0)
            except Exception as exc:  # noqa: BLE001
                self.errors.append(repr(exc))

        self._run_worker(worker)
        # 信号是 queued 的，必须让 GUI 线程跑一轮事件循环才会落到控件上
        self.app.processEvents()
        self.assertEqual(dialog.progress.value(), 42)
        self.assertIn("42%", dialog.detail_label.text())
        self.assertIn("a.txt", dialog.title_label.text())

    def test_cancel_flag_is_safe_across_threads(self):
        dialog = self.dialog
        self.assertFalse(dialog.is_cancelled())
        self._run_worker(dialog.cancel)
        self.assertTrue(dialog.is_cancelled())
        self.app.processEvents()
        self.assertTrue(dialog._closed)

    def test_close_from_worker_is_delivered_to_gui_thread(self):
        dialog = self.dialog

        def worker():
            try:
                dialog.update_progress(10, 0.1, 1.0)
                dialog.close()
            except Exception as exc:  # noqa: BLE001
                self.errors.append(repr(exc))

        self._run_worker(worker)
        self.app.processEvents()
        self.assertTrue(dialog._closed)
        self.assertFalse(dialog.isVisible())
        # GUI 线程已经接手销毁，worker 侧的引用消失不会再触发跨线程析构
        from gui.qt_download_dialog import _alive_dialogs

        self.assertNotIn(dialog, _alive_dialogs)


class TestSaveStreamCleanup(unittest.TestCase):
    """旧协议直出文件流时，取消 / 失败也必须关掉进度窗。"""

    def test_cancel_closes_progress_dialog(self):
        from gui.file_handler import FileHandler

        with tempfile.TemporaryDirectory() as folder:
            handler = FileHandler.__new__(FileHandler)
            handler.config = SimpleNamespace(
                save_path=folder, key="secret", local_name="PC"
            )
            response = MagicMock()
            response.headers = {"content-length": "100"}
            response.iter_content.return_value = [b"x" * 10]
            progress = MagicMock()
            progress.is_cancelled.return_value = True

            with patch("gui.file_handler.post_to_main_thread", return_value=progress), \
                    patch("gui.file_handler.show_message"), \
                    patch("gui.file_handler.copy_files_to_clipboard"), \
                    patch.object(handler, "_announce_saved"):
                handler._save_file_stream(response, "a.txt")

            progress.close.assert_called_once()
            self.assertFalse((Path(folder) / "a.txt").exists())


class TestUiQueueReentrancy(unittest.TestCase):
    """QMessageBox.exec() 会在队列泵任务内部开嵌套事件循环。"""

    def setUp(self):
        import common.ui_bridge as bridge

        self.bridge = bridge
        self.old_queue = bridge._queue
        self.old_draining = bridge._draining
        self.old_root = bridge._ui_root
        self.old_accepting = bridge._ui_accepting
        bridge._queue = queue.Queue()
        bridge._draining = False
        bridge._ui_root = object()  # 让 worker 线程的投递被接受
        bridge._ui_accepting = True
        self.events = []

    def tearDown(self):
        self.bridge._queue = self.old_queue
        self.bridge._draining = self.old_draining
        self.bridge._ui_root = self.old_root
        self.bridge._ui_accepting = self.old_accepting

    def _deliver_from_worker(self, *functions):
        def deliver():
            for function in functions:
                self.bridge.post_to_main_thread_no_wait(function)

        thread = threading.Thread(target=deliver)
        thread.start()
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())

    def test_reentrant_pump_does_not_pull_next_task(self):
        def outer():
            self.events.append("outer-start")
            # 模拟模态框内的嵌套事件循环再次触发 timer
            self.bridge.process_ui_queue()
            self.events.append("outer-end")

        self._deliver_from_worker(outer, lambda: self.events.append("inner"))
        self.bridge.process_ui_queue()
        self.assertEqual(self.events, ["outer-start", "outer-end", "inner"])

    def test_show_message_never_blocks_worker(self):
        bridge = self.bridge
        old_handler = bridge._message_handler
        old_root = bridge._ui_root
        old_accepting = bridge._ui_accepting
        bridge._message_handler = lambda title, message: self.events.append(message)
        bridge._ui_root = object()
        bridge._ui_accepting = True
        try:
            def worker():
                bridge.show_message("标题", "内容")
                self.events.append("worker-继续跑")

            thread = threading.Thread(target=worker)
            thread.start()
            thread.join(timeout=3)
            self.assertFalse(thread.is_alive(), "消息框把下载线程卡住了")
            self.assertIn("worker-继续跑", self.events)
        finally:
            bridge._message_handler = old_handler
            bridge._ui_root = old_root
            bridge._ui_accepting = old_accepting


if __name__ == "__main__":
    unittest.main()
