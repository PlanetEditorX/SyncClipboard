import os
import re
import sys
import json
import time
import winreg   # 仅 Windows，若需跨平台请自行替换
import struct
import logging
import pystray
import tempfile          # 临时目录（备用）
import requests          # 用于 HTTP 请求
import threading
import pyperclip         # 用于操作剪贴板文本
import subprocess
import tkinter as tk
from PIL import Image
import win32clipboard    # 用于将文件列表放入剪贴板
import multiprocessing
from pathlib import Path
from urllib.parse import unquote
from pystray import MenuItem, Menu
from tkinter import filedialog, messagebox

from server.run import main as server_main
from client.run import main as client_main
from common.path import BASE_DIR
from common.file_watcher import watch_files
from common.notification import show_notification, show_notification_with_click

# ---------- 配置文件路径 ----------
CLIENT_CONFIG = BASE_DIR / "config" / "client_config.json"
SERVER_CONFIG = BASE_DIR / "config" / "server_config.json"
STATE_FILE = BASE_DIR / "config" / "gui_state.json"
CLIENT_LATEST_FILE = BASE_DIR / "latest" / "client_latest.json"
FILE_LATEST_FILE = BASE_DIR / "latest" / "file_latest.json"
CLIENT_CONFIG.parent.mkdir(parents=True, exist_ok=True)

logger = logging.getLogger("gui")

# ========== 辅助函数：复制文件列表到剪贴板（Windows） ==========
def copy_files_to_clipboard(file_paths):
    """
    将文件路径列表复制到剪贴板，之后可以在资源管理器中粘贴出这些文件。
    参数: file_paths - 文件路径字符串列表
    返回: bool 成功返回 True
    """
    if not file_paths:
        return False
    try:
        # 构建 CF_HDROP 格式的数据
        # 格式：DROPFILES 结构 + 双NULL结尾的文件路径列表（ANSI编码）
        files_joined = '\0'.join(file_paths) + '\0\0'
        dropfiles = struct.pack('IIII', 20, 0, 0, 0) + files_joined.encode('mbcs')
        win32clipboard.OpenClipboard()
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(win32clipboard.CF_HDROP, dropfiles)
        logger.info(f"已复制 {len(file_paths)} 个文件到剪贴板")
        return True
    except Exception as e:
        logger.error(f"复制文件到剪贴板失败: {e}")
        return False
    finally:
        try:
            win32clipboard.CloseClipboard()
        except:
            pass

# ========== 辅助函数：显示简单的消息框（线程安全） ==========
def show_message(title, msg):
    """
    在一个独立线程中弹出 tkinter 消息框，避免阻塞托盘主循环。
    """
    def _show():
        root = tk.Tk()
        root.withdraw()
        messagebox.showinfo(title, msg)
        root.destroy()
    threading.Thread(target=_show, daemon=True).start()

# ========== 文件名解析 ==========
def parse_filename_from_cd(content_disposition):
    """
    从 Content-Disposition 头中解析文件名，支持 RFC 5987 (filename*=UTF-8'')
    返回解码后的文件名，失败返回 None
    """
    if not content_disposition:
        return None
    # 尝试 filename*=UTF-8''...
    match = re.search(r"filename\*=UTF-8''([^;]+)", content_disposition, re.IGNORECASE)
    if match:
        encoded = match.group(1)
        return unquote(encoded)   # 解码 % 编码
    # 尝试 filename="..."
    match = re.search(r'filename="([^"]+)"', content_disposition, re.IGNORECASE)
    if match:
        return match.group(1)
    # 尝试 filename=... (无引号)
    match = re.search(r'filename=([^;]+)', content_disposition, re.IGNORECASE)
    if match:
        return match.group(1).strip('"')
    return None

