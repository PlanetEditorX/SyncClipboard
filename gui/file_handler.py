import os
import threading
import logging
import requests
import tkinter as tk
from tkinter import filedialog
from common.utils import copy_files_to_clipboard, show_message, parse_filename_from_cd
from gui.download_dialog import DownloadProgressDialog

logger = logging.getLogger("gui")


class FileHandler:
    """文件下载处理器"""
    def __init__(self, config_manager):
        self.config = config_manager

    def _ask_save_path(self, filename):
        """弹出保存对话框，获取保存路径"""
        result = [None]

        def ask():
            root = tk.Tk()
            root.withdraw()
            file_path = filedialog.asksaveasfilename(
                title="保存文件",
                initialdir=self.config.last_dir,
                initialfile=filename,
                defaultextension="",
                filetypes=[("所有文件", "*.*")]
            )
            root.destroy()
            result[0] = file_path
            if file_path:
                self.config.last_dir = os.path.dirname(file_path)
                self.config.save_client_config()

        t = threading.Thread(target=ask)
        t.start()
        t.join()
        return result[0]

    def _save_file_stream(self, response, filename):
        """流式接收文件并写入磁盘"""
        file_path = self._ask_save_path(filename)
        if not file_path:
            logger.info("用户取消保存文件")
            response.close()
            return

        progress = DownloadProgressDialog("下载进度")

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
                            except:
                                pass
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
                show_message("保存成功", f"文件已保存至:\n{file_path}\n路径已复制到剪贴板")
            else:
                show_message("保存成功", f"文件已保存至:\n{file_path}")

        except Exception as e:
            progress.close()
            logger.error(f"保存文件失败: {e}")
            show_message("保存失败", f"无法保存文件: {e}")
            try:
                if os.path.exists(file_path):
                    os.remove(file_path)
            except:
                pass
        finally:
            response.close()

    def _download_from_url(self, download_url, filename):
        """从URL下载文件"""
        file_path = self._ask_save_path(filename)
        if not file_path:
            return

        progress = DownloadProgressDialog("下载进度")

        try:
            with requests.get(download_url, stream=True, timeout=30) as r:
                r.raise_for_status()
                total_size = int(r.headers.get('content-length', 0))
                downloaded = 0
                last_percentage = -1

                with open(file_path, 'wb') as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        if chunk:
                            if progress.is_cancelled():
                                f.close()
                                try:
                                    os.remove(file_path)
                                except:
                                    pass
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

            logger.info(f"文件从URL下载成功: {file_path}")
            if copy_files_to_clipboard([file_path]):
                show_message("保存成功", f"文件已保存至:\n{file_path}\n路径已复制到剪贴板")
            else:
                show_message("保存成功", f"文件已保存至:\n{file_path}")

        except Exception as e:
            progress.close()
            logger.error(f"从URL下载文件失败: {e}")
            show_message("下载失败", f"下载失败: {e}")
            try:
                if os.path.exists(file_path):
                    os.remove(file_path)
            except:
                pass

    def fetch_file(self):
        """获取文件（托盘菜单回调）"""
        logger.info("用户点击『获取文件』")
        url = f"http://{self.config.server_host}:{self.config.server_port}/request_file"

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

        if resp.status_code == 200:
            content_type = resp.headers.get("Content-Type", "")
            content_disposition = resp.headers.get("Content-Disposition", "")

            is_file = ("application/octet-stream" in content_type or
                      "application/x-msdownload" in content_type or
                      "attachment" in content_disposition)

            if is_file:
                filename = parse_filename_from_cd(content_disposition) or "downloaded_file"
                self._save_file_stream(resp, filename)
                return

            try:
                data = resp.json()
            except Exception:
                logger.error("响应既不是文件也不是合法JSON")
                show_message("错误", "服务器返回格式无法识别")
                return

            if data.get("status") == "download" and data.get("type") == "file":
                self._download_from_url(data.get("download_url"), data.get("name", "downloaded_file"))
            elif data.get("type") == "text":
                import pyperclip
                latest = data.get("latest_global")
                if latest and latest.get("content"):
                    pyperclip.copy(latest["content"])
                    logger.info(f"获取文本成功: {latest['content'][:50]}")
                    show_message("获取成功", "文本已复制到剪贴板")
                else:
                    show_message("无内容", "服务器没有可用的文本")
            else:
                show_message("未知响应", "服务器返回格式无法识别")
        elif resp.status_code == 302:
            location = resp.headers.get("Location")
            if location:
                self._download_from_url(location, "downloaded_file")
            else:
                show_message("错误", "服务器重定向错误")
        else:
            show_message("请求失败", f"HTTP {resp.status_code}")

    def fetch_file_with_progress(self, suggested_filename="downloaded_file"):
        """
        专门用于通知点击下载的方法
        这个方法是为了兼容 ClipboardHandler 的回调
        """
        logger.info(f"开始下载文件: {suggested_filename}")

        url = f"http://{self.config.server_host}:{self.config.server_port}/request_file"
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

        if resp.status_code == 200:
            content_type = resp.headers.get("Content-Type", "")
            content_disposition = resp.headers.get("Content-Disposition", "")

            is_file = ("application/octet-stream" in content_type or
                      "application/x-msdownload" in content_type or
                      "attachment" in content_disposition)

            if is_file:
                filename = parse_filename_from_cd(content_disposition) or suggested_filename
                self._save_file_stream(resp, filename)
            else:
                try:
                    data = resp.json()
                    if data.get("status") == "download" and data.get("type") == "file":
                        self._download_from_url(data.get("download_url"), data.get("name", suggested_filename))
                    else:
                        show_message("错误", "服务器返回了非文件响应")
                except Exception as e:
                    logger.error(f"解析服务器响应失败: {e}")
                    show_message("错误", "服务器响应格式异常")
        else:
            show_message("请求失败", f"HTTP {resp.status_code}")