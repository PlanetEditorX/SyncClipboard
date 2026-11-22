import os
import time
import logging
import requests
import threading
from common import ui_bridge
from common.utils import (
    post_to_main_thread,
    post_to_main_thread_no_wait,
    show_message,
    copy_files_to_clipboard,
    parse_filename_from_cd,
    reveal_in_explorer,
)
from common.notification import show_notification_with_click
from gui.qt_download_dialog import DownloadProgressDialog

logger = logging.getLogger("gui")

# 下载中断自动重试次数（含首次）。发送端在传输失败时会保留共享与压缩包，
# 所以这里的重试能真正接上；续传用 HTTP Range，不必从头再来。
MAX_DOWNLOAD_ATTEMPTS = 3
RETRY_WAIT_SECONDS = 2

# 目录共享的打包等待上限：打包 10GB 级目录在机械盘上可能要十几分钟
PREPARE_TIMEOUT_SECONDS = 30 * 60

# (连接超时, 读超时)。读超时按「等下一块数据」计，不是总时长。
CONNECT_TIMEOUT = 10
READ_TIMEOUT = 60


class FileHandler:
    """文件下载处理器"""
    def __init__(self, config_manager):
        self.config = config_manager

    def _run_in_worker_thread(self, func, *args, **kwargs):
        """如果当前在主线程，则将阻塞操作移到后台线程执行。"""
        if threading.current_thread() is threading.main_thread():
            thread = threading.Thread(target=func, args=args, kwargs=kwargs, daemon=True)
            thread.start()
            return thread
        return func(*args, **kwargs)

    def _run_in_main_thread(self, func, *args, **kwargs):
        """在主线程执行 func 并同步返回结果。"""
        if threading.current_thread() is threading.main_thread():
            return func(*args, **kwargs)
        try:
            return post_to_main_thread(func, *args, **kwargs)
        except (RuntimeError, TimeoutError):
            logger.error("UI 主循环未就绪，无法执行界面操作")
            return None

    def _get_dialog_parent(self):
        return ui_bridge.get_main_window()

    @staticmethod
    def _announce_saved(target, count=None):
        """保存完成后直接在资源管理器里定位，并留一条可点击的通知。

        以前这里弹一个「下载完成」模态框，用户点掉之后还得自己去找文件；
        直接定位到文件更符合「点通知 → 我要这份文件」的意图。
        """
        reveal_in_explorer(target)
        if count:
            title = "下载完成"
            msg = f"已保存 {count} 个文件，已在资源管理器中打开"
        else:
            title = "文件已保存"
            msg = f"已在资源管理器中定位\n文件：{os.path.basename(target)}"
        try:
            show_notification_with_click(
                title, msg, lambda: reveal_in_explorer(target)
            )
        except Exception:
            logger.exception("保存完成通知发送失败")

    def _target_path(self, filename, save_dir=None):
        """保存到下载目录，重名自动加 (1)；不再弹「保存到哪」的对话框。"""
        folder = save_dir or self.config.save_path
        try:
            os.makedirs(folder, exist_ok=True)
        except OSError as e:
            logger.error(f"无法创建下载目录: {folder}, 错误: {e}")
        file_path = os.path.join(folder, filename or "downloaded_file")
        base, ext = os.path.splitext(file_path)
        counter = 1
        while os.path.exists(file_path):
            file_path = f"{base} ({counter}){ext}"
            counter += 1
        return file_path

    def _save_file_stream(self, response, filename):
        """流式接收文件并写入下载目录（服务端直接回文件流的旧协议）。"""
        file_path = self._target_path(filename)

        progress = post_to_main_thread(DownloadProgressDialog, "下载进度")

        try:
            total_size = int(response.headers.get('content-length', 0))
            downloaded = 0
            last_percentage = -1

            with open(file_path, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        if progress.is_cancelled():
                            logger.info("用户取消下载")
                            f.close()
                            try:
                                os.remove(file_path)
                            except OSError as e:
                                logger.warning(f"清理下载文件失败: {e}", exc_info=True)
                            show_message("已取消", "文件下载已取消")
                            return

                        f.write(chunk)
                        downloaded += len(chunk)

                        if total_size > 0:
                            percentage = int(downloaded / total_size * 100)
                            if percentage != last_percentage:
                                downloaded_mb = downloaded / (1024 * 1024)
                                total_mb = total_size / (1024 * 1024)
                                progress.update_progress(percentage, downloaded_mb, total_mb)
                                last_percentage = percentage

            progress.update_progress(100, downloaded/(1024*1024), downloaded/(1024*1024))
            progress.close()

            logger.info(f"文件已保存: {file_path} ({downloaded} 字节)")

            if copy_files_to_clipboard([file_path]):
                logger.info(f"保存成功文件已保存至:\n{file_path}")
            else:
                logger.info(f"文件保存至:\n{file_path}失败")
            self._announce_saved(file_path)

        except Exception as e:
            progress.close()
            logger.error(f"保存文件失败: {e}")
            show_message("保存失败", f"无法保存文件: {e}")
            try:
                if os.path.exists(file_path):
                    os.remove(file_path)
            except OSError as e:
                logger.warning(f"清理下载文件失败: {e}", exc_info=True)
        finally:
            response.close()

    def _prepare_folder_share(self, download_url, progress_dialog):
        """目录共享：先让发送端打包，打包完成才真正去取。

        发送端是按需打包的（复制时不压缩），大目录压缩可能要好几分钟。
        直接 GET 会让连接一直等首字节，超过读超时就被掐断；这里先轮询
        `/prepare/<file_id>` 拿进度，打包好了再下载。
        """
        marker = "/file/"
        if marker not in download_url:
            return True
        base, file_id = download_url.rsplit(marker, 1)
        prepare_url = f"{base}/prepare/{file_id}"
        deadline = time.time() + PREPARE_TIMEOUT_SECONDS
        last_note = ""

        while time.time() < deadline:
            if progress_dialog is not None and progress_dialog.is_cancelled():
                logger.info("用户取消，放弃等待发送端打包")
                return False
            try:
                resp = requests.get(prepare_url, timeout=CONNECT_TIMEOUT)
                data = resp.json() if resp.content else {}
            except Exception as e:
                logger.warning(f"查询打包状态失败，稍后重试: {e}")
                time.sleep(RETRY_WAIT_SECONDS)
                continue

            status = data.get("status")
            if status == "ready":
                logger.info("发送端已打包完成，开始下载: %s", file_id)
                return True
            if status in ("error", "unknown", "cancelled"):
                logger.error("发送端打包失败: %s", data.get("message"))
                show_message(
                    "目录打包失败",
                    data.get("message") or "发送端无法打包该目录，请稍后在电脑上重试",
                )
                return False

            note = data.get("progress") or "正在打包…"
            if note != last_note:
                last_note = note
                logger.info(f"等待发送端打包: {note}")
                if progress_dialog is not None:
                    post_to_main_thread_no_wait(
                        progress_dialog.set_busy, f"发送端正在打包：{note}"
                    )
            time.sleep(1)

        logger.error("等待发送端打包超时")
        show_message("打包超时", "等待对方打包目录超时，请稍后重试")
        return False

    def _download_with_resume(self, download_url, file_path, progress_dialog):
        """带断线重试与断点续传的下载；返回 (是否成功, 已收字节数)。"""
        downloaded = 0
        last_error = None

        for attempt in range(1, MAX_DOWNLOAD_ATTEMPTS + 1):
            headers = {}
            if downloaded:
                headers["Range"] = f"bytes={downloaded}-"
            try:
                with requests.get(
                    download_url, stream=True, headers=headers,
                    timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
                ) as r:
                    if r.status_code == 416:
                        # 请求的区间已超出文件长度：当作已经取全
                        logger.info("服务端返回 416，判定文件已完整: %s", file_path)
                        return True, downloaded
                    r.raise_for_status()

                    if r.status_code == 206 and downloaded:
                        mode = "ab"
                    else:
                        # 服务端不支持 Range（或首次下载）：只能从头来
                        mode = "wb"
                        downloaded = 0

                    total_size = self._resolve_total_size(r, downloaded)
                    last_percentage = -1

                    with open(file_path, mode) as f:
                        for chunk in r.iter_content(chunk_size=8192):
                            if not chunk:
                                continue
                            if progress_dialog is not None and progress_dialog.is_cancelled():
                                raise _DownloadCancelled()
                            f.write(chunk)
                            downloaded += len(chunk)
                            if total_size > 0:
                                percentage = int(downloaded / total_size * 100)
                                if percentage != last_percentage and percentage <= 100:
                                    progress_dialog.update_progress(
                                        max(0, min(percentage, 100)),
                                        downloaded / (1024 * 1024),
                                        total_size / (1024 * 1024),
                                    )
                                    last_percentage = percentage
                return True, downloaded
            except _DownloadCancelled:
                raise
            except Exception as e:
                last_error = e
                logger.warning(
                    f"下载中断（第 {attempt}/{MAX_DOWNLOAD_ATTEMPTS} 次）: {e}，"
                    f"已收到 {downloaded / (1024 * 1024):.1f} MB"
                )
                if attempt < MAX_DOWNLOAD_ATTEMPTS:
                    time.sleep(RETRY_WAIT_SECONDS)

        raise last_error if last_error else RuntimeError("下载失败")

    @staticmethod
    def _resolve_total_size(response, downloaded):
        """算出完整文件大小：续传时要用 Content-Range 的总量，不能只看本次长度。"""
        content_range = response.headers.get("Content-Range", "")
        if "/" in content_range:
            try:
                return int(content_range.rsplit("/", 1)[1])
            except ValueError:
                pass
        try:
            length = int(response.headers.get("content-length", 0) or 0)
        except ValueError:
            length = 0
        return length + (downloaded if response.status_code == 206 else 0)

    def _download_from_url(self, download_url, filename, save_path=None,
                           progress_dialog=None, announce=False, kind="file"):
        """
        从URL下载文件到下载目录。
        若提供 progress_dialog 则复用该对话框，下载完成后只调用 reset()，
        最后由外部调用 close()。
        announce=True 时下载成功后自动在资源管理器中定位该文件（单文件流程用，
        多文件流程由调用方统一 announce 一次）。
        kind="folder" 表示这是发送端按需打包的目录，先 prepare 再下载。
        """
        file_path = self._target_path(filename, save_path)

        # 决定进度对话框：外部传入则复用，否则自己创建
        own_dialog = False
        if progress_dialog is None:
            progress_dialog = post_to_main_thread(DownloadProgressDialog, "下载进度")
            own_dialog = True

        progress_dialog.reset(filename, 0)
        try:
            if kind == "folder":
                if not self._prepare_folder_share(download_url, progress_dialog):
                    return False
                progress_dialog.reset(filename, 0)

            downloaded = 0
            try:
                _, downloaded = self._download_with_resume(
                    download_url, file_path, progress_dialog
                )
            except _DownloadCancelled:
                logger.info("用户取消下载")
                self._remove_partial(file_path)
                show_message("已取消", "文件下载已取消")
                return False

            progress_dialog.update_progress(
                100, downloaded / (1024 * 1024), downloaded / (1024 * 1024)
            )
            logger.info(f"文件从URL下载成功: {file_path}")
            if copy_files_to_clipboard([file_path]):
                logger.info(f"保存成功,文件已保存至:\n{file_path}")
            else:
                logger.info(f"文件保存至:\n{file_path}失败")
            if announce:
                self._announce_saved(file_path)
            return True
        except Exception as e:
            logger.error(f"从URL下载文件失败: {e}")
            show_message(
                "下载失败",
                f"下载失败: {e}\n已重试 {MAX_DOWNLOAD_ATTEMPTS} 次，请稍后再试",
            )
            self._remove_partial(file_path)
            return False
        finally:
            if own_dialog:
                progress_dialog.close()

    @staticmethod
    def _remove_partial(file_path):
        try:
            if os.path.exists(file_path):
                os.remove(file_path)
        except OSError as e:
            logger.warning(f"清理下载文件失败: {e}", exc_info=True)

    def _clear_latest_file(self, host, port):
        """
        通知服务器清理latest_file
        """
        try:
            clear_url = f"http://{host}:{port}/latest/clear"
            resp = requests.get(clear_url, timeout=3)
            if resp.status_code == 200:
                logger.info(f"成功通知中心服务器清理 latest_file")
            else:
                logger.warning(f"通知中心服务器返回非200: {resp.status_code}")
        except Exception as e:
            logger.error(f"通知中心服务器清理失败: {e}")

    def fetch_file_with_progress(self, suggested_filename="downloaded_file"):
        """
        点击通知后下载所有待拉取文件
        （兼容旧版单文件流 / 单文件下载链接 / 新版文件列表）
        """
        if threading.current_thread() is threading.main_thread():
            threading.Thread(target=self.fetch_file_with_progress,
                             args=(suggested_filename,),
                             daemon=True).start()
            return

        logger.info("开始拉取服务器文件...")
        server_host = self.config.server_host
        server_port = self.config.server_port
        url = f"http://{server_host}:{server_port}/request_file"
        try:
            resp = requests.post(
                url,
                headers={"key": self.config.key},
                json={"source": self.config.local_name},
                timeout=10,
                stream=True
            )
        except Exception as e:
            logger.error(f"请求服务器失败: {e}")
            show_message("请求失败", f"无法连接服务器: {e}")
            return

        if resp.status_code != 200:
            show_message("请求失败", f"HTTP {resp.status_code}")
            return

        content_type = resp.headers.get("Content-Type", "")
        # 1. 处理直接文件流
        if "application/octet-stream" in content_type or "attachment" in content_type:
            filename = parse_filename_from_cd(
                resp.headers.get("Content-Disposition", "")
            ) or suggested_filename
            self._save_file_stream(resp, filename)
            return

        # 2. 解析 JSON 响应
        try:
            data = resp.json()
        except Exception as e:
            logger.error(f"解析服务器响应失败: {e}")
            show_message("错误", "服务器响应格式异常")
            return

        # 2.1 单文件下载链接
        if data.get("status") == "download" and data.get("type") == "file":
            download_url = data.get("download_url")
            name = data.get("name", suggested_filename)
            if download_url:
                self._download_from_url(
                    download_url, name, announce=True,
                    kind=data.get("kind", "file"),
                )
            return

        # 多文件列表：直接存到配置的下载目录，不再先问「保存到哪」
        if data.get("type") == "file_list":
            files = data.get("files", [])
            if not files:
                return

            save_dir = self.config.save_path
            progress_dialog = post_to_main_thread(DownloadProgressDialog, "下载进度")
            all_completed = True
            saved_any = False
            try:
                for idx, file in enumerate(files, 1):
                    if progress_dialog.is_cancelled():
                        all_completed = False
                        break
                    name = file.get("name", f"file_{idx}")
                    total = len(files)
                    download_url = file.get("download_url")
                    if not download_url:
                        logger.warning(f"文件 {name} 无下载地址，跳过")
                        continue
                    logger.info(f"({idx}/{total}) 下载: {name}")
                    success = self._download_from_url(
                        download_url, name, save_path=save_dir,
                        progress_dialog=progress_dialog,
                        kind=file.get("kind", "file"),
                    )
                    if not success:
                        all_completed = False
                        break
                    saved_any = True
            finally:
                progress_dialog.close()

            if all_completed and not progress_dialog.is_cancelled():
                # 不再弹「下载完成」模态框：直接把资源管理器切到保存目录
                self._announce_saved(save_dir, count=total)
                self._clear_latest_file(server_host, server_port)
            elif saved_any:
                logger.info(
                    "部分文件未下载完成。服务端记录已在 request_file 时清空，"
                    "如需重试请在发送端重新复制一次共享内容"
                )
            return


class _DownloadCancelled(Exception):
    """用户在进度窗口点了取消。"""
