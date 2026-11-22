import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from client import file_server as file_server_mod
from client import main_menu as main_menu_mod
from client.file_server import FileServer
from client.main_menu import SyncClient, folder_size_bytes, format_size
from common.sync_modes import (
    MODE_BOTH,
    MODE_RECEIVE,
    MODE_SEND,
    describe_mode,
    is_receive_allowed,
    is_send_allowed,
    normalize_mode,
)
from gui.main_window import validate_client_settings
from server.core import file_latest


class TestSyncModes(unittest.TestCase):
    def test_invalid_mode_falls_back_to_both(self):
        self.assertEqual(normalize_mode("weird"), MODE_BOTH)
        self.assertEqual(normalize_mode(None), MODE_BOTH)
        self.assertEqual(normalize_mode(" SEND "), MODE_SEND)

    def test_direction_gates(self):
        self.assertTrue(is_send_allowed(MODE_BOTH))
        self.assertTrue(is_send_allowed(MODE_SEND))
        self.assertFalse(is_send_allowed(MODE_RECEIVE))

        self.assertTrue(is_receive_allowed(MODE_BOTH))
        self.assertFalse(is_receive_allowed(MODE_SEND))
        self.assertTrue(is_receive_allowed(MODE_RECEIVE))

    def test_describe_mode_is_chinese(self):
        self.assertEqual(describe_mode(MODE_BOTH), "双向")
        self.assertEqual(describe_mode(MODE_SEND), "只发送")
        self.assertEqual(describe_mode(MODE_RECEIVE), "只接收")


class TestValidateSyncSettings(unittest.TestCase):
    def _validate(self, **overrides):
        kwargs = {
            "server_host": "192.168.1.10",
            "server_port": "8000",
            "local_name": "PC-01",
            "file_server_port": "8899",
            "key": "secret",
            "save_path": "D:/Downloads",
        }
        kwargs.update(overrides)
        return validate_client_settings(**kwargs)

    def test_defaults_keep_previous_behaviour(self):
        values = self._validate()
        self.assertEqual(values["text_sync_mode"], MODE_BOTH)
        self.assertEqual(values["file_sync_mode"], MODE_BOTH)
        self.assertTrue(values["folder_sync_enabled"])
        self.assertEqual(values["folder_zip_confirm_gb"], 10)
        self.assertEqual(values["file_share_ttl_minutes"], 10)

    def test_custom_values_are_normalized(self):
        values = self._validate(
            text_sync_mode="send",
            file_sync_mode="receive",
            folder_sync_enabled=False,
            folder_zip_confirm_gb="12.5",
            file_share_ttl_minutes="45",
        )
        self.assertEqual(values["text_sync_mode"], "send")
        self.assertEqual(values["file_sync_mode"], "receive")
        self.assertFalse(values["folder_sync_enabled"])
        self.assertEqual(values["folder_zip_confirm_gb"], 12.5)
        self.assertEqual(values["file_share_ttl_minutes"], 45)

    def test_invalid_mode_is_rejected(self):
        with self.assertRaises(ValueError):
            self._validate(text_sync_mode="upload")

    def test_invalid_folder_limit_is_rejected(self):
        with self.assertRaises(ValueError):
            self._validate(folder_zip_confirm_gb="abc")
        with self.assertRaises(ValueError):
            self._validate(folder_zip_confirm_gb=0)

    def test_invalid_ttl_is_rejected(self):
        with self.assertRaises(ValueError):
            self._validate(file_share_ttl_minutes="0")
        with self.assertRaises(ValueError):
            self._validate(file_share_ttl_minutes=2000)


