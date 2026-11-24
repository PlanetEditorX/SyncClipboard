import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from common import single_instance as si


class TestPidFile(unittest.TestCase):
    def setUp(self):
        self._temp_dir = tempfile.TemporaryDirectory()
        self._original = si.PID_FILE
        si.PID_FILE = Path(self._temp_dir.name) / "syncclipboard.pid"
        self.addCleanup(self._restore)

    def _restore(self):
        si.PID_FILE = self._original
        self._temp_dir.cleanup()

    def test_missing_file_returns_none(self):
        self.assertIsNone(si.read_pid())

    def test_write_then_read_round_trip(self):
        self.assertTrue(si.write_pid(4321))
        self.assertEqual(si.read_pid(), 4321)

    def test_corrupted_file_returns_none(self):
        si.PID_FILE.write_text("not-a-pid", encoding="utf-8")
        self.assertIsNone(si.read_pid())

    def test_empty_file_returns_none(self):
        si.PID_FILE.write_text("", encoding="utf-8")
        self.assertIsNone(si.read_pid())


class TestEventNames(unittest.TestCase):
    def test_show_and_shown_events_differ(self):
        self.assertNotEqual(si.show_event_name(100), si.shown_event_name(100))

    def test_event_names_are_per_pid(self):
        self.assertNotEqual(si.show_event_name(100), si.show_event_name(200))
        self.assertIn("100", si.show_event_name(100))
        self.assertIn("200", si.shown_event_name(200))


class TestNonWindowsNoop(unittest.TestCase):
    def test_ensure_single_instance_is_primary_off_windows(self):
        with patch.object(si, "_IS_WINDOWS", False):
            self.assertEqual(si.ensure_single_instance(), si.PRIMARY)

    def test_start_show_watcher_returns_none_without_event(self):
        with patch.object(si, "_IS_WINDOWS", False):
            self.assertIsNone(si.start_show_watcher(lambda: None))

    def test_notify_unreachable_is_noop_off_windows(self):
        with patch.object(si, "_IS_WINDOWS", False):
            si.notify_unreachable()


class TestShowRequest(unittest.TestCase):
    """主实例已注册事件时，唤出请求应送达并等待回执。"""

    def setUp(self):
        if not si._IS_WINDOWS:
            self.skipTest("仅 Windows 有效")
        self._temp_dir = tempfile.TemporaryDirectory()
        self._original_pid_file = si.PID_FILE
        si.PID_FILE = Path(self._temp_dir.name) / "syncclipboard.pid"
        self.addCleanup(self._cleanup)

        self._namespace = f"SCUnit{__import__('os').getpid()}"
        self._original_prefixes = (si.SHOW_EVENT_PREFIX, si.SHOWN_EVENT_PREFIX)
        si.SHOW_EVENT_PREFIX = f"Local\\{self._namespace}.Show."
        si.SHOWN_EVENT_PREFIX = f"Local\\{self._namespace}.Shown."
        # 让主实例注册的事件指向测试命名空间
        si._handles["show"] = si._kernel32.CreateEventW(
            None, False, False, si.show_event_name(4242)
        )
        si._handles["shown"] = si._kernel32.CreateEventW(
            None, False, False, si.shown_event_name(4242)
        )

    def _cleanup(self):
        for key in ("show", "shown"):
            handle = si._handles.get(key)
            if handle:
                si._kernel32.CloseHandle(handle)
                si._handles[key] = None
        si.SHOW_EVENT_PREFIX, si.SHOWN_EVENT_PREFIX = self._original_prefixes
        si.PID_FILE = self._original_pid_file
        self._temp_dir.cleanup()

    def test_request_show_waits_for_ack(self):
        import threading

        def ack():
            si._kernel32.WaitForSingleObject(si._handles["show"], 5000)
            si._kernel32.SetEvent(si._handles["shown"])

        worker = threading.Thread(target=ack)
        worker.start()
        self.assertTrue(si._request_show(4242, timeout=5))
        worker.join()

    def test_request_show_times_out_without_ack(self):
        self.assertFalse(si._request_show(4242, timeout=0.5))

    def test_request_show_fails_without_event(self):
        self.assertFalse(si._request_show(999999, timeout=0.5))


class TestTakeover(unittest.TestCase):
    """「重启程序」时新实例要能接管即将退出的旧实例。

    旧实例还没退干净时互斥量仍被占着，常规逻辑会把新实例当成「重复启动」
    直接丢掉 —— 用户看到的就是点了重启、程序却再也没回来。
    """

    def setUp(self):
        if not si._IS_WINDOWS:
            self.skipTest("仅 Windows 有效")
        self._temp_dir = tempfile.TemporaryDirectory()
        self._original_pid_file = si.PID_FILE
        self._original_name = si.MUTEX_NAME
        si.PID_FILE = Path(self._temp_dir.name) / "syncclipboard.pid"
        si.MUTEX_NAME = f"Local\\SCUnitTakeover.{__import__('os').getpid()}.{id(self)}"
        self._held = []
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        for handle in self._held:
            if handle:
                si._kernel32.CloseHandle(handle)
        for key in ("mutex", "show", "shown"):
            handle = si._handles.get(key)
            if handle:
                si._kernel32.CloseHandle(handle)
            si._handles[key] = None
        si.MUTEX_NAME = self._original_name
        si.PID_FILE = self._original_pid_file
        self._temp_dir.cleanup()

    def _hold_mutex(self):
        """模拟旧实例：先占住同名互斥量。"""
        handle, already_running = si._create_mutex()
        self.assertFalse(already_running)
        self._held.append(handle)
        return handle

    def test_takeover_waits_until_old_instance_releases(self):
        import threading
        import time

        old_handle = self._hold_mutex()

        def release():
            time.sleep(0.3)
            si._kernel32.CloseHandle(old_handle)
            self._held.remove(old_handle)

        threading.Thread(target=release, daemon=True).start()

        started = time.monotonic()
        outcome = si.ensure_single_instance(takeover=True)
        elapsed = time.monotonic() - started

        self.assertEqual(outcome, si.PRIMARY)
        self.assertGreaterEqual(elapsed, 0.2)
        # 接管成功后 pid 文件要换成新实例
        self.assertEqual(si.read_pid(), __import__("os").getpid())

    def test_takeover_gives_up_after_timeout(self):
        self._hold_mutex()
        si.write_pid(999999)  # 指向一个没有唤出事件的 pid，好落到 UNREACHABLE

        with patch.object(si, "TAKEOVER_TIMEOUT", 0.2):
            outcome = si.ensure_single_instance(takeover=True, timeout=0.2)

        self.assertEqual(outcome, si.UNREACHABLE)

    def test_no_takeover_still_delegates(self):
        self._hold_mutex()
        si.write_pid(999999)

        with patch.object(si, "_request_show", return_value=False):
            outcome = si.ensure_single_instance(takeover=False, timeout=0.1)

        self.assertEqual(outcome, si.UNREACHABLE)

    def test_acquire_returns_immediately_when_mutex_is_free(self):
        handle, already_running = si._acquire_mutex_with_takeover(5)

        self.assertFalse(already_running)
        self.assertTrue(handle)
        self._held.append(handle)


if __name__ == "__main__":
    unittest.main()
