"""接收端：目录清单逐文件下载、断线重试与断点续传、默认保存目录。"""

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


class TestFolderManifestDownload(_HandlerFixture):
    """目录共享：取清单 → 逐文件下载 → 按相对路径重建目录结构。"""

    def _resp(self, payload, status_code=200):
        resp = MagicMock()
        resp.status_code = status_code
        resp.content = b"{}"
        resp.json.return_value = payload
        return resp

    def _manifest(self, entries, root="课程"):
        return {
            "status": "ok",
            "root": root,
            "count": len(entries),
            "empty_dirs": [],
            "notice": "",
            "entries": entries,
        }

    def _serve(self, manifest, files):
        """按 URL 分派：清单走 JSON，文件走 _FakeResponse。"""
        def fake_get(url, *args, **kwargs):
            if url.endswith("/manifest/d1"):
                return self._resp(manifest)
            name = url.rsplit("/", 1)[-1]
            if name in files:
                data = files[name]
                chunks = [data[i:i + 4] for i in range(0, len(data), 4)]
                return _FakeResponse(headers={"content-length": str(len(data))},
                                     chunks=chunks)
            raise ConnectionError("404")
        return fake_get

    def test_files_land_in_rebuilt_structure(self):
        manifest = self._manifest([
            {"path": "readme.md", "dir": "", "name": "readme.md", "size": 5,
             "url": "http://1.2.3.4:8899/file/d1/readme.md"},
            {"path": "模块/a.txt", "dir": "模块/", "name": "a.txt", "size": 9,
             "url": "http://1.2.3.4:8899/file/d1/模块/a.txt"},
        ])
        with patch.object(fh_mod.requests, "get",
                          side_effect=self._serve(manifest, {"readme.md": b"hello",
                                                             "a.txt": b"content-a"})):
            ok, done = self.handler._download_folder(
                {"manifest_url": "http://1.2.3.4:8899/manifest/d1",
                 "name": "课程", "kind": "folder"},
                str(self.root), self._dialog())

        self.assertTrue(ok)
        self.assertEqual(done, 2)
        root_dir = self.root / "课程"
        self.assertEqual((root_dir / "readme.md").read_bytes(), b"hello")
        # 关键：子目录必须被重建，而不是平铺在根下
        self.assertEqual((root_dir / "模块" / "a.txt").read_bytes(), b"content-a")

    def test_failed_file_does_not_abort_the_rest(self):
        manifest = self._manifest([
            {"path": "a.txt", "dir": "", "name": "a.txt", "size": 1,
             "url": "http://1.2.3.4:8899/file/d1/a.txt"},
            {"path": "b.txt", "dir": "", "name": "b.txt", "size": 1,
             "url": "http://1.2.3.4:8899/file/d1/b.txt"},
        ])
        served = {"b.txt": b"B"}
        with patch.object(fh_mod.requests, "get",
                          side_effect=self._serve(manifest, served)), \
             patch.object(fh_mod, "MAX_DOWNLOAD_ATTEMPTS", 1), \
             patch.object(fh_mod.time, "sleep"):
            ok, done = self.handler._download_folder(
                {"manifest_url": "http://1.2.3.4:8899/manifest/d1", "name": "课程"},
                str(self.root), self._dialog())

        # a.txt 取不到 → 整体不算成功，但 b.txt 仍然落盘
        self.assertFalse(ok)
        self.assertEqual(done, 1)
        self.assertEqual((self.root / "课程" / "b.txt").read_bytes(), b"B")
        self.assertFalse((self.root / "课程" / "a.txt").exists())

    def test_manifest_error_status_reports_and_gives_up(self):
        with patch.object(fh_mod.requests, "get",
                          return_value=self._resp({"status": "error",
                                                   "message": "目录已不存在"},
                                                  status_code=404)), \
             patch("gui.file_handler.show_message") as show_message:
            ok, done = self.handler._download_folder(
                {"manifest_url": "http://1.2.3.4:8899/manifest/d1"},
                str(self.root), self._dialog())

        self.assertFalse(ok)
        self.assertEqual(done, 0)
        self.assertIn("目录已不存在", show_message.call_args.args[1])

    def test_missing_manifest_url_is_refused_locally(self):
        with patch.object(fh_mod.requests, "get") as get, \
             patch("gui.file_handler.show_message"):
            ok, done = self.handler._download_folder(
                {"download_url": "http://1.2.3.4:8899/file/d1"}, str(self.root), None)

        self.assertFalse(ok)
        get.assert_not_called()

    def test_path_traversal_entries_are_skipped(self):
        manifest = self._manifest([
            {"path": "../逃出去.txt", "dir": "", "name": "逃出去.txt", "size": 1,
             "url": "http://1.2.3.4:8899/file/d1/x"},
        ])
        with patch.object(fh_mod.requests, "get", side_effect=self._serve(manifest, {})):
            ok, done = self.handler._download_folder(
                {"manifest_url": "http://1.2.3.4:8899/manifest/d1", "name": "课程"},
                str(self.root), self._dialog())

        self.assertFalse(ok)
        self.assertEqual(done, 0)
        self.assertFalse((self.root / "逃出去.txt").exists())

    def test_empty_folder_creates_root_only(self):
        manifest = self._manifest([])
        with patch.object(fh_mod.requests, "get", side_effect=self._serve(manifest, {})):
            ok, done = self.handler._download_folder(
                {"manifest_url": "http://1.2.3.4:8899/manifest/d1", "name": "课程"},
                str(self.root), self._dialog())

        self.assertTrue(ok)
        self.assertEqual(done, 0)
        self.assertTrue((self.root / "课程").is_dir())


class TestTargetPath(_HandlerFixture):
    def test_saves_into_default_dir(self):
        path = self.handler._target_path("demo.bin")
        self.assertEqual(Path(path).parent, self.root)

    def test_duplicate_name_gets_suffix(self):
        (self.root / "demo.bin").write_bytes(b"1")
        second = self.handler._target_path("demo.bin")
        self.assertEqual(os.path.basename(second), "demo (1).bin")
