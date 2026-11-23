"""共享下载次数 / 下载记录：按实际发出的字节判定「一次完整下载」。

接收端（GUI 与 iOS 快捷指令）不需要回报下载完成：字节全部由发送端本机
文件服务发出，在流式响应里逐 chunk 累加即可。普通文件发满整个文件、目录
清单里的每个成员都发满各自体积，才算一次完整下载；取清单本身不算。
"""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from client.file_server import FileServer
from gui import qt_main_window
from gui.main_window import validate_client_settings


def authed(server):
    client = server.app.test_client()
    client.environ_base["HTTP_KEY"] = server.KEY
    return client


def get(client, path, source):
    return client.get(path, headers={"source": source})


def fetch(client, path, source):
    """必须把响应体读完，流式生成器才会执行 finally 里的字节统计。"""
    resp = get(client, path, source)
    resp.get_data()
    return resp


class _ShareFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.plain_file = self.root / "shared.txt"
        self.plain_file.write_bytes(b"hello")           # 5 字节
        self.folder = self.root / "shared_dir"
        self.folder.mkdir()
        (self.folder / "one.txt").write_text("1", encoding="utf-8")     # 1 字节
        (self.folder / "sub").mkdir()
        (self.folder / "sub" / "two.txt").write_text("2", encoding="utf-8")  # 1 字节
        self.members = ("one.txt", "sub/two.txt")       # 合计 2 字节

    def download_all(self, client, file_id="d1", source="phone-a"):
        for rel in self.members:
            fetch(client, f"/file/{file_id}/{rel}", source)


class TestValidateDownloadLimit(unittest.TestCase):
    def _validate(self, value):
        return validate_client_settings(
            "host", "8000", "PC", "8899", "key", "D:/Downloads",
            share_download_limit=value,
        )["share_download_limit"]

    def test_zero_means_unlimited(self):
        self.assertEqual(self._validate(0), 0)
        self.assertEqual(self._validate("0"), 0)

    def test_positive_values_are_ints(self):
        self.assertEqual(self._validate("1"), 1)
        self.assertEqual(self._validate(99), 99)

    def test_out_of_range_is_rejected(self):
        for value in (-1, 100, "abc", None, ""):
            with self.assertRaises(ValueError):
                self._validate(value)


class TestCollectSyncSettings(unittest.TestCase):
    """面板开关关闭 = 0（不限制），开启时读数字框。"""

    @staticmethod
    def _window(cancel_on, limit_text="2"):
        edit = lambda text: MagicMock(**{"text.return_value": str(text)})
        switch = lambda on: MagicMock(**{"isChecked.return_value": bool(on)})
        return SimpleNamespace(
            folder_sync_switch=switch(True),
            folder_limit_edit=edit(10),
            folder_count_edit=edit(3000),
            share_ttl_edit=edit(30),
            share_auto_cancel_switch=switch(cancel_on),
            share_limit_edit=edit(limit_text),
            config=SimpleNamespace(
                text_sync_mode="both",
                file_sync_mode="both",
                folder_sync_enabled=True,
                folder_zip_confirm_gb=10,
                folder_file_count_confirm=3000,
                file_share_ttl_minutes=30,
                share_download_limit=0,
            ),
            _selected_mode=lambda buttons, fallback: fallback,
        )

    def test_switch_off_is_unlimited(self):
        values = qt_main_window.MainWindow._collect_sync_settings(
            self._window(False)
        )
        self.assertEqual(values["share_download_limit"], 0)

    def test_switch_on_reads_count(self):
        values = qt_main_window.MainWindow._collect_sync_settings(
            self._window(True, "3")
        )
        self.assertEqual(values["share_download_limit"], "3")


