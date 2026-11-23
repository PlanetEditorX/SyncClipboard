import unittest
from unittest.mock import patch, MagicMock
from datetime import datetime, timedelta
import requests
from common.utils import isExpired, safe_get, parse_filename_from_cd, safe_post, fix_mojibake_filename

class TestSafeGet(unittest.TestCase):
    def test_safe_get_happy_path(self):
        data = {"a": {"b": {"c": "value"}}}
        self.assertEqual(safe_get(data, "a", "b", "c"), "value")

    def test_safe_get_missing_key(self):
        data = {"a": {"b": {"c": "value"}}}
        self.assertIsNone(safe_get(data, "a", "x", "c"))
        self.assertEqual(safe_get(data, "a", "x", "c", default="not_found"), "not_found")

    def test_safe_get_non_dict_intermediate(self):
        data = {"a": "not_a_dict"}
        self.assertIsNone(safe_get(data, "a", "b"))
        self.assertEqual(safe_get(data, "a", "b", default="fallback"), "fallback")

    def test_safe_get_empty_keys(self):
        data = {"a": 1}
        self.assertEqual(safe_get(data), {"a": 1})

    def test_safe_get_non_dict_data(self):
        data = "just_a_string"
        self.assertIsNone(safe_get(data, "a"))
        self.assertEqual(safe_get(data, "a", default="default_val"), "default_val")


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

class TestParseFilenameFromCd(unittest.TestCase):
    def test_none_input(self):
        self.assertIsNone(parse_filename_from_cd(None))

    def test_empty_string(self):
        self.assertIsNone(parse_filename_from_cd(""))

    def test_rfc_5987_format(self):
        # "%E6%B5%8B%E8%AF%95.txt" is URL encoded for "测试.txt"
        header = "attachment; filename*=UTF-8''%E6%B5%8B%E8%AF%95.txt"
        self.assertEqual(parse_filename_from_cd(header), "测试.txt")

        # Test with standard ascii filename in RFC 5987 format
        header = "attachment; filename*=UTF-8''test_file.txt"
        self.assertEqual(parse_filename_from_cd(header), "test_file.txt")

    def test_standard_format(self):
        header = 'attachment; filename="test_file.txt"'
        self.assertEqual(parse_filename_from_cd(header), "test_file.txt")

    def test_simple_format(self):
        header = 'attachment; filename=test_file.txt'
        self.assertEqual(parse_filename_from_cd(header), "test_file.txt")

        header = 'filename=test_file.txt; attachment'
        self.assertEqual(parse_filename_from_cd(header), "test_file.txt")

    def test_no_filename(self):
        header = "attachment; something_else=value"
        self.assertIsNone(parse_filename_from_cd(header))

        header = "inline"
        self.assertIsNone(parse_filename_from_cd(header))

    def test_case_insensitivity(self):
        # RFC 5987 format
        header = "attachment; FILENAME*=utf-8''%E6%B5%8B%E8%AF%95.txt"
        self.assertEqual(parse_filename_from_cd(header), "测试.txt")

        # Standard format
        header = 'attachment; FileName="test.txt"'
        self.assertEqual(parse_filename_from_cd(header), "test.txt")

        # Simple format
        header = 'attachment; filename=TEST.txt'
        self.assertEqual(parse_filename_from_cd(header), "TEST.txt")

class TestSafePost(unittest.TestCase):
    @patch('common.utils.requests.post')
    def test_safe_post_success(self, mock_post):
        mock_response = MagicMock()
        mock_post.return_value = mock_response

        result = safe_post("http://example.com", data={"key": "value"})

        self.assertEqual(result, mock_response)
        mock_post.assert_called_once_with("http://example.com", timeout=10, data={"key": "value"})

    @patch('common.utils._handle_request_error')
    @patch('common.utils.requests.post')
    def test_safe_post_connect_timeout(self, mock_post, mock_handle_error):
        mock_post.side_effect = requests.exceptions.ConnectTimeout()
        result = safe_post("http://example.com")
        self.assertIsNone(result)
        mock_handle_error.assert_called_once_with("连接失败", "服务器连接超时")

    @patch('common.utils._handle_request_error')
    @patch('common.utils.requests.post')
    def test_safe_post_connection_error(self, mock_post, mock_handle_error):
        mock_post.side_effect = requests.exceptions.ConnectionError()
        result = safe_post("http://example.com")
        self.assertIsNone(result)
        mock_handle_error.assert_called_once_with("连接失败", "服务器不可达")

    @patch('common.utils._handle_request_error')
    @patch('common.utils.requests.post')
    def test_safe_post_generic_exception(self, mock_post, mock_handle_error):
        mock_post.side_effect = ValueError("Some error")
        result = safe_post("http://example.com")
        self.assertIsNone(result)
        mock_handle_error.assert_called_once_with("错误", "Some error")


class TestFixMojibakeFilename(unittest.TestCase):
    """iOS 把 UTF-8 字节按 GBK 解读时，落盘文件名的还原兜底。"""

    def test_real_garbled_samples(self):
        # 用户在真机上实际看到的乱码名
        self.assertEqual(fix_mojibake_filename("SEXJ62_璇佷功鏂囦欢(瀵嗙爜1)"),
                         "SEXJ62_证书文件(密码1)")
        self.assertEqual(fix_mojibake_filename("椋炵墰涓嬭浇"), "飞牛下载")

    def test_normal_names_are_untouched(self):
        for name in ("SEXJ62_证书文件(密码1)", "飞牛下载", "report.pdf",
                     "中文 空格&符号#1.txt", "a.png", "uploaded_file"):
            self.assertEqual(fix_mojibake_filename(name), name, name)

    def test_extension_survives(self):
        self.assertEqual(fix_mojibake_filename("椋炵墰涓嬭浇.zip"), "飞牛下载.zip")


if __name__ == '__main__':
    unittest.main()
