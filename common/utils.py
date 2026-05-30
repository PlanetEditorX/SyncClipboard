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

# ---------- 全局 Tk 根窗口支持 ----------
import queue
import threading
import tkinter as tk
from tkinter import messagebox

_tk_root = None
_tk_lock = threading.Lock()
_ui_queue = queue.Queue()

def set_tk_root(root):
    """在主线程启动时调用，注册全局根窗口"""
    global _tk_root
    with _tk_lock:
        _tk_root = root

def get_tk_root():
    with _tk_lock:
        return _tk_root

def process_ui_queue():
    try:
        while True:
            func, args, kwargs, result_event = _ui_queue.get_nowait()
            try:
                ret = func(*args, **kwargs)
                if result_event is not None:
                    result_event.set_result(ret)
            except Exception as e:
                if result_event is not None:
                    result_event.set_exception(e)
            _ui_queue.task_done()
    except queue.Empty:
        pass
    root = get_tk_root()
    if root is not None:
        root.after(50, process_ui_queue)

def post_to_main_thread(func, *args, **kwargs):
    """同步：必须主线程调用，或者调用 root.after 等待结果"""
    root = get_tk_root()
    if threading.current_thread() is threading.main_thread():
        return func(*args, **kwargs)
    else:
        # 使用队列 + Event 实现同步等待
        event = threading.Event()
        result_container = []

        def wrapper():
            try:
                result_container.append(func(*args, **kwargs))
            except Exception as e:
                result_container.append(e)
            finally:
                event.set()

        root.after(0, wrapper)
        event.wait()
        result = result_container[0]
        if isinstance(result, Exception):
            raise result
        return result

def post_to_main_thread_no_wait(func, *args, **kwargs):
    """异步：直接 after(0) 投递，不等待"""
    root = get_tk_root()
    if root is None:
        # 降级处理：无法投递时记录错误或直接执行（风险）
        func(*args, **kwargs)
        return
    if threading.current_thread() is threading.main_thread():
        func(*args, **kwargs)
    else:
        root.after(0, func, *args, **kwargs)

class _ResultEvent:
    """简单的线程同步结果容器"""
    def __init__(self):
        self._event = threading.Event()
        self._result = None
        self._exception = None

    def set_result(self, result):
        self._result = result
        self._event.set()

    def set_exception(self, exc):
        self._exception = exc
        self._event.set()

    def wait(self):
        self._event.wait()
        if self._exception is not None:
            raise self._exception
        return self._result


def show_message(title, message):
    """线程安全的消息框"""
    post_to_main_thread(messagebox.showinfo, title, message)

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

def safe_get(data, *keys, default=None):
    """安全获取嵌套字典的值
    示例:
        content = safe_get(self.data, "latest_global", "content")
    """
    for key in keys:
        if isinstance(data, dict):
            data = data.get(key, default)
        else:
            return default
    return data