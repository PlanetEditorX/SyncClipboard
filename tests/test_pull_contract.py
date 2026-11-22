"""三端拉取契约的回归测试。

覆盖三处「服务端 / 客户端 / 接收端」容易各自演化走偏的地方：

1. `/request_file` 的文本分支必须复用 `_take_ios_latest` 判重，否则同一段
   文本会被反复投递 —— 手机上连跑两次快捷指令，第二次同样返回完整内容，
   再把用户刚复制的东西覆盖掉。
2. `/file_sync` 收到 `port: null` 时不能原样落库：`dict.get` 在「键存在、
   值为 None」时不回落默认值，记录会以 port=None 存下，`/request_file`
   再把每条都跳过，文件静默消失。
3. 下载中断后客户端保留压缩包，但服务端的记录已被清空，需要重新登记一次
   共享，接收端才能直接重试。
"""

import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from client import file_server as file_server_mod
from client.file_server import FileServer
from client.main_menu import SyncClient
from server.api import flask_app

KEY = "secret"
DEVICE = "熊熊的iPhone"
DESKTOP = "DESKTOP-TEST"


class _FakeLatestFile:
    def __init__(self, records=None):
        self.records = list(records or [])
        self.cleared = 0

    def get_all_files(self):
        return list(self.records)

    def clear(self):
        self.cleared += 1
        self.records = []

    def upsert_file(self, **kwargs):
        self.records.append(dict(kwargs))

    def _save(self):
        pass


class _FakeTracker:
    """够用即可以的 TextTracker 替身：_take_ios_latest 只用到这几个成员。"""

    def __init__(self, latest=None):
        self.latest = latest
        self.data = {"clients": {}, "latest_global": latest}

    def get_global_latest(self):
        return dict(self.latest) if self.latest else None

    def mark_pasted(self, source, item):
        self.data.setdefault("clients", {})[source] = dict(item)

    def note_client_hold(self, source, content):
        self.data["clients"].setdefault(source, {})["content"] = content


def _patch_flask(**values):
    patchers = []
    for name, value in values.items():
        patcher = patch.object(flask_app, name, value)
        patcher.start()
        patchers.append(patcher)
    return patchers


class _FlaskCase(unittest.TestCase):
    def setUp(self):
        self.client = flask_app.app.test_client()
        self.latest_file = _FakeLatestFile()
        self.tracker = _FakeTracker()
        self._patchers = _patch_flask(
            KEY=KEY, latest_file=self.latest_file, tracker=self.tracker,
            touch_client=MagicMock(), load_clients_ip=MagicMock(),
            notify_clients=MagicMock(),
        )
        ios = patch.dict(flask_app.IOS_NOTIFY, {}, clear=True)
        ios.start()
        self._patchers.append(ios)

    def tearDown(self):
        for patcher in self._patchers:
            patcher.stop()

    def _pull(self, source=DEVICE, key=KEY):
        return self.client.post(
            "/request_file", headers={"key": key}, json={"source": source}
        ).get_json()

    def _push(self, file_list, port):
        return self.client.post(
            "/file_sync",
            headers={"key": KEY},
            json={"file_list": file_list, "source": DESKTOP, "port": port},
        )


class TestTextDeliveryDedup(_FlaskCase):
    """同一段文本只投递一次，避免反复覆盖手机剪贴板。"""

    def setUp(self):
        super().setUp()
        self.tracker.latest = {
            "id": "t1", "type": "text", "content": "刚复制的内容", "source": DESKTOP,
        }
        self.tracker.data["latest_global"] = self.tracker.latest

    def test_first_pull_delivers_text(self):
        payload = self._pull()
        self.assertEqual(payload["type"], "text")
        self.assertEqual(payload["latest_global"]["content"], "刚复制的内容")
        self.assertFalse(payload["latest_global"]["pasted"])

    def test_second_pull_returns_empty(self):
        self._pull()
        payload = self._pull()
        self.assertEqual(payload["type"], "text")
        # 这台设备手上已经有一模一样的内容了 → 空对象，快捷指令什么都不做
        self.assertTrue(payload["latest_global"]["pasted"])
        self.assertNotIn("content", payload["latest_global"])

    def test_own_text_is_not_echoed_back(self):
        self.tracker.latest["source"] = DEVICE
        payload = self._pull()
        self.assertTrue(payload["latest_global"]["pasted"])
        self.assertNotIn("content", payload["latest_global"])

    def test_no_source_returns_empty(self):
        payload = self._pull(source="")
        self.assertTrue(payload["latest_global"]["pasted"])


