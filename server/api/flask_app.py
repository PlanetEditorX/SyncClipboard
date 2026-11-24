import os
import re
import sys
import json
import base64
import socket
import time
import logging
import requests
import threading
import concurrent.futures
from pathlib import Path
from datetime import datetime, timedelta
from urllib.parse import urlparse, parse_qs, unquote
from flask import Flask, request, after_this_request, jsonify, send_file, send_from_directory, url_for

def get_default_save_dir():
    return str(Path.home() / "Downloads")

def secure_save_path(base_dir, user_path):
    """
    Safely joins a base directory with a user-provided path.
    Prevents path traversal vulnerabilities.
    """
    if not user_path:
        return None

    # Handle Windows-style path traversal attempts on non-Windows systems
    user_path = user_path.replace('\\', '/')
    user_path = user_path.lstrip('/')

    base_dir = os.path.abspath(base_dir)
    target_path = os.path.abspath(os.path.join(base_dir, user_path))

    # Ensure the target path is within the base directory
    if not target_path.startswith(base_dir + os.sep):
        # Allow exact match of the base directory if needed, but for files we usually want something inside
        if target_path != base_dir:
            return None
    return target_path

# 统一使用 server 包路径的绝对导入
from server.core.item_builder import build_text_item
from server.core.file_handler import FileHandler
from server.core.text_tracker import TextTracker
from server.core.file_sync import LatestFileManager
from server.core.file_latest import FileLatestTracker
from common.utils import (BASE_DIR, get_default_server_host, isExpired,
                          fix_mojibake_filename, request_key_matches)
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
CLIENT_EXPIRE_HOURS = 168   # 客户端超过1周没有任何活跃证据就删除
CLIENT_TOUCH_INTERVAL = 600  # 同一客户端 10 分钟内最多写一次注册表
ONLINE_CHECK_TIMEOUT = 2    # 推送前的在线探测超时（原先 5 秒，串行时会拖慢复制）
CLIENT_NOTIFY = {} # 客户端通知记录
IOS_NOTIFY = {} # ios的通知文件
CLIENT_TOUCH = {}  # (客户端名, seen|delivery) -> 最近一次写盘的时间戳

# 发送端文件服务端口兜底值。客户端偶尔会把 file_server 的端口传成 null
# （对象还没就绪时），而 dict.get 在「键存在、值为 None」时不会回落到默认值，
# 结果记录落库时 port=None，/request_file 的 `if file_id and ip and port`
# 会把每一条都跳过 —— 文件静默消失、接收端只看到「没有新内容」。
DEFAULT_FILE_PORT = 8899


def normalize_file_port(value):
    """把发送端上报的端口收敛成 1..65535 的整数，非法值一律回落默认端口。

    返回 (端口, 是否合法)。只做 `or DEFAULT_FILE_PORT` 是不够的：`"0"`、`"abc"`、
    `-1`、`65536` 这些值都是 truthy，会原样落库并拼出 `http://ip:0/file/xxx`
    这类不可用地址 —— 接收端拿到的是一个必然失败的 URL，比「没有文件」更难查。
    """
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return DEFAULT_FILE_PORT, False
    try:
        port = int(str(value).strip())
    except (TypeError, ValueError):
        return DEFAULT_FILE_PORT, False
    if not 0 < port < 65536:
        return DEFAULT_FILE_PORT, False
    return port, True


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
            default_save = str(config_manager.save_path) if getattr(config_manager, 'save_path', None) else get_default_save_dir()
            app.config['save_path'] = default_save
        if not app.config.get('save_path'):
            app.config['save_path'] = app.config['save_path']
        if not app.config.get('port'):
            app.config['port'] = config_manager.server_port
        if app.config.get('clipboard_enabled') is None:
            app.config['clipboard_enabled'] = True
        if app.config.get('host') is None:
            app.config['host'] = config_manager.server_host or get_default_server_host()

        logger.info(f"使用 ConfigManager 配置 | 保存路径: {app.config['save_path']} | 端口: {app.config['port']}")

    # 初始化各个服务
    tracker = TextTracker()
    file_handler = FileHandler(app.config.get('save_path', get_default_save_dir()))
    latest_file = FileLatestTracker()
    load_clients_ip()

    KEY = app.config.get("key", "")
    LOCAL_NAME = app.config.get("local_name", "Server")
    SAVE_PATH = app.config.get("save_path", app.config.get('save_path', get_default_save_dir()))
    PORT = app.config.get("port", 8000)
    global CLIPBOARD_ENABLED
    CLIPBOARD_ENABLED = app.config.get("clipboard_enabled", True)

    logger.info(f"API组件初始化完成 | 服务名称: {LOCAL_NAME} | 端口: {PORT}")

def get_api_key():
    return request.headers.get("key", "")


def require_key():
    """全站唯一的鉴权入口，与电脑端 FileServer 共用同一套校验。"""
    return request_key_matches(KEY)


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
# 黑名单：跟「移除」是两回事。移除只是把设备踢出当前派发列表，它下次请求
# 还会自动回来；黑名单是持久的，命中后连自动登记 / 注册都不收，也不推送。
BLACKLIST_FILE = BASE_DIR / "config" / "blacklist.json"
blacklist = {}          # local_name -> 客户端记录快照 + blacklisted_at
BLACKLIST_LOADED = False


def load_blacklist():
    """读黑名单；文件坏了按空表处理，别让服务起不来。"""
    global blacklist, BLACKLIST_LOADED
    try:
        if BLACKLIST_FILE.exists():
            loaded = json.loads(BLACKLIST_FILE.read_text(encoding="utf-8"))
            blacklist = loaded if isinstance(loaded, dict) else {}
        else:
            blacklist = {}
    except (OSError, json.JSONDecodeError) as exc:
        logging.warning("读取黑名单失败，按空表处理: %s", exc)
        blacklist = {}
    BLACKLIST_LOADED = True


def save_blacklist():
    with _lock:
        BLACKLIST_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(BLACKLIST_FILE, "w", encoding="utf-8") as f:
            json.dump(blacklist, f, indent=2, ensure_ascii=False)


def is_blacklisted(name):
    if not BLACKLIST_LOADED:
        load_blacklist()
    return str(name or "").strip() in blacklist


@app.before_request
def reject_blacklisted_source():
    """黑名单设备连同步请求本身都不收，不只是不登记、不推送。

    面板自己的三个接口必须放行：否则一旦加黑，连「解除」都调不动了。
    """
    if request.path in ("/clients", "/clients/delete", "/clients/blacklist"):
        return None
    name = ""
    if request.method in ("GET", "HEAD"):
        name = request.args.get("source", "")
    elif request.is_json:
        data = request.get_json(silent=True) or {}
        name = data.get("source") or data.get("local_name") or ""
    if not name:
        # 文件上传走 header 传来源
        name = request.headers.get("source", "")
    name = str(name or "").strip()
    if name and is_blacklisted(name):
        return jsonify({"status": "error", "message": "设备已在黑名单"}), 403
    return None


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

def _parse_client_time(value):
    """解析注册表里的时间戳；认不出来的返回 None。"""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None

def _client_last_alive(client):
    """
    这台客户端最后一次「还活着」的证据。

    主动请求（last_seen）与成功投递（last_delivery）都算，取较晚的一个。
    投递只在真的送达时才记账（见 touch_clients_delivered），所以已经下线的
    设备不会靠投递记录无限续命 —— 它既没有新请求，也没有成功的投递，
    TTL 一到就被清掉。
    """
    stamps = [
        stamp for stamp in (
            _parse_client_time(client.get("last_seen")),
            _parse_client_time(client.get("last_delivery")),
        ) if stamp is not None
    ]
    return max(stamps) if stamps else None

def _find_client(local_name, ip):
    """按设备身份查找：先认名字，再退回 IP。"""
    for c in clients:
        if c.get("local_name") == local_name:
            return c
    if ip:
        for c in clients:
            if c.get("ip") == ip:
                return c
    return None

def save_clients():
    with _lock:
        now = datetime.now()
        threshold = now - timedelta(hours=CLIENT_EXPIRE_HOURS)
        active_clients = []

        # 清理过期客户端：判定基准是「最后活跃时间」，而不是单纯的 last_seen
        for c in clients:
            last_alive = _client_last_alive(c)
            if last_alive is None or last_alive < threshold:
                logging.info(
                    "移除过期客户端: %s (%s) 最后活跃=%s",
                    c.get("local_name", "未知"),
                    c.get("ip"),
                    last_alive.strftime("%Y-%m-%d %H:%M:%S") if last_alive else "无记录",
                )
            else:
                active_clients.append(c)

        clients[:] = active_clients

        # 写入文件
        with open(CLIENT_IP_FILE, "w", encoding="utf-8") as f:
            json.dump(clients, f, indent=2, ensure_ascii=False)

