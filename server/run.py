# server/run.py
import re
import sys
import logging
from pathlib import Path
from logging.handlers import RotatingFileHandler
from server.api.flask_app import app, init_services
from common.utils import BASE_DIR

# 导入 ConfigManager
sys.path.insert(0, str(BASE_DIR))
from gui.config_manager import ConfigManager

# ---------- 自定义格式化器：过滤 ANSI 转义序列 ----------
class CleanFormatter(logging.Formatter):
    ansi_escape = re.compile(r'\x1b\[[0-9;]*m')

    def format(self, record):
        record.msg = self.ansi_escape.sub('', str(record.msg))
        return super().format(record)
# -------------------------------------------------------

def main():
    # ---------- 服务器独立日志配置 ----------
    LOG_FILE = BASE_DIR / "log" / "syncclipboard.log"
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

    # 获取 root logger，并清除从父进程（gui）继承的 handler
    root_logger = logging.getLogger()
    root_logger.handlers.clear()

    handler = RotatingFileHandler(
        LOG_FILE, maxBytes=128*1024, backupCount=1, encoding='utf-8'
    )

    formatter = CleanFormatter(
        '%(asctime)s [%(levelname)s] %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )

    handler.setFormatter(formatter)
    root_logger.setLevel(logging.INFO)
    root_logger.addHandler(handler)

    logging.info("服务初始化完成")
    # ---------------------------------------------------

    # ---------- 使用 ConfigManager 加载服务器配置 ----------
    config_manager = ConfigManager()

    if not config_manager.load_server_config():
        logging.critical("加载服务器配置文件失败，服务退出")
        sys.exit(1)

    # 更新 Flask app 配置
    app.config.update({
        "port": config_manager.server_port,
        "key": config_manager.key,
        "save_path": config_manager.save_path,
        "local_name": config_manager.local_name
    })

    # 初始化服务（传递配置）
    init_services(config_manager)

    logging.info(f"配置加载完成 | 端口: {config_manager.server_port} | 保存路径: {config_manager.save_path}")

    app.run(
        host="0.0.0.0",
        port=config_manager.server_port,
        debug=False
    )

if __name__ == "__main__":
    main()