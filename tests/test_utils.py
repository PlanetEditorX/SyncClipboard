import unittest
from unittest.mock import patch
from datetime import datetime, timedelta
from common.utils import isExpired

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
