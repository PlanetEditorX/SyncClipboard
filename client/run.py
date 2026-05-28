# client/run.py
import json
import os
import sys
import time
import signal
import logging
import requests
from pathlib import Path
from common.tools import BASE_DIR, SAFE_POST
from client.main_menu import SyncClient
from client.file_server import FileServer
from logging.handlers import RotatingFileHandler

# ---------- 配置文件路径 ----------
CONFIG_FILE = BASE_DIR / "config" / "client_config.json"
CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)

def load_config():
    if not os.path.exists(CONFIG_FILE):
        default_config = {
            "server_host": "127.0.0.1",
            "server_port": 8000,
            "key": "123456",
            "local_name": "PC-01",
            "file_server_port": 8899
        }
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(default_config, f, ensure_ascii=False, indent=2)
        return default_config
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def main():
    # ---------- 客户端独立日志配置 ----------
    LOG_FILE = BASE_DIR / "log" / "client.log"
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

    # 使用独立的 logger，不与 gui 共用
    logger = logging.getLogger("client")
    logger.setLevel(logging.INFO)
    # 清除从父进程继承的 handler，避免日志写入 gui.log
    logger.handlers.clear()
    logger.propagate = False   # 防止传播到 root logger

    handler = RotatingFileHandler(
        LOG_FILE, maxBytes=1*1024*1024, backupCount=1, encoding='utf-8'
    )
    handler.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s'))
    logger.addHandler(handler)

    logger.info("客户端进程启动")

    config = load_config()
    # 启动客户端专用文件服务器
    file_server = FileServer(
        port=config.get("file_server_port"),
        center_host=config.get("server_host"),
        center_port=config.get("server_port"),
        local_name=config.get("local_name"),
        key=config.get("key")
    )

    client = SyncClient(
        config,
        file_server
    )
    file_server.start()

    # ---------- 使用 SAFE_POST 注册到服务器 ----------
    url = f"http://{config['server_host']}:{config['server_port']}/register"
    payload = {
        "file_server_port": config["file_server_port"],
        "local_name": config["local_name"],
        "key": config["key"]
    }

    resp = SAFE_POST(url, json=payload)   # 直接调用，内置超时和异常处理

    if resp is None:
        # 请求失败
        logger.critical("无法注册到服务器，客户端退出")
        sys.exit(1)

    if resp.status_code == 200:
        data = resp.json()
        if data.get("is_new"):
            logger.info("首次注册成功...")
        else:
            logger.info("更新注册成功...")
    else:
        logger.warning(f"注册返回异常状态码: {resp.status_code}")

    def graceful_exit(signum, frame):
        logger.info("正在关闭客户端...")
        client.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, graceful_exit)
    signal.signal(signal.SIGTERM, graceful_exit)

    client.start()

    try:
        while client.running:
            time.sleep(1)
    except KeyboardInterrupt:
        graceful_exit(None, None)

if __name__ == "__main__":
    main()