import os
import unittest
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch


class TestTimeFormatting(unittest.TestCase):
    def setUp(self):
        from gui.qt_clients import human_time

        self.human_time = human_time
        self.now = datetime(2026, 11, 24, 15, 30, 0)

    def test_today_and_yesterday_are_relative(self):
        self.assertEqual(
            self.human_time("2026-11-24 15:10:00", self.now), "今天 15:10"
        )
        self.assertEqual(
            self.human_time("2026-11-23 09:02:00", self.now), "昨天 09:02"
        )

    def test_same_year_drops_the_year(self):
        self.assertEqual(
            self.human_time("2026-11-20 09:02:00", self.now), "11-20 09:02"
        )

    def test_other_year_keeps_the_date(self):
        self.assertEqual(self.human_time("2025-11-20 09:02:00", self.now), "2025-11-20")

    def test_empty_and_broken_values(self):
        self.assertEqual(self.human_time("", self.now), "—")
        self.assertEqual(self.human_time(None, self.now), "—")
        self.assertEqual(self.human_time("not-a-date", self.now), "not-a-date")


class TestIdleText(unittest.TestCase):
    def test_ranges(self):
        from gui.qt_clients import idle_text

        self.assertEqual(idle_text(None), "")
        self.assertEqual(idle_text(5), "刚刚")
        self.assertEqual(idle_text(90), "1 分钟前")
        self.assertEqual(idle_text(3 * 3600), "3 小时前")
        self.assertEqual(idle_text(3 * 86400), "3 天前")


class TestRateAndStatus(unittest.TestCase):
    def test_rate_without_attempts_is_a_dash(self):
        from gui.qt_clients import rate_text

        self.assertEqual(rate_text({"push_rate": None}), "—")
        self.assertEqual(rate_text({"push_rate": 0}), "0%")
        self.assertEqual(rate_text({"push_rate": 83.3}), "83.3%")
        self.assertEqual(rate_text({"push_rate": 100.0}), "100%")

    def test_status_calls_unprobeable_devices_polling(self):
        from gui.qt_clients import status_text

        self.assertEqual(status_text({"online": True}), "在线")
        self.assertEqual(status_text({"online": False}), "离线")
        # 探不出来就是只能等它自己来请求，iOS 和还没上报端口的电脑都一样
        self.assertEqual(status_text({"online": None, "os": "iOS"}), "轮询")
        self.assertEqual(status_text({"online": None, "os": "Windows"}), "轮询")


