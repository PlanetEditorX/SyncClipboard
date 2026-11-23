# -*- coding: utf-8 -*-
"""五端统一契约的回归防线。

本轮修复把这些行为固定下来，防止回退：
1. 所有请求一律用请求头 key 鉴权（require_key 全站唯一入口）；
2. /latest/clear 不再免密；
3. Android UA 收敛成 "Android"，不再把整串 UA 写进 os 字段；
4. 目录条目不再下发必然 409 的 download_url 死链；
5. 文件名里的字面 %XX 不被二次解码改名；
6. 电脑端 FileServer 除 /ping 外全路由鉴权。
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from client.file_server import FileServer
from common.utils import request_key_matches
from server.api import flask_app

KEY = "secret"


class UnifiedServerContractTest(unittest.TestCase):
    def setUp(self):
        self.client = flask_app.app.test_client()
        self._patchers = [
            patch.object(flask_app, "KEY", KEY),
            patch.object(flask_app, "latest_file", MagicMock()),
            patch.object(flask_app, "notify_clients", MagicMock()),
        ]
        for patcher in self._patchers:
            patcher.start()
        self.addCleanup(self._stop_patchers)

    def _stop_patchers(self):
        for patcher in reversed(self._patchers):
            patcher.stop()

    def test_require_key_is_single_entry_and_rejects_bad_key(self):
        with flask_app.app.test_request_context("/x", headers={"key": KEY}):
            self.assertTrue(flask_app.require_key())
            self.assertTrue(request_key_matches(KEY))
        # JSON body / query 是等价入口，不是给旧客户端留的分支
        with flask_app.app.test_request_context("/x", json={"key": KEY}):
            self.assertTrue(flask_app.require_key())
        with flask_app.app.test_request_context("/x?key=" + KEY):
            self.assertTrue(flask_app.require_key())
        with flask_app.app.test_request_context("/x", headers={"key": "bad"}):
            self.assertFalse(flask_app.require_key())
        with flask_app.app.test_request_context("/x"):
            self.assertFalse(flask_app.require_key())

    def test_latest_clear_requires_key(self):
        self.assertEqual(self.client.get("/latest/clear").status_code, 403)
        self.assertEqual(
            self.client.get("/latest/clear", headers={"key": "bad"}).status_code, 403)
        self.assertEqual(
            self.client.get("/latest/clear", headers={"key": KEY}).status_code, 200)
        flask_app.latest_file.clear.assert_called_once()

    def test_ios_sync_accepts_header_only_key(self):
        """文档写的是请求头，旧实现只读 body → 第三方接入必 403。"""
        with patch.object(flask_app, "tracker", MagicMock()), \
             patch.object(flask_app, "touch_client", MagicMock()), \
             patch.object(flask_app, "LOCAL_NAME", "Server"), \
             patch.object(flask_app, "_take_ios_latest",
                          return_value={"pasted": True}):
            resp = self.client.post(
                "/ios/sync", headers={"key": KEY},
                json={"content": "", "source": "phone"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["status"], "ok")

    def test_get_os_type_normalizes_android_and_unknown_ua(self):
        self.assertEqual(
            flask_app.get_os_type("SyncClipboardAndroid/0.3.4 (Android)"), "Android")
        self.assertEqual(flask_app.get_os_type("okhttp/4.12.0"), "Android")
        self.assertEqual(flask_app.get_os_type("python-requests/2.31"), "Windows")
        self.assertEqual(flask_app.get_os_type("SomeRandomUA/1.0"), "Unknown")

    def test_folder_entry_has_manifest_url_only(self):
        fake = MagicMock()
        fake.get_all_files.return_value = [{
            "file_id": "d1", "name": "课程", "size": 10, "source": "pc",
            "ip": "127.0.0.1", "port": 8899, "kind": "folder",
        }]
        with patch.object(flask_app, "latest_file", fake):
            resp = self.client.post(
                "/request_file", headers={"key": KEY}, json={"source": "phone"})
        payload = resp.get_json()
        self.assertEqual(payload["type"], "folder")
        self.assertNotIn("download_url", payload["files"][0])
        self.assertTrue(payload["files"][0]["manifest_url"].endswith("/manifest/d1"))

    def test_literal_percent_in_filename_is_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(flask_app, "SAVE_PATH", tmp), \
                 patch.object(flask_app, "touch_client", MagicMock()), \
                 patch.object(flask_app, "show_notification", MagicMock()):
                resp = self.client.put(
                    # %25 解码一次得到字面的 %20；旧实现再 unquote 一次会改名为空格
                    "/upload_file?filename=report%2520final.txt",
                    data=b"payload", headers={"key": KEY})
            self.assertEqual(resp.status_code, 200)
            self.assertTrue(
                (Path(tmp) / "report%20final.txt").exists(),
                "文件名被二次解码改名了")


class FileServerAuthContractTest(unittest.TestCase):
    def setUp(self):
        self.server = FileServer(key=KEY)

    def test_every_route_except_ping_requires_key(self):
        client = self.server.app.test_client()
        for path in ("/files", "/check/x", "/file/x", "/manifest/x",
                     "/manifest/batch?ids=x", "/clear/file_latest",
                     "/update/current_latest"):
            self.assertEqual(client.get(path).status_code, 403, path)
        self.assertEqual(client.get("/ping").status_code, 200)

    def test_correct_key_grants_access(self):
        client = self.server.app.test_client()
        client.environ_base["HTTP_KEY"] = KEY
        self.assertEqual(client.get("/files").status_code, 200)


class LinuxServerDefaultsTest(unittest.TestCase):
    def test_default_share_ttl_is_30_minutes(self):
        from server import linux_run
        with tempfile.TemporaryDirectory() as tmp:
            conf = Path(tmp) / "server_config.json"
            with patch.object(linux_run.LinuxConfigManager, "SERVER_CONFIG", conf):
                manager = linux_run.LinuxConfigManager()
                self.assertTrue(manager.load_server_config())
                self.assertEqual(manager.file_share_ttl_minutes, 30)


if __name__ == "__main__":
    unittest.main()
