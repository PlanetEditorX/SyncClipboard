# -*- coding: utf-8 -*-
"""iOS 快捷指令运行日志接口 /ios_log。

日志是排查目录同步的唯一抓手，而 iOS 的「存储到文件」保存纯文本会**静默
失败**（v7 真机：日志文件一条都没有，通知里也什么都没说）。所以改成手机把
累积的完整日志 POST 过来，服务端覆盖写 `log/ios_shortcut.log`。
"""

import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

from server.api import flask_app

KEY = "secret"


class IosLogTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.client = flask_app.app.test_client()
        self._patchers = [
            patch.object(flask_app, "KEY", KEY),
            patch.object(flask_app, "BASE_DIR", self.tmp),
        ]
        for patcher in self._patchers:
            patcher.start()

    def tearDown(self):
        for patcher in self._patchers:
            patcher.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    @property
    def log_path(self):
        return os.path.join(self.tmp, "log", "ios_shortcut.log")

    def _post(self, log="x", key=KEY):
        return self.client.post(
            "/ios_log", headers={"key": key}, json={"log": log})

    def test_writes_log_file(self):
        resp = self._post("④ 清单 课程 ｜ 5 个文件\n· a.txt ｜ prefix=课程")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["status"], "ok")
        with open(self.log_path, encoding="utf-8") as handle:
            self.assertIn("prefix=课程", handle.read())

    def test_second_push_overwrites(self):
        """每次推的是累积后的**完整**日志，服务端必须是覆盖写。"""
        self._post("第一段")
        self._post("第一段\n第二段")
        with open(self.log_path, encoding="utf-8") as handle:
            body = handle.read()
        self.assertEqual(body, "第一段\n第二段")

    def test_wrong_key_is_403(self):
        self.assertEqual(self._post("x", key="nope").status_code, 403)
        self.assertFalse(os.path.exists(self.log_path))

    def test_empty_log_is_400(self):
        self.assertEqual(self._post("   ").status_code, 400)
        self.assertFalse(os.path.exists(self.log_path))

    def test_missing_field_is_400(self):
        resp = self.client.post("/ios_log", headers={"key": KEY}, json={})
        self.assertEqual(resp.status_code, 400)

    def test_dotdot_in_log_is_harmless(self):
        """日志内容只是写进固定文件的数据，不参与路径拼接。"""
        self._post("../../etc/passwd\n<b>不转义</b>")
        with open(self.log_path, encoding="utf-8") as handle:
            self.assertIn("../../etc/passwd", handle.read())
        self.assertTrue(os.path.exists(self.log_path))


if __name__ == "__main__":
    unittest.main()
