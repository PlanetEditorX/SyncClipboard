import os
import sys
import time
import signal                                      # 信号处理，用于捕获退出信号实现优雅退出
import logging
import requests
import platform
from pathlib import Path                           # 面向对象的文件路径处理
from client.main_menu import SyncClient            # 客户端同步逻辑类
from client.file_server import FileServer          # 客户端文件服务类
from common.utils import BASE_DIR, SAFE_POST       # 公共工具
from logging.handlers import RotatingFileHandler   # 按大小轮转的日志文件处理器

# 将项目根目录加入模块搜索路径，以便导入 gui 模块
sys.path.insert(0, str(BASE_DIR))
from gui.config_manager import ConfigManager  # 配置管理器，读取客户端设置

# ========== PyInstaller 多进程兼容修复 ==========
def fix_multiprocessing():
    """修复 PyInstaller 打包后多进程标准流为 None 的问题"""
    # 判断是否在 PyInstaller 打包环境中运行
    if getattr(sys, 'frozen', False):
        # 将 None 的标准流重定向到空设备（os.devnull），防止子进程因流为空而崩溃
        if sys.stderr is None:
            sys.stderr = open(os.devnull, 'w')  # 标准错误输出 → 空设备
        if sys.stdout is None:
            sys.stdout = open(os.devnull, 'w')  # 标准输出 → 空设备
        if sys.stdin is None:
            sys.stdin = open(os.devnull, 'r')   # 标准输入 ← 空设备

# 在导入其他模块前调用修复函数，确保多进程环境正常
fix_multiprocessing()

def get_os_type():
    os_name = platform.system()
    if os_name == "Windows":
        return "Windows"
    elif os_name == "Darwin":
        if platform.machine().startswith('iP'):
            return "iOS"
        else:
            return "macOS"
    elif os_name == "Linux":
        if hasattr(platform, 'android_ver') or 'Android' in platform.platform():
            return "Android"
        else:
            return "Linux"
    elif os_name == "Java":
        return "Java"
    else:
        return "Unknown"

def register_to_server(server_host, server_port, file_server_port, local_name, key, logger):
    """
    注册到服务器，支持重试机制
    每1分钟重试一次，如果持续失败，等待总共10分钟后退出
    """
    RETRY_DELAY = 60          # 重试间隔：1分钟
    MAX_WAIT_TIME = 10 * 60   # 最大等待时间：10分钟
    start_time = time.time()  # 记录首次注册开始的时间点
    attempt = 0               # 尝试次数计数器
    url = f"http://{server_host}:{server_port}/register"  # 服务器注册接口地址
    # 要发送给服务器的注册数据
    payload = {
        "file_server_port": file_server_port,   # 本客户端文件服务监听的端口
        "local_name": local_name,               # 本机名称（标识）
        "os": get_os_type(),                    # 设备类型
        "key": key                              # 共享密钥，用于认证
    }
    while True:
        attempt += 1
        elapsed = time.time() - start_time      # 已经过去的时间（秒）
        logger.info(f"正在注册到服务器 (第 {attempt} 次尝试)...")
        try:
            resp = SAFE_POST(url, json=payload, timeout=30)  # 发送注册请求，超时30秒
        except Exception as e:
            logger.error(f"注册请求异常: {e}")
            resp = None
        # 检查是否成功注册（HTTP 200 且服务端返回有效数据）
        if resp is not None and resp.status_code == 200:
            data = resp.json()
            if data.get("is_new"):
                logger.info("首次注册成功")     # 首次连接该服务器
            else:
                logger.info("连接服务器成功")   # 已注册过的客户端重新连接
            return True                        # 注册成功，返回 True
        # 注册失败的处理
        if resp is not None:
            logger.warning(f"注册失败，状态码: {resp.status_code}")
        else:
            logger.warning("注册失败，无法连接到服务器")
        # 检查是否超过最大等待时间（10分钟）
        if elapsed >= MAX_WAIT_TIME:
            logger.critical(f"已等待超过 {MAX_WAIT_TIME/60} 分钟，注册仍然失败，客户端退出")
            return False
        logger.info(f"将在 {RETRY_DELAY} 秒后重试...")
        time.sleep(RETRY_DELAY)