class TestFileSyncPortFallback(_FlaskCase):
    """port 为 null / 0 时按默认端口落库，别让整批文件静默消失。"""

    def _record(self, file_id="f1", name="note.txt", port=None):
        return {"file_id": file_id, "path": "D:/Downloads/note.txt",
                "name": name, "size": 12, "kind": "file"}

    def test_null_port_falls_back_to_default(self):
        resp = self._push([self._record()], port=None)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.latest_file.records[0]["port"], flask_app.DEFAULT_FILE_PORT)

    def test_missing_port_falls_back_to_default(self):
        self.client.post("/file_sync", headers={"key": KEY},
                         json={"file_list": [self._record()], "source": DESKTOP})
        self.assertEqual(self.latest_file.records[0]["port"], flask_app.DEFAULT_FILE_PORT)

    def test_zero_port_falls_back_to_default(self):
        self._push([self._record()], port=0)
        self.assertEqual(self.latest_file.records[0]["port"], flask_app.DEFAULT_FILE_PORT)

    def test_explicit_port_is_kept(self):
        self._push([self._record()], port=9100)
        self.assertEqual(self.latest_file.records[0]["port"], 9100)

    def test_null_port_file_is_still_deliverable(self):
        """回归：曾经因为 port=None，/request_file 把这条记录整个跳过。"""
        self._push([self._record()], port=None)
        payload = self._pull()
        self.assertEqual(payload["type"], "file_list")
        self.assertEqual(len(payload["files"]), 1)
        self.assertEqual(
            payload["files"][0]["download_url"],
            f"http://127.0.0.1:{flask_app.DEFAULT_FILE_PORT}/file/f1",
        )


class TestShareReannounce(unittest.TestCase):
    """下载中断后把仍留着的共享重新登记，让接收端能直接重试。"""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        patcher = patch.object(
            file_server_mod, "FOLDER_SHARE_TEMP_DIR", self.root / "share"
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.folder = self.root / "课程"
        self.folder.mkdir()
        (self.folder / "a.txt").write_text("x", encoding="utf-8")
        self.file_path = self.root / "note.txt"
        self.file_path.write_text("hello", encoding="utf-8")

    def _server(self):
        return FileServer(local_name="client-a", key="secret")

    def test_share_entries_cover_file_and_folder(self):
        server = self._server()
        server.register_file("f1", str(self.file_path))
        server.register_folder("d1", str(self.folder), "课程.zip", size=123)

        entries = {item["file_id"]: item for item in server.share_entries()}
        self.assertEqual(entries["f1"]["kind"], "file")
        self.assertEqual(entries["f1"]["name"], "note.txt")
        self.assertEqual(entries["f1"]["size"], 5)
        self.assertEqual(entries["d1"]["kind"], "folder")
        self.assertEqual(entries["d1"]["name"], "课程.zip")
        self.assertEqual(entries["d1"]["size"], 123)
        # 目录上报原始路径，不能把临时压缩包路径泄给服务端
        self.assertEqual(entries["d1"]["path"], str(self.folder))

    def test_download_failure_triggers_reannounce(self):
        server = self._server()
        fired = threading.Event()
        server.set_reannounce_handler(fired.set)
        server.register_file("f1", str(self.file_path))

        server._keep_for_retry("f1", str(self.file_path))

        self.assertTrue(fired.wait(2), "下载中断应当触发重新登记")

    def test_no_handler_means_no_crash(self):
        server = self._server()
        server.register_file("f1", str(self.file_path))
        server._keep_for_retry("f1", str(self.file_path))   # 不应抛异常

    def test_reannounce_posts_current_shares(self):
        server = self._server()
        server.register_file("f1", str(self.file_path))
        client = SyncClient({
            "server_host": "127.0.0.1", "server_port": 8000,
            "key": "secret", "local_name": "client-a", "file_server_port": 8899,
        }, server)

        with patch("client.main_menu.requests.post") as post:
            post.return_value = MagicMock(status_code=200, text="")
            client.reannounce_shares()

        url, kwargs = post.call_args[0][0], post.call_args[1]
        self.assertTrue(url.endswith("/file_sync"))
        payload = kwargs["json"]
        self.assertEqual(payload["port"], 8899)
        self.assertEqual([item["file_id"] for item in payload["file_list"]], ["f1"])

    def test_reannounce_skips_when_nothing_shared(self):
        client = SyncClient({
            "server_host": "127.0.0.1", "server_port": 8000,
            "key": "secret", "local_name": "client-a",
        }, self._server())

        with patch("client.main_menu.requests.post") as post:
            client.reannounce_shares()

        post.assert_not_called()

    def test_reannounce_respects_send_gate(self):
        server = self._server()
        server.register_file("f1", str(self.file_path))
        client = SyncClient({
            "server_host": "127.0.0.1", "server_port": 8000,
            "key": "secret", "local_name": "client-a", "file_sync_mode": "receive",
        }, server)

        with patch("client.main_menu.requests.post") as post:
            client.reannounce_shares()

        post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
