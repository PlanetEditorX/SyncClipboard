# server/api/flask_app.py
import os
import sys
import json
import logging
import requests
import threading
from pathlib import Path
from datetime import datetime
from urllib.parse import unquote
from flask import Flask, request, jsonify, send_file

# 统一使用 server 包路径的绝对导入
from server.core.clipboard_manager import get_clipboard_text, set_clipboard_text, generate_id
from server.core.cache_manager import CacheManager
from server.core.item_builder import build_text_item
from server.services.file_handler import FileHandler
from server.services.client_tracker import ClientTracker
from server.services.file_sync import LatestFileManager
from server.services.latest_file import LatestFileTracker

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

# ------------------- 客户端列表 -------------------
clients = []  # 内存中的客户端列表
_lock = threading.Lock()
CLIENT_IP_FILE = Path(__file__).resolve().parent.parent.parent / "config" / "client_ip.json"

def load_clients():
    global clients
    if CLIENT_IP_FILE.exists():
        try:
            with open(CLIENT_IP_FILE, "r") as f:
                clients = json.load(f)
        except:
            clients = []

def save_clients():
    with _lock:
        with open(CLIENT_IP_FILE, "w") as f:
            json.dump(clients, f, indent=2, ensure_ascii=False)

def add_or_update_client(ip, port, local_name):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    # 查找是否已存在同 IP
    for c in clients:
        if c["ip"] == ip:
            c["last_seen"] = now
            # 可更新端口和名称（如果变化）
            c["port"] = port
            c["local_name"] = local_name
            save_clients()
            return False  # 已存在，仅更新
    # 新客户端
    clients.append({
        "ip": ip,
        "port": port,
        "local_name": local_name,
        "first_seen": now,
        "last_seen": now
    })
    save_clients()
    return True

# 通知客户端
def notify_clients(_type):
    if _type == "text":
        # 获取全局最新内容
        latest = tracker.get_global_latest()
        if latest is None:
            return  # 如果没有最新内容，直接退出
        else:
            latest_global = latest.copy()
    else:
        latest  = latest_file.get_latest()
    for client in clients:
        source = client["local_name"]
        client_ip = client['ip']
        local_name = client['local_name']

        # 核心判断：如果最新内容不是该客户端自己推送的，才需要通知它
        if latest.get("source") == source or latest.get("source") == local_name or latest == None:
            continue

        if _type == "text":
            # 检查该客户端是否已经标记过粘贴（防止重复推送）
            client_last = tracker.data.get("clients", {}).get(source)
            already_pasted = (
                client_last
                and client_last.get("id") == latest["id"]
                and client_last.get("pasted") is True
            )
        else:
            # 文件默认未使用，已粘贴的文件会自动清理数据
            already_pasted = False
            latest_global = latest.copy()

        if already_pasted:
            continue

        # --- 第一步：检查客户端是否在线 ---
        check_url = f"http://{client_ip}:{client['port']}/ping"
        try:
            resp = requests.get(check_url, timeout=5)
            if resp.status_code != 200:
                logging.warning(f"客户端 {source}({client_ip}) 状态异常: {resp.status_code}")
                continue
            logging.info(f"客户端 {source}({client_ip}) 在线，准备推送")
        except Exception:
            logging.warning(f"客户端 {source}({client_ip}) 离线")
            continue

        # --- 第二步：在线状态确认无误后，执行推送 ---
        if _type == "text":
            # 标记为已粘贴
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
                str(latest["content"])[:30], # 防止 content 不是字符串导致报错
                latest["source"]
            )
        else:
            logging.info(
                "客户端 %s 已获取文件发布通知: %s (来自 %s)",
                client["local_name"],
                latest["name"],
                latest["source"]
            )
        # 推送更新到客户端
        update_url = f"http://{client_ip}:{client['port']}/update/client_latest"
        try:
            resp = requests.post(
                update_url,
                json={
                    "key": KEY,
                    "latest_global": latest_global,
                    "server_source": LOCAL_NAME,
                    "type": _type
                },
                timeout=5
            )
            if resp.status_code == 200:
                if _type == "text":
                    content_preview = str(latest["content"])[:50]
                    logging.info(f"文字推送成功: {content_preview}...")
                else:
                    logging.info(f"文件发布推送成功: {latest["name"]}...")
            else:
                logging.warning(f"推送失败: {resp.status_code} {resp.text}")
        except Exception as e:
            logging.error(f"连接客户端 {source} 失败: {e}")

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
        latest_global = tracker.get_global_latest()   # 必然就是这个 item
    else:
        # 内容没变 → 纯拉取操作
        latest = tracker.get_global_latest()
        # 如果全局最新不是自己，则标记该手机已粘贴（更新 clients）
        if source and latest and latest.get("source") != source:
            # 新内容
            if latest.get("content") != content:
                latest_global = latest.copy()
                latest_global["pasted"] = False
                pasted_item = {
                    "id": latest["id"],
                    "type": latest.get("type", "text"),
                    "content": latest["content"],
                    "timestamp": datetime.now().isoformat(),
                    "source": latest["source"],   # 保留原始来源
                    "pasted": True
                }
                tracker.mark_pasted(source, pasted_item)
            else:
                latest_global = {
                    "pasted": False
                }

    return jsonify({"status": "ok", "latest_global": latest_global})

