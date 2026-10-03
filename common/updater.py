"""
应用版本信息与手动更新检查。

版本号在此处集中维护，发布新版本时同步修改 APP_VERSION。
更新检查只在用户手动触发时访问 GitHub，不做任何后台轮询。
"""
import logging
from dataclasses import dataclass
from typing import Optional

import requests

logger = logging.getLogger(__name__)

# 当前应用版本，发布时与 git tag 保持一致
APP_VERSION = "1.8.1"

# 代码仓库与发布地址
REPOSITORY = "PlanetEditorX/SyncClipboard"
RELEASES_API = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
RELEASES_PAGE = f"https://github.com/{REPOSITORY}/releases/latest"

REQUEST_TIMEOUT = 10

# Windows 发布产物的文件名前缀，用于从 Release 附件中挑选安装包
WINDOWS_ASSET_PREFIX = "SyncClipboard-Windows-"


@dataclass
class UpdateResult:
    """一次更新检查的结果。"""

    current_version: str
    latest_version: Optional[str] = None
    has_update: bool = False
    release_url: str = RELEASES_PAGE
    download_url: Optional[str] = None
    error: Optional[str] = None


def parse_version(value):
    """把 "v1.7.0" 之类的版本号转换为可比较的整数元组。"""
    text = str(value or "").strip().lstrip("vV")
    core = text.split("-", 1)[0].split("+", 1)[0]
    parts = []
    for chunk in core.split("."):
        digits = "".join(ch for ch in chunk if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3])


def is_newer(latest, current):
    """判断 latest 是否比 current 更新。"""
    return parse_version(latest) > parse_version(current)


def select_windows_asset(assets):
    """从 Release 附件中挑选 Windows 安装包下载地址。"""
    for asset in assets or []:
        name = str(asset.get("name") or "")
        if name.startswith(WINDOWS_ASSET_PREFIX) and name.lower().endswith(".zip"):
            return asset.get("browser_download_url")
    return None


def download_file(url, destination, progress=None, timeout=REQUEST_TIMEOUT * 6, chunk_size=64 * 1024):
    """流式下载文件，progress(downloaded, total) 用于汇报进度。"""
    downloaded = 0
    total = 0
    with requests.get(url, stream=True, timeout=timeout) as response:
        response.raise_for_status()
        total = int(response.headers.get("Content-Length") or 0)
        with open(destination, "wb") as target:
            for chunk in response.iter_content(chunk_size=chunk_size):
                if not chunk:
                    continue
                target.write(chunk)
                downloaded += len(chunk)
                if progress is not None:
                    progress(downloaded, total)
    return downloaded


def check_for_update(current_version=APP_VERSION, timeout=REQUEST_TIMEOUT):
    """查询 GitHub 最新 Release，返回更新结果；网络异常时通过 error 字段反馈。"""
    try:
        response = requests.get(
            RELEASES_API,
            timeout=timeout,
            headers={"Accept": "application/vnd.github+json"},
        )
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        logger.warning("检查更新失败: %s", exc)
        return UpdateResult(current_version=current_version, error="检查更新失败，请检查网络后重试")

    tag = str(payload.get("tag_name") or "").strip()
    if not tag:
        logger.warning("更新接口未返回版本号")
        return UpdateResult(current_version=current_version, error="未能获取最新版本信息")

    latest = tag.lstrip("vV")
    return UpdateResult(
        current_version=current_version,
        latest_version=latest,
        has_update=is_newer(latest, current_version),
        release_url=payload.get("html_url") or RELEASES_PAGE,
        download_url=select_windows_asset(payload.get("assets")),
    )