def add_or_update_client(ip, port, local_name, os):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    # 先按设备名匹配：换 IP 不该变成另一台设备
    client = _find_client(local_name, ip)
    if client is not None:
        client["ip"] = ip
        # 只接受有效的推送端口；自动登记的记录默认是 0，别把已有端口覆盖掉
        if port:
            client["port"] = port
        client["local_name"] = local_name
        client["os"] = os or client.get("os", "Windows")
        client["last_seen"] = now
        save_clients()
        return False  # 已存在，仅更新
    # 新客户端
    clients.append({
        "ip": ip,
        "port": port,
        "local_name": local_name,
        "os": os,
        "first_seen": now,
        "last_seen": now
    })
    save_clients()
    return True

def touch_client(ip, source, os_name=None, *, delivery=False):
    """
    自动登记 / 续期一台客户端。

    任何带 key 且能报出自己名字的请求都可以走到这里 —— 首次见到就登记，
    之后每次请求刷新 last_seen，投递成功则另记 last_delivery。同一客户端
    10 分钟内最多写一次注册表，避免 iOS 那种几秒一次的轮询把文件写爆。
    """
    source = (source or "").strip()
    ip = (ip or "").strip()
    if not source or not ip:
        return False

    # 黑名单设备不能自动登记，也就永远不会再进入派发列表
    if is_blacklisted(source):
        return False

    now = datetime.now()
    throttle_key = (source, "delivery" if delivery else "seen")
    if now.timestamp() - CLIENT_TOUCH.get(throttle_key, 0) < CLIENT_TOUCH_INTERVAL:
        return False
    CLIENT_TOUCH[throttle_key] = now.timestamp()

    stamp = now.strftime("%Y-%m-%d %H:%M:%S")
    load_clients_ip()
    client = _find_client(source, ip)
    is_new = client is None
    if is_new:
        client = {
            "ip": ip,
            "port": 0,
            "local_name": source,
            "os": os_name or "Windows",
            "first_seen": stamp,
            "last_seen": stamp,
        }
        clients.append(client)
    else:
        client["ip"] = ip
        if os_name:
            client["os"] = os_name
    if delivery:
        client["last_delivery"] = stamp
        client.setdefault("last_seen", stamp)
    else:
        client["last_seen"] = stamp

    save_clients()
    if is_new:
        logging.info("自动登记客户端: %s (%s, %s)", source, ip, client.get("os"))
    return True

def touch_clients_delivered(targets):
    """
    批量记录「投递成功」：一次读盘、一次写盘。

    只有真的送到才调用它。送不到（离线、端口不通、iOS 还等着轮询来取）
    就不记账，于是那台设备会按 last_seen 正常过期。
    """
    if not targets:
        return
    now = datetime.now()
    pending = []
    for ip, source, os_name in targets:
        source = (source or "").strip()
        if not source:
            continue
        throttle_key = (source, "delivery")
        if now.timestamp() - CLIENT_TOUCH.get(throttle_key, 0) < CLIENT_TOUCH_INTERVAL:
            continue
        CLIENT_TOUCH[throttle_key] = now.timestamp()
        pending.append((ip, source, os_name))
    if not pending:
        return

    stamp = now.strftime("%Y-%m-%d %H:%M:%S")
    load_clients_ip()
    touched = False
    for ip, source, os_name in pending:
        client = _find_client(source, ip)
        if client is None:
            continue
        if os_name:
            client["os"] = os_name
        client["last_delivery"] = stamp
        client.setdefault("last_seen", stamp)
        touched = True
    if touched:
        save_clients()

# ------------------- 推送统计 -------------------
# 每条统计都写在注册表的同一条记录里，面板只读不改：
#   push_total   服务端为这台设备发起投递的次数（离线、超时也算一次尝试）
#   push_ok      确认送达的次数（主动推送是 HTTP 200；轮询型是它真把内容取走）
#   push_fail    没送达的次数
#   last_push    最近一次投递尝试的时间
#   last_push_ok 最近一次尝试是否送达
#   pull_ok      轮询型客户端（iOS）实际取走内容的次数
#   last_pull    最近一次取走内容的时间
PUSH_STAT_DEFAULTS = {
    "push_total": 0,
    "push_ok": 0,
    "push_fail": 0,
    "pull_ok": 0,
    "last_push": None,
    "last_push_ok": None,
    "last_pull": None,
}


def _stat(client, name):
    """读一个统计字段；老注册表里没有这个键时回落到默认值。"""
    value = client.get(name)
    return PUSH_STAT_DEFAULTS[name] if value is None else value


def record_push_stats(entries):
    """批量记推送统计：entries = [(ip, source, ok, kind)]，kind 取 push / pull。

    只有「内容真的到了设备手上」才算成功：主动推送看 HTTP 200，轮询型看它
    有没有把内容取走（见 _take_ios_latest）。离线、端口不通、iOS 排队还没被
    取走都记成失败 —— 推送率要能回答「这台设备到底有没有收到」，把「我以为
    推了」算成成功，这个数字就没有意义了。

    一次广播只读一次盘、写一次盘：iOS 是秒级轮询，逐条写会把注册表刷爆。
    """
    if not entries:
        return
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    load_clients_ip()
    touched = False
    for ip, source, ok, kind in entries:
        source = (source or "").strip()
        if not source:
            continue
        client = _find_client(source, ip)
        if client is None:
            # 记录刚被 TTL 或面板删掉，不再把它复活 —— 统计没有归属了
            continue
        if kind == "pull":
            client["pull_ok"] = _stat(client, "pull_ok") + 1
            client["last_pull"] = stamp
            client["push_ok"] = _stat(client, "push_ok") + 1
            # 兜底取内容的路径（服务端重启过、设备从表里过期过）没有排队记录，
            # 这里把分母补上，保证成功率不会冒出 120% 这种数字
            client["push_total"] = max(_stat(client, "push_total"), client["push_ok"])
        else:
            client["push_total"] = _stat(client, "push_total") + 1
            client["last_push"] = stamp
            client["last_push_ok"] = bool(ok)
            if ok:
                client["push_ok"] = _stat(client, "push_ok") + 1
            else:
                client["push_fail"] = _stat(client, "push_fail") + 1
        touched = True
    if touched:
        save_clients()


def _client_view(client, online=None, now=None):
    """把注册表里的一条记录整理成面板要的字段。

    时间原样给字符串，相对时间（「3 分钟前」）交给界面算：服务端只保证
    时间是本机时区的 `YYYY-MM-DD HH:MM:SS`，怎么显示是表现层的事。
    """
    now = now or datetime.now()
    last_seen = _parse_client_time(client.get("last_seen"))
    last_delivery = _parse_client_time(client.get("last_delivery"))
    last_alive = _client_last_alive(client)
    push_total = int(_stat(client, "push_total") or 0)
    push_ok = int(_stat(client, "push_ok") or 0)
    return {
        "local_name": str(client.get("local_name") or "未知"),
        "ip": str(client.get("ip") or ""),
        "port": int(client.get("port") or 0),
        "os": str(client.get("os") or "Unknown"),
        "first_seen": client.get("first_seen") or "",
        "last_seen": last_seen.strftime("%Y-%m-%d %H:%M:%S") if last_seen else "",
        "last_delivery": last_delivery.strftime("%Y-%m-%d %H:%M:%S") if last_delivery else "",
        "last_alive": last_alive.strftime("%Y-%m-%d %H:%M:%S") if last_alive else "",
        "last_alive_ts": last_alive.timestamp() if last_alive else 0,
        "idle_seconds": int((now - last_alive).total_seconds()) if last_alive else None,
        "push_total": push_total,
        "push_ok": push_ok,
        "push_fail": int(_stat(client, "push_fail") or 0),
        "push_rate": round(push_ok * 100.0 / push_total, 1) if push_total else None,
        "last_push": client.get("last_push") or "",
        "last_push_ok": client.get("last_push_ok"),
        "pull_ok": int(_stat(client, "pull_ok") or 0),
        "last_pull": client.get("last_pull") or "",
        "online": online,
        "is_server": client.get("local_name") == LOCAL_NAME,
        "blacklisted": is_blacklisted(client.get("local_name")),
    }


# 在线探测结果缓存：面板每隔几秒刷新一次，没必要每次都把整个局域网 ping 一遍
ONLINE_CACHE = {}
ONLINE_CACHE_SECONDS = 5


def _probe_client(ip, port, os_name):
    """静默探测一台设备是否在线；负值表示「这类设备探不出来」。"""
    if str(os_name or "").casefold() in {"ios", "ipados", "iphone", "ipad"}:
        # iOS 快捷指令没有推送端口，只能等它自己来拉，探不出在线与否
        return None
    try:
        port = int(port or 0)
    except (TypeError, ValueError):
        port = 0
    if port <= 0:
        return None
    try:
        resp = requests.get(f"http://{ip}:{port}/ping", timeout=1.2)
        return resp.status_code == 200
    except Exception:  # noqa: BLE001 - 探不到就是在离线/端口不通，都算不在线
        return False