class TestByteAccounting(_ShareFixture):
    """发送端按实际 yield 的字节统计进度，不依赖接收端回报。"""

    def test_plain_file_bytes_are_counted(self):
        server = FileServer(key="k", download_limit=0)
        server.register_file("f1", str(self.plain_file))
        client = authed(server)
        self.assertEqual(fetch(client, "/file/f1", "phone-a").get_data(), b"hello")

        payload = get(client, "/downloads", "gui").get_json()
        completed = [r for r in payload["records"] if r["event"] == "completed"]
        self.assertEqual(len(completed), 1)
        self.assertEqual(completed[0]["bytes"], 5)
        self.assertEqual(completed[0]["sent"], 5)
        self.assertEqual(completed[0]["total"], 5)
        self.assertEqual(completed[0]["percent"], 100)
        self.assertEqual(payload["bytes_sent_total"], 5)
        self.assertEqual(payload["completed_downloads"], 1)

    def test_progress_session_tracks_partial_directory(self):
        server = FileServer(key="k", download_limit=0)
        server.register_folder("d1", str(self.folder), name="shared_dir")
        client = authed(server)
        get(client, "/manifest/d1", "phone-a")
        fetch(client, "/file/d1/one.txt", "phone-a")

        payload = get(client, "/downloads", "gui").get_json()
        session = payload["sessions"][0]
        self.assertEqual(session["sent"], 1)
        self.assertEqual(session["total"], 2)
        self.assertEqual(session["percent"], 50)
        self.assertEqual((session["parts"], session["files"]), (1, 2))
        self.assertEqual(session["state"], "active")

        fetch(client, "/file/d1/sub/two.txt", "phone-a")
        payload = get(client, "/downloads", "gui").get_json()
        session = payload["sessions"][0]
        self.assertEqual((session["sent"], session["total"]), (2, 2))
        self.assertEqual(session["state"], "completed")
        self.assertEqual(payload["bytes_sent_total"], 2)

    def test_repeated_member_does_not_inflate_progress(self):
        """重试 / 断点续传按成员取 max，不会把 1 字节算成 2 字节。"""
        server = FileServer(key="k", download_limit=0)
        server.register_folder("d1", str(self.folder), name="shared_dir")
        client = authed(server)
        get(client, "/manifest/d1", "phone-a")
        fetch(client, "/file/d1/one.txt", "phone-a")
        fetch(client, "/file/d1/one.txt", "phone-a")
        self.assertEqual(
            get(client, "/downloads", "gui").get_json()["sessions"][0]["sent"], 1
        )

    def test_manifest_alone_is_not_a_completed_download(self):
        server = FileServer(key="k", download_limit=1)
        server.register_folder("d1", str(self.folder), name="shared_dir")
        client = authed(server)
        self.assertEqual(get(client, "/manifest/d1", "phone-a").status_code, 200)
        self.assertIn("d1", server.shared_files)
        self.assertFalse(server._is_exhausted("d1"))
        events = [r["event"] for r in server._download_records]
        self.assertIn("manifest", events)
        self.assertNotIn("completed", events)

    def test_empty_folder_completes_when_manifest_is_delivered(self):
        empty = self.root / "empty_dir"
        empty.mkdir()
        server = FileServer(key="k", download_limit=1)
        server.register_folder("d1", str(empty), name="empty_dir")
        client = authed(server)
        self.assertEqual(get(client, "/manifest/d1", "phone-a").status_code, 200)
        # 没有成员文件可下，取完清单就是一次完整下载
        self.assertNotIn("d1", server.shared_files)
        self.assertIn("completed", [r["event"] for r in server._download_records])

    def test_batch_manifest_counts_only_listed_members(self):
        server = FileServer(key="k", download_limit=1)
        server.register_folder("d1", str(self.folder), name="shared_dir")
        client = authed(server)
        resp = get(client, "/manifest/batch?ids=d1", "phone-a")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["count"], 2)
        self.assertEqual(resp.get_json()["total_size"], 2)
        self.assertEqual(server._share_expected["d1"],
                         {"one.txt": 1, "sub/two.txt": 1})
        self.assertIn("d1", server.shared_files)
        self.download_all(client)
        self.assertNotIn("d1", server.shared_files)


