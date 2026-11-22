"""接收端：等待发送端打包、断线重试与断点续传、默认保存目录。"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from gui import file_handler as fh_mod
from gui.file_handler import FileHandler


class _FakeResponse:
    """极简 requests 响应替身：可指定状态码、响应头、数据块与中断位置。"""

    def __init__(self, status_code=200, headers=None, chunks=(), fail_after=None):
        self.status_code = status_code
        self.headers = headers or {}
        self._chunks = list(chunks)
        self._fail_after = fail_after

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size=8192):
        for index, chunk in enumerate(self._chunks):
            if self._fail_after is not None and index >= self._fail_after:
                raise ConnectionError("连接被重置")
            yield chunk


class _HandlerFixture(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.handler = FileHandler.__new__(FileHandler)
        self.handler.config = type("C", (), {"save_path": str(self.root)})()

    def _dialog(self):
        dialog = MagicMock()
        dialog.is_cancelled.return_value = False
        return dialog


class TestDownloadResume(_HandlerFixture):
    def test_retry_uses_range_and_completes_file(self):
        target = self.root / "demo.bin"
        first = _FakeResponse(
            headers={"content-length": "10"},
            chunks=[b"aaaa", b"bbbb", b"cccc"],
            fail_after=2,
        )
        second = _FakeResponse(
            status_code=206,
            headers={"Content-Range": "bytes 8-11/12", "content-length": "4"},
            chunks=[b"cccc"],
        )

        with patch.object(fh_mod.requests, "get", side_effect=[first, second]) as get, \
             patch.object(fh_mod.time, "sleep"):
            ok, downloaded = self.handler._download_with_resume(
                "http://x/file/1", str(target), self._dialog()
            )

        self.assertTrue(ok)
        self.assertEqual(downloaded, 12)
        self.assertEqual(target.read_bytes(), b"aaaabbbbcccc")
        self.assertEqual(get.call_count, 2)
        # 第二次必须带 Range，从已收到的 8 字节续传
        self.assertEqual(get.call_args_list[1].kwargs["headers"], {"Range": "bytes=8-"})

    def test_gives_up_after_max_attempts(self):
        target = self.root / "demo.bin"
        responses = [
            _FakeResponse(chunks=[b"aa", b"bb"], fail_after=1)
            for _ in range(fh_mod.MAX_DOWNLOAD_ATTEMPTS)
        ]

        with patch.object(fh_mod.requests, "get", side_effect=responses), \
             patch.object(fh_mod.time, "sleep"):
            with self.assertRaises(Exception):
                self.handler._download_with_resume(
                    "http://x/file/1", str(target), self._dialog()
                )

    def test_server_without_range_restarts_from_zero(self):
        target = self.root / "demo.bin"
        first = _FakeResponse(chunks=[b"aaaa", b"bbbb"], fail_after=1)
        second = _FakeResponse(chunks=[b"xyzw"])   # 200，不支持续传

        with patch.object(fh_mod.requests, "get", side_effect=[first, second]), \
             patch.object(fh_mod.time, "sleep"):
            ok, downloaded = self.handler._download_with_resume(
                "http://x/file/1", str(target), self._dialog()
            )

        self.assertTrue(ok)
        self.assertEqual(downloaded, 4)
        self.assertEqual(target.read_bytes(), b"xyzw")


class TestPrepareFolder(_HandlerFixture):
    def _resp(self, payload, status_code=200):
        resp = MagicMock()
        resp.status_code = status_code
        resp.content = b"{}"
        resp.json.return_value = payload
        return resp

    def test_ready_returns_true(self):
        with patch.object(fh_mod.requests, "get",
                          return_value=self._resp({"status": "ready"})):
            self.assertTrue(
                self.handler._prepare_folder_share("http://1.2.3.4:8899/file/abc", self._dialog())
            )

    def test_waits_until_ready(self):
        packing = self._resp({"status": "packing", "progress": "已处理 10 个文件"})
        ready = self._resp({"status": "ready"})
        with patch.object(fh_mod.requests, "get", side_effect=[packing, ready]), \
             patch("gui.file_handler.post_to_main_thread_no_wait") as post, \
             patch.object(fh_mod.time, "sleep") as sleep:
            self.assertTrue(
                self.handler._prepare_folder_share("http://1.2.3.4:8899/file/abc", self._dialog())
            )
        self.assertTrue(sleep.called)
        # 进度文本要投递到主线程更新对话框
        self.assertTrue(post.called)

    def test_error_returns_false_with_message(self):
        with patch.object(fh_mod.requests, "get",
                          return_value=self._resp({"status": "error", "message": "磁盘已满"})), \
             patch("gui.file_handler.show_message") as show_message:
            self.assertFalse(
                self.handler._prepare_folder_share("http://1.2.3.4:8899/file/abc", self._dialog())
            )
        self.assertIn("磁盘已满", show_message.call_args.args[1])

    def test_url_without_file_marker_is_skipped(self):
        with patch.object(fh_mod.requests, "get") as get:
            self.assertTrue(self.handler._prepare_folder_share("http://x/download/1", None))
        get.assert_not_called()


class TestTargetPath(_HandlerFixture):
    def test_saves_into_default_dir(self):
        path = self.handler._target_path("demo.bin")
        self.assertEqual(Path(path).parent, self.root)

    def test_duplicate_name_gets_suffix(self):
        (self.root / "demo.bin").write_bytes(b"1")
        second = self.handler._target_path("demo.bin")
        self.assertEqual(os.path.basename(second), "demo (1).bin")
