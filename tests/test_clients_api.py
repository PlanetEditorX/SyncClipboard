"""客户端名单接口：登记了什么、推了多少次、成没成、能不能删。

统计口径是这套功能的核心，所以测试盯的是「什么算成功」：
- 主动推送：HTTP 200 才算送达；
- 轮询型（iOS）：它真把内容取走才算送达，排队不算；
- 离线 / 端口不通：算一次未送达的尝试（分母要有，否则长期离线的设备
  反而显示「从没推过」）。
"""

import json
import ipaddress
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


class ClientsApiTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        self.latest_file = base / "text_latest.json"
        self.clients_file = base / "client_ip.json"
        self.clients_file.write_text("[]", encoding="utf-8")
        self.blacklist_file = base / "blacklist.json"
        self.blacklist_file.write_text("{}", encoding="utf-8")

        self._patch("server.core.text_tracker.TEXT_LATEST_FILE", self.latest_file)
        for name, value in (
            ("CLIENT_IP_FILE", self.clients_file),
            ("BLACKLIST_FILE", self.blacklist_file),
            ("blacklist", {}),
            ("BLACKLIST_LOADED", True),
            ("CLIENT_NOTIFY", {}),
            ("IOS_NOTIFY", {}),
            ("CLIENT_TOUCH", {}),
            ("ONLINE_CACHE", {}),
            ("KEY", "secret"),
            ("tracker", TextTracker()),
            ("clients", []),
            ("LOCAL_NAME", LOCAL),
            ("SAVE_PATH", str(base)),
        ):
            self._patch_obj(name, value)
        for name in ("copy_text_to_clipboard", "show_notification"):
            self._patch_obj(name, name, is_mock=True)

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

    def _add_client(self, name, ip="192.168.1.24", port=8899, os_name="Windows", **extra):
        entry = {
            "ip": ip,
            "port": port,
            "local_name": name,
            "os": os_name,
            "first_seen": _ago(hours=2),
            "last_seen": _ago(minutes=1),
        }
        entry.update(extra)
        flask_app.clients.append(entry)
        flask_app.save_clients()
        return entry

    def _publish(self, content, source="MBP"):
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

    def _list(self, probe=False):
        params = {"probe": 1} if probe else {}
        response = self.client.get("/clients", query_string=params, headers={"key": "secret"})
        self.assertEqual(response.status_code, 200)
        return response.get_json()

    def _row(self, name):
        return next(row for row in self._list()["clients"] if row["local_name"] == name)


class ListClientsTest(ClientsApiTestBase):
    def test_requires_key(self):
        response = self.client.get("/clients")
        self.assertEqual(response.status_code, 403)

    def test_lists_registered_client_fields(self):
        self._add_client(DESKTOP)
        payload = self._list()
        row = payload["clients"][0]

        self.assertEqual(payload["status"], "ok")
        self.assertEqual(row["local_name"], DESKTOP)
        self.assertEqual(row["ip"], "192.168.1.24")
        self.assertEqual(row["port"], 8899)
        self.assertEqual(row["os"], "Windows")
        self.assertEqual(row["push_total"], 0)
        self.assertIsNone(row["push_rate"])
        self.assertTrue(row["last_alive"])

    def test_empty_registry_returns_empty_summary(self):
        payload = self._list()
        self.assertEqual(payload["clients"], [])
        self.assertEqual(payload["summary"]["total"], 0)
        self.assertIsNone(payload["summary"]["push_rate"])

    def test_iOS_client_is_marked_unknown_when_probing(self):
        """iOS 没有推送端口，探不出在线与否，不能谎报「离线」。"""
        self._add_client(PHONE, ip="192.168.1.30", port=0, os_name="iOS")
        payload = self._list(probe=True)
        row = payload["clients"][0]
        self.assertIsNone(row["online"])
        # 探不出在线的设备在汇总里算「轮询」，与行状态一致
        self.assertEqual(payload["summary"]["polling"], 1)
        self.assertEqual(payload["summary"]["offline"], 0)

    def test_probe_marks_unreachable_client_offline(self):
        self._add_client(DESKTOP, ip="192.168.1.24", port=8899)

        def boom(*_args, **_kwargs):
            raise OSError("connect timeout")

        with patch.object(flask_app.requests, "get", side_effect=boom):
            row = self._list(probe=True)["clients"][0]
        self.assertIs(row["online"], False)


