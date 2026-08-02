import unittest
from unittest.mock import patch
from datetime import datetime, timedelta
from common.utils import isExpired, copy_files_to_clipboard
import sys
from unittest.mock import MagicMock

class TestCopyFilesToClipboard(unittest.TestCase):
    @patch('common.utils.logger')
    def test_close_clipboard_exception(self, mock_logger):
        # Create a mock win32clipboard module
        mock_win32clipboard = MagicMock()
        mock_win32clipboard.CloseClipboard.side_effect = Exception("Test clipboard close error")

        # Patch sys.modules to inject our mock
        with patch.dict(sys.modules, {'win32clipboard': mock_win32clipboard}):
            copy_files_to_clipboard(["file1.txt", "file2.txt"])

            # CloseClipboard is called in finally.
            # We expect the error to be logged.
            mock_logger.error.assert_any_call("关闭剪贴板失败: Test clipboard close error")

class TestIsExpired(unittest.TestCase):
    def test_isExpired_not_expired(self):
        dt = datetime.now() - timedelta(minutes=5)
        self.assertFalse(isExpired(dt.isoformat()))

    def test_isExpired_expired(self):
        dt = datetime.now() - timedelta(minutes=15)
        self.assertTrue(isExpired(dt.isoformat()))

    def test_isExpired_future(self):
        dt = datetime.now() + timedelta(minutes=5)
        self.assertFalse(isExpired(dt.isoformat()))

    @patch('common.utils.datetime')
    def test_isExpired_exact_boundary(self, mock_datetime):
        fixed_now = datetime(2023, 1, 1, 12, 0, 0)
        mock_datetime.now.return_value = fixed_now
        mock_datetime.fromisoformat.side_effect = datetime.fromisoformat

        ts_9m59s = (fixed_now - timedelta(minutes=9, seconds=59)).isoformat()
        self.assertFalse(isExpired(ts_9m59s))

        ts_10m = (fixed_now - timedelta(minutes=10)).isoformat()
        self.assertTrue(isExpired(ts_10m))

        ts_10m1s = (fixed_now - timedelta(minutes=10, seconds=1)).isoformat()
        self.assertTrue(isExpired(ts_10m1s))

if __name__ == '__main__':
    unittest.main()
