"""目录共享的基础工具：体积统计、展示名、体积格式化。

这些函数原先在 folder_archive.py（zip 打包模块）里。v1.12.4 起目录不再打包，
模块随之删除，工具挪到 folder_manifest.py —— 这里盯住它们的边界行为，
尤其是「盘符根目录」那种没有 basename 的情况（共享整个 D: 盘时要有个名字）。
"""

import tempfile
import unittest
from pathlib import Path

from client.folder_manifest import (
    folder_display_name,
    folder_size_bytes,
    format_size,
    scan_folder,
)


class FormatSizeTest(unittest.TestCase):
    def test_units(self):
        self.assertEqual(format_size(0), "0 B")
        self.assertEqual(format_size(2048), "2 KB")
        self.assertEqual(format_size(5 * 1024 ** 3), "5.0 GB")

    def test_megabytes_have_one_decimal(self):
        self.assertEqual(format_size(3 * 1024 ** 2), "3.0 MB")


class FolderSizeTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)

    def test_counts_nested_files(self):
        (self.root / "子目录").mkdir()
        (self.root / "a.txt").write_bytes(b"x" * 10)
        (self.root / "子目录" / "b.bin").write_bytes(b"y" * 32)
        self.assertEqual(folder_size_bytes(self.root), 42)

    def test_empty_folder_is_zero(self):
        self.assertEqual(folder_size_bytes(self.root), 0)

    def test_scan_folder_returns_size_and_count_in_one_walk(self):
        (self.root / "子目录").mkdir()
        (self.root / "a.txt").write_bytes(b"x" * 10)
        (self.root / "子目录" / "b.bin").write_bytes(b"y" * 32)
        self.assertEqual(scan_folder(self.root), (42, 2))

    def test_scan_empty_folder(self):
        self.assertEqual(scan_folder(self.root), (0, 0))


class FolderDisplayNameTest(unittest.TestCase):
    def test_plain_folder(self):
        self.assertEqual(folder_display_name("D:/Downloads/课程"), "课程")

    def test_trailing_separator_ignored(self):
        self.assertEqual(folder_display_name("D://Downloads//课程//"), "课程")

    def test_drive_root_falls_back_to_drive_letter(self):
        # 共享整个盘时 basename 是空的，退回盘符，别让上报名字变成 ""
        self.assertEqual(folder_display_name("D://"), "D")
        self.assertEqual(folder_display_name("D:/"), "D")


if __name__ == "__main__":
    unittest.main()
