# server/flask_app.py —— 修改配置加载路径
import json
import logging
from pathlib import Path
from flask import Flask, request, jsonify
import sys
import os
sys.path.append(os.path.dirname(__file__))  # 添加 server 目录到路径
from clipboard_manager import get_clipboard_text, set_clipboard_text, generate_id
# from cache_manager import CacheManager
from file_handler import FileHandler
from item_builder import build_text_item
from logging.handlers import RotatingFileHandler
from client_tracker import ClientTracker


# 日志配置（保持不变）
LOG_FILE = Path("syncclipboard.log")
handler = RotatingFileHandler(LOG_FILE, maxBytes=1*1024*1024, backupCount=0, encoding='utf-8')
formatter = logging.Formatter(fmt='%(asctime)s [%(levelname)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
handler.setFormatter(formatter)
logger = logging.getLogger()
logger.setLevel(logging.INFO)
logger.addHandler(handler)
logging.info("服务初始化完成")

app = Flask(__name__)
# cache = CacheManager()
tracker = ClientTracker()

# ---------- 修改点：从 server_config.json 加载 ----------
with open("server_config.json", "r", encoding="utf-8") as f:
    config = json.load(f)

KEY = config["key"]
LOCAL_NAME = config["local_name"]
SAVE_PATH = config["save_path"]
PORT = config["port"]

file_handler = FileHandler(SAVE_PATH)
logging.info("配置加载完成: %s", config)

# ------------------- 文字同步接口 -------------------
@app.route('/text_sync', methods=['POST'])
def text_sync():
    data = request.get_json()
    if not data:
        return jsonify({"status": "error", "message": "无效的请求数据"}), 400

    if data.get("key") != KEY:
        return jsonify({"status": "error", "message": "密钥错误"}), 403

    source = data.get("source", "")
    if source == LOCAL_NAME:
        return jsonify({"status": "ignored", "message": "忽略自身来源"}), 200

    content = data.get("content", "")
    if not content:
        return jsonify({"status": "error", "message": "内容为空"}), 400

    item = build_text_item(text=content, source=source, pasted=False)

    # 去重：用 tracker 的 is_duplicate 方法
    if tracker.is_duplicate(item["id"]):
        return jsonify({"status": "duplicate", "message": "重复内容"}), 200

    # 更新记录（同时注册 ID、更新客户端最新和全局最新）
    tracker.update(item)

    # 同步到服务端剪贴板（可选，看需求）
    set_clipboard_text(content)

    logging.info("同步文本: %s", content[:50])
    return jsonify({"status": "ok", "message": "同步成功"}), 200

# ------------------- 文件上传接口 -------------------
@app.route("/file_upload", methods=["POST"])
def file_upload():
    logging.info("收到 /file_upload 请求")
    if request.form.get("key") != KEY:
        logging.warning("文件上传密钥不匹配")
        return jsonify({"error": "Invalid key"}), 403

    f = request.files.get("file")
    if not f:
        logging.warning("没有上传文件")
        return jsonify({"error": "No file"}), 400

    try:
        path = file_handler.save_file(f.filename, f.read())
        logging.info("文件保存成功: %s", path)
        return jsonify({"status": "saved", "path": path})
    except Exception as e:
        logging.error("文件保存失败: %s", str(e))
        return jsonify({"error": "Save failed"}), 500

# ------------------- 获取本地文字接口 -------------------
@app.route("/get_text", methods=["GET"])
def get_text():
    logging.info("收到 /get_text 请求")
    key = request.headers.get("key")  # 从头部获取
    order = request.headers.get("order")
    if key != KEY:
        logging.warning("get_text 密钥不匹配: %s", key)
        return jsonify({"error": "Invalid key"}), 403

    data = cache.get_latest_text()
    logging.info("返回本地剪贴板: %s", data)
    if order == 'first':
        _data = {
            "status": "getting",
            "data": data
        }
    else:
        _data = {
                "status": "getting",
                "data": data["content"],
                "pasted": data["pasted"]
            }
    cache.update_cache("pasted", True)
    return jsonify(_data), 200

# ------------------- 启动函数 -------------------
def start_flask():
    logging.info("Flask 服务启动，监听端口: %s", PORT)
    app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)

# ------------------- 独立运行 -------------------
if __name__ == "__main__":
    print("启动 SyncClipboard Flask 服务...")
    logging.info("脚本直接运行，启动服务")
    start_flask()