# ========== 托盘管理类 ==========
class TrayManager:
    def __init__(self):
        self.icon = None
        self.server_process = None
        self.client_process = None
        self.server_running = False
        self.client_running = False
        self.last_global_id = None
        self.client_latest = CLIENT_LATEST_FILE
        self.file_latest = FILE_LATEST_FILE
        self.last_global_id = None
        self._file_observer = None
        self._monitor_running = False
        self._monitor_thread = None
        # 加载上次运行状态
        self.load_state()
        self.server_host = None
        self.server_port = None
        self.key = None
        self.local_name = None

    def load_client_config(self):
        # 1. 读取客户端配置，获取服务器地址、密钥、本机名称
        if not CLIENT_CONFIG.exists():
            logger.error("客户端配置文件不存在")
            show_message("错误", "未找到 client_config.json")
            return
        try:
            with open(CLIENT_CONFIG, 'r', encoding='utf-8') as f:
                config = json.load(f)
            self.server_host = config.get("server_host", "127.0.0.1")
            self.server_port = config.get("server_port", 8000)
            self.key = config.get("key", "")
            self.local_name = config.get("local_name", "unknown")
        except Exception as e:
            logger.error(f"读取配置文件失败: {e}")
            show_message("错误", "读取配置文件失败")
            return

    # -------- 状态持久化 --------
    def load_state(self):
        if STATE_FILE.exists():
            try:
                with open(STATE_FILE, 'r', encoding='utf-8') as f:
                    s = json.load(f)
                self.server_running = s.get('server_running', False)
                self.client_running = s.get('client_running', False)
            except Exception:
                pass

    def save_state(self):
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(STATE_FILE, 'w', encoding='utf-8') as f:
            json.dump({
                'server_running': self.server_running,
                'client_running': self.client_running
            }, f)

    # -------- 图标更新 --------
    def update_icon(self):
        """根据运行状态更新托盘图标"""
        if not self.icon:
            return
        active = self.server_running or self.client_running
        icon_name = "icon-active.png" if active else "icon-stop.png"
        icon_path = BASE_DIR / "gui" / "icon" / icon_name

        if icon_path.exists():
            self.icon.icon = Image.open(icon_path)

    # -------- 开机启动（Windows）--------
    def _autostart_key(self):
        return r"Software\Microsoft\Windows\CurrentVersion\Run", "SyncClipboardTray"

    def is_autostart_enabled(self):
        try:
            key_path, name = self._autostart_key()
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_READ) as key:
                winreg.QueryValueEx(key, name)
                return True
        except FileNotFoundError:
            return False

    def toggle_autostart(self, icon, item):
        key_path, name = self._autostart_key()
        try:
            if item.checked:   # 当前是开启状态，用户点击后要关闭开机启动
                with winreg.OpenKey(
                    winreg.HKEY_CURRENT_USER,
                    key_path,
                    0,
                    winreg.KEY_SET_VALUE
                ) as key:
                    winreg.DeleteValue(key, name)

            else:              # 当前是关闭状态，用户点击后要开启开机启动
                if getattr(sys, "frozen", False):
                    cmd = f'"{sys.executable}"'
                else:
                    cmd = f'"{sys.executable}" -m gui.run'

                with winreg.OpenKey(
                    winreg.HKEY_CURRENT_USER,
                    key_path,
                    0,
                    winreg.KEY_SET_VALUE
                ) as key:
                    winreg.SetValueEx(
                        key,
                        name,
                        0,
                        winreg.REG_SZ,
                        cmd
                    )

        except Exception as e:
            logger.error(f"开机启动操作失败: {e}")

    # -------- 服务器启停 --------
    def start_server(self, icon=None, item=None):
        logger.info(f"尝试启动服务器，当前状态: running={self.server_running}")
        if self.server_process and self.server_process.is_alive():
            self.server_running = True
            if icon: icon.update_menu()
            return

        try:
            self.server_process = multiprocessing.Process(
                target=server_main,
                daemon=True
            )
            self.server_process.start()
            self.server_running = True
            self.save_state()
            self.update_icon()
            logger.info("服务器已启动")
        except Exception as e:
            logger.error(f"服务器启动失败: {e}")
            self.server_running = False
        if icon:
            icon.update_menu()

    def stop_server(self):
        if self.server_process and self.server_process.is_alive():
            self.server_process.terminate()
            self.server_process.join(timeout=5)
            if self.server_process.is_alive():
                self.server_process.kill()
            self.server_process = None
        self.server_running = False
        self.save_state()
        self.update_icon()
        logger.info("服务器已停止")

    def toggle_server(self, icon, item):
        if self.server_running:
            self.stop_server()
        else:
            self.start_server(icon, item)
        icon.update_menu()

    # -------- 客户端启停 --------
    def start_client(self, icon=None, item=None):
        logger.info(f"尝试启动客户端，当前状态: running={self.client_running}")
        if self.client_process and self.client_process.is_alive():
            self.client_running = True
            if icon: icon.update_menu()
            return
        try:
            self.client_process = multiprocessing.Process(
                target=client_main,
                daemon=True
            )
            self.client_process.start()
            self.client_running = True
            self.save_state()
            self.update_icon()
            logger.info("客户端已启动")
        except Exception as e:
            logger.error(f"客户端启动失败: {e}")
            self.client_running = False
        if icon:
            icon.update_menu()

    def stop_client(self):
        if self.client_process and self.client_process.is_alive():
            self.client_process.terminate()
            self.client_process.join(timeout=5)
            if self.client_process.is_alive():
                self.client_process.kill()
            self.client_process = None
        self.client_running = False
        self.save_state()
        self.update_icon()
        logger.info("客户端已停止")

    def toggle_client(self, icon, item):
        if self.client_running:
            self.stop_client()
        else:
            self.start_client(icon, item)
        icon.update_menu()

    # -------- 重启服务 --------
    def restart_services(self):
        logger.info("正在重启服务...")
        if self.server_running:
            self.stop_server()
        if self.client_running:
            self.stop_client()
        time.sleep(1)
        self.start_server()
        self.start_client()
        if self.icon:
            self.icon.update_menu()

    # -------- 编辑配置文件 --------
    def edit_client_config(self):
        if CLIENT_CONFIG.exists():
            os.startfile(CLIENT_CONFIG)
        else:
            logger.warning("客户端配置文件不存在")

    def edit_server_config(self):
        if SERVER_CONFIG.exists():
            os.startfile(SERVER_CONFIG)
        else:
            logger.warning("服务器配置文件不存在")

    # -------- 手动获取文件/文本 --------
    def fetch_file(self, icon, item):
        """
        托盘菜单『获取文件』的回调函数。
        向服务器 /request_file 发送 POST 请求，根据响应：
        - 如果是文件（status=download）：弹出保存对话框，下载文件并复制到剪贴板
        - 如果是文本（type=text）：直接复制文本到剪贴板
        """
        logger.info("用户点击『获取文件』")

        # 构造请求 URL 并发送 POST
        url = f"http://{self.server_host}:{self.server_port}/request_file"
        try:
            resp = requests.post(
                url,
                headers={"key": self.key},
                json={"source": self.local_name},
                timeout=10
            )
        except Exception as e:
            logger.error(f"请求服务器失败: {e}")
            show_message("请求失败", f"无法连接服务器: {e}")
            return

        # 3. 根据状态码处理响应
        if resp.status_code == 200:
            content_type = resp.headers.get("Content-Type", "")
            content_disposition = resp.headers.get("Content-Disposition", "")

            # 判断是否为文件下载响应
            is_file = ("application/octet-stream" in content_type or
                    "application/x-msdownload" in content_type or
                    "attachment" in content_disposition)

            if is_file:
                # 解析文件名（支持中文）
                filename = parse_filename_from_cd(content_disposition)
                if not filename:
                    filename = "downloaded_file"
                self._save_file_from_response(resp, filename)
                return

            # 否则按 JSON 处理
            try:
                data = resp.json()
            except Exception:
                logger.error("响应既不是文件也不是合法JSON")
                show_message("错误", "服务器返回格式无法识别")
                return

            # 原有的 JSON 处理逻辑（文件下载链接或文本）
            if data.get("status") == "download" and data.get("type") == "file":
                download_url = data.get("download_url")
                filename = data.get("name", "downloaded_file")
                self._download_and_save_file(download_url, filename)
            elif data.get("type") == "text":
                latest = data.get("latest_global")
                if latest and latest.get("content"):
                    pyperclip.copy(latest["content"])
                    logger.info(f"获取文本成功: {latest['content'][:50]}")
                    show_message("获取成功", "文本已复制到剪贴板")
                else:
                    show_message("无内容", "服务器没有可用的文本")
            else:
                logger.warning(f"未知响应格式: {data}")
                show_message("未知响应", "服务器返回格式无法识别")
        elif resp.status_code == 302:
            # 情况3：服务器直接返回重定向（比如直接 send_file 返回文件）
            location = resp.headers.get("Location")
            if location:
                self._download_and_save_file(location, "downloaded_file")
            else:
                logger.error("302重定向无Location头")
                show_message("错误", "服务器重定向错误")
        else:
            logger.warning(f"服务器返回错误状态码: {resp.status_code}")
            show_message("请求失败", f"HTTP {resp.status_code}")

    def _save_file_from_response(self, response, default_filename):
        """将响应内容（文件流）保存为用户选择的文件"""
        result = [None]

        def ask_filename():
            root = tk.Tk()
            root.withdraw()
            initial_dir = str(Path.home() / "Desktop")
            file_path = filedialog.asksaveasfilename(
                title="保存文件",
                initialdir=initial_dir,
                initialfile=default_filename,
                defaultextension="",
                filetypes=[("所有文件", "*.*")]
            )
            root.destroy()
            result[0] = file_path

        t = threading.Thread(target=ask_filename)
        t.start()
        t.join()
        save_path = result[0]
        if not save_path:
            logger.info("用户取消保存")
            return

        try:
            with open(save_path, 'wb') as f:
                f.write(response.content)
            logger.info(f"文件已保存到: {save_path}")
            if copy_files_to_clipboard([save_path]):
                show_message("获取成功", f"文件已保存并到\n{save_path}")
            else:
                show_message("下载成功但复制剪贴板失败", f"文件保存在:\n{save_path}")
        except Exception as e:
            logger.error(f"保存文件失败: {e}")
            show_message("保存失败", str(e))

    def _download_and_save_file(self, url, default_filename):
        """
        下载文件，弹出保存对话框让用户选择保存位置，下载完成后将文件复制到剪贴板。
        """
        result = [None]  # 用于线程间传递选择的结果

        def ask_filename():
            root = tk.Tk()
            root.withdraw()
            # 设置默认保存路径为“桌面”文件夹
            initial_dir = str(Path.home() / "Desktop")
            file_path = filedialog.asksaveasfilename(
                title="保存文件",
                initialdir=initial_dir,
                initialfile=default_filename,
                defaultextension="",
                filetypes=[("所有文件", "*.*")]
            )
            root.destroy()
            result[0] = file_path

        t = threading.Thread(target=ask_filename)
        t.start()
        t.join()  # 等待用户选择
        save_path = result[0]
        if not save_path:
            logger.info("用户取消了文件保存")
            return
        # 开始下载
        try:
            with requests.get(url, stream=True, timeout=30) as r:
                r.raise_for_status()
                with open(save_path, 'wb') as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        f.write(chunk)
            logger.info(f"文件已保存到: {save_path}")
            # 将下载的文件复制到剪贴板
            if copy_files_to_clipboard([save_path]):
                show_message("获取成功", f"文件已保存到\n{save_path}")
            else:
                show_message("下载成功但复制剪贴板失败", f"文件保存在:\n{save_path}")
        except Exception as e:
            logger.error(f"下载文件失败: {e}")
            show_message("下载失败", str(e))

    # -------- 退出程序 --------
    def quit_app(self, icon):
        # 记住退出前的运行状态
        keep_server = self.server_running
        keep_client = self.client_running

        # 停止所有服务（这会修改 self.server_running = False 并保存）
        self.stop_server()
        self.stop_client()

        # 恢复真实的意图状态，并保存到文件
        self.server_running = keep_server
        self.client_running = keep_client
        self.save_state()

        # 退出托盘
        icon.stop()
        os._exit(0)

    # -------- 状态查询（供菜单 checked 使用）--------
    def is_server_running(self):
        return self.server_running

    def is_client_running(self):
        return self.client_running

    # ---------- 文件变化时的处理函数 ----------
    def _on_file_changed(self, changed_path):
        """根据变化的文件路径分发到不同处理函数"""
        if changed_path == self.client_latest.resolve():
            self._handle_client_latest()
        elif changed_path == self.file_latest.resolve():
            self._handle_file_latest()

    def _handle_client_latest(self):
        # 处理 client_latest.json 的逻辑
        try:
            if not self.client_latest.exists():
                return
            with open(self.client_latest, 'r', encoding='utf-8') as f:
                data = json.load(f)
            latest = data.get('latest_global')
            if not latest:
                return
            current_id = latest.get('id')
            source = latest.get('source')
            if current_id and current_id != self.last_global_id and source != self.local_name:
                self.last_global_id = current_id
                source = latest.get('source', '未知来源')
                content = latest.get('content', '')
                ctype = latest.get('type', 'text')
                title = "✂️ 剪贴板更新"
                if ctype == 'text':
                    preview = content[:60] + ('…' if len(content) > 60 else '')
                    msg = f"来源：{source}\n内容：{preview}"
                else:
                    msg = f"来源：{source}\n类型：{ctype}"
                show_notification("检测到剪贴板更新", msg)
        except Exception as e:
            logger.error(f"处理文件变化失败: {e}")

    def _handle_file_latest(self):
        # 处理 file_latest.json 的逻辑
        try:
            if not self.file_latest.exists():
                return
            with open(self.file_latest, 'r', encoding='utf-8') as f:
                data = json.load(f)
            file_id = data.get('file_id')
            if not file_id:
                return
            source = data.get('source', '未知来源')
            if file_id and source != self.local_name:
                name = data.get('name', '未知文件')
                msg = f"来源：{source}\n文件：{name}"
                show_notification_with_click(
                    "检测到文件发布, 点击保存。",
                    msg,
                    lambda: self.fetch_file(None, None)
                )
        except Exception as e:
            logger.error(f"处理文件变化失败: {e}")

    # ---------- 启动文件监听 ----------
    def _start_file_watchers(self):
        if self._file_observer is not None:
            return
        files_to_watch = [self.client_latest, self.file_latest]
        self._file_observer = watch_files(
            files_to_watch,
            self._on_file_changed,
            debounce_seconds=0.8
        )
        logger.info("多文件监控已启动（latest/）")

    # ---------- 停止文件监听 ----------
    def _stop_file_watchers(self):
        if self._file_observer:
            self._file_observer.stop()
            self._file_observer.join()
            self._file_observer = None

    # -------- 菜单构建 --------
    def create_menu(self):
        # 将获取文件设为默认菜单项（左键触发）
        get_file_item = MenuItem('获取文件', self.fetch_file, default=True)

        return Menu(
            get_file_item,           # 左键点击会执行这个
            Menu.SEPARATOR,
            MenuItem(
                '开机启动',
                self.toggle_autostart,
                checked=lambda item: self.is_autostart_enabled()
            ),
            Menu.SEPARATOR,
            MenuItem(
                '启动服务器',
                self.toggle_server,
                checked=lambda item: self.is_server_running()
            ),
            MenuItem(
                '启动客户端',
                self.toggle_client,
                checked=lambda item: self.is_client_running()
            ),
            Menu.SEPARATOR,
            MenuItem('修改服务器配置', self.edit_server_config),
            MenuItem('修改客户端配置', self.edit_client_config),
            Menu.SEPARATOR,
            MenuItem('重启服务', self.restart_services),
            Menu.SEPARATOR,
            MenuItem('退出', self.quit_app)
        )

    def on_left_click(self, icon):
        """左键点击托盘图标时触发的动作"""
        self.fetch_file(icon, None)

    # -------- 运行托盘 --------
    def run(self):
        active = self.server_running or self.client_running
        icon_name = "icon-active.png" if active else "icon-stop.png"
        icon_path = BASE_DIR / "gui" / "icon" / icon_name
        # 读取客户端配置
        self.load_client_config()
        if icon_path.exists():
            image = Image.open(icon_path)
        else:
            image = Image.new("RGB", (64, 64), "blue")

        # 根据历史状态自动恢复服务
        logger.info(f"状态恢复: server={self.server_running}, client={self.client_running}")
        if self.server_running:
            self.start_server()   # 不带 icon，容错
        if self.client_running:
            self.start_client()

        # 启动文件监控
        self._start_file_watchers()

        # 托盘
        self.icon = pystray.Icon("SyncClipboard", image, "SyncClipboard", self.create_menu())
        self.icon.on_click = self.on_left_click
        self.update_icon()
        self.icon.run()
        # 托盘退出后停止文件监控
        self._stop_file_watcher()

# ========== 主入口 ==========
if __name__ == "__main__":
    multiprocessing.freeze_support()
    TrayManager().run()