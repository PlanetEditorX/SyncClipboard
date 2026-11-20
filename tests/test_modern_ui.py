import importlib
import json
import queue
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from common.updater import UpdateResult
from gui.config_manager import ConfigManager
from gui.main_window import validate_client_settings
from gui.qt_main_window import MainWindow
from gui.qt_theme import LIGHT
from gui.service_manager import ServiceManager


class TestClientSettingsValidation(unittest.TestCase):
    def test_valid_settings_are_trimmed_and_ports_are_normalized(self):
        result = validate_client_settings(
            " 192.0.2.10 ", "8000", " Office-PC ", "8899", " secret ", " D:/Downloads "
        )

        self.assertEqual(result["server_host"], "192.0.2.10")
        self.assertEqual(result["server_port"], 8000)
        self.assertEqual(result["file_server_port"], 8899)
        self.assertEqual(result["local_name"], "Office-PC")

    def test_key_keeps_intentional_surrounding_spaces(self):
        result = validate_client_settings(
            "host", "8000", "PC", "8899", " secret with spaces ", "D:/Downloads"
        )
        self.assertEqual(result["key"], " secret with spaces ")

    def test_invalid_port_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "服务器端口"):
            validate_client_settings("host", "70000", "PC", "8899", "key", "D:/Downloads")

    def test_empty_key_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "共享密钥"):
            validate_client_settings("host", "8000", "PC", "8899", "  ", "D:/Downloads")


