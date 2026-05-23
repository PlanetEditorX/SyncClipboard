# server/flask_app.py —— 修改配置加载路径
import json
import logging
from pathlib import Path
from flask import Flask, request, jsonify
import sys
import os
sys.path.append(os.path.dirname(__file__))  # 添加 server 目录到路径
from clipboard_manager import get_clipboard_text, set_clipboard_text, generate_id
from cache_manager import CacheManager
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
cache = CacheManager()
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

    logging.info("远程数据: %s", data)

    # 防死循环
    if data["source"] == LOCAL_NAME:
        logging.info("忽略来自本机的数据")
        return jsonify({"status": "ignored"}), 200

    # 比较时间
    local_item = cache.get_latest_text()
    logging.info("本地剪贴板: %s", local_item)

    if cache.search_text(data["content"]):
        logging.info("接受到来自 「%s」 的历史数据 “%s”，不更新本地缓存", data["source"], data["content"])
        return jsonify({"status": "ignored"}), 200

    else:
        logging.info("接受到来自 「%s」 的新数据 “%s”，更新本地缓存", data["source"], data["content"])
        item = build_text_item(
            text=data["content"],
            source=data["source"],
            pasted=False
        )
        cache.update_text(item)
        tracker.update(item)
        # 写入剪贴板
        set_clipboard_text(data["content"])
        # cache.update_cache("pasted", True)
        logging.info("已更新本地剪贴板: %s", data["content"])
        return jsonify({"status": "updated"}), 200


    # if data["timestamp"] >= local_item.get("timestamp", ""):
    #     # 远程比本地新，但已同步过
    #     if cache.id_exists(data["id"]):
    #         logging.info("忽略已同步的数据 id: %s", data["id"])
    #         return jsonify({"status": "ignored"}), 200

        # cache.update_text(data)
        # # 写入剪贴板
        # set_clipboard_text(data["content"])
        # # cache.update_cache("pasted", True)
        # logging.info("已更新本地剪贴板: %s", data["content"])
        # return jsonify({"status": "updated"}), 200

    if local_item["pasted"] == False:
        set_clipboard_text(local_item["content"])
        cache.update_text(local_item)
        logging.info("推送本地剪贴板: %s", local_item["content"])
        cache.update_cache("pasted", True)
        return jsonify({
            "status": "getting",
            "data": local_item["content"]
        }), 200

    logging.info("未更新剪贴板")
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