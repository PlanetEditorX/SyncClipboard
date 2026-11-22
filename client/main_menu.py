import os
import sys
import queue
import uuid
import time
import json
import logging
import zipfile
import requests
import threading
import pyperclip
import win32con
import win32api
import win32clipboard
from pathlib import Path
from datetime import datetime
from common.utils import SAFE_POST
from client.file_server import FOLDER_SHARE_TEMP_DIR
from common.sync_modes import (
    MODE_BOTH,
    describe_mode,
    is_receive_allowed,
    is_send_allowed,
    normalize_mode,
)
from gui.main_window import DEFAULT_FOLDER_ZIP_CONFIRM_GB
from server.core.text_tracker import TextTracker

logger = logging.getLogger("client")

# 压缩级别：1~9。文件夹里多是已压缩过的镜像/APK，用中等档位换取速度。
FOLDER_ZIP_COMPRESS_LEVEL = 6


def _as_bool(value, default):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() not in {"false", "0", "no", "off", ""}
    return default if value is None else bool(value)


def _as_float(value, default):
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def format_size(num_bytes):
    """把字节数变成便于阅读的 GB/MB 文本。"""
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit in ("B", "KB") else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def folder_size_bytes(folder_path):
    """统计目录内所有文件的总字节数（读不到的文件按 0 计）。"""
    total = 0
    for root, _dirs, names in os.walk(folder_path):
        for name in names:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                continue
    return total


def confirm_large_folder(folder_name, size_text, limit_gb):
    """原生确认框：目录超过上限时询问是否仍然压缩传输。

    客户端是独立进程（GUI 的子进程），没有现成的界面通道，这里直接用 Win32
    消息框；非 Windows 或无法弹框时按「否」处理，避免在无人值守时悄悄传大目录。
    """
    message = (
        f"目录「{folder_name}」大小为 {size_text}，"
        f"超过设定的上限 {limit_gb:g} GB。\n\n"
        "是否仍然压缩后传输？压缩包会在传输完成后自动删除。"
    )
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.MessageBoxW.restype = ctypes.c_int
        user32.MessageBoxW.argtypes = (
            wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.UINT,
        )
        MB_YESNO = 0x00000004
        MB_ICONWARNING = 0x00000030
        MB_DEFBUTTON2 = 0x00000100
        MB_TOPMOST = 0x00040000
        MB_SETFOREGROUND = 0x00010000
        IDYES = 6
        result = user32.MessageBoxW(
            None,
            message,
            "SyncClipboard · 目录超出上限",
            MB_YESNO | MB_ICONWARNING | MB_DEFBUTTON2 | MB_TOPMOST | MB_SETFOREGROUND,
        )
        return result == IDYES
    except Exception as exc:
        logger.error("目录超限确认框弹出失败，按取消处理: %s", exc)
        return False


def get_clipboard_sequence():
    """
    读取 Windows 剪贴板序列号（GetClipboardSequenceNumber）。

    每次剪贴板内容发生变化（用户 Ctrl+C，或程序调用 SetClipboardData）系统都会
    自增这个计数器。它是判断“这次变化是用户手动复制，还是我们自己注入的”的
    最可靠依据，比对比文本内容可靠得多 —— 用户重复复制同一段文字也能识别出来。
    非 Windows 平台返回 None，调用方会退回纯文本比较的老逻辑。
    """
    try:
        return int(win32clipboard.GetClipboardSequenceNumber())
    except Exception:
        return None