def probe_clients(entries):
    """并发探测一批客户端，返回 {(ip, port): True/False/None}。

    不复用 check_online：那个每次成功/失败都打日志，面板每几秒扫一遍会把
    日志刷满，排查真正的问题时反而找不到东西。
    """
    now = time.time()
    result = {}
    pending = []
    for client in entries:
        ip = client.get("ip")
        port = client.get("port") or 0
        try:
            key = (ip, int(port))
        except (TypeError, ValueError):
            key = (ip, 0)
        cached = ONLINE_CACHE.get(key)
        if cached and now - cached[0] < ONLINE_CACHE_SECONDS:
            result[key] = cached[1]
            continue
        # 轮询型 / 没有推送端口：探不出来，先记「未知」，别浪费一次 ping
        if str(client.get("os") or "").casefold() in {"ios", "ipados", "iphone", "ipad"}:
            result[key] = None
            continue
        try:
            if int(port) <= 0:
                result[key] = None
                continue
        except (TypeError, ValueError):
            result[key] = None
            continue
        pending.append((key, ip, port, client.get("os")))

    if pending:
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(8, len(pending))
        ) as pool:
            futures = {
                pool.submit(_probe_client, ip, port, os_name): key
                for key, ip, port, os_name in pending
            }
            for future, key in futures.items():
                try:
                    value = future.result()
                except Exception:  # noqa: BLE001 - 单台探测异常不该拖垮整张表
                    value = False
                result[key] = value
                if value is not None:
                    ONLINE_CACHE[key] = (time.time(), value)
    return result


def check_online(ip, port, os, source):
    """检测客户端是否在线（也就是：能不能主动推给它）"""
    try:
        port = int(port or 0)
    except (TypeError, ValueError):
        port = 0
    # iOS 快捷指令没有推送端口，只能等它自己来拉
    if os == "iOS":
        return True
    if port <= 0:
        # 还没上报推送端口的设备（比如只被自动登记过）无从推送，
        # 等它自己 register 带上 file_server_port 之后才会走主动推送
        logging.info("客户端 %s(%s) 未上报推送端口，跳过主动推送", source, ip)
        return False
    check_url = f"http://{ip}:{port}/ping"
    try:
        resp = requests.get(check_url, timeout=ONLINE_CHECK_TIMEOUT)
        if resp.status_code != 200:
            logging.warning(f"客户端 {source}({ip}) 状态异常: {resp.status_code}")
            return False
        logging.info(f"客户端 {source}({ip}) 在线")
        return True
    except Exception:
        logging.warning(f"客户端 {source}({ip}) 离线")
        return False

def push_notify(_type, latest, ip, port, os, source, latest_global):
    """
    向一台客户端投递一次更新，返回「是否真的送到了」。

    返回值决定要不要给这台设备记 last_delivery（见 touch_clients_delivered）：
    只有真送到才算它还活着，送不到就让它按 last_seen 正常过期。

    iOS 例外：服务端这里只是把内容挂到内存字典等它来拉，投递尚未完成，
    因此返回 False —— 它拉走时才会刷新自己的活跃时间。
    """
    global IOS_NOTIFY
    if os == "iOS":
        if _type == "text":
            pasted_item = {
                "id": latest["id"],
                "type": latest.get("type", "text"),
                "content": latest["content"],
                "timestamp": datetime.now().isoformat(),
                "source": latest["source"],
                "pasted": False
            }
            IOS_NOTIFY[source] = pasted_item

            logging.info(
                "iOS客户端 %s 通知数据已准备: %s (来自 %s)",
                source,
                str(latest["content"])[:30],
                latest["source"]
            )
        return False

    # 推送更新到客户端
    update_url = f"http://{ip}:{port}/update/current_latest"
    try:
        resp = requests.post(
            update_url,
            json={
                "key": KEY,
                "latest_global": latest_global,
                "server_source": LOCAL_NAME,
                "type": _type
            },
            # 统一契约：key 放请求头。电脑端 / Android 的入站路由都按请求头校验。
            headers={"key": KEY},
            timeout=5
        )
    except Exception as e:
        logging.error(f"连接客户端 {source} 失败: {e}")
        return False

    if resp.status_code != 200:
        logging.warning(f"推送失败: {resp.status_code} {resp.text}")
        return False

    # 只有确认送达，才把它标记为已获取；否则下次还要再推一次
    if _type == "text":
        tracker.mark_pasted(source, {
            "id": latest["id"],
            "type": latest.get("type", "text"),
            "content": latest["content"],
            "timestamp": datetime.now().isoformat(),
            "source": latest["source"],
            "pasted": True
        })
        logging.info(
            "客户端 %s 已获取并标记粘贴: %s (来自 %s)",
            source,
            str(latest["content"])[:30],
            latest["source"]
        )
        logging.info("文字推送成功: %s...", str(latest["content"])[:50])
    else:
        try:
            if isinstance(latest, list) and latest:
                name = latest[0].get('name') if isinstance(latest[0], dict) else str(latest[0])
            elif isinstance(latest, dict):
                name = latest.get('name', '<unknown>')
            else:
                name = '<unknown>'
        except Exception:
            name = '<unknown>'
        logging.info(f"客户端 {source} 已获取文件发布通知")
        logging.info(f"文件发布推送成功: {name}...")
    return True

def get_timestamp(data):
    """返回时间戳"""
    item = None
    if isinstance(data, dict):
        item = data
    elif isinstance(data, list) and data and isinstance(data[0], dict):
        item = data[0]

    if item:
        if item.get("timestamp"):
            return item["timestamp"]
        if item.get("updated_at") is not None:
            try:
                return datetime.fromtimestamp(float(item["updated_at"])).isoformat()
            except (TypeError, ValueError, OSError):
                pass

    return datetime.now().isoformat()

def _clear_client_task(client, key):
    try:
        resp = requests.get(
            f"http://{client['ip']}:{client['port']}/clear/file_latest",
            headers={
                "key": key
            },
            timeout=5
        )
        if resp.status_code == 200:
            logging.info(f"最新文件清理通知客户端{client['local_name']}成功...")
        else:
            logging.warning(f"最新文件清理通知客户端{client['local_name']}失败: {resp.status_code} {resp.text}")
    except Exception as e:
        logging.error(f"连接客户端 {client['local_name']} 失败: {e}")

def notify_clients(_type):
    """通知客户端"""
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

    # 第一遍：筛出这次真正需要投递的客户端
    candidates = []
    for client in clients:
        source = client["local_name"]
        client_ip = client['ip']

        # 推送的客户端是服务器跳过
        if source == LOCAL_NAME:
            continue
        # 黑名单即使还残留在旧记录里也绝不投递（加黑时会顺手清记录，
        # 这里是双保险：服务端重启带旧注册表也不会误发）
        if is_blacklisted(source):
            continue
        # 同步清理最新文件信息
        if _type == "clear":
            t = threading.Thread(target=_clear_client_task, args=(client, KEY), daemon=True)
            t.start()
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
            # 兜底：该设备手上已经是同一份内容（哪怕事件 id 不同）就不再推送。
            # 回音造成的“来源漂移”以前会把同一段文字反复推给同一台设备，
            # 表现就是电脑一轮一轮弹“剪贴板更新”。
            if client_last and client_last.get("content") == latest.get("content"):
                already_pasted = True
        else:
            # 文件默认未使用，已粘贴的文件会自动清理数据
            already_pasted = False
            latest_global = latest.copy()

        if already_pasted:
            continue

        if isinstance(latest, list) and latest:
            file_id = latest[0].get('file_id') if isinstance(latest[0], dict) else str(latest[0])
        else:
            file_id = latest.get('id') if isinstance(latest, dict) else None
        if CLIENT_NOTIFY.get(client_ip, '') == file_id:
            continue

        candidates.append((client, source, client_ip, file_id))

    if not candidates:
        return

    # 并发探测在线状态：串行 5 秒超时会让一次复制卡住十几秒
    online = {}
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=min(8, len(candidates))
    ) as pool:
        futures = {
            pool.submit(check_online, c['ip'], c['port'], c['os'], s): c['ip']
            for c, s, _ip, _fid in candidates
        }
        for future, client_ip in futures.items():
            try:
                online[client_ip] = future.result()
            except Exception as e:  # noqa: BLE001
                logging.warning(f"在线探测异常 {client_ip}: {e}")
                online[client_ip] = False

    delivered = []
    # 推送统计：这一轮对每台设备「试图投递」和「是否送达」都要留账（见
    # record_push_stats）。在线探测失败也算一次尝试 —— 它回答了「这台设备
    # 错过了多少次同步」，只统计真正发出 HTTP 的话，长期离线设备反而一片空白。
    stats = []
    for client, source, client_ip, file_id in candidates:
        try:
            if not online.get(client_ip):
                stats.append((client_ip, source, False, "push"))
                continue
            # 未超时10分钟才执行推送
            timestamp = get_timestamp(latest) if latest else get_timestamp(latest_global)
            if isExpired(timestamp):
                # 内容已经过期，本来就不该投递，不算一次尝试
                continue
            # 执行推送；只有真送到才记账，送不到下次还会重试
            if push_notify(
                _type, latest, client_ip, client['port'], client['os'], source, latest_global
            ):
                CLIENT_NOTIFY[client_ip] = file_id
                stats.append((client_ip, source, True, "push"))
                # iOS 只是把内容挂到内存字典，等它自己来拉，不算送达
                if client.get('os') != "iOS":
                    delivered.append((client_ip, source, client.get('os')))
            else:
                CLIENT_NOTIFY.pop(client_ip, None)
                # iOS 走的是「排队等它拉」，这一步还没到设备手上；它真拉走时
                # 由 _take_ios_latest 补记成功，所以这里先算没送到
                stats.append((client_ip, source, False, "push"))
        except Exception as e:
            logging.error(f"通知客户端 {client['local_name']} 失败: {e}")
            stats.append((client_ip, source, False, "push"))

    record_push_stats(stats)

    # 批量续期：投递成功 = 这台设备还在线，别按 last_seen 把它清掉
    touch_clients_delivered(delivered)

