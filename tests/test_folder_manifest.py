"""目录清单（逐文件传输）的回归测试。

覆盖两件事：
1. 清单本身的形状 —— iOS 端直接拿 `dir` 当「存储到文件」的子路径，所以
   `dir` 必须**以 "/" 结尾**、根目录下必须是空串。写错这两点，手机上是
   「看着跑完了、目录还是平的」。
2. 单个文件的下载必须限死在共享目录内，`../` 不能穿出去。
"""


import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from client.file_server import FileServer
from client.folder_manifest import build_manifest


class _FolderCase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)

        self.folder = self.root / "课程"
        (self.folder / "子目录" / "更内层").mkdir(parents=True)
        (self.folder / "空目录").mkdir()
        (self.folder / "readme.txt").write_text("root", encoding="utf-8")
        (self.folder / "子目录" / "a.txt").write_text("A", encoding="utf-8")
        (self.folder / "子目录" / "更内层" / "b.txt").write_text("BB", encoding="utf-8")
        # 目录之外的文件，用来验证路径穿越被拦住
        self.outside = self.root / "secret.txt"
        self.outside.write_text("SECRET", encoding="utf-8")

    def _server(self):
        server = FileServer(local_name="client-a", key="secret")
        server.register_folder("d1", str(self.folder), size=999)
        return server


class ManifestShapeTest(_FolderCase):
    def test_dir_field_ends_with_slash(self):
        m = build_manifest(str(self.folder))
        by_path = {e["path"]: e for e in m["entries"]}
        # 根目录下的文件：dir 是空串（而不是 "." 或 "/"）
        self.assertEqual(by_path["readme.txt"]["dir"], "")
        self.assertEqual(by_path["readme.txt"]["name"], "readme.txt")
        # 子目录：dir 以 "/" 结尾，path = dir + name
        self.assertEqual(by_path["子目录/a.txt"]["dir"], "子目录/")
        self.assertEqual(by_path["子目录/更内层/b.txt"]["dir"], "子目录/更内层/")
        self.assertEqual(by_path["子目录/更内层/b.txt"]["path"],
                         "子目录/更内层/b.txt")

    def test_root_and_counts(self):
        m = build_manifest(str(self.folder))
        self.assertEqual(m["root"], "课程")
        self.assertEqual(m["count"], 3)
        self.assertEqual(m["total_size"], 4 + 1 + 2)   # root / A / BB
        self.assertEqual(m["status"], "ok")
        self.assertFalse(m["truncated"])
        self.assertEqual(m["notice"], "")
        # 空目录单列，接收端据此提示；不能混进 entries（没有文件可写）
        self.assertIn("空目录/", m["empty_dirs"])
        self.assertNotIn("空目录/", [e["dir"] for e in m["entries"]])

    def test_missing_folder_returns_none(self):
        self.assertIsNone(build_manifest(str(self.root / "不存在")))

    def test_truncate_keeps_notice(self):
        m = build_manifest(str(self.folder), max_entries=1)
        self.assertEqual(m["count"], 1)
        self.assertTrue(m["truncated"])
        self.assertIn("只传前", m["notice"])


class ManifestHttpTest(_FolderCase):
    def test_manifest_lists_absolute_urls(self):
        client = self._server().app.test_client()
        resp = client.get("/manifest/d1")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["root"], "课程")
        self.assertEqual(body["file_id"], "d1")
        urls = {e["path"]: e["url"] for e in body["entries"]}
        # 路径按段百分号编码后再拼进 URL（空格、中文都得编）
        self.assertIn("/file/d1/%E5%AD%90%E7%9B%AE%E5%BD%95/a.txt",
                      urls["子目录/a.txt"])
        self.assertTrue(urls["readme.txt"].startswith("http://"))

    def test_manifest_rejects_non_folder_share(self):
        server = self._server()
        note = self.root / "note.txt"
        note.write_text("hi", encoding="utf-8")
        server.register_file("f1", str(note))
        client = server.app.test_client()
        self.assertEqual(client.get("/manifest/f1").status_code, 404)
        self.assertEqual(client.get("/manifest/nope").status_code, 404)

    def test_download_member_returns_content(self):
        client = self._server().app.test_client()
        resp = client.get("/file/d1/子目录/a.txt")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data, b"A")

    def test_download_member_blocks_traversal(self):
        client = self._server().app.test_client()
        for path in ("/file/d1/../secret.txt",
                     "/file/d1/..%2Fsecret.txt",
                     "/file/d1/子目录/../../secret.txt"):
            resp = client.get(path)
            self.assertIn(resp.status_code, (404, 308), path)
            if resp.status_code == 200:
                self.assertNotIn(b"SECRET", resp.data, path)

    def test_download_member_missing_file_is_404(self):
        client = self._server().app.test_client()
        self.assertEqual(client.get("/file/d1/没有这个文件.txt").status_code, 404)


class ManifestShareKeptTest(_FolderCase):
    """逐文件下载后共享必须还在 —— 撤销由接收端用完/到期负责。"""

    def test_share_survives_member_downloads(self):
        server = self._server()
        client = server.app.test_client()
        client.get("/file/d1/readme.txt")
        client.get("/file/d1/子目录/a.txt")
        with server._shared_files_lock:
            self.assertIn("d1", server.shared_files)
            self.assertIn("d1", server.shared_folders)


if __name__ == "__main__":
    unittest.main()
