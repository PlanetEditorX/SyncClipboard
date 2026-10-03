"""单实例控制：重复启动时唤出已有实例的窗口，而不是重启程序。

理由：双启动最常见的真实意图是「它在托盘里，我想打开窗口」。杀死旧进程会打断
正在运行的同步服务与 multiprocessing 子进程，且新版生效的场景已由 update_apply.py
的批处理独立负责，单实例逻辑不应与之耦合。

只依赖 Win32 命名内核对象与 pid 文件，不引入额外依赖。
非 Windows 平台下所有函数均视为单实例可用。
"""
import ctypes
import logging
import os
import sys
import threading
import time
from ctypes import wintypes
from pathlib import Path

from common.utils import BASE_DIR

logger = logging.getLogger(__name__)

MUTEX_NAME = "Local\\SyncClipboard.SingleInstance"
SHOW_EVENT_PREFIX = "Local\\SyncClipboard.Show."
SHOWN_EVENT_PREFIX = "Local\\SyncClipboard.Shown."
PID_FILE = BASE_DIR / "syncclipboard.pid"

# 等待主实例确认已处理唤出请求的上限
DEFAULT_SHOW_TIMEOUT = 5.0
# 等待主实例写入 pid 的上限
PID_WAIT_TIMEOUT = 3.0

# ensure_single_instance 的三种结果
PRIMARY = "primary"
DELEGATED = "delegated"
UNREACHABLE = "unreachable"

ERROR_ALREADY_EXISTS = 183
EVENT_MODIFY_STATE = 0x0002
SYNCHRONIZE = 0x00100000
WAIT_OBJECT_0 = 0
INFINITE = 0xFFFFFFFF

MB_OK = 0x0
MB_ICONWARNING = 0x30
MB_SETFOREGROUND = 0x10000

_IS_WINDOWS = sys.platform == "win32"

if _IS_WINDOWS:
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.CreateMutexW.restype = wintypes.HANDLE
    _kernel32.CreateMutexW.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)
    _kernel32.CreateEventW.restype = wintypes.HANDLE
    _kernel32.CreateEventW.argtypes = (
        wintypes.LPVOID,
        wintypes.BOOL,
        wintypes.BOOL,
        wintypes.LPCWSTR,
    )
    _kernel32.OpenEventW.restype = wintypes.HANDLE
    _kernel32.OpenEventW.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR)
    _kernel32.SetEvent.restype = wintypes.BOOL
    _kernel32.SetEvent.argtypes = (wintypes.HANDLE,)
    _kernel32.WaitForSingleObject.restype = wintypes.DWORD
    _kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    _kernel32.CloseHandle.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    _user32 = ctypes.WinDLL("user32", use_last_error=True)

# 保持句柄存活到进程结束，句柄关闭即代表主实例已释放
_handles = {"mutex": None, "show": None, "shown": None}


def show_event_name(pid):
    """新实例向主实例发送唤出请求的事件名。"""
    return f"{SHOW_EVENT_PREFIX}{pid}"


def shown_event_name(pid):
    """主实例确认已处理唤出请求的事件名。"""
    return f"{SHOWN_EVENT_PREFIX}{pid}"


def read_pid():
    """读取上一次记录的主实例 pid，文件缺失或损坏时返回 None。"""
    try:
        text = PID_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return int(text) if text.isdigit() else None


def write_pid(pid):
    try:
        PID_FILE.parent.mkdir(parents=True, exist_ok=True)
        PID_FILE.write_text(str(pid), encoding="utf-8")
        return True
    except OSError as exc:
        logger.warning("写入 pid 文件失败: %s", exc)
        return False


def _wait_for_recorded_pid(deadline):
    """互斥量显示已有实例但 pid 尚未写入时，短暂重试读取。"""
    while time.monotonic() < deadline:
        pid = read_pid()
        if pid:
            return pid
        time.sleep(0.1)
    return None


