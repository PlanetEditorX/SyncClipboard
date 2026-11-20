"""Windows 桌面通知（WinRT toast）。

重要：通知器必须惰性创建。
``InteractableWindowsToaster`` 的构造函数会初始化 WinRT/COM；如果这件事
发生在 Qt 初始化 GUI 线程的 COM 之前，进程会直接 access violation ——
表现就是「同样的代码，import 顺序不同就崩」。所以这里不在模块导入时
建立实例，等第一次真正要发通知时再建。
"""

import logging
import platform
import threading
import time

logger = logging.getLogger("gui")

AUMID = "PlanetEditorX.SyncClipboard"

_toaster = None
_toaster_ready = False
_toaster_lock = threading.Lock()

# 通知展示多久后自动清理（后台线程执行，不阻塞调用方）
_CLEAR_DELAY = 5


def _get_toaster():
    """首次调用时创建通知器；失败后不再重试，直接退化为日志。"""
    global _toaster, _toaster_ready
    if _toaster_ready:
        return _toaster
    with _toaster_lock:
        if _toaster_ready:
            return _toaster
        _toaster_ready = True
        if platform.system() != "Windows":
            return None
        try:
            from windows_toasts import InteractableWindowsToaster

            _toaster = InteractableWindowsToaster("SyncClipboard", notifierAUMID=AUMID)
        except Exception as exc:
            logger.warning("Windows 通知初始化失败: %s", exc)
            _toaster = None
        return _toaster


def _build_toast(title, msg):
    from windows_toasts import Toast

    toast = Toast()
    toast.text_fields = [title, msg]
    return toast


def _schedule_clear(toaster):
    """延时清理通知，避免像原来那样在调用线程里 sleep 5 秒。"""

    def _clear():
        time.sleep(_CLEAR_DELAY)
        try:
            toaster.clear_toasts()
        except Exception:
            logger.debug("清理通知失败", exc_info=True)

    threading.Thread(target=_clear, name="toast-clear", daemon=True).start()


def show_notification(title, msg):
    toaster = _get_toaster()
    if toaster is None:
        logger.info("[通知] %s: %s", title, msg)
        return

    try:
        toaster.show_toast(_build_toast(title, msg))
        _schedule_clear(toaster)
    except Exception as exc:
        logger.error("通知弹出失败: %s", exc)


def show_notification_with_click(title, msg, callback):
    toaster = _get_toaster()
    if toaster is None:
        logger.info("[通知] %s: %s", title, msg)
        threading.Thread(target=callback, daemon=True).start()
        return

    try:
        toast = _build_toast(title, msg)

        def _clicked(event_args):
            try:
                threading.Thread(target=callback, daemon=True).start()
            except Exception as exc:
                logger.error("通知回调失败: %s", exc)

        toast.on_activated = _clicked
        toaster.show_toast(toast)
        _schedule_clear(toaster)
    except Exception as exc:
        logger.error("带点击通知失败: %s", exc)