class SyncClient:
    """剪贴板同步客户端：推送本地变化 + 拉取远程最新"""
    def __init__(self, config, file_server=None):
        self.server_url = f"http://{config['server_host']}:{config['server_port']}"
        self.key = config["key"]
        self.local_name = config["local_name"]
        self.last_text = ""
        self.running = False
        self.last_remote_id = None
        self._last_remote_content = None
        # ---- 同步开关（面板可调）----
        self.text_sync_mode = normalize_mode(config.get("text_sync_mode"))
        self.file_sync_mode = normalize_mode(config.get("file_sync_mode"))
        self.folder_sync_enabled = _as_bool(config.get("folder_sync_enabled"), True)
        # 目录体积超过这个值就弹窗询问，未超过直接后台打包
        self.folder_zip_confirm_bytes = int(
            _as_float(config.get("folder_zip_confirm_gb"), DEFAULT_FOLDER_ZIP_CONFIRM_GB)
            * 1024 ** 3
        )
        # 最近一次“已处理过”的剪贴板序列号，用于精确识别手动复制
        self._last_clipboard_seq = None
        self.push_thread = None
        self.pull_thread = None
        # 文件服务
        self.file_server = file_server
        # 文件推送队列：文件夹要先压缩，耗时不可控，放到后台线程按“最新优先”处理，
        # 避免卡住剪贴板轮询。队列长度 1，新内容直接顶掉还没开始的旧任务。
        self._file_push_queue = queue.Queue(maxsize=1)
        self._file_push_thread = None
        # 全局锁，避免同时读写剪贴板
        self.clipboard_lock = threading.Lock()
        self.tracker = TextTracker()
        if self.file_server and hasattr(self.file_server, 'set_remote_text_handler'):
            self.file_server.set_remote_text_handler(self.apply_remote_text)

    def safe_paste(self, retries=5):
        for _ in range(retries):
            try:
                return pyperclip.paste()
            except Exception:
                time.sleep(0.05)
        return ""

    def start(self):
        self.running = True
        self.last_text = self.safe_paste()
        self._last_clipboard_seq = get_clipboard_sequence()
        logger.info("客户端剪贴板监听启动")

        # 推送线程
        self.push_thread = threading.Thread(target=self._push_loop, daemon=True)
        self.push_thread.start()

        # 文件推送线程（压缩文件夹可能耗时，独立成线程）
        self._file_push_thread = threading.Thread(
            target=self._file_push_worker, daemon=True
        )
        self._file_push_thread.start()

        # self.pull_thread = threading.Thread(target=self._pull_loop, daemon=True)
        # self.pull_thread.start()

    def _push_loop(self):
        self.last_text = self.safe_paste() or ""
        self.last_file_set = None
        logger.info(
            f"同步方向: 文字={describe_mode(self.text_sync_mode)}, "
            f"文件={describe_mode(self.file_sync_mode)}"
        )
        while self.running:
            try:
                files = self.get_clipboard_files()
                if files:
                    if not is_send_allowed(self.file_sync_mode):
                        # 文件发送已关闭：剪贴板里是文件时什么都不推，也别把路径当文字发出去
                        time.sleep(1)
                        continue
                    current_set = frozenset(files)
                    if current_set != self.last_file_set:
                        self.last_file_set = current_set
                        self._enqueue_file_push(files)
                    time.sleep(1)
                    continue

                self.last_file_set = None
                text_to_push = self._get_local_text_to_push()
                # 只接收模式下仍要走完状态更新，免得重新打开发送时把旧内容补发出去
                self._push_text_if_allowed(text_to_push)
            except Exception as e:
                logger.error(f"监听异常: {e}")
            time.sleep(0.5)

    def _push_text_if_allowed(self, text):
        """按文字同步方向决定是否上传；不合法方向返回 False。"""
        if text is None:
            return False
        if not is_send_allowed(self.text_sync_mode):
            return False
        self.push_text(text)
        return True

    def _enqueue_file_push(self, file_paths):
        """把一批文件交给后台线程推送；只保留最新一批，避免积压。"""
        if not is_send_allowed(self.file_sync_mode):
            logger.info("文件发送已关闭，忽略本次文件推送")
            return False
        paths = list(file_paths)
        while True:
            try:
                self._file_push_queue.put_nowait(paths)
                return
            except queue.Full:
                # 丢旧留新：用户又复制了新东西，还在排队的旧任务已无意义
                try:
                    dropped = self._file_push_queue.get_nowait()
                except queue.Empty:
                    continue
                logger.info(
                    f"文件推送队列已更新，放弃排队的旧内容（{len(dropped)} 项）"
                )

    def _file_push_worker(self):
        while True:
            try:
                paths = self._file_push_queue.get(timeout=1)
            except queue.Empty:
                if not self.running:
                    break
                continue
            if paths is None:
                break
            try:
                self._push_latest_file(paths)
            except Exception:
                logger.exception("文件推送失败")
            finally:
                self._file_push_queue.task_done()

    def _get_local_text_to_push(self):
        """原子地读取剪贴板并判断这次变化是否需要上传。"""
        with self.clipboard_lock:
            # 溯源第一层：剪贴板序列号没变 → 这一轮没有任何复制动作，直接跳过。
            # 序列号变了才可能是用户手动复制，也可能是别的程序（含本机服务端）
            # 写入剪贴板，因此后面还要再做一次内容/回音判断。
            seq = get_clipboard_sequence()
            if seq is not None:
                if seq == self._last_clipboard_seq:
                    return None
                self._last_clipboard_seq = seq

            text = self.safe_paste() or ""
            if text == self.last_text:
                return None

            is_remote_echo = text == self._last_remote_content
            self.last_text = text
            # 远程内容标记只用于抑制紧随其后的那一次剪贴板变化。
            self._last_remote_content = None

        if not text or is_remote_echo:
            return None
        return text

    def apply_remote_text(self, latest):
        """应用服务器文字，并同步更新上传监听状态以阻断回环。"""
        if not is_receive_allowed(self.text_sync_mode):
            logger.info("文字接收已关闭，忽略远端文字")
            return False
        if not isinstance(latest, dict):
            return False

        item_id = latest.get("id")
        content = latest.get("content")
        source = latest.get("source")
        if not item_id or content is None or source == self.local_name:
            return False
        if item_id == self.last_remote_id or self.tracker.is_duplicate(item_id):
            return False

        with self.clipboard_lock:
            # 剪贴板里已经是这份内容（本地刚复制过，或这是服务器把同一段文字
            # 又推回来一次的回音）：不重复写入，也不再产生“新内容”提示。
            current = self.safe_paste() or ""
            if current == content:
                self.last_remote_id = item_id
                self.last_text = content
                self._last_remote_content = None
                self._last_clipboard_seq = get_clipboard_sequence()
                return False

            pyperclip.copy(content)
            self.last_remote_id = item_id
            self._last_remote_content = content
            self.last_text = content
            # 记录注入后的序列号：这次写入是我们自己干的，不该再被当成手动复制上传
            self._last_clipboard_seq = get_clipboard_sequence()

        self.tracker.update(latest)
        logger.info(f"更新剪贴板: {content[:50]} (来自 {source})")
        return True

    def push_text(self, text):
        try:
            latest_global = self.tracker.get_global_latest()
            if latest_global is None or latest_global["content"] != text:
                resp = SAFE_POST(
                    f"{self.server_url}/text_sync",
                    json={
                        "key": self.key,
                        "content": text,
                        "source": self.local_name
                    },
                    timeout=30
                )
                if resp.status_code == 200:
                    logger.info(f"推送成功: {text[:50]}...")
                else:
                    logger.warning(f"推送失败: {resp.status_code} {resp.text}")
        except Exception:
            logger.exception("连接服务端失败")

    def _push_latest_file(self, file_paths):
        """
        推送最新的文件
        """
        if not file_paths:
            logger.warning("没有文件需要推送")
            return

        if not is_send_allowed(self.file_sync_mode):
            logger.info("文件发送已关闭，忽略本次文件推送")
            return

        # 先把可推送的内容筛出来，再动共享列表：
        # 复制的是空目录或已被删除的路径时，不能顺手清掉上一次有效共享。
        files = []
        folders = []
        skipped = []
        for path in file_paths:
            if os.path.isfile(path):
                files.append(path)
                continue
            sanitized = path.encode('utf-8', 'surrogatepass').decode('utf-8', 'replace')
            if os.path.isdir(path):
                folders.append(path)
            else:
                logger.warning(f"文件不存在，跳过: {sanitized}")
                skipped.append(sanitized)

        # 文件夹没有直接共享的能力，先打包成 zip（多个文件夹各自一个包）
        entries = []  # [(本地路径, 展示名, 是否临时文件)]
        for path in files:
            entries.append((path, os.path.basename(path), False))
        folder_skipped = 0
        for folder in folders:
            if not self._folder_ready_for_zip(folder):
                folder_skipped += 1
                continue
            zip_path = self._zip_folder(folder)
            if zip_path is None:
                folder_skipped += 1
                continue
            folder_name = os.path.basename(os.path.normpath(folder)) or "folder"
            entries.append((str(zip_path), f"{folder_name}.zip", True))

        if not entries:
            # 整批都无效：不发请求，也不清空共享列表。
            # 服务端对空 file_list 一律返回 400，本机共享列表也没必要跟着清。
            logger.warning(
                f"剪贴板中没有可共享的内容，本次不推送"
                f"（跳过 {len(skipped)} 项，{folder_skipped} 个文件夹未打包）"
            )
            return

        file_list = []
        # 推送前先清空之前的共享（上一批的临时压缩包也会一起删掉）
        if hasattr(self, 'file_server') and self.file_server:
            self.file_server.clear_files()

        for path, name, is_temp in entries:
            try:
                size = os.path.getsize(path)
            except OSError as e:
                logger.warning(f"读取文件大小失败，跳过: {name}, 错误: {e}")
                continue
            file_id = str(uuid.uuid4())
            # 注册到 FileServer
            if hasattr(self, 'file_server') and self.file_server:
                self.file_server.register_file(file_id, path, temporary=is_temp)
            file_list.append({
                "file_id": file_id,
                "path": path,
                "name": name,
                "size": size
            })

        if not file_list:
            logger.warning("没有可共享的文件，本次不推送")
            return

        # 用于日志的文件摘要，避免依赖循环里最后一次赋值的局部变量
        if len(file_list) == 1:
            label = f"{file_list[0]['name']} ({file_list[0]['size']} bytes)"
        else:
            label = f"{file_list[0]['name']} 等 {len(file_list)} 个文件"

        try:
            resp = requests.post(
                f"{self.server_url}/file_sync",
                headers={"key": self.key},
                json={
                    "file_list": file_list,
                    "source": getattr(self, 'local_name', 'unknown'),
                    "port": getattr(self.file_server, 'port', None)
                },
                timeout=10
            )

            if resp.status_code == 200:
                logger.info(f"文件路径已同步: {label}")
                return
            logger.error(f"同步失败: {label}, 状态码: {resp.status_code}, 内容: {resp.text}")
        except Exception as e:
            logger.error(f"同步文件路径失败: {label}, 错误: {e}")

        # 同步没成功：这批共享对方根本看不到，撤销登记并删掉临时压缩包，别占磁盘
        if hasattr(self, 'file_server') and self.file_server:
            for item in file_list:
                self.file_server.unregister_file(item["file_id"])

    def _folder_ready_for_zip(self, folder_path):
        """判断这个目录要不要打包上传：关闭 / 超限未确认都返回 False。"""
        if not self.folder_sync_enabled:
            logger.warning(f"目录同步已关闭，跳过: {folder_path}")
            return False

        size = folder_size_bytes(folder_path)
        size_text = format_size(size)
        limit_bytes = self.folder_zip_confirm_bytes
        limit_gb = limit_bytes / 1024 ** 3

        if limit_bytes > 0 and size > limit_bytes:
            folder_name = os.path.basename(os.path.normpath(folder_path)) or "文件夹"
            logger.info(
                f"目录 {folder_name} 体积 {size_text}，超过上限 {limit_gb:g} GB，等待用户确认"
            )
            approved = confirm_large_folder(folder_name, size_text, limit_gb)
            logger.info(
                f"目录超限确认结果: {'同意压缩传输' if approved else '用户取消，本次不传输'}"
            )
            return approved

        logger.info(f"目录 {folder_path} 体积 {size_text}，在限额内，开始自动打包")
        return True

    def _zip_folder(self, folder_path):
        """把文件夹打包成临时 zip，返回压缩包路径；失败返回 None。

        压缩包放在 FOLDER_SHARE_TEMP_DIR，并登记到 FileServer 的临时文件集合，
        共享结束（对方下载完 / 取消共享 / 过期 / 退出）时由 FileServer 删除。
        """
        folder_path = os.path.normpath(folder_path)
        # 复制整个盘符根目录时 basename 为空，退回用盘符当目录名
        folder_name = (
            os.path.basename(folder_path)
            or os.path.splitdrive(folder_path)[0].rstrip(":\\/")
            or "folder"
        )
        archive_name = f"{uuid.uuid4().hex[:12]}_{folder_name}.zip"

        try:
            FOLDER_SHARE_TEMP_DIR.mkdir(parents=True, exist_ok=True)
            zip_path = FOLDER_SHARE_TEMP_DIR / archive_name
        except OSError as e:
            logger.error(f"无法创建压缩包目录: {FOLDER_SHARE_TEMP_DIR}, 错误: {e}")
            return None

        started = time.time()
        last_report = started
        file_count = 0
        raw_size = 0
        logger.info(f"开始打包文件夹: {folder_path}")

        try:
            with zipfile.ZipFile(
                zip_path, "w", zipfile.ZIP_DEFLATED,
                compresslevel=FOLDER_ZIP_COMPRESS_LEVEL,
            ) as zf:
                for root, _dirs, names in os.walk(folder_path):
                    # 相对被复制目录自身计算，包内统一保留一层同名根目录
                    rel = os.path.relpath(root, folder_path)
                    arc_root = folder_name if rel == "." else os.path.join(folder_name, rel)
                    # 目录条目一并写入，保留空目录结构
                    try:
                        zf.write(root, arc_root + os.sep)
                    except OSError as e:
                        logger.warning(f"打包跳过无法读取的目录: {root}, 错误: {e}")
                    for name in names:
                        full_path = os.path.join(root, name)
                        arcname = os.path.join(arc_root, name)
                        try:
                            zf.write(full_path, arcname)
                            file_count += 1
                            raw_size += os.path.getsize(full_path)
                        except OSError as e:
                            # 单个文件被独占/权限不足不应让整包失败
                            logger.warning(f"打包跳过无法读取的文件: {full_path}, 错误: {e}")
                    if time.time() - last_report >= 3:
                        logger.info(
                            f"打包中: {folder_name} 已处理 {file_count} 个文件, "
                            f"{raw_size / 1048576:.1f} MB"
                        )
                        last_report = time.time()
        except Exception as e:
            logger.error(f"文件夹打包失败: {folder_path}, 错误: {e}")
            self._remove_temp_zip(zip_path)
            return None

        try:
            zip_size = zip_path.stat().st_size
        except OSError:
            zip_size = 0

        logger.info(
            f"打包完成: {folder_name}.zip, {file_count} 个文件, "
            f"{raw_size / 1048576:.1f} MB -> {zip_size / 1048576:.1f} MB, "
            f"耗时 {time.time() - started:.1f}s"
        )
        return zip_path

    def _remove_temp_zip(self, zip_path):
        """删除打包失败时残留的压缩包。"""
        try:
            if os.path.isfile(zip_path):
                os.remove(zip_path)
                logger.info(f"已清理未完成的压缩包: {zip_path}")
        except OSError as e:
            logger.warning(f"清理压缩包失败: {zip_path}, 错误: {e}")

    def _pull_loop(self):
        while self.running:
            try:
                resp = requests.get(
                    f"{self.server_url}/latest?source={self.local_name}",
                    headers={
                        "key": self.key
                    },
                    timeout=5
                )
                if resp.status_code == 200:
                    data = resp.json()
                    latest = data.get("latest_global")
                    if self.apply_remote_text(latest):
                        logger.info(
                            f"拉取并更新剪贴板: {latest['content'][:50]} "
                            f"(来自 {latest['source']})"
                        )
            except Exception as e:
                logger.error(f"拉取失败: {e} 等待10秒后重试")
                time.sleep(7)
            time.sleep(3)

    def stop(self):
        self.running = False
        # 唤醒并结束文件推送线程
        try:
            self._file_push_queue.put_nowait(None)
        except queue.Full:
            try:
                self._file_push_queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self._file_push_queue.put_nowait(None)
            except queue.Full:
                pass
        logger.info("客户端已停止")

    def get_clipboard_files(self):
        """
        获取剪贴板中的文件路径列表。
        如果剪贴板中包含从资源管理器复制的文件，则返回文件路径列表（仅包含有效的、存在的路径）；
        否则返回 None。
        """
        try:
            win32clipboard.OpenClipboard()
            if not win32clipboard.IsClipboardFormatAvailable(win32con.CF_HDROP):
                return None

            hdrop = win32clipboard.GetClipboardData(win32con.CF_HDROP)

            # ---- 情况1: 如果返回的是元组/列表（直接是文件路径列表） ----
            if isinstance(hdrop, (list, tuple)):
                # 过滤出存在的有效路径（避免乱码或无效内容）
                valid_paths = [p for p in hdrop if isinstance(p, str) and os.path.exists(p)]
                return valid_paths if valid_paths else None

            # ---- 情况2: 返回的是 PyHANDLE 或整数句柄 ----
            try:
                hdrop_int = int(hdrop)   # 尝试转为整数句柄
            except (TypeError, ValueError):
                # 如果转换失败，说明类型不支持，直接返回 None
                return None

            file_count = win32api.DragQueryFile(hdrop_int, -1)
            if file_count == 0:
                return None

            file_paths = []
            for i in range(file_count):
                path = win32api.DragQueryFile(hdrop_int, i)
                if path and isinstance(path, str) and os.path.exists(path):
                    file_paths.append(path)
            return file_paths if file_paths else None

        except Exception as e:
            print(f"获取剪贴板文件失败: {e}", file=sys.stderr)
            return None
        finally:
            try:
                win32clipboard.CloseClipboard()
            except Exception as e:
                print(f"关闭剪贴板失败: {e}", file=sys.stderr)
