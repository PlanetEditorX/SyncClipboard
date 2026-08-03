from pathlib import Path
import unittest
import tempfile
import json
import os
from unittest.mock import patch
from server.core.file_latest import FileLatestTracker

class TestFileLatestTracker(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.temp_file = Path(self.temp_dir.name) / "file_latest.json"

        # We need to mock the FILE_LATEST_FILE constant in server.core.file_latest
        self.patcher = patch('server.core.file_latest.FILE_LATEST_FILE', self.temp_file)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        self.temp_dir.cleanup()

    def test_load_legacy_dict_format(self):
        # Create a legacy format json file
        old_data = {
            "file_id": "test_id_123",
            "path": "/some/path/file.txt",
            "name": "file.txt",
            "size": 1024,
            "source": "remote",
            "ip": "192.168.1.100"
            # port and updated_at are missing
        }
        with open(self.temp_file, "w", encoding="utf-8") as f:
            json.dump(old_data, f)

        tracker = FileLatestTracker()

        self.assertEqual(len(tracker.data), 1)
        item = tracker.data[0]

        self.assertEqual(item["file_id"], "test_id_123")
        self.assertEqual(item["path"], "/some/path/file.txt")
        self.assertEqual(item["name"], "file.txt")
        self.assertEqual(item["size"], 1024)
        self.assertEqual(item["source"], "remote")
        self.assertEqual(item["ip"], "192.168.1.100")
        self.assertIsNone(item["port"])
        self.assertIsNotNone(item["updated_at"])

    def test_load_legacy_dict_format_missing_file_id(self):
        old_data = {
            "path": "/some/path/file.txt",
            "name": "file.txt"
        }
        with open(self.temp_file, "w", encoding="utf-8") as f:
            json.dump(old_data, f)

        tracker = FileLatestTracker()
        self.assertEqual(tracker.data, [])

if __name__ == '__main__':
    unittest.main()
