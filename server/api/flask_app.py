# server/api/flask_app.py
import os
import sys
import json
import socket
import logging
import requests
import threading
from pathlib import Path
from urllib.parse import unquote
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, send_file


def get_default_save_dir():
    return str(Path.home() / "Downloads")

# 统一使用 server 包路径的绝对导入
from server.core.item_builder import build_text_item
from server.core.file_handler import FileHandler
from server.core.text_tracker import ClientTracker
from server.core.file_sync import LatestFileManager
from server.core.file_latest import FileLatestTracker
from common.utils import BASE_DIR
from common.notification import show_notification

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
computer_name = socket.gethostname()
clipboard_lock = threading.Lock()
CLIPBOARD_ENABLED = True
CLIENT_EXPIRE_HOURS = 168   # 客户端超过1周未出现就删除

def init_services(config_manager=None):
    """由 run.py 在配置注入后调用，初始化依赖配置的服务"""
    global tracker, file_handler, latest_file, KEY, LOCAL_NAME, SAVE_PATH, PORT
    # 如果传入了 config_manager，使用它来设置 app.config
    if config_manager:
        # 确保 app.config 中有必要的配置
        if not app.config.get('key'):
            app.config['key'] = config_manager.key
        if not app.config.get('local_name'):
            app.config['local_name'] = config_manager.server_local_name
        if not app.config.get('save_path'):
            default_save = str(config_manager.save_path) if config_manager.save_path else get_default_save_dir()
            app.config['save_path'] = default_save
        if not app.config.get('port'):
            app.config['port'] = config_manager.server_port
        if app.config.get('clipboard_enabled') is None:
            app.config['clipboard_enabled'] = True

        logger.info(f"使用 ConfigManager 配置 | 保存路径: {app.config['save_path']}")

    # 初始化各个服务
    tracker = ClientTracker()
    file_handler = FileHandler(app.config.get('save_path', get_default_save_dir()))
    latest_file = FileLatestTracker()
    load_clients_ip()

    KEY = app.config.get("key", "")
    LOCAL_NAME = app.config.get("local_name", "Server")
    SAVE_PATH = app.config.get("save_path", get_default_save_dir())
    PORT = app.config.get("port", 8000)
    global CLIPBOARD_ENABLED
    CLIPBOARD_ENABLED = app.config.get("clipboard_enabled", True)

    logger.info(f"API组件初始化完成 | 服务名称: {LOCAL_NAME} | 端口: {PORT}")

def get_api_key():
    return request.headers.get("key", "")


def copy_text_to_clipboard(text):
    if not CLIPBOARD_ENABLED:
        return
    try:
        import pyperclip
    except ImportError as e:
        logging.warning(f"pyperclip 导入失败，跳过剪贴板写入: {e}")
        return

    with clipboard_lock:
        try:
            pyperclip.copy(text)
        except Exception as e:
            logging.warning(f"剪贴板写入失败，跳过: {e}")


