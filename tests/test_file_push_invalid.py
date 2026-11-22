import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from client import file_server as file_server_mod
from client import main_menu as main_menu_mod
from client.file_server import FileServer
from client.main_menu import SyncClient
from server.core.file_latest import FILE_SHARE_TTL_SECONDS


class _TempDirMixin(unittest.TestCase):
    """把临时压缩包目录指到测试用临时目录，避免污染仓库。"""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.share_dir = self.root / "folder_share"

        for module in (main_menu_mod, file_server_mod):
            patcher = patch.object(module, "FOLDER_SHARE_TEMP_DIR", self.share_dir)
            patcher.start()
            self.addCleanup(patcher.stop)


class TestPushLatestFileInvalidPaths(_TempDirMixin):
    """回归：整批路径都无效时 _push_latest_file 不能抛 UnboundLocalError。

    线上日志（2026-11-21 13:44:40 与 14:13:05）表现为：
        共享文件列表已清空
        文件不存在，跳过: D:\\Downloads\\红米note8
        监听异常: cannot access local variable 'name' where it is not associated with a value
    根因是 `name` 只在通过校验的分支里赋值，全部跳过时日志语句仍引用它。
    """

    def setUp(self):
        super().setUp()
        self.file_server = FileServer(local_name="client-a", key="secret")
        self.sync_client = SyncClient(
            {
                "server_host": "127.0.0.1",
                "server_port": 8000,
                "key": "secret",
                "local_name": "client-a",
            },
            self.file_server,
        )

    def _mock_post(self, status_code=200, text=""):
        patcher = patch("client.main_menu.requests.post")
        mock_post = patcher.start()
        self.addCleanup(patcher.stop)
        mock_post.return_value = MagicMock(status_code=status_code, text=text)
        return mock_post

    def test_all_paths_missing_does_not_raise(self):
        mock_post = self._mock_post()
        missing = str(self.root / "红米note8" / "magisk")

        # 不允许抛异常（此前会抛 UnboundLocalError 并被 _push_loop 记成“监听异常”）
        self.sync_client._push_latest_file([missing])

        mock_post.assert_not_called()

    def test_mixed_paths_push_only_valid_entries(self):
        mock_post = self._mock_post()
        valid = self.root / "demo.bin"
        valid.write_bytes(b"0123456789")
        missing = str(self.root / "不存在.bin")

        self.sync_client._push_latest_file([missing, str(valid)])

        mock_post.assert_called_once()
        payload = mock_post.call_args.kwargs["json"]
        names = [item["name"] for item in payload["file_list"]]
        self.assertEqual(names, ["demo.bin"])
        self.assertEqual(payload["file_list"][0]["size"], 10)
        self.assertEqual(payload["source"], "client-a")
        self.assertEqual(len(self.file_server.shared_files), 1)

    def test_server_rejects_result_without_raising(self):
        """服务端返回 400 时日志仍需正常引用摘要变量，并撤销这次共享。"""
        mock_post = self._mock_post(status_code=400, text="file_list 为空")
        valid = self.root / "demo.txt"
        valid.write_text("x", encoding="utf-8")

        with self.assertLogs("client", level="ERROR") as captured:
            self.sync_client._push_latest_file([str(valid)])

        self.assertTrue(any("同步失败" in line for line in captured.output))
        self.assertFalse(any("UnboundLocalError" in line for line in captured.output))
        mock_post.assert_called_once()
        # 同步失败 → 撤销登记，不留下无人引用的共享
        self.assertEqual(self.file_server.shared_files, {})


