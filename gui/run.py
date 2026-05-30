import logging
import multiprocessing
from pathlib import Path
from common.utils import BASE_DIR
from gui.tray_manager import TrayManager
from logging.handlers import RotatingFileHandler

def main():
    """启动托盘程序"""
    TrayManager().run()

if __name__ == "__main__":
    # PyInstaller 打包后需要调用 freeze_support
    multiprocessing.freeze_support()

    # 只有真正的“主进程”才执行托盘启动，避免子进程递归
    if multiprocessing.current_process().name == 'MainProcess':
        # ---------- GUI 日志配置（仅主进程）----------
        LOG_FILE = BASE_DIR / "log" / "gui.log"
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

        handler = RotatingFileHandler(
            LOG_FILE, maxBytes=128*1024, backupCount=1, encoding='utf-8'
        )
        handler.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s'))

        root_logger = logging.getLogger()
        root_logger.setLevel(logging.INFO)
        root_logger.addHandler(handler)

        logger = logging.getLogger(__name__)
        logger.info("托盘管理程序启动")

        # 启动托盘（会阻塞在这里）
        main()