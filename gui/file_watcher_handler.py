import logging
from pathlib import Path
from common.file_watcher import watch_files

logger = logging.getLogger("gui")


class FileWatcherHandler:
    """文件监控处理器"""
    def __init__(self, clipboard_handler, config_manager):
        self.clipboard_handler = clipboard_handler
        self.config = config_manager
        self._observer = None

    def start(self):
        """启动文件监控"""
        if self._observer is not None:
            return

        files_to_watch = [
            self.config.CLIENT_LATEST_FILE,
            self.config.FILE_LATEST_FILE
        ]

        self._observer = watch_files(
            files_to_watch,
            self._on_file_changed,
            debounce_seconds=0.8
        )
        logger.info("多文件监控已启动（latest/）")

    def stop(self):
        """停止文件监控"""
        if self._observer:
            self._observer.stop()
            self._observer.join()
            self._observer = None
            logger.info("文件监控已停止")

    def _on_file_changed(self, changed_path):
        """文件变化时的分发处理"""
        client_latest = self.config.CLIENT_LATEST_FILE.resolve()
        file_latest = self.config.FILE_LATEST_FILE.resolve()

        if changed_path == client_latest:
            self.clipboard_handler.handle_client_latest()
            self.clipboard_handler.notify_server_if_running("text")
        elif changed_path == file_latest:
            self.clipboard_handler.handle_file_latest()
            self.clipboard_handler.notify_server_if_running("file")