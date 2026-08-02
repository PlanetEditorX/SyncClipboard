import unittest
import json
from unittest.mock import patch, mock_open
from client.settings import load_client_config

class TestSettingsLoadClientConfig(unittest.TestCase):
    @patch('builtins.open')
    def test_load_client_config_file_not_found(self, mock_file_open):
        """Test that load_client_config raises FileNotFoundError when config file is missing."""
        mock_file_open.side_effect = FileNotFoundError("No such file or directory")
        with self.assertRaises(FileNotFoundError):
            load_client_config()

    @patch('builtins.open', new_callable=mock_open, read_data='{')
    def test_load_client_config_invalid_json(self, mock_file_open):
        """Test that load_client_config raises JSONDecodeError when config file contains invalid JSON."""
        with self.assertRaises(json.JSONDecodeError):
            load_client_config()

    @patch('builtins.open', new_callable=mock_open, read_data='{"key": "value"}')
    def test_load_client_config_success(self, mock_file_open):
        """Test that load_client_config returns parsed JSON on success."""
        result = load_client_config()
        self.assertEqual(result, {"key": "value"})

if __name__ == '__main__':
    unittest.main()