class TestFolderZipShare(_TempDirMixin):
    """文件夹 → 临时 zip → 共享 → 共享结束删除压缩包。"""

    def setUp(self):
        super().setUp()
        # 被复制的文件夹：含子目录与文件
        self.folder = self.root / "红米note8" / "magisk"
        (self.folder / "模块").mkdir(parents=True)
        (self.folder / "模块" / "a.txt").write_text("content-a", encoding="utf-8")
        (self.folder / "readme.md").write_text("hello", encoding="utf-8")

        self.file_server = FileServer(local_name="client-a", key="secret")
        self.sync_client = SyncClient(
            {
                "server_host": "127.0.0.1",
                "server_port": 8000,
                "key": "secret",
                "local_name": "client-a",
            },
            self.file_server,
        )

    def _mock_post(self, status_code=200, text=""):
        patcher = patch("client.main_menu.requests.post")
        mock_post = patcher.start()
        self.addCleanup(patcher.stop)
        mock_post.return_value = MagicMock(status_code=status_code, text=text)
        return mock_post

    def test_folder_is_zipped_then_shared(self):
        mock_post = self._mock_post()

        self.sync_client._push_latest_file([str(self.folder)])

        payload = mock_post.call_args.kwargs["json"]
        self.assertEqual(len(payload["file_list"]), 1)
        item = payload["file_list"][0]
        self.assertEqual(item["name"], "magisk.zip")

        zip_path = Path(item["path"])
        self.assertTrue(zip_path.is_file())
        self.assertEqual(zip_path.parent, self.share_dir)
        self.assertEqual(item["size"], zip_path.stat().st_size)
        # 压缩包是临时产物，必须登记为 temporary，共享结束才会被删
        self.assertIn(str(zip_path), self.file_server._temp_files)

        # 包内保留文件夹名作为根目录，解压后不会散落成一堆文件
        with zipfile.ZipFile(zip_path) as zf:
            names = {n.replace("\\", "/") for n in zf.namelist()}
        self.assertIn("magisk/readme.md", names)
        self.assertIn("magisk/模块/a.txt", names)

    def test_folder_and_files_share_together(self):
        mock_post = self._mock_post()
        loose = self.root / "note.txt"
        loose.write_text("x", encoding="utf-8")

        self.sync_client._push_latest_file([str(loose), str(self.folder)])

        payload = mock_post.call_args.kwargs["json"]
        names = sorted(item["name"] for item in payload["file_list"])
        self.assertEqual(names, ["magisk.zip", "note.txt"])

    def test_download_deletes_temp_zip(self):
        mock_post = self._mock_post()
        self.sync_client._push_latest_file([str(self.folder)])
        item = mock_post.call_args.kwargs["json"]["file_list"][0]
        zip_path = Path(item["path"])
        self.assertTrue(zip_path.exists())

        response = self.file_server.app.test_client().get(f"/file/{item['file_id']}")
        self.assertEqual(response.status_code, 200)
        expected = zip_path.read_bytes()
        # 包装层必须原样转发内容（不能截断）
        self.assertEqual(response.get_data(), expected)
        response.close()

        self.assertFalse(zip_path.exists(), "下载完成后临时压缩包应被删除")
        self.assertEqual(self.file_server.shared_files, {})
        self.assertEqual(self.file_server._temp_files, set())

    def test_new_share_clears_previous_temp_zip(self):
        mock_post = self._mock_post()
        self.sync_client._push_latest_file([str(self.folder)])
        first_zip = Path(mock_post.call_args.kwargs["json"]["file_list"][0]["path"])

        other = self.root / "other"
        other.mkdir()
        (other / "b.txt").write_text("b", encoding="utf-8")
        self.sync_client._push_latest_file([str(other)])

        self.assertFalse(first_zip.exists(), "换共享时上一批压缩包应被删除")
        second_zip = Path(mock_post.call_args.kwargs["json"]["file_list"][0]["path"])
        self.assertTrue(second_zip.exists())
        self.assertNotEqual(first_zip, second_zip)

    def test_push_failure_removes_temp_zip(self):
        self._mock_post(status_code=400, text="file_list 为空")
        with self.assertLogs("client", level="ERROR"):
            self.sync_client._push_latest_file([str(self.folder)])

        self.assertEqual(list(self.share_dir.glob("*.zip")), [])
        self.assertEqual(self.file_server.shared_files, {})

    def test_expired_share_deletes_temp_zip(self):
        mock_post = self._mock_post()
        self.sync_client._push_latest_file([str(self.folder)])
        item = mock_post.call_args.kwargs["json"]["file_list"][0]
        zip_path = Path(item["path"])
        registered_at = self.file_server.shared_file_registered_at[item["file_id"]]

        with patch.object(
            file_server_mod.time,
            "time",
            return_value=registered_at + FILE_SHARE_TTL_SECONDS,
        ):
            self.assertIsNone(self.file_server.get_file_path(item["file_id"]))

        self.assertFalse(zip_path.exists(), "过期的共享应删除临时压缩包")

    def test_clear_files_deletes_temp_zip(self):
        mock_post = self._mock_post()
        self.sync_client._push_latest_file([str(self.folder)])
        zip_path = Path(mock_post.call_args.kwargs["json"]["file_list"][0]["path"])

        self.file_server.clear_files()

        self.assertFalse(zip_path.exists())

    def test_stale_temp_files_cleaned_on_startup(self):
        self.share_dir.mkdir(parents=True, exist_ok=True)
        stale = self.share_dir / "leftover.zip"
        stale.write_bytes(b"old")

        FileServer(local_name="client-b", key="secret")

        self.assertFalse(stale.exists(), "启动时应清理上次残留的压缩包")

    def test_unreadable_file_does_not_break_archive(self):
        """单个文件读不了只跳过，不影响整包。"""
        mock_post = self._mock_post()

        with patch.object(
            main_menu_mod.zipfile.ZipFile, "write", side_effect=OSError("locked")
        ):
            self.sync_client._push_latest_file([str(self.folder)])

        mock_post.assert_called_once()
        item = mock_post.call_args.kwargs["json"]["file_list"][0]
        self.assertEqual(item["name"], "magisk.zip")
        self.assertTrue(Path(item["path"]).is_file())


class TestFilePushQueue(_TempDirMixin):
    """文件夹打包耗时长，推送必须走后台队列且只保留最新一批。"""

    def setUp(self):
        super().setUp()
        self.sync_client = SyncClient(
            {
                "server_host": "127.0.0.1",
                "server_port": 8000,
                "key": "secret",
                "local_name": "client-a",
            },
        )

    def test_queue_keeps_latest_batch_only(self):
        self.sync_client._enqueue_file_push(["a"])
        self.sync_client._enqueue_file_push(["b", "c"])

        self.assertEqual(self.sync_client._file_push_queue.qsize(), 1)
        self.assertEqual(self.sync_client._file_push_queue.get_nowait(), ["b", "c"])

    def test_stop_wakes_worker(self):
        self.sync_client.running = False
        self.sync_client.stop()

        # 哨兵放入成功（队列已空，可放入 None）
        self.assertEqual(self.sync_client._file_push_queue.get_nowait(), None)


if __name__ == "__main__":
    unittest.main()
