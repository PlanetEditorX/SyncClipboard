# common/notification.py
import time
import logging
import threading
import platform

logger = logging.getLogger("gui")

toaster = None
if platform.system() == "Windows":
    try:
        from windows_toasts import (
            InteractableWindowsToaster,
            Toast
        )

        AUMID = "PlanetEditorX.SyncClipboard"
        toaster = InteractableWindowsToaster("SyncClipboard", notifierAUMID=AUMID)
    except Exception as e:
        logger.warning(f"Windows 通知初始化失败: {e}")

def show_notification(title, msg):
    if toaster is None:
        logger.info(f"[通知] {title}: {msg}")
        return

    try:
        toast = Toast()
        toast.text_fields = [
            title,
            msg
        ]
        toaster.show_toast(toast)
        time.sleep(5)
        toaster.clear_toasts()
    except Exception as e:
        logger.error(f"通知弹出失败: {e}")

def show_notification_with_click(title, msg, callback):
    if toaster is None:
        logger.info(f"[通知] {title}: {msg}")
        threading.Thread(target=callback, daemon=True).start()
        return

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