def main():
    # ========== 多进程支持 ==========
    import multiprocessing
    multiprocessing.freeze_support()   # 解决 PyInstaller 打包后多进程启动问题

    # ---------- 客户端独立日志配置 ----------
    LOG_FILE = BASE_DIR / "log" / "client.log"          # 日志文件路径：项目根目录/log/client.log
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)  # 创建父目录（如果不存在）

    root_logger = logging.getLogger()            # 获取根日志记录器
    root_logger.setLevel(logging.INFO)           # 设置日志级别为 INFO
    root_logger.handlers.clear()                 # 清除已有的处理器，避免重复

    # 创建 RotatingFileHandler，最大 128KB，保留 1 个备份，UTF-8 编码
    handler = RotatingFileHandler(
        LOG_FILE, maxBytes=128*1024, backupCount=1, encoding='utf-8'
    )
    # 设置日志格式：时间 [级别] 消息
    handler.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s'))
    root_logger.addHandler(handler)              # 将处理器添加到根日志记录器

    logger = logging.getLogger("client")         # 创建名为 "client" 的日志记录器
    logger.info("客户端进程启动")                 # 记录启动信息

    # ---------- 使用 ConfigManager 加载配置 ----------
    config_manager = ConfigManager()             # 实例化配置管理器
    if not config_manager.load_client_config():  # 加载客户端配置文件
        logger.critical("加载配置文件失败，客户端退出")
        sys.exit(1)

    # 从配置管理器中读取各项参数
    server_host = config_manager.server_host           # 服务器地址
    server_port = config_manager.server_port           # 服务器端口
    key = config_manager.key                           # 共享密钥
    local_name = config_manager.local_name             # 本机名称
    file_server_port = config_manager.file_server_port # 本机文件服务端口
    save_path = config_manager.save_path                 # 最近使用的目录（用于文件选择）
    logger.info(f"本机名称: {local_name}")

    # 启动客户端专用文件服务器
    file_server = FileServer(
        port=file_server_port,
        center_host=server_host,
        center_port=server_port,
        local_name=local_name,
        key=key,
        save_path=save_path
    )

    # 创建同步客户端实例
    client = SyncClient(
        {
            "server_host": server_host,
            "server_port": server_port,
            "key": key,
            "local_name": local_name,
            "file_server_port": file_server_port
        },
        file_server    # 传入文件服务器对象，供客户端内部使用
    )
    file_server.start()   # 启动文件服务器的后台线程（监听文件上传/下载请求）

    # ---------- 使用重试机制注册到服务器 ----------
    if not register_to_server(server_host, server_port, file_server_port, local_name, key, logger):
        logger.critical("无法注册到服务器，客户端退出")
        # 安全地停止 file_server（避免残留线程）
        try:
            file_server.stop()
        except AttributeError:
            logger.warning("FileServer 没有 stop 方法，尝试直接终止")
            if hasattr(file_server, 'running'):
                file_server.running = False
        except Exception as e:
            logger.error(f"停止 file_server 时出错: {e}")
        sys.exit(1)

    # ---------- 退出函数 ----------
    def graceful_exit(signum, frame):
        """处理 SIGINT 和 SIGTERM 信号，安全关闭客户端"""
        logger.info("正在关闭客户端...")
        try:
            client.stop()                 # 停止同步客户端（停止网络循环、释放资源）
        except Exception as e:
            logger.error(f"停止客户端时出错: {e}")
        sys.exit(0)                       # 正常退出

    # 注册信号处理器，使得 Ctrl+C 或 kill 命令可以触发 graceful_exit
    signal.signal(signal.SIGINT, graceful_exit)   # 键盘中断
    signal.signal(signal.SIGTERM, graceful_exit)  # 终止信号

    # 启动同步客户端主循环
    client.start()

    # 主线程等待，直到客户端标志位 running 变为 False
    try:
        while client.running:
            time.sleep(1)                # 每秒检查一次，避免忙等
    except KeyboardInterrupt:            # 再次捕获 Ctrl+C，确保能够退出
        graceful_exit(None, None)

# 程序入口点：当脚本被直接执行时调用 main()
if __name__ == "__main__":
    main()