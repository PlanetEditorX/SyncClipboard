"""日志文件的按需读取（纯 Python，不依赖图形库）。

这里刻意不含任何轮询、监听或后台线程：日志只有在你点开查看器的那一刻
才被读一次，而且只读文件尾部有限字节——server.log / client.log 由
RotatingFileHandler 写入（单文件 128KB 上限），这里再兜一层，防止日志
被手动放大后一次性读进内存。

服务端与客户端各自独立写自己的文件，所以「区分服务日志和客户端日志」
在数据层面天然成立，UI 只是给它们各开一个入口。
"""

import logging
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# 单次最多读 256KB / 2000 行，足够覆盖两个轮转周期
MAX_TAIL_BYTES = 256 * 1024
MAX_LINES = 2000

# source -> (显示名, 文件名, 说明)
LOG_SOURCES: Dict[str, Tuple[str, str, str]] = {
    "server": ("服务日志", "server.log", "本机服务器进程"),
    "client": ("客户端日志", "client.log", "同步客户端进程"),
    "gui": ("界面日志", "gui.log", "托盘与主窗口"),
}

DEFAULT_SOURCE = "server"

# server/run.py 清过 ANSI，但第三方库偶尔会漏到文件里
_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")


@dataclass(frozen=True)
class LogSnapshot:
    """一次读取的结果。任何 I/O 问题都折成 error，不向 UI 抛异常。"""

    source: str
    path: Path
    text: str = ""
    line_count: int = 0
    file_size: int = 0
    truncated: bool = False
    modified_at: Optional[datetime] = None
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error

    @property
    def title(self) -> str:
        return LOG_SOURCES[self.source][0]


def log_path(base_dir, source=DEFAULT_SOURCE) -> Path:
    """日志文件路径；来源非法时抛 ValueError（调用方传的是常量，属于编码错误）。"""
    if source not in LOG_SOURCES:
        raise ValueError(f"未知日志来源: {source}")
    return Path(base_dir) / "log" / LOG_SOURCES[source][1]


def read_log(
    source=DEFAULT_SOURCE,
    base_dir=None,
    max_bytes=MAX_TAIL_BYTES,
    max_lines=MAX_LINES,
) -> LogSnapshot:
    """读取日志尾部。

    参数:
        source: LOG_SOURCES 的键
        base_dir: 程序根目录，缺省用 common.utils.BASE_DIR
        max_bytes: 最多从文件尾部读多少字节
        max_lines: 最多保留多少行
    返回:
        LogSnapshot；文件缺失/被锁等情况下 error 非空，text 为空
    """
    if base_dir is None:
        from common.utils import BASE_DIR

        base_dir = BASE_DIR

    path = log_path(base_dir, source)

    try:
        stat = path.stat()
    except FileNotFoundError:
        return LogSnapshot(source, path, error=f"尚未生成日志文件：{path.name}")
    except OSError as exc:
        return LogSnapshot(source, path, error=f"无法访问日志文件：{exc}")

    modified = _to_datetime(stat.st_mtime)
    if stat.st_size == 0:
        # 空文件是正常状态（刚轮转完），不算错误，交给 UI 显示占位文案
        return LogSnapshot(source, path, modified_at=modified)

    truncated = stat.st_size > max_bytes
    try:
        raw = _read_tail(path, stat.st_size, max_bytes, truncated)
    except OSError as exc:
        # Windows 上轮转瞬间文件可能被独占，提示重试而不是崩掉界面
        return LogSnapshot(
            source,
            path,
            file_size=stat.st_size,
            modified_at=modified,
            error=f"读取失败：{exc}（文件可能正被服务写入，稍后重试）",
        )

    text = _ANSI_ESCAPE.sub("", raw.decode("utf-8", errors="replace"))
    lines = text.splitlines()
    if len(lines) > max_lines:
        lines = lines[-max_lines:]
        truncated = True

    return LogSnapshot(
        source=source,
        path=path,
        text="\n".join(lines),
        line_count=len(lines),
        file_size=stat.st_size,
        truncated=truncated,
        modified_at=modified,
    )


def _read_tail(path: Path, size: int, max_bytes: int, truncated: bool) -> bytes:
    """只读尾部；被截断时丢掉开头那半行，避免显示残句。"""
    with path.open("rb") as handle:
        if truncated:
            start = size - max_bytes
            handle.seek(start - 1)
            if handle.read(1) != b"\n":
                handle.readline()
        return handle.read()


def _to_datetime(timestamp: float) -> datetime:
    try:
        return datetime.fromtimestamp(timestamp)
    except (OverflowError, OSError, ValueError):
        return datetime.now()


def format_size(num_bytes: int) -> str:
    """人类可读的大小，仅用于 UI 状态行。"""
    if num_bytes < 1024:
        return f"{num_bytes} B"
    if num_bytes < 1024 * 1024:
        return f"{num_bytes / 1024:.1f} KB"
    return f"{num_bytes / (1024 * 1024):.1f} MB"