def _request_show(pid, timeout):
    """请求主实例唤出窗口，并等待其确认。"""
    handle = _kernel32.OpenEventW(EVENT_MODIFY_STATE, False, show_event_name(pid))
    if not handle:
        logger.warning("主实例 pid=%s 未注册唤出事件", pid)
        return False

    ack = _kernel32.OpenEventW(SYNCHRONIZE, False, shown_event_name(pid))
    try:
        _kernel32.SetEvent(handle)
        logger.info("已请求主实例 pid=%s 唤出窗口", pid)
    finally:
        _kernel32.CloseHandle(handle)

    if not ack:
        # 主实例未提供确认事件，只能认为请求已送达
        logger.info("主实例 pid=%s 无确认事件，跳过确认", pid)
        return True
    try:
        return _kernel32.WaitForSingleObject(ack, int(timeout * 1000)) == WAIT_OBJECT_0
    finally:
        _kernel32.CloseHandle(ack)


def ensure_single_instance(timeout=DEFAULT_SHOW_TIMEOUT):
    """判断本次启动是主实例、还是应唤出已有实例。

    返回 PRIMARY 时调用方继续启动；返回 DELEGATED 时应静默退出；
    返回 UNREACHABLE 时已有实例存在但无法唤出，调用方应提示用户。
    """
    if not _IS_WINDOWS:
        return PRIMARY

    previous_pid = read_pid()
    # CreateMutexW 只在“已存在”时设置错误码，需先清零避免读到旧值
    ctypes.set_last_error(0)
    mutex = _kernel32.CreateMutexW(None, False, MUTEX_NAME)
    if not mutex:
        logger.warning("创建单实例互斥量失败，错误码=%s", ctypes.get_last_error())
    _handles["mutex"] = mutex
    already_running = bool(mutex) and ctypes.get_last_error() == ERROR_ALREADY_EXISTS

    if already_running:
        if not previous_pid:
            previous_pid = _wait_for_recorded_pid(time.monotonic() + PID_WAIT_TIMEOUT)
        if previous_pid and previous_pid != os.getpid():
            return DELEGATED if _request_show(previous_pid, timeout) else UNREACHABLE
        logger.warning("已有实例在运行但无法确定其 pid，无法唤出窗口")
        return UNREACHABLE

    # 先建好事件再写 pid，避免新实例读到 pid 却找不到事件
    _handles["show"] = _kernel32.CreateEventW(
        None, False, False, show_event_name(os.getpid())
    )
    _handles["shown"] = _kernel32.CreateEventW(
        None, False, False, shown_event_name(os.getpid())
    )
    write_pid(os.getpid())
    return PRIMARY


def start_show_watcher(on_show):
    """后台等待新实例的唤出请求，收到后回调 on_show 并回执。"""
    show_handle = _handles.get("show")
    if not _IS_WINDOWS or not show_handle:
        return None
    shown_handle = _handles.get("shown")

    def _watch():
        while True:
            _kernel32.WaitForSingleObject(show_handle, INFINITE)
            logger.info("收到唤出请求，正在显示主窗口")
            try:
                on_show()
            except Exception:
                logger.exception("唤出主窗口失败")
            if shown_handle:
                _kernel32.SetEvent(shown_handle)

    thread = threading.Thread(target=_watch, name="single-instance-show", daemon=True)
    thread.start()
    return thread


def notify_unreachable():
    """已有实例但无法唤出时，用原生消息框提示用户。"""
    if not _IS_WINDOWS:
        return
    try:
        _user32.MessageBoxW(
            None,
            "已有一个 SyncClipboard 实例正在运行，但未能唤出它的窗口。\n"
            "请从系统托盘图标打开，或在任务管理器中结束该进程后重试。",
            "SyncClipboard",
            MB_OK | MB_ICONWARNING | MB_SETFOREGROUND,
        )
    except Exception:
        logger.exception("显示单实例提示框失败")
