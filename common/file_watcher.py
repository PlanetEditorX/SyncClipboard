# common/file_watcher.py
import time
import threading
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

class FileChangeHandler(FileSystemEventHandler):
    """当监控的文件发生修改时，调用回调函数（带防抖）"""
    def __init__(self, callback, debounce_seconds=0.5):
        super().__init__()
        self.callback = callback
        self.debounce_seconds = debounce_seconds
        self._timer = None
        self._lock = threading.Lock()

    def on_modified(self, event):
        if event.is_directory:
            return
        # 防抖：在 debounce_seconds 内只有最后一次修改才触发回调
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(self.debounce_seconds, self.callback)
            self._timer.start()

def watch_file(file_path, callback, debounce_seconds=0.5):
    """
    启动一个后台线程，监控指定文件的变化。
    当文件被修改时（防抖后），调用 callback()。
    返回 Observer 对象，可调用 observer.stop() 停止。
    """
    observer = Observer()
    handler = FileChangeHandler(callback, debounce_seconds)
    # 只监听文件所在目录，事件过滤在 handler 中自行判断
    observer.schedule(handler, path=str(file_path.parent), recursive=False)
    observer.start()
    return observer