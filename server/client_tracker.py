# server/client_tracker.py
import json
import os
from datetime import datetime
from item_builder import build_text_item

LATEST_FILE = "client_latest.json"

class ClientTracker:
    def __init__(self):
        self.data = self._load()

    def _load(self):
        if os.path.exists(LATEST_FILE):
            with open(LATEST_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        else:
            return {"latest_global": None, "clients": {}}

    def _save(self):
        with open(LATEST_FILE, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)

    def update(self, item: dict):
        """收到新条目时更新客户端最新记录"""
        source = item.get("source", "unknown")
        # 更新对应客户端的最新内容
        self.data["clients"][source] = item

        # 更新全局最新：如果没有全局记录，或当前条目的时间晚于全局，则替换
        global_item = self.data["latest_global"]
        if global_item is None:
            self.data["latest_global"] = item
        else:
            current_time = datetime.fromisoformat(item["timestamp"])
            global_time = datetime.fromisoformat(global_item["timestamp"])
            if current_time > global_time:
                self.data["latest_global"] = item

        self._save()

    def get_latest_by_client(self, client_name: str):
        return self.data["clients"].get(client_name)

    def get_global_latest(self):
        return self.data["latest_global"]