"""托盘/面板「打开配置」的兜底，以及面板共享密钥对哪一方生效。

两条真实故障都来自「以为系统能帮我们打开，其实不能」：
- Win10 上 .json 常常没有文件关联，os.startfile 抛 WinError -2147221003，
  托盘菜单点了像没反应（异常只写进日志）；
- 面板密钥只写 client_config.json，连接指向本机时本机服务器仍认旧密钥，
  客户端拿新密钥敲门直接 401，界面上却看不出原因。
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from common.utils import open_path
from gui import qt_dialogs
from gui.config_manager import LOCAL_SERVER_HOST, ConfigManager
from gui.qt_main_window import MainWindow, points_to_local_server
from gui.tray_manager import TrayManager
from gui.qt_theme import LIGHT

BASE_CONFIG = {
    "server_host": "192.168.1.10",
    "server_port": 8000,
    "key": "old-key",
    "local_name": "Office-PC",
    "file_server_port": 8899,
    "save_path": "D:/Downloads",
}


class TestOpenPathFallback(unittest.TestCase):
    """startfile 打不开时，按目标类型退到记事本 / 资源管理器。"""

    def test_file_falls_back_to_notepad(self):
        target = Path("C:/demo/config/client_config.json")
        with patch("common.utils.os.startfile", side_effect=OSError("没有文件关联")), \
             patch("common.utils.subprocess.Popen") as popen:
            ok, error = open_path(target)

        self.assertTrue(ok, error)
        self.assertEqual(popen.call_args.args[0], ["notepad.exe", str(target)])

    @unittest.skipUnless(os.name == "nt", "仅 Windows 有 explorer 兜底")
    def test_directory_falls_back_to_explorer(self):
        target = Path("C:/demo/config")
        with patch("common.utils.os.startfile", side_effect=OSError("没有文件关联")), \
             patch("common.utils.subprocess.Popen") as popen:
            ok, error = open_path(target, is_directory=True)

        self.assertTrue(ok, error)
        self.assertEqual(popen.call_args.args[0], ["explorer.exe", str(target)])

    def test_primary_open_is_used_when_available(self):
        with patch("common.utils.os.startfile") as startfile, \
             patch("common.utils.subprocess.Popen") as popen:
            ok, error = open_path(Path("C:/demo/a.json"))

        self.assertTrue(ok, error)
        startfile.assert_called_once()
        popen.assert_not_called()

    def test_both_failures_return_reason(self):
        with patch("common.utils.os.startfile", side_effect=OSError("没有文件关联")), \
             patch("common.utils.subprocess.Popen", side_effect=OSError("没装记事本")):
            ok, error = open_path(Path("C:/demo/a.json"))

        self.assertFalse(ok)
        self.assertIn("没有文件关联", error)
        self.assertIn("没装记事本", error)


class TestTrayOpenConfig(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "client_config.json"
        # 直接按未绑定方法调用，不需要真的 QSystemTrayIcon
        self.path.write_text("{}", encoding="utf-8")

    def test_existing_file_uses_fallback_opener(self):
        with patch("gui.tray_manager.BASE_DIR", self.path.parent.parent), \
             patch("gui.tray_manager.open_path", return_value=(True, "")) as opener, \
             patch.object(qt_dialogs, "info_message") as dialog:
            # BASE_DIR/config/<name>，所以把文件放到 <tmp>/config/ 下
            config_dir = self.path.parent.parent / "config"
            config_dir.mkdir(exist_ok=True)
            target = config_dir / "client_config.json"
            target.write_text("{}", encoding="utf-8")
            TrayManager._open_config("client_config.json")

        opener.assert_called_once_with(target)
        dialog.assert_not_called()

    def test_failed_open_shows_dialog_instead_of_silence(self):
        config_dir = self.path.parent.parent / "config"
        config_dir.mkdir(exist_ok=True)
        (config_dir / "client_config.json").write_text("{}", encoding="utf-8")
        with patch("gui.tray_manager.BASE_DIR", self.path.parent.parent), \
             patch("gui.tray_manager.open_path", return_value=(False, "打不开")), \
             patch.object(qt_dialogs, "info_message") as dialog:
            TrayManager._open_config("client_config.json")

        self.assertEqual(dialog.call_args.args[0], "无法打开配置文件")

    def test_missing_file_tells_the_user(self):
        with patch("gui.tray_manager.BASE_DIR", self.path.parent.parent), \
             patch.object(qt_dialogs, "info_message") as dialog:
            TrayManager._open_config("server_config.json")

        self.assertEqual(dialog.call_args.args[0], "配置文件尚未生成")


class TestServerKeySync(unittest.TestCase):
    """共享密钥：连本机时两边一起改，连远端时只改客户端。"""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.client_path = self.root / "client_config.json"
        self.server_path = self.root / "server_config.json"
        self.patches = [
            patch.object(ConfigManager, "CLIENT_CONFIG", self.client_path),
            patch.object(ConfigManager, "SERVER_CONFIG", self.server_path),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def _manager(self, **overrides):
        data = dict(BASE_CONFIG)
        data.update(overrides)
        self.client_path.write_text(json.dumps(data), encoding="utf-8")
        manager = ConfigManager()
        self.assertTrue(manager.load_client_config())
        return manager

    def test_apply_key_only_touches_that_field(self):
        self.server_path.write_text(
            json.dumps({"key": "old-key", "port": 8001, "host": "0.0.0.0",
                        "local_name": "Srv", "file_share_ttl_minutes": 30}),
            encoding="utf-8",
        )
        manager = self._manager(key="new-key")

        self.assertTrue(manager.apply_key_to_server_config())
        saved = json.loads(self.server_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["key"], "new-key")
        self.assertEqual(saved["port"], 8001)
        self.assertEqual(saved["host"], "0.0.0.0")
        self.assertEqual(saved["local_name"], "Srv")
        self.assertEqual(saved["file_share_ttl_minutes"], 30)

    def test_switching_to_local_server_brings_key_along(self):
        manager = self._manager(server_host="192.168.1.10", key="client-key")
        self.server_path.write_text(json.dumps({"key": "legacy-key", "port": 8000}),
                                    encoding="utf-8")

        self.assertTrue(manager.use_local_server())
        saved = json.loads(self.server_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["key"], "client-key")
        self.assertEqual(manager.server_host, LOCAL_SERVER_HOST)

    def test_points_to_local_server_aliases(self):
        for host in ("127.0.0.1", " localhost ", "::1", "[::1]"):
            self.assertTrue(points_to_local_server(host), host)
        for host in ("192.168.1.23", "", None, "127.0.0.2"):
            self.assertFalse(points_to_local_server(host), host)


class _BareWindow:
    """只装 save_settings 需要的控件，省掉 Qt 窗口构造。"""

    def __init__(self, host, key, server_enabled=True):
        self._palette = dict(LIGHT)
        self.config = SimpleNamespace(
            server_host="old-host",
            server_port=8000,
            local_name="Old-PC",
            file_server_port=8899,
            key="old-key",
            save_path="D:/Old",
            file_share_ttl_minutes=30,
            text_sync_mode="both",
            file_sync_mode="both",
            folder_sync_enabled=True,
            notify_remote_push=True,
            update_client_config=MagicMock(return_value=True),
            apply_key_to_server_config=MagicMock(return_value=True),
            apply_file_share_ttl_to_server_config=MagicMock(return_value=True),
        )
        self.manager = MagicMock()
        self.manager.get_service_state.return_value = {
            "server_enabled": server_enabled, "client_enabled": True, "busy": False,
            "server_alive": server_enabled, "client_alive": True,
        }
        self.settings_message = MagicMock()
        self.welcome_detail = MagicMock()
        self.server_host_edit = MagicMock()
        self.server_host_edit.text.return_value = host
        self.server_port_edit = MagicMock()
        self.server_port_edit.text.return_value = "8000"
        self.local_name_edit = MagicMock()
        self.local_name_edit.text.return_value = "PC"
        self.file_port_edit = MagicMock()
        self.file_port_edit.text.return_value = "8899"
        self.key_edit = MagicMock()
        self.key_edit.text.return_value = key
        self.save_path_edit = MagicMock()
        self.save_path_edit.text.return_value = "D:/Old"
        self.key_hint = None     # 裸窗口没有提示标签，_refresh_key_hint 应跳过

    def save(self):
        window = MainWindow.__new__(MainWindow)
        for name, value in vars(self).items():
            setattr(window, name, value)
        window.save_settings()
        return window


class TestPanelSaveKeyBehaviour(unittest.TestCase):
    def test_local_host_syncs_key_and_restarts_server(self):
        window = _BareWindow(LOCAL_SERVER_HOST, "new-key")

        window.save()

        window.config.apply_key_to_server_config.assert_called_once_with()
        window.manager.restart_services_async.assert_called_once_with()

    def test_remote_host_without_local_server_leaves_its_key_alone(self):
        window = _BareWindow("192.168.1.23", "remote-key", server_enabled=False)

        window.save()

        window.config.apply_key_to_server_config.assert_not_called()
        # 连的是远端，只重启客户端让新凭证生效
        window.manager.restart_client_async.assert_called_once_with()
        window.manager.restart_services_async.assert_not_called()

    def test_remote_host_but_local_server_on_syncs_key(self):
        """本机服务器开着时，面板密钥就是它的密钥 —— 哪怕客户端连的是 LAN IP。"""
        window = _BareWindow("192.168.1.23", "new-key", server_enabled=True)

        window.save()

        window.config.apply_key_to_server_config.assert_called_once_with()
        window.manager.restart_services_async.assert_called_once_with()

    def test_unchanged_key_does_not_touch_server(self):
        window = _BareWindow(LOCAL_SERVER_HOST, "old-key")

        window.save()

        window.config.apply_key_to_server_config.assert_not_called()


if __name__ == "__main__":
    unittest.main()
