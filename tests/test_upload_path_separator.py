"""手机上传 / 接收文件时的落盘路径分隔符。

设置里手填「D:/Downloads」时，Windows 的 os.path.join 会拼出
「D:/Downloads\\文件名」：前半段 `/`、后半段 `\\`。通知里的「保存：」、
日志和返回给手机端的 path 都跟着变丑。这里锁死「路径一律用本机原生
分隔符」，顺带保证真的能落盘、通知点击回调拿的是同一条路径。
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote

from client.file_server import FileServer
from common.utils import normalize_local_path
from gui.file_handler import FileHandler


class TestUploadPathSeparator(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        # 模拟用户在设置里手填的正斜杠路径（Win10 上最典型的输入）
        self.forward_slash_dir = self.root.as_posix()

    def _upload(self, save_path, filename="文字同步.shortcut", data=b"payload"):
        server = FileServer(local_name="client-a", key="secret",
                            save_path=save_path)
        client = server.app.test_client()
        with patch("client.file_server.show_notification_with_click") as notify:
            resp = client.put(
                "/upload_file?filename=" + quote(filename),
                data=data,
                headers={"key": "secret", "source": "熊熊的iPhone"},
            )
        return resp, notify

    def test_upload_uses_native_separator_in_response_and_notice(self):
        resp, notify = self._upload(self.forward_slash_dir)
        self.assertEqual(resp.status_code, 200)
        path = resp.get_json()["path"]

        expected = os.path.normpath(
            os.path.join(self.forward_slash_dir, "文字同步.shortcut")
        )
        self.assertEqual(path, expected)
        self.assertEqual(path, normalize_local_path(path))  # 幂等
        if os.sep == "\\":
            self.assertNotIn("/", path)
            self.assertTrue(path.startswith(str(self.root) + os.sep))

        # 通知正文展示的就是这条路径，不能出现「保存：D:/Downloads\xxx」
        self.assertEqual(notify.call_count, 1)
        _, msg, _ = notify.call_args.args
        self.assertIn("保存：" + path, msg)
        self.assertIn("点击打开所在文件夹", msg)

        # 路径好看之外还得真的落盘
        self.assertTrue(os.path.isfile(path))
        with open(path, "rb") as f:
            self.assertEqual(f.read(), b"payload")

    def test_trailing_and_mixed_slashes_are_collapsed(self):
        if os.sep == "\\":
            mixed = self.forward_slash_dir.replace("/", "\\") + "/"
        else:
            mixed = self.forward_slash_dir + "//"

        resp, _ = self._upload(mixed, filename="a.bin")
        self.assertEqual(resp.status_code, 200)
        path = resp.get_json()["path"]
        self.assertEqual(path, os.path.normpath(os.path.join(mixed, "a.bin")))
        self.assertNotIn(os.sep + os.sep, path)
        if os.sep == "\\":
            self.assertNotIn("/", path)
        self.assertTrue(os.path.isfile(path))

    def test_filename_path_segments_do_not_leak_into_saved_path(self):
        """文件名里的目录段仍被剥掉，规范化不能把它变成路径穿越。"""
        resp, _ = self._upload(self.forward_slash_dir,
                               filename="C:\\fake\\a.txt")
        self.assertEqual(resp.status_code, 200)
        path = resp.get_json()["path"]
        self.assertEqual(os.path.dirname(path), os.path.normpath(self.root.as_posix()))
        self.assertEqual(os.path.basename(path), "a.txt")
        self.assertTrue(os.path.isfile(path))


class TestNormalizeLocalPath(unittest.TestCase):
    def test_relative_path_stays_relative(self):
        normalized = normalize_local_path("./uploads/")
        self.assertEqual(normalized, os.path.normpath("./uploads"))
        self.assertFalse(os.path.isabs(normalized))

    def test_accepts_pathlib_path(self):
        self.assertEqual(
            normalize_local_path(Path("D:/Downloads")),
            os.path.normpath("D:/Downloads"),
        )


class TestDownloadTargetPathSeparator(unittest.TestCase):
    def test_target_path_uses_native_separator(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp).as_posix() + "/"
            handler = FileHandler.__new__(FileHandler)
            handler.config = type("C", (), {"save_path": folder})()

            target = handler._target_path("demo.txt")

            self.assertEqual(
                target, os.path.normpath(os.path.join(folder, "demo.txt"))
            )
            self.assertNotIn(os.sep + os.sep, target)
            if os.sep == "\\":
                self.assertNotIn("/", target)
            self.assertEqual(os.path.basename(target), "demo.txt")