def _is_echo_submission(content, source):
    """
    溯源判断（主动推送型客户端，例如 Windows 客户端）：这次 /text_sync 提交是
    “用户手动复制”还是“客户端把收到的东西回传”？

    这类客户端自己能区分手动复制（剪贴板序列号），所以这里只兜一个确定性的底：
    全局最新已经是同一份内容时，任何设备再发一遍都没有广播价值，直接忽略。
    全局最新不是它（比如 A → B 之后用户又复制了 A），照常广播。
    """
    if not content:
        return False
    latest = tracker.get_global_latest()
    return bool(latest and latest.get("content") == content)


def _is_poll_echo(content, source):
    """
    溯源判断（轮询型客户端，例如 iOS 快捷指令 / Android）：它们每隔一两秒把
    本地剪贴板整块 POST 上来，本身无法区分“手动复制”和“回传”，因此两个信号
    任一命中就按回音处理：

    1. 内容就是全局最新 —— 已经广播过了，没有再次广播的价值；
    2. 内容最近刚投递给这台设备 —— 它只是把收到的东西原样发回来。

    用“投递记录”而不是“一次性票据”：轮询每 2 秒一发，票据会被反复回传瞬间
    消耗完，之后同一段文字的每次轮询都会被当成新内容，来源被改成手机、
    电脑端于是反复收到“新内容”弹窗。
    """
    if not content or source == LOCAL_NAME:
        return False
    latest = tracker.get_global_latest()
    if latest is not None and latest.get("content") == content:
        return True
    return tracker.is_echo_of_delivered(content, source)


def _handle_echo(source, content):
    """忽略回音时要做的记账：让该设备知道“这份内容我已经有了”。"""
    if not source or content is None:
        return
    tracker.note_client_hold(source, content)


def _take_ios_latest(source, ip=None):
    """
    给轮询型 iOS 客户端取一份待投递内容，没有就返回空。

    不能只依赖内存里的 IOS_NOTIFY：那是 notify_clients 主动推送时填的，
    而 notify_clients 只遍历 clients 表。这台手机一旦从表里过期消失（见
    touch_client），就再也不会有人给它塞数据，接口会一直返回
    {"pasted": true} 空对象 —— 表现就是「手机拉不到电脑刚复制的内容」。

    所以分两级取：
    1. IOS_NOTIFY 里有主动推送的记录 → 直接给，并记为已投递；
    2. 否则看全局最新：不是这台设备自己发的、且它手上还不是这份内容 → 给；
    3. 都没有 → 空（pasted: true），让快捷指令什么都不做。

    ip 只用于推送统计：轮询型客户端的「送达」= 它真的把内容取走了，记在
    这里才算数（notify_clients 排队时先记了一次未送达）。
    """
    if not source:
        return {"pasted": True}

    # 黑名单设备连兜底取内容都不给：拉黑就要连轮询一起断掉
    if is_blacklisted(source):
        return {"pasted": True}

    if source in IOS_NOTIFY:
        pending = IOS_NOTIFY.pop(source)
        latest_global = dict(pending)
        latest_global["pasted"] = False
        tracker.mark_pasted(source, dict(pending, pasted=True))
        record_push_stats([(ip, source, True, "pull")])
        return latest_global

    latest = tracker.get_global_latest()
    if not latest or latest.get("source") == source:
        return {"pasted": True}

    client_last = tracker.data.get("clients", {}).get(source) or {}
    if client_last.get("content") == latest.get("content"):
        return {"pasted": True}

    latest_global = dict(latest)
    latest_global["pasted"] = False
    tracker.mark_pasted(source, dict(latest, pasted=True))
    record_push_stats([(ip, source, True, "pull")])
    return latest_global


# ------------------- 文字同步接口 -------------------
@app.route('/text_sync', methods=['POST'])
def text_sync():
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"status": "error", "message": "无效的请求数据"}), 400

    if not require_key():
        return jsonify({"status": "error", "message": "密钥错误"}), 403

    source = data.get("source", "")
    content = data.get("content", "")
    encode = data.get("encode", "")
    # 首次见到的设备自动登记，之后靠请求续期；不再需要单独手动注册
    touch_client(request.remote_addr, source, get_os_type(request.headers.get("User-Agent", "")))
    if encode == "base64":
        content = base64.b64decode(content).decode('utf-8')

    if not content:
        return jsonify({"status": "error", "message": "内容为空"}), 400

    # 任意客户端把已广播过的内容原样发回，都属于同步回音；不再改变来源或重新广播。
    if _is_echo_submission(content, source):
        _handle_echo(source, content)
        return jsonify({"status": "ignored", "message": "忽略重复回传内容"}), 200

    item = build_text_item(text=content, source=source, pasted=False)

    # 去重：仅拦截完全相同的推送事件（事件 ID 唯一，正常复制不会被拦）
    if tracker.is_duplicate(item["id"]):
        return jsonify({"status": "duplicate", "message": "重复内容"}), 200
    # 更新记录（同时注册 ID、更新客户端最新和全局最新）
    # 用 force_latest：能走到这里说明已经确认是“用户手动复制”的新内容，
    # 以到达顺序为准，避免客户端时钟偏差导致新内容顶不上来（历史上同样表现为“手机拿不到新文字”）。
    tracker.update(item, force_latest=True)
    # 在需要时写入剪贴板（仅 Windows 服务器或启用了剪贴板功能时）
    copy_text_to_clipboard(content)
    load_clients_ip()
    notify_clients("text")

    logging.info("同步文本: %s", content[:50])
    return jsonify({"status": "ok", "message": "文字同步成功"}), 200

@app.route('/sync', methods=['POST'])
def sync():
    data = request.get_json(silent=True) or {}
    if not require_key():
        return jsonify({"status": "error", "message": "密钥错误"}), 403

    source = data.get("source", "")
    content = data.get("content", "")
    latest_global = { "pasted": False }
    touch_client(request.remote_addr, source, get_os_type(request.headers.get("User-Agent", "")))
    # 获取该客户端当前记录
    client_last = tracker.data.get("clients", {}).get(source)

    # 判断内容是否发生了变化（首次连接也算变化）
    is_new = (not client_last) or (client_last.get("content") != content)

    # 溯源：这次“新内容”其实是别的设备刚广播过来、被本机剪贴板原样回传吗？
    is_echo = bool(is_new and content) and _is_poll_echo(content, source)
    if is_echo:
        logging.info("忽略客户端 %s 的回音内容: %s", source, str(content)[:30])
        _handle_echo(source, content)

    if is_new and content and source != LOCAL_NAME and not is_echo:
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
                latest_global["pasted"]  = isExpired(latest_global["timestamp"])
                pasted_item = {
                    "id": latest["id"],
                    "type": latest.get("type", "text"),
                    "content": latest["content"],
                    "timestamp": datetime.now().isoformat(),
                    "source": latest["source"],   # 保留原始来源
                    "pasted": True
                }
                tracker.mark_pasted(source, pasted_item)

    return jsonify({"status": "ok", "latest_global": latest_global})

