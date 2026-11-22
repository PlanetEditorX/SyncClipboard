"""下载完成后的“打开所在目录”行为。

以前多文件下载结束会弹一个「下载完成」模态框，用户点掉之后还得自己去找文件；
现在改成直接在资源管理器里定位到文件（或保存目录），并留一条可点击的通知。
这里覆盖定位函数的降级路径，以及 FileHandler 的落点。
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from common import utils as utils_mod
from common.utils import reveal_in_explorer
from gui.file_handler import FileHandler


class TestRevealInExplorer(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.file = self.root / "子目录" / "demo.txt"
        self.file.parent.mkdir(parents=True)
        self.file.write_text("hello", encoding="utf-8")

    def test_empty_path_returns_false(self):
        self.assertFalse(reveal_in_explorer(""))

    def test_windows_file_is_selected_via_explorer(self):
        with patch.object(sys, "platform", "win32"), \
             patch("subprocess.Popen") as popen:
            self.assertTrue(reveal_in_explorer(str(self.file)))
        self.assertEqual(popen.call_count, 1)
        command = popen.call_args.args[0]
        self.assertIn("explorer", command)
        self.assertIn("/select,", command)
        self.assertIn(str(self.file), command)

    def test_windows_directory_is_opened_directly(self):
        with patch.object(sys, "platform", "win32"), \
             patch.object(utils_mod.os, "startfile", create=True) as startfile:
            self.assertTrue(reveal_in_explorer(str(self.root)))
        startfile.assert_called_once_with(str(self.root))

    def test_missing_file_falls_back_to_parent_directory(self):
        missing = self.root / "子目录" / "已被删除.txt"
        with patch.object(sys, "platform", "win32"), \
             patch.object(utils_mod.os, "startfile", create=True) as startfile:
            self.assertTrue(reveal_in_explorer(str(missing)))
        startfile.assert_called_once_with(str(missing.parent))

    def test_fully_missing_path_returns_false(self):
        gone = self.root / "no-such" / "no-such-2"
        with patch.object(sys, "platform", "win32"), \
             patch("subprocess.Popen") as popen:
            self.assertFalse(reveal_in_explorer(str(gone)))
        popen.assert_not_called()

    def test_linux_opens_containing_directory(self):
        with patch.object(sys, "platform", "linux"), \
             patch("subprocess.Popen") as popen:
            self.assertTrue(reveal_in_explorer(str(self.file)))
        self.assertEqual(popen.call_args.args[0][0], "xdg-open")
        self.assertEqual(popen.call_args.args[0][1], str(self.file.parent))


class TestAnnounceSaved(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.file = self.root / "demo.bin"
        self.file.write_bytes(b"x")

    def test_single_file_reveals_and_notifies(self):
        with patch("gui.file_handler.reveal_in_explorer") as reveal, \
             patch("gui.file_handler.show_notification_with_click") as notify:
            FileHandler._announce_saved(str(self.file))

        reveal.assert_called_once_with(str(self.file))
        notify.assert_called_once()
        title, msg, callback = notify.call_args.args
        self.assertEqual(title, "文件已保存")
        self.assertIn("demo.bin", msg)

        # 点击通知要能再次定位（用户可能顺手关掉了资源管理器）
        with patch("gui.file_handler.reveal_in_explorer") as again:
            callback()
        again.assert_called_once_with(str(self.file))

    def test_multi_file_reveals_directory_with_count(self):
        with patch("gui.file_handler.reveal_in_explorer") as reveal, \
             patch("gui.file_handler.show_notification_with_click") as notify:
            FileHandler._announce_saved(str(self.root), count=3)

        reveal.assert_called_once_with(str(self.root))
        title, msg, _ = notify.call_args.args
        self.assertEqual(title, "下载完成")
        self.assertIn("3", msg)

    def test_notification_failure_does_not_raise(self):
        with patch("gui.file_handler.reveal_in_explorer") as reveal, \
             patch("gui.file_handler.show_notification_with_click",
                   side_effect=RuntimeError("no toast")):
            FileHandler._announce_saved(str(self.file))
        reveal.assert_called_once()

    def test_multi_file_download_no_longer_shows_modal(self):
        """多文件下载：不弹「保存到哪」、不弹「下载完成」，最后定位到保存目录。"""
        import gui.file_handler as fh

        handler = FileHandler.__new__(FileHandler)
        handler.config = type("C", (), {
            "save_path": str(self.root),
            "server_host": "127.0.0.1",
            "server_port": 8000,
            "key": "secret",
            "local_name": "PC-A",
        })()

        response = type("R", (), {
            "headers": {"Content-Type": "application/json"},
            "status_code": 200,
            "json": lambda self: {
                "type": "file_list",
                "files": [{"name": "a.bin", "download_url": "http://x/file/1"}],
            },
            "close": lambda self: None,
        })()

        with patch.object(fh.requests, "post", return_value=response), \
             patch.object(handler, "_download_from_url", return_value=True) as download, \
             patch("gui.file_handler.post_to_main_thread") as post, \
             patch("gui.file_handler.DownloadProgressDialog") as dialog_cls, \
             patch.object(handler, "_announce_saved") as announce, \
             patch.object(handler, "_clear_latest_file"), \
             patch("gui.file_handler.show_message") as show_message, \
             patch("gui.file_handler.threading.current_thread",
                   return_value=object()):  # 非主线程 → 原地执行，不再另起线程
            dialog = dialog_cls.return_value
            dialog.is_cancelled.return_value = False
            post.return_value = dialog

            handler.fetch_file_with_progress()

        show_message.assert_not_called()
        announce.assert_called_once()
        self.assertEqual(announce.call_args.kwargs.get("count"), 1)
        # 不再询问保存位置：直接落到配置里的下载目录
        self.assertEqual(download.call_args.kwargs["save_path"], str(self.root))
