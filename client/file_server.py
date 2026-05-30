# client/file_server.py
import os
import json
import logging
import threading
import pyperclip
import requests
from threading import Thread
from urllib.parse import unquote
from common.utils import BASE_DIR
from server.services.client_tracker import ClientTracker
from flask import Flask, jsonify, send_file, after_this_request, request

FILE_LATEST_FILE = BASE_DIR / "latest" / "file_latest.json"
FILE_LATEST_FILE.parent.mkdir(parents=True, exist_ok=True)

logger = logging.getLogger("client")

def get_api_key():
    return request.headers.get("key", "")

def get_files_latest_file():
    """读取 files_latest.json，如果文件不存在或无效则返回 None"""
    if not FILE_LATEST_FILE.exists():
        return []
    try:
        with open(FILE_LATEST_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data
    except (json.JSONDecodeError, AttributeError):
        return None

class FileServer:
    def __init__(self, port=8899, center_host="127.0.0.1", center_port=8000, local_name="PC-01", key="123456"):
        self.port = port
        self.center_host = center_host
        self.center_port = center_port
        self.shared_files = {}
        self.app = Flask(__name__)
        # 关闭 Flask 默认访问日志
        logging.getLogger("werkzeug").setLevel(
            logging.ERROR
        )
        self._register_routes()
        # 全局锁，避免同时读写剪贴板
        self.clipboard_lock = threading.Lock()
        self.last_remote_id = None
        self._last_remote_content = None
        self.last_text = ""
        self.KEY = str(key)
        self.local_name = local_name
        self.tracker = ClientTracker()

    def _register_routes(self):
        @self.app.route("/ping", methods=["GET"])
        def ping():
            return jsonify({
                "status": "ok",
                "service": "file_server"
            })

        @self.app.route("/files", methods=["GET"])
        def files():
            client_ip = request.remote_addr
            logger.info(f"获取文件下载列表 - 请求来自: {client_ip}")
            return jsonify({
                "count": len(self.shared_files),
                "files": self.shared_files
            })

        @self.app.route("/update/client_latest", methods=["POST"])
        def update_client_latest():
            data = request.get_json()
            if not data:
                return jsonify({"status": "error", "message": "无效的请求数据"}), 400
            if data.get("key") != self.KEY:
                return jsonify({"status": "error", "message": "密钥错误"}), 403
            client_ip = request.remote_addr
            if data.get("type") == "text":
                logger.info(f"更新文字列表 - 请求来自: {client_ip}")
                latest = data.get("latest_global")
                if latest and latest.get("source") != self.local_name:
                    if latest["id"] != self.last_remote_id:
                        # 更新到文件
                        if self.tracker.is_duplicate(latest["id"]):
                            return jsonify({"status": "duplicate", "message": "重复内容"}), 200
                        # 更新记录（同时注册 ID、更新客户端最新和全局最新）
                        self.tracker.update(latest)

                        # 更新剪贴板
                        with self.clipboard_lock:
                            pyperclip.copy(latest["content"])
                        self.last_remote_id = latest["id"]
                        self._last_remote_content = latest["content"]
                        self.last_text = latest["content"]
                        logging.info(f"更新剪贴板: {latest['content'][:50]} (来自 {latest['source']})")
            else:
                logger.info(f"更新文件列表 - 请求来自: {client_ip}")
                latest = data.get("latest_global")
                # 获取本地已存储的文件
                file_latest = get_files_latest_file()
                if latest == file_latest:
                    logger.info(f"文件未变化，跳过写入")
                else:
                    # 只有当 file_id 不一致时才写入
                    file_latest = latest
                    with open(FILE_LATEST_FILE, "w", encoding="utf-8") as f:
                        json.dump(file_latest, f, ensure_ascii=False, indent=2)
                    logger.info("file_latest文件已更新")
            return jsonify({
                "status": "ok",
            })

        @self.app.route("/clear/file_latest", methods=["GET"])
        def clear_file_latest():
            key = get_api_key()
            if key != self.KEY:
                return jsonify({"status": "error", "message": "密钥错误"}), 403
            client_ip = request.remote_addr
            logger.info(f"清空文件列表 - 请求来自: {client_ip}")
            with open(FILE_LATEST_FILE, "w", encoding="utf-8") as f:
                json.dump([], f, ensure_ascii=False, indent=2)
            logger.info("file_latest文件已清空")
            return jsonify({
                "status": "ok",
            })

        @self.app.route("/check/<file_id>", methods=["GET"])
        def check_files(file_id):
            client_ip = request.remote_addr
            logger.info(f"检测文件状态 - 请求来自: {client_ip}")

            path = self.shared_files.get(file_id)
            if not path:
                return jsonify({"status": "error", "message": "file_id不存在"}), 404

            if not os.path.isfile(path):
                return jsonify({"status": "error", "message": "文件不存在"}), 404

            logger.info("文件正常: %s -> %s", file_id, path)
            return jsonify({"status": "ok", "message": "文件正常"}), 200

        @self.app.route("/file/<file_id>", methods=["GET"])
        def download_file(file_id):
            # 调试断点（仅在 DEBUG_MODE=1 时生效）
            # if os.getenv("DEBUG_MODE") == "1":
            #     debugpy.breakpoint()

            client_ip = request.remote_addr
            source = request.headers.get("source", "未知设备")
            logger.info(f"获取文件下载 - 请求来自: {unquote(request.headers.get("source", ""))}({client_ip})")

            path = self.shared_files.get(file_id)
            if not path:
                return jsonify({"status": "error", "message": "file_id不存在"}), 404

            if not os.path.isfile(path):
                return jsonify({"status": "error", "message": "文件不存在"}), 404

            logger.info("文件下载请求: %s -> %s", file_id, path)

            # 注册一个后置回调：文件发送完成后取消共享
            @after_this_request
            def after_download(response):
                # 取消本机共享
                self.unregister_file(file_id)
                return response

            return send_file(path, as_attachment=True)

    def register_file(self, file_id, path):
        """
        注册共享文件
        Parameters
        ----------
        file_id : str
            文件唯一ID
        path : str
            本地文件路径
        """
        self.shared_files[file_id] = path
        logger.info(
            "注册共享文件: %s -> %s",
            file_id,
            path
        )

    def unregister_file(self, file_id):
        """
        取消共享
        """
        if file_id in self.shared_files:
            path = self.shared_files.pop(file_id)
            logger.info(
                "取消共享文件: %s -> %s",
                file_id,
                path
            )

    def clear_files(self):
        """
        清空共享列表
        """
        self.shared_files.clear()
        logger.info(
            "共享文件列表已清空"
        )

    def get_file_path(self, file_id):
        """
        获取文件路径
        """
        return self.shared_files.get(file_id)

    def start(self):
        def run():
            logger.info(
                "文件服务启动: 0.0.0.0:%s",
                self.port
            )
            self.app.run(
                host="0.0.0.0",
                port=self.port,
                debug=False,
                use_reloader=False,
                threaded=True
            )
        Thread(
            target=run,
            daemon=True
        ).start()