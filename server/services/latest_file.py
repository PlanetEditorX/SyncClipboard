import os
import json
from pathlib import Path

STATE_FILE = Path("latest/latest_file.json")

class LatestFileTracker:
    def __init__(self):
        self.data = self._load()

    def _load(self):
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, "r") as f:
                return json.load(f)
        return {"path": None, "name": None, "size": 0}

    def _save(self):
        with open(STATE_FILE, "w") as f:
            json.dump(self.data, f, indent=2)

    def set_latest(self, path, name, size):
        self.data["path"] = path
        self.data["name"] = name
        self.data["size"] = size
        self._save()

    def get_latest(self):
        return self.data

    def clear(self):
        self.data = {"path": None, "name": None, "size": 0}
        self._save()