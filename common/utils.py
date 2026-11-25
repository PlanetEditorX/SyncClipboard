"""
工具模块
"""
import os
import re
import sys
import socket
import struct
import logging
import subprocess
import requests
from pathlib import Path
from urllib.parse import unquote
from typing import Optional, List
from datetime import datetime, timedelta

from common import ui_bridge

# 创建模块级日志记录器
logger = logging.getLogger(__name__)

# ---------- UI 主线程投递 ----------
# 实现搬到 common/ui_bridge.py（Qt 时代仍在用同一套队列模型）。
# 这里的函数名带 tk_ 是历史包袱，为了让 file_handler / clipboard_handler
# 等调用方零改动而保留；新代码请直接用 ui_bridge 里的中性命名。


def set_tk_root(root):
    """在主线程启动时调用，登记 UI 根对象。"""
    ui_bridge.set_ui_root(root)


def clear_tk_root(root=None):
    """清除已停止的根对象，避免后台线程继续投递 UI 任务。"""
    ui_bridge.clear_ui_root(root)


def request_tk_shutdown(root):
    """停止接收新任务，并保证退出动作由 UI 队列执行。"""
    return ui_bridge.request_ui_shutdown(root)


def get_tk_root():
    return ui_bridge.get_ui_root()


def process_ui_queue():
    """抽干待执行的界面任务（由 Qt 定时器周期驱动）。"""
    ui_bridge.process_ui_queue()


def post_to_main_thread(func, *args, **kwargs):
    return ui_bridge.post_to_main_thread(func, *args, **kwargs)


def post_to_main_thread_no_wait(func, *args, **kwargs):
    """异步投递到主线程队列，不等待执行结果。"""
    return ui_bridge.post_to_main_thread_no_wait(func, *args, **kwargs)


def show_message(title, message):
    """线程安全的消息框"""
    ui_bridge.show_message(title, message)

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


def open_path(path, is_directory=None):
    """用系统默认程序打开文件或目录，返回 ``(是否成功, 失败原因)``。

    ``is_directory`` 显式指定目标类型；不传时按当前磁盘状态判断。

    不能只靠 ``os.startfile``：Windows 上 ``.json`` 常常没有文件关联，
    它会抛 ``OSError [WinError -2147221003] 找不到应用程序``。托盘里的
    「打开客户端配置」原来把这个异常吞掉只写日志，用户看到的就是
    「点了没有任何反应」。这里按目标类型退一步兜底：文件用记事本、
    目录用资源管理器；都不行才把原因交给调用方弹窗。
    """
    target = Path(path)
    want_dir = target.is_dir() if is_directory is None else bool(is_directory)
    try:
        os.startfile(str(target))          # noqa: S606 - 只在本机打开用户指定的路径
        return True, ""
    except AttributeError:
        # 非 Windows 平台没有 os.startfile，走下面按类型的 system open
        first_error = "当前系统不支持 startfile"
    except OSError as exc:
        first_error = str(exc)

    if os.name == "nt":
        fallback = ["explorer.exe", str(target)] if want_dir else [
            "notepad.exe", str(target)
        ]
    else:
        fallback = ["open" if sys.platform == "darwin" else "xdg-open", str(target)]

    try:
        subprocess.Popen(fallback)
        return True, ""
    except Exception as exc:               # noqa: BLE001 - 兜底失败要把原因带回去
        logger.warning("打开路径失败: %s (%s / %s)", target, first_error, exc)
        return False, f"{first_error}\n{exc}"


def open_url(url):
    """用系统默认浏览器打开网址，返回 ``(是否成功, 失败原因)``。

    仓库 / 发布页这类外链统一走这里，而不是 ``os.startfile``：后者在没配
    默认浏览器的机器上会抛 ``OSError``，``webbrowser`` 会按平台依次尝试
    （Windows 走 os.startfile、macOS 走 ``open``、Linux 走 ``xdg-open``），
    并且明确返回「有没有成功」。
    """
    target = str(url or "").strip()
    if not target:
        return False, "网址为空"
    try:
        import webbrowser

        if webbrowser.open(target, new=2):
            return True, ""
        return False, "系统没有可用的默认浏览器"
    except Exception as exc:               # noqa: BLE001 - 失败原因要带回给调用方
        logger.warning("打开网址失败: %s (%s)", target, exc)
        return False, str(exc)


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

