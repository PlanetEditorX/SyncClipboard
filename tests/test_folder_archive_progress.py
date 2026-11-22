"""目录打包的进度上报与包内容完整性。

历史实现的进度检查放在「一个目录遍历完」之后，遇到单个大文件（几 GB 的镜像）
或单目录上万小文件时，整段压缩过程不会产生任何进度，日志里只剩开始与结束两行。
现在按 1MB 分块上报，这里覆盖分块写入与进度文本。
"""

import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from client import folder_archive
from client.folder_archive import format_size, zip_folder


class ZipFolderProgressTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / "课程资料"
        self.source.mkdir()
        self.dest = self.root / "out"

    def _big_file(self, name, megabytes):
        path = self.source / name
        path.write_bytes(os.urandom(megabytes * 1024 * 1024))
        return path

    def test_large_file_reports_progress_per_chunk(self):
        """4MB 以上的单文件必须在压缩过程中多次上报，而不是只在结束时报一次。"""
        big = self._big_file("video.bin", 5)
        total = big.stat().st_size
        notes = []

        with patch.object(folder_archive, "PROGRESS_INTERVAL", 0):
            zip_folder(
                str(self.source), self.dest,
                progress=notes.append, total_size=total,
            )

        self.assertGreaterEqual(len(notes), 4, f"进度次数过少: {notes}")
        # 首次上报时已经处理过至少一块，说明不是结束后补报
        self.assertTrue(any("/" in note for note in notes))
        self.assertIn("(100%)", notes[-1])

    def test_progress_text_contains_size_and_percent(self):
        self._big_file("a.bin", 2)
        total = folder_archive.folder_size_bytes(str(self.source))
        notes = []
        with patch.object(folder_archive, "PROGRESS_INTERVAL", 0):
            zip_folder(str(self.source), self.dest,
                       progress=notes.append, total_size=total)
        self.assertTrue(notes)
        self.assertIn("/", notes[0])
        self.assertIn("%", notes[0])

    def test_without_total_size_falls_back_to_counts(self):
        self._big_file("a.bin", 2)
        notes = []
        with patch.object(folder_archive, "PROGRESS_INTERVAL", 0):
            zip_folder(str(self.source), self.dest, progress=notes.append)
        self.assertTrue(notes)
        self.assertIn("个文件", notes[0])
        self.assertNotIn("%", notes[0])

    def test_archive_content_is_intact(self):
        """分块写入后包必须能正常解压：内容、CRC、空目录、中文名都要对。"""
        payload = os.urandom(3 * 1024 * 1024 + 123)
        (self.source / "镜像.bin").write_bytes(payload)
        (self.source / "说明.txt").write_text("hello 打包", encoding="utf-8")
        (self.source / "空目录").mkdir()
        nested = self.source / "子目录"
        nested.mkdir()
        (nested / "inner.txt").write_text("nested", encoding="utf-8")

        zip_path = zip_folder(str(self.source), self.dest)

        with zipfile.ZipFile(zip_path) as zf:
            self.assertIsNone(zf.testzip(), "CRC 校验失败")
            names = zf.namelist()
            self.assertIn("课程资料/镜像.bin", names)
            self.assertIn("课程资料/空目录/", names)
            self.assertIn("课程资料/子目录/inner.txt", names)
            self.assertEqual(zf.read("课程资料/镜像.bin"), payload)
            self.assertEqual(
                zf.read("课程资料/说明.txt").decode("utf-8"), "hello 打包")

    def test_cancel_during_large_file_removes_partial_archive(self):
        import threading

        self._big_file("video.bin", 6)
        cancel = threading.Event()

        def progress(note):
            cancel.set()  # 第一块报进度时立刻撤销共享

        with patch.object(folder_archive, "PROGRESS_INTERVAL", 0):
            with self.assertRaises(folder_archive.FolderArchiveCancelled):
                zip_folder(str(self.source), self.dest,
                           progress=progress, cancel_event=cancel)

        leftovers = list(self.dest.glob("*.zip")) if self.dest.exists() else []
        self.assertEqual(leftovers, [], "取消后必须删掉半成品")

    def test_skipped_unreadable_file_still_produces_archive(self):
        (self.source / "ok.txt").write_text("fine", encoding="utf-8")
        locked = self.source / "locked.bin"
        locked.write_bytes(b"x" * 1024)

        real_open = open

        def fake_open(path, *args, **kwargs):
            if str(path).endswith("locked.bin"):
                raise OSError("locked")
            return real_open(path, *args, **kwargs)

        with patch("builtins.open", side_effect=fake_open):
            zip_path = zip_folder(str(self.source), self.dest)

        with zipfile.ZipFile(zip_path) as zf:
            self.assertIn("课程资料/ok.txt", zf.namelist())
            self.assertNotIn("课程资料/locked.bin", zf.namelist())


class FormatSizeTest(unittest.TestCase):
    def test_units(self):
        self.assertEqual(format_size(0), "0 B")
        self.assertEqual(format_size(2048), "2 KB")
        self.assertEqual(format_size(5 * 1024 ** 3), "5.0 GB")


if __name__ == "__main__":
    unittest.main()
