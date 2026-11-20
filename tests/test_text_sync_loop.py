import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from client.file_server import FileServer
from client.main_menu import SyncClient
from server.api import flask_app
from server.core.item_builder import build_text_item
from server.core.text_tracker import TextTracker


class TestTextSyncLoopPrevention(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.text_latest_file = Path(self.temp_dir.name) / "text_latest.json"
        self.latest_patcher = patch(
            'server.core.text_tracker.TEXT_LATEST_FILE',
            self.text_latest_file,
        )
        self.latest_patcher.start()

    def tearDown(self):
        self.latest_patcher.stop()
        self.temp_dir.cleanup()

    def _make_client(self, local_name="client-b"):
        file_server = FileServer(local_name=local_name, key="secret")
        sync_client = SyncClient(
            {
                "server_host": "127.0.0.1",
                "server_port": 8000,
                "key": "secret",
                "local_name": local_name,
            },
            file_server,
        )
        return file_server, sync_client

    def _make_server(self):
        """返回 (flask test_client, tracker, mocks dict)，环境与真实服务器一致。"""
        tracker = TextTracker()
        mocks = {}

        def start(name, new):
            patcher = patch.object(flask_app, name, new)
            mocks[name] = patcher.start()
            self.addCleanup(patcher.stop)

        start("KEY", "secret")
        start("tracker", tracker)
        start("clients", [])
        # new=unittest.mock.DEFAULT 时 patch 会自动生成 MagicMock
        for name in ("copy_text_to_clipboard", "notify_clients", "load_clients_ip"):
            patcher = patch.object(flask_app, name)
            mocks[name] = patcher.start()
            self.addCleanup(patcher.stop)
        return flask_app.app.test_client(), tracker, mocks

    def _push(self, client, content, source):
        response = client.post(
            "/text_sync",
            json={"key": "secret", "content": content, "source": source},
        )
        self.assertEqual(response.status_code, 200)
        return response.get_json()["status"]

    # ---------- 客户端：远程文字不应被再次上传 ----------

    @patch('client.main_menu.pyperclip.copy')
    def test_remote_push_updates_sync_client_state_and_is_not_reuploaded(self, mock_copy):
        file_server, sync_client = self._make_client()
        remote_item = build_text_item(
            "hello",
            "client-a",
            timestamp="2026-08-23T10:00:00",
        )

        response = file_server.app.test_client().post(
            "/update/current_latest",
            json={
                "key": "secret",
                "type": "text",
                "latest_global": remote_item,
            },
        )

        self.assertEqual(response.status_code, 200)
        mock_copy.assert_called_once_with("hello")
        self.assertEqual(sync_client.last_remote_id, remote_item["id"])
        self.assertEqual(sync_client.last_text, "hello")

        with patch.object(sync_client, 'safe_paste', return_value="hello"), \
                patch('client.main_menu.get_clipboard_sequence', return_value=None):
            self.assertIsNone(sync_client._get_local_text_to_push())

    # ---------- 事件 ID：同一内容每次复制都是新事件 ----------

    def test_text_item_fingerprint_is_stable_but_event_id_is_unique(self):
        first = build_text_item("hello", "client-a", timestamp="2026-08-23T10:00:00")
        retry = build_text_item("hello", "client-a", timestamp="2026-08-23T10:05:00")

        # 内容指纹稳定，回音/去重判定依赖它
        self.assertEqual(first["fingerprint"], retry["fingerprint"])
        # 但事件 ID 必须唯一，否则重复复制同一段文字会被去重吞掉
        self.assertNotEqual(first["id"], retry["id"])
        self.assertEqual(first["origin"], "client-a")

    # ---------- 回归：A → B → A ----------

    def test_recopy_same_text_after_other_content_is_broadcast_again(self):
        client, tracker, mocks = self._make_server()

        self.assertEqual(self._push(client, "A", "PC"), "ok")
        self.assertEqual(tracker.get_global_latest()["content"], "A")
        first_id = tracker.get_global_latest()["id"]

        self.assertEqual(self._push(client, "B", "PC"), "ok")
        self.assertEqual(tracker.get_global_latest()["content"], "B")

        # 用户重新复制 A：必须能再次广播，手机端才能重新拿到 A
        self.assertEqual(self._push(client, "A", "PC"), "ok")
        latest = tracker.get_global_latest()
        self.assertEqual(latest["content"], "A")
        self.assertNotEqual(latest["id"], first_id)
        self.assertEqual(mocks["notify_clients"].call_count, 3)

    def test_stale_echo_after_other_content_is_ignored(self):
        client, tracker, mocks = self._make_server()

        self._push(client, "A", "PC")          # PC 首发 A，回音票据待消耗
        self._push(client, "B", "PC")          # PC 又复制了 B

        # 手机这时才把更早的 A 回传，仍属于回音，不能把全局最新改回 A
        self.assertEqual(self._push(client, "A", "phone"), "ignored")
        self.assertEqual(tracker.get_global_latest()["content"], "B")
        self.assertEqual(mocks["notify_clients"].call_count, 2)

    def test_same_content_returned_by_another_client_is_ignored(self):
        client, tracker, mocks = self._make_server()
        self._push(client, "hello", "client-a")
        copy_calls = mocks["copy_text_to_clipboard"].call_count
        notify_calls = mocks["notify_clients"].call_count

        self.assertEqual(self._push(client, "hello", "client-b"), "ignored")
        self.assertEqual(mocks["copy_text_to_clipboard"].call_count, copy_calls)
        self.assertEqual(mocks["notify_clients"].call_count, notify_calls)
        self.assertEqual(tracker.get_global_latest()["source"], "client-a")

    def test_manual_copy_on_other_device_is_still_broadcast(self):
        """票据用完之后，其它设备再次复制同样内容属于手动操作，必须放行。"""
        client, tracker, mocks = self._make_server()

        self._push(client, "A", "PC")            # PC 首发 A，发放一张回音票据
        self.assertEqual(self._push(client, "A", "phone"), "ignored")  # 票据被消耗
        self._push(client, "B", "PC")            # 内容变化

        # 票据已耗尽 → 手机这次是真的手动复制了 A
        self.assertEqual(self._push(client, "A", "phone"), "ok")
        latest = tracker.get_global_latest()
        self.assertEqual(latest["content"], "A")
        self.assertEqual(latest["source"], "phone")
        self.assertEqual(latest["origin"], "phone")

    def test_new_copy_wins_even_if_device_clock_is_behind(self):
        """客户端时钟偏慢时，新复制的内容也必须顶到全局最新（按到达顺序为准）。"""
        client, tracker, mocks = self._make_server()

        self._push(client, "A", "PC")
        # 模拟另一台时钟偏快的设备写入了一条“未来”时间戳的内容
        future = build_text_item("future", "phone", timestamp="2099-01-01T00:00:00")
        tracker.update(future, force_latest=True)
        self.assertEqual(tracker.get_global_latest()["content"], "future")

        # PC 重新复制 A：时间戳比 future 小，但仍然是最新事件
        self.assertEqual(self._push(client, "A", "PC"), "ok")
        self.assertEqual(tracker.get_global_latest()["content"], "A")

    # ---------- 追踪器：回音票据语义 ----------

    def test_echo_ticket_is_single_use_and_skips_origin(self):
        tracker = TextTracker()
        tracker.register_echo("A", origin="PC", pending=1)

        # 首发者自己不算回音，也不消耗票据
        self.assertFalse(tracker.consume_echo("A", "PC"))
        # 其它设备第一次回传被吞掉
        self.assertTrue(tracker.consume_echo("A", "phone"))
        # 票据用完，再收到同样内容视为新的一次复制
        self.assertFalse(tracker.consume_echo("A", "phone"))

    def test_echo_ticket_expires(self):
        tracker = TextTracker()
        tracker.register_echo("A", origin="PC", pending=1)

        with patch('server.core.text_tracker.time.time', return_value=1e12):
            self.assertFalse(tracker.consume_echo("A", "phone"))


if __name__ == '__main__':
    unittest.main()
