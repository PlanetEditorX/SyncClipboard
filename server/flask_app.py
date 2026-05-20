from flask import Flask, request, jsonify
import sys
import os
sys.path.append(os.path.dirname(__file__))  # 添加 server 目录到路径
from clipboard_manager import get_clipboard_text, set_clipboard_text, generate_id
from cache_manager import CacheManager
from file_handler import FileHandler
import json
import logging
from pathlib import Path

# ------------------- 日志配置 -------------------
LOG_FILE = Path("syncclipboard.log")
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logging.info("服务初始化完成")

# ------------------- Flask & 缓存 & 文件 -------------------
app = Flask(__name__)
cache = CacheManager()

with open("config.json", "r", encoding="utf-8") as f:
    config = json.load(f)
KEY = config["key"]
LOCAL_NAME = config["local_name"]
SAVE_PATH = config["save_path"]
PORT = config["port"]

file_handler = FileHandler(SAVE_PATH)
logging.info("配置加载完成: %s", config)

# ------------------- 文字同步接口 -------------------
@app.route("/text_sync", methods=["POST"])
def text_sync():
    logging.info("收到 /text_sync 请求")
    data = request.json
    if not data:
        logging.warning("请求没有 JSON 数据")
        return jsonify({"error": "No JSON"}), 400

    if data.get("key") != KEY:
        logging.warning("密钥不匹配: %s", data.get("key"))
        return jsonify({"error": "Invalid key"}), 403

    remote_item = data.get("item")
    if not remote_item:
        logging.warning("没有 item 字段")
        return jsonify({"error": "No item"}), 400

    logging.info("远程 item: %s", remote_item)

    # 防死循环
    if remote_item["source"] == LOCAL_NAME:
        logging.info("忽略来自本机的 item")
        return jsonify({"status": "ignored"}), 200

    if cache.id_exists(remote_item["id"]):
        logging.info("忽略已同步的 item id: %s", remote_item["id"])
        return jsonify({"status": "ignored"}), 200

    # 比较时间
    local_item = cache.get_text()
    logging.info("本地剪贴板: %s", local_item)

    if remote_item["timestamp"] > local_item.get("timestamp", ""):
        set_clipboard_text(remote_item["content"])
        cache.update_text(remote_item)
        logging.info("已更新本地剪贴板: %s", remote_item["content"])
        return jsonify({"status": "updated"}), 200

    logging.info("未更新剪贴板，因为本地较新")
    return jsonify({"status": "ignored"}), 200

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
    key = request.args.get("key")
    if key != KEY:
        logging.warning("get_text 密钥不匹配: %s", key)
        return jsonify({"error": "Invalid key"}), 403

    item = cache.get_text()
    logging.info("返回本地剪贴板: %s", item)
    return jsonify({"item": item})

# ------------------- 启动函数 -------------------
def start_flask():
    logging.info("Flask 服务启动，监听端口: %s", PORT)
    app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)

# ------------------- 独立运行 -------------------
if __name__ == "__main__":
    print("启动 SyncClipboard Flask 服务...")
    logging.info("脚本直接运行，启动服务")
    start_flask()