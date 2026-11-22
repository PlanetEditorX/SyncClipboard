"""边界测试：畸形输入不能变成 500，也不能静默产出不可用的地址。

跟 `test_pull_contract.py` 的分工：那边守「正常路径的三端契约」，这边专门喂
发送端可能真的发出来的脏数据 —— 端口写成 `"0"`、`file_list` 写成对象、
记录里混进字符串、文本内容为空、共享的文件已被删除。判据有两条：

1. **不许 500**。发送端拿不到可用错误码，就只能看到「服务器错误」。
2. **不许产出坏 URL**。比「没有文件」更难查 —— 接收端会拿着
   `http://ip:0/file/xxx` 去下载并失败，看起来像网络问题。
"""

import unittest
from urllib.parse import urlparse
from unittest.mock import MagicMock, patch

from server.api import flask_app

try:                                    # python -m unittest discover -s tests
    from tests.test_pull_contract import (
        DESKTOP, DEVICE, KEY, _FakeLatestFile, _FakeTracker, _patch_flask,
    )
except ImportError:                     # 在 tests/ 目录里直接跑
    from test_pull_contract import (    # type: ignore
        DESKTOP, DEVICE, KEY, _FakeLatestFile, _FakeTracker, _patch_flask,
    )


class _Case(unittest.TestCase):
    """与 test_pull_contract._FlaskCase 同构，但保留对 latest_file 的直接控制。"""

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

    def _push(self, file_list, port=None, source=DESKTOP):
        return self.client.post(
            "/file_sync", headers={"key": KEY},
            json={"file_list": file_list, "source": source, "port": port},
        )

    def _pull(self, source=DEVICE):
        return self.client.post(
            "/request_file", headers={"key": KEY}, json={"source": source}
        )

    def _pull_json(self, source=DEVICE):
        return self._pull(source).get_json()

    @staticmethod
    def _record(file_id="f1", name="note.txt", kind="file", size=12):
        return {"file_id": file_id, "path": f"D:/Downloads/{name}",
                "name": name, "size": size, "kind": kind}


class TestPortNormalization(_Case):
    """端口必须落在 1..65535；任何畸形值都回落默认端口。"""

    def test_empty_string_falls_back(self):
        port, ok = flask_app.normalize_file_port("")
        self.assertFalse(ok)
        self.assertEqual(port, flask_app.DEFAULT_FILE_PORT)

    def test_string_of_zero_falls_back(self):
        """`"0"` 是 truthy —— 只做 `or` 兜底时会被原样写进下载地址。"""
        port, ok = flask_app.normalize_file_port("0")
        self.assertFalse(ok)
        self.assertEqual(port, flask_app.DEFAULT_FILE_PORT)

    def test_non_numeric_string_falls_back(self):
        self.assertEqual(flask_app.normalize_file_port("abc"),
                         (flask_app.DEFAULT_FILE_PORT, False))

    def test_negative_falls_back(self):
        self.assertEqual(flask_app.normalize_file_port(-1),
                         (flask_app.DEFAULT_FILE_PORT, False))

    def test_out_of_range_high_falls_back(self):
        self.assertEqual(flask_app.normalize_file_port(65536),
                         (flask_app.DEFAULT_FILE_PORT, False))

    def test_float_falls_back(self):
        """3.14 不该被截成 3 —— 静默截断比直接拒绝更难发现。"""
        self.assertEqual(flask_app.normalize_file_port(3.14),
                         (flask_app.DEFAULT_FILE_PORT, False))

    def test_bool_falls_back(self):
        """True 是 int 的子类，会被当成端口 1。"""
        self.assertEqual(flask_app.normalize_file_port(True),
                         (flask_app.DEFAULT_FILE_PORT, False))

    def test_none_falls_back(self):
        self.assertEqual(flask_app.normalize_file_port(None),
                         (flask_app.DEFAULT_FILE_PORT, False))

    def test_boundary_ports_are_kept(self):
        self.assertEqual(flask_app.normalize_file_port(1), (1, True))
        self.assertEqual(flask_app.normalize_file_port(65535), (65535, True))
        self.assertEqual(flask_app.normalize_file_port(8899), (8899, True))

    def test_numeric_string_is_converted(self):
        port, ok = flask_app.normalize_file_port(" 8899 ")
        self.assertTrue(ok)
        self.assertEqual(port, 8899)
        self.assertIsInstance(port, int)

    def test_dirty_ports_never_reach_the_url(self):
        """端到端：脏端口推送进来，拉取到的地址必须是可用端口。"""
        for raw in (None, "", "0", "abc", -1, 65536, 3.14, True):
            with self.subTest(port=raw):
                self.latest_file.records = []
                self._push([self._record()], port=raw)
                payload = self._pull_json()
                self.assertEqual(payload["type"], "file_list")
                url = payload["files"][0]["download_url"]
                parsed = urlparse(url)
                self.assertEqual(parsed.scheme, "http")
                self.assertEqual(parsed.hostname, "127.0.0.1")
                self.assertEqual(parsed.port, flask_app.DEFAULT_FILE_PORT)