class _ClientFixture(unittest.TestCase):
    """带临时目录与 FileServer 的 SyncClient。"""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.share_dir = self.root / "folder_share"
        for module in (main_menu_mod, file_server_mod):
            patcher = patch.object(module, "FOLDER_SHARE_TEMP_DIR", self.share_dir)
            patcher.start()
            self.addCleanup(patcher.stop)

        self.file_server = FileServer(local_name="client-a", key="secret")
        self._make_client()

    def _make_client(self, **overrides):
        config = {
            "server_host": "127.0.0.1",
            "server_port": 8000,
            "key": "secret",
            "local_name": "client-a",
        }
        config.update(overrides)
        self.client = SyncClient(config, self.file_server)
        return self.client

    def _mock_post(self, status_code=200, text=""):
        patcher = patch("client.main_menu.requests.post")
        mock_post = patcher.start()
        self.addCleanup(patcher.stop)
        mock_post.return_value = MagicMock(status_code=status_code, text=text)
        return mock_post


class TestClientDirectionGates(_ClientFixture):
    def test_receive_disabled_ignores_remote_text(self):
        client = self._make_client(text_sync_mode="send")
        latest = {"id": "t1", "content": "hello", "source": "other"}

        with patch.object(client, "safe_paste", return_value=""), \
                patch("client.main_menu.pyperclip.copy") as copy:
            self.assertFalse(client.apply_remote_text(latest))
        copy.assert_not_called()

    def test_send_disabled_skips_text_upload(self):
        client = self._make_client(text_sync_mode="receive")
        with patch.object(client, "push_text") as push:
            self.assertFalse(client._push_text_if_allowed("hello"))
        push.assert_not_called()

    def test_both_mode_uploads_text(self):
        client = self._make_client()
        with patch.object(client, "push_text") as push:
            self.assertTrue(client._push_text_if_allowed("hello"))
        push.assert_called_once_with("hello")

    def test_file_send_disabled_does_not_push_or_queue(self):
        mock_post = self._mock_post()
        client = self._make_client(file_sync_mode="receive")
        sample = self.root / "a.txt"
        sample.write_text("x", encoding="utf-8")

        client._push_latest_file([str(sample)])

        mock_post.assert_not_called()
        self.assertFalse(client._enqueue_file_push([str(sample)]))
        self.assertEqual(client._file_push_queue.qsize(), 0)
        self.assertEqual(self.file_server.shared_files, {})


class TestFolderSyncGates(_ClientFixture):
    def setUp(self):
        super().setUp()
        self.folder = self.root / "红米note8" / "ROM"
        self.folder.mkdir(parents=True)
        (self.folder / "system.img").write_bytes(b"x" * 2048)

    def test_folder_helpers(self):
        self.assertEqual(folder_size_bytes(self.folder), 2048)
        self.assertIn("MB", format_size(5 * 1024 * 1024))

    def test_folder_sync_disabled_skips_zip(self):
        mock_post = self._mock_post()
        client = self._make_client(folder_sync_enabled=False)

        client._push_latest_file([str(self.folder)])

        mock_post.assert_not_called()
        self.assertFalse(self.share_dir.exists() and any(self.share_dir.iterdir()))

    def test_large_folder_waits_for_confirmation(self):
        mock_post = self._mock_post()
        client = self._make_client()
        client.folder_zip_confirm_bytes = 1024  # 上限 1KB，目录 2KB 必然超限

        with patch("client.main_menu.confirm_large_folder", return_value=False) as ask:
            client._push_latest_file([str(self.folder)])

        ask.assert_called_once()
        mock_post.assert_not_called()
        self.assertFalse(self.share_dir.exists() and any(self.share_dir.iterdir()))

    def test_large_folder_zipped_after_confirmation(self):
        mock_post = self._mock_post()
        client = self._make_client()
        client.folder_zip_confirm_bytes = 1024

        with patch("client.main_menu.confirm_large_folder", return_value=True):
            client._push_latest_file([str(self.folder)])

        mock_post.assert_called_once()
        item = mock_post.call_args.kwargs["json"]["file_list"][0]
        self.assertEqual(item["name"], "ROM.zip")
        self.assertTrue(Path(item["path"]).is_file())

    def test_small_folder_is_zipped_without_asking(self):
        mock_post = self._mock_post()
        client = self._make_client()  # 默认 10GB，2048 字节远小于上限

        with patch("client.main_menu.confirm_large_folder") as ask:
            client._push_latest_file([str(self.folder)])

        ask.assert_not_called()
        mock_post.assert_called_once()
        item = mock_post.call_args.kwargs["json"]["file_list"][0]
        self.assertEqual(item["name"], "ROM.zip")


