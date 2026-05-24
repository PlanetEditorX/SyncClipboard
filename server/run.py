import json
import re
from pathlib import Path
import logging
from logging.handlers import RotatingFileHandler
from server.api.flask_app import app, init_services
from common.path import BASE_DIR

CONFIG_FILE = BASE_DIR / "config" / "server_config.json"

def load_config():
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

# ---------- 自定义格式化器：过滤 ANSI 转义序列 ----------
class CleanFormatter(logging.Formatter):
    ansi_escape = re.compile(r'\x1b\[[0-9;]*m')

    def format(self, record):
        record.msg = self.ansi_escape.sub('', str(record.msg))
        return super().format(record)
# -------------------------------------------------------

def main():
    LOG_FILE = BASE_DIR / "log" / "syncclipboard.log"
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

    handler = RotatingFileHandler(
        LOG_FILE,
        maxBytes=1 * 1024 * 1024,
        backupCount=0,
        encoding="utf-8"
    )

    formatter = CleanFormatter(
        '%(asctime)s [%(levelname)s] %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )

    handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    root_logger.addHandler(handler)

    logging.info("服务初始化完成")

    config = load_config()
    app.config.update(config)

    init_services()

    logging.info("配置加载完成: %s", config)

    app.run(
        host="0.0.0.0",
        port=config["port"],
        debug=False
    )

if __name__ == "__main__":
    main()