"""退出流程的回归测试。

背景：托盘点了「退出」以后出现过「托盘图标消失、任务管理器里进程还在」。
要求是退出这件事必须成功——停服务失败、队列卡住、事件循环退不掉，
最后都要落到「进程一定结束」。
"""

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from gui import tray_manager
from gui.tray_manager import (
    TrayManager,
    force_exit,
    force_exit_after,
    stop_leftover_processes,
)


class FakeProcess:
    def __init__(self, alive=True):
        self.alive = alive
        self.killed = False
        self.joined = []

    def is_alive(self):
        return self.alive

    def kill(self):
        self.killed = True
        self.alive = False

    def join(self, timeout=None):
        self.joined.append(timeout)


class TestForceExit(unittest.TestCase):
    def test_leftover_processes_are_killed(self):
        alive = FakeProcess(alive=True)
        done = FakeProcess(alive=False)
        services = SimpleNamespace(server_process=alive, client_process=done)

        stop_leftover_processes(services)

        self.assertTrue(alive.killed)
        self.assertFalse(done.killed)
        self.assertEqual(alive.joined, [1])

    def test_force_exit_ends_the_process_even_without_services(self):
        with patch.object(
            tray_manager, "_terminate_current_process"
        ) as terminate, patch.object(tray_manager.logging, "shutdown") as log_shutdown:
            force_exit(None, "测试")

        terminate.assert_called_once_with()
        log_shutdown.assert_called_once_with()

    def test_force_exit_kills_children_before_exiting(self):
        alive = FakeProcess(alive=True)
        services = SimpleNamespace(server_process=alive, client_process=None)

        with patch.object(tray_manager, "_terminate_current_process"):
            force_exit(services, "测试")

        self.assertTrue(alive.killed)

    def test_kill_failure_still_exits(self):
        broken = MagicMock()
        broken.is_alive.side_effect = ValueError("进程已关闭")
        services = SimpleNamespace(server_process=broken, client_process=None)

        with patch.object(tray_manager, "_terminate_current_process") as terminate:
            force_exit(services)

        terminate.assert_called_once_with()

    def test_win32_terminate_is_preferred_over_os_exit(self):
        with patch.object(
            tray_manager, "_terminate_via_win32", return_value=True
        ), patch.object(tray_manager.os, "_exit") as exit_mock:
            tray_manager._terminate_current_process()

        exit_mock.assert_not_called()

    def test_falls_back_to_os_exit_when_win32_terminate_fails(self):
        with patch.object(
            tray_manager, "_terminate_via_win32", return_value=False
        ), patch.object(tray_manager.os, "_exit") as exit_mock:
            tray_manager._terminate_current_process(0)

        exit_mock.assert_called_once_with(0)


class TestForceExitAfter(unittest.TestCase):
    def test_forces_exit_only_after_the_delay(self):
        with patch.object(tray_manager.time, "sleep") as sleep, patch.object(
            tray_manager, "force_exit"
        ) as force:
            force_exit_after(12.5, "S")

        sleep.assert_called_once_with(12.5)
        force.assert_called_once()
        self.assertEqual(force.call_args[0][0], "S")

    def test_reason_mentions_the_timeout(self):
        with patch.object(tray_manager.time, "sleep"), patch.object(
            tray_manager, "force_exit"
        ) as force:
            force_exit_after(15, None)

        self.assertIn("15 秒", force.call_args[0][1])


class FakeManager:
    """只覆盖 quit_app 需要的接口，避免为测试拉起 QApplication。"""

    def __init__(self, queue_accepts=True, window_hidden=True):
        self._quit_requested = False
        self.services = SimpleNamespace(
            server_process=None,
            client_process=None,
            stop_server=lambda preserve_intent=False: None,
            stop_client=lambda preserve_intent=False: None,
        )
        self.config = SimpleNamespace(
            server_running=False, client_running=False, save_state=lambda: None
        )
        self.root = object()
        self.window = SimpleNamespace(isHidden=lambda: window_hidden)
        self.queue_accepts = queue_accepts
        self.shutdown_calls = []
        self.refresh_calls = 0
        self.watchdogs = []
        self.quit_calls = 0
        self._actions = SimpleNamespace(shutdown=self._shutdown)

    def _shutdown(self, action):
        self.shutdown_calls.append(action)
        return self.queue_accepts

    def _quit_watchdog(self):
        self.watchdogs.append(1)

    def quit_app(self):
        self.quit_calls += 1
        self._quit_requested = True

    def refresh_ui(self):
        self.refresh_calls += 1


