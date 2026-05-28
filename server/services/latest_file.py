import os
import json
from pathlib import Path
from common.path import BASE_DIR

STATE_FILE = BASE_DIR / "latest" / "file_latest.json"
STATE_FILE.parent.mkdir(parents=True, exist_ok=True)

class LatestFileTracker:
    def __init__(self):
        self.data = self._load()

    def _load(self):
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        return {"path": None, "name": None, "size": 0}

    def _save(self):
        with open(STATE_FILE, "w") as f:
            json.dump(self.data, f, indent=2)

    def set_latest(self, uuid, path, name, size, source, ip, port):
        self.data["file_id"] = uuid
        self.data["path"] = path
        self.data["name"] = name
        self.data["size"] = size
        self.data["source"] = source
        self.data["ip"] = ip
        self.data["port"] = port
        self._save()

    def get_latest(self):
        '''返回文件信息'''
        if self.data["file_id"]:
            return self.data
        return None

    def clear(self):
        self.data = {"file_id": None, "path": None, "name": None, "size": 0, "source": None, "ip": None}
        self._save()