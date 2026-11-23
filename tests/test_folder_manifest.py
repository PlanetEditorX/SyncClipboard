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


def authed(server):
    """统一契约：FileServer 的每个请求都要带 key 请求头。"""
    client = server.app.test_client()
    client.environ_base["HTTP_KEY"] = server.KEY
    return client


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
        client = authed(self._server())
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
        client = authed(server)
        self.assertEqual(client.get("/manifest/f1").status_code, 404)
        self.assertEqual(client.get("/manifest/nope").status_code, 404)

    def test_download_member_returns_content(self):
        client = authed(self._server())
        resp = client.get("/file/d1/子目录/a.txt")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data, b"A")

    def test_download_member_blocks_traversal(self):
        client = authed(self._server())
        for path in ("/file/d1/../secret.txt",
                     "/file/d1/..%2Fsecret.txt",
                     "/file/d1/子目录/../../secret.txt"):
            resp = client.get(path)
            self.assertIn(resp.status_code, (404, 308), path)
            if resp.status_code == 200:
                self.assertNotIn(b"SECRET", resp.data, path)

    def test_download_member_missing_file_is_404(self):
        client = authed(self._server())
        self.assertEqual(client.get("/file/d1/没有这个文件.txt").status_code, 404)


class ManifestShareKeptTest(_FolderCase):
    """逐文件下载后共享必须还在 —— 撤销由接收端用完/到期负责。"""

    def test_share_survives_member_downloads(self):
        server = self._server()
        client = authed(server)
        client.get("/file/d1/readme.txt")
        client.get("/file/d1/子目录/a.txt")
        with server._shared_files_lock:
            self.assertIn("d1", server.shared_files)
            self.assertIn("d1", server.shared_folders)


if __name__ == "__main__":
    unittest.main()


