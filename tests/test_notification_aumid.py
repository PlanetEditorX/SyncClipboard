"""通知 AUMID 的运行时注册：替代已删除的手动注册脚本。

注册只写 HKCU，不需要提权；测试用假的 winreg / windows_toasts 模块，
不会真的动本机注册表或弹通知。
"""

import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from common import notification


class _WinregCase(unittest.TestCase):
    def setUp(self):
        self.fake_key = object()
        self.winreg = SimpleNamespace(
            HKEY_CURRENT_USER="HKCU",
            KEY_SET_VALUE=0x0006,
            REG_SZ=1,
            CreateKeyEx=MagicMock(),
            SetValueEx=MagicMock(),
        )
        self.winreg.CreateKeyEx.return_value.__enter__.return_value = self.fake_key

        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        icon = self.root / "gui" / "icon"
        icon.mkdir(parents=True)
        (icon / "icon-active.png").write_bytes(b"png")

        patcher = patch.object(notification, "BASE_DIR", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _call(self, system="Windows"):
        with patch.object(notification.platform, "system", return_value=system), patch.dict(
            sys.modules, {"winreg": self.winreg}
        ):
            return notification._ensure_aumid()


class TestEnsureAumid(_WinregCase):
    def test_writes_display_name_and_icon_into_hkcu(self):
        self._call()

        self.winreg.CreateKeyEx.assert_called_once_with(
            "HKCU",
            rf"SOFTWARE\Classes\AppUserModelId\{notification.AUMID}",
            0,
            self.winreg.KEY_SET_VALUE,
        )
        writes = [call.args[1] for call in self.winreg.SetValueEx.call_args_list]
        self.assertIn("DisplayName", writes)
        self.assertIn("IconUri", writes)

    def test_icon_is_optional(self):
        (self.root / "gui" / "icon" / "icon-active.png").unlink()

        self._call()

        writes = [call.args[1] for call in self.winreg.SetValueEx.call_args_list]
        self.assertEqual(writes, ["DisplayName"])

    def test_non_windows_does_not_touch_registry(self):
        self._call(system="Linux")

        self.winreg.CreateKeyEx.assert_not_called()

    def test_registry_failure_is_only_logged(self):
        self.winreg.CreateKeyEx.side_effect = OSError("access denied")

        self._call()  # 不能把异常抛给发通知的调用方


class TestToasterRegistrationOrder(unittest.TestCase):
    def setUp(self):
        self.saved = (notification._toaster, notification._toaster_ready)
        notification._toaster = None
        notification._toaster_ready = False
        self.addCleanup(self._restore)

    def _restore(self):
        notification._toaster, notification._toaster_ready = self.saved

    def test_registers_aumid_before_creating_notifier(self):
        toaster = MagicMock()
        module = SimpleNamespace(
            InteractableWindowsToaster=MagicMock(return_value=toaster)
        )
        order = []

        with patch.object(notification.platform, "system", return_value="Windows"), \
             patch.object(notification, "_ensure_aumid", side_effect=lambda: order.append("register")), \
             patch.dict(sys.modules, {"windows_toasts": module}):
            module.InteractableWindowsToaster.side_effect = lambda *a, **kw: (
                order.append("create") or toaster
            )
            result = notification._get_toaster()

        self.assertIs(result, toaster)
        self.assertEqual(order, ["register", "create"])
        module.InteractableWindowsToaster.assert_called_once_with(
            "SyncClipboard", notifierAUMID=notification.AUMID
        )

    def test_registration_runs_only_once(self):
        module = SimpleNamespace(InteractableWindowsToaster=MagicMock())

        with patch.object(notification.platform, "system", return_value="Windows"), \
             patch.object(notification, "_ensure_aumid") as ensure, \
             patch.dict(sys.modules, {"windows_toasts": module}):
            notification._get_toaster()
            notification._get_toaster()

        ensure.assert_called_once_with()


class TestConfirmNotification(unittest.TestCase):
    """目录超限确认通知：点按钮 / 超时 / 发不出来三种结局。"""

    def setUp(self):
        self.saved = (notification._toaster, notification._toaster_ready)
        # 直接给定成品，跳过真实 WinRT 初始化
        notification._toaster = None
        notification._toaster_ready = True
        self.addCleanup(self._restore)

    def _restore(self):
        notification._toaster, notification._toaster_ready = self.saved

    @staticmethod
    def _fake_toasts():
        created = {}

        class _Toast:
            def __init__(self, **kwargs):
                self.kwargs = kwargs
                self.on_activated = kwargs.get("on_activated")
                created["toast"] = self

        module = SimpleNamespace(
            Toast=_Toast,
            ToastButton=lambda content="", arguments="": (content, arguments),
            ToastDuration=SimpleNamespace(Long="long"),
        )
        toaster = MagicMock()
        return module, toaster, created

    def test_unavailable_notifier_returns_none(self):
        # 返回 None 而不是 False：调用方据此决定退回原生框还是直接不共享
        self.assertIsNone(
            notification.show_confirm_notification("标题", "内容", timeout=1)
        )

    def test_timeout_counts_as_not_shared(self):
        module, toaster, _created = self._fake_toasts()
        notification._toaster = toaster
        with patch.dict(sys.modules, {"windows_toasts": module}):
            started = time.time()
            allowed = notification.show_confirm_notification("标题", "内容", timeout=1)
            elapsed = time.time() - started

        self.assertFalse(allowed)
        self.assertLess(elapsed, 6)
        # 到点要主动清掉通知，别留在通知中心又被点到
        self.assertTrue(toaster.clear_toasts.called)

    def test_clicking_allow_returns_true(self):
        module, toaster, created = self._fake_toasts()

        def _click_allow(toast):
            toast.on_activated(SimpleNamespace(arguments="syncclipboard-allow"))

        toaster.show_toast.side_effect = _click_allow
        notification._toaster = toaster
        with patch.dict(sys.modules, {"windows_toasts": module}):
            allowed = notification.show_confirm_notification("标题", "内容", timeout=5)

        self.assertTrue(allowed)
        self.assertEqual(created["toast"].kwargs["duration"], "long")
        self.assertEqual(len(created["toast"].kwargs["actions"]), 2)

    def test_clicking_deny_returns_false(self):
        module, toaster, _created = self._fake_toasts()
        toaster.show_toast.side_effect = lambda toast: toast.on_activated(
            SimpleNamespace(arguments="syncclipboard-deny")
        )
        notification._toaster = toaster
        with patch.dict(sys.modules, {"windows_toasts": module}):
            allowed = notification.show_confirm_notification("标题", "内容", timeout=5)

        self.assertFalse(allowed)


if __name__ == "__main__":
    unittest.main()
