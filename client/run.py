# client/run.py
import sys
import time
import signal
import logging
import requests
from pathlib import Path
from client.main_menu import SyncClient
from client.file_server import FileServer
from common.utils import BASE_DIR, SAFE_POST
from logging.handlers import RotatingFileHandler

# 导入 ConfigManager
sys.path.insert(0, str(BASE_DIR))
from gui.config_manager import ConfigManager

def register_to_server(server_host, server_port, file_server_port, local_name, key, logger):
    """
    注册到服务器，支持重试机制
    1分钟后重试一次，如果仍失败则等待总共10分钟后退出
    """
    RETRY_DELAY = 60          # 1分钟
    MAX_WAIT_TIME = 10 * 60   # 10分钟
    start_time = time.time()
    attempt = 0
    url = f"http://{server_host}:{server_port}/register"
    payload = {
        "file_server_port": file_server_port,
        "local_name": local_name,
        "key": key
    }
    while True:
        attempt += 1
        elapsed = time.time() - start_time
        logger.info(f"正在注册到服务器 (第 {attempt} 次尝试)...")
        try:
            resp = SAFE_POST(url, json=payload, timeout=30)
        except Exception as e:
            logger.error(f"注册请求异常: {e}")
            resp = None
        if resp is not None and resp.status_code == 200:
            data = resp.json()
            if data.get("is_new"):
                logger.info("首次注册成功")
            else:
                logger.info("连接服务器成功")
            return True
        # 注册失败的处理
        if resp is not None:
            logger.warning(f"注册失败，状态码: {resp.status_code}")
        else:
            logger.warning("注册失败，无法连接到服务器")
        # 检查是否超过最大等待时间
        if elapsed >= MAX_WAIT_TIME:
            logger.critical(f"已等待超过 {MAX_WAIT_TIME/60} 分钟，注册仍然失败，客户端退出")
            return False
        # 等待后重试
        logger.info(f"将在 {RETRY_DELAY} 秒后重试...")
        time.sleep(RETRY_DELAY)


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
        LOG_FILE, maxBytes=128*1024, backupCount=1, encoding='utf-8'
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

    # ---------- 使用重试机制注册到服务器 ----------
    if not register_to_server(server_host, server_port, file_server_port, local_name, key, logger):
        logger.critical("无法注册到服务器，客户端退出")
        file_server.stop()
        sys.exit(1)

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