@app.route('/latest', methods=['GET'])
# 最新数据
def get_latest():
    key = get_api_key()
    if key != KEY:
        return jsonify({"status": "error", "message": "密钥错误"}), 403

    # 获取请求客户端的名称（用于自动标记粘贴）
    source = request.args.get("source", "")
    latest = tracker.get_global_latest()
    if latest is None:
        latest_global = {}
    else:
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
            latest_global["pasted"] = False
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
        else:
            latest_global = {
                "pasted": False
            }

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
    file_id = data.get("file_id", 0)
    path = data.get("path")
    name = data.get("name")
    size = data.get("size", 0)
    source = data.get("source", 0)
    port = data.get("port", 8899)

    if not path or not name or not source or not file_id or not port:
        return jsonify({"status": "error", "message": "参数不完整"}), 400

    latest_file.set_latest(file_id, path, name, size, source, client_ip, port)
    logging.info(f"最新文件已记录: {name} ({size} bytes), 路径: {path}, 来源: {source}")
    return jsonify({"status": "ok"})

@app.route('/latest/clear', methods=['GET'])
def clear_latest():
    """清理最新文件"""
    latest_file.clear()
    return jsonify({"status": "ok"}), 200

@app.route('/request_file', methods=['POST'])
def request_file():
    """统一拉取接口：有文件则返回文件并清空，无文件则返回最新文本"""
    key = request.headers.get("key", "")
    if key != KEY:
        return jsonify({"status": "error", "message": "密钥错误"}), 403

    data = request.get_json()
    if not data:
        return jsonify({"status": "error", "message": "无效请求"}), 400
    source = data.get("source", "unknown")

    # 1. 检查是否有最新文件
    info = latest_file.get_latest()
    path = None
    if info:
        path = info.get("path")

    if path:
        # 服务器能直接访问到
        if os.path.isfile(path):
            filename = info["name"]
            # 清空文件记录，避免重复下载
            latest_file.clear()
            return send_file(path, as_attachment=True, download_name=filename)
        else:
            # 为其它客户端的文件
            download_url = f"http://{info['ip']}:{info['port']}/file/{info['file_id']}"
            check_url = f"http://{info['ip']}:{info['port']}/check/{info['file_id']}"
            try:
                resp = requests.get(check_url, timeout=5)
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("status") == "ok":
                        # 文件正常，返回给客户端
                        return jsonify({
                            "status": "download",
                            "type": "file",
                            "name": info["name"],
                            "download_url": download_url
                        }), 200
                    else:
                        # 远程文件不可用，提取它返回的 message
                        remote_msg = data.get("message", "未知错误")
                        latest_file.clear()
                        return jsonify({
                            "status": "error",
                            "message": f"远程文件[{info['name']}]不可用: {remote_msg}"
                        }), 503
                else:
                    # HTTP 状态码非 200，尝试提取错误信息
                    remote_msg = ""
                    try:
                        err_data = resp.json()
                        remote_msg = err_data.get("message", "")
                    except:
                        pass
                    latest_file.clear()
                    return jsonify({
                        "status": "error",
                        "message": f"远程检查失败，HTTP {resp.status_code}" + (f": {remote_msg}" if remote_msg else "")
                    }), 503
            except requests.exceptions.RequestException as e:
                latest_file.clear()
                return jsonify({
                    "status": "error",
                    "message": f"无法连接到 {info['ip']}:{info['port']}，原因：{str(e)}"
                }), 503

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

# 客户端注册
@app.route('/register', methods=['POST'])
def register():
    data = request.get_json(silent=True) or {}
    # IP 优先使用服务器看到的 remote_addr
    ip = request.remote_addr
    # 如果客户端明确传了 ip，且与 remote_addr 不同，可根据需求选择

    port = data.get('file_server_port', 0)
    local_name = data.get('local_name', 'unknown')
    key = data.get('key')

    # 可在此验证密钥，与 server_config 中的 key 比对
    if key != app.config.get('key'):
        return jsonify({"status": "error", "msg": "invalid key"}), 403
    load_clients()
    is_new = add_or_update_client(ip, port, local_name)
    logging.info(f"客户端 {local_name}({ip}) 已成功注册。")
    return jsonify({
        "status": "ok",
        "is_new": is_new,
        "server_ip": ip  # 告诉客户端服务器认为它的 IP 是什么
    })

# 内部通知接口
@app.route('/internal/notify_clients', methods=['POST'])
def internal_notify():
    # 验证请求来源是本地
    if request.remote_addr == '127.0.0.1':
        data = request.get_json()
        notify_clients(data['changed_type'])
        return {'status': 'ok'}

# ------------------- 启动函数 -------------------
def start_flask():
    logging.info("Flask 服务启动，监听端口: %s", PORT)
    app.run(host="0.0.0.0", port=PORT, debug=True, use_reloader=False)

# ------------------- 独立运行 -------------------
if __name__ == "__main__":
    print("启动 SyncClipboard Flask 服务...")
    logging.info("脚本直接运行，启动服务")
    start_flask()