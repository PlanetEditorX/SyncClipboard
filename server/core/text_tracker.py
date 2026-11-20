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

# 投递记录的保鲜期（秒）：投递给某台设备的文字，在这个时间内被原样回传
# 就算回音；超过这个时间再收到，就当作用户手动重新复制。
ECHO_TTL_SECONDS = 300
# 投递记录最多保留多少台设备，以及每台设备保留几条最近投递
ECHO_MAX_DEVICES = 32
ECHO_KEEP_PER_DEVICE = 4

class TextTracker:
    """
    文本追踪器，用于管理跨客户端的文本数据。
    数据结构：
    - latest_global: 全局最新的文本条目（完整字典）
    - clients: 字典，键为客户端名称，值为该客户端的最新条目
    - global_ids: 列表，按添加顺序存储最近 N 个条目的 ID
    - delivered: 投递记录，{设备名: [{key: 内容指纹, at: 时间戳}, ...]}，
      记录“这份内容最近被投递给哪些设备”。设备把收到的东西原样回传
      （回音）时命中记录；用户手动复制的新内容不会命中。
      注意这里按“最近投递”判断，不做一次性消耗 —— 客户端（尤其是 iOS
      快捷指令）会每隔一两秒把剪贴板整块 POST 上来，一次性票据很容易被
      反复回传消耗干净，之后同一段文字的每次轮询都会被当成“新内容”，
      表现为来源被改成手机、电脑反复弹窗。
    """
    def __init__(self):
        self.lock = threading.Lock()
        self.data = self._load()

    def _load(self):
        default = {
            "latest_global": None,
            "clients": {},
            "global_ids": [],
            "delivered": {}
        }
        if os.path.exists(TEXT_LATEST_FILE):
            try:
                with open(TEXT_LATEST_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                # 确保所有必需的键都存在
                for key in default:
                    if key not in data:
                        data[key] = default[key]
                # 旧版本的一次性回音票据，已经被 delivered 取代，顺手清掉
                data.pop("echo_guard", None)
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

    def _alive_delivered(self, delivered, now):
        """清理过期的投递记录。"""
        result = {}
        for device, records in (delivered or {}).items():
            alive = [
                record for record in records
                if isinstance(record, dict)
                and now - float(record.get("at", 0)) <= ECHO_TTL_SECONDS
            ]
            if alive:
                result[device] = alive
        return result

    def _record_delivery_locked(self, device, content):
        """（调用方已持锁）记录“这份内容刚投递给了这台设备”。"""
        if not device or content is None:
            return
        now = time.time()
        key = self.content_key(content)
        delivered = self._alive_delivered(self.data.get("delivered"), now)
        records = [r for r in delivered.get(device, []) if r.get("key") != key]
        records.insert(0, {"key": key, "at": now})
        delivered[device] = records[:ECHO_KEEP_PER_DEVICE]
        if len(delivered) > ECHO_MAX_DEVICES:
            oldest = sorted(
                delivered,
                key=lambda name: delivered[name][0]["at"],
            )[: len(delivered) - ECHO_MAX_DEVICES]
            for name in oldest:
                delivered.pop(name, None)
        self.data["delivered"] = delivered

    def record_delivery(self, device, content):
        """记录一次向某台设备的文字投递（用于识别该设备的回音）。"""
        with self.lock:
            self.data = self._load()
            self._record_delivery_locked(device, content)
            self._save()

    def is_echo_of_delivered(self, content, source) -> bool:
        """
        这份内容最近是否刚好投递给该设备？

        是 → 本次提交属于“收到后原样回传”的回音，应当忽略；
        否 → 用户手动复制（或该设备根本没收到过这份内容），正常处理。
        """
        if not source or content is None:
            return False
        key = self.content_key(content)
        with self.lock:
            self.data = self._load()
            raw = self.data.get("delivered")
            delivered = self._alive_delivered(raw, time.time())
            if delivered != raw:
                self.data["delivered"] = delivered
                self._save()
            for record in delivered.get(source, []):
                if record.get("key") == key:
                    return True
        return False

    def note_client_hold(self, source, content):
        """
        记账：某台设备当前持有的内容就是这一份（回音场景）。

        把它写进 clients[source] 之后，下一轮轮询里 is_new 直接为 False，
        不会再次走到“有新内容”的分支 —— 光靠票据判断的话，票据一过期，
        同一段文字的每次轮询都会被当成新内容重新广播。
        """
        if not source or content is None:
            return
        with self.lock:
            self.data = self._load()
            latest = self.data.get("latest_global") or {}
            if latest.get("content") == content and latest.get("id"):
                item = {
                    "id": latest["id"],
                    "type": latest.get("type", "text"),
                    "content": content,
                    "timestamp": latest.get("timestamp") or datetime.now().isoformat(),
                    "source": latest.get("source", source),
                    "pasted": True,
                }
                latest["pasted"] = True
                self.data["latest_global"] = latest
            else:
                # 回音的是一份更早的内容：只更新该设备的持有记录，不动全局最新
                item = {
                    "id": "echo-" + self.content_key(content)[:12],
                    "type": "text",
                    "content": content,
                    "timestamp": datetime.now().isoformat(),
                    "source": (self.data.get("latest_global") or {}).get("source", source),
                    "pasted": True,
                }
            self.data["clients"][source] = item
            self._save()

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
            # 这台设备已经拿到这份内容了：登记投递记录，后面它把内容回传时就能认出来
            self._record_delivery_locked(client_name, item.get("content"))
            self._save()
