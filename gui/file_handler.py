import os
import time
import logging
import requests
import threading
from common import ui_bridge
from common.utils import (
    post_to_main_thread,
    show_message,
    copy_files_to_clipboard,
    parse_filename_from_cd,
    reveal_in_explorer,
    normalize_local_path,
)
from common.notification import show_notification_with_click
from gui.qt_download_dialog import DownloadProgressDialog

logger = logging.getLogger("gui")

# 下载中断自动重试次数（含首次）。发送端在传输失败时会保留共享，
# 所以这里的重试能真正接上；续传用 HTTP Range，不必从头再来。
MAX_DOWNLOAD_ATTEMPTS = 3
RETRY_WAIT_SECONDS = 2

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
        # 与手机上传走同一套分隔符规范：配置写 D:/Downloads 时，
        # 落盘路径、日志和「打开所在文件夹」看到的都是原生反斜杠
        file_path = normalize_local_path(
            os.path.join(folder, filename or "downloaded_file")
        )
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

            logger.info(f"文件已保存: {file_path} ({downloaded} 字节)")

            if copy_files_to_clipboard([file_path]):
                logger.info(f"保存成功文件已保存至:\n{file_path}")
            else:
                logger.info(f"文件保存至:\n{file_path}失败")
            self._announce_saved(file_path)

        except Exception as e:
            logger.error(f"保存文件失败: {e}")
            show_message("保存失败", f"无法保存文件: {e}")
            try:
                if os.path.exists(file_path):
                    os.remove(file_path)
            except OSError as e:
                logger.warning(f"清理下载文件失败: {e}", exc_info=True)
        finally:
            # 成功 / 失败 / 用户取消都要关掉进度窗，否则它会一直挂在屏幕上
            progress.close()
            response.close()

    def _fetch_manifest(self, manifest_url):
        """取目录清单；失败返回 None（调用方据此放弃这一项）。"""
        if not manifest_url:
            show_message("目录不可用", "发送端没有给出目录清单地址，请重新复制一次")
            return None
        try:
            resp = requests.get(
                manifest_url,
                headers={"key": self.config.key},
                timeout=(CONNECT_TIMEOUT, READ_TIMEOUT))
            data = resp.json() if resp.content else {}
        except Exception as e:
            logger.error(f"获取目录清单失败: {e}")
            show_message("获取目录失败", f"无法从发送端取目录清单: {e}")
            return None
        if data.get("status") != "ok":
            logger.error("目录清单无效: %s", data.get("message"))
            show_message(
                "目录不可用",
                data.get("message") or "发送端返回的目录清单不可用，请重新复制一次",
            )
            return None
        if data.get("notice"):
            logger.warning("目录清单提示: %s", data["notice"])
            # 清单被截断时只附带一句 notice，接收端必须让用户看见「没传完」，
            # 否则会误以为整个目录已经同步完毕。
            show_message("目录传输不完整", data["notice"])
        return data

    def _download_folder(self, file, save_dir, progress_dialog):
        """目录共享：按清单逐文件下载，在本地重建目录结构。

        目录不再打包 —— 接收端拿到的是清单（相对路径 + 每项的下载地址），
        这里逐项取回并按 `save_dir/<目录名>/<相对路径>` 落盘。相比整包下载，
        好处是结构完整、单个文件失败不影响其余文件，也能显示细粒度进度。
        返回 (是否全部成功, 已下文件数)。
        """
        data = self._fetch_manifest(file.get("manifest_url"))
        if data is None:
            return False, 0

        entries = data.get("entries") or []
        root = data.get("root") or file.get("name") or "folder"
        root_dir = self._target_path(root, save_dir)
        try:
            os.makedirs(root_dir, exist_ok=True)
        except OSError as e:
            logger.error("无法创建目录: %s, 错误: %s", root_dir, e)
            show_message("保存失败", f"无法创建目录: {root_dir}\n{e}")
            return False, 0

        if not entries:
            logger.info("目录为空，只创建目录: %s", root_dir)
            if data.get("empty_dirs"):
                logger.info("清单里还有空目录，接收端不重建: %s",
                            data["empty_dirs"])
            return True, 0

        total = len(entries)
        done = 0
        for idx, entry in enumerate(entries, 1):
            if progress_dialog is not None and progress_dialog.is_cancelled():
                logger.info("用户取消目录下载: 已完成 %s/%s", done, total)
                return False, done

            rel = (entry.get("path") or entry.get("name") or "").replace("\\", "/")
            url = entry.get("url")
            if not rel or not url:
                logger.warning("清单项缺路径或地址，跳过: %s", entry)
                continue

            parts = [p for p in rel.split("/") if p and p != "."]
            if not parts or ".." in parts:
                logger.warning("清单项路径可疑，跳过: %s", rel)
                continue

            target = os.path.join(root_dir, *parts)
            try:
                os.makedirs(os.path.dirname(target), exist_ok=True)
            except OSError as e:
                logger.error("创建子目录失败: %s, 错误: %s", target, e)
                return False, done

            if progress_dialog is not None:
                progress_dialog.reset(f"[{idx}/{total}] {rel}", 0)
            try:
                self._download_with_resume(url, target, progress_dialog)
            except _DownloadCancelled:
                logger.info("用户取消下载")
                self._remove_partial(target)
                return False, done
            except Exception as e:
                logger.error(f"文件下载失败，跳过: {rel} ({e})")
                self._remove_partial(target)
                continue
            done += 1

        logger.info("目录下载完成: %s 共 %s/%s 个文件 -> %s",
                    root, done, total, root_dir)
        return done == total, done

    def _download_with_resume(self, download_url, file_path, progress_dialog):
        """带断线重试与断点续传的下载；返回 (是否成功, 已收字节数)。"""
        downloaded = 0
        last_error = None

        for attempt in range(1, MAX_DOWNLOAD_ATTEMPTS + 1):
            # 发送端 FileServer 已全面鉴权，下载地址也必须带 key；
            # Range 只是附加头，不能把 key 挤掉。
            headers = {"key": self.config.key}
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
                           progress_dialog=None, announce=False):
        """
        从URL下载文件到下载目录。
        若提供 progress_dialog 则复用该对话框，下载完成后只调用 reset()，
        最后由外部调用 close()。
        announce=True 时下载成功后自动在资源管理器中定位该文件（单文件流程用，
        多文件流程由调用方统一 announce 一次）。

        目录共享不走这里，见 `_download_folder`：目录按清单逐文件下载。
        """
        file_path = self._target_path(filename, save_path)

        # 决定进度对话框：外部传入则复用，否则自己创建
        own_dialog = False
        if progress_dialog is None:
            progress_dialog = post_to_main_thread(DownloadProgressDialog, "下载进度")
            own_dialog = True

        progress_dialog.reset(filename, 0)
        try:
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
            # 服务端 /latest/clear 已加鉴权：不带 key 会被 403，且共享记录
            # 不会被清掉（下次拉取重复投递）。
            resp = requests.get(
                clear_url,
                headers={"key": self.config.key},
                timeout=3,
            )
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
            # 目录项现在只给 manifest_url（不再有必然 409 的 download_url）
            if data.get("kind") == "folder" and data.get("manifest_url"):
                self._download_folder(data, self.config.save_path, None)
            elif data.get("download_url"):
                self._download_from_url(
                    data["download_url"], name, announce=True)
            return

        # 多文件列表：直接存到配置的下载目录，不再先问「保存到哪」
        # 服务端在只共享一个目录时返回 type=folder，结构与 file_list 完全一致，
        # 这里一并处理；目录项走清单逐文件下载，普通文件直接取。
        if data.get("type") in ("file_list", "folder"):
            files = data.get("files", [])
            if not files:
                return

            save_dir = self.config.save_path
            progress_dialog = post_to_main_thread(DownloadProgressDialog, "下载进度")
            all_completed = True
            saved_count = 0
            try:
                for idx, file in enumerate(files, 1):
                    if progress_dialog.is_cancelled():
                        all_completed = False
                        break
                    name = file.get("name", f"file_{idx}")
                    total = len(files)
                    if file.get("kind") == "folder":
                        logger.info(f"({idx}/{total}) 目录: {name}")
                        ok, done = self._download_folder(
                            file, save_dir, progress_dialog)
                        saved_count += done
                        if not ok:
                            all_completed = False
                            break
                        continue
                    download_url = file.get("download_url")
                    if not download_url:
                        logger.warning(f"文件 {name} 无下载地址，跳过")
                        continue
                    logger.info(f"({idx}/{total}) 下载: {name}")
                    success = self._download_from_url(
                        download_url, name, save_path=save_dir,
                        progress_dialog=progress_dialog,
                    )
                    if not success:
                        all_completed = False
                        break
                    saved_count += 1
            finally:
                progress_dialog.close()

            if all_completed and not progress_dialog.is_cancelled():
                # 不再弹「下载完成」模态框：直接把资源管理器切到保存目录
                self._announce_saved(save_dir, count=saved_count)
                self._clear_latest_file(server_host, server_port)
            elif saved_count:
                logger.info(
                    "部分文件未下载完成（已保存 %s 个）。服务端记录已在 "
                    "request_file 时清空，如需重试请在发送端重新复制一次共享内容",
                    saved_count,
                )
            return


class _DownloadCancelled(Exception):
    """用户在进度窗口点了取消。"""
