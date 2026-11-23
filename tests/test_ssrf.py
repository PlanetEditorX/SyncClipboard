import unittest
from unittest.mock import patch
from server.api import flask_app
from server.api.flask_app import app, clients

# 统一契约：所有请求都在请求头带 key。测试直接 patch 服务端密钥，
# 真实走 require_key()，不再 mock 掉鉴权函数本身。
TEST_KEY = "test-key"


class TestSSRFPrevention(unittest.TestCase):
    def setUp(self):
        self.app = app.test_client()
        self.app.testing = True
        key_patch = patch.object(flask_app, "KEY", TEST_KEY)
        key_patch.start()
        self.addCleanup(key_patch.stop)

    def tearDown(self):
        clients.clear()

    def _put(self, url, data=b"test data"):
        return self.app.put(url, data=data, headers={"key": TEST_KEY})

    def test_ssrf_untrusted_url(self):
        # 不可信的地址必须被 SSRF 检查挡住（而不是先被鉴权挡住）
        response = self._put(
            '/upload_file_to_download?redirect=http://evil.com/upload?filename=test.txt')
        self.assertEqual(response.status_code, 403)
        self.assertIn('不信任的重定向地址', response.get_json()['message'])

    @patch('server.api.flask_app.secure_save_path')
    def test_ssrf_trusted_localhost(self, mock_secure_save_path):
        mock_secure_save_path.return_value = None  # 不落盘，预期 400

        # localhost 是可信目标，不该被 SSRF 拦成 403
        response = self._put(
            '/upload_file_to_download?redirect=http://127.0.0.1:8080/upload?filename=test.txt')
        self.assertNotEqual(response.status_code, 403)

    @patch('server.api.flask_app.secure_save_path')
    def test_ssrf_trusted_client(self, mock_secure_save_path):
        mock_secure_save_path.return_value = None
        clients.append({"ip": "192.168.1.100"})

        # 已登记客户端 IP 是可信目标
        response = self._put(
            '/upload_file_to_download?redirect=http://192.168.1.100:8080/upload?filename=test.txt')
        self.assertNotEqual(response.status_code, 403)

    def test_ssrf_invalid_url(self):
        # 没有 hostname 的 redirect 应该 400，而不是 403
        response = self._put('/upload_file_to_download?redirect=invalid-url')
        self.assertEqual(response.status_code, 400)
        self.assertIn('无效的 redirect_url', response.get_json()['message'])


if __name__ == '__main__':
    unittest.main()
