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

    def test_empty_log_is_skipped_not_4xx(self):
        """空日志回 200 但不写盘。

        iOS 的「获取 URL 内容」遇到非 2xx 会**中断整条快捷指令**，日志只是
        附属通道，它自己出问题不该把同步带崩（v10 真机：推了个空 body → 400
        → 指令停在半路）。
        """
        resp = self._post("   ")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["status"], "skipped")
        self.assertFalse(os.path.exists(self.log_path))

    def test_missing_field_is_skipped(self):
        resp = self.client.post("/ios_log", headers={"key": KEY}, json={})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["status"], "skipped")
        self.assertFalse(os.path.exists(self.log_path))

    def test_raw_body_is_accepted(self):
        """正文不是 JSON 时，正文本身就是日志。

        v8 真机：快捷指令连续 POST /ios_log 全是 400，日志一条没上来 —— iOS
        的「获取 URL 内容」在参数组合稍有出入时会改发原始正文。这里兜住它，
        宁可多写一份也不要再丢一次排查线索。
        """
        resp = self.client.post("/ios_log", headers={"key": KEY},
                                data="① 服务器 http://192.168.1.23:8000 ｜ 设备 iPhone",
                                content_type="text/plain")
        self.assertEqual(resp.status_code, 200)
        with open(self.log_path, encoding="utf-8") as handle:
            self.assertIn("192.168.1.23:8000", handle.read())

    def test_new_run_keeps_previous_as_backup(self):
        """新的一次运行开始时，上一次的日志留成 .1。

        同一轮运行里每轮都推全量快照（只追加），所以要按「是不是前缀」区分：
        是前缀 → 覆盖，不是 → 说明开了新的一次运行，旧的留档。不留的话，
        排查「这次为什么没同步」时连一份正常样本都对照不了。
        """
        self._post("=== 文字同步开始 ===\n[1] 文本未变 → 拉取")
        self._post("=== 文字同步开始 ===\n[1] 文本未变 → 拉取\n[2] 已写回剪贴板")
        backup = self.log_path + ".1"
        self.assertFalse(os.path.exists(backup),
                         "同一次运行不该产生备份，否则备份会一直变")
        with open(self.log_path, encoding="utf-8") as handle:
            self.assertIn("[2] 已写回剪贴板", handle.read())

        # 第二次运行：开头依然是「=== 文字同步开始 ===」，但内容不是上一份的
        # 前缀（上一份多了 [2] 那行）
        self._post("=== 文字同步开始 ===\n地址 http://192.168.1.23:8000")
        self.assertTrue(os.path.exists(backup), "新一次运行没把上一份留档")
        with open(backup, encoding="utf-8") as handle:
            self.assertIn("[2] 已写回剪贴板", handle.read())
        with open(self.log_path, encoding="utf-8") as handle:
            self.assertIn("192.168.1.23:8000", handle.read())

    def test_dotdot_in_log_is_harmless(self):
        """日志内容只是写进固定文件的数据，不参与路径拼接。"""
        self._post("../../etc/passwd\n<b>不转义</b>")
        with open(self.log_path, encoding="utf-8") as handle:
            self.assertIn("../../etc/passwd", handle.read())
        self.assertTrue(os.path.exists(self.log_path))


if __name__ == "__main__":
    unittest.main()