class TestClientsDialog(unittest.TestCase):
    """真实窗口的接线测试，用 offscreen 平台跑，不需要显示器。"""

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance()
        if cls.app is None:
            try:
                cls.app = QApplication([])
            except Exception as exc:  # noqa: BLE001 - 环境缺少平台插件时跳过
                raise unittest.SkipTest(f"无法创建 QApplication: {exc}")

    def _dialog(self):
        from gui.qt_clients import ClientsDialog

        dialog = ClientsDialog(None, host="127.0.0.1", port=8000, key="k")
        dialog._timer.stop()
        self.addCleanup(dialog.deleteLater)
        return dialog

    @staticmethod
    def _payload():
        return {
            "status": "ok",
            "server_name": "SERVER-PC",
            "probed": True,
            "clients": [
                {
                    "local_name": "DESKTOP-A",
                    "ip": "192.168.1.24",
                    "port": 8899,
                    "os": "Windows",
                    "first_seen": "2026-11-20 09:00:00",
                    "last_seen": "2026-11-24 15:00:00",
                    "last_delivery": "2026-11-24 14:58:00",
                    "last_alive": "2026-11-24 15:00:00",
                    "idle_seconds": 120,
                    "push_total": 12,
                    "push_ok": 11,
                    "push_fail": 1,
                    "push_rate": 91.7,
                    "last_push": "2026-11-24 14:58:00",
                    "last_push_ok": True,
                    "pull_ok": 0,
                    "online": True,
                    "is_server": False,
                },
                {
                    "local_name": "熊熊的iPhone",
                    "ip": "192.168.1.30",
                    "port": 0,
                    "os": "iOS",
                    "first_seen": "2026-11-20 09:00:00",
                    "last_seen": "2026-11-24 15:05:00",
                    "last_delivery": "",
                    "last_alive": "2026-11-24 15:05:00",
                    "idle_seconds": 600,
                    "push_total": 3,
                    "push_ok": 3,
                    "push_fail": 0,
                    "push_rate": 100.0,
                    "last_push": "2026-11-24 15:05:00",
                    "last_push_ok": True,
                    "pull_ok": 3,
                    "online": None,
                    "is_server": False,
                },
            ],
            "summary": {
                "total": 2,
                "online": 1,
                "offline": 0,
                "polling": 1,
                "unknown": 1,
                "push_total": 15,
                "push_ok": 14,
                "push_rate": 93.3,
            },
        }

    @staticmethod
    def _text(dialog, row, column):
        return dialog.table.item(row, column).text()

    def test_rows_render_name_ip_attempts_and_rate(self):
        from gui.qt_clients import HEADERS

        dialog = self._dialog()
        dialog._render(self._payload())

        self.assertEqual(dialog.table.rowCount(), 2)
        self.assertEqual(self._text(dialog, 0, HEADERS.index("客户端")), "DESKTOP-A")
        self.assertEqual(self._text(dialog, 0, HEADERS.index("IP")), "192.168.1.24")
        self.assertEqual(self._text(dialog, 0, HEADERS.index("推送次数")), "12")
        self.assertEqual(self._text(dialog, 0, HEADERS.index("推送率")), "91.7%")
        self.assertEqual(self._text(dialog, 0, HEADERS.index("状态")), "在线")
        # iOS 没有推送端口：端口列给「—」，状态是「轮询」而不是「离线」
        self.assertEqual(self._text(dialog, 1, HEADERS.index("推送端口")), "—")
        self.assertEqual(self._text(dialog, 1, HEADERS.index("状态")), "轮询")

    def test_summary_shows_totals_and_rate(self):
        dialog = self._dialog()
        dialog._render(self._payload())
        text = dialog._status.text()
        self.assertIn("共 2 台", text)
        self.assertIn("在线 1", text)
        # iOS 这类探不出在线的设备归到「轮询」，不能写成「未知」
        self.assertIn("轮询 1", text)
        self.assertNotIn("未知", text)
        # 分母是推送次数，所以叫「推送率」，别写成「成功率」
        self.assertIn("累计推送 15 次 / 成功 14 次（推送率 93.3%）", text)
        self.assertNotIn("成功率", text)

    def test_old_server_without_polling_key_still_counts(self):
        """老服务端只下发 unknown：面板要照样算出「轮询 N」。"""
        payload = self._payload()
        payload["summary"].pop("polling")

        dialog = self._dialog()
        dialog._render(payload)

        self.assertIn("轮询 1", dialog._status.text())

    def test_empty_registry_explains_auto_registration(self):
        dialog = self._dialog()
        dialog._render({"status": "ok", "clients": [], "summary": {}})
        self.assertEqual(dialog.table.rowCount(), 0)
        self.assertIn("自动登记", dialog._status.text())

    def test_rows_are_reused_when_identity_is_unchanged(self):
        """身份不变时不能重建行，否则点「移除」的手会被刷新打断。"""
        dialog = self._dialog()
        dialog._render(self._payload())
        button = dialog.action_button(0)

        dialog._render(self._payload())
        self.assertIs(dialog.action_button(0), button)

    def test_local_and_server_roles_are_marked_separately(self):
        """在另一台电脑上看服务器名单：21 是该机的本机，23 是服务器。"""
        from gui.qt_clients import HEADERS, ClientsDialog

        payload = self._payload()
        payload["clients"][1]["local_name"] = "SERVER-PC"
        payload["clients"][1]["os"] = "Windows"
        payload["clients"][1]["is_server"] = True
        dialog = ClientsDialog(
            None, host="192.168.1.23", port=8000, key="k", local_name="DESKTOP-A"
        )
        dialog._timer.stop()
        self.addCleanup(dialog.deleteLater)
        dialog._render(payload)

        name_col = HEADERS.index("客户端")
        os_col = HEADERS.index("系统")
        self.assertEqual(dialog.table.item(0, name_col).text(), "DESKTOP-A（本机）")
        self.assertEqual(dialog.table.item(1, name_col).text(), "SERVER-PC（服务器）")
        # 「本机」不再挂在系统列上，也不会把服务器冒认成查看者本机
        self.assertEqual(dialog.table.item(0, os_col).text(), "Windows")
        self.assertEqual(dialog.table.item(1, os_col).text(), "Windows")

    def test_server_itself_shows_both_roles(self):
        from gui.qt_clients import HEADERS, ClientsDialog

        payload = self._payload()
        payload["clients"][0]["is_server"] = True
        dialog = ClientsDialog(None, key="k", local_name="DESKTOP-A")
        dialog._timer.stop()
        self.addCleanup(dialog.deleteLater)
        dialog._render(payload)

        text = dialog.table.item(0, HEADERS.index("客户端")).text()
        self.assertIn("本机", text)
        self.assertIn("服务器", text)

    def test_push_rate_is_never_colored_as_failure(self):
        """推送率低是使用节奏，不是故障：不能满屏红。"""
        from gui.qt_clients import HEADERS

        dialog = self._dialog()
        payload = self._payload()
        payload["clients"][1]["push_rate"] = 3.1
        dialog._render(payload)

        rate_col = HEADERS.index("推送率")
        for row in range(dialog.table.rowCount()):
            color = dialog.table.item(row, rate_col).foreground().color().name().lower()
            self.assertNotIn(color, (dialog._danger.lower(), dialog._warning.lower()))

    def test_blacklist_row_disables_remove_and_shows_unblock(self):
        from gui.qt_clients import HEADERS

        dialog = self._dialog()
        payload = self._payload()
        payload["clients"][1]["blacklisted"] = True
        payload["summary"]["blacklisted"] = 1
        dialog._render(payload)

        self.assertEqual(dialog.blacklist_button(0).text(), "黑名单")
        self.assertEqual(dialog.blacklist_button(1).text(), "解除")
        self.assertTrue(dialog.action_button(0).isEnabled())
        self.assertFalse(dialog.action_button(1).isEnabled())
        self.assertIn("已拉黑", dialog.table.item(1, HEADERS.index("状态")).text())
        self.assertIn("黑名单 1", dialog._status.text())

    def test_add_to_blacklist_posts_block_true(self):
        from PySide6.QtWidgets import QMessageBox

        dialog = self._dialog()
        dialog._render(self._payload())
        with patch.object(dialog, "_post_blacklist", return_value=(True, "")) as post, \
                patch.object(dialog, "_fetch", return_value=(self._payload(), None)), \
                patch("gui.qt_clients.QMessageBox.question", return_value=QMessageBox.Yes):
            dialog._on_blacklist_row(0)
        post.assert_called_once_with({"local_name": "DESKTOP-A", "block": True})

    def test_unblock_posts_block_false(self):
        from PySide6.QtWidgets import QMessageBox

        dialog = self._dialog()
        payload = self._payload()
        payload["clients"][1]["blacklisted"] = True
        dialog._render(payload)
        with patch.object(dialog, "_post_blacklist", return_value=(True, "")) as post, \
                patch.object(dialog, "_fetch", return_value=(payload, None)), \
                patch("gui.qt_clients.QMessageBox.question", return_value=QMessageBox.Yes):
            dialog._on_blacklist_row(1)
        post.assert_called_once_with({"local_name": "熊熊的iPhone", "block": False})

    def test_blacklist_action_cancelled_does_nothing(self):
        from PySide6.QtWidgets import QMessageBox

        dialog = self._dialog()
        dialog._render(self._payload())
        with patch.object(dialog, "_post_blacklist") as post, \
                patch("gui.qt_clients.QMessageBox.question", return_value=QMessageBox.No):
            dialog._on_blacklist_row(0)
        post.assert_not_called()

    def test_remote_server_makes_dialog_read_only(self):
        dialog = self._dialog()
        dialog.set_endpoint(host="192.168.1.23")
        payload = self._payload()
        payload["can_manage"] = False
        dialog._render(payload)

        for row in range(dialog.table.rowCount()):
            self.assertFalse(dialog.action_button(row).isEnabled())
            self.assertFalse(dialog.blacklist_button(row).isEnabled())
        self.assertFalse(dialog._clear_button.isEnabled())
        self.assertFalse(dialog._manage_hint.isHidden())
        self.assertIn("只能查看", dialog._manage_hint.text())
        self.assertIn("config/client_ip.json", dialog._manage_hint.text())
        self.assertIn("192.168.1.23", dialog._manage_hint.text())

    def test_read_only_dialog_never_posts(self):
        dialog = self._dialog()
        payload = self._payload()
        payload["can_manage"] = False
        dialog._render(payload)

        with patch.object(dialog, "_post_delete") as delete, \
                patch.object(dialog, "_post_blacklist") as block:
            dialog._on_remove_row(0)
            dialog._on_blacklist_row(0)
            dialog._on_clear_all()

        delete.assert_not_called()
        block.assert_not_called()

    def test_permission_comes_back_when_local_server_says_so(self):
        dialog = self._dialog()
        read_only = self._payload()
        read_only["can_manage"] = False
        dialog._render(read_only)
        writable = self._payload()
        writable["can_manage"] = True
        dialog._render(writable)

        self.assertTrue(dialog.action_button(0).isEnabled())
        self.assertTrue(dialog.blacklist_button(0).isEnabled())
        self.assertTrue(dialog._clear_button.isEnabled())
        self.assertTrue(dialog._manage_hint.isHidden())

    def test_switching_endpoint_revokes_permission_until_refreshed(self):
        dialog = self._dialog()
        dialog._render(self._payload())
        self.assertTrue(dialog.action_button(0).isEnabled())

        dialog.set_endpoint(host="192.168.1.23")
        self.assertFalse(dialog.action_button(0).isEnabled())
        self.assertFalse(dialog._clear_button.isEnabled())


    def test_action_button_is_not_clipped_by_row_height(self):
        """「移除」按钮曾被默认行高裁掉下边框：行高必须容得下按钮 + 上下留白。"""
        from gui.qt_clients import (
            ACTION_BUTTON_HEIGHT,
            ACTION_COLUMN,
            ACTION_ROW_HEIGHT,
            ACTION_ROW_PADDING,
        )

        dialog = self._dialog()
        payload = self._payload()
        dialog._render(payload)
        with patch.object(dialog, "_fetch", return_value=(payload, None)):
            dialog.resize(1120, 460)
            dialog.show()
            self.app.processEvents()

        for row in range(dialog.table.rowCount()):
            button = dialog.action_button(row)
            holder = dialog.table.cellWidget(row, ACTION_COLUMN)
            self.assertGreaterEqual(dialog.table.rowHeight(row), ACTION_ROW_HEIGHT)
            self.assertGreaterEqual(button.height(), ACTION_BUTTON_HEIGHT)
            # 按钮整块落在容器里：底边不越界，上下也各有留白
            self.assertGreaterEqual(
                holder.height(), button.height() + ACTION_ROW_PADDING
            )
            self.assertLessEqual(button.y() + button.height(), holder.height())

    def test_row_remove_posts_local_name(self):
        from PySide6.QtWidgets import QMessageBox

        dialog = self._dialog()
        dialog._render(self._payload())

        with patch.object(dialog, "_post_delete", return_value=(True, "", 1)) as post, \
                patch.object(dialog, "_fetch", return_value=(self._payload(), None)), \
                patch("gui.qt_clients.QMessageBox.question", return_value=QMessageBox.Yes):
            dialog._on_remove_row(0)

        post.assert_called_once_with({"local_name": "DESKTOP-A"})

    def test_row_remove_cancelled_does_nothing(self):
        from PySide6.QtWidgets import QMessageBox

        dialog = self._dialog()
        dialog._render(self._payload())

        with patch.object(dialog, "_post_delete") as post, \
                patch("gui.qt_clients.QMessageBox.question", return_value=QMessageBox.No):
            dialog._on_remove_row(0)

        post.assert_not_called()

    def test_clear_all_sends_all_flag(self):
        dialog = self._dialog()
        dialog._render(self._payload())

        from PySide6.QtWidgets import QMessageBox

        with patch.object(dialog, "_post_delete", return_value=(True, "", 2)) as post, \
                patch.object(dialog, "_fetch", return_value=({"clients": []}, None)), \
                patch("gui.qt_clients.QMessageBox.question", return_value=QMessageBox.Yes):
            dialog._on_clear_all()

        post.assert_called_once_with({"all": True})

    def test_failed_fetch_shows_reason_and_keeps_previous_rows(self):
        dialog = self._dialog()
        dialog._render(self._payload())
        with patch.object(dialog, "_fetch", return_value=(None, "Connection refused")):
            dialog.refresh_now()

        self.assertIn("Connection refused", dialog._status.text())
        self.assertEqual(dialog.table.rowCount(), 2)


if __name__ == "__main__":
    unittest.main()
