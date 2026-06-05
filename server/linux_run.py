import json
import sys
import re
import os
import logging
import socket
from pathlib import Path
from logging.handlers import RotatingFileHandler

# Ensure repository root is on sys.path when running this script directly.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server.api.flask_app import app, init_services
from common.utils import BASE_DIR

# ---------- 自定义格式化器：过滤 ANSI 转义序列 ----------
class CleanFormatter(logging.Formatter):
    ansi_escape = re.compile(r'\x1b\[[0-9;]*m')

    def format(self, record):
        record.msg = self.ansi_escape.sub('', str(record.msg))
        return super().format(record)


def default_save_path():
    return str(Path.home() / 'Downloads')


class LinuxConfigManager:
    SERVER_CONFIG = BASE_DIR / 'config' / 'server_config.json'

    def __init__(self):
        self.server_port = 8000
        self.key = '123456'
        self.local_name = socket.gethostname()
        self.last_dir = default_save_path()
        self.save_path = self.last_dir

    def load_server_config(self):
        config_file = LinuxConfigManager.SERVER_CONFIG
        config_file.parent.mkdir(parents=True, exist_ok=True)

        if not config_file.exists():
            logging.info('服务器配置文件不存在，正在创建默认配置...')
            default_config = {
                'key': self.key,
                'save_path': self.save_path,
                'port': self.server_port,
                'local_name': self.local_name
            }
            try:
                with open(config_file, 'w', encoding='utf-8') as f:
                    json.dump(default_config, f, ensure_ascii=False, indent=4)
                logging.info(f"默认服务器配置文件已创建，端口: {default_config['port']}")
            except Exception as e:
                logging.error(f"创建默认服务器配置文件失败: {e}")
                return False

        try:
            with open(config_file, 'r', encoding='utf-8') as f:
                config = json.load(f)
        except Exception as e:
            logging.error(f"读取服务器配置文件失败: {e}")
            return False

        need_save = False
        if config.get('local_name') == 'Server':
            config['local_name'] = self.local_name
            need_save = True

        default_fields = {
            'key': self.key,
            'last_dir': self.last_dir,
            'port': self.server_port,
            'local_name': self.local_name
        }
        for field, default_value in default_fields.items():
            if field not in config:
                logging.warning(f"服务器配置文件缺少字段 '{field}'，使用默认值: {default_value}")
                config[field] = default_value
                need_save = True
        if 'save_path' not in config:
            config['save_path'] = config.get('last_dir', self.last_dir)

        if need_save:
            try:
                with open(config_file, 'w', encoding='utf-8') as f:
                    json.dump(config, f, ensure_ascii=False, indent=4)
                logging.info('服务器配置文件已更新')
            except Exception as e:
                logging.error(f"保存服务器配置文件失败: {e}")

        self.server_port = config.get('port', self.server_port)
        self.key = config.get('key', self.key)
        self.last_dir = config.get('last_dir', config.get('save_path', self.last_dir))
        self.save_path = self.last_dir
        self.local_name = config.get('local_name', self.local_name)
        logging.info(
            f"读取服务器配置 | 端口={self.server_port} | 保存路径={self.last_dir} | 服务器名称={self.local_name}"
        )
        return True


def main():
    # 支持通过环境变量指定日志文件或日志目录
    log_file_env = os.environ.get('LOG_FILE') or os.environ.get('LOG_DIR')
    if log_file_env:
        p = Path(log_file_env)
        if p.suffix:
            LOG_FILE = p if p.is_absolute() else BASE_DIR / p
        else:
            # 当作目录处理
            LOG_FILE = (p if p.is_absolute() else BASE_DIR / p) / 'server_linux.log'
    else:
        LOG_FILE = BASE_DIR / 'log' / 'server_linux.log'
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

    root_logger = logging.getLogger()
    root_logger.handlers.clear()

    handler = RotatingFileHandler(
        LOG_FILE,
        maxBytes=1024 * 1024,
        backupCount=1,
        encoding='utf-8'
    )
    formatter = CleanFormatter(
        '%(asctime)s [%(levelname)s] %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    handler.setFormatter(formatter)
    root_logger.setLevel(logging.INFO)
    root_logger.addHandler(handler)

    logging.info('Linux 服务初始化完成')

    # 支持通过环境变量覆盖配置文件路径或配置目录
    server_conf_env = (
        os.environ.get('SERVER_CONFIG_FILE')
        or os.environ.get('SERVER_CONFIG')
        or os.environ.get('CONFIG_FILE')
        or os.environ.get('CONFIG_DIR')
    )
    if server_conf_env:
        scp = Path(server_conf_env)
        if not scp.is_absolute():
            scp = BASE_DIR / scp
        if scp.is_dir() or not scp.suffix:
            scp = scp / 'server_config.json'
        LinuxConfigManager.SERVER_CONFIG = scp

    config_manager = LinuxConfigManager()
    if not config_manager.load_server_config():
        logging.critical('加载服务器配置文件失败，服务退出')
        sys.exit(1)

    app.config.update({
        'port': config_manager.server_port,
        'key': config_manager.key,
        'save_path': config_manager.save_path,
        'local_name': config_manager.local_name,
        'clipboard_enabled': False
    })

    init_services(config_manager)

    # 支持通过环境变量覆盖运行端口
    port_env = os.environ.get('PORT') or os.environ.get('SERVER_PORT')
    run_port = int(port_env) if port_env else int(config_manager.server_port)
    logging.info(f"配置加载完成 | 端口: {run_port} | 保存路径: {config_manager.save_path}")
    app.run(
        host='0.0.0.0',
        port=run_port,
        debug=False
    )


if __name__ == '__main__':
    main()
