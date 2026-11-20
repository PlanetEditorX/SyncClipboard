"""托盘与窗口图标的主题适配。

Windows 11 的任务栏/托盘有浅色和深色两套主题：

- 浅色主题下，原来那套近白色图标（icon-active.png / icon-stop.png）
  几乎和任务栏同色，基本看不见；
- 深色主题下反过来，深色图标会糊掉。

所以这里按系统主题挑对应的图标文件，读取失败时退回原来的白色图标。

只负责「状态 + 主题 -> 文件路径」。界面迁到 Qt 后，托盘直接用
``QIcon(icon_path(state))`` 加载 PNG；本模块不读图像内容，因此不依赖
Pillow，打包时可以整体排除（省约 11MB）。图标配色的回归验证放在
``tests/test_icon_theme.py`` 里，由测试自己读像素。
"""
import logging
import sys
import time

from common.utils import BASE_DIR

logger = logging.getLogger("gui")

# 状态 -> 文件名。active = 服务在跑，stop = 服务已停止
_LIGHT_TASKBAR_FILES = {
    "active": "icon-active-black.png",
    "stop": "icon-stop-black.png",
}
_DARK_TASKBAR_FILES = {
    "active": "icon-active.png",
    "stop": "icon-stop-white.png",
}
# 变体缺失时的兜底（仓库里一定有这两张）
_FALLBACK_FILES = {
    "active": "icon-active.png",
    "stop": "icon-stop.png",
}

# 主题查询结果缓存（秒）：切换系统主题后最多 3 秒生效，避免每次刷新都读注册表
_THEME_CACHE_TTL = 3.0
_theme_cache = {"at": 0.0, "light": True}


def system_uses_light_taskbar():
    """任务栏是否为浅色。读不到时按浅色处理（也就是用深色图标，保证看得见）。"""
    now = time.monotonic()
    if now - _theme_cache["at"] < _THEME_CACHE_TTL:
        return _theme_cache["light"]

    light = True
    if sys.platform == "win32":
        try:
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
            ) as key:
                for value_name in ("SystemUsesLightTheme", "AppsUseLightTheme"):
                    try:
                        value, _ = winreg.QueryValueEx(key, value_name)
                    except FileNotFoundError:
                        continue
                    light = bool(value)
                    break
        except Exception:
            logger.debug("读取任务栏主题失败，按浅色处理", exc_info=True)

    _theme_cache["at"] = now
    _theme_cache["light"] = light
    return light


def icon_path(state="active", light_taskbar=None):
    """返回当前主题下该状态应使用的图标路径（找不到返回 None）。"""
    if state not in _FALLBACK_FILES:
        state = "active"
    if light_taskbar is None:
        light_taskbar = system_uses_light_taskbar()

    candidates = (
        (_LIGHT_TASKBAR_FILES if light_taskbar else _DARK_TASKBAR_FILES).get(state),
        _FALLBACK_FILES.get(state),
    )
    icon_dir = BASE_DIR / "gui" / "icon"
    for name in candidates:
        if not name:
            continue
        path = icon_dir / name
        if path.exists():
            return path
    return None


def is_active_state(service_state):
    """把服务状态压成 active / stop 两档。"""
    return "active" if (service_state.get("server_alive") or service_state.get("client_alive")) else "stop"
