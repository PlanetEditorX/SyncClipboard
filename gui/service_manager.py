import time
import logging
import multiprocessing
from server.run import main as server_main
from client.run import main as client_main

logger = logging.getLogger("gui")


class ServiceManager:
    """服务管理器（服务器和客户端的启停）"""
    def __init__(self, config_manager):
        self.config = config_manager
        self.server_process = None
        self.client_process = None

    @property
    def server_running(self):
        return self.config.server_running

    @server_running.setter
    def server_running(self, value):
        self.config.server_running = value
        self.config.save_state()

    @property
    def client_running(self):
        return self.config.client_running

    @client_running.setter
    def client_running(self, value):
        self.config.client_running = value
        self.config.save_state()

    def start_server(self):
        """启动服务器"""
        logger.info(f"尝试启动服务器，当前状态: running={self.server_running}")
        if self.server_process and self.server_process.is_alive():
            self.server_running = True
            return

        try:
            self.server_process = multiprocessing.Process(target=server_main, daemon=True)
            self.server_process.start()
            self.server_running = True
            logger.info("服务器已启动")
        except Exception as e:
            logger.error(f"服务器启动失败: {e}")
            self.server_running = False

    def stop_server(self):
        """停止服务器"""
        if self.server_process and self.server_process.is_alive():
            self.server_process.terminate()
            self.server_process.join(timeout=5)
            if self.server_process.is_alive():
                self.server_process.kill()
            self.server_process = None
        self.server_running = False
        logger.info("服务器已停止")

    def toggle_server(self):
        """切换服务器状态"""
        if self.server_running:
            self.stop_server()
        else:
            self.start_server()

    def start_client(self):
        """启动客户端"""
        logger.info(f"尝试启动客户端，当前状态: running={self.client_running}")
        if self.client_process and self.client_process.is_alive():
            self.client_running = True
            return

        try:
            self.client_process = multiprocessing.Process(target=client_main, daemon=True)
            self.client_process.start()
            self.client_running = True
            logger.info("客户端已启动")
        except Exception as e:
            logger.error(f"客户端启动失败: {e}")
            self.client_running = False

    def stop_client(self):
        """停止客户端"""
        if self.client_process and self.client_process.is_alive():
            self.client_process.terminate()
            self.client_process.join(timeout=5)
            if self.client_process.is_alive():
                self.client_process.kill()
            self.client_process = None
        self.client_running = False
        logger.info("客户端已停止")

    def toggle_client(self):
        """切换客户端状态"""
        if self.client_running:
            self.stop_client()
        else:
            self.start_client()

    def restart_services(self):
        """重启所有服务"""
        logger.info("正在重启服务...")
        if self.server_running:
            self.stop_server()
        if self.client_running:
            self.stop_client()
        time.sleep(1)
        self.start_server()
        self.start_client()