@app.route('/latest', methods=['GET'])
def get_latest():
    if not require_key():
        return jsonify({"status": "error", "message": "密钥错误"}), 403

    # 获取请求客户端的名称（用于自动标记粘贴）
    source = request.args.get("source", "")
    touch_client(request.remote_addr, source, get_os_type(request.headers.get("User-Agent", "")))
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
                "pasted": True
            }

    return jsonify({"status": "ok", "latest_global": latest_global}), 200

# ------------------- ios接口 -------------------
@app.route('/ios/sync', methods=['POST'])
def iosSync():
    data = request.get_json(silent=True) or {}
    # 统一走 require_key()：请求头 key 是唯一契约，body/query 由它兜底。
    if not require_key():
        return jsonify({"status": "error", "message": "密钥错误"}), 403

    source = data.get("source", "")
    content = data.get("content", "")
    os = data.get("os", "iOS")
    latest_global = { "pasted": True, "content": content }
    touch_client(request.remote_addr, source, "iOS")
    # 获取该客户端当前记录
    client_last = tracker.data.get("clients", {}).get(source)

    # 判断内容是否发生了变化（首次连接也算变化）
    is_new = (not client_last) or (client_last.get("content") != content)

    # 溯源：过滤“收到后原样回传”的回音，但保留用户手动复制的新内容
    is_echo = bool(is_new and content) and _is_poll_echo(content, source)
    if is_echo:
        logging.info("忽略 iOS 客户端 %s 的回音内容: %s", source, str(content)[:30])
        _handle_echo(source, content)

    if is_new and content and source != LOCAL_NAME and not is_echo:
        # 手机有新内容 → 强制更新为自己，并设为全局最新
        item = build_text_item(text=content, source=source, pasted=True)
        copy_text_to_clipboard(content)
        if not tracker.is_duplicate(item["id"]):
            tracker.update(item, force_latest=True)
            load_clients_ip()
            notify_clients("text")
        latest_global =  item
    # 获取该客户端当前记录
    else:
        # 内容没变 → 纯拉取操作
        latest_global = _take_ios_latest(source, request.remote_addr)
    return jsonify({"status": "ok", "latest_global": latest_global})

@app.route('/ios/latest', methods=['GET'])
def ios_get_latest():
    if not require_key():
        return jsonify({"status": "error", "message": "密钥错误"}), 403

    # 获取请求客户端的名称（用于自动标记粘贴）
    source = request.args.get("source", "")
    touch_client(request.remote_addr, source, "iOS")
    latest_global = _take_ios_latest(source, request.remote_addr)

    return jsonify({"status": "ok", "latest_global": latest_global}), 200
# --------------------------------------------

@app.route('/clients/online', methods=['GET'])
def get_online_clients():
    """获取可直接接收文件的在线客户端。"""
    if not require_key():
        return jsonify({
            "status": "error",
            "message": "密钥错误"
        }), 403

    # Android 可以传这两个参数隐藏自身；
    # iOS 快捷指令不传也可以正常请求。
    requester_name = request.args.get("source", "").strip()
    requester_port = request.args.get("source_port", type=int)
    requester_ip = request.remote_addr

    # 来问「谁在线」的设备本身也是客户端，顺手登记
    touch_client(requester_ip, requester_name, get_os_type(request.headers.get("User-Agent", "")))

    load_clients_ip()

    client_items = []
    online_clients = []
    upload_url = {}

    server_host = app.config.get("host", "127.0.0.1")
    server_port = app.config.get("port", 8000)

    # 1. 过滤需要 ping 的客户端
    clients_to_ping = []
    for client in clients:
        ip = client.get("ip")
        port = int(client.get("port", 0) or 0)
        local_name = str(client.get("local_name", "未知")).strip()
        os_name = str(client.get("os", "Windows")).strip()
        os_normalized = os_name.casefold()

        if not ip or port <= 0:
            continue

        # iOS/iPadOS 只能主动轮询，不能作为文件直传目标。
        if os_normalized in {
            "ios",
            "ipados",
            "iphone",
            "ipad"
        }:
            continue

        # Android 请求时隐藏自己。
        # iOS 不传 source/source_port 时，不执行这部分过滤。
        is_same_name = (
            bool(requester_name)
            and local_name.casefold() == requester_name.casefold()
        )

        is_same_endpoint = (
            requester_port is not None
            and ip == requester_ip
            and port == requester_port
        )

        if is_same_name or is_same_endpoint:
            continue

        clients_to_ping.append({
            "ip": ip,
            "port": port,
            "local_name": local_name,
            "os_name": os_name,
            "os_normalized": os_normalized
        })

    # 2. 并发 ping 检查在线状态
    def check_ping(client_info):
        ping_url = f"http://{client_info['ip']}:{client_info['port']}/ping"
        try:
            resp = requests.get(
                ping_url,
                headers={"key": KEY},
                timeout=3
            )
            if resp.status_code == 200:
                return client_info
        except requests.RequestException:
            pass
        return None

    online_clients_info = []
    if clients_to_ping:
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            results = executor.map(check_ping, clients_to_ping)
            for result in results:
                if result:
                    online_clients_info.append(result)

    # 3. 组装结果
    for client_info in online_clients_info:
        ip = client_info["ip"]
        port = client_info["port"]
        local_name = client_info["local_name"]
        os_name = client_info["os_name"]
        os_normalized = client_info["os_normalized"]

        display_name = f"{local_name} ({ip})"

        if os_normalized == "android":
            target_upload_url = (
                f"http://{server_host}:{server_port}"
                f"/upload_file_to_download"
                f"?redirect=http://{ip}:{port}/upload_file"
            )
        else:
            target_upload_url = f"http://{ip}:{port}/upload_file"

        # Android 新版使用。
        client_items.append({
            "name": local_name,
            "display_name": display_name,
            "ip": ip,
            "port": port,
            "os": os_name,
            "upload_url": target_upload_url
        })

        # iOS 快捷指令直接使用。
        online_clients.append(display_name)
        upload_url[display_name] = target_upload_url

    return jsonify({
        "status": "ok",

        # Android 使用结构化数组。
        "clients": client_items,

        # iOS 快捷指令直接使用。
        "online_clients": online_clients,
        "upload_url": upload_url
    }), 200

@app.route('/clients', methods=['GET'])
def list_clients():
    """客户端名单：谁登记过、推过多少次、成了几次、最近什么时候还在。

    和 /clients/online 的区别是「列名单」和「找投递目标」：那个只返回
    *现在能收文件的* 设备（要求端口可 ping 通），而面板要看到全部记录，
    包括没上报端口的、只能轮询的 iOS、以及暂时离线的。

    `probe=1` 时顺带并发 ping 一遍看谁在线（结果缓存 5 秒）。不带就纯读
    注册表，零网络耗时 —— 面板刷新时可以按需要便宜或准确。
    """
    if not require_key():
        return jsonify({"status": "error", "message": "密钥错误"}), 403

    load_clients_ip()
    probe = str(request.args.get("probe", "")).lower() in ("1", "true", "yes", "on")
    online_map = probe_clients(clients) if probe else {}

    now = datetime.now()
    rows = []
    for client in clients:
        try:
            key = (client.get("ip"), int(client.get("port") or 0))
        except (TypeError, ValueError):
            key = (client.get("ip"), 0)
        rows.append(_client_view(client, online=online_map.get(key), now=now))

    # 黑名单设备已经从派发列表清掉了，但面板得看得见它才能解除。用加黑时
    # 存的快照补一行，标 blacklisted / 不显示活跃时间（它不会再连上来了）。
    for name, snapshot in blacklist.items():
        item = {k: v for k, v in snapshot.items() if k != "blacklisted_at"}
        item.setdefault("local_name", name)
        row = _client_view(item, online=None, now=now)
        row["last_alive"] = ""
        row["idle_seconds"] = None
        rows.append(row)

    # 在线的排前面，其余按最近活跃倒序 —— 面板上最先要看的永远是「正在用的」
    rows.sort(key=lambda row: (row.get("online") is not True, -row.get("last_alive_ts", 0)))

    push_total = sum(row["push_total"] for row in rows)
    push_ok = sum(row["push_ok"] for row in rows)
    return jsonify({
        "status": "ok",
        "server_name": LOCAL_NAME,
        "probed": probe,
        "clients": rows,
        "summary": {
            "total": len(rows),
            "online": sum(1 for row in rows if row.get("online") is True),
            "offline": sum(1 for row in rows if row.get("online") is False),
            "unknown": sum(1 for row in rows if row.get("online") is None),
            "blacklisted": len(blacklist),
            "push_total": push_total,
            "push_ok": push_ok,
            "push_rate": round(push_ok * 100.0 / push_total, 1) if push_total else None,
        },
    }), 200


