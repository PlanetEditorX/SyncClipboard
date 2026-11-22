"""文件拉取接口的 type 语义。

历史实现不论内容是什么都返回 type=file_list，手机端（含 iOS 快捷指令）
无法据此判断「这是目录的压缩包，要先等打包」还是普通文件。
现在只共享一个目录时返回 type=folder，其余保持 file_list 以兼容旧客户端。
"""

import unittest
from unittest.mock import MagicMock, patch

from server.api import flask_app

KEY = "secret"
DESKTOP = "DESKTOP-TEST"


class _FakeFileLatest:
    def __init__(self, records):
        self._records = list(records)
        self.clear_calls = 0

    def get_all_files(self):
        return list(self._records)

    def clear(self):
        self.clear_calls += 1
        self._records = []


def _record(file_id, name, kind, size=1024, ip="192.168.1.24", port=8899):
    return {
        "file_id": file_id,
        "name": name,
        "size": size,
        "source": DESKTOP,
        "kind": kind,
        "ip": ip,
        "port": port,
    }


class RequestFileTypeTest(unittest.TestCase):
    def setUp(self):
        self.client = flask_app.app.test_client()
        self.tracker = MagicMock()
        self.tracker.get_global_latest.return_value = None
        for name, value in (("KEY", KEY), ("tracker", self.tracker)):
            patcher = patch.object(flask_app, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _pull(self, records, key=KEY):
        fake = _FakeFileLatest(records)
        patcher = patch.object(flask_app, "latest_file", fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        response = self.client.post(
            "/request_file",
            headers={"key": key},
            json={"source": "熊熊的iPhone"},
        )
        return response, fake

    # ---------- 核心：目录用 folder ----------

    def test_single_folder_returns_folder_type(self):
        response, fake = self._pull([_record("bb8d0d3c", "课程.zip", "folder")])
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["type"], "folder")
        # 结构保持与 file_list 一致，老解析器读 files 也不会崩
        self.assertEqual(len(payload["files"]), 1)
        self.assertEqual(payload["files"][0]["kind"], "folder")
        self.assertEqual(payload["files"][0]["download_url"],
                         "http://192.168.1.24:8899/file/bb8d0d3c")
        # 目录要等发送端打包，顶层直接给出 prepare 地址，手机端不必自己拼
        self.assertEqual(payload["prepare_url"],
                         "http://192.168.1.24:8899/prepare/bb8d0d3c")
        self.assertEqual(payload["files"][0]["prepare_url"],
                         "http://192.168.1.24:8899/prepare/bb8d0d3c")
        self.assertEqual(fake.clear_calls, 1)

    def test_single_file_stays_file_list(self):
        response, _ = self._pull([_record("aaa", "photo.jpg", "file")])
        payload = response.get_json()
        self.assertEqual(payload["type"], "file_list")
        self.assertNotIn("prepare_url", payload["files"][0])
        self.assertNotIn("prepare_url", payload)

    def test_multiple_files_stay_file_list(self):
        response, _ = self._pull([
            _record("aaa", "a.txt", "file"),
            _record("bbb", "b.txt", "file"),
        ])
        self.assertEqual(response.get_json()["type"], "file_list")

    def test_folder_mixed_with_file_is_file_list(self):
        """多选里既有目录又有文件时，顶层不能用 folder，避免手机端漏掉文件。"""
        response, _ = self._pull([
            _record("aaa", "课程.zip", "folder"),
            _record("bbb", "note.txt", "file"),
        ])
        payload = response.get_json()
        self.assertEqual(payload["type"], "file_list")
        self.assertEqual([item["kind"] for item in payload["files"]], ["folder", "file"])

    def test_record_without_port_is_skipped(self):
        response, _ = self._pull([
            _record("aaa", "broken", "file", port=0),
            _record("bbb", "ok.txt", "file"),
        ])
        payload = response.get_json()
        self.assertEqual(payload["type"], "file_list")
        self.assertEqual([item["file_id"] for item in payload["files"]], ["bbb"])

    def test_only_invalid_records_falls_back_to_text(self):
        response, _ = self._pull([_record("aaa", "broken", "file", ip="")])
        self.assertEqual(response.get_json()["type"], "text")

    def test_wrong_key_is_rejected(self):
        response, fake = self._pull([_record("aaa", "a.txt", "file")], key="bad")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(fake.clear_calls, 0)


if __name__ == "__main__":
    unittest.main()