class TestFileShareTtl(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.share_dir = Path(self.temp_dir.name) / "folder_share"
        patcher = patch.object(file_server_mod, "FOLDER_SHARE_TEMP_DIR", self.share_dir)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        # 全局 TTL 会被修改，测试结束后恢复默认，避免污染其它用例
        file_latest.set_file_share_ttl(file_latest.DEFAULT_FILE_SHARE_TTL_SECONDS)

    def test_set_ttl_rejects_invalid_values(self):
        file_latest.set_file_share_ttl(300)
        self.assertEqual(file_latest.get_file_share_ttl_seconds(), 300)
        self.assertFalse(file_latest.set_file_share_ttl(0))
        self.assertFalse(file_latest.set_file_share_ttl("abc"))
        self.assertEqual(file_latest.get_file_share_ttl_seconds(), 300)

    def test_file_server_applies_configured_ttl(self):
        FileServer(local_name="c", key="k", share_ttl_seconds=90)
        self.assertEqual(file_latest.get_file_share_ttl_seconds(), 90)

    def test_share_expires_after_configured_ttl(self):
        server = FileServer(local_name="c", key="k", share_ttl_seconds=120)
        sample = Path(self.temp_dir.name) / "a.txt"
        sample.write_text("x", encoding="utf-8")

        with patch.object(file_server_mod.time, "time", return_value=1000.0):
            server.register_file("id1", str(sample))
        with patch.object(file_server_mod.time, "time", return_value=1000.0 + 121):
            self.assertIsNone(server.get_file_path("id1"))

    def test_file_record_expiry_follows_ttl(self):
        record = {"file_id": "x", "updated_at": 1000.0}
        file_latest.set_file_share_ttl(60)
        self.assertFalse(file_latest.is_file_record_expired(record, now=1030.0))
        self.assertTrue(file_latest.is_file_record_expired(record, now=1061.0))


class TestConfigManagerSyncFields(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.config_path = Path(self.temp_dir.name) / "client_config.json"
        from gui.config_manager import ConfigManager

        self.manager_class = ConfigManager
        patcher = patch.object(ConfigManager, "CLIENT_CONFIG", self.config_path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_old_config_gets_new_fields_on_load(self):
        self.config_path.write_text(
            json.dumps({
                "server_host": "192.168.1.10",
                "server_port": 8000,
                "key": "k",
                "local_name": "PC",
                "file_server_port": 8899,
                "save_path": "D:/Downloads",
            }),
            encoding="utf-8",
        )

        manager = self.manager_class()
        self.assertTrue(manager.load_client_config())

        self.assertEqual(manager.text_sync_mode, MODE_BOTH)
        self.assertEqual(manager.file_sync_mode, MODE_BOTH)
        self.assertTrue(manager.folder_sync_enabled)
        self.assertEqual(manager.folder_zip_confirm_gb, 10)
        self.assertEqual(manager.file_share_ttl_minutes, 10)

        saved = json.loads(self.config_path.read_text(encoding="utf-8"))
        for key in (
            "text_sync_mode",
            "file_sync_mode",
            "folder_sync_enabled",
            "folder_zip_confirm_gb",
            "file_share_ttl_minutes",
        ):
            self.assertIn(key, saved)

    def test_invalid_saved_values_fall_back(self):
        self.config_path.write_text(
            json.dumps({
                "server_host": "192.168.1.10",
                "server_port": 8000,
                "key": "k",
                "local_name": "PC",
                "file_server_port": 8899,
                "save_path": "D:/Downloads",
                "text_sync_mode": "upload",
                "folder_sync_enabled": "false",
                "folder_zip_confirm_gb": "bad",
                "file_share_ttl_minutes": "bad",
            }),
            encoding="utf-8",
        )

        manager = self.manager_class()
        self.assertTrue(manager.load_client_config())

        self.assertEqual(manager.text_sync_mode, MODE_BOTH)
        self.assertFalse(manager.folder_sync_enabled)
        self.assertEqual(manager.folder_zip_confirm_gb, 10)
        self.assertEqual(manager.file_share_ttl_minutes, 10)

    def test_update_client_config_persists_sync_fields(self):
        manager = self.manager_class()
        self.assertTrue(manager.load_client_config())

        self.assertTrue(manager.update_client_config({
            "text_sync_mode": "receive",
            "file_sync_mode": "send",
            "folder_sync_enabled": False,
            "folder_zip_confirm_gb": 3,
            "file_share_ttl_minutes": 30,
        }))

        saved = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["text_sync_mode"], "receive")
        self.assertEqual(saved["file_sync_mode"], "send")
        self.assertFalse(saved["folder_sync_enabled"])
        self.assertEqual(saved["folder_zip_confirm_gb"], 3)
        self.assertEqual(saved["file_share_ttl_minutes"], 30)

    def test_unknown_field_is_rejected(self):
        manager = self.manager_class()
        manager.load_client_config()
        with self.assertRaises(ValueError):
            manager.update_client_config({"text_sync_mode_typo": "send"})

    def test_ttl_is_mirrored_into_server_config(self):
        self.config_path.write_text(
            json.dumps({
                "server_host": "192.168.1.10",
                "server_port": 8000,
                "key": "k",
                "local_name": "PC",
                "file_server_port": 8899,
                "save_path": "D:/Downloads",
            }),
            encoding="utf-8",
        )
        server_path = Path(self.temp_dir.name) / "server_config.json"
        server_path.write_text(
            json.dumps({"key": "k", "port": 8001, "local_name": "Srv"}),
            encoding="utf-8",
        )

        with patch.object(self.manager_class, "SERVER_CONFIG", server_path):
            manager = self.manager_class()
            manager.load_client_config()
            manager.file_share_ttl_minutes = 45
            self.assertTrue(manager.apply_file_share_ttl_to_server_config())

        saved = json.loads(server_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["file_share_ttl_minutes"], 45)
        # 只改这一个键，服务器自己的端口不能被客户端配置覆盖
        self.assertEqual(saved["port"], 8001)


class TestClipboardHandlerGating(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.file_latest = Path(self.temp_dir.name) / "file_latest.json"
        self.file_latest.write_text(
            json.dumps([{
                "file_id": "f1",
                "path": "D:/x/a.txt",
                "name": "a.txt",
                "size": 1,
                "source": "other",
                "updated_at": time.time(),
            }]),
            encoding="utf-8",
        )

        from gui.clipboard_handler import ClipboardHandler

        self.config = SimpleNamespace(
            FILE_LATEST_FILE=self.file_latest,
            local_name="me",
            file_sync_mode=MODE_SEND,
        )
        self.handler = ClipboardHandler(self.config, MagicMock())

    def test_file_receive_disabled_skips_notification(self):
        with patch("gui.clipboard_handler.show_notification_with_click") as notify:
            self.handler.handle_file_latest()
        notify.assert_not_called()

    def test_file_receive_enabled_notifies(self):
        self.config.file_sync_mode = MODE_BOTH
        with patch("gui.clipboard_handler.show_notification_with_click") as notify:
            self.handler.handle_file_latest()
        notify.assert_called_once()


if __name__ == "__main__":
    unittest.main()