@app.route('/clients/delete', methods=['POST'])
def delete_client():
    """把一台（或全部）客户端从名单里移除，连同它的推送统计。

    这只是从派发列表删掉记录，不是黑名单：设备只要还在跑，下一次请求就会
    重新登记并回到派发列表，统计从 0 开始。要真正拦住一台设备，用单独的
    黑名单接口（见 /clients/blacklist）。

    请求体：{"local_name": "..."} 或 {"ip": "..."}，或 {"all": true} 清空。
    """
    if not require_key():
        return jsonify({"status": "error", "message": "密钥错误"}), 403

    data = request.get_json(silent=True) or {}
    name = str(data.get("local_name") or "").strip()
    ip = str(data.get("ip") or "").strip()
    remove_all = bool(data.get("all"))
    if not remove_all and not name and not ip:
        return jsonify({"status": "error", "message": "缺少 local_name / ip"}), 400

    load_clients_ip()
    removed = []
    kept = []
    for client in clients:
        hit = remove_all
        if not hit and name and client.get("local_name") == name:
            hit = True
        # 没给名字才退回按 IP 删，避免「同名多 IP」时误伤
        if not hit and not name and ip and client.get("ip") == ip:
            hit = True
        (removed if hit else kept).append(client)

    if removed:
        clients[:] = kept
        for client in removed:
            client_ip = client.get("ip")
            client_name = str(client.get("local_name") or "").strip()
            CLIENT_NOTIFY.pop(client_ip, None)
            IOS_NOTIFY.pop(client_name, None)
            for key in [key for key in CLIENT_TOUCH if key and key[0] == client_name]:
                CLIENT_TOUCH.pop(key, None)
            logging.info("从客户端管理移除: %s (%s)", client_name, client_ip)
        save_clients()

    return jsonify({
        "status": "ok",
        "removed": len(removed),
        "names": [c.get("local_name") for c in removed],
    }), 200


@app.route('/clients/blacklist', methods=['POST'])
def set_client_blacklist():
    """把设备加入 / 移出黑名单。

    「移除」只清当前派发列表，设备下次请求会自动回来；黑名单是持久的：
    命中后 touch_client 不自动登记、/register 直接拒绝、派发和 iOS 轮询
    兜底取内容也一律不给。只有从这个接口解除才恢复。

    请求体：{"local_name": "...", "block": true/false}（也支持只给 ip）；
    block 省略时按 true 处理。
    """
    if not require_key():
        return jsonify({"status": "error", "message": "密钥错误"}), 403

    data = request.get_json(silent=True) or {}
    name = str(data.get("local_name") or "").strip()
    ip = str(data.get("ip") or "").strip()
    block = bool(data.get("block", True))

    load_clients_ip()
    load_blacklist()
    if not name and ip:
        for client in clients:
            if client.get("ip") == ip:
                name = str(client.get("local_name") or "").strip()
                break
        if not name:
            for saved_name, snapshot in blacklist.items():
                if snapshot.get("ip") == ip:
                    name = saved_name
                    break
    if not name:
        return jsonify({"status": "error", "message": "缺少 local_name / ip"}), 400

    if block:
        # 把整条记录存成快照：面板上要继续显示这台设备、它的统计和 IP，
        # 还得能从快照里按 IP 找到它。加黑即摘出派发列表，立刻不再推送。
        hit = [c for c in clients if c.get("local_name") == name]
        snapshot = dict(hit[0]) if hit else dict(blacklist.get(name, {}))
        snapshot["local_name"] = name
        snapshot["blacklisted_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        blacklist[name] = snapshot
        if hit:
            clients[:] = [c for c in clients if c.get("local_name") != name]
            for client in hit:
                CLIENT_NOTIFY.pop(client.get("ip"), None)
                IOS_NOTIFY.pop(name, None)
                for key in [key for key in CLIENT_TOUCH if key and key[0] == name]:
                    CLIENT_TOUCH.pop(key, None)
            save_clients()
        save_blacklist()
        logging.info("加入黑名单: %s (%s)", name, snapshot.get("ip"))
    elif name in blacklist:
        blacklist.pop(name, None)
        save_blacklist()
        logging.info("解除黑名单: %s", name)

    return jsonify({"status": "ok", "blocked": block, "name": name}), 200


@app.route('/mark_pasted', methods=['POST'])
def mark_pasted():
    data = request.get_json(silent=True) or {}
    if not require_key():
        return jsonify({"status": "error", "message": "密钥错误"}), 403

    source = data.get("source")          # 客户端名称，如 "PC-01"
    item_id = data.get("id")
    content = data.get("content")
    original_source = data.get("original_source")  # 内容的原始来源，如 "Xun’s iPhone"

    touch_client(request.remote_addr, source, get_os_type(request.headers.get("User-Agent", "")))

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
    if not require_key():
        return jsonify({"status": "error", "message": "密钥错误"}), 403

    client_ip = request.remote_addr
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"status": "error", "message": "无效请求"}), 400
    file_list = data.get("file_list") or []
    if not isinstance(file_list, list):
        # 直接遍历一个 dict 会拿 key 当条目，`.get` 当场 AttributeError → 500，
        # 发送端只看到「服务器错误」，不知道是自己把数组写成了对象。
        return jsonify({"status": "error", "message": "file_list 必须是数组"}), 400
    source = data.get("source") or "unknown"
    port, port_ok = normalize_file_port(data.get("port"))
    if not port_ok:
        logging.warning("发送端未提供有效端口(%r)，按默认 %s 记录（否则下载地址无法拼出）",
                        data.get("port"), DEFAULT_FILE_PORT)
    if source and source != "unknown":
        touch_client(
            request.remote_addr,
            source,
            get_os_type(request.headers.get("User-Agent", "")),
        )

    if not file_list:
        return jsonify({"status": "error", "message": "file_list 为空"}), 400

    success_count = 0
    errors = []
    # 先清理文件数据
    latest_file.clear()
    for file_info in file_list:
        if not isinstance(file_info, dict):
            errors.append(f"无效记录: {str(file_info)[:40]}")
            continue
        file_id = file_info.get("file_id")
        path = file_info.get("path")
        name = file_info.get("name")
        size = file_info.get("size", 0)
        kind = file_info.get("kind", "file")

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
                port=port,
                save=False,
                kind=kind
            )
            success_count += 1
            logging.info(f"文件已记录: {name} ({size} bytes), 来源: {source}")
        except Exception as e:
            logging.error(f"记录文件失败: {name}, 错误: {e}")
            errors.append(f"{name}: {str(e)}")

    if success_count > 0:
        latest_file._save()
        load_clients_ip()
        notify_clients("file")

    return jsonify({
        "status": "ok",
        "success_count": success_count,
        "error_count": len(errors),
        "errors": errors
    })

@app.route('/latest/clear', methods=['GET', 'POST'])
def clear_latest():
    """清理最新文件（需鉴权）。

    旧实现没有任何校验：局域网里任何人 GET 一下就能清空共享记录并向所有
    客户端广播 clear。调用方 gui/file_handler.py::_clear_latest_file 已在
    请求头带 key。
    """
    if not require_key():
        return jsonify({"status": "error", "message": "密钥错误"}), 403
    latest_file.clear()
    notify_clients("clear")
    return jsonify({"status": "ok"}), 200

def _same_ios_log_run(path, text):
    """这次推来的日志，跟磁盘上那份是不是同一次运行。

    快捷指令在每轮结束时把**累积的完整日志**整份推上来，所以同一次运行里后一次
    的内容一定以前一次那份为前缀（只追加、不删改）。是前缀就说明还是同一次运行，
    直接覆盖；不是就说明开了新的一次，旧的该留档了。
    """
    try:
        with open(path, encoding="utf-8") as handle:
            old = handle.read(4096)
    except OSError:
        return False
    if not old.strip():
        return False
    return text.startswith(old)


