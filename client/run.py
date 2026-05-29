# client/run.py
import sys
import time
import signal
import logging
import requests
from pathlib import Path
from common.utils import BASE_DIR, SAFE_POST
from client.main_menu import SyncClient
from client.file_server import FileServer
from logging.handlers import RotatingFileHandler

# 导入 ConfigManager
sys.path.insert(0, str(BASE_DIR))
from gui.config_manager import ConfigManager

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

    # ---------- 使用 ConfigManager 加载配置 ----------
    config_manager = ConfigManager()
    if not config_manager.load_client_config():
        logger.critical("加载配置文件失败，客户端退出")
        sys.exit(1)

    # 获取配置值
    server_host = config_manager.server_host
    server_port = config_manager.server_port
    key = config_manager.key
    local_name = config_manager.local_name
    file_server_port = config_manager.file_server_port

    logger.info(f"本机名称: {local_name}")

    # 启动客户端专用文件服务器
    file_server = FileServer(
        port=file_server_port,
        center_host=server_host,
        center_port=server_port,
        local_name=local_name,
        key=key
    )

    client = SyncClient(
        {
            "server_host": server_host,
            "server_port": server_port,
            "key": key,
            "local_name": local_name,
            "file_server_port": file_server_port
        },
        file_server
    )
    file_server.start()

    # ---------- 使用 SAFE_POST 注册到服务器 ----------
    url = f"http://{server_host}:{server_port}/register"
    payload = {
        "file_server_port": file_server_port,
        "local_name": local_name,
        "key": key
    }

    resp = SAFE_POST(url, json=payload, timeout=30)

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