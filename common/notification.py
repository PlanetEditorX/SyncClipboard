# common/notification.py
import logging
from winotify import Notification

logger = logging.getLogger("notification")

def show_notification(title, msg, duration='short'):
    """
    弹出右下角系统通知，自动消失。
    duration: 'short' (约5秒) 或 'long' (约25秒)
    """
    try:
        toast = Notification(
            app_id="SyncClipboard",
            title=title,
            msg=msg,
            duration=duration
        )
        toast.show()       # 阻塞时间极短，内部会异步显示
        logger.debug(f"通知弹出成功: {title}")
    except Exception as e:
        logger.error(f"通知弹出失败: {e}")