@app.route('/ios_log', methods=['POST'])
def ios_log():
    """接收 iOS 快捷指令的运行日志，整份覆盖写进 `log/ios_shortcut.log`。

    为什么不让手机自己存就行：iOS 的「存储到文件」保存纯文本时可能**静默
    不落盘**（v7 真机现象：调试日志一条都没出现，通知里也什么都没说）。而
    HTTP 通道在 /request_file 上已经跑通了无数次，日志直接落到电脑上，排查
    时不用再让用户回传文件。

    每次收到的是**完整日志**（快捷指令侧累积后整份发来），所以这里用覆盖写
    而不是追加：一次运行对应一个文件，重复请求只是在刷新同一份内容。
    """
    if not require_key():
        return jsonify({"status": "error", "message": "密钥错误"}), 403
    # 客户端要是把日志放 JSON 里就取 JSON；iOS 的「获取 URL 内容」在参数组合
    # 稍有出入时会改发原始正文（v8 真机：连续 POST /ios_log 全是 400），那种
    # 情况下正文本身就是日志，照收不误 —— 少一次「日志没上来」的来回。
    raw = request.get_data(as_text=True) or ""
    data = request.get_json(silent=True)
    if isinstance(data, dict):
        # 显式给了字段就以字段为准（哪怕是空的，那是客户端的问题，要报错）
        text = data.get("log")
        if text is None:
            text = data.get("text")
    else:
        text = raw
        if raw.strip():
            logging.warning("iOS 日志请求不是 JSON，按原始正文写入（content-type=%s）",
                            request.content_type)
    if not isinstance(text, str) or not text.strip():
        # 不返回 4xx：iOS 的「获取 URL 内容」在非 2xx 时会**中断整条快捷指令**，
        # 日志是个附属通道，它自己出问题绝不该把同步带崩（v10 真机：循环里推了
        # 一个空 body，服务端 400，指令当场停在半路）。空日志就跳过，200 回去。
        logging.warning("iOS 日志请求没有可写内容 | content-type=%s | body=%r",
                        request.content_type, raw[:300])
        return jsonify({"status": "skipped", "message": "空日志"}), 200
    # 日志是排查用的，宁可留半份也不要把服务打挂
    if len(text) > 2000000:
        text = text[:2000000] + "\n…（日志过长，已截断）\n"

    path = os.path.join(BASE_DIR, "log", "ios_shortcut.log")
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # 同一轮运行里每轮都会推一次全量快照，内容只增不减：旧内容是新内容的前缀
        # 就说明是同一次运行，直接覆盖；否则（新一轮运行）把上一份留成 .1。
        # 排查「这次为什么没同步」时，上一次的完整记录往往是唯一能对照的样本，
        # 不留就只剩这一次的残片。
        if not _same_ios_log_run(path, text):
            try:
                os.replace(path, path + ".1")
            except OSError:
                pass
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
    except OSError as exc:
        logging.error("写入 iOS 快捷指令日志失败: %s", exc)
        return jsonify({"status": "error", "message": str(exc)}), 500
    logging.info("收到 iOS 快捷指令日志: %s 字 -> %s", len(text), path)
    return jsonify({"status": "ok", "bytes": len(text)}), 200


@app.route('/request_file', methods=['POST'])
def request_file():
    """统一拉取接口：有文件则返回所有文件列表并清空记录，无文件则返回最新文本"""
    if not require_key():
        return jsonify({"status": "error", "message": "密钥错误"}), 403

    data = request.get_json(silent=True)
    if not isinstance(data, dict) or not data:
        return jsonify({"status": "error", "message": "无效请求"}), 400
    # 这里不能像 /file_sync 那样兜底成 "unknown"：判重是「按设备名」做的，
    # 拿一个假的设备名去查，既命中不了「这台设备手上有没有」，又会在 clients
    # 表里留下一条叫 unknown 的垃圾记录。没有 source 就什么都不投递。
    source = data.get("source")

    # 1. 获取所有待拉取文件
    all_files = latest_file.get_all_files()
    if all_files:
        file_list = []
        # 目录共享的来源（file_id, ip, port）。接收端只用一份「合并清单」，
        # 所以这些目录必须落在同一个文件服务上才合得起来。
        folder_origins = []
        for info in all_files:
            if not isinstance(info, dict):
                continue
            file_id = info.get("file_id")
            ip = info.get("ip")
            # 磁盘上可能留着旧版本写入的脏端口（0、"0"、"abc"、越界）。
            # 读的时候一律判定、不做改写：无效就整条跳过，绝不拿默认端口去猜 ——
            # 发送端的文件服务未必在默认端口上，猜错比「拉不到」更难查。
            # 真正的兜底放在写入侧（/file_sync），那里必须收敛，否则整批文件消失。
            port, port_ok = normalize_file_port(info.get("port"))
            if not port_ok:
                port = None
            name = info.get("name")
            size = info.get("size")
            src = info.get("source")
            # 必须有 ip/port/file_id 才能构建下载地址
            if file_id and ip and port:
                kind = info.get("kind", "file")
                entry = {
                    "file_id": file_id,
                    "name": name,
                    "size": size,
                    "source": src,
                    "kind": kind,
                }
                if kind == "folder":
                    # 目录没有整包下载地址：以前这里给的 /file/<id> 是必然
                    # 返回 409 的死链，不认 kind 的客户端会把它当文件下载。
                    # 统一契约：目录只给 manifests，逐文件下载。
                    entry["manifest_url"] = f"http://{ip}:{port}/manifest/{file_id}"
                    folder_origins.append((file_id, ip, port))
                else:
                    entry["download_url"] = f"http://{ip}:{port}/file/{file_id}"
                file_list.append(entry)
            else:
                logging.warning(f"跳过无效文件记录: {info}")

        # 一次性清空所有记录（文件已交付给请求方）
        latest_file.clear()

        if file_list:
            # 给普通文件编号：接收端直接读 index/total，弹「正在下载 第 3/10 个」。
            # 编号只在一类里算 —— 目录不在这一串里（它的文件由合并清单编号），
            # 否则一次「2 个文件 + 1 个目录」的复制会得到 1/3、2/3 这种错位。
            plain_files = [e for e in file_list if e.get("kind") != "folder"]
            for position, entry in enumerate(plain_files, 1):
                entry["index"] = position
                entry["total"] = len(plain_files)

            # 只共享了一个目录时用 type=folder：接收端据此知道这是一次完整的
            # 目录传输（走清单逐文件下载）。单个/多个普通文件仍统一返回
            # file_list，老客户端无需改动。
            is_single_folder = len(file_list) == 1 and file_list[0].get("kind") == "folder"
            payload = {
                "status": "ok",
                "type": "folder" if is_single_folder else "file_list",
                "files": file_list,
                # 这次到底带没带目录。接收端据此决定「拿不到合并清单」要不要
                # 出声：复制普通文件时本来就没有目录，v7 却照样弹一句「没有
                # 目录内容可同步」，纯属噪音（真机反馈）。
                "folder_count": len(folder_origins),
                # 普通文件个数（不含目录）。通知里说「共 N 项」时要的是它，
                # 而不是 files 的条目数 —— 复制一个目录时 files 只有 1 条，
                # 说「共 1 项」用户会以为只传一个文件。
                "file_count": len(plain_files),
            }
            if is_single_folder:
                payload["manifest_url"] = file_list[0]["manifest_url"]

            # 合并清单：所有目录的文件摊平成一张表，接收端只遍历一层。
            #
            # iOS 快捷指令里「重复每一项」嵌两层时，内层拿到的 Repeat Item 是
            # 空的（v5 真机现象：内层取 url 得到空，报「未指定 URL」）。所以
            # 这里给出一条把所有目录合并好的地址，让接收端全程只有一层循环。
            origins = {(ip, port) for _fid, ip, port in folder_origins}
            if len(origins) == 1:
                host_ip, host_port = origins.pop()
                ids = ",".join(fid for fid, _ip, _port in folder_origins)
                payload["manifest_batch_url"] = (
                    f"http://{host_ip}:{host_port}/manifest/batch?ids={ids}")
                payload["manifest_count"] = len(folder_origins)
            elif len(origins) > 1:
                # 多个来源（多台电脑同时登记了目录）合并不了，明确告知而不是
                # 悄悄只给一份 —— 少传一部分文件比报错更难查。
                logging.warning(
                    "目录共享来自多个文件服务，无法合并清单: %s", sorted(origins))
                payload["manifest_batch_error"] = "目录来自多个文件服务，请分开同步"
            return jsonify(payload), 200
        # 如果列表为空（所有记录均无效），继续执行文本逻辑

    # 2. 无文件时投递最新文本。
    #
    #    这里必须复用 iOS 轮询那套判重（_take_ios_latest），不能只判「不是自己
    #    发的」：那样同一段文本会被反复投递，手机上连跑两次快捷指令，第二次
    #    同样返回完整内容、再覆盖一次剪贴板，把用户这期间复制的东西冲掉。
    #    _take_ios_latest 会做三级判断 —— IOS_NOTIFY 主动推送优先、跳过本机
    #    自己发的、跳过这台设备手上已经有的 —— 都没有时返回 {"pasted": True}
    #    空对象，接收端据此什么都不做。
    latest = _take_ios_latest(source, request.remote_addr)

    return jsonify({
        "status": "ok",
        "type": "text",
        "latest_global": latest
    })

