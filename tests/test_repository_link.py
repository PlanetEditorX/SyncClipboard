"""「查看仓库」入口：面板按钮与托盘菜单都要能拉起默认浏览器。

这里锁的是三件事：
1. 地址只有一处定义（common.updater.PROJECT_PAGE），两边共用；
2. 打开浏览器失败时不能静默——弹窗里要带上地址，用户还能自己复制；
3. 面板按钮点下去真的把仓库地址交给了系统，而不是只改了文案。
"""

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from common.updater import PROJECT_PAGE, REPOSITORY
from common.utils import open_url
from gui import qt_dialogs
from gui.tray_manager import TrayManager


class TestProjectPageConstant(unittest.TestCase):
    def test_page_matches_repository(self):
        self.assertEqual(PROJECT_PAGE, f"https://github.com/{REPOSITORY}")
        self.assertEqual(PROJECT_PAGE, "https://github.com/PlanetEditorX/SyncClipboard")


class TestOpenUrl(unittest.TestCase):
    def test_success_hands_url_to_browser(self):
        with patch("webbrowser.open", return_value=True) as opener:
            ok, error = open_url(PROJECT_PAGE)

        self.assertTrue(ok, error)
        opener.assert_called_once_with(PROJECT_PAGE, new=2)

    def test_no_browser_returns_reason(self):
        with patch("webbrowser.open", return_value=False):
            ok, error = open_url(PROJECT_PAGE)

        self.assertFalse(ok)
        self.assertIn("浏览器", error)

    def test_exception_is_swallowed_into_reason(self):
        with patch("webbrowser.open", side_effect=RuntimeError("注册表坏了")):
            ok, error = open_url(PROJECT_PAGE)

        self.assertFalse(ok)
        self.assertIn("注册表坏了", error)

    def test_empty_url_is_rejected_without_touching_browser(self):
        with patch("webbrowser.open") as opener:
            ok, error = open_url("   ")

        self.assertFalse(ok)
        self.assertTrue(error)
        opener.assert_not_called()


class TestTrayRepositoryEntry(unittest.TestCase):
    def test_menu_item_opens_browser(self):
        with patch("gui.tray_manager.open_url", return_value=(True, "")) as opener, \
             patch.object(qt_dialogs, "info_message") as dialog:
            TrayManager.open_repository()

        opener.assert_called_once_with(PROJECT_PAGE)
        dialog.assert_not_called()

    def test_failure_shows_url_in_dialog(self):
        with patch("gui.tray_manager.open_url", return_value=(False, "没有默认浏览器")), \
             patch.object(qt_dialogs, "info_message") as dialog:
            TrayManager.open_repository()

        self.assertEqual(dialog.call_args.args[0], "无法打开浏览器")
        self.assertIn(PROJECT_PAGE, dialog.call_args.args[1])


class TestPanelRepositoryButton(unittest.TestCase):
    """真实窗口 + offscreen 平台：按钮存在、点了真的开浏览器。"""

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance()
        if cls.app is None:
            try:
                cls.app = QApplication([])
            except Exception as exc:  # noqa: BLE001 - 缺平台插件时跳过
                raise unittest.SkipTest(f"无法创建 QApplication: {exc}")

    def _build_window(self):
        from gui.qt_main_window import MainWindow

        config = SimpleNamespace(
            server_host="192.0.2.10",
            server_port=8000,
            local_name="Test-PC",
            file_server_port=8899,
            key="secret",
            save_path=str(Path(tempfile.gettempdir())),
            appearance_mode="Light",
            is_autostart_enabled=lambda: False,
        )
        manager = SimpleNamespace(config=config, file_handler=None)
        manager.get_service_state = lambda: {
            "server_enabled": False,
            "client_enabled": False,
            "server_alive": False,
            "client_alive": False,
            "busy": False,
        }
        manager.restart_services_async = lambda: True
        manager.restart_client_async = lambda: True

        window = MainWindow(manager)
        window._status_timer.stop()
        self.addCleanup(window.deleteLater)
        return window

    def test_button_sits_next_to_update_with_repo_in_tooltip(self):
        window = self._build_window()

        self.assertEqual(window.repo_button.text(), "查看仓库")
        self.assertIn(PROJECT_PAGE, window.repo_button.toolTip())

    def test_click_opens_repository(self):
        window = self._build_window()
        with patch("gui.qt_main_window.open_url", return_value=(True, "")) as opener, \
             patch.object(qt_dialogs, "info_message") as dialog:
            window.repo_button.click()

        opener.assert_called_once_with(PROJECT_PAGE)
        dialog.assert_not_called()

    def test_click_failure_tells_the_user(self):
        window = self._build_window()
        with patch("gui.qt_main_window.open_url", return_value=(False, "打不开")), \
             patch.object(qt_dialogs, "info_message") as dialog:
            window.repo_button.click()

        self.assertEqual(dialog.call_args.args[0], "无法打开浏览器")
        self.assertIn(PROJECT_PAGE, dialog.call_args.args[1])


if __name__ == "__main__":
    unittest.main()
