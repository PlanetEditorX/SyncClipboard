"""剪贴板更新弹窗的触发条件。

重点覆盖「服务端和客户端装在同一台机器」这种部署方式：服务端收到远端文字后
会顺手把内容写进本机剪贴板，桌面客户端随后轮询同一个 latest 文件。此时
「剪贴板里已经是这份内容」并不代表这条记录是回音，恰恰相反，它说明内容刚刚
同步过来了 —— 不能因此吞掉弹窗。
"""

import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from gui.clipboard_handler import ClipboardHandler
from server.api import flask_app
from server.core.text_tracker import TextTracker

# 同机部署时，服务端 local_name 与客户端 local_name 默认都是机器名
MACHINE_NAME = "PC-01"


class _FakeConfig:
    def __init__(self, base_dir, local_name="PC-01", notify_remote_push=True):
        self.TEXT_LATEST_FILE = Path(base_dir) / "text_latest.json"
        self.FILE_LATEST_FILE = Path(base_dir) / "file_latest.json"
        self.local_name = local_name
        self.server_running = False
        self.server_port = 8000
        self.notify_remote_push = notify_remote_push


class ClipboardNotificationTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.config = _FakeConfig(self._tmp.name)
        self.handler = ClipboardHandler(self.config)

        self.notify_patcher = patch("gui.clipboard_handler.show_notification")
        self.notify = self.notify_patcher.start()
        self.addCleanup(self.notify_patcher.stop)

        # 通知账本是跨进程共享的真实文件，测试里指到临时目录，免得
        # 上一次运行留下的 id 把这一次的弹窗吞掉。
        self.ledger_patcher = patch(
            "common.notify_ledger.LEDGER_FILE", Path(self._tmp.name) / "ledger.json"
        )
        self.ledger_patcher.start()
        self.addCleanup(self.ledger_patcher.stop)

    def _write(self, item):
        payload = {"latest_global": item}
        self.config.TEXT_LATEST_FILE.write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )

    def _item(self, content="手机复制的一段话", source="Phone", item_id="id-1", minutes_ago=0):
        return {
            "id": item_id,
            "type": "text",
            "content": content,
            "source": source,
            "timestamp": (datetime.now() - timedelta(minutes=minutes_ago)).isoformat(),
        }

    def test_remote_text_notifies_even_if_clipboard_already_holds_it(self):
        """服务端已把内容写进本机剪贴板（同机部署），弹窗仍必须出现。"""
        content = "手机复制的一段话"
        self._write(self._item(content))

        with patch("pyperclip.paste", return_value=content):
            self.handler.handle_client_latest()

        self.assertEqual(self.notify.call_count, 1)
        _title, msg = self.notify.call_args.args
        self.assertIn("Phone", msg)
        self.assertIn(content, msg)

    def test_local_source_is_silent(self):
        """自己复制的内容不需要提醒自己。"""
        self._write(self._item(source=self.config.local_name))
        self.handler.handle_client_latest()
        self.notify.assert_not_called()

    def test_same_item_is_only_announced_once(self):
        item = self._item()
        self._write(item)
        with patch("pyperclip.paste", return_value=""):
            self.handler.handle_client_latest()
            self.handler.handle_client_latest()
        self.assertEqual(self.notify.call_count, 1)

    def test_repeated_push_of_same_content_is_deduped(self):
        """id 变了但来源与内容都没变（轮询型客户端反复回传），不再弹第二次。"""
        self._write(self._item(item_id="id-1"))
        with patch("pyperclip.paste", return_value=""):
            self.handler.handle_client_latest()
            self._write(self._item(item_id="id-2"))
            self.handler.handle_client_latest()
        self.assertEqual(self.notify.call_count, 1)

    def test_new_content_from_same_source_notifies_again(self):
        with patch("pyperclip.paste", return_value=""):
            self._write(self._item(content="第一条", item_id="id-1"))
            self.handler.handle_client_latest()
            self._write(self._item(content="第二条", item_id="id-2"))
            self.handler.handle_client_latest()
        self.assertEqual(self.notify.call_count, 2)

    def test_expired_item_is_silent(self):
        self._write(self._item(minutes_ago=11))
        with patch("pyperclip.paste", return_value=""):
            self.handler.handle_client_latest()
        self.notify.assert_not_called()

    def test_missing_file_does_nothing(self):
        self.handler.handle_client_latest()
        self.notify.assert_not_called()