@app.route('/upload_file', methods=['PUT'])
def upload_file():
    """手机主动上传文件到电脑（服务端保存到 save_path）"""
    if not require_key():
        return jsonify({"status": "error", "message": "密钥错误"}), 403

    # request.args / parse_qs 已经做过一次 URL 解码，这里不能再 unquote：
    # 文件名里字面含 %20（如 "report%20final.txt"）会被二次解码成
    # "report final.txt"，用户拿到的文件被悄悄改名。
    encoded_filename = request.args.get('filename', 'uploaded_file')
    filename = fix_mojibake_filename(encoded_filename).replace('\\', '/')

    # 读取原始二进制数据
    file_data = request.get_data()

    if not file_data:
        return jsonify({"status": "error", "message": "未收到文件"}), 400

    # 保存到文件
    save_path = secure_save_path(SAVE_PATH, filename)
    if save_path is None:
        return jsonify({"status": "error", "message": "非法的文件路径"}), 400

    with open(save_path, 'wb') as f:
        f.write(file_data)

    logging.info(f"手机上传文件已保存: {save_path}")
    source = unquote(request.headers.get("source", ""))
    touch_client(request.remote_addr, source, get_os_type(request.headers.get("User-Agent", "")))
    msg = f"来源：{source}\n文件：{filename}\n保存：{save_path}"
    show_notification("手机文件已保存", msg)
    return jsonify({"status": "ok", "message": "文件上传成功","path": save_path}), 200

clear_files = []

@app.route('/clear/<path:filename>')
def clear_file(filename):
    if not require_key():
        return jsonify({"status": "error"}), 403
    deleted = []
    failed = []
    for file in clear_files[:]:
        if not os.path.exists(file):
            continue
        success = False
        for retry in range(10):
            try:
                os.remove(file)
                logging.info(
                    f"文件已删除: {file} "
                    f"(第{retry + 1}次尝试成功)"
                )
                deleted.append(file)
                clear_files.remove(file)
                success = True
                break
            except PermissionError as e:
                logging.warning(
                    f"文件被占用: {file} "
                    f"(第{retry + 1}/10次)"
                )
                time.sleep(2)
            except Exception as e:
                logging.error(f"删除失败: {file}, {e}")
                break
        if not success:
            failed.append(file)
    return jsonify({
        "status": "ok",
        "deleted": len(deleted),
        "failed": len(failed)
    })

@app.route('/download/<path:filename>')
def download_file(filename):
    """提供文件下载"""
    if not require_key():
        return jsonify({"status": "error", "message": "密钥错误"}), 403

    filepath = secure_save_path(SAVE_PATH, filename)
    if filepath is None:
        return "非法的文件路径", 400

    # 检查文件是否存在
    if not os.path.exists(filepath):
        logging.warning(f"文件不存在: {filepath}")
        return "文件不存在或已被下载", 404
    @after_this_request
    def after_download(response):
        clear_files.append(filepath)
        return response
    return send_file(filepath, as_attachment=True)

@app.route('/upload_file_to_download', methods=['PUT'])
def upload_file_to_download():
    """手机主动上传文件到电脑，并通知客户端下载"""
    if not require_key():
        return jsonify({"status": "error", "message": "密钥错误"}), 403

    # 从 URL 参数获取客户端的回调地址和文件名
    redirect_url = request.args.get('redirect')
    if not redirect_url:
        return jsonify({"status": "error", "message": "缺少 redirect 参数"}), 400
    target = request.headers.get("target", "")

    # 解析 redirect_url 中的 filename 参数（作为保存的文件名）
    parsed = urlparse(redirect_url)
    redirect_hostname = parsed.hostname

    # SSRF防护：校验重定向主机名是否可信
    if not redirect_hostname:
        return jsonify({"status": "error", "message": "无效的 redirect_url"}), 400

    is_trusted = False
    if redirect_hostname in ['127.0.0.1', 'localhost']:
        is_trusted = True
    else:
        with _lock:
            for c in clients:
                if c.get("ip") == redirect_hostname:
                    is_trusted = True
                    break

    if not is_trusted:
        logging.warning(f"拒绝未授权的重定向地址: {redirect_url}")
        return jsonify({"status": "error", "message": "不信任的重定向地址"}), 403

    query_params = parse_qs(parsed.query)
    encoded_filename = query_params.get('filename', ['uploaded_file'])[0]
    # parse_qs 已解码一次，不能再 unquote（见 /upload_file 的说明）
    filename = fix_mojibake_filename(encoded_filename).replace('\\', '/')

    # 读取文件数据并保存到本地
    file_data = request.get_data()
    if not file_data:
        return jsonify({"status": "error", "message": "未收到文件"}), 400

    save_path = secure_save_path(SAVE_PATH, filename)
    if save_path is None:
        return jsonify({"status": "error", "message": "非法的文件路径"}), 400

    with open(save_path, 'wb') as f:
        f.write(file_data)

    logging.info(f"文件已保存: {save_path}")

    # 生成文件的下载链接
    download_url = url_for('download_file', filename=filename, _external=True)
    clear_url = url_for('clear_file', filename=filename, _external=True)

    # 通知客户端调用download_url下载
    try:
        resp = requests.post(
            redirect_url,
            json={"download_url": download_url, "clear_url": clear_url},
            # 回调过去也要带密钥：对方（Android MiniHttpServer / 电脑端）
            # 现在可以据此校验来源，而不是只靠 IP 白名单免密收通知。
            headers={"key": KEY},
            timeout=5,
        )
        if resp.status_code >= 400:
            logging.warning(f"通知客户端{target}失败，HTTP {resp.status_code}: {resp.text}")
        else:
            logging.info(f"已通知客户端{target}: {redirect_url}，下载链接: {download_url}")
    except Exception as e:
        logging.error(f"通知客户端{target}时发生异常: {e}")
        return jsonify({
            "status": "partial_success",
            "message": f"文件上传成功，但通知客户端{target}失败",
            "path": save_path,
            "download_url": download_url
        }), 200

    return jsonify({
        "status": "ok",
        "message": f"文件上传成功并已通知客户端{target}",
        "path": save_path,
        "download_url": download_url
    }), 200

@app.route('/status', methods=['GET'])
def status():
    return jsonify({
        "status": "ok",
        "message": "SyncClipboard Server Running"
    }), 200

def get_os_type(ua):
    """把 User-Agent 收敛成注册表里稳定的 os 标签。

    绝不能把整串 UA 原样落库：Android 客户端发的是
    `SyncClipboardAndroid/0.3.4`，而 /clients/online 靠 `os == 'android'`
    决定要不要走 upload_file_to_download 中转。存成 UA 字符串后该分支永远
    命不中，上传地址退化成 Android 根本不接受的直连 PUT，文件静默丢失。
    """
    ua_lower = (ua or "").lower()
    # MacroDroid 是旧版宏的遗留 UA；okhttp / dalvik / SyncClipboardAndroid
    # 是 Android 客户端常见的几种写法，统一归成 Android。
    if re.search(r'macrodroid|android|okhttp|dalvik|syncclipboardandroid', ua_lower):
        return 'Android'
    if re.search(r'darwin', ua_lower):
        # macOS 的 UA 也包含 Darwin，但通常还包含 'Mac OS X'
        if re.search(r'mac os x|macintosh', ua_lower):
            return 'macOS'
        # 移动端 ShortcutRunner 这种往往是 iOS 上的 App 扩展
        return 'iOS'
    if re.search(r'iphone|ipad|ios|shortcutrunner', ua_lower):
        return 'iOS'
    if re.search(r'linux', ua_lower):
        return 'Linux'
    # python-requests 是 Windows 客户端的默认 UA；其余未识别的一律给
    # 稳定标签，避免把任意 UA 字符串写进 os 字段污染分支判断。
    if re.search(r'python|windows|win32', ua_lower) or not ua_lower:
        return 'Windows'
    return 'Unknown'
# 客户端注册
@app.route('/register', methods=['POST'])
def register():
    data = request.get_json(silent=True) or {}
    # IP 优先使用服务器看到的 remote_addr
    ip = request.remote_addr
    port = data.get('file_server_port', 0)
    local_name = str(data.get('local_name') or 'unknown').strip()
    os = data.get('os', get_os_type(request.headers.get('User-Agent')))
    if not require_key():
        return jsonify({"status": "error", "msg": "invalid key"}), 403
    # 黑名单是硬拦截：显式注册也不收，这是它和「移除」的根本区别
    if is_blacklisted(local_name):
        logging.info("拒绝黑名单客户端注册: %s (%s)", local_name, ip)
        return jsonify({"status": "error", "msg": "设备已在黑名单"}), 403
    # 加载客户端列表
    load_clients_ip()
    is_new = add_or_update_client(ip, port, local_name, os)
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
    # 只允许本机调用；非本机必须明确返回 403，否则函数落到 None，
    # Flask 会抛 "View function did not return a valid response" → 500。
    if request.remote_addr not in ('127.0.0.1', '::1'):
        return jsonify({"status": "error", "message": "仅允许本机调用"}), 403
    data = request.get_json(silent=True) or {}
    changed_type = data.get('changed_type')
    if not changed_type:
        return jsonify({"status": "error", "message": "缺少 changed_type"}), 400
    notify_clients(changed_type)
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