class TestFileListPayload(_Case):
    """`file_list` 的形状不对时要给 400，而不是抛异常。"""

    def test_empty_list_returns_400(self):
        resp = self._push([])
        self.assertEqual(resp.status_code, 400)

    def test_empty_list_keeps_existing_records(self):
        """空推送不能顺手把别的设备刚放上来的共享清掉。"""
        self._push([self._record()])
        self.assertEqual(len(self.latest_file.records), 1)
        self._push([])
        self.assertEqual(len(self.latest_file.records), 1)

    def test_null_payload_returns_400(self):
        resp = self.client.post("/file_sync", headers={"key": KEY}, json=None)
        self.assertEqual(resp.status_code, 400)

    def test_non_json_body_returns_400(self):
        resp = self.client.post("/file_sync", headers={"key": KEY}, data="not json",
                                content_type="application/json")
        self.assertEqual(resp.status_code, 400)

    def test_object_instead_of_array_returns_400(self):
        """发送端把数组写成对象时，遍历 dict 会拿 key 当条目 → AttributeError。"""
        resp = self.client.post(
            "/file_sync", headers={"key": KEY},
            json={"file_list": {"f1": self._record()}, "source": DESKTOP, "port": 8899},
        )
        self.assertEqual(resp.status_code, 400)

    def test_string_instead_of_array_returns_400(self):
        resp = self.client.post(
            "/file_sync", headers={"key": KEY},
            json={"file_list": "f1", "source": DESKTOP, "port": 8899},
        )
        self.assertEqual(resp.status_code, 400)

    def test_non_dict_entries_are_skipped_without_500(self):
        resp = self._push([self._record(), "垃圾", 42, None])
        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json()
        self.assertEqual(payload["success_count"], 1)
        self.assertEqual(payload["error_count"], 3)

    def test_all_invalid_entries_fall_through_to_text(self):
        """整批记录都不完整时走文本分支，不能 500。"""
        resp = self._push([{"name": "无 id"}, {"file_id": "x"}])
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["success_count"], 0)
        payload = self._pull_json()
        self.assertEqual(payload["type"], "text")

    def test_size_none_does_not_break_serialization(self):
        self._push([self._record(size=None)])
        self.assertEqual(self._pull().status_code, 200)

    def test_huge_size_is_preserved(self):
        self._push([self._record(size=5 * 1024 ** 3)])
        payload = self._pull_json()
        self.assertEqual(payload["files"][0]["size"], 5 * 1024 ** 3)


class TestTextDeliveryBoundary(_Case):
    """文本分支的空值、优先级、重复投递。"""

    def test_no_latest_returns_empty_object(self):
        payload = self._pull_json()
        self.assertEqual(payload["type"], "text")
        self.assertTrue(payload["latest_global"]["pasted"])

    def test_none_source_returns_empty_object(self):
        self.tracker.latest = {"id": "t1", "content": "x", "source": DESKTOP}
        payload = self._pull_json(source=None)
        self.assertTrue(payload["latest_global"]["pasted"])

    def test_empty_content_is_delivered_only_once(self):
        """空文本不是「没有文本」：第一次照发（手机侧另有「有任何值」判断）。"""
        self.tracker.latest = {"id": "t1", "content": "", "source": DESKTOP}
        first = self._pull_json()
        self.assertEqual(first["latest_global"]["content"], "")
        second = self._pull_json()
        self.assertTrue(second["latest_global"]["pasted"])

    def test_ios_notify_wins_and_is_consumed(self):
        self.tracker.latest = {"id": "t1", "content": "全局", "source": DESKTOP}
        flask_app.IOS_NOTIFY[DEVICE] = {"id": "n1", "content": "推送", "source": DESKTOP}
        payload = self._pull_json()
        self.assertEqual(payload["latest_global"]["content"], "推送")
        self.assertNotIn(DEVICE, flask_app.IOS_NOTIFY)
        # 消费掉之后回落到全局最新，而不是一直返回推送内容
        second = self._pull_json()
        self.assertEqual(second["latest_global"]["content"], "全局")

    def test_tracker_without_clients_key_does_not_crash(self):
        self.tracker.data = {"latest_global": {"content": "x", "source": DESKTOP}}
        self.tracker.latest = {"id": "t1", "content": "x", "source": DESKTOP}
        self.assertEqual(self._pull().status_code, 200)

    def test_same_content_from_other_device_still_blocked(self):
        """判重按内容比，不按 id —— 同一段文字换了 id 也不能再投一次。"""
        self.tracker.latest = {"id": "t1", "content": "同一段", "source": DESKTOP}
        self._pull_json()
        self.tracker.latest = {"id": "t2", "content": "同一段", "source": DESKTOP}
        self.assertTrue(self._pull_json()["latest_global"]["pasted"])

    def test_new_content_is_delivered_again(self):
        self.tracker.latest = {"id": "t1", "content": "第一段", "source": DESKTOP}
        self._pull_json()
        self.tracker.latest = {"id": "t2", "content": "第二段", "source": DESKTOP}
        self.assertEqual(self._pull_json()["latest_global"]["content"], "第二段")


