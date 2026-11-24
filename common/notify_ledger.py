"""跨进程的「这条内容已经弹过通知了吗」共享账本。

SyncClipboard 里有两个进程都可能为同一次同步弹通知：

- GUI 进程：监视 ``latest/*.json``，处理「服务器收到手机推送」这条路径；
- 客户端进程：收到服务器 ``/update/current_latest`` 派发时直接弹。

两边会在同一秒内看到同一条记录，而进程内变量去不了重（各进程各一份），
所以把「刚弹过哪条」写在 ``latest/notify_ledger.json`` 里共享。账本的唯一
用途是「刚弹过就别再弹」，因此只保留最近一段时间和固定条数，过期即丢。

读写都在多进程下：写用 ``os.replace`` 原子替换，读失败一律当作「没弹过」。
偶发重复弹窗远好过把通知整个吞掉 —— 宁可多提醒一次，不能漏。
"""

import json
import logging
import os
import threading
import time

from common.utils import BASE_DIR

logger = logging.getLogger("gui")

LEDGER_FILE = BASE_DIR / "latest" / "notify_ledger.json"

# 记账保鲜期：超出这个时间的记录不再有意义（通知最多也就看几分钟）
KEEP_SECONDS = 600
# 账本最多留多少条，防止文件无限增长
MAX_ENTRIES = 200

_lock = threading.Lock()


def _prune(entries, now):
    """丢掉过期记录。容量裁剪单独做，见 ``_trim``。"""
    fresh = {}
    for key, at in entries.items():
        try:
            stamp = float(at)
        except (TypeError, ValueError):
            continue
        if now - stamp <= KEEP_SECONDS:
            fresh[key] = stamp
    return fresh


def _trim(entries):
    """只保留最新的 MAX_ENTRIES 条，防止文件无限增长。"""
    if len(entries) <= MAX_ENTRIES:
        return entries
    newest = sorted(entries.items(), key=lambda item: item[1])[-MAX_ENTRIES:]
    return dict(newest)


def _read():
    try:
        with open(LEDGER_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write(entries):
    LEDGER_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = LEDGER_FILE.with_name(LEDGER_FILE.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(entries, f, ensure_ascii=False)
    os.replace(tmp, LEDGER_FILE)


def claim(key, now=None):
    """抢一条通知的「弹窗权」。

    返回 True 表示本次由调用方弹（并顺手占位），False 表示别的进程刚弹过、
    这次应当跳过。``key`` 为空时一律返回 True —— 没有 id 的记录本来就无法
    去重，交给调用方自己决定。
    """
    key = str(key or "")
    if not key:
        return True
    stamp = time.time() if now is None else float(now)
    with _lock:
        entries = _read()
        entries = _prune(entries, stamp)
        if key in entries:
            return False
        entries[key] = stamp
        entries = _trim(entries)
        try:
            _write(entries)
        except OSError as exc:  # 写不进去也照样弹，只是可能重复一次
            logger.debug("通知账本写入失败: %s", exc)
        return True


def was_notified(key, now=None):
    """只查不占位，给测试和排障用。"""
    key = str(key or "")
    if not key:
        return False
    stamp = time.time() if now is None else float(now)
    with _lock:
        return key in _prune(_read(), stamp)


def reset():
    """清空账本（测试用）。"""
    with _lock:
        try:
            os.remove(LEDGER_FILE)
        except OSError:
            pass
