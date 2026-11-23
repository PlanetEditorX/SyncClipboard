import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from client.file_server import FileServer
from server.core.file_latest import FILE_SHARE_TTL_SECONDS


def authed(server):
    """统一契约：FileServer 的每个请求都要带 key 请求头。"""
    client = server.app.test_client()
    client.environ_base["HTTP_KEY"] = server.KEY
    return client


class TestFileServerExpiration(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.file_path = Path(self.temp_dir.name) / "shared.txt"
        self.file_path.write_text("shared", encoding="utf-8")
        self.latest_path = Path(self.temp_dir.name) / "file_latest.json"
        self.server = FileServer()

    def tearDown(self):
        self.temp_dir.cleanup()

    @patch('client.file_server.time.time', return_value=1000.0)
    def test_shared_file_expires_at_ttl_boundary(self, mock_time):
        self.server.register_file("id1", str(self.file_path))

        mock_time.return_value = 1000.0 + FILE_SHARE_TTL_SECONDS - 0.001
        self.assertEqual(self.server.get_file_path("id1"), str(self.file_path))

        mock_time.return_value = 1000.0 + FILE_SHARE_TTL_SECONDS
        self.assertIsNone(self.server.get_file_path("id1"))
        self.assertNotIn("id1", self.server.shared_files)

    @patch('client.file_server.time.time', return_value=1000.0)
    def test_download_endpoint_rejects_expired_file(self, mock_time):
        self.server.register_file("id1", str(self.file_path))
        mock_time.return_value = 1000.0 + FILE_SHARE_TTL_SECONDS

        response = authed(self.server).get("/file/id1")

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.get_json()["message"], "file_id不存在")

    @patch('server.core.file_latest.time.time', return_value=1000.0 + FILE_SHARE_TTL_SECONDS)
    def test_client_rejects_expired_file_notification(self, _mock_time):
        expired_record = {
            "file_id": "id1",
            "path": str(self.file_path),
            "name": self.file_path.name,
            "size": self.file_path.stat().st_size,
            "source": "client-a",
            "ip": "192.168.1.10",
            "port": 8899,
            "updated_at": 1000.0,
        }
        self.latest_path.write_text(
            json.dumps([expired_record]),
            encoding="utf-8",
        )

        with patch('client.file_server.FILE_LATEST_FILE', self.latest_path):
            response = authed(self.server).post(
                "/update/current_latest",
                json={
                    "key": self.server.KEY,
                    "type": "file",
                    "latest_global": [expired_record],
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            json.loads(self.latest_path.read_text(encoding="utf-8")),
            [],
        )

    @patch('client.file_server.time.time', return_value=1000.0)
    def test_manifest_and_member_requests_renew_folder_share(self, mock_time):
        """逐文件下载不能被固定 TTL 腰斩：成功请求把窗口往后推。"""
        folder = Path(self.temp_dir.name) / "shared_dir"
        folder.mkdir()
        (folder / "a.txt").write_text("a", encoding="utf-8")
        self.server.register_folder("f1", str(folder), name="shared_dir")

        # 快到期时取清单：登记时间被刷新
        almost_expired = 1000.0 + FILE_SHARE_TTL_SECONDS - 1
        mock_time.return_value = almost_expired
        client = authed(self.server)
        response = client.get("/manifest/f1")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.server.shared_file_registered_at["f1"], almost_expired)

        # 原始 TTL 已过，但仍在续期窗口内：目录成员文件还能取到
        mock_time.return_value = 1000.0 + FILE_SHARE_TTL_SECONDS + 10
        response = client.get("/file/f1/a.txt")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, b"a")

    @patch('client.file_server.time.time', return_value=1000.0)
    def test_expired_folder_is_not_revived_by_manifest(self, mock_time):
        """续期不能让「首次获取」也无限期有效：过期请求仍应 404。"""
        folder = Path(self.temp_dir.name) / "shared_dir"
        folder.mkdir()
        (folder / "a.txt").write_text("a", encoding="utf-8")
        self.server.register_folder("f1", str(folder), name="shared_dir")

        mock_time.return_value = 1000.0 + FILE_SHARE_TTL_SECONDS
        response = authed(self.server).get("/manifest/f1")

        self.assertEqual(response.status_code, 404)
        self.assertNotIn("f1", self.server.shared_files)
        self.assertNotIn("f1", self.server.shared_folders)


if __name__ == '__main__':
    unittest.main()