# ------------------- 客户端列表 -------------------
clients = []  # 内存中的客户端列表
_lock = threading.Lock()
CLIENT_IP_FILE = BASE_DIR / "config" / "client_ip.json"
def load_clients_ip():
    global clients
    # 如果文件不存在，先创建一个空的 JSON 文件
    if not CLIENT_IP_FILE.exists():
        with open(CLIENT_IP_FILE, "w", encoding="utf-8") as f:
            json.dump([], f)
        clients = []
        return

    try:
        with open(CLIENT_IP_FILE, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        if isinstance(loaded, list):
            clients = loaded
        else:
            logging.warning("客户端 IP 文件格式错误，已重置为列表")
            clients = []
            with open(CLIENT_IP_FILE, "w", encoding="utf-8") as f:
                json.dump(clients, f, indent=2, ensure_ascii=False)
    except (json.JSONDecodeError, FileNotFoundError) as e:
        logging.warning(f"加载客户端 IP 文件失败，已重置为列表: {e}")
        clients = []

def save_clients():
    with _lock:
        now = datetime.now()
        expired = []
        # 先清理过期客户端
        for c in clients[:]:
            try:
                last = datetime.strptime(c["last_seen"], "%Y-%m-%d %H:%M:%S")
            except (ValueError, KeyError):
                # 时间格式错误或缺失字段，视为无效，直接移除
                expired.append(c)
                continue
            if now - last > timedelta(hours=CLIENT_EXPIRE_HOURS):
                expired.append(c)

        for c in expired:
            clients.remove(c)
            logging.info(f"移除过期客户端: {c.get('local_name', '未知')} ({c.get('ip')})")

        # 写入文件
        with open(CLIENT_IP_FILE, "w", encoding="utf-8") as f:
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
        latest_global = latest.copy()
    else:
        latest = latest_file.get_all_files()
        if not latest:
            return  # 如果没有文件记录，直接退出
        latest_global = latest.copy()
        latest_file_item = latest[0]

    for client in clients:
        source = client["local_name"]
        client_ip = client['ip']

        # 推送的客户端是服务器跳过
        if source == LOCAL_NAME:
            continue
        # 同步清理最新文件信息
        if _type == "clear":
            try:
                resp = requests.get(
                    f"http://{client_ip}:{client['port']}/clear/file_latest",
                    headers={
                        "key": KEY
                    },
                    timeout=5
                )
                if resp.status_code == 200:
                    logging.info(f"最新文件清理通知客户端{client['local_name']}成功...")
                else:
                    logging.warning(f"最新文件清理通知客户端{client['local_name']}失败: {resp.status_code} {resp.text}")
            except Exception as e:
                logging.error(f"连接客户端 {client['local_name']} 失败: {e}")
            continue

        #  推送的文件来源是要通知的客户端跳过
        if _type == "text":
            if latest is None or latest.get("source") == source:
                continue
        else:
            if not latest or latest_file_item.get("source") == source:
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
            logging.info("客户端 %s 已获取文件发布通知", client["local_name"])
        # 推送更新到客户端
        update_url = f"http://{client_ip}:{client['port']}/update/current_latest"
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
                    logging.info(f"文件发布推送成功: {latest['name']}...")
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
    content = data.get("content", "")
    if source == LOCAL_NAME and content == tracker.get_latest_global_content():
        return jsonify({"status": "ignored", "message": "忽略自身来源"}), 200

    if not content:
        return jsonify({"status": "error", "message": "内容为空"}), 400

    item = build_text_item(text=content, source=source, pasted=False)

    # 去重：用 tracker 的 is_duplicate 方法
    if tracker.is_duplicate(item["id"]):
        return jsonify({"status": "duplicate", "message": "重复内容"}), 200
    # 更新记录（同时注册 ID、更新客户端最新和全局最新）
    tracker.update(item)
    # 在需要时写入剪贴板（仅 Windows 服务器或启用了剪贴板功能时）
    copy_text_to_clipboard(content)
    load_clients_ip()
    notify_clients("text")

    logging.info("同步文本: %s", content[:50])
    return jsonify({"status": "ok", "message": "文字同步成功"}), 200

@app.route('/sync', methods=['POST'])
def sync():
    data = request.get_json()
    if not data or data.get("key") != KEY:
        return jsonify({"status": "error", "message": "密钥错误"}), 403

    source = data.get("source", "")
    content = data.get("content", "")
    latest_global = { "pasted": False }
    # 获取该客户端当前记录
    client_last = tracker.data.get("clients", {}).get(source)

    # 判断内容是否发生了变化（首次连接也算变化）
    is_new = (not client_last) or (client_last.get("content") != content)

    if is_new and content and source != LOCAL_NAME:
        # 手机有新内容 → 强制更新为自己，并设为全局最新
        item = build_text_item(text=content, source=source, pasted=True)
        copy_text_to_clipboard(content)
        if not tracker.is_duplicate(item["id"]):
            tracker.update(item, force_latest=True)
            load_clients_ip()
            notify_clients("text")
        # latest_global = tracker.get_global_latest()
        latest_global =  { "pasted": True }
    # 获取该客户端当前记录
    else:
        # 内容没变 → 纯拉取操作
        latest = tracker.get_global_latest()
        # 如果全局最新不是自己，则标记该手机已粘贴（更新 clients）
        if source and latest and latest.get("source") != source:
            # 新内容
            if latest.get("content") != content:
                latest_global = latest.copy()
                # 获取当前时间
                now_time_str = datetime.now().isoformat()
                # 转换为 datetime 对象
                now_time = datetime.fromisoformat(now_time_str)
                latest_time = datetime.fromisoformat(latest_global["timestamp"])
                # 计算时间差（返回 timedelta 对象）
                diff = now_time - latest_time
                # 判断是否超过 10 分钟（600 秒）
                if diff < timedelta(minutes=10):
                    # 拉取时未超过 10 分钟才标记为未使用，超过10分钟默认已使用了
                    latest_global["pasted"] = False
                else:
                    latest_global["pasted"] = True
                pasted_item = {
                    "id": latest["id"],
                    "type": latest.get("type", "text"),
                    "content": latest["content"],
                    "timestamp": now_time_str,
                    "source": latest["source"],   # 保留原始来源
                    "pasted": True
                }
                tracker.mark_pasted(source, pasted_item)

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

@app.route('/clients/online', methods=['GET'])
def get_online_clients():
    """获取在线客户端列表"""
    key = get_api_key()
    if key != KEY:
        return jsonify({"status": "error", "message": "密钥错误"}), 403

    load_clients_ip()
    online_clients = []
    upload_url = {}
    for client in clients:
        ip = client.get('ip')
        port = client.get('port', 8899)
        if not ip:
            continue

        ping_url = f"http://{ip}:{port}/ping"
        try:
            resp = requests.get(ping_url, timeout=3)
            if resp.status_code == 200:
                item = f"{client.get('local_name', '未知')} ({ip})"
                online_clients.append(item)
                upload_url[item] = f"http://{ip}:{port}/upload_file"
        except Exception:
            continue

    return jsonify({"status": "ok", "online_clients": online_clients, "upload_url": upload_url}), 200

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
# ---------- 文件同步----------
@app.route('/file_sync', methods=['POST'])
def file_sync():
    """电脑复制文件时调用，告诉服务端最新文件的路径"""
    key = request.headers.get("key", "")
    if key != KEY:
        return jsonify({"status": "error", "message": "密钥错误"}), 403

    client_ip = request.remote_addr
    data = request.get_json()
    file_list = data.get("file_list", [])
    source = data.get("source", "unknown")
    port = data.get("port", 8899)

    if not file_list:
        return jsonify({"status": "error", "message": "file_list 为空"}), 400

    success_count = 0
    errors = []
    # 先清理文件数据
    latest_file.clear()
    for file_info in file_list:
        file_id = file_info.get("file_id")
        path = file_info.get("path")
        name = file_info.get("name")
        size = file_info.get("size", 0)

        if not file_id or not name or not path:
            errors.append(f"{name or '未知文件'} 参数不完整")
            continue

        try:
            latest_file.upsert_file(
                file_id=file_id,
                path=path,
                name=name,
                size=size,
                source=source,
                ip=client_ip,
                port=port
            )
            success_count += 1
            logging.info(f"文件已记录: {name} ({size} bytes), 来源: {source}")
        except Exception as e:
            logging.error(f"记录文件失败: {name}, 错误: {e}")
            errors.append(f"{name}: {str(e)}")

    if success_count > 0:
        load_clients_ip()
        notify_clients("file")

    return jsonify({
        "status": "ok",
        "success_count": success_count,
        "error_count": len(errors),
        "errors": errors
    })

@app.route('/latest/clear', methods=['GET'])
def clear_latest():
    """清理最新文件"""
    latest_file.clear()
    notify_clients("clear")
    return jsonify({"status": "ok"}), 200

@app.route('/request_file', methods=['POST'])
def request_file():
    """统一拉取接口：有文件则返回所有文件列表并清空记录，无文件则返回最新文本"""
    key = request.headers.get("key", "")
    if key != KEY:
        return jsonify({"status": "error", "message": "密钥错误"}), 403

    data = request.get_json()
    if not data:
        return jsonify({"status": "error", "message": "无效请求"}), 400
    source = data.get("source", "unknown")

    # 1. 获取所有待拉取文件
    all_files = latest_file.get_all_files()
    if all_files:
        file_list = []
        for info in all_files:
            file_id = info.get("file_id")
            ip = info.get("ip")
            port = info.get("port")
            name = info.get("name")
            size = info.get("size")
            src = info.get("source")

            # 必须有 ip/port/file_id 才能构建下载地址
            if file_id and ip and port:
                download_url = f"http://{ip}:{port}/file/{file_id}"
                file_list.append({
                    "file_id": file_id,
                    "name": name,
                    "size": size,
                    "source": src,
                    "download_url": download_url
                })
            else:
                logging.warning(f"跳过无效文件记录: {info}")

        # 一次性清空所有记录（文件已交付给请求方）
        latest_file.clear()

        if file_list:
            return jsonify({
                "status": "ok",
                "type": "file_list",
                "files": file_list
            }), 200
        # 如果列表为空（所有记录均无效），继续执行文本逻辑

    # 2. 无文件时，执行原有的文本拉取逻辑
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
        logging.info("客户端 %s 已获取并标记粘贴: %s (来自 %s)",
                    source, latest["content"][:30], latest["source"])

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
    source = unquote(request.headers.get("source", ""))
    msg = f"来源：{source}\n文件：{filename}\n保存：{save_path}"
    show_notification("手机文件已保存", msg)
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
    # 加载客户端列表
    load_clients_ip()
    is_new = add_or_update_client(ip, port, local_name)
    msg = f"客户端 {local_name}({ip}) 已连接。"
    logging.info(msg)
    # 不是本地的客户端才显示注册
    if ip != "127.0.0.1" and local_name != computer_name:
        show_notification("发现客户端", msg)
    return jsonify({
        "status": "ok",
        "is_new": is_new,
        "server_ip": ip
    })

# 内部通知接口
@app.route('/internal/notify_clients', methods=['POST'])
def internal_notify():
    # 验证请求来源是本地
    if request.remote_addr == '127.0.0.1':
        data = request.get_json()
        notify_clients(data['changed_type'])
        return jsonify({"status": "ok"})

# ------------------- 启动函数 -------------------
def start_flask():
    logging.info("Flask 服务启动，监听端口: %s", PORT)
    app.run(host="0.0.0.0", port=PORT, debug=True, use_reloader=False)

# ------------------- 独立运行 -------------------
if __name__ == "__main__":
    print("启动 SyncClipboard Flask 服务...")
    logging.info("脚本直接运行，启动服务")
    start_flask()