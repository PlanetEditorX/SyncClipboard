# common/utils.py
"""
工具模块
"""
import os
import re
import sys
import struct
import logging
import requests
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox
from urllib.parse import unquote
from typing import Optional, List

# 创建模块级日志记录器
logger = logging.getLogger(__name__)

def show_message(title: str, message: str) -> None:
    """
    显示提示消息。
    线程安全，使用 tkinter 消息框。
    参数:
        title: 消息框标题
        message: 消息内容
    """
    def _show() -> None:
        root = tk.Tk()
        root.withdraw()
        messagebox.showinfo(title, message)
        root.destroy()
    threading.Thread(target=_show, daemon=True).start()

def get_base_dir() -> Path:
    """
    获取应用程序的基础目录。
    - 若程序被打包成可执行文件（如 PyInstaller），返回可执行文件所在目录；
    - 否则返回当前脚本所在目录的父目录（即项目根目录）。
    返回:
        基础目录路径
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    else:
        return Path(__file__).resolve().parent.parent

# 项目基础目录
BASE_DIR = get_base_dir()

def _should_show_debug_message() -> bool:
    """检查是否应该显示调试消息"""
    return os.getenv("DEBUG_MODE") == "1"

def _handle_request_error(title: str, message: str) -> None:
    """
    统一处理请求错误：记录日志并根据调试模式显示消息。
    参数:
        title: 错误标题
        message: 错误消息
    """
    if _should_show_debug_message():
        show_message(title, message)

def safe_post(url: str, **kwargs) -> Optional[requests.Response]:
    """
    发送 POST 请求，内置超时（10秒）和异常处理。
    参数:
        url: 请求地址
        **kwargs: 其他传递给 requests.post 的关键字参数
    返回:
        成功时返回 Response 对象，失败时返回 None 并弹出提示。
    """
    timeout = kwargs.pop("timeout", 10)
    try:
        return requests.post(url, timeout=timeout, **kwargs)
    except requests.exceptions.ConnectTimeout:
        logger.error(f"连接超时: {url}")
        _handle_request_error("连接失败", "服务器连接超时")
    except requests.exceptions.ConnectionError:
        logger.error(f"无法连接服务器: {url}")
        _handle_request_error("连接失败", "服务器不可达")
    except Exception as e:
        logger.error(f"请求异常: {e}")
        _handle_request_error("错误", str(e))
    return None

# 兼容性别名
SAFE_POST = safe_post

def copy_files_to_clipboard(file_paths: List[str]) -> bool:
    """
    将文件路径列表复制到剪贴板（Windows）。
    之后可以在资源管理器中粘贴出这些文件。
    参数:
        file_paths: 文件路径字符串列表
    返回:
        bool: 成功返回 True
    """
    if not file_paths:
        return False
    try:
        import win32clipboard
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

def parse_filename_from_cd(content_disposition: Optional[str]) -> Optional[str]:
    """
    从 Content-Disposition 头中解析文件名。
    支持 RFC 5987 (filename*=UTF-8'') 和标准格式。
    参数:
        content_disposition: Content-Disposition 头的值
    返回:
        解码后的文件名，失败返回 None
    """
    if not content_disposition:
        return None
    # 优先匹配 RFC 5987 格式: filename*=UTF-8''encoded-name
    match = re.search(r"filename\*=UTF-8''([^;]+)", content_disposition, re.IGNORECASE)
    if match:
        encoded = match.group(1)
        return unquote(encoded)  # 解码 % 编码
    # 匹配标准格式: filename="name"
    match = re.search(r'filename="([^"]+)"', content_disposition, re.IGNORECASE)
    if match:
        return match.group(1)
    # 匹配简单格式: filename=name
    match = re.search(r'filename=([^;]+)', content_disposition, re.IGNORECASE)
    if match:
        return match.group(1).strip('"')
    return None