import os
from pystray import MenuItem, Menu

class TrayMenu:
    """托盘菜单构建器"""
    def __init__(self, tray_manager):
        self.manager = tray_manager

    def create(self):
        """构建菜单"""
        get_file_item = MenuItem('获取文件', self.manager.file_handler.fetch_file, default=True)

        return Menu(
            get_file_item,
            Menu.SEPARATOR,
            MenuItem(
                '开机启动',
                self._toggle_autostart,
                checked=lambda item: self.manager.config.is_autostart_enabled()
            ),
            Menu.SEPARATOR,
            MenuItem(
                '启动服务器',
                self._toggle_server,
                checked=lambda item: self.manager.services.server_running
            ),
            MenuItem(
                '启动客户端',
                self._toggle_client,
                checked=lambda item: self.manager.services.client_running
            ),
            Menu.SEPARATOR,
            MenuItem('修改服务器配置', self._edit_server_config),
            MenuItem('修改客户端配置', self._edit_client_config),
            Menu.SEPARATOR,
            MenuItem('重启服务', self._restart_services),
            Menu.SEPARATOR,
            MenuItem('退出', self.manager.quit_app)
        )

    def _toggle_autostart(self, icon, item):
        current = self.manager.config.is_autostart_enabled()
        self.manager.config.toggle_autostart(not current)

    def _toggle_server(self, icon, item):
        self.manager.services.toggle_server()
        self.manager.update_icon()
        icon.update_menu()

    def _toggle_client(self, icon, item):
        self.manager.services.toggle_client()
        self.manager.update_icon()
        icon.update_menu()

    def _edit_server_config(self):
        server_config = BASE_DIR / "config" / "server_config.json"
        if server_config.exists():
            os.startfile(server_config)

    def _edit_client_config(self):
        client_config = BASE_DIR / "config" / "client_config.json"
        if client_config.exists():
            os.startfile(client_config)

    def _restart_services(self, icon, item):
        self.manager.services.restart_services()
        if self.manager.icon:
            self.manager.icon.update_menu()