class SameMachineEndToEndTest(unittest.TestCase):
    """服务端与客户端装在同一台机器：手机推来的文字必须一路走到桌面弹窗。

    这是用户实际报的场景 —— 分开部署时正常，同机就没有弹窗。差别在于同机
    时服务端会先把文字写进本机剪贴板，桌面端随后读到的「剪贴板已有内容」
    并不代表这条记录是回音。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        latest_path = Path(self._tmp.name) / "text_latest.json"

        patcher = patch("server.core.text_tracker.TEXT_LATEST_FILE", latest_path)
        patcher.start()
        self.addCleanup(patcher.stop)

        for name, new in (
            ("KEY", "secret"),
            ("tracker", TextTracker()),
            ("clients", []),
            ("LOCAL_NAME", MACHINE_NAME),
        ):
            p = patch.object(flask_app, name, new)
            p.start()
            self.addCleanup(p.stop)
        for name in ("copy_text_to_clipboard", "notify_clients", "load_clients_ip"):
            p = patch.object(flask_app, name)
            p.start()
            self.addCleanup(p.stop)

        self.client = flask_app.app.test_client()
        self.config = _FakeConfig(self._tmp.name, local_name=MACHINE_NAME)
        self.handler = ClipboardHandler(self.config)

        notify = patch("gui.clipboard_handler.show_notification")
        self.notify = notify.start()
        self.addCleanup(notify.stop)

    def _push(self, content, source):
        response = self.client.post(
            "/text_sync",
            json={"key": "secret", "content": content, "source": source},
        )
        self.assertEqual(response.status_code, 200)
        return response.get_json()["status"]

    def test_phone_text_reaches_desktop_toast(self):
        self.assertEqual(self._push("手机复制的一段话", "Phone"), "ok")
        self.handler.handle_client_latest()

        self.assertEqual(self.notify.call_count, 1)
        _title, msg = self.notify.call_args.args
        self.assertIn("Phone", msg)
        self.assertIn("手机复制的一段话", msg)

    def test_local_push_is_not_announced_to_itself(self):
        """本机客户端上传的内容，不需要给本机弹窗。"""
        self.assertEqual(self._push("本机复制的", MACHINE_NAME), "ok")
        self.handler.handle_client_latest()
        self.notify.assert_not_called()

    def test_desktop_echo_after_remote_push_does_not_reannounce(self):
        """手机推来后，本机客户端把同一段文字回传，不应再弹一次。"""
        self._push("手机复制的一段话", "Phone")
        self.handler.handle_client_latest()
        self.assertEqual(self.notify.call_count, 1)

        self._push("手机复制的一段话", MACHINE_NAME)
        self.handler.handle_client_latest()
        self.assertEqual(self.notify.call_count, 1)

    def test_own_copy_is_silent_then_remote_copy_notifies(self):
        """同机双开的完整交替：自己复制静默，只有别的设备推来才弹。"""
        # ① 本机复制：自己提醒自己没有意义
        self.assertEqual(self._push("本机复制的第一条", MACHINE_NAME), "ok")
        self.handler.handle_client_latest()
        self.notify.assert_not_called()

        # ② 手机上复制了另一条：这才是要提醒的内容
        self.assertEqual(self._push("手机上复制的第二条", "Phone"), "ok")
        self.handler.handle_client_latest()
        self.assertEqual(self.notify.call_count, 1)
        self.assertIn("Phone", self.notify.call_args.args[1])
        self.assertIn("手机上复制的第二条", self.notify.call_args.args[1])

        # ③ 回到本机复制：再次静默
        self.assertEqual(self._push("本机复制的第三条", MACHINE_NAME), "ok")
        self.handler.handle_client_latest()
        self.assertEqual(self.notify.call_count, 1)


if __name__ == "__main__":
    unittest.main()
