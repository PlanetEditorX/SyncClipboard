# common/notification.py

import threading
import logging

from windows_toasts import (
    WindowsToaster,
    Toast
)

logger = logging.getLogger("notification")
toaster = WindowsToaster("SyncClipboard")

def show_notification(title, msg):
    try:
        toast = Toast()
        # 标题
        toast.text_fields = [
            title,
            msg
        ]
        toaster.show_toast(toast)
    except Exception as e:
        logger.error(f"通知弹出失败: {e}")

def show_notification_with_click(title, msg, callback):
    try:
        toast = Toast()
        toast.text_fields = [
            title,
            msg
        ]
        def _clicked(event_args):
            try:
                threading.Thread(
                    target=callback,
                    daemon=True
                ).start()
            except Exception as e:
                logger.error(f"通知回调失败: {e}")
        toast.on_activated = _clicked
        toaster.show_toast(toast)
    except Exception as e:
        logger.error(f"带点击通知失败: {e}")