class PushStatsTest(ClientsApiTestBase):
    def test_successful_push_counts_as_one_attempt_one_success(self):
        self._add_client(DESKTOP)
        self._publish("新内容")
        with patch.object(flask_app, "check_online", return_value=True), \
                patch.object(flask_app, "push_notify", return_value=True):
            flask_app.notify_clients("text")

        row = self._row(DESKTOP)
        self.assertEqual(row["push_total"], 1)
        self.assertEqual(row["push_ok"], 1)
        self.assertEqual(row["push_fail"], 0)
        self.assertEqual(row["push_rate"], 100.0)
        self.assertTrue(row["last_push_ok"])
        self.assertTrue(row["last_push"])

    def test_offline_client_still_counts_as_an_attempt(self):
        """离线也算一次尝试：否则长期连不上的设备反而显示「从没推过」。"""
        self._add_client(DESKTOP)
        self._publish("新内容")
        with patch.object(flask_app, "check_online", return_value=False):
            flask_app.notify_clients("text")

        row = self._row(DESKTOP)
        self.assertEqual(row["push_total"], 1)
        self.assertEqual(row["push_ok"], 0)
        self.assertEqual(row["push_fail"], 1)
        self.assertEqual(row["push_rate"], 0.0)
        self.assertIs(row["last_push_ok"], False)

    def test_failed_http_push_is_recorded_as_failure(self):
        self._add_client(DESKTOP)
        self._publish("新内容")
        with patch.object(flask_app, "check_online", return_value=True), \
                patch.object(flask_app, "push_notify", return_value=False):
            flask_app.notify_clients("text")

        row = self._row(DESKTOP)
        self.assertEqual((row["push_total"], row["push_ok"], row["push_fail"]), (1, 0, 1))

    def test_source_client_is_not_counted(self):
        """内容就是这台设备自己复制的，压根不该给它推送，也就不该有统计。"""
        self._add_client(DESKTOP)
        self._publish("自己复制的", source=DESKTOP)
        with patch.object(flask_app, "check_online", return_value=True), \
                patch.object(flask_app, "push_notify", return_value=True):
            flask_app.notify_clients("text")

        row = self._row(DESKTOP)
        self.assertEqual(row["push_total"], 0)

    def test_ios_queue_is_not_a_success_until_it_pulls(self):
        self._add_client(PHONE, ip="192.168.1.30", port=0, os_name="iOS")
        self._publish("给手机的内容", source=DESKTOP)
        with patch.object(flask_app, "check_online", return_value=True):
            flask_app.notify_clients("text")

        row = self._row(PHONE)
        self.assertEqual((row["push_total"], row["push_ok"]), (1, 0))
        self.assertIn(PHONE, flask_app.IOS_NOTIFY)

        # 手机来拉走 —— 这才算送达
        response = self.client.get(
            "/ios/latest", query_string={"source": PHONE}, headers={"key": "secret"}
        )
        self.assertEqual(response.status_code, 200)
        row = self._row(PHONE)
        self.assertEqual((row["push_total"], row["push_ok"]), (1, 1))
        self.assertEqual(row["pull_ok"], 1)
        self.assertEqual(row["push_rate"], 100.0)

    def test_ios_pull_without_queue_never_exceeds_full_rate(self):
        """服务端重启过、设备不在表里过 —— 兜底取内容的路径也不能把成功率算到 100% 以上。"""
        self._add_client(PHONE, ip="192.168.1.30", port=0, os_name="iOS")
        self._publish("兜底内容", source=DESKTOP)

        response = self.client.get(
            "/ios/latest", query_string={"source": PHONE}, headers={"key": "secret"}
        )
        self.assertEqual(response.status_code, 200)
        row = self._row(PHONE)
        self.assertEqual(row["push_total"], 1)
        self.assertEqual(row["push_ok"], 1)
        self.assertEqual(row["push_rate"], 100.0)

    def test_stats_survive_a_reload_from_disk(self):
        self._add_client(DESKTOP)
        self._publish("新内容")
        with patch.object(flask_app, "check_online", return_value=True), \
                patch.object(flask_app, "push_notify", return_value=True):
            flask_app.notify_clients("text")

        entry = next(c for c in self._registry() if c["local_name"] == DESKTOP)
        self.assertEqual(entry["push_total"], 1)
        self.assertEqual(entry["push_ok"], 1)

    def test_summary_aggregates_all_clients(self):
        self._add_client(DESKTOP, ip="192.168.1.24")
        self._add_client("LAPTOP-B", ip="192.168.1.25")
        self._publish("新内容")
        with patch.object(flask_app, "check_online", return_value=True), \
                patch.object(flask_app, "push_notify", return_value=True):
            flask_app.notify_clients("text")

        summary = self._list()["summary"]
        self.assertEqual(summary["total"], 2)
        self.assertEqual(summary["push_total"], 2)
        self.assertEqual(summary["push_ok"], 2)
        self.assertEqual(summary["push_rate"], 100.0)


