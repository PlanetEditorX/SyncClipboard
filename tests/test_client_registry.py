"""客户端注册表的生命周期：自动登记、投递续期、过期清理。

设计要点：
- 任何带 key 且能报出自己名字的请求都会自动登记，不需要手动 /register；
- 清理判定用 max(last_seen, last_delivery)，投递只在真正送达时记账，
  所以已经下线的设备不会靠“每轮投递”无限续命。
"""

import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from server.api import flask_app
from server.core.text_tracker import TextTracker

LOCAL = "SERVER-PC"
DESKTOP = "DESKTOP-A"
PHONE = "熊熊的iPhone"


def _ago(**kwargs):
    return (datetime.now() - timedelta(**kwargs)).strftime("%Y-%m-%d %H:%M:%S")


class RegistryTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        self.latest_file = base / "text_latest.json"
        self.clients_file = base / "client_ip.json"
        self.clients_file.write_text("[]", encoding="utf-8")

        self._patch("server.core.text_tracker.TEXT_LATEST_FILE", self.latest_file)
        for name, value in (
            ("CLIENT_IP_FILE", self.clients_file),
            ("CLIENT_NOTIFY", {}),
            ("IOS_NOTIFY", {}),
            ("CLIENT_TOUCH", {}),
            ("KEY", "secret"),
            ("tracker", TextTracker()),
            ("clients", []),
            ("LOCAL_NAME", LOCAL),
            ("SAVE_PATH", str(base)),
        ):
            self._patch_obj(name, value)
        for name in ("copy_text_to_clipboard", "show_notification"):
            self._patch_obj(name, None, is_mock=True)

        self.client = flask_app.app.test_client()

    def _patch(self, target, value):
        p = patch(target, value)
        p.start()
        self.addCleanup(p.stop)

    def _patch_obj(self, name, value, is_mock=False):
        p = patch.object(flask_app, name) if is_mock else patch.object(flask_app, name, value)
        p.start()
        self.addCleanup(p.stop)

    def _registry(self):
        return json.loads(self.clients_file.read_text(encoding="utf-8"))

    def _names(self):
        return [c.get("local_name") for c in self._registry()]

    def _write_client(self, name, ip, last_seen, last_delivery=None):
        entry = {
            "ip": ip,
            "port": 8899,
            "local_name": name,
            "os": "Windows",
            "first_seen": last_seen,
            "last_seen": last_seen,
        }
        if last_delivery:
            entry["last_delivery"] = last_delivery
        flask_app.clients.append(entry)
        flask_app.save_clients()
        return entry


class AutoRegisterTest(RegistryTestBase):
    def _text_sync(self, content, source):
        response = self.client.post(
            "/text_sync",
            json={"key": "secret", "content": content, "source": source},
        )
        self.assertEqual(response.status_code, 200)
        return response.get_json()["status"]

    def test_first_request_registers_unknown_client(self):
        self.assertEqual(self._names(), [])
        self._text_sync("第一次同步", DESKTOP)
        self.assertIn(DESKTOP, self._names())

    def test_client_is_not_duplicated_and_follows_new_ip(self):
        self._text_sync("第一条", DESKTOP)
        entry = self._registry()[0]
        entry_ip = entry["ip"]

        # 同一台设备换 IP 再来一次：仍然一条记录
        flask_app.CLIENT_TOUCH.clear()
        flask_app.touch_client("10.0.0.9", DESKTOP, "Windows")

        names = self._names()
        self.assertEqual(names.count(DESKTOP), 1)
        self.assertEqual(self._registry()[0]["ip"], "10.0.0.9")
        self.assertNotEqual(entry_ip, "10.0.0.9")

    def test_empty_source_does_not_register(self):
        response = self.client.get("/latest", query_string={"source": ""}, headers={"key": "secret"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self._names(), [])

    def test_registry_write_is_throttled(self):
        self._text_sync("第一条", DESKTOP)
        first_mtime = self.clients_file.stat().st_mtime_ns
        self._text_sync("第二条", DESKTOP)
        self.assertEqual(self.clients_file.stat().st_mtime_ns, first_mtime)

    def test_removed_client_is_registered_again(self):
        self._write_client(DESKTOP, "192.168.1.24", _ago(days=30))
        self.assertNotIn(DESKTOP, self._names())

        self._text_sync("客户端又出现了", DESKTOP)
        self.assertIn(DESKTOP, self._names())

    def test_late_register_fills_push_port(self):
        """自动登记时还不知道推送端口；客户端启动 register 后要补上，且不新增记录。"""
        self._text_sync("先同步一条", DESKTOP)
        entry = self._registry()[0]
        self.assertEqual(entry["port"], 0)

        old_config_key = flask_app.app.config.get("key")
        flask_app.app.config["key"] = "secret"
        self.addCleanup(flask_app.app.config.__setitem__, "key", old_config_key)

        response = self.client.post("/register", json={
            "key": "secret",
            "local_name": DESKTOP,
            "file_server_port": 8899,
            "os": "Windows",
        })
        self.assertEqual(response.status_code, 200)

        entries = [c for c in self._registry() if c["local_name"] == DESKTOP]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["port"], 8899)


