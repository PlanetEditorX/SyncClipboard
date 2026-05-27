# common/file_watcher.py
import time
import threading
from pathlib import Path
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

class FileChangeHandler(FileSystemEventHandler):
    """当目录下任意文件修改时触发，防抖后调用统一回调，并传递变化的文件路径"""
    def __init__(self, callback, watch_files=None, debounce_seconds=0.5):
        super().__init__()
        self.callback = callback          # 回调函数签名: callback(changed_file_path)
        self.watch_files = set(Path(f).resolve() for f in (watch_files or []))
        self.debounce_seconds = debounce_seconds
        self._timers = {}                 # 每个文件一个防抖定时器
        self._lock = threading.Lock()

    def on_modified(self, event):
        if event.is_directory:
            return
        changed_path = Path(event.src_path).resolve()
        # 只处理我们关心的文件
        if changed_path not in self.watch_files:
            return
        with self._lock:
            # 为该文件取消旧定时器，启动新定时器
            if changed_path in self._timers:
                self._timers[changed_path].cancel()
            timer = threading.Timer(
                self.debounce_seconds,
                self._trigger_callback,
                args=[changed_path]
            )
            self._timers[changed_path] = timer
            timer.start()

    def _trigger_callback(self, path):
        with self._lock:
            self._timers.pop(path, None)
        self.callback(path)

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

def watch_files(file_paths, callback, debounce_seconds=0.5):
    """
    监控同一目录下的多个文件，发生修改时调用 callback(file_path)。
    file_paths: 文件路径列表（必须都在同一目录下，取第一个文件的父目录作为监听目录）
    返回 Observer 对象，可调用 observer.stop() 停止。
    """
    if not file_paths:
        raise ValueError("至少提供一个文件路径")
    # 取第一个文件的父目录作为监听目录（假设所有文件在同一目录）
    watch_dir = Path(file_paths[0]).parent
    observer = Observer()
    handler = FileChangeHandler(callback, watch_files=file_paths, debounce_seconds=debounce_seconds)
    observer.schedule(handler, str(watch_dir), recursive=False)
    observer.start()
    return observer