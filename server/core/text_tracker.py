# server/text_tracker.py
import hashlib
import json
import os
import time
from datetime import datetime
from pathlib import Path
from common.utils import BASE_DIR, safe_get
import threading

TEXT_LATEST_FILE = BASE_DIR / "latest" / "text_latest.json"
TEXT_LATEST_FILE.parent.mkdir(parents=True, exist_ok=True)

# 回音票据的有效期（秒）与数量上限：过期即视为“用户手动复制”放行
ECHO_TTL_SECONDS = 600
ECHO_MAX_ENTRIES = 32

class TextTracker:
    """
    文本追踪器，用于管理跨客户端的文本数据。
    数据结构：
    - latest_global: 全局最新的文本条目（完整字典）
    - clients: 字典，键为客户端名称，值为该客户端的最新条目
    - global_ids: 列表，按添加顺序存储最近 N 个条目的 ID
    - echo_guard: 回音票据表，{内容指纹: {origin, pending, at}}，
      记录“刚刚被广播出去的内容”，用于识别其它客户端把同一份内容
      原样回传（回音）还是用户手动又复制了一次。
    """
    def __init__(self):
        self.lock = threading.Lock()
        self.data = self._load()

    def _load(self):
        default = {
            "latest_global": None,
            "clients": {},
            "global_ids": [],
            "echo_guard": {}
        }
        if os.path.exists(TEXT_LATEST_FILE):
            try:
                with open(TEXT_LATEST_FILE, "r", encoding="utf-8") as f:
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
        with open(TEXT_LATEST_FILE, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)

    def is_duplicate(self, item_id: str) -> bool:
        """检查 ID 是否已存在（去重）"""
        with self.lock:
            self.data = self._load()
            return item_id in self.data["global_ids"]

    @staticmethod
    def content_key(content) -> str:
        """内容指纹：与客户端无关，用于跨设备判断“是不是同一份文字”。"""
        if content is None:
            return ""
        if not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False, sort_keys=True)
        return hashlib.sha1(content.encode("utf-8")).hexdigest()

    def _alive_guard(self, guard, now):
        """清理过期的回音票据。"""
        return {
            key: ticket
            for key, ticket in (guard or {}).items()
            if isinstance(ticket, dict) and now - float(ticket.get("at", 0)) <= ECHO_TTL_SECONDS
        }

    def register_echo(self, content, origin, pending=1):
        """
        广播成功后登记回音票据。

        收到这份内容的其它客户端，很可能会在本地剪贴板变化时把它原样回传一次；
        每登记一张票据就允许被“吞掉” pending 次回音。票据被消耗完或过期后，
        再收到同样的内容就视为用户手动复制，正常广播。
        """
        with self.lock:
            self.data = self._load()
            now = time.time()
            guard = self._alive_guard(self.data.get("echo_guard"), now)
            guard[self.content_key(content)] = {
                "origin": origin,
                "pending": max(1, int(pending)),
                "at": now,
            }
            if len(guard) > ECHO_MAX_ENTRIES:
                for key in sorted(guard, key=lambda k: guard[k]["at"])[: len(guard) - ECHO_MAX_ENTRIES]:
                    guard.pop(key, None)
            self.data["echo_guard"] = guard
            self._save()

    def consume_echo(self, content, source) -> bool:
        """
        判断这次提交是不是“回音”。

        返回 True 表示：这份内容刚由别的客户端首发并广播过，本次提交只是
        接收方把剪贴板内容原样发回来的噪音，应当丢弃并且不消耗票据以外的状态。
        返回 False 表示：要么没有票据，要么票据已耗尽/过期（用户手动重新复制），
        要么提交者就是首发者本人。
        """
        key = self.content_key(content)
        with self.lock:
            self.data = self._load()
            raw_guard = self.data.get("echo_guard")
            guard = self._alive_guard(raw_guard, time.time())
            ticket = guard.get(key)
            self.data["echo_guard"] = guard

            if not ticket or ticket.get("origin") == source:
                # 没有票据（=用户手动复制），或提交者本人就是首发者
                if guard != raw_guard:
                    self._save()
                return False

            remaining = int(ticket.get("pending", 1)) - 1
            if remaining > 0:
                guard[key] = {**ticket, "pending": remaining}
            else:
                guard.pop(key, None)
            self._save()
            return True

    def update(self, item: dict, force_latest=False):
        # 写入时加锁，保证原子性
        with self.lock:
            # 多个进程可能共用同一个记录文件，写入前必须刷新。
            self.data = self._load()
            source = item.get("source", "unknown")
            item_id = item["id"]

            if item_id in self.data["global_ids"]:
                return   # 绝对重复，直接忽略

            self.data["global_ids"].append(item_id)
            item.setdefault("origin", source)
            self.data["clients"][source] = item

            # 限制 global_ids 最大长度（事件 ID 唯一，该列表只作为重复提交的兜底）
            MAX_IDS = 20
            if len(self.data["global_ids"]) > MAX_IDS:
                # 计算需要移除的数量
                remove_count = len(self.data["global_ids"]) - MAX_IDS
                # 移除最旧的 remove_count 个 ID
                self.data["global_ids"] = self.data["global_ids"][remove_count:]

            if force_latest:
                self.data["latest_global"] = item
            else:
                global_item = self.data["latest_global"]
                if global_item is None:
                    self.data["latest_global"] = item
                else:
                    if item["timestamp"] > global_item["timestamp"]:
                        self.data["latest_global"] = item

            self._save()

    def get_global_latest(self):
        """返回最新的文字内容对象"""
        with self.lock:
            # 重新加载最新数据，防止覆盖其他进程已添加的 id
            self.data = self._load()
            latest = self.data.get("latest_global")
            return latest.copy() if latest else None

    def get_latest_global_content(self):
        """返回最新的文字内容"""
        with self.lock:
            self.data = self._load()
            return safe_get(self.data, "latest_global", "content")

    def mark_pasted(self, client_name: str, item: dict):
        """标记客户端已粘贴某内容，更新对应客户端条目和全局最新状态"""
        with self.lock:
            self.data = self._load()
            # 更新该客户端的条目（如果还不存在就创建）
            self.data["clients"][client_name] = item
            # 如果全局最新的 id 正好是这个条目的 id，把 global 的 pasted 也设为 true
            latest = self.data.get("latest_global")
            if latest and latest.get("id") == item["id"]:
                latest["pasted"] = True
                self.data["latest_global"] = latest
            self._save()
