# server/client_tracker.py
import json
import os
from datetime import datetime

LATEST_FILE = "client_latest.json"

class ClientTracker:
    def __init__(self):
        self.data = self._load()

    def _load(self):
        default = {
            "latest_global": None,
            "clients": {},
            "global_ids": []
        }
        if os.path.exists(LATEST_FILE):
            try:
                with open(LATEST_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                # 确保所有必需的键都存在
                for key in default:
                    if key not in data:
                        data[key] = default[key]
                return data
            except (json.JSONDecodeError, FileNotFoundError):
                return default
        return default

    def _save(self):
        with open(LATEST_FILE, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)

    def is_duplicate(self, item_id: str) -> bool:
        """检查 ID 是否已存在（去重）"""
        return item_id in self.data["global_ids"]

    def update(self, item: dict):
        """更新客户端最新记录，并注册 ID"""
        item_id = item.get("id")
        if not item_id:
            return

        # 去重判断
        if self.is_duplicate(item_id):
            return

        # 注册 ID
        self.data["global_ids"].append(item_id)

        # 更新客户端最新
        source = item.get("source", "unknown")
        self.data["clients"][source] = item

        # 更新全局最新
        global_item = self.data["latest_global"]
        if global_item is None:
            self.data["latest_global"] = item
        else:
            current_time = datetime.fromisoformat(item["timestamp"])
            global_time = datetime.fromisoformat(global_item["timestamp"])
            if current_time > global_time:
                self.data["latest_global"] = item

        self._save()

    def get_global_latest(self):
      return self.data.get("latest_global")