class TestUnlimitedShare(_ShareFixture):
    def test_plain_file_stays_registered_for_multiple_clients(self):
        server = FileServer(key="k", download_limit=0)
        server.register_file("f1", str(self.plain_file))
        client = authed(server)

        self.assertEqual(fetch(client, "/file/f1", "phone-a").get_data(), b"hello")
        self.assertEqual(fetch(client, "/file/f1", "phone-b").get_data(), b"hello")
        # 不限制时普通文件不再「下完一次就撤登记」，与目录统一由 TTL 管理
        self.assertIn("f1", server.shared_files)
        self.assertEqual(len(server._share_claims["f1"]), 2)
        self.assertEqual(
            get(client, "/downloads", "gui").get_json()["bytes_sent_total"], 10
        )

    def test_unlimited_member_download_without_manifest_still_works(self):
        """老客户端直接拼成员 URL 的兼容路径，不限制时保持可用。"""
        server = FileServer(key="k", download_limit=0)
        server.register_folder("d1", str(self.folder), name="shared_dir")
        resp = get(authed(server), "/file/d1/one.txt", "phone-x")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_data(), b"1")

    def test_records_are_written_even_without_limit(self):
        server = FileServer(key="k", download_limit=0)
        server.register_file("f1", str(self.plain_file))
        client = authed(server)
        fetch(client, "/file/f1", "phone-a")

        payload = get(client, "/downloads", "gui").get_json()
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["limit"], 0)
        events = [item["event"] for item in payload["records"]]
        self.assertIn("started", events)
        self.assertIn("completed", events)
        self.assertTrue(all(item["client"] == "phone-a@127.0.0.1"
                            for item in payload["records"] if item["event"] != "cancelled"))


