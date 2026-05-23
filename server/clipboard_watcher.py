# server/clipboard_watcher.py —— 修改 source 获取方式
import time
import logging
from clipboard_manager import get_clipboard_text
from cache_manager import CacheManager
from item_builder import build_text_item

class ClipboardWatcher:
    def __init__(self, cache: CacheManager, on_new_item=None, interval=0.5):
        self.cache = cache
        self.on_new_item = on_new_item  # 回调函数，用于更新 tracker
        self.interval = interval
        self.last_text = get_clipboard_text()

    def start(self):
        logging.info("本地剪贴板监听器启动")
        while True:
            try:
                text = get_clipboard_text()
                if text != self.last_text:
                    self.last_text = text
                    # ---------- 修改点 ----------
                    source = self.cache.cache.get("local_name", "PC")
                    # ---------- 修改点 ----------
                    item = build_text_item(text=text, source=source, pasted=False)
                    self.cache.update_text(item)
                    if self.on_new_item:
                        self.on_new_item(item)   # 调用回调
                    logging.info("本地剪贴板变化: %s", text)
            except Exception as e:
                logging.error("剪贴板监听异常: %s", str(e))
            time.sleep(self.interval)