import json
import logging
import winreg
import sys
import socket
from pathlib import Path
from common.utils import BASE_DIR

logger = logging.getLogger("gui")


class ConfigManager:
    """配置管理器"""

    # 配置文件路径常量
    CLIENT_CONFIG = BASE_DIR / "config" / "client_config.json"
    SERVER_CONFIG = BASE_DIR / "config" / "server_config.json"
    STATE_FILE = BASE_DIR / "config" / "gui_state.json"
    CLIENT_LATEST_FILE = BASE_DIR / "latest" / "client_latest.json"
    FILE_LATEST_FILE = BASE_DIR / "latest" / "file_latest.json"

    def __init__(self):
        self.server_host = None
        self.server_port = None
        self.key = None
        self.local_name = None
        self.file_server_port = None
        self.last_dir = str(Path.home() / "Downloads")
        self.server_running = False
        self.client_running = False

        # 确保必要的目录存在
        ConfigManager.CLIENT_CONFIG.parent.mkdir(parents=True, exist_ok=True)
        ConfigManager.CLIENT_LATEST_FILE.parent.mkdir(parents=True, exist_ok=True)

    def load_state(self):
        """加载服务运行状态"""
        if ConfigManager.STATE_FILE.exists():
            try:
                with open(ConfigManager.STATE_FILE, 'r', encoding='utf-8') as f:
                    s = json.load(f)
                self.server_running = s.get('server_running', False)
                self.client_running = s.get('client_running', False)
            except Exception:
                pass

    def save_state(self):
        """保存服务运行状态"""
        ConfigManager.STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(ConfigManager.STATE_FILE, 'w', encoding='utf-8') as f:
            json.dump({
                'server_running': self.server_running,
                'client_running': self.client_running
            }, f)

    def load_client_config(self):
        """加载客户端配置（自动创建默认配置并处理电脑名称）"""
        config_file = ConfigManager.CLIENT_CONFIG

        # 1. 如果配置文件不存在，创建默认配置
        if not config_file.exists():
            logger.info("客户端配置文件不存在，正在创建默认配置...")
            default_config = {
                "server_host": "127.0.0.1",
                "server_port": 8000,
                "key": "123456",
                "local_name": socket.gethostname(),  # 直接使用电脑名称
                "file_server_port": 8899,
                "last_dir": str(Path.home() / "Downloads")
            }
            try:
                with open(config_file, 'w', encoding='utf-8') as f:
                    json.dump(default_config, f, ensure_ascii=False, indent=4)
                logger.info(f"默认配置文件已创建，电脑名称: {default_config['local_name']}")
            except Exception as e:
                logger.error(f"创建默认配置文件失败: {e}")
                return False

        # 2. 读取配置文件
        try:
            with open(config_file, 'r', encoding='utf-8') as f:
                config = json.load(f)
        except Exception as e:
            logger.error(f"读取配置文件失败: {e}")
            return False

        # 3. 处理电脑名称：如果是默认值 "PC-01"，则替换为真实电脑名称
        need_save = False
        if config.get("local_name") == "PC-01":
            real_hostname = socket.gethostname()
            logger.info(f"检测到默认电脑名称 'PC-01'，正在替换为真实电脑名称: {real_hostname}")
            config["local_name"] = real_hostname
            need_save = True

        # 4. 检查并补充缺失的字段（兼容旧版本配置文件）
        default_fields = {
            "server_host": "127.0.0.1",
            "server_port": 8000,
            "key": "",
            "local_name": socket.gethostname(),
            "file_server_port": 8899,
            "last_dir": str(Path.home() / "Downloads")
        }

        for field, default_value in default_fields.items():
            if field not in config:
                logger.warning(f"配置文件缺少字段 '{field}'，使用默认值: {default_value}")
                config[field] = default_value
                need_save = True

        # 5. 如果有修改，保存配置文件
        if need_save:
            try:
                with open(config_file, 'w', encoding='utf-8') as f:
                    json.dump(config, f, ensure_ascii=False, indent=4)
                logger.info("配置文件已更新")
            except Exception as e:
                logger.error(f"保存配置文件失败: {e}")

        # 6. 赋值给实例变量
        self.server_host = config.get("server_host")
        self.server_port = config.get("server_port")
        self.key = config.get("key", "")
        self.local_name = config.get("local_name")
        self.file_server_port = config.get("file_server_port")
        self.last_dir = config.get("last_dir", str(Path.home() / "Downloads"))

        logger.info(f"读取客户端配置 | 服务器={self.server_host}:{self.server_port} | 客户端={self.local_name}")
        return True

    def save_client_config(self):
        """保存客户端配置"""
        try:
            config = {
                "server_host": self.server_host,
                "server_port": self.server_port,
                "key": self.key,
                "local_name": self.local_name,
                "file_server_port": self.file_server_port,
                "last_dir": self.last_dir
            }
            with open(ConfigManager.CLIENT_CONFIG, 'w', encoding='utf-8') as f:
                json.dump(config, f, ensure_ascii=False, indent=4)
            logger.info(f"客户端配置已保存 | 电脑名称: {self.local_name}")
        except Exception as e:
            logger.error(f"保存配置失败: {e}")

    def is_autostart_enabled(self):
        """检查是否已设置开机启动"""
        try:
            key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_READ) as key:
                winreg.QueryValueEx(key, "SyncClipboardTray")
                return True
        except FileNotFoundError:
            return False

    def toggle_autostart(self, enabled):
        """切换开机启动"""
        key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
        try:
            if enabled:
                # 开启
                if getattr(sys, "frozen", False):
                    cmd = f'"{sys.executable}"'
                else:
                    cmd = f'"{sys.executable}" -m gui.run'
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE) as key:
                    winreg.SetValueEx(key, "SyncClipboardTray", 0, winreg.REG_SZ, cmd)
                logger.info("开机启动已启用")
            else:
                # 关闭
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE) as key:
                    winreg.DeleteValue(key, "SyncClipboardTray")
                logger.info("开机启动已禁用")
        except Exception as e:
            logger.error(f"开机启动操作失败: {e}")