"""服务器派发到本机客户端时的通知。

背景：手机推给「本机服务器」时，GUI 通过监控 latest/text_latest.json 弹
「✂️ 剪贴板更新」；但服务器再把内容派发到另一台只跑客户端的电脑时，那条
链路原来只是静默写剪贴板，用户完全看不到提示。这里新增了面板开关
``notify_remote_push``（默认开），并把两个进程的弹窗用共享账本去重。
"""

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

from client.main_menu import SyncClient
from common import notify_ledger
from gui.clipboard_handler import ClipboardHandler


class _FakeConfig:
    def __init__(self, base_dir, notify_remote_push=True, local_name="PC-01"):
        self.TEXT_LATEST_FILE = Path(base_dir) / "text_latest.json"
        self.FILE_LATEST_FILE = Path(base_dir) / "file_latest.json"
        self.local_name = local_name
        self.server_running = False
        self.server_port = 8000
        self.notify_remote_push = notify_remote_push


class _LedgerIsolated(unittest.TestCase):
    """把共享账本指向临时目录，避免用例之间互相吞掉弹窗。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        patcher = patch.object(notify_ledger, "LEDGER_FILE", self.root / "ledger.json")
        patcher.start()
        self.addCleanup(patcher.stop)


class NotifyLedgerTest(_LedgerIsolated):
    def test_first_claim_wins_and_second_is_rejected(self):
        self.assertTrue(notify_ledger.claim("text:abc"))
        self.assertFalse(notify_ledger.claim("text:abc"))

    def test_different_keys_are_independent(self):
        self.assertTrue(notify_ledger.claim("text:a"))
        self.assertTrue(notify_ledger.claim("text:b"))

    def test_empty_key_always_allowed(self):
        self.assertTrue(notify_ledger.claim(""))
        self.assertTrue(notify_ledger.claim(None))

    def test_stale_entries_can_be_claimed_again(self):
        now = 1_000_000.0
        self.assertTrue(notify_ledger.claim("text:old", now=now))
        # 保鲜期过后，同一 key 允许重新提醒（用户可能确实又复制了一次）
        self.assertTrue(notify_ledger.claim("text:old", now=now + notify_ledger.KEEP_SECONDS + 5))

    def test_ledger_is_pruned_to_max_entries(self):
        for index in range(notify_ledger.MAX_ENTRIES + 20):
            notify_ledger.claim(f"text:{index}")
        stored = json.loads((self.root / "ledger.json").read_text(encoding="utf-8"))
        self.assertLessEqual(len(stored), notify_ledger.MAX_ENTRIES)

    def test_write_failure_does_not_block_notification(self):
        with patch.object(notify_ledger, "_write", side_effect=OSError("denied")):
            self.assertTrue(notify_ledger.claim("text:x"))


class ClipboardHandlerSwitchTest(_LedgerIsolated):
    def setUp(self):
        super().setUp()
        self.handler = None

    def _handler(self, notify_remote_push=True):
        config = _FakeConfig(self.root, notify_remote_push=notify_remote_push)
        return ClipboardHandler(config), config

    def _write(self, config, item):
        config.TEXT_LATEST_FILE.write_text(
            json.dumps({"latest_global": item}, ensure_ascii=False), encoding="utf-8"
        )

    def _item(self, item_id="id-1", content="手机复制的一段话", source="Phone"):
        return {
            "id": item_id,
            "type": "text",
            "content": content,
            "source": source,
            "timestamp": datetime.now().isoformat(),
        }

    def test_switch_on_notifies(self):
        handler, config = self._handler(True)
        self._write(config, self._item())
        with patch("gui.clipboard_handler.show_notification") as notify:
            handler.handle_client_latest()
        self.assertEqual(notify.call_count, 1)

    def test_switch_off_still_syncs_without_notifying(self):
        handler, config = self._handler(False)
        self._write(config, self._item())
        with patch("gui.clipboard_handler.show_notification") as notify:
            handler.handle_client_latest()
        notify.assert_not_called()

    def test_same_item_is_not_notified_by_two_processes(self):
        """GUI 与客户端进程共用账本：同一条记录只允许响一次。"""
        handler, config = self._handler(True)
        self._write(config, self._item(item_id="id-shared"))

        # 客户端进程先弹（真实代码里发生在 /update/current_latest 收到时）
        self.assertTrue(notify_ledger.claim("text:id-shared"))
        with patch("gui.clipboard_handler.show_notification") as notify:
            handler.handle_client_latest()
        notify.assert_not_called()


class SyncClientRemotePushTest(_LedgerIsolated):
    def _client(self, notify_remote_push=True):
        config = {
            "server_host": "127.0.0.1",
            "server_port": 8000,
            "key": "secret",
            "local_name": "pc-b",
            "notify_remote_push": notify_remote_push,
        }
        client = SyncClient(config, None)
        # 不碰真实的 latest 文件：这条链路只关心「写完剪贴板后弹不弹」
        client.tracker = MagicMock()
        client.tracker.is_duplicate.return_value = False
        return client

    def _latest(self, item_id="t1", content="来自手机的一段话", source="Phone"):
        return {
            "id": item_id,
            "type": "text",
            "content": content,
            "source": source,
            "timestamp": datetime.now().isoformat(),
        }

    def test_dispatch_notifies_by_default(self):
        client = self._client()
        with patch.object(client, "safe_paste", return_value=""), \
                patch("client.main_menu.pyperclip.copy"), \
                patch("client.main_menu.show_notification") as notify:
            self.assertTrue(client.apply_remote_text(self._latest()))

        self.assertEqual(notify.call_count, 1)
        title, msg = notify.call_args.args
        self.assertIn("剪贴板更新", title)
        self.assertIn("Phone", msg)
        self.assertIn("来自手机的一段话", msg)

    def test_switch_off_silently_syncs(self):
        client = self._client(notify_remote_push=False)
        with patch.object(client, "safe_paste", return_value=""), \
                patch("client.main_menu.pyperclip.copy") as copy, \
                patch("client.main_menu.show_notification") as notify:
            self.assertTrue(client.apply_remote_text(self._latest()))

        # 同步照常发生，只是不弹
        copy.assert_called_once()
        notify.assert_not_called()

    def test_same_item_only_notified_once(self):
        client = self._client()
        with patch.object(client, "safe_paste", return_value=""), \
                patch("client.main_menu.pyperclip.copy"), \
                patch("client.main_menu.show_notification") as notify:
            client.apply_remote_text(self._latest(item_id="t-dup"))
            client.apply_remote_text(self._latest(item_id="t-dup", content="换了个内容"))

        # 第二次是同一 id（服务器重复派发），账本拦住
        self.assertEqual(notify.call_count, 1)

    def test_no_notification_when_content_already_in_clipboard(self):
        """剪贴板里已经是这份内容（回音）：不写也不弹。"""
        client = self._client()
        with patch.object(client, "safe_paste", return_value="来自手机的一段话"), \
                patch("client.main_menu.pyperclip.copy"), \
                patch("client.main_menu.show_notification") as notify:
            self.assertFalse(client.apply_remote_text(self._latest()))

        notify.assert_not_called()


if __name__ == "__main__":
    unittest.main()