class TestRequestFileBoundary(_Case):
    """`/request_file` 自身的畸形请求。"""

    def test_missing_key_returns_403(self):
        resp = self.client.post("/request_file", json={"source": DEVICE})
        self.assertEqual(resp.status_code, 403)

    def test_wrong_key_returns_403(self):
        resp = self.client.post("/request_file", headers={"key": "bad"},
                                json={"source": DEVICE})
        self.assertEqual(resp.status_code, 403)

    def test_null_body_returns_400(self):
        resp = self.client.post("/request_file", headers={"key": KEY}, json=None)
        self.assertEqual(resp.status_code, 400)

    def test_record_without_ip_is_skipped(self):
        """有记录但缺 ip 时不能拼出 http://None:8899/..."""
        self.latest_file.records = [{
            "file_id": "f1", "ip": None, "port": 8899, "name": "a.txt",
            "size": 1, "kind": "file",
        }]
        payload = self._pull_json()
        self.assertEqual(payload["type"], "text")

    def test_dirty_port_record_is_skipped_not_guessed(self):
        """磁盘上遗留 `"0"` 端口的记录整条跳过，不能拼出 http://ip:0/...。"""
        self.latest_file.records = [{
            "file_id": "f1", "ip": "127.0.0.1", "port": "0", "name": "a.txt",
            "size": 1, "kind": "file",
        }]
        payload = self._pull_json()
        self.assertEqual(payload["type"], "text")

    def test_dirty_port_does_not_poison_valid_records(self):
        """一条脏记录被跳过，同批的合法记录照常交付。"""
        self.latest_file.records = [
            {"file_id": "bad", "ip": "127.0.0.1", "port": 0, "name": "b.txt",
             "size": 1, "kind": "file"},
            {"file_id": "ok", "ip": "127.0.0.1", "port": 9000, "name": "a.txt",
             "size": 1, "kind": "file"},
        ]
        payload = self._pull_json()
        self.assertEqual([item["file_id"] for item in payload["files"]], ["ok"])
        self.assertEqual(urlparse(payload["files"][0]["download_url"]).port, 9000)

    def test_record_consumed_exactly_once(self):
        self._push([self._record()])
        self.assertEqual(self._pull_json()["type"], "file_list")
        self.assertEqual(self._pull_json()["type"], "text")


class TestClientSharePort(unittest.TestCase):
    """电脑端上报端口的三级兜底，任何一级为空都不能把 None 发出去。"""

    def _client(self, server_port=None, config_port=None):
        from client.main_menu import SyncClient

        server = MagicMock()
        server.port = server_port
        server.share_entries.return_value = [
            {"file_id": "f1", "path": "D:/a.txt", "name": "a.txt",
             "size": 1, "kind": "file"},
        ]
        config = {
            "server_host": "127.0.0.1", "server_port": 8000,
            "key": "k", "local_name": "pc",
        }
        if config_port is not None:
            config["file_server_port"] = config_port
        return SyncClient(config, server)

    def _posted_port(self, client):
        with patch("client.main_menu.requests.post") as post:
            post.return_value = MagicMock(status_code=200, text="")
            client.reannounce_shares()
        self.assertTrue(post.called, "没有发出登记请求")
        return post.call_args[1]["json"]["port"]

    def test_server_port_wins(self):
        self.assertEqual(self._posted_port(self._client(9100, 9200)), 9100)

    def test_falls_back_to_config_when_server_port_missing(self):
        self.assertEqual(self._posted_port(self._client(None, 9200)), 9200)

    def test_falls_back_to_config_when_server_port_zero(self):
        """0 是 falsy —— 代表「还没就绪」，不是「端口 0」。"""
        self.assertEqual(self._posted_port(self._client(0, 9200)), 9200)

    def test_falls_back_to_default_when_everything_missing(self):
        self.assertEqual(self._posted_port(self._client(None, None)),
                         flask_app.DEFAULT_FILE_PORT)


if __name__ == "__main__":
    unittest.main()
