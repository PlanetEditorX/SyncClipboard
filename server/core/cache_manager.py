import json
from pathlib import Path
from collections import deque
from common.tools import BASE_DIR

CACHE_FILE = BASE_DIR / "log" / "clipboard_cache.json"
CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)

MAX_ITEMS = 5  # 最大剪贴板缓存数量

class CacheManager:
    def __init__(self):
        self.cache = {"text": deque(maxlen=MAX_ITEMS), "file": deque(maxlen=MAX_ITEMS), "ids": set()}
        if CACHE_FILE.exists():
            self.load_cache()

    def load_cache(self):
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            self.cache["text"] = deque(data.get("text", []), maxlen=MAX_ITEMS)
            self.cache["file"] = deque(data.get("file", []), maxlen=MAX_ITEMS)
            self.cache["ids"] = set(data.get("ids", []))

    def save_cache(self):
        data = {
            "text": list(self.cache["text"]),
            "file": list(self.cache["file"]),
            "ids": list(self.cache["ids"])
        }
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def add_id(self, unique_id):
        self.cache["ids"].add(unique_id)
        self.save_cache()

    def id_exists(self, unique_id):
        return unique_id in self.cache["ids"]

    def update_text(self, text_item):
        """
        更新缓存json内容
        text_item：传入的数据
        check：是否检测，从剪切板进来的检测去重
        """
        if text_item["content"] == self.cache["text"][-1]["content"]:
            return

        if text_item["id"] in self.cache["ids"]:
            # 已存在：更新内容和时间戳
            for idx, item in enumerate(self.cache["text"]):
                if item["id"] == text_item["id"]:
                    self.cache["text"][idx]["content"] = text_item["content"]
                    self.cache["text"][idx]["timestamp"] = text_item["timestamp"]
                    break
        else:
            # 新条目：如果容量已满，先移除最旧的 id
            if len(self.cache["text"]) >= self.cache["text"].maxlen:
                oldest_id = self.cache["text"][0]["id"]
                self.cache["ids"].discard(oldest_id)
            # 现在添加新元素（此时长度 < maxlen，不会自动丢弃）
            self.cache["text"].append(text_item)
            self.cache["ids"].add(text_item["id"])

        self.save_cache()

    def get_latest_text(self):
        return self.cache["text"][-1] if self.cache["text"] else {}

    def update_cache(self, key, value):
        """
        更新最新的参数
        """
        self.cache["text"][-1][key] = value

    def search_text(self, key):
        """
        搜索文本缓存中是否存在 content 等于 key 的条目
        :param key: 要匹配的内容字符串
        :return: 存在返回 item，否则返回 False
        """
        for item in self.cache["text"]:
            if item.get("content") == key:
                return item
        return None