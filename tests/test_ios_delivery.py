"""iOS 快捷指令的拉取路径。

背景：iOS 只能定时轮询，既不注册也不会被主动推送。历史实现把待投递内容
只放在内存字典 IOS_NOTIFY 里，而那张表由 notify_clients() 遍历 clients
注册表时填充 —— 一旦手机从注册表里过期消失，接口就永远返回
{"pasted": true} 空对象，电脑复制的新内容再也到不了手机。
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from server.api import flask_app
from server.core.text_tracker import TextTracker

PHONE = "熊熊的iPhone"
DESKTOP = "DESKTOP-TEST"


class IOSDeliveryTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        self.latest_file = base / "text_latest.json"
        self.clients_file = base / "client_ip.json"
        self.clients_file.write_text("[]", encoding="utf-8")

        self._start_patch("server.core.text_tracker.TEXT_LATEST_FILE", self.latest_file)
        for name, value in (
            ("CLIENT_IP_FILE", self.clients_file),
            ("CLIENT_NOTIFY", {}),
            ("IOS_NOTIFY", {}),
            ("CLIENT_TOUCH", {}),
            ("KEY", "secret"),
            ("tracker", TextTracker()),
            ("clients", []),
            ("LOCAL_NAME", DESKTOP),
        ):
            self._start_patch_obj(name, value)
        for name in ("copy_text_to_clipboard", "notify_clients"):
            p = patch.object(flask_app, name)
            p.start()
            self.addCleanup(p.stop)

        self.client = flask_app.app.test_client()

    def _start_patch(self, target, value):
        p = patch(target, value)
        p.start()
        self.addCleanup(p.stop)

    def _start_patch_obj(self, name, value):
        p = patch.object(flask_app, name, value)
        p.start()
        self.addCleanup(p.stop)

    def _desktop_copy(self, content):
        response = self.client.post(
            "/text_sync",
            json={"key": "secret", "content": content, "source": DESKTOP},
        )
        self.assertEqual(response.status_code, 200)
        return response.get_json()["status"]

    def _phone_pull(self):
        response = self.client.get(
            "/ios/latest",
            query_string={"source": PHONE},
            headers={"key": "secret"},
        )
        self.assertEqual(response.status_code, 200)
        return response.get_json()["latest_global"]

    def _registered_names(self):
        flask_app.load_clients_ip()
        return [client.get("local_name") for client in flask_app.clients]

    # ---------- 核心回归：手机不在注册表里也要能拉到 ----------

    def test_phone_gets_new_text_even_when_missing_from_registry(self):
        """用户报的故障：注册表里没有 iPhone 时，仍然必须拿到电脑复制的内容。"""
        self.assertNotIn(PHONE, self._registered_names())
        self.assertEqual(self._desktop_copy("电脑复制的新内容"), "ok")

        latest = self._phone_pull()
        self.assertEqual(latest.get("content"), "电脑复制的新内容")
        self.assertFalse(latest.get("pasted"))

    def test_content_is_delivered_only_once(self):
        self._desktop_copy("只该给一次")
        self.assertEqual(self._phone_pull().get("content"), "只该给一次")

        second = self._phone_pull()
        self.assertNotIn("content", second)
        self.assertTrue(second.get("pasted"))

    def test_phone_own_content_is_not_sent_back(self):
        """手机自己发的内容，不要当成“新内容”再推回给它。"""
        self.client.post(
            "/ios/sync",
            json={"key": "secret", "content": "手机复制的", "source": PHONE, "os": "iOS"},
        )
        latest = self._phone_pull()
        self.assertNotIn("content", latest)
        self.assertTrue(latest.get("pasted"))

    def test_new_desktop_text_arrives_after_phone_pushed(self):
        """手机发过内容之后，电脑再复制新内容，手机仍要能收到。"""
        self.client.post(
            "/ios/sync",
            json={"key": "secret", "content": "手机先复制的", "source": PHONE, "os": "iOS"},
        )
        self._phone_pull()

        self._desktop_copy("电脑后复制的新内容")
        latest = self._phone_pull()
        self.assertEqual(latest.get("content"), "电脑后复制的新内容")
        self.assertFalse(latest.get("pasted"))

    # ---------- 注册表续期 ----------

    def test_polling_registers_phone_so_it_never_expires(self):
        """轮询要顺手登记，否则 7 天后被 save_clients() 清掉，推送彻底断掉。"""
        self._phone_pull()

        self.assertIn(PHONE, self._registered_names())
        persisted = json.loads(self.clients_file.read_text(encoding="utf-8"))
        names = [client.get("local_name") for client in persisted]
        self.assertIn(PHONE, names)
        entry = next(c for c in persisted if c.get("local_name") == PHONE)
        self.assertEqual(entry.get("os"), "iOS")

    def test_registry_write_is_throttled(self):
        """几秒一个轮询，不能每次都重写注册表。"""
        self._phone_pull()
        first_mtime = self.clients_file.stat().st_mtime_ns
        self._phone_pull()
        self.assertEqual(self.clients_file.stat().st_mtime_ns, first_mtime)

    # ---------- 主动推送路径保持可用 ----------

    def test_pending_push_is_preferred_and_consumed(self):
        flask_app.IOS_NOTIFY[PHONE] = {
            "id": "pushed-1",
            "type": "text",
            "content": "服务端主动推送的",
            "timestamp": "2026-11-20T14:00:00",
            "source": DESKTOP,
            "pasted": False,
        }
        latest = self._phone_pull()
        self.assertEqual(latest.get("content"), "服务端主动推送的")
        self.assertFalse(latest.get("pasted"))
        self.assertNotIn(PHONE, flask_app.IOS_NOTIFY)


if __name__ == "__main__":
    unittest.main()
