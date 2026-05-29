# common/notification.py
import time
import logging
import threading

from windows_toasts import (
    InteractableWindowsToaster,
    Toast
)

AUMID = "PlanetEditorX.SyncClipboard"

logger = logging.getLogger("gui")
toaster = InteractableWindowsToaster("SyncClipboard", notifierAUMID=AUMID)

def show_notification(title, msg):
    try:
        toast = Toast()
        # 标题
        toast.text_fields = [
            title,
            msg
        ]
        toaster.show_toast(toast)
        time.sleep(5)
        # 这会移除操作中心里的历史记录，但不影响已经弹出的横幅。
        toaster.clear_toasts()
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
        time.sleep(5)
        toaster.clear_toasts()
    except Exception as e:
        logger.error(f"带点击通知失败: {e}")