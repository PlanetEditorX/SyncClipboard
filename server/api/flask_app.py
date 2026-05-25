# server/api/flask_app.py
import os
import sys
import json
import uuid
import logging
from pathlib import Path
from flask import Flask, request, jsonify, send_file

# 统一使用 server 包路径的绝对导入
from server.core.clipboard_manager import get_clipboard_text, set_clipboard_text, generate_id
from server.core.cache_manager import CacheManager
from server.core.item_builder import build_text_item
from server.services.file_handler import FileHandler
from server.services.client_tracker import ClientTracker
from server.services.file_sync import LatestFileManager
from server.services.latest_file import LatestFileTracker

from datetime import datetime
from urllib.parse import unquote

# ---------- 日志：不再配置 handler，交给 run.py 统一处理 ----------
logger = logging.getLogger(__name__)   # 使用模块级 logger，会自动继承根 logger 的 handler

app = Flask(__name__)

KEY = None
LOCAL_NAME = None
SAVE_PATH = None
PORT = None
tracker = None
file_handler = None
latest_file = None

def init_services():
    """由 run.py 在配置注入后调用，初始化依赖配置的服务"""
    global tracker, file_handler, latest_file, KEY, LOCAL_NAME, SAVE_PATH, PORT
    from server.services.client_tracker import ClientTracker
    from server.services.file_handler import FileHandler
    from server.services.file_sync import LatestFileManager  # 或 LatestFileTracker

    tracker = ClientTracker()
    file_handler = FileHandler(app.config['save_path'])
    latest_file = LatestFileTracker()

    KEY = app.config["key"]
    LOCAL_NAME = app.config["local_name"]
    SAVE_PATH = app.config["save_path"]
    PORT = app.config["port"]

    logger.info("API组件初始化完成")


def get_api_key():
    return request.headers.get("key", "")

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

    # # 同步到服务端剪贴板（可选，看需求）
    # set_clipboard_text(content)

    logging.info("同步文本: %s", content[:50])
    return jsonify({"status": "ok", "message": "文字同步成功"}), 200

@app.route('/sync', methods=['POST'])
def sync():
    data = request.get_json()
    if not data or data.get("key") != KEY:
        return jsonify({"status": "error", "message": "密钥错误"}), 403

    source = data.get("source", "")
    content = data.get("content", "")

    # 获取该客户端当前记录
    client_last = tracker.data.get("clients", {}).get(source)

    # 判断内容是否发生了变化（首次连接也算变化）
    is_new = (not client_last) or (client_last.get("content") != content)

    if is_new and content and source != LOCAL_NAME:
        # 手机有新内容 → 强制更新为自己，并设为全局最新
        item = build_text_item(text=content, source=source, pasted=False)
        if not tracker.is_duplicate(item["id"]):
            tracker.update(item, force_latest=True)
        latest = tracker.get_global_latest()   # 必然就是这个 item
    else:
        # 内容没变 → 纯拉取操作
        latest = tracker.get_global_latest()
        # 如果全局最新不是自己，则标记该手机已粘贴（更新 clients）
        if source and latest and latest.get("source") != source:
            pasted_item = {
                "id": latest["id"],
                "type": latest.get("type", "text"),
                "content": latest["content"],
                "timestamp": datetime.now().isoformat(),
                "source": latest["source"],   # 保留原始来源
                "pasted": True
            }
            tracker.mark_pasted(source, pasted_item)

    return jsonify({"status": "ok", "latest_global": latest})

@app.route('/latest', methods=['GET'])
def get_latest():
    key = get_api_key()
    if key != KEY:
        return jsonify({"status": "error", "message": "密钥错误"}), 403

    # logger.info(
    #     "LATEST REQUEST source=%s ua=%s",
    #     request.args.get("source"),
    #     request.headers.get("User-Agent")
    # )

    # 获取请求客户端的名称（用于自动标记粘贴）
    source = request.args.get("source", "")

    latest = tracker.get_global_latest()
    latest_global = latest.copy()

    # 如果提供了 source，且最新内容存在，且不是该客户端自己推送的，则自动标记粘贴
    if source and latest and latest.get("source") != source:

        client_last = tracker.data.get("clients", {}).get(source)

        already_pasted = (
            client_last
            and client_last.get("id") == latest["id"]
            and client_last.get("pasted") is True
        )

        # 首次
        if not already_pasted:
            latest_global = latest.copy()
            pasted_item = {
                "id": latest["id"],
                "type": latest.get("type", "text"),
                "content": latest["content"],
                "timestamp": datetime.now().isoformat(),
                "source": latest["source"],
                "pasted": True
            }

            tracker.mark_pasted(source, pasted_item)

            logging.info(
                "客户端 %s 已获取并标记粘贴: %s (来自 %s)",
                source,
                latest["content"][:30],
                latest["source"]
            )

    return jsonify({"status": "ok", "latest_global": latest_global}), 200

