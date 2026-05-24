# server/api/flask_app.py
import os
import sys
import json
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
# cache = CacheManager()

# KEY = config["key"]
# LOCAL_NAME = config["local_name"]
# SAVE_PATH = config["save_path"]
# PORT = config["port"]

# file_handler = FileHandler(SAVE_PATH)
# latest_file = LatestFileManager(SAVE_PATH)
# latest_file = LatestFileTracker()
# tracker = ClientTracker()

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

    # 新增：获取请求客户端的名称（用于自动标记粘贴）
    source = request.args.get("source", "")

    latest = tracker.get_global_latest()

    # 如果提供了 source，且最新内容存在，且不是该客户端自己推送的，则自动标记粘贴
    if source and latest and latest.get("source") != source:
        # 构建已粘贴条目（内容、id 保持不变，来源为原始来源）
        pasted_item = {
            "id": latest["id"],
            "type": latest.get("type", "text"),
            "content": latest["content"],
            "timestamp": datetime.now().isoformat(),  # 标记时间
            "source": latest["source"],              # 原始来源
            "pasted": True
        }
        tracker.mark_pasted(source, pasted_item)
        logging.info("客户端 %s 已获取并标记粘贴: %s (来自 %s)", source, latest["content"][:30], latest["source"])

    return jsonify({"status": "ok", "latest_global": latest})

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

    data = request.get_json()
    path = data.get("path")
    name = data.get("name")
    size = data.get("size", 0)

    if not path or not name:
        return jsonify({"status": "error", "message": "参数不完整"}), 400

    latest_file.set_latest(path, name, size)
    logging.info(f"最新文件已记录: {name} ({size} bytes) 路径: {path}")
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



# @app.route('/file_sync', methods=['POST'])
# def file_sync():
#     """电脑客户端推送最新文件元数据"""
#     key = request.headers.get("key", "")
#     if key != KEY:
#         return jsonify({"status": "error", "message": "密钥错误"}), 403

#     data = request.get_json()
#     file_name = data.get("file_name")
#     file_size = data.get("file_size", 0)
#     source = data.get("source")
#     if not file_name or not source:
#         return jsonify({"status": "error", "message": "参数不完整"}), 400

#     latest_file.update_meta(file_name, file_size, source)
#     return jsonify({"status": "ok"})


# @app.route('/request_file', methods=['POST'])
# def request_file():
#     """手机一键请求下载最新文件（无需任何参数）"""
#     key = request.headers.get("key", "")
#     if key != KEY:
#         return jsonify({"status": "error", "message": "密钥错误"}), 403

#     data = request.get_json()
#     requested_by = data.get("source", "unknown")

#     status, filepath = latest_file.request_download(requested_by)

#     if status == "uploaded":
#         # 文件已就绪，直接返回文件
#         return send_file(filepath, as_attachment=True)
#     elif status == "pending":
#         return jsonify({"status": "pending", "message": "文件请求已发出，等待上传"}), 202
#     elif status == "ready":
#         return jsonify({"status": "ready", "message": "文件元数据已就绪，但尚未请求"}), 200
#     else:
#         return jsonify({"status": "idle", "message": "暂无最新文件"}), 404


# @app.route('/upload_file', methods=['POST'])
# def upload_file():
#     """电脑客户端手动上传文件"""
#     key = request.headers.get("key", "")
#     if key != KEY:
#         return jsonify({"status": "error", "message": "密钥错误"}), 403

#     file = request.files.get("file")
#     if not file:
#         return jsonify({"status": "error", "message": "未收到文件"}), 400

#     # 保存文件
#     filename = file.filename
#     save_path = os.path.join(latest_file.upload_dir, filename)
#     file.save(save_path)

#     latest_file.mark_uploaded(save_path)
#     return jsonify({"status": "ok"})


# @app.route('/download_file', methods=['GET'])
# def download_file():
#     """手机轮询下载实际文件"""
#     key = request.headers.get("key", "")
#     if key != KEY:
#         return jsonify({"status": "error", "message": "密钥错误"}), 403

#     status = latest_file.get_status()
#     if status == "uploaded":
#         return send_file(latest_file.data["saved_path"], as_attachment=True)
#     else:
#         return jsonify({"status": status, "message": "文件尚未准备好"}), 202

# # ------------------- 文件上传接口 -------------------
# @app.route("/file_upload", methods=["POST"])
# def file_upload():
#     logging.info("收到 /file_upload 请求")
#     if request.form.get("key") != KEY:
#         logging.warning("文件上传密钥不匹配")
#         return jsonify({"error": "Invalid key"}), 403

#     f = request.files.get("file")
#     if not f:
#         logging.warning("没有上传文件")
#         return jsonify({"error": "No file"}), 400

#     try:
#         path = file_handler.save_file(f.filename, f.read())
#         logging.info("文件保存成功: %s", path)
#         return jsonify({"status": "saved", "path": path})
#     except Exception as e:
#         logging.error("文件保存失败: %s", str(e))
#         return jsonify({"error": "Save failed"}), 500

# # ------------------- 获取本地文字接口 -------------------
# @app.route("/get_text", methods=["GET"])
# def get_text():
#     logging.info("收到 /get_text 请求")
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