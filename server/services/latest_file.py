import os
import json
from pathlib import Path
from common.utils import BASE_DIR

STATE_FILE = BASE_DIR / "latest" / "file_latest.json"
STATE_FILE.parent.mkdir(parents=True, exist_ok=True)

class LatestFileTracker:
    def __init__(self):
        self.data = self._load()

    def _load(self):
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                # 确保所有字段都存在（兼容旧数据）
                return {
                    "file_id": data.get("file_id"),
                    "path": data.get("path"),
                    "name": data.get("name"),
                    "size": data.get("size", 0),
                    "source": data.get("source"),
                    "ip": data.get("ip"),
                    "port": data.get("port")  # 补全 port 字段
                }
        return self._empty_data()

    def _empty_data(self):
        """返回空数据模板"""
        return {
            "file_id": None,
            "path": None,
            "name": None,
            "size": 0,
            "source": None,
            "ip": None,
            "port": None
        }

    def _save(self):
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=2, ensure_ascii=False)

    def set_latest(self, uuid, path, name, size, source, ip, port):
        """设置最新的文件信息"""
        # 验证必要参数
        if not uuid:
            raise ValueError("uuid cannot be empty")

        self.data["file_id"] = uuid
        self.data["path"] = path
        self.data["name"] = name
        self.data["size"] = size
        self.data["source"] = source
        self.data["ip"] = ip
        self.data["port"] = port
        self._save()

    def get_latest(self):
        """
        返回文件信息字典，如果没有有效文件则返回 None

        返回格式:
        {
            "file_id": str,  # 必须存在且非空
            "path": str or None,  # 本地路径，None 表示远程文件
            "name": str,
            "size": int,
            "source": str,
            "ip": str,
            "port": int or None
        }
        """
        # 检查是否有有效的文件记录（file_id 存在且非空）
        if self.data.get("file_id"):
            return self.data.copy()  # 返回副本，避免外部修改
        return None

    def clear(self):
        """清空文件记录"""
        self.data = self._empty_data()
        self._save()

    def is_remote_file(self):
        """判断当前文件是否为远程文件"""
        if not self.data.get("file_id"):
            return False
        return not self.data.get("path") or not os.path.isfile(self.data["path"])