import time
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from client import file_server as file_server_mod
from client import folder_archive as folder_archive_mod
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


class TestFolderShareLazyPack(_TempDirMixin):
    """文件夹共享：复制时只登记，接收端来取（prepare / GET）时才打包。"""

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

    def _push_folder(self, mock_post):
        self.sync_client._push_latest_file([str(self.folder)])
        return mock_post.call_args.kwargs["json"]["file_list"][0]

    def test_folder_registered_without_packing(self):
        mock_post = self._mock_post()
        item = self._push_folder(mock_post)

        self.assertEqual(item["name"], "magisk.zip")
        self.assertEqual(item["kind"], "folder")
        # 共享的是目录本身，不是临时压缩包
        self.assertEqual(item["path"], str(self.folder))
        self.assertEqual(self.file_server.shared_files[item["file_id"]], str(self.folder))

        # 关键：复制时不应在磁盘上留下任何压缩包
        self.assertEqual(list(self.share_dir.glob("*.zip")), [])
        self.assertEqual(self.file_server._temp_files, set())

    def test_prepare_packs_folder(self):
        mock_post = self._mock_post()
        item = self._push_folder(mock_post)

        zip_path = Path(self.file_server._ensure_archive(item["file_id"]))
        self.assertTrue(zip_path.is_file())
        self.assertEqual(zip_path.parent, self.share_dir)

        # 包内保留文件夹名作为根目录，解压后不会散落成一堆文件
        with zipfile.ZipFile(zip_path) as zf:
            names = {n.replace("\\", "/") for n in zf.namelist()}
        self.assertIn("magisk/readme.md", names)
        self.assertIn("magisk/模块/a.txt", names)

        # 压缩包登记为临时文件，共享结束时要删
        self.assertIn(str(zip_path), self.file_server._temp_files)

    def test_prepare_endpoint_reports_progress(self):
        mock_post = self._mock_post()
        item = self._push_folder(mock_post)
        self.file_server._ensure_archive(item["file_id"])

        resp = self.file_server.app.test_client().get(f"/prepare/{item['file_id']}")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertEqual(body["status"], "ready")
        self.assertEqual(body["name"], "magisk.zip")

    def test_prepare_endpoint_starts_background_pack(self):
        """接收端先 prepare：发一次就该在后台开始打包，之后轮询到 ready。"""
        mock_post = self._mock_post()
        item = self._push_folder(mock_post)
        client = self.file_server.app.test_client()

        first = client.get(f"/prepare/{item['file_id']}").get_json()
        self.assertIn(first["status"], ("idle", "packing", "ready"))

        deadline = time.time() + 5
        status = first["status"]
        while time.time() < deadline:
            status = client.get(f"/prepare/{item['file_id']}").get_json()["status"]
            if status == "ready":
                break
            time.sleep(0.05)
        self.assertEqual(status, "ready")
        self.assertTrue(Path(self.file_server._folder_shares[item["file_id"]]["path"]).is_file())

    def test_prepare_unknown_file_id(self):
        resp = self.file_server.app.test_client().get("/prepare/not-exist")
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.get_json()["status"], "unknown")

    def test_download_packs_on_demand_then_deletes(self):
        mock_post = self._mock_post()
        item = self._push_folder(mock_post)

        response = self.file_server.app.test_client().get(f"/file/{item['file_id']}")
        self.assertEqual(response.status_code, 200)
        content = response.get_data()
        response.close()

        # 内容必须是合法 zip（说明是现场打包出来的）
        self.assertTrue(content.startswith(b"PK"))
        self.assertEqual(list(self.share_dir.glob("*.zip")), [],
                         "下载完成后临时压缩包应被删除")
        self.assertEqual(self.file_server.shared_files, {})
        self.assertEqual(self.file_server._temp_files, set())

    def test_interrupted_download_keeps_zip_for_retry(self):
        """传输到一半断开时不能删包：压缩包要留着给下一次重试。"""
        mock_post = self._mock_post()
        item = self._push_folder(mock_post)
        zip_path = Path(self.file_server._ensure_archive(item["file_id"]))
        expected = zip_path.read_bytes()

        response = self.file_server.app.test_client().get(
            f"/file/{item['file_id']}", buffered=False
        )
        self.assertEqual(response.status_code, 200)
        iterator = iter(response.response)
        next(iterator)          # 只取第一块就当作连接中断
        response.close()

        self.assertTrue(zip_path.exists(), "中断后压缩包应保留，方便对方重试")
        self.assertIn(item["file_id"], self.file_server.shared_files)

        # 重试成功之后才允许删除（第二次不会再打包）
        retry = self.file_server.app.test_client().get(f"/file/{item['file_id']}")
        self.assertEqual(retry.status_code, 200)
        self.assertEqual(retry.get_data(), expected)
        retry.close()
        self.assertFalse(zip_path.exists(), "重试成功后才删除压缩包")

    def test_concurrent_downloads_keep_file_until_last_finishes(self):
        """两端同时取同一份目录：先下完的一端不能把压缩包删掉。"""
        mock_post = self._mock_post()
        item = self._push_folder(mock_post)
        zip_path = Path(self.file_server._ensure_archive(item["file_id"]))
        client = self.file_server.app.test_client()

        first = client.get(f"/file/{item['file_id']}", buffered=False)
        first_iter = iter(first.response)
        next(first_iter)
        second = client.get(f"/file/{item['file_id']}", buffered=False)

        list(first_iter)          # 第一路取完
        first.close()
        self.assertTrue(zip_path.exists(), "另一路还在下载，压缩包不能被删")
        self.assertIn(item["file_id"], self.file_server.shared_files)

        list(iter(second.response))   # 第二路取完
        second.close()
        self.assertFalse(zip_path.exists(), "最后一路取完后才删除")

    def test_new_share_discards_previous_archive(self):
        mock_post = self._mock_post()
        item = self._push_folder(mock_post)
        first_zip = Path(self.file_server._ensure_archive(item["file_id"]))
        self.assertTrue(first_zip.exists())

        other = self.root / "other"
        other.mkdir()
        (other / "b.txt").write_text("b", encoding="utf-8")
        self.sync_client._push_latest_file([str(other)])

        self.assertFalse(first_zip.exists(), "换共享时上一批压缩包应被删除")
        self.assertNotIn(item["file_id"], self.file_server._folder_shares)

    def test_push_failure_clears_registration(self):
        self._mock_post(status_code=400, text="file_list 为空")
        with self.assertLogs("client", level="ERROR"):
            self.sync_client._push_latest_file([str(self.folder)])

        self.assertEqual(self.file_server.shared_files, {})
        self.assertEqual(self.file_server._folder_shares, {})
        self.assertEqual(list(self.share_dir.glob("*.zip")), [])

    def test_expired_share_deletes_archive(self):
        mock_post = self._mock_post()
        item = self._push_folder(mock_post)
        zip_path = Path(self.file_server._ensure_archive(item["file_id"]))
        registered_at = self.file_server.shared_file_registered_at[item["file_id"]]

        with patch.object(
            file_server_mod.time,
            "time",
            return_value=registered_at + FILE_SHARE_TTL_SECONDS,
        ):
            self.assertIsNone(self.file_server.get_file_path(item["file_id"]))

        self.assertFalse(zip_path.exists(), "过期的共享应删除临时压缩包")
        self.assertEqual(self.file_server._folder_shares, {})

    def test_clear_files_deletes_archive(self):
        mock_post = self._mock_post()
        item = self._push_folder(mock_post)
        zip_path = Path(self.file_server._ensure_archive(item["file_id"]))

        self.file_server.clear_files()

        self.assertFalse(zip_path.exists())

    def test_stale_temp_files_cleaned_on_startup(self):
        self.share_dir.mkdir(parents=True, exist_ok=True)
        stale = self.share_dir / "leftover.zip"
        stale.write_bytes(b"old")

        FileServer(local_name="client-b", key="secret")

        self.assertFalse(stale.exists(), "启动时应清理上次残留的压缩包")

    def test_cancel_during_pack_discards_archive(self):
        """打包途中共享被撤销：停止压缩并清掉半成品。"""
        mock_post = self._mock_post()
        item = self._push_folder(mock_post)
        self.file_server._folder_shares[item["file_id"]]["cancel"].set()

        self.assertIsNone(self.file_server._ensure_archive(item["file_id"]))
        self.assertEqual(list(self.share_dir.glob("*.zip")), [])

    def test_unreadable_file_does_not_break_archive(self):
        """单个文件读不了只跳过，不影响整包。"""
        mock_post = self._mock_post()
        item = self._push_folder(mock_post)

        with patch.object(
            folder_archive_mod.zipfile.ZipFile, "write", side_effect=OSError("locked")
        ):
            zip_path = self.file_server._ensure_archive(item["file_id"])

        self.assertIsNotNone(zip_path)
        self.assertTrue(Path(zip_path).is_file())


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