class ExpiryTest(RegistryTestBase):
    def test_stale_client_is_removed(self):
        self._write_client(DESKTOP, "192.168.1.24", _ago(days=30))
        self.assertNotIn(DESKTOP, self._names())

    def test_delivery_keeps_client_alive(self):
        """last_seen 过期，但最近成功投递过 —— 说明它还在用，不能删。"""
        self._write_client(DESKTOP, "192.168.1.24", _ago(days=30), _ago(hours=1))
        self.assertIn(DESKTOP, self._names())

    def test_delivery_older_than_ttl_is_removed(self):
        """投递记录也过期了，说明这段时间一直没送达过，才真正清理。"""
        self._write_client(DESKTOP, "192.168.1.24", _ago(days=60), _ago(days=30))
        self.assertNotIn(DESKTOP, self._names())

    def test_record_without_delivery_field_still_works(self):
        """老版本注册表没有 last_delivery 字段，按 last_seen 判定即可。"""
        flask_app.clients.append({
            "ip": "192.168.1.24",
            "port": 8899,
            "local_name": DESKTOP,
            "os": "Windows",
            "first_seen": _ago(hours=2),
            "last_seen": _ago(hours=1),
        })
        flask_app.save_clients()
        self.assertIn(DESKTOP, self._names())

    def test_broken_timestamp_is_treated_as_expired(self):
        flask_app.clients.append({
            "ip": "192.168.1.24",
            "port": 8899,
            "local_name": DESKTOP,
            "os": "Windows",
            "last_seen": "not-a-date",
        })
        flask_app.save_clients()
        self.assertNotIn(DESKTOP, self._names())


class DeliveryTest(RegistryTestBase):
    def setUp(self):
        super().setUp()
        flask_app.clients[:] = [{
            "ip": "192.168.1.24",
            "port": 8899,
            "local_name": DESKTOP,
            "os": "Windows",
            "first_seen": _ago(days=1),
            "last_seen": _ago(days=1),
        }]
        flask_app.save_clients()

    def _publish(self, content, source=PHONE):
        flask_app.tracker.update(
            {
                "id": f"id-{content}",
                "type": "text",
                "content": content,
                "timestamp": datetime.now().isoformat(),
                "source": source,
                "pasted": False,
            },
            force_latest=True,
        )

    def test_successful_push_records_delivery(self):
        self._publish("新内容")
        with patch.object(flask_app, "check_online", return_value=True), \
                patch.object(flask_app, "push_notify", return_value=True):
            flask_app.notify_clients("text")

        entry = next(c for c in self._registry() if c["local_name"] == DESKTOP)
        self.assertIn("last_delivery", entry)
        self.assertGreater(entry["last_delivery"], entry["last_seen"])

    def test_failed_push_does_not_extend_lifetime(self):
        self._publish("新内容")
        with patch.object(flask_app, "check_online", return_value=True), \
                patch.object(flask_app, "push_notify", return_value=False):
            flask_app.notify_clients("text")

        entry = next(c for c in self._registry() if c["local_name"] == DESKTOP)
        self.assertNotIn("last_delivery", entry)

    def test_ios_pending_start_is_not_counted_as_delivered(self):
        """iOS 只是把内容挂到内存字典，还没送达，不能算它的活跃证据。"""
        flask_app.clients[:] = [{
            "ip": "192.168.1.30",
            "port": 0,
            "local_name": PHONE,
            "os": "iOS",
            "first_seen": _ago(days=1),
            "last_seen": _ago(days=1),
        }]
        flask_app.save_clients()
        self._publish("给手机的内容", source=DESKTOP)

        with patch.object(flask_app, "check_online", return_value=True):
            flask_app.notify_clients("text")

        entry = next(c for c in self._registry() if c["local_name"] == PHONE)
        self.assertNotIn("last_delivery", entry)
        self.assertIn(PHONE, flask_app.IOS_NOTIFY)


if __name__ == "__main__":
    unittest.main()
