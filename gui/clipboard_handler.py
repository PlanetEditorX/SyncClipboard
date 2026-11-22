import hashlib
import json
import logging
import threading
import time
from datetime import datetime, timedelta
from common.utils import SAFE_POST
from common.notification import show_notification, show_notification_with_click
from common.sync_modes import is_receive_allowed
from server.core.file_latest import is_file_record_expired

logger = logging.getLogger("gui")

# 同一来源的同一份内容，在这个时间窗内重复出现只提醒一次。
# 轮询型客户端（iOS 快捷指令 / Android）会反复把本地剪贴板 POST 上来，
# 服务端偶尔会为同一段文字生成新的 id，光靠 id 去重挡不住。
NOTIFY_DEDUP_SECONDS = 60


class ClipboardHandler:
    """剪贴板更新处理器"""
    def __init__(self, config_manager, tray_manager_ref=None):
        self.config = config_manager
        self.tray_manager = tray_manager_ref
        self.last_global_id = None
        # 最近一次处理过的 (来源, 内容指纹)，用于识别反复推送的同一段文字
        self._last_seen_key = None
        self._last_seen_at = 0.0

    @staticmethod
    def _content_digest(content):
        """内容指纹：来源不同、内容相同也能稳定比较。"""
        if isinstance(content, str):
            raw = content
        else:
            raw = json.dumps(content, ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()

    def _should_announce(self, source, content):
        """这条记录该不该弹窗。

        只拦「同一来源的同一份内容刚刚提醒过」：轮询型客户端每隔一两秒把
        剪贴板整块推上来，服务端偶尔会给同一段文字生成新的 id，靠 id 去重
        挡不住，会变成反复弹窗。

        反过来，中间只要换过内容（A → B → A），即便 A 的内容指纹相同，
        也照常提醒 —— 那是用户又复制了一次。
        """
        key = (source, self._content_digest(content))
        now = time.monotonic()
        if key == self._last_seen_key and now - self._last_seen_at < NOTIFY_DEDUP_SECONDS:
            return False
        self._last_seen_key = key
        self._last_seen_at = now
        return True

    def handle_client_latest(self):
        """处理文本剪贴板更新"""
        try:
            if not is_receive_allowed(getattr(self.config, "text_sync_mode", "both")):
                # 文字接收已关闭：客户端不会写剪贴板，这里也不必提醒
                return
            if not self.config.TEXT_LATEST_FILE.exists():
                return

            with open(self.config.TEXT_LATEST_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)

            latest = data.get('latest_global')
            if not latest:
                return

            current_id = latest.get('id')
            source = latest.get('source')

            if current_id and current_id != self.last_global_id and source != self.config.local_name:
                # 时间判断：超过10分钟不显示
                timestamp_str = latest.get('timestamp')
                if timestamp_str:
                    try:
                        record_time = datetime.fromisoformat(timestamp_str)
                        if datetime.now() - record_time > timedelta(minutes=10):
                            logger.info(f"剪贴板记录已过期（超过10分钟），跳过弹窗: {timestamp_str}")
                            return
                    except Exception as e:
                        logger.warning(f"解析时间戳失败: {timestamp_str}, 错误: {e}")

                self.last_global_id = current_id
                source = latest.get('source', '未知来源')
                content = latest.get('content', '')
                ctype = latest.get('type', 'text')
                title = "✂️ 剪贴板更新"

                # 判重只看「这条记录本身是不是刚刚处理过的那一份」，不能拿本机
                # 剪贴板的内容来判：服务端和客户端装在同一台机器时，服务端收到
                # 远端文字会先把内容写进本机剪贴板，此时「剪贴板里已经是这份
                # 内容」恰恰说明同步刚刚发生，拿它判重会把本该出现的弹窗吞掉。
                if not self._should_announce(source, content if ctype == 'text' else ctype):
                    logger.info(
                        "同一份内容刚刚提醒过，跳过弹窗: %s",
                        str(content)[:30],
                    )
                    return

                if ctype == 'text':
                    preview = content[:60] + ('…' if len(content) > 60 else '')
                    msg = f"来源：{source}\n内容：{preview}"
                else:
                    msg = f"来源：{source}\n类型：{ctype}"

                show_notification(title, msg)
        except Exception:
            logger.exception("处理剪贴板变化失败")

    def handle_file_latest(self):
        """处理文件剪贴板更新"""
        try:
            if not is_receive_allowed(getattr(self.config, "file_sync_mode", "both")):
                # 文件接收已关闭：不再提示远端发布的文件
                return
            if not self.config.FILE_LATEST_FILE.exists():
                return

            with open(self.config.FILE_LATEST_FILE, 'r', encoding='utf-8') as f:
                file_list = json.load(f)

            if isinstance(file_list, dict):
                file_list = [file_list]
            if not isinstance(file_list, list):
                return

            name_list = []
            msg = ""
            for file in file_list:
                if not isinstance(file, dict):
                    continue
                if is_file_record_expired(file):
                    logger.info(
                        "文件记录已过期，跳过通知: %s",
                        file.get('file_id')
                    )
                    continue
                file_id = file.get('file_id')
                if not file_id:
                    return

                source = file.get('source', '未知来源')
                if file_id and source != self.config.local_name and self.tray_manager:
                    name = file.get('name', '未知文件')
                    if name not in name_list:
                        name_list.append(name)
                        if msg == "":
                            msg += f"来源：{source}\n文件：{name}"
                        else:
                            msg += f"\n文件：{name}"
            if name_list and msg:
                def download_callback():
                    threading.Thread(
                        target=self.tray_manager.file_handler.fetch_file_with_progress,
                        daemon=True
                    ).start()

                show_notification_with_click(
                    "检测到文件发布, 点击接收。",
                    msg,
                    download_callback
                )
        except Exception as e:
            logger.error(f"处理文件变化失败: {e}")

    def notify_server_if_running(self, changed_type):
        """如果服务器在运行，通知服务器"""
        if self.config.server_running:
            import requests
            try:
                resp = SAFE_POST(
                    f"http://127.0.0.1:{self.config.server_port}/internal/notify_clients",
                    json={"changed_type": changed_type},
                    timeout=60
                )
                if resp is not None and resp.status_code == 200:
                    logger.info("通知服务器成功")
                else:
                    logger.warning(f"通知服务器失败: {resp.status_code}")
            except Exception:
                logger.exception("客户端通知内部服务器异常")