class DeleteClientTest(ClientsApiTestBase):
    def test_delete_by_name_removes_record_and_stats(self):
        self._add_client(DESKTOP)
        self._add_client("LAPTOP-B", ip="192.168.1.25")

        response = self.client.post(
            "/clients/delete", json={"local_name": DESKTOP}, headers={"key": "secret"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["removed"], 1)

        names = [c["local_name"] for c in self._registry()]
        self.assertEqual(names, ["LAPTOP-B"])
        self.assertEqual([row["local_name"] for row in self._list()["clients"]], ["LAPTOP-B"])

    def test_delete_by_ip_only_when_name_missing(self):
        self._add_client(DESKTOP, ip="192.168.1.24")
        response = self.client.post(
            "/clients/delete", json={"ip": "192.168.1.24"}, headers={"key": "secret"}
        )
        self.assertEqual(response.get_json()["removed"], 1)
        self.assertEqual(self._registry(), [])

    def test_delete_all_clears_registry(self):
        self._add_client(DESKTOP)
        self._add_client("LAPTOP-B", ip="192.168.1.25")
        response = self.client.post(
            "/clients/delete", json={"all": True}, headers={"key": "secret"}
        )
        self.assertEqual(response.get_json()["removed"], 2)
        self.assertEqual(self._registry(), [])

    def test_delete_unknown_client_is_not_an_error(self):
        response = self.client.post(
            "/clients/delete", json={"local_name": "nobody"}, headers={"key": "secret"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["removed"], 0)

    def test_delete_requires_key(self):
        self._add_client(DESKTOP)
        response = self.client.post("/clients/delete", json={"local_name": DESKTOP})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(len(self._registry()), 1)

    def test_delete_clears_pending_ios_content(self):
        """删掉记录时，内存里还没被取走的内容也一起清掉，别给下一位同名设备。"""
        self._add_client(PHONE, ip="192.168.1.30", port=0, os_name="iOS")
        flask_app.IOS_NOTIFY[PHONE] = {"id": "x", "content": "旧内容"}
        self.client.post(
            "/clients/delete", json={"local_name": PHONE}, headers={"key": "secret"}
        )
        self.assertEqual(flask_app.IOS_NOTIFY, {})

    def test_missing_identifier_is_rejected(self):
        response = self.client.post("/clients/delete", json={}, headers={"key": "secret"})
        self.assertEqual(response.status_code, 400)


class BlacklistTest(ClientsApiTestBase):
    """黑名单 vs 移除：移除只是清派发列表，黑名单是持久拦截。"""

    def _block(self, name, block=True):
        return self.client.post(
            "/clients/blacklist",
            json={"local_name": name, "block": block},
            headers={"key": "secret"},
        )

    def test_blacklist_clears_dispatch_and_blocks_both_register_paths(self):
        self._add_client(DESKTOP)
        response = self._block(DESKTOP)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["blocked"])
        self.assertEqual(self._registry(), [])

        # 普通请求的自动登记进不来
        flask_app.touch_client("192.168.1.24", DESKTOP, "Windows")
        self.assertEqual(self._registry(), [])
        # 显式注册也进不来 —— 这才是黑名单和「移除」的区别
        response = self.client.post(
            "/register",
            json={"local_name": DESKTOP, "file_server_port": 8899, "os": "Windows"},
            headers={"key": "secret"},
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self._registry(), [])

    def test_blacklisted_client_stays_visible_for_unblocking(self):
        self._add_client(DESKTOP, port=8899)
        self._block(DESKTOP)
        payload = self._list()
        self.assertEqual(len(payload["clients"]), 1)
        row = payload["clients"][0]
        self.assertEqual(row["local_name"], DESKTOP)
        self.assertTrue(row["blacklisted"])
        self.assertEqual(payload["summary"]["blacklisted"], 1)

    def test_unblock_restores_auto_registration(self):
        self._add_client(DESKTOP)
        self._block(DESKTOP)
        response = self._block(DESKTOP, block=False)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.get_json()["blocked"])

        flask_app.touch_client("192.168.1.24", DESKTOP, "Windows")
        self.assertIn(DESKTOP, [c["local_name"] for c in self._registry()])
        self.assertFalse(self._list()["clients"][0]["blacklisted"])

    def test_blacklist_survives_reload(self):
        self._add_client(DESKTOP)
        self._block(DESKTOP)
        # 模拟服务端重启
        flask_app.blacklist.clear()
        flask_app.BLACKLIST_LOADED = False
        flask_app.load_blacklist()

        flask_app.touch_client("192.168.1.24", DESKTOP, "Windows")
        self.assertEqual(self._registry(), [])

    def test_blacklist_requires_key(self):
        self._add_client(DESKTOP)
        response = self.client.post("/clients/blacklist", json={"local_name": DESKTOP})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(len(self._registry()), 1)

    def test_blacklisted_ios_cannot_pull_content(self):
        self._add_client(PHONE, ip="192.168.1.30", port=0, os_name="iOS")
        self._publish("新内容", source="MBP")
        self._block(PHONE)

        response = self.client.get(
            "/ios/latest",
            query_string={"source": PHONE},
            headers={"key": "secret"},
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()["message"], "设备已在黑名单")

    def test_blacklisted_sync_request_is_rejected(self):
        self._add_client(DESKTOP)
        self._block(DESKTOP)
        response = self.client.post(
            "/text_sync",
            json={"source": DESKTOP, "content": "x", "encode": "plain"},
            headers={"key": "secret"},
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()["message"], "设备已在黑名单")
        # 面板自己的接口仍然可用，否则解不开黑名单
        self.assertEqual(self._list()["summary"]["blacklisted"], 1)


