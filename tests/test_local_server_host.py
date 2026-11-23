"""「开启本机服务器 → 连接地址自动改成 127.0.0.1」的回归测试。

两个层次：
- ConfigManager：127.0.0.1 必须是能落盘、能重新读回来的合法值；
- TrayManager：开关动作里配置写盘、服务启停、客户端重启的先后顺序。

TrayManager 是 QObject，测试里不实例化它，直接以未绑定方法的形式调用，
这样不需要 Qt 运行时，也和项目里其他纯逻辑测试的做法一致。
"""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from gui.config_manager import LOCAL_SERVER_HOST, ConfigManager
from gui.tray_manager import TrayManager


BASE_CONFIG = {
    "server_host": "192.168.1.10",
    "server_port": 8000,
    "key": "secret",
    "local_name": "Office-PC",
    "file_server_port": 8899,
    "save_path": "D:/Downloads",
}


class _ConfigCase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        for name in ("CLIENT_CONFIG", "SERVER_CONFIG", "STATE_FILE"):
            patcher = patch.object(ConfigManager, name, self.root / f"{name}.json")
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client_config = self.root / "CLIENT_CONFIG.json"

    def write_config(self, **overrides):
        data = dict(BASE_CONFIG)
        data.update(overrides)
        self.client_config.write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8"
        )
        return data


class TestLocalHostIsARealConfigValue(_ConfigCase):
    def test_load_does_not_rewrite_local_host_to_lan_ip(self):
        """旧逻辑会把 127.0.0.1 自动改成本机 LAN IP，开关一保存就被覆盖。"""
        self.write_config(server_host=LOCAL_SERVER_HOST)

        manager = ConfigManager()
        with patch("gui.config_manager.socket.gethostname", return_value="RealPC"):
            self.assertTrue(manager.load_client_config())

        self.assertEqual(manager.server_host, LOCAL_SERVER_HOST)
        saved = json.loads(self.client_config.read_text(encoding="utf-8"))
        self.assertEqual(saved["server_host"], LOCAL_SERVER_HOST)

    def test_new_config_defaults_to_local_host(self):
        manager = ConfigManager()
        self.assertTrue(manager.load_client_config())

        self.assertEqual(manager.server_host, LOCAL_SERVER_HOST)
        saved = json.loads(self.client_config.read_text(encoding="utf-8"))
        self.assertEqual(saved["server_host"], LOCAL_SERVER_HOST)
        self.assertEqual(saved["server_port"], 8000)

    def test_use_local_server_switches_only_the_host(self):
        self.write_config(server_host="192.168.1.10", key=" spaced key ")

        manager = ConfigManager()
        self.assertTrue(manager.load_client_config())
        self.assertTrue(manager.use_local_server())

        self.assertEqual(manager.server_host, LOCAL_SERVER_HOST)
        saved = json.loads(self.client_config.read_text(encoding="utf-8"))
        self.assertEqual(saved["server_host"], LOCAL_SERVER_HOST)
        # 只改这一项，端口 / 密钥 / 设备名都不能被顺手重置
        for field in ("server_port", "key", "local_name", "file_server_port", "save_path"):
            self.assertEqual(saved[field], BASE_CONFIG[field] if field != "key" else " spaced key ")

    def test_use_local_server_is_idempotent(self):
        self.write_config(server_host=LOCAL_SERVER_HOST)

        manager = ConfigManager()
        self.assertTrue(manager.load_client_config())
        with patch.object(manager, "_save_client_config_locked") as save:
            self.assertFalse(manager.use_local_server())
            save.assert_not_called()

    def test_failed_persist_rolls_back_in_memory(self):
        self.write_config(server_host="192.168.1.10")

        manager = ConfigManager()
        self.assertTrue(manager.load_client_config())
        with patch.object(manager, "_save_client_config_locked", return_value=False):
            self.assertFalse(manager.use_local_server())
        self.assertEqual(manager.server_host, "192.168.1.10")


class _Actions:
    """只记录被接受的 action，由测试显式执行。"""

    def __init__(self, accept=True):
        self.actions = []
        self.accept = accept

    def submit(self, action):
        if not self.accept:
            return False
        self.actions.append(action)
        return True

    def run(self):
        for action in self.actions:
            action()


