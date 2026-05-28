# common/tools.py
"""
工具模块
"""

import sys
import logging
from pathlib import Path

import requests

# 创建模块级日志记录器
logger = logging.getLogger(__name__)


def show_message(title, message):
    """
    显示提示消息的占位函数。
    实际项目中可替换为 GUI 弹窗、控制台输出或日志记录。
    """
    # 这里简单用 print 输出，您可以根据项目需求改为其他实现
    print(f"[{title}] {message}")


def get_base_dir():
    """
    获取应用程序的基础目录。
    - 若程序被打包成可执行文件（如 PyInstaller），返回可执行文件所在目录；
    - 否则返回当前脚本所在目录的父目录（即项目根目录）。
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    else:
        return Path(__file__).resolve().parent.parent


# 项目基础目录
BASE_DIR = get_base_dir()


def safe_post(url, **kwargs):
    """
    发送 POST 请求，内置超时（5秒）和异常处理。
    参数:
        url: 请求地址
        **kwargs: 其他传递给 requests.post 的关键字参数
    返回:
        成功时返回 Response 对象，失败时返回 None 并弹出提示。
    """
    timeout = kwargs.pop("timeout", 5)
    try:
        return requests.post(url, timeout=timeout, **kwargs)
    except requests.exceptions.ConnectTimeout:
        logger.error(f"连接超时: {url}")
        show_message("连接失败", "服务器连接超时")
    except requests.exceptions.ConnectionError:
        logger.error(f"无法连接服务器: {url}")
        show_message("连接失败", "服务器不可达")
    except Exception as e:
        logger.error(f"请求异常: {e}")
        show_message("错误", str(e))
    return None

SAFE_POST = safe_post