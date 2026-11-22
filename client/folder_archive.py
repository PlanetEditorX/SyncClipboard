"""目录打包：把文件夹压缩成 zip，供客户端按需共享。

打包是「按需」的：复制文件夹时只统计体积并发一条共享记录，等接收端真正来取
（`/prepare` 预打包或直接 GET）时才压缩。这样本地只是复制一个大目录、并不需要
传给别的设备时，不会白白消耗 CPU 和临时磁盘。
"""

import logging
import os
import time
import uuid
import zipfile
from pathlib import Path

logger = logging.getLogger("client")

# 压缩级别：1~9。文件夹里多是已压缩过的镜像/APK，用中等档位换取速度。
FOLDER_ZIP_COMPRESS_LEVEL = 6

# 打包进度上报间隔（秒）
PROGRESS_INTERVAL = 3


class FolderArchiveCancelled(RuntimeError):
    """共享在打包过程中被撤销（换共享 / 过期 / 退出）。"""


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


def folder_display_name(folder_path):
    """目录名；复制盘符根目录时退回用盘符当名字。"""
    folder_path = os.path.normpath(folder_path)
    return (
        os.path.basename(folder_path)
        or os.path.splitdrive(folder_path)[0].rstrip(":\\/")
        or "folder"
    )


def zip_folder(folder_path, dest_dir, progress=None, cancel_event=None):
    """把 folder_path 压成 dest_dir 里的临时 zip，返回 Path。

    包内保留一层同名根目录（解压出来不会散成一堆文件），空目录也写入。
    单个文件读不了只跳过，不影响整包；整体失败会删掉半成品再抛出异常。

    Parameters
    ----------
    progress : callable | None
        接收一句可读进度文本，用于 `/prepare` 轮询展示。
    cancel_event : threading.Event | None
        置位后中止打包并删除半成品（共享已被撤销时用）。
    """
    folder_path = os.path.normpath(folder_path)
    folder_name = folder_display_name(folder_path)
    archive_name = f"{uuid.uuid4().hex[:12]}_{folder_name}.zip"

    dest_dir = Path(dest_dir)
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise OSError(f"无法创建压缩包目录 {dest_dir}: {e}") from e

    zip_path = dest_dir / archive_name
    started = time.time()
    last_report = started
    file_count = 0
    raw_size = 0
    logger.info("开始打包文件夹: %s", folder_path)

    def _check_cancel():
        if cancel_event is not None and cancel_event.is_set():
            raise FolderArchiveCancelled(folder_path)

    try:
        with zipfile.ZipFile(
            zip_path, "w", zipfile.ZIP_DEFLATED,
            compresslevel=FOLDER_ZIP_COMPRESS_LEVEL,
        ) as zf:
            for root, _dirs, names in os.walk(folder_path):
                _check_cancel()
                # 相对被复制目录自身计算，包内统一保留一层同名根目录
                rel = os.path.relpath(root, folder_path)
                arc_root = folder_name if rel == "." else os.path.join(folder_name, rel)
                # 目录条目一并写入，保留空目录结构
                try:
                    zf.write(root, arc_root + os.sep)
                except OSError as e:
                    logger.warning("打包跳过无法读取的目录: %s, 错误: %s", root, e)
                for name in names:
                    _check_cancel()
                    full_path = os.path.join(root, name)
                    arcname = os.path.join(arc_root, name)
                    try:
                        zf.write(full_path, arcname)
                        file_count += 1
                        raw_size += os.path.getsize(full_path)
                    except OSError as e:
                        # 单个文件被独占/权限不足不应让整包失败
                        logger.warning("打包跳过无法读取的文件: %s, 错误: %s", full_path, e)
                if time.time() - last_report >= PROGRESS_INTERVAL:
                    last_report = time.time()
                    note = f"已处理 {file_count} 个文件, {raw_size / 1048576:.1f} MB"
                    logger.info("打包中: %s %s", folder_name, note)
                    if progress is not None:
                        try:
                            progress(note)
                        except Exception:  # noqa: BLE001 - 进度回调不该影响打包
                            logger.debug("打包进度回调失败", exc_info=True)
    except FolderArchiveCancelled:
        _remove(zip_path)
        raise
    except Exception as e:
        _remove(zip_path)
        raise OSError(f"文件夹打包失败 {folder_path}: {e}") from e

    try:
        zip_size = zip_path.stat().st_size
    except OSError:
        zip_size = 0

    logger.info(
        "打包完成: %s.zip, %s 个文件, %.1f MB -> %.1f MB, 耗时 %.1fs",
        folder_name, file_count,
        raw_size / 1048576, zip_size / 1048576, time.time() - started,
    )
    return zip_path


def _remove(path):
    try:
        if os.path.isfile(path):
            os.remove(path)
    except OSError as e:
        logger.warning("清理未完成的压缩包失败: %s, 错误: %s", path, e)