@app.route('/mark_pasted', methods=['POST'])
def mark_pasted():
    data = request.get_json()
    if not data or data.get("key") != KEY:
        return jsonify({"status": "error", "message": "密钥错误"}), 403

    source = data.get("source")          # 客户端名称，如 "PC-01"
    item_id = data.get("id")
    content = data.get("content")
    original_source = data.get("original_source")  # 内容的原始来源，如 "Xun’s iPhone"

    if not all([source, item_id, content, original_source]):
        return jsonify({"status": "error", "message": "参数不完整"}), 400

    # 构建粘贴条目
    from datetime import datetime
    item = {
        "id": item_id,
        "type": "text",
        "content": content,
        "timestamp": datetime.now().isoformat(),
        "source": original_source,   # 保留原始来源
        "pasted": True
    }

    tracker.mark_pasted(source, item)
    logging.info("客户端 %s 已粘贴: %s (原始来源: %s)", source, content[:30], original_source)
    return jsonify({"status": "ok"})

# ---------- 文件同步相关路由 ----------
# ---------- 文件同步（独立于文本）----------
@app.route('/file_sync', methods=['POST'])
def file_sync():
    """电脑复制文件时调用，告诉服务端最新文件的路径"""
    key = request.headers.get("key", "")
    if key != KEY:
        return jsonify({"status": "error", "message": "密钥错误"}), 403

    client_ip = request.remote_addr
    data = request.get_json()
    file_id = str(uuid.uuid4())
    path = data.get("path")
    name = data.get("name")
    size = data.get("size", 0)
    source = data.get("source", 0)

    if not path or not name or not source:
        return jsonify({"status": "error", "message": "参数不完整"}), 400

    latest_file.set_latest(file_id, path, name, size, source, client_ip)
    logging.info(f"最新文件已记录: {name} ({size} bytes), 路径: {path}, 来源: {source}")
    return jsonify({"status": "ok"})


@app.route('/request_file', methods=['POST'])
def request_file():
    """手机统一拉取接口：有文件则返回文件并清空，无文件则返回最新文本"""
    key = request.headers.get("key", "")
    if key != KEY:
        return jsonify({"status": "error", "message": "密钥错误"}), 403

    data = request.get_json()
    if not data:
        return jsonify({"status": "error", "message": "无效请求"}), 400
    source = data.get("source", "unknown")

    # 1. 检查是否有最新文件
    info = latest_file.get_latest()
    path = info.get("path")
    if path and os.path.isfile(path):
        filename = info["name"]
        # 清空文件记录，避免重复下载
        latest_file.clear()
        return send_file(path, as_attachment=True, download_name=filename)

    # 2. 没有文件，执行文本拉取逻辑（同 /latest 的标记粘贴）
    latest = tracker.get_global_latest()
    if source and latest and latest.get("source") != source:
        pasted_item = {
            "id": latest["id"],
            "type": latest.get("type", "text"),
            "content": latest["content"],
            "timestamp": datetime.now().isoformat(),
            "source": latest["source"],
            "pasted": True
        }
        tracker.mark_pasted(source, pasted_item)
        logging.info("客户端 %s 已获取并标记粘贴: %s (来自 %s)", source, latest["content"][:30], latest["source"])

    return jsonify({
        "status": "ok",
        "type": "text",
        "latest_global": latest
    })

@app.route('/upload_file', methods=['PUT'])
def upload_file():
    """手机主动上传文件到电脑（服务端保存到 save_path）"""
    key = get_api_key()
    if key != KEY:
        return jsonify({"status": "error", "message": "密钥错误"}), 403

    # 从 URL 参数获取文件名，例如 ?filename=my%20file.txt
    encoded_filename = request.args.get('filename', 'uploaded_file')
    filename = unquote(encoded_filename)  # 解码 %20 为空格

    # 读取原始二进制数据
    file_data = request.get_data()

    if not file_data:
        return jsonify({"status": "error", "message": "未收到文件"}), 400

    # 保存到文件
    save_path = os.path.join(SAVE_PATH, filename)
    with open(save_path, 'wb') as f:
        f.write(file_data)

    logging.info(f"手机上传文件已保存: {save_path}")
    return jsonify({"status": "ok", "message": "文件上传成功","path": save_path}), 200

@app.route('/status', methods=['GET'])
def status():
    return jsonify({
        "status": "ok",
        "message": "SyncClipboard Server Running"
    }), 200

# ------------------- 启动函数 -------------------
def start_flask():
    logging.info("Flask 服务启动，监听端口: %s", PORT)
    app.run(host="0.0.0.0", port=PORT, debug=True, use_reloader=False)

# ------------------- 独立运行 -------------------
if __name__ == "__main__":
    print("启动 SyncClipboard Flask 服务...")
    logging.info("脚本直接运行，启动服务")
    start_flask()