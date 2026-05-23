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

    def update(self, item: dict, skip_duplicate_content=True):
        """
        更新记录，如果 skip_duplicate_content=True，
        且该客户端已有记录且内容完全相同，则忽略（不更新时间戳）。
        """
        source = item.get("source", "unknown")
        item_id = item["id"]

        # 全局 ID 去重
        if item_id in self.data["global_ids"]:
            return

        # 内容去重（与自身客户端上次记录比较）
        if skip_duplicate_content:
            last = self.data["clients"].get(source)
            if last and last.get("content") == item.get("content"):
                return  # 内容未变，跳过

        # 保存 ID
        self.data["global_ids"].append(item_id)
        # 更新客户端最新
        self.data["clients"][source] = item

        # 更新全局最新（比较时间戳）
        global_item = self.data["latest_global"]
        if global_item is None:
            self.data["latest_global"] = item
        else:
            # 使用 ISO 时间字符串比较
            if item["timestamp"] > global_item["timestamp"]:
                self.data["latest_global"] = item

        self._save()

    def get_global_latest(self):
      return self.data.get("latest_global")

    def mark_pasted(self, client_name: str, item: dict):
        """标记客户端已粘贴某内容，更新对应客户端条目和全局最新状态"""
        # 更新该客户端的条目（如果还不存在就创建）
        self.data["clients"][client_name] = item
        # 如果全局最新的 id 正好是这个条目的 id，把 global 的 pasted 也设为 true
        latest = self.data.get("latest_global")
        if latest and latest.get("id") == item["id"]:
            latest["pasted"] = True
            self.data["latest_global"] = latest
        self._save()