class TestQuitApp(unittest.TestCase):
    def test_quit_stops_services_once_and_starts_the_watchdog(self):
        manager = FakeManager()

        TrayManager.quit_app(manager)
        TrayManager.quit_app(manager)  # 重复点击（托盘 + 界面）不该再排一次

        self.assertEqual(len(manager.shutdown_calls), 1)
        self.assertEqual(manager.refresh_calls, 1)
        self.assertTrue(manager._quit_requested)

    def test_shutdown_action_really_stops_both_services(self):
        manager = FakeManager()
        services = manager.services

        stopped = []
        services.stop_server = lambda preserve_intent=False: stopped.append("server")
        services.stop_client = lambda preserve_intent=False: stopped.append("client")
        manager.config.server_running = True
        manager.config.client_running = True

        with patch.object(tray_manager, "request_ui_shutdown", return_value=True):
            TrayManager.quit_app(manager)
            manager.shutdown_calls[0]()

        self.assertEqual(stopped, ["server", "client"])
        # 「启用意图」必须保留，否则下次启动不会自动恢复服务
        self.assertTrue(manager.config.server_running)
        self.assertTrue(manager.config.client_running)

    def test_ui_quit_comes_after_both_services_stopped(self):
        manager = FakeManager()
        order = []
        manager.services.stop_server = lambda preserve_intent=False: order.append(
            "server"
        )
        manager.services.stop_client = lambda preserve_intent=False: order.append(
            "client"
        )

        with patch.object(
            tray_manager,
            "request_ui_shutdown",
            side_effect=lambda _root: order.append("ui-quit") or True,
        ):
            TrayManager.quit_app(manager)
            manager.shutdown_calls[0]()

        self.assertEqual(order, ["server", "client", "ui-quit"])

    def test_falls_back_to_ui_quit_when_queue_refuses(self):
        manager = FakeManager(queue_accepts=False)

        with patch.object(
            tray_manager, "request_ui_shutdown", return_value=True
        ) as request_shutdown:
            TrayManager.quit_app(manager)

        request_shutdown.assert_called_once_with(manager.root)
        self.assertTrue(manager._quit_requested)


class TestRestartCommand(unittest.TestCase):
    """「重启程序」的命令行必须带接管意图，否则新实例会被单实例逻辑丢掉。"""

    def test_frozen_build_restarts_the_exe(self):
        command = tray_manager.build_restart_command(
            frozen=True, executable="C:/app/SyncClipboard.exe"
        )

        self.assertEqual(
            command, ["C:/app/SyncClipboard.exe", tray_manager.RESTART_TAKEOVER_FLAG]
        )

    def test_source_run_uses_the_same_module_as_autostart(self):
        command = tray_manager.build_restart_command(
            frozen=False, executable="C:/py/python.exe"
        )

        self.assertEqual(
            command,
            ["C:/py/python.exe", "-m", "gui.run", tray_manager.RESTART_TAKEOVER_FLAG],
        )

    def test_minimized_is_passed_before_takeover(self):
        command = tray_manager.build_restart_command(
            minimized=True, frozen=True, executable="app.exe"
        )

        self.assertEqual(command, ["app.exe", "--minimized", "--takeover"])

    def test_spawn_uses_detached_safe_flags(self):
        with patch.object(tray_manager.subprocess, "Popen") as popen:
            popen.return_value = SimpleNamespace(pid=4242)

            process = tray_manager.spawn_restarted_instance(
                ["app.exe", "--takeover"], cwd="D:/proj"
            )

        self.assertEqual(process.pid, 4242)
        kwargs = popen.call_args.kwargs
        self.assertEqual(kwargs["cwd"], "D:/proj")
        # DETACHED_PROCESS 会让子进程丢标准句柄而卡住，这里只允许 NO_WINDOW
        if hasattr(tray_manager.subprocess, "DETACHED_PROCESS"):
            self.assertFalse(
                kwargs["creationflags"] & tray_manager.subprocess.DETACHED_PROCESS
            )
        self.assertIs(kwargs["stdout"], tray_manager.subprocess.DEVNULL)

    def test_spawn_failure_returns_none(self):
        with patch.object(
            tray_manager.subprocess, "Popen", side_effect=OSError("拒绝访问")
        ):
            self.assertIsNone(
                tray_manager.spawn_restarted_instance(["app.exe"], cwd="D:/proj")
            )


class TestRestartApp(unittest.TestCase):
    def test_spawns_new_instance_before_quitting(self):
        manager = FakeManager()
        order = []

        with patch.object(
            tray_manager,
            "spawn_restarted_instance",
            side_effect=lambda *args, **kwargs: order.append(("spawn", args))
            or SimpleNamespace(pid=1),
        ):
            manager.quit_app = lambda: order.append(("quit", None))
            TrayManager.restart_app(manager)

        self.assertEqual([step[0] for step in order], ["spawn", "quit"])

    def test_hidden_window_stays_minimized_after_restart(self):
        manager = FakeManager(window_hidden=True)

        with patch.object(
            tray_manager, "spawn_restarted_instance", return_value=SimpleNamespace(pid=1)
        ) as spawn:
            TrayManager.restart_app(manager)

        command = spawn.call_args.args[0]
        self.assertIn("--minimized", command)

    def test_visible_window_reopens_after_restart(self):
        manager = FakeManager(window_hidden=False)

        with patch.object(
            tray_manager, "spawn_restarted_instance", return_value=SimpleNamespace(pid=1)
        ) as spawn:
            TrayManager.restart_app(manager)

        command = spawn.call_args.args[0]
        self.assertNotIn("--minimized", command)
        self.assertIn("--takeover", command)

    def test_keeps_running_when_new_instance_cannot_start(self):
        manager = FakeManager()

        with patch.object(
            tray_manager, "spawn_restarted_instance", return_value=None
        ), patch.object(tray_manager.ui_bridge, "show_message") as message:
            TrayManager.restart_app(manager)

        # 新实例没起来就退出等于程序彻底没了：必须保持运行并提示
        self.assertEqual(manager.quit_calls, 0)
        message.assert_called_once()

    def test_duplicate_restart_is_ignored(self):
        manager = FakeManager()
        manager._quit_requested = True

        with patch.object(
            tray_manager, "spawn_restarted_instance"
        ) as spawn:
            TrayManager.restart_app(manager)

        spawn.assert_not_called()


if __name__ == "__main__":
    unittest.main()
