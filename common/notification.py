"""Windows 桌面通知（WinRT toast）。

重要：通知器必须惰性创建。
``InteractableWindowsToaster`` 的构造函数会初始化 WinRT/COM；如果这件事
发生在 Qt 初始化 GUI 线程的 COM 之前，进程会直接 access violation ——
表现就是「同样的代码，import 顺序不同就崩」。所以这里不在模块导入时
建立实例，等第一次真正要发通知时再建。

自定义 AUMID 也在这里惰性注册：早先需要用户手动以管理员运行脚本
写 HKLM 并在开始菜单建快捷方式，现在改成首次弹通知时写 HKCU（用户级、无需提权），不满足的条件一律退化为日志，不影响主流程。
"""

import logging
import platform
import threading
import time
from pathlib import Path

from common.utils import BASE_DIR

logger = logging.getLogger("gui")

AUMID = "PlanetEditorX.SyncClipboard"

_toaster = None
_toaster_ready = False
_toaster_lock = threading.Lock()

# 通知展示多久后自动清理（后台线程执行，不阻塞调用方）
_CLEAR_DELAY = 5


def _ensure_aumid():
    """在 HKCU 注册自定义 AUMID，让 WinRT 认得通知来源。

    ``InteractableWindowsToaster`` 用 AUMID 创建通知器，点击通知触发
    ``on_activated`` 也要求这个 AUMID 已被系统识别；未注册时通知可能
    不显示或点击无响应。HKCU 不需要管理员权限，也不用建开始菜单快捷方式。
    """
    if platform.system() != "Windows":
        return
    try:
        import winreg

        key_path = rf"SOFTWARE\Classes\AppUserModelId\{AUMID}"
        with winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.SetValueEx(key, "DisplayName", 0, winreg.REG_SZ, "SyncClipboard")
            icon = Path(BASE_DIR) / "gui" / "icon" / "icon-active.png"
            if icon.is_file():
                winreg.SetValueEx(key, "IconUri", 0, winreg.REG_SZ, str(icon))
    except Exception as exc:  # noqa: BLE001 - 注册失败不能拖垮通知主流程
        logger.warning("AUMID 注册失败，通知可能显示为系统默认来源: %s", exc)


def _get_toaster():
    """首次调用时创建通知器；失败后不再重试，直接退化为日志。"""
    global _toaster, _toaster_ready
    if _toaster_ready:
        return _toaster
    with _toaster_lock:
        if _toaster_ready:
            return _toaster
        _toaster_ready = True
        if platform.system() != "Windows":
            return None
        _ensure_aumid()
        try:
            from windows_toasts import InteractableWindowsToaster

            _toaster = InteractableWindowsToaster("SyncClipboard", notifierAUMID=AUMID)
        except Exception as exc:
            logger.warning("Windows 通知初始化失败: %s", exc)
            _toaster = None
        return _toaster


def _build_toast(title, msg):
    from windows_toasts import Toast

    toast = Toast()
    toast.text_fields = [title, msg]
    return toast


def _schedule_clear(toaster):
    """延时清理通知，避免像原来那样在调用线程里 sleep 5 秒。"""

    def _clear():
        time.sleep(_CLEAR_DELAY)
        try:
            toaster.clear_toasts()
        except Exception:
            logger.debug("清理通知失败", exc_info=True)

    threading.Thread(target=_clear, name="toast-clear", daemon=True).start()


def show_notification(title, msg):
    toaster = _get_toaster()
    if toaster is None:
        logger.info("[通知] %s: %s", title, msg)
        return

    try:
        toaster.show_toast(_build_toast(title, msg))
        _schedule_clear(toaster)
    except Exception as exc:
        logger.error("通知弹出失败: %s", exc)


def show_notification_with_click(title, msg, callback):
    toaster = _get_toaster()
    if toaster is None:
        logger.info("[通知] %s: %s", title, msg)
        threading.Thread(target=callback, daemon=True).start()
        return

    try:
        toast = _build_toast(title, msg)

        def _clicked(event_args):
            try:
                threading.Thread(target=callback, daemon=True).start()
            except Exception as exc:
                logger.error("通知回调失败: %s", exc)

        toast.on_activated = _clicked
        toaster.show_toast(toast)
        _schedule_clear(toaster)
    except Exception as exc:
        logger.error("带点击通知失败: %s", exc)


def _clear_toasts_quietly(toaster):
    try:
        toaster.clear_toasts()
    except Exception:
        logger.debug("清理确认通知失败", exc_info=True)


def show_confirm_notification(title, msg, timeout=15,
                              allow_label="允许共享", deny_label="暂不共享"):
    """右下角通知 + 两个按钮，等用户在 timeout 秒内做决定。

    返回 True（点了允许）/ False（点了拒绝或超时）/ None（通知发不出来）。

    与 ``show_notification_with_click`` 的区别是这里**要等结果**，所以：
    - 用 ``ToastDuration.Long``，否则 Windows 会把横幅 5 秒就收走；
    - 只认「点按钮」和「超时」两种结局。用户把横幅划掉不单独处理，
      到点自然算作不共享 —— 这样不必依赖 activated / dismissed 的先后顺序，
      少一个竞态。
    - 超时后主动清掉通知，避免它留在通知中心又被点到。

    发不出通知时返回 None 而不是 False：调用方据此决定退回原生对话框还是
    直接按不共享处理，这里不替调用方拍板。
    """
    toaster = _get_toaster()
    if toaster is None:
        logger.info("[确认通知不可用] %s: %s", title, msg)
        return None

    try:
        from windows_toasts import Toast, ToastButton, ToastDuration
    except Exception as exc:  # noqa: BLE001 - 缺库时同样退化为「发不出」
        logger.warning("确认通知不可用: %s", exc)
        return None

    allow_arg = "syncclipboard-allow"
    deny_arg = "syncclipboard-deny"
    state = {"allowed": None}
    done = threading.Event()
    lock = threading.Lock()

    def settle(allowed):
        with lock:
            if state["allowed"] is not None:
                return
            state["allowed"] = bool(allowed)
        done.set()

    def _activated(event_args):
        # 点按钮会带回 arguments；点通知本体没有 arguments，按允许处理
        arguments = str(getattr(event_args, "arguments", "") or "")
        if deny_arg in arguments:
            settle(False)
        elif allow_arg in arguments:
            settle(True)
        else:
            settle(True)

    toast = Toast(
        text_fields=[title, msg],
        duration=ToastDuration.Long,
        on_activated=_activated,
        actions=[
            ToastButton(content=allow_label, arguments=allow_arg),
            ToastButton(content=deny_label, arguments=deny_arg),
        ],
    )

    try:
        toaster.show_toast(toast)
    except Exception as exc:  # noqa: BLE001
        logger.error("确认通知弹出失败: %s", exc)
        return None

    def _expire():
        settle(False)
        _clear_toasts_quietly(toaster)

    timer = threading.Timer(max(1, int(timeout)), _expire)
    timer.daemon = True
    timer.start()
    try:
        # 多等几秒：定时器到点会 settle，这里只是兜底不要永久挂住
        done.wait(max(1, int(timeout)) + 5)
    finally:
        timer.cancel()
        _clear_toasts_quietly(toaster)

    allowed = bool(state["allowed"])
    logger.info(
        "确认通知: %s → %s（%s 秒内未点击即按不共享处理）",
        title, "允许" if allowed else "不共享", timeout,
    )
    return allowed
