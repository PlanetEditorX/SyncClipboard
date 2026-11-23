"""通知 AUMID 的运行时注册：替代已删除的手动注册脚本。

注册只写 HKCU，不需要提权；测试用假的 winreg / windows_toasts 模块，
不会真的动本机注册表或弹通知。
"""

import sys
import tempfile
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


if __name__ == "__main__":
    unittest.main()
