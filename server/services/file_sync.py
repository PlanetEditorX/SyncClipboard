import os
import json
import logging
from pathlib import Path
from common.path import BASE_DIR

LATEST_FILE = BASE_DIR / "latest" / "file_latest.json"
LATEST_FILE.parent.mkdir(parents=True, exist_ok=True)

class LatestFileManager:
    def __init__(self, save_dir):
        self.save_dir = save_dir
        self.upload_dir = os.path.join(save_dir, "uploaded_files")
        os.makedirs(self.upload_dir, exist_ok=True)
        self.data = self._load()

    def _load(self):
        if os.path.exists(LATEST_FILE):
            with open(LATEST_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        return {
            "file_name": None,
            "file_size": 0,
            "source": None,
            "status": "idle",        # idle -> ready -> pending -> uploaded
            "saved_path": None
        }

    def _save(self):
        with open(LATEST_FILE, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)

    def update_meta(self, file_name, file_size, source):
        """电脑推送新文件元数据"""
        self.data["file_name"] = file_name
        self.data["file_size"] = file_size
        self.data["source"] = source
        self.data["status"] = "ready"
        self.data["saved_path"] = None
        self._save()
        logging.info(f"最新文件元数据已更新: {file_name} ({file_size} bytes)")

    def request_download(self, requested_by):
        """手机请求下载，如果状态为 ready 则改为 pending；若 uploaded 则直接返回路径"""
        if self.data["status"] == "ready":
            self.data["status"] = "pending"
            self._save()
            logging.info(f"收到下载请求，状态改为 pending，请求者: {requested_by}")
            return "pending", None
        elif self.data["status"] == "uploaded":
            return "uploaded", self.data["saved_path"]
        else:
            return self.data["status"], None

    def mark_uploaded(self, saved_path):
        """文件上传完成"""
        self.data["status"] = "uploaded"
        self.data["saved_path"] = saved_path
        self._save()
        logging.info(f"文件已上传: {saved_path}")

    def get_status(self):
        return self.data["status"]

    def get_info(self):
        return {
            "file_name": self.data["file_name"],
            "file_size": self.data["file_size"],
            "source": self.data["source"],
            "status": self.data["status"]
        }