class LocalOnlyManagementTest(ClientsApiTestBase):
    """写操作只许服务器本机；远端即使拿着正确 key 也只能查看。"""

    REMOTE = "192.168.1.21"   # 另一台电脑
    LOCAL_LAN = "192.168.1.23"  # 服务器自己的局域网地址

    def test_list_tells_panel_whether_it_can_manage(self):
        self.assertTrue(self._list()["can_manage"])
        payload = self.client.get(
            "/clients", headers={"key": "secret"},
            environ_base={"REMOTE_ADDR": self.REMOTE},
        ).get_json()
        self.assertFalse(payload["can_manage"])

    def test_remote_cannot_delete_even_with_valid_key(self):
        self._add_client(DESKTOP)
        response = self.client.post(
            "/clients/delete", json={"local_name": DESKTOP},
            headers={"key": "secret"}, environ_base={"REMOTE_ADDR": self.REMOTE},
        )
        self.assertEqual(response.status_code, 403)
        self.assertIn("本机", response.get_json()["message"])
        self.assertEqual(len(self._registry()), 1)

    def test_remote_cannot_blacklist_even_with_valid_key(self):
        self._add_client(DESKTOP)
        response = self.client.post(
            "/clients/blacklist", json={"local_name": DESKTOP},
            headers={"key": "secret"}, environ_base={"REMOTE_ADDR": self.REMOTE},
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(flask_app.blacklist, {})
        self.assertEqual(len(self._registry()), 1)

    def test_read_endpoints_stay_open_remotely(self):
        self._add_client(DESKTOP)
        opts = {"headers": {"key": "secret"},
                "environ_base": {"REMOTE_ADDR": self.REMOTE}}
        self.assertEqual(self.client.get("/clients", **opts).status_code, 200)
        self.assertEqual(self.client.get("/clients/online", **opts).status_code, 200)

    def test_server_lan_ip_counts_as_local(self):
        addresses = {ipaddress.ip_address("127.0.0.1"),
                     ipaddress.ip_address(self.LOCAL_LAN)}
        with patch.object(flask_app, "_local_addresses", return_value=addresses):
            payload = self.client.get(
                "/clients", headers={"key": "secret"},
                environ_base={"REMOTE_ADDR": self.LOCAL_LAN},
            ).get_json()
            self.assertTrue(payload["can_manage"])
            response = self.client.post(
                "/clients/delete", json={"local_name": DESKTOP},
                headers={"key": "secret"},
                environ_base={"REMOTE_ADDR": self.LOCAL_LAN},
            )
            self.assertEqual(response.status_code, 200)

    def test_foreign_lan_ip_stays_read_only(self):
        addresses = {ipaddress.ip_address("127.0.0.1"),
                     ipaddress.ip_address(self.LOCAL_LAN)}
        with patch.object(flask_app, "_local_addresses", return_value=addresses):
            payload = self.client.get(
                "/clients", headers={"key": "secret"},
                environ_base={"REMOTE_ADDR": self.REMOTE},
            ).get_json()
            self.assertFalse(payload["can_manage"])


if __name__ == "__main__":
    unittest.main()
