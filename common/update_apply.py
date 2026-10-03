"""应用自更新：解压安装包、退出后替换程序目录并重启。

仅在 PyInstaller 打包版本（onedir）中可用。替换由独立批处理脚本完成，
因为程序自身正在运行时无法覆盖自己的可执行文件。
"""
import logging
import os
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

logger = logging.getLogger(__name__)

# 等待主程序退出的最长秒数
WAIT_SECONDS = 60

_UPDATER_SCRIPT = r"""@echo off
setlocal enabledelayedexpansion
set "SRC={src}"
set "DST={dst}"
set "EXE={exe}"
set "WORK={work}"
set /a COUNT=0
:wait
tasklist /FI "IMAGENAME eq SyncClipboard.exe" 2>NUL | find /I "SyncClipboard.exe" >NUL
if not errorlevel 1 (
    set /a COUNT+=1
    if !COUNT! GEQ {wait} goto copy
    ping -n 2 127.0.0.1 >NUL
    goto wait
)
:copy
robocopy "%SRC%" "%DST%" /E /IS /IT /R:2 /W:1 /NFL /NDL /NJH /NJS
start "" "%EXE%"
rmdir /S /Q "%WORK%" 2>NUL
endlocal
"""


class UpdateError(RuntimeError):
    """自更新过程中的可预期失败。"""


def is_self_update_available():
    """只有 PyInstaller 打包后的 exe 才能自我替换。"""
    return bool(getattr(sys, "frozen", False))


def resolve_app_dir():
    """返回需要被替换的程序目录（打包后的 SyncClipboard 目录）。"""
    if not is_self_update_available():
        raise UpdateError("开发模式下不支持自动更新")
    return Path(sys.executable).resolve().parent


def find_payload_root(extract_dir):
    """在解压结果中定位包含 SyncClipboard.exe 的目录。"""
    extract_dir = Path(extract_dir)
    if (extract_dir / "SyncClipboard.exe").exists():
        return extract_dir
    for child in extract_dir.iterdir():
        if child.is_dir() and (child / "SyncClipboard.exe").exists():
            return child
    raise UpdateError("安装包中未找到 SyncClipboard.exe")


def _safe_extract(archive, target):
    """解压并拒绝越出目标目录的路径，防止压缩包路径穿越。"""
    target = Path(target).resolve()
    root = str(target)
    for member in archive.namelist():
        resolved = str((target / member).resolve())
        if resolved != root and not resolved.startswith(root + os.sep):
            raise UpdateError("安装包包含非法路径，已中止更新")
    archive.extractall(target)


def extract_package(zip_path, extract_dir):
    """解压安装包并返回实际内容目录。"""
    extract_dir = Path(extract_dir)
    extract_dir.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(zip_path) as archive:
            _safe_extract(archive, extract_dir)
    except zipfile.BadZipFile as exc:
        raise UpdateError("安装包已损坏，无法解压") from exc
    return find_payload_root(extract_dir)


def ensure_writable(app_dir):
    """确认程序目录可写，否则更新无法完成。"""
    probe = Path(app_dir) / f".update_probe_{os.getpid()}"
    try:
        probe.write_text("", encoding="utf-8")
    except OSError as exc:
        raise UpdateError("程序目录不可写，请把程序放到有写入权限的位置后重试") from exc
    finally:
        try:
            probe.unlink(missing_ok=True)
        except OSError:
            pass


def launch_updater(app_dir, payload_dir, work_dir):
    """生成替换脚本并分离启动，脚本会等待本程序退出后覆盖文件并重启。"""
    app_dir = Path(app_dir)
    script = _UPDATER_SCRIPT.format(
        src=payload_dir,
        dst=app_dir,
        exe=app_dir / "SyncClipboard.exe",
        work=work_dir,
        wait=WAIT_SECONDS,
    )
    script_path = Path(tempfile.gettempdir()) / f"syncclipboard_update_{os.getpid()}.bat"
    # cmd.exe 使用系统 ANSI 代码页解析批处理，用 mbcs 保证中文路径正确
    script_path.write_text(script, encoding="mbcs")

    creation_flags = 0
    if hasattr(subprocess, "DETACHED_PROCESS"):
        creation_flags |= subprocess.DETACHED_PROCESS
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        creation_flags |= subprocess.CREATE_NO_WINDOW
    subprocess.Popen(
        ["cmd", "/c", str(script_path)],
        creationflags=creation_flags,
        close_fds=True,
    )
    logger.info("更新脚本已启动: %s", script_path)
    return script_path