def _manager(host=LOCAL_SERVER_HOST, client_running=True, host_will_change=None):
    """伪造 TrayManager 需要的属性；host_will_change 模拟是否真的改了地址。"""
    state = {"host": host}

    def use_local_server():
        if state["host"] == LOCAL_SERVER_HOST:
            return False
        state["host"] = LOCAL_SERVER_HOST
        return True if host_will_change is None else host_will_change

    config = SimpleNamespace(
        server_host=host,
        server_port=8000,
        local_name="PC",
        client_running=client_running,
        use_local_server=use_local_server,
    )
    services = SimpleNamespace(
        start_server=MagicMock(return_value=True),
        stop_server=MagicMock(return_value=True),
        start_client=MagicMock(return_value=True),
        stop_client=MagicMock(return_value=True),
    )
    actions = _Actions()
    window = MagicMock()
    manager = SimpleNamespace(
        config=config,
        services=services,
        _actions=actions,
        window=window,
        state=state,
    )
    # 两个私有辅助方法同样以未绑定方法的形式挂到伪造对象上
    manager._restart_running_client = (
        lambda: TrayManager._restart_running_client(manager)
    )
    manager._notify_local_server_host = (
        lambda: TrayManager._notify_local_server_host(manager)
    )
    return manager


class TestServiceSwitchRebindsLocalHost(unittest.TestCase):
    def _request(self, manager, service, enabled):
        with patch("gui.tray_manager.post_to_main_thread_no_wait") as post:
            accepted = TrayManager.request_service_state(manager, service, enabled)
            manager._actions.run()
            return accepted, post

    def test_enabling_server_rebinds_and_restarts_client(self):
        manager = _manager(host="192.168.1.10")

        accepted, post = self._request(manager, "server", True)

        self.assertTrue(accepted)
        self.assertEqual(manager.state["host"], LOCAL_SERVER_HOST)
        manager.services.start_server.assert_called_once_with()
        manager.services.stop_client.assert_called_once_with(preserve_intent=True)
        manager.services.start_client.assert_called_once_with()
        self.assertEqual(
            post.call_args.args,
            (manager.window.on_local_server_host_applied, LOCAL_SERVER_HOST),
        )

    def test_disabling_server_leaves_host_alone(self):
        manager = _manager(host="192.168.1.10")

        accepted, post = self._request(manager, "server", False)

        self.assertTrue(accepted)
        self.assertEqual(manager.state["host"], "192.168.1.10")
        manager.services.stop_server.assert_called_once_with()
        manager.services.start_server.assert_not_called()
        manager.services.stop_client.assert_not_called()
        post.assert_not_called()

    def test_client_switch_does_not_touch_host(self):
        manager = _manager(host="192.168.1.10")

        accepted, post = self._request(manager, "client", True)

        self.assertTrue(accepted)
        self.assertEqual(manager.state["host"], "192.168.1.10")
        manager.services.start_client.assert_called_once_with()
        manager.services.stop_client.assert_not_called()
        post.assert_not_called()

    def test_no_restart_when_host_already_local(self):
        manager = _manager(host=LOCAL_SERVER_HOST)

        _, post = self._request(manager, "server", True)

        manager.services.start_server.assert_called_once_with()
        manager.services.stop_client.assert_not_called()
        post.assert_not_called()

    def test_no_restart_when_server_fails_to_start(self):
        manager = _manager(host="192.168.1.10")
        manager.services.start_server.return_value = False

        _, post = self._request(manager, "server", True)

        manager.services.stop_client.assert_not_called()
        post.assert_not_called()

    def test_client_not_running_still_updates_the_panel(self):
        manager = _manager(host="192.168.1.10", client_running=False)

        _, post = self._request(manager, "server", True)

        manager.services.stop_client.assert_not_called()
        manager.services.start_client.assert_not_called()
        self.assertEqual(manager.state["host"], LOCAL_SERVER_HOST)
        post.assert_called_once()

    def test_busy_queue_does_not_change_config(self):
        manager = _manager(host="192.168.1.10")
        manager._actions.accept = False

        accepted, post = self._request(manager, "server", True)

        self.assertFalse(accepted)
        self.assertEqual(manager.state["host"], "192.168.1.10")
        manager.services.start_server.assert_not_called()
        post.assert_not_called()

    def test_restart_client_async_reuses_the_same_helper(self):
        manager = _manager(host="192.168.1.10")

        self.assertTrue(TrayManager.restart_client_async(manager))
        manager._actions.run()

        manager.services.stop_client.assert_called_once_with(preserve_intent=True)
        manager.services.start_client.assert_called_once_with()

    def test_unknown_service_is_rejected(self):
        with self.assertRaises(ValueError):
            TrayManager.request_service_state(_manager(), "printer", True)


if __name__ == "__main__":
    unittest.main()
