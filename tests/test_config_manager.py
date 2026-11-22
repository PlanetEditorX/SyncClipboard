import unittest
import sys
import json
from unittest.mock import patch, mock_open, MagicMock, ANY

class TestConfigManager(unittest.TestCase):
    def setUp(self):
        # Safely mock winreg module for non-Windows platforms
        self.winreg_mock = MagicMock()
        self.sys_modules_patcher = patch.dict('sys.modules', {'winreg': self.winreg_mock})
        self.sys_modules_patcher.start()

        # Import ConfigManager after mocking winreg
        from gui.config_manager import ConfigManager
        # 默认共享存活时间的权威定义在 gui.main_window，这里跟着它走，
        # 免得默认值调整后测试又要改一遍
        from gui.main_window import DEFAULT_FILE_SHARE_TTL_MINUTES
        self.default_ttl = DEFAULT_FILE_SHARE_TTL_MINUTES
        self.ConfigManager = ConfigManager

    def tearDown(self):
        self.sys_modules_patcher.stop()

    @patch('os.path.exists', return_value=False)
    @patch('pathlib.Path.exists', return_value=False)
    @patch('gui.config_manager.json.dump')
    def test_load_server_config_creates_default(self, mock_json_dump, mock_path_exists, mock_os_exists):
        """Test that load_server_config creates a default config when the file is missing."""
        manager = self.ConfigManager()

        m_open = mock_open(read_data='{"port": 8000, "key": "123456", "local_name": "Server"}')
        with patch('builtins.open', m_open) as mocked_open:
            result = manager.load_server_config()

        self.assertTrue(result)

        # Verify the file was opened for writing the default config
        mocked_open.assert_any_call(self.ConfigManager.SERVER_CONFIG, 'w', encoding='utf-8')

        # Verify json.dump was called with a dictionary containing expected default keys
        mock_json_dump.assert_any_call(
            {
                "key": "123456",
                "port": 8000,
                "local_name": manager.local_name,
                "file_share_ttl_minutes": self.default_ttl
            },
            ANY,
            ensure_ascii=False,
            indent=4
        )

    @patch('os.path.exists', return_value=False)
    @patch('pathlib.Path.exists', return_value=False)
    @patch('gui.config_manager.json.dump')
    def test_load_client_config_creates_default(self, mock_json_dump, mock_path_exists, mock_os_exists):
        """Test that load_client_config creates a default config when the file is missing."""
        manager = self.ConfigManager()

        m_open = mock_open(read_data='{"server_host": "127.0.0.1", "server_port": 8000, "key": "123456", "local_name": "PC-01", "file_server_port": 8899}')
        with patch('builtins.open', m_open) as mocked_open:
            result = manager.load_client_config()

        self.assertTrue(result)

        # Verify the file was opened for writing the default config
        mocked_open.assert_any_call(self.ConfigManager.CLIENT_CONFIG, 'w', encoding='utf-8')

        # Verify json.dump was called with the default client configuration
        mock_json_dump.assert_any_call(
            {
                "server_host": ANY,
                "server_port": 8000,
                "key": "123456",
                "local_name": manager.local_name,
                "file_server_port": 8899,
                "save_path": ANY,
                "text_sync_mode": "both",
                "file_sync_mode": "both",
                "folder_sync_enabled": True,
                "folder_zip_confirm_gb": 10,
                "file_share_ttl_minutes": self.default_ttl
            },
            ANY,
            ensure_ascii=False,
            indent=4
        )

if __name__ == '__main__':
    unittest.main()