class TestModernUiCompatibility(unittest.TestCase):
    def test_failed_settings_save_rolls_back_runtime_values(self):
        config = SimpleNamespace(
            server_host="old-host",
            server_port=8000,
            local_name="Old-PC",
            file_server_port=8899,
            key="old-key",
            save_path="D:/Old",
            update_client_config=MagicMock(return_value=False),
        )
        window = MainWindow.__new__(MainWindow)
        window._palette = dict(LIGHT)
        window.config = config
        window.manager = MagicMock()
        window.settings_message = MagicMock()
        window.server_host_edit = MagicMock(text=MagicMock(return_value="new-host"))
        window.server_port_edit = MagicMock(text=MagicMock(return_value="9000"))
        window.local_name_edit = MagicMock(text=MagicMock(return_value="New-PC"))
        window.file_port_edit = MagicMock(text=MagicMock(return_value="9999"))
        window.key_edit = MagicMock(text=MagicMock(return_value="new-key"))
        window.save_path_edit = MagicMock(text=MagicMock(return_value="D:/New"))

        window.save_settings()

        self.assertEqual(config.server_host, "old-host")
        self.assertEqual(config.server_port, 8000)
        self.assertEqual(config.local_name, "Old-PC")
        self.assertEqual(config.key, "old-key")

    def test_new_version_enables_update_button(self):
        window = MainWindow.__new__(MainWindow)
        window._palette = dict(LIGHT)
        window._update_state = "idle"
        window._update_result = None
        window.update_button = MagicMock()
        window.update_message = MagicMock()

        with patch("gui.qt_main_window.is_self_update_available", return_value=True):
            window._render_update_result(
                UpdateResult(
                    "1.7.0",
                    latest_version="1.8.0",
                    has_update=True,
                    download_url="https://example.com/app.zip",
                )
            )

        self.assertEqual(window._update_state, "available")
        self.assertIn("1.8.0", window.update_message.setText.call_args.args[0])
        self.assertIn("1.8.0", window.update_button.setText.call_args.args[0])
        self.assertTrue(window.update_button.setEnabled.call_args.args[0])

    def test_new_version_without_windows_asset_stays_idle(self):
        window = MainWindow.__new__(MainWindow)
        window._palette = dict(LIGHT)
        window._update_state = "idle"
        window._update_result = None
        window.update_button = MagicMock()
        window.update_message = MagicMock()

        with patch("gui.qt_main_window.is_self_update_available", return_value=True):
            window._render_update_result(
                UpdateResult("1.7.0", latest_version="1.8.0", has_update=True)
            )

        self.assertEqual(window._update_state, "idle")
        self.assertIn("未找到 Windows 安装包", window.update_message.setText.call_args.args[0])

    def test_new_version_in_source_mode_does_not_offer_update(self):
        window = MainWindow.__new__(MainWindow)
        window._palette = dict(LIGHT)
        window._update_state = "idle"
        window._update_result = None
        window.update_button = MagicMock()
        window.update_message = MagicMock()

        with patch("gui.qt_main_window.is_self_update_available", return_value=False):
            window._render_update_result(
                UpdateResult(
                    "1.7.0",
                    latest_version="1.8.0",
                    has_update=True,
                    download_url="https://example.com/app.zip",
                )
            )

        self.assertEqual(window._update_state, "idle")
        self.assertIn("源码运行", window.update_message.setText.call_args.args[0])

    def test_up_to_date_message_is_shown(self):
        window = MainWindow.__new__(MainWindow)
        window._palette = dict(LIGHT)
        window._update_state = "idle"
        window._update_result = None
        window.update_button = MagicMock()
        window.update_message = MagicMock()

        window._render_update_result(UpdateResult("1.7.0", latest_version="1.7.0"))

        self.assertEqual(window._update_state, "idle")
        self.assertIn("已是最新版本", window.update_message.setText.call_args.args[0])

    def test_check_error_resets_button(self):
        window = MainWindow.__new__(MainWindow)
        window._palette = dict(LIGHT)
        window._update_state = "checking"
        window._update_result = None
        window.update_button = MagicMock()
        window.update_message = MagicMock()

        window._render_update_result(UpdateResult("1.7.0", error="检查更新失败，请检查网络后重试"))

        self.assertEqual(window._update_state, "idle")
        self.assertIn("检查更新失败", window.update_message.setText.call_args.args[0])
        self.assertEqual(window.update_button.setText.call_args.args[0], "检查更新")

    def test_importing_tray_manager_does_not_create_a_root_window(self):
        module = importlib.import_module("gui.tray_manager")
        self.assertFalse(hasattr(module, "root"))

    def test_build_script_is_import_safe_and_uses_onedir(self):
        build = importlib.import_module("release.build")
        command = build.build_command()

        self.assertIn("--onedir", command)
        self.assertNotIn("--onefile", command)
        # Qt 由 PyInstaller 官方 hook 负责收集，不再手工搬运 customtkinter 资源
        self.assertNotIn("--add-data", command)
        self.assertIn("--exclude-module=tkinter", command)
        self.assertIn("--exclude-module=customtkinter", command)
        self.assertTrue(any("QtQml" in value for value in command))

    def test_build_embeds_dpi_aware_manifest(self):
        """缺 manifest 时进程会被 DPI 虚拟化，200% 缩放下界面直接糊掉。"""
        build = importlib.import_module("release.build")
        command = build.build_command()

        self.assertTrue(any(str(value).startswith("--manifest=") for value in command))
        self.assertTrue(build.MANIFEST_FILE.exists())
        text = build.MANIFEST_FILE.read_text(encoding="utf-8")
        self.assertIn("PerMonitorV2", text)
        self.assertIn("asInvoker", text)

    def test_clean_step_keeps_manifest_and_drops_build_outputs(self):
        """clean 曾是「删掉白名单外的一切」，会把 app.manifest 一起删掉。

        CI 跑的是 main() -> clean_release_outputs() -> build_command()，
        因此 manifest 必须在 clean 阶段存活，否则 PyInstaller 报找不到文件。
        """
        build = importlib.import_module("release.build")
        with tempfile.TemporaryDirectory() as temp_dir:
            release_dir = Path(temp_dir) / "release"
            (release_dir / "bat").mkdir(parents=True)
            (release_dir / "bat" / "start.bat").write_text("echo", encoding="utf-8")
            (release_dir / "build.py").write_text("# build", encoding="utf-8")
            (release_dir / "app.manifest").write_text("<assembly/>", encoding="utf-8")
            for stale in ("dist", "spec", "build_cache"):
                (release_dir / stale).mkdir()
            (release_dir / "stale.spec").write_text("", encoding="utf-8")

            with patch.object(build, "RELEASE_DIR", release_dir):
                build.clean_release_outputs()

            self.assertTrue((release_dir / "app.manifest").exists())
            self.assertTrue((release_dir / "build.py").exists())
            self.assertTrue((release_dir / "bat" / "start.bat").exists())
            for removed in ("dist", "spec", "build_cache", "stale.spec"):
                self.assertFalse((release_dir / removed).exists(), removed)

    def test_build_assets_keep_templates_separate_from_runtime_config(self):
        build = importlib.import_module("release.build")
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            project = temp / "project"
            release_dir = project / "release"
            app_dir = release_dir / "dist" / "SyncClipboard"
            (project / "config" / "example").mkdir(parents=True)
            (project / "config" / "example" / "client_config.json").write_text("{}", encoding="utf-8")
            (project / "gui" / "icon").mkdir(parents=True)
            app_dir.mkdir(parents=True)
            (app_dir / "SyncClipboard.exe").write_bytes(b"preview")

            with (
                patch.object(build, "PROJECT_ROOT", project),
                patch.object(build, "RELEASE_DIR", release_dir),
                patch.object(build, "APP_DIR", app_dir),
            ):
                build.copy_runtime_assets()

            self.assertTrue((app_dir / "config" / "example" / "client_config.json").exists())
            self.assertFalse((app_dir / "config" / "client_config.json").exists())

    def test_ui_shutdown_releases_waiting_worker(self):
        from common import utils

        root = MagicMock()
        errors = []
        started = threading.Event()
        utils.clear_tk_root()
        utils.set_tk_root(root)

        def worker():
            started.set()
            try:
                utils.post_to_main_thread(lambda: None)
            except Exception as exc:
                errors.append(exc)

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        self.assertTrue(started.wait(timeout=1))
        utils.request_tk_shutdown(root)
        thread.join(timeout=1)
        utils.clear_tk_root(root)

        self.assertFalse(thread.is_alive())
        self.assertTrue(any(isinstance(error, RuntimeError) for error in errors))

    def test_state_and_client_config_preserve_unknown_fields(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            state_file = temp / "config" / "gui_state.json"
            client_file = temp / "config" / "client_config.json"
            server_file = temp / "config" / "server_config.json"
            latest_file = temp / "latest" / "text_latest.json"
            state_file.parent.mkdir(parents=True)
            client_file.parent.mkdir(parents=True, exist_ok=True)

            state_file.write_text(
                json.dumps({"server_running": False, "client_running": True, "future_flag": 7}),
                encoding="utf-8",
            )
            client_file.write_text(
                json.dumps(
                    {
                        "server_host": "192.0.2.10",
                        "server_port": 8000,
                        "key": "secret",
                        "local_name": "Office-PC",
                        "file_server_port": 8899,
                        "save_path": str(temp),
                        "future_option": {"enabled": True},
                    }
                ),
                encoding="utf-8",
            )
            server_file.write_text(
                json.dumps(
                    {
                        "host": "0.0.0.0",
                        "port": 8000,
                        "key": "secret",
                        "local_name": "Office-Server",
                        "future_server_option": True,
                    }
                ),
                encoding="utf-8",
            )

            with (
                patch.object(ConfigManager, "STATE_FILE", state_file),
                patch.object(ConfigManager, "CLIENT_CONFIG", client_file),
                patch.object(ConfigManager, "SERVER_CONFIG", server_file),
                patch.object(ConfigManager, "TEXT_LATEST_FILE", latest_file),
                patch.object(ConfigManager, "FILE_LATEST_FILE", temp / "latest" / "file_latest.json"),
            ):
                manager = ConfigManager()
                manager.load_state()
                external_state = json.loads(state_file.read_text(encoding="utf-8"))
                external_state["added_while_running"] = "state-value"
                state_file.write_text(json.dumps(external_state), encoding="utf-8")
                manager.appearance_mode = "Dark"
                manager.save_state()

                self.assertTrue(manager.load_client_config())
                externally_edited = json.loads(client_file.read_text(encoding="utf-8"))
                externally_edited["added_while_running"] = "preserve-me"
                client_file.write_text(json.dumps(externally_edited), encoding="utf-8")
                manager.server_port = 9000
                self.assertTrue(manager.save_client_config())
                persisted_client = client_file.read_text(encoding="utf-8")
                failed_update = {
                    "server_host": manager.server_host,
                    "server_port": 9200,
                    "local_name": manager.local_name,
                    "file_server_port": manager.file_server_port,
                    "key": manager.key,
                    "save_path": manager.save_path,
                }
                with patch("gui.config_manager.os.replace", side_effect=OSError("disk busy")):
                    self.assertFalse(manager.update_client_config(failed_update))
                self.assertEqual(client_file.read_text(encoding="utf-8"), persisted_client)
                self.assertEqual(list(client_file.parent.glob(".client_config.json.*.tmp")), [])
                self.assertEqual(manager.server_port, 9000)
                self.assertTrue(manager.load_server_config())
                external_server = json.loads(server_file.read_text(encoding="utf-8"))
                external_server["added_while_running"] = "server-value"
                server_file.write_text(json.dumps(external_server), encoding="utf-8")
                manager.server_port = 9100
                self.assertTrue(manager.save_server_config())

            state = json.loads(state_file.read_text(encoding="utf-8"))
            client = json.loads(client_file.read_text(encoding="utf-8"))
            server = json.loads(server_file.read_text(encoding="utf-8"))
            self.assertEqual(state["future_flag"], 7)
            self.assertEqual(state["added_while_running"], "state-value")
            self.assertEqual(state["appearance_mode"], "Dark")
            self.assertEqual(client["future_option"], {"enabled": True})
            self.assertEqual(client["added_while_running"], "preserve-me")
            self.assertEqual(client["server_port"], 9000)
            self.assertEqual(server["host"], "0.0.0.0")
            self.assertTrue(server["future_server_option"])
            self.assertEqual(server["added_while_running"], "server-value")
            self.assertEqual(server["port"], 9100)

    def test_restart_only_restarts_enabled_services(self):
        config = MagicMock(server_running=False, client_running=True)
        manager = ServiceManager(config)
        manager.stop_server = MagicMock()
        manager.start_server = MagicMock()
        manager.stop_client = MagicMock()
        manager.start_client = MagicMock()

        with patch("gui.service_manager.time.sleep"):
            manager.restart_services()

        manager.stop_server.assert_not_called()
        manager.start_server.assert_not_called()
        manager.stop_client.assert_called_once_with(preserve_intent=True)
        manager.start_client.assert_called_once_with()

    def test_start_failure_preserves_enabled_intent(self):
        config = SimpleNamespace(
            server_running=False,
            client_running=False,
            save_state=MagicMock(return_value=True),
        )
        manager = ServiceManager(config)
        process = MagicMock()
        process.start.side_effect = OSError("spawn failed")

        with patch("gui.service_manager.multiprocessing.Process", return_value=process):
            self.assertFalse(manager.start_client())

        self.assertTrue(config.client_running)
        self.assertIsNone(manager.client_process)

    def test_shutdown_runs_after_an_accepted_service_action(self):
        from gui.service_queue import ServiceActionQueue
        from gui.tray_manager import build_shutdown_handler

        order = []
        config = SimpleNamespace(
            server_running=False,
            client_running=False,
            save_state=lambda: order.append("save"),
        )

        class Services:
            server_process = None
            client_process = None

            def start_client(self):
                order.append("start-client")
                config.client_running = True

            def stop_client(self, preserve_intent=False):
                order.append("stop-client")
                if not preserve_intent:
                    config.client_running = False

            def stop_server(self, preserve_intent=False):
                order.append("stop-server")
                if not preserve_intent:
                    config.server_running = False

        services = Services()
        actions = ServiceActionQueue()

        with patch("gui.tray_manager.request_ui_shutdown") as request_shutdown:
            self.assertTrue(actions.submit(services.start_client))
            self.assertTrue(
                actions.shutdown(build_shutdown_handler(services, config, "UI-ROOT"))
            )
            actions.join(timeout=2)

        # 退出动作必须排在已接受的服务操作之后，且意图被还原
        self.assertEqual(order[:3], ["start-client", "stop-server", "stop-client"])
        self.assertEqual(order[-1], "save")
        self.assertTrue(config.client_running)
        request_shutdown.assert_called_once_with("UI-ROOT")

    def test_busy_queue_rejects_a_second_action(self):
        from gui.service_queue import ServiceActionQueue

        gate = threading.Event()
        actions = ServiceActionQueue()
        try:
            self.assertTrue(actions.submit(gate.wait))
            self.assertTrue(actions.busy)
            self.assertFalse(actions.submit(lambda: None))
        finally:
            gate.set()
            actions.join(timeout=2)
        self.assertFalse(actions.busy)


if __name__ == "__main__":
    unittest.main()