class BatchManifestTest(_FolderCase):
    """合并清单：把「目录 → 条目」摊平成一层，接收端只跑一层循环。

    这是 v1.12.5 的关键改动。iOS 的「重复每一项」嵌两层时，内层拿到的
    Repeat Item 是空的（真机现象：内层取 url 得空，报「未指定 URL」），
    所以服务端必须先把多目录摊平，手机上才不会有第二层循环。
    """

    def setUp(self):
        super().setUp()
        # 第二个目录，名字不同，用来验证前缀不会撞车
        self.folder2 = self.root / "文档"
        (self.folder2 / "第一章").mkdir(parents=True)
        (self.folder2 / "讲义.txt").write_text("L", encoding="utf-8")
        (self.folder2 / "第一章" / "作业.txt").write_text("H", encoding="utf-8")

    def _two_servers(self):
        server = FileServer(local_name="client-a", key="secret")
        server.register_folder("d1", str(self.folder), size=1)
        server.register_folder("d2", str(self.folder2), size=1)
        return server

    def test_dirs_are_prefixed_with_root(self):
        body = authed(self._two_servers()).get(
            "/manifest/batch?ids=d1,d2").get_json()
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["roots"], ["课程", "文档"])
        self.assertEqual(body["count"], 5)
        dirs = {e["path"]: e["dir"] for e in body["entries"]}
        # 根目录下文件也带前缀 —— 接收端拿 dir 直接当子路径，不需要再拼接
        self.assertEqual(dirs["readme.txt"], "课程/")
        self.assertEqual(dirs["子目录/a.txt"], "课程/子目录/")
        self.assertEqual(dirs["讲义.txt"], "文档/")
        self.assertEqual(dirs["第一章/作业.txt"], "文档/第一章/")
        # 每个 dir 都以 "/" 结尾，否则 iOS 会把它当文件名
        for dir_value in dirs.values():
            self.assertTrue(dir_value.endswith("/"), dir_value)

    def test_prefix_is_dir_without_trailing_slash(self):
        """prefix 是 dir 去掉末尾斜杠的版本，供快捷指令拼「prefix/文件名」。

        iOS 的「存储到文件」会对子路径做规范化，变量值结尾的 "/" 保不住 ——
        v6 真机就是因此把 `目录名/` 当成文件名，所有文件互相覆盖，最后只剩
        一个叫目录名的文件。所以斜杠必须由快捷指令里的字面文本提供。
        """
        body = authed(self._two_servers()).get(
            "/manifest/batch?ids=d1,d2").get_json()
        for entry in body["entries"]:
            prefix = entry["prefix"]
            self.assertTrue(prefix, entry)
            self.assertFalse(prefix.endswith("/"), prefix)
            self.assertEqual(prefix, entry["dir"].rstrip("/"), entry)
            # prefix + "/" + name 必须正好是「根名/相对路径」
            root = prefix.split("/")[0]
            self.assertEqual(f"{prefix}/{entry['name']}",
                             f"{root}/{entry['path']}", entry)

    def test_entries_carry_progress_index(self):
        """每条带 index/total，接收端据此弹「正在下载 第 3/10 个」。

        编号由服务端算而不是让快捷指令自己累加：iOS 里做计数得靠「计算」动作
        维护变量，多一个动作就多一个真机上可能出错的地方，而这里的编号是确定
        的、可以测试的。两个目录合并后序号必须连续，不能各自从 1 开始。
        """
        body = authed(self._two_servers()).get(
            "/manifest/batch?ids=d1,d2").get_json()
        total = body["count"]
        indexes = [e["index"] for e in body["entries"]]
        self.assertEqual(indexes, list(range(1, total + 1)))
        for entry in body["entries"]:
            self.assertEqual(entry["total"], total, entry)

    def test_empty_dirs_keep_prefix(self):
        body = authed(self._two_servers()).get(
            "/manifest/batch?ids=d1").get_json()
        self.assertEqual(body["empty_dirs"], ["课程/空目录/"])

    def test_urls_downloadable_and_point_at_own_folder(self):
        server = self._two_servers()
        body = authed(server).get(
            "/manifest/batch?ids=d1,d2").get_json()
        by_name = {e["name"]: e for e in body["entries"]}
        # url 里的 file_id 必须是它自己那个目录，串了就会拿到别的目录的文件
        self.assertIn("/file/d2/", by_name["讲义.txt"]["url"])
        self.assertIn("/file/d1/", by_name["readme.txt"]["url"])
        resp = authed(server).get(
            "/file/d2/第一章/作业.txt")
        self.assertEqual(resp.get_data(), b"H")
        resp.close()

    def test_unknown_id_is_ignored(self):
        body = authed(self._two_servers()).get(
            "/manifest/batch?ids=d1,nope").get_json()
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["roots"], ["课程"])
        self.assertEqual(body["count"], 3)

    def test_all_ids_unknown_is_404(self):
        resp = authed(self._two_servers()).get(
            "/manifest/batch?ids=nope,nada")
        self.assertEqual(resp.status_code, 404)

    def test_missing_ids_is_400(self):
        resp = authed(self._two_servers()).get("/manifest/batch")
        self.assertEqual(resp.status_code, 400)

    def test_duplicate_ids_not_double_counted(self):
        body = authed(self._two_servers()).get(
            "/manifest/batch?ids=d1,d1").get_json()
        self.assertEqual(body["count"], 3)
        self.assertEqual(body["roots"], ["课程"])

    def test_over_limit_truncates_with_notice(self):
        from client import file_server as fs
        server = self._two_servers()
        # file_server 是值导入，得补丁它自己那份绑定
        with patch.object(fs, "MANIFEST_MAX_ENTRIES", 2):
            body = authed(server).get(
                "/manifest/batch?ids=d1,d2").get_json()
        self.assertEqual(body["count"], 2)
        self.assertTrue(body["truncated"])
        self.assertIn("只传前 2 个", body["notice"])