class TestLimitedShare(_ShareFixture):
    def test_first_complete_download_cancels_plain_file_share(self):
        server = FileServer(key="k", download_limit=1)
        server.register_file("f1", str(self.plain_file))
        client = authed(server)

        first = get(client, "/file/f1", "phone-a")
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.get_data(), b"hello")
        self.assertNotIn("f1", server.shared_files)
        self.assertEqual(get(client, "/file/f1", "phone-b").status_code, 404)

        records = list(server._download_records)
        self.assertIn("started", [r["event"] for r in records])
        self.assertIn("completed", [r["event"] for r in records])
        self.assertIn("cancelled", [r["event"] for r in records])

    def test_new_client_is_blocked_while_first_is_downloading(self):
        """名额在「开始下载」时预留，避免两个设备同时下完造成超发。"""
        server = FileServer(key="k", download_limit=1)
        server.register_folder("d1", str(self.folder), name="shared_dir")
        client = authed(server)

        self.assertEqual(get(client, "/manifest/d1", "phone-a").status_code, 200)
        blocked = get(client, "/manifest/d1", "phone-b")
        self.assertEqual(blocked.status_code, 410)
        self.assertIn("下载中或已完成", blocked.get_json()["message"])
        self.assertIn("blocked", [r["event"] for r in server._download_records])
        # 只是拒绝新设备，A 还没下完，共享不能提前取消
        self.assertIn("d1", server.shared_files)

    def test_same_client_retry_does_not_consume_another_slot(self):
        server = FileServer(key="k", download_limit=1)
        server.register_folder("d1", str(self.folder), name="shared_dir")
        client = authed(server)

        self.assertEqual(get(client, "/manifest/d1", "phone-a").status_code, 200)
        self.assertEqual(get(client, "/manifest/d1", "phone-a").status_code, 200)
        self.assertEqual(get(client, "/manifest/d1", "phone-b").status_code, 410)
        self.assertEqual(len(server._share_claims["d1"]), 1)
        started = [r for r in server._download_records if r["event"] == "started"]
        self.assertEqual(len(started), 1)

    def test_partial_directory_does_not_cancel_share(self):
        server = FileServer(key="k", download_limit=1)
        server.register_folder("d1", str(self.folder), name="shared_dir")
        client = authed(server)
        get(client, "/manifest/d1", "phone-a")
        fetch(client, "/file/d1/one.txt", "phone-a")

        self.assertIn("d1", server.shared_files)
        self.assertNotIn("completed",
                         [r["event"] for r in server._download_records])
        # 最后一个成员发满才判完成，并立即撤销登记
        fetch(client, "/file/d1/sub/two.txt", "phone-a")
        self.assertNotIn("d1", server.shared_files)
        completed = [r for r in server._download_records if r["event"] == "completed"]
        self.assertEqual(len(completed), 1)
        self.assertEqual((completed[0]["sent"], completed[0]["total"]), (2, 2))
        self.assertIn("2 B / 2 B", completed[0]["detail"])

    def test_member_requests_do_not_count_as_new_downloads(self):
        server = FileServer(key="k", download_limit=1)
        server.register_folder("d1", str(self.folder), name="shared_dir")
        client = authed(server)

        get(client, "/manifest/d1", "phone-a")
        self.download_all(client)
        started = [r for r in server._download_records if r["event"] == "started"]
        completed = [r for r in server._download_records if r["event"] == "completed"]
        self.assertEqual(len(started), 1, "目录成员下载不能重复计次")
        self.assertEqual(len(completed), 1)
        self.assertNotIn("d1", server.shared_files)

    def test_completed_share_is_not_reannounced(self):
        server = FileServer(key="k", download_limit=1)
        server.register_folder("d1", str(self.folder), name="shared_dir")
        client = authed(server)
        get(client, "/manifest/d1", "phone-a")
        self.download_all(client)
        self.assertEqual(server.share_entries(), [])

    def test_unclaimed_client_cannot_fetch_members_directly(self):
        server = FileServer(key="k", download_limit=1)
        server.register_folder("d1", str(self.folder), name="shared_dir")
        client = authed(server)
        self.assertEqual(get(client, "/manifest/d1", "phone-a").status_code, 200)

        blocked = get(client, "/file/d1/one.txt", "phone-b")
        self.assertEqual(blocked.status_code, 410)
        claimed = get(client, "/file/d1/one.txt", "phone-a")
        self.assertEqual(claimed.status_code, 200)
        claimed.get_data()

    def test_dead_routes_do_not_consume_quota(self):
        server = FileServer(key="k", download_limit=1)
        server.register_folder("d1", str(self.folder), name="shared_dir")
        server.register_file("f1", str(self.plain_file))
        client = authed(server)

        # 目录的 /file/<id> 是 409 死链；普通文件的 /manifest 会 404
        self.assertEqual(get(client, "/file/d1", "phone-a").status_code, 409)
        self.assertEqual(get(client, "/manifest/f1", "phone-a").status_code, 404)
        self.assertEqual(server._share_claims.get("d1", {}), {})
        self.assertEqual(server._share_claims.get("f1", {}), {})

    def test_download_records_endpoint_requires_key(self):
        server = FileServer(key="k", download_limit=1)
        self.assertEqual(server.app.test_client().get("/downloads").status_code, 403)

    def test_limit_two_allows_two_devices(self):
        server = FileServer(key="k", download_limit=2)
        server.register_folder("d1", str(self.folder), name="shared_dir")
        client = authed(server)
        self.assertEqual(get(client, "/manifest/d1", "phone-a").status_code, 200)
        self.download_all(client, source="phone-a")
        self.assertFalse(server._is_exhausted("d1"))
        self.assertIn("d1", server.shared_files)

        self.assertEqual(get(client, "/manifest/d1", "phone-b").status_code, 200)
        self.download_all(client, source="phone-b")
        self.assertNotIn("d1", server.shared_files)
        completed = [r for r in server._download_records if r["event"] == "completed"]
        self.assertEqual(len(completed), 2)

    def test_completed_events_carry_byte_totals(self):
        server = FileServer(key="k", download_limit=1)
        server.register_folder("d1", str(self.folder), name="shared_dir")
        client = authed(server)
        get(client, "/manifest/d1", "phone-a")
        self.download_all(client)
        payload = get(client, "/downloads", "gui").get_json()
        self.assertEqual(payload["completed_downloads"], 1)
        self.assertEqual(payload["bytes_sent_total"], 2)
        # 共享已撤销，会话清了，但事件记录仍保留已发字节
        self.assertEqual(payload["sessions"], [])
        completed = [r for r in payload["records"] if r["event"] == "completed"][0]
        self.assertEqual((completed["sent"], completed["total"]), (2, 2))


if __name__ == '__main__':
    unittest.main()
