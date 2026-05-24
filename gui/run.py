import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from gui.tray import TrayManager
from common.path import BASE_DIR

# ---------- 日志配置 ----------
LOG_FILE = BASE_DIR / "log" / "gui.log"
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

handler = RotatingFileHandler(LOG_FILE, maxBytes=1*1024*1024, backupCount=1, encoding='utf-8')
handler.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s'))

root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)
root_logger.addHandler(handler)

logger = logging.getLogger(__name__)

if __name__ == "__main__":
    logger.info("托盘管理程序启动")
    tray = TrayManager()
    tray.run()