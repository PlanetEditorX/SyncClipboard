"""UI 线程桥接：把后台线程的界面操作安全地投递到主线程。

历史背景：这套 API 早先以 ``*_tk_*`` 命名，实现是「Tk root.after 轮询
queue」。界面换成 Qt 后约束没有变——Qt 的 widget 同样只能在主线程操作，
所以仍然需要这一层投递。这里刻意保留与旧实现一一对应的语义
（可等待、可取消、关闭后拒绝新任务），使 file_handler / clipboard_handler
等业务代码完全不必改动。

本模块不 import PySide6：它只负责线程与队列，真正的 Qt 弹窗由
``gui.qt_dialogs`` 注册进来，纯逻辑测试因此不需要图形库。
"""

import logging
import queue
import threading

logger = logging.getLogger(__name__)


class _ResultEvent:
    """跨线程回传执行结果。"""

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

    def wait(self, timeout=300):
        if not self._event.wait(timeout):
            raise TimeoutError("等待 UI 主线程响应超时")
        if self._exception is not None:
            raise self._exception
        return self._result


_ui_root = None
_main_window = None
_ui_accepting = False
_lock = threading.Lock()
_queue = queue.Queue()

# 抽干队列的间隔。33ms ≈ 30fps，足够让下载进度条看起来连续。
DRAIN_INTERVAL_MS = 33

_message_handler = None


def set_ui_root(root):
    """在主线程启动时登记 UI 根对象（QApplication），并开始接受任务。"""
    global _ui_root, _ui_accepting
    with _lock:
        _ui_root = root
        _ui_accepting = True


def get_ui_root():
    with _lock:
        return _ui_root


def set_main_window(window):
    """登记主窗口，作为模态对话框的 parent。"""
    global _main_window
    with _lock:
        _main_window = window


def get_main_window():
    with _lock:
        return _main_window


def set_message_handler(handler):
    """注册 messagebox 实现（由 gui 层注入，避免 common 依赖图形库）。"""
    global _message_handler
    _message_handler = handler


def _reject_pending(message):
    while True:
        try:
            _, _, _, result_event = _queue.get_nowait()
        except queue.Empty:
            break
        if result_event is not None:
            result_event.set_exception(RuntimeError(message))
        _queue.task_done()


def clear_ui_root(root=None):
    """清除已停止的根对象，避免后台线程继续投递界面任务。"""
    global _ui_root, _main_window, _ui_accepting
    with _lock:
        if root is None or _ui_root is root:
            _ui_accepting = False
            _ui_root = None
            _main_window = None
            _reject_pending("UI 主循环已经停止")


def request_ui_shutdown(root):
    """停止接收新任务，并保证退出动作由 UI 队列执行。"""
    global _ui_accepting
    with _lock:
        if _ui_root is not root:
            return False
        _ui_accepting = False
        _reject_pending("UI 主循环正在关闭")
        _queue.put((root.quit, (), {}, None))
        return True


def is_ui_ready():
    with _lock:
        return _ui_root is not None and _ui_accepting


def process_ui_queue():
    """抽干待执行的界面任务。由 QTimer 周期调用。"""
    while True:
        try:
            func, args, kwargs, result_event = _queue.get_nowait()
        except queue.Empty:
            break

        try:
            ret = func(*args, **kwargs)
            if result_event is not None:
                result_event.set_result(ret)
        except Exception as exc:  # noqa: BLE001 - 单条任务失败不应拖垮主循环
            if result_event is not None:
                result_event.set_exception(exc)
            else:
                logger.error("UI 队列任务执行失败: %s", exc, exc_info=True)
        _queue.task_done()


def post_to_main_thread(func, *args, **kwargs):
    """同步投递：调用方阻塞到界面线程执行完毕。"""
    if threading.current_thread() is threading.main_thread():
        return func(*args, **kwargs)
    with _lock:
        if _ui_root is None or not _ui_accepting:
            raise RuntimeError("UI 主循环尚未启动或已经停止")
        result_event = _ResultEvent()
        _queue.put((func, args, kwargs, result_event))
    return result_event.wait()


def post_to_main_thread_no_wait(func, *args, **kwargs):
    """异步投递：不等待执行结果。"""
    if threading.current_thread() is threading.main_thread():
        func(*args, **kwargs)
        return True
    with _lock:
        if _ui_root is None or not _ui_accepting:
            logger.debug("忽略 UI 投递：主循环尚未启动或已经停止")
            return False
        _queue.put((func, args, kwargs, None))
        return True


def show_message(title, message):
    """线程安全的消息框；没有界面时退化为日志。"""
    handler = _message_handler
    if handler is None or not is_ui_ready():
        logger.info("[消息] %s: %s", title, message)
        return
    try:
        post_to_main_thread(handler, title, message)
    except (RuntimeError, TimeoutError) as exc:
        logger.info("[消息未显示] %s: %s (%s)", title, message, exc)
