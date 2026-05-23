# clipboard_watcher.py
import time
from clipboard_manager import get_clipboard_text
from cache_manager import CacheManager
from item_builder import build_text_item
import logging

class ClipboardWatcher:
    def __init__(self, cache: CacheManager, interval=0.5):
        self.cache = cache
        self.interval = interval
        self.last_text = get_clipboard_text()

    def start(self):
        logging.info("本地剪贴板监听器启动")
        while True:
            try:
                text = get_clipboard_text()
                if text != self.last_text:
                    self.last_text = text
                    source = self.cache.cache.get("local_name", "PC")  # 获取来源
                    item = build_text_item(
                        text=text,
                        source=source,
                        pasted=False
                    )
                    self.cache.update_text(item)
                    logging.info("本地剪贴板变化: %s", text)
            except Exception as e:
                logging.error("剪贴板监听异常: %s", str(e))
            time.sleep(self.interval)