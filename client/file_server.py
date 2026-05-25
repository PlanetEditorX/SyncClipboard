# client/file_server.py
import os
import logging
import requests
from threading import Thread

from flask import Flask, jsonify
from flask import Flask, jsonify, send_file

logger = logging.getLogger("client")


class FileServer:
    def __init__(self, port=8899):
        self.port = port

        # file_id -> path
        self.shared_files = {}

        self.app = Flask(__name__)

        # 关闭 Flask 默认访问日志
        logging.getLogger("werkzeug").setLevel(
            logging.ERROR
        )

        self._register_routes()

    def _register_routes(self):

        @self.app.route("/ping", methods=["GET"])
        def ping():
            return jsonify({
                "status": "ok",
                "service": "file_server"
            })

        @self.app.route("/files", methods=["GET"])
        def files():
            return jsonify({
                "count": len(self.shared_files),
                "files": self.shared_files
            })

        @self.app.route(
            "/file/<file_id>",
            methods=["GET"]
        )
        def download_file(file_id):
            path = self.shared_files.get(file_id)
            if not path:
                return jsonify({
                    "status": "error",
                    "message": "file_id不存在"
                }), 404

            if not os.path.isfile(path):
                return jsonify({
                    "status": "error",
                    "message": "文件不存在"
                }), 404

            logger.info(
                "文件下载请求: %s -> %s",
                file_id,
                path
            )

            return send_file(
                path,
                as_attachment=True
            )

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