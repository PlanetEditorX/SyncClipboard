import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.parse import unquote

from client import file_server as file_server_mod
from client import folder_manifest as folder_manifest_mod
from client.file_server import FileServer
from client.main_menu import SyncClient
from server.core.file_latest import FILE_SHARE_TTL_SECONDS


def authed(server):
    """统一契约：FileServer 的每个请求都要带 key 请求头。"""
    client = server.app.test_client()
    client.environ_base["HTTP_KEY"] = server.KEY
    return client


class _TempDirMixin(unittest.TestCase):
    """每个用例一个临时目录，被共享的目录建在里面。"""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)


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


class TestFolderShareManifest(_TempDirMixin):
    """文件夹共享：只登记路径，接收端拿清单逐文件取（不打包、不压缩）。"""

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

        # 目录名就是目录名，不再挂 .zip 后缀
        self.assertEqual(item["name"], "magisk")
        self.assertEqual(item["kind"], "folder")
        self.assertEqual(item["path"], str(self.folder))
        self.assertEqual(self.file_server.shared_files[item["file_id"]], str(self.folder))
        self.assertEqual(
            self.file_server.shared_folders[item["file_id"]]["path"], str(self.folder))

    def test_manifest_lists_structure(self):
        mock_post = self._mock_post()
        item = self._push_folder(mock_post)

        body = authed(self.file_server).get(
            f"/manifest/{item['file_id']}").get_json()
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["root"], "magisk")
        self.assertEqual(body["count"], 2)
        by_path = {e["path"]: e for e in body["entries"]}
        self.assertEqual(by_path["readme.md"]["dir"], "")
        self.assertEqual(by_path["模块/a.txt"]["dir"], "模块/")
        # 路径按段百分号编码后再拼进 URL，中文目录同样要编码
        self.assertTrue(unquote(by_path["模块/a.txt"]["url"]).endswith(
            "/file/%s/模块/a.txt" % item["file_id"]))

    def test_zip_route_on_folder_points_at_manifest(self):
        mock_post = self._mock_post()
        item = self._push_folder(mock_post)

        resp = authed(self.file_server).get(f"/file/{item['file_id']}")
        self.assertEqual(resp.status_code, 409)
        body = resp.get_json()
        self.assertIn("manifest", body["message"])
        self.assertTrue(body["manifest_url"].endswith(f"/manifest/{item['file_id']}"))

    def test_member_download_keeps_share(self):
        mock_post = self._mock_post()
        item = self._push_folder(mock_post)
        client = authed(self.file_server)

        resp = client.get(f"/file/{item['file_id']}/模块/a.txt")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_data(), b"content-a")
        resp.close()
        self.assertIn(item["file_id"], self.file_server.shared_folders)

    def test_manifest_skips_unreadable_file(self):
        """单个文件读不到体积只跳过它，其余条目照常给。"""
        mock_post = self._mock_post()
        item = self._push_folder(mock_post)

        real_getsize = folder_manifest_mod.os.path.getsize

        def fake_getsize(path, *args, **kwargs):
            if str(path).endswith("readme.md"):
                raise OSError("locked")
            return real_getsize(path, *args, **kwargs)

        with patch.object(folder_manifest_mod.os.path, "getsize", fake_getsize):
            body = authed(self.file_server).get(
                f"/manifest/{item['file_id']}").get_json()

        self.assertEqual(body["count"], 1)
        self.assertEqual([e["path"] for e in body["entries"]], ["模块/a.txt"])

    def test_push_failure_clears_registration(self):
        self._mock_post(status_code=400, text="file_list 为空")
        with self.assertLogs("client", level="ERROR"):
            self.sync_client._push_latest_file([str(self.folder)])

        self.assertEqual(self.file_server.shared_files, {})
        self.assertEqual(self.file_server.shared_folders, {})

    def test_expired_share_is_dropped(self):
        mock_post = self._mock_post()
        item = self._push_folder(mock_post)
        registered_at = self.file_server.shared_file_registered_at[item["file_id"]]

        with patch.object(
            file_server_mod.time,
            "time",
            return_value=registered_at + FILE_SHARE_TTL_SECONDS,
        ):
            self.assertIsNone(self.file_server.get_file_path(item["file_id"]))

        self.assertEqual(self.file_server.shared_folders, {})
        # 目录本身当然还在（我们从不碰用户文件）
        self.assertTrue(self.folder.is_dir())

    def test_clear_files_clears_registration(self):
        mock_post = self._mock_post()
        item = self._push_folder(mock_post)

        self.file_server.clear_files()

        self.assertEqual(self.file_server.shared_folders, {})
        self.assertNotIn(item["file_id"], self.file_server.shared_files)
        self.assertTrue(self.folder.is_dir(), "清空共享不能动用户的目录")


class TestFilePushQueue(_TempDirMixin):
    """推送走后台队列且只保留最新一批（避免连点复制时堆积）。"""

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
