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
            self.config.TEXT_LATEST_FILE,
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
        # 统一为绝对路径 Path 对象
        changed = Path(changed_path).resolve()
        client_latest = Path(self.config.TEXT_LATEST_FILE).resolve()
        file_latest   = Path(self.config.FILE_LATEST_FILE).resolve()

        if changed == client_latest:
            self.clipboard_handler.handle_client_latest()
            self.clipboard_handler.notify_server_if_running("text")
        elif changed == file_latest:
            self.clipboard_handler.handle_file_latest()
            self.clipboard_handler.notify_server_if_running("file")