def normalize_local_path(path) -> str:
    r"""把路径统一成本机原生分隔符，Windows 下把 `/` 归一成 `\`。

    配置里可以手填 `D:/Downloads`，而 `os.path.join` 在 Windows 上用 `\`
    拼接，结果就是 `D:/Downloads\文件名` 这种前半段正斜杠、后半段反斜杠
    的混合路径：能落盘、资源管理器也能打开，但通知和日志里看着像路径坏了，
    复制给别的程序还可能被当成转义字符。

    这里只归一化分隔符并去掉冗余层级/尾部斜杠，不改相对与绝对性
    （故意不用 abspath，免得把 `./uploads` 变成一长串绝对路径）。
    """
    return os.path.normpath(os.fspath(path))


def reveal_in_explorer(path: str) -> bool:
    """
    在文件管理器中定位并选中 path（Windows 下为资源管理器）。
    Python 与 Tk 都没有原生的「定位到文件」能力，只能交给系统外壳:
    Windows 用 `explorer /select,<path>`，其他平台退化为打开所在目录。

    参数:
        path: 要定位的文件或目录；不存在时自动退化为打开其所在目录
    返回:
        bool: 已发起打开动作返回 True
    """
    if not path:
        return False

    target = os.path.abspath(path)
    if not os.path.exists(target):
        # 文件被移动/删除（例如共享到期后源目录已被挪走）时退化为打开它的直接上级目录。
        # 只退一层：再往上翻到磁盘根目录对用户没有意义。
        folder = os.path.dirname(target)
        if not folder or not os.path.isdir(folder):
            logger.warning(f"定位失败，路径不存在: {target}")
            return False
        target = folder

    try:
        if sys.platform == "win32":
            if os.path.isdir(target):
                os.startfile(target)  # noqa: S606 —— 打开目录本身
            else:
                # 必须是单个参数「/select,<path>」，explorer 才能选中该文件
                import subprocess

                subprocess.Popen(f'explorer /select,"{target}"')
            return True

        import subprocess

        opener = "open" if sys.platform == "darwin" else "xdg-open"
        folder = target if os.path.isdir(target) else os.path.dirname(target)
        subprocess.Popen([opener, folder])
        return True
    except Exception as e:
        logger.error(f"打开所在目录失败: {target}, 错误: {e}")
        return False


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

def get_default_server_host():
    """默认服务器地址"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"

def isExpired(timestamp):
    """
    判断是否过期
    参数:
        timestamp: 变量中的时间值
    返回:
        False: 小于10分钟
        True: 大于10分钟
    """
    # 获取当前时间
    now_time_str = datetime.now().isoformat()
    # 转换为 datetime 对象
    now_time = datetime.fromisoformat(now_time_str)
    # latest_time = datetime.fromisoformat(latest_global["timestamp"])
    latest_time = datetime.fromisoformat(timestamp)
    # 计算时间差（返回 timedelta 对象）
    diff = now_time - latest_time
    # 判断是否超过 10 分钟（600 秒）
    if diff < timedelta(minutes=10):
        # 拉取时未超过 10 分钟才标记为未使用，超过10分钟默认已使用了
        return False
    else:
        return True


def request_key_matches(expected_key):
    """校验 Flask 请求携带的密钥（中心服务端与电脑端 FileServer 共用）。

    统一契约：所有端一律在**请求头**里带 `key`。JSON body / 查询串里的 key
    也认，但只作为等价入口（部分内部回调是 POST JSON），不是给旧客户端留的
    兼容分支。集中在一处，避免某个路由自己写一套、漏掉鉴权。
    """
    from flask import request
    supplied = request.headers.get("key", "")
    if not supplied:
        data = request.get_json(silent=True)
        if isinstance(data, dict):
            supplied = data.get("key") or ""
    if not supplied:
        supplied = request.args.get("key", "")
    return supplied == expected_key


def fix_mojibake_filename(name):
    """把「UTF-8 字节被按 GBK 解读」产生的乱码文件名还原回中文。

    iOS 把文件变量转文本时，某些路径会把 UTF-8 字节当 GBK 字符读：
    「证书文件」→「璇佷功鏂囦欢」，「飞牛下载」→「椋炵墰涓嬭浇」。
    这类字符串按 GBK 编回去恰好是合法 UTF-8，解出来是更短的正常中文；
    而正常中文 / 纯 ASCII 按 GBK 编码后不是合法 UTF-8，会直接抛错并原样
    返回 —— 所以不会误伤本来正确的文件名。
    """
    try:
        fixed = name.encode('gbk').decode('utf-8')
    except (UnicodeEncodeError, UnicodeDecodeError):
        return name
    if len(fixed) >= len(name) or '\ufffd' in fixed:
        return name
    return fixed
