"""
应用版本信息与手动更新检查。

版本号在此处集中维护，发布新版本时同步修改 APP_VERSION。
更新检查只在用户手动触发时访问 GitHub，不做任何后台轮询。

更新源分三档，互不干扰：
- ``direct``：直连 GitHub；
- ``mirror``：镜像加速，只把请求 URL 换成「前缀 + 原始 URL」；
- ``manual``：手动代理，只给 requests 挂 proxies，URL 不变。
镜像与代理都失败时不再自动乱试：镜像会退一步直连原始地址，代理按用户
明确指定的路径走，失败就如实报错。下载阶段的取消由 cancel 令牌控制，
取消后抛 UpdateCancelled，由调用方清理临时目录。
"""
import logging
from dataclasses import dataclass
from typing import Optional

import requests

logger = logging.getLogger(__name__)

# 当前应用版本，发布时与 git tag 保持一致
APP_VERSION = "1.12.21"

# 代码仓库与发布地址
REPOSITORY = "PlanetEditorX/SyncClipboard"
RELEASES_API = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
RELEASES_PAGE = f"https://github.com/{REPOSITORY}/releases/latest"

REQUEST_TIMEOUT = 10

# Windows 发布产物的文件名前缀，用于从 Release 附件中挑选安装包
WINDOWS_ASSET_PREFIX = "SyncClipboard-Windows-"

# ---------------- 更新源 ----------------

UPDATE_SOURCE_DIRECT = "direct"
UPDATE_SOURCE_MIRROR = "mirror"
UPDATE_SOURCE_MANUAL = "manual"
VALID_UPDATE_SOURCES = (
    UPDATE_SOURCE_DIRECT,
    UPDATE_SOURCE_MIRROR,
    UPDATE_SOURCE_MANUAL,
)

DEFAULT_UPDATE_SOURCE = UPDATE_SOURCE_DIRECT
# 镜像前缀的形态是「前缀 + 原始 URL」整段拼接，例如
# https://gh-proxy.com/https://github.com/xxx/releases/download/xxx.zip。
# 这几个都是 GitHub 全站代理，能同时转发 api.github.com 与 Release 安装包；
# 第三方服务随时可能失效，所以面板给的是可编辑下拉，用户可以自选或自填。
# 刻意不放 jsDelivr 系（cdn.jsdelivr.net / fastly.jsdelivr.net 等）：它们只能
# 取仓库里的 raw 文件，代理不了 Release 的 zip，也转发不了 api.github.com。
UPDATE_MIRROR_PRESETS = (
    "https://gh-proxy.com",
    "https://ghfast.top",
    "https://ghproxy.net",
    "https://hub.gitmirror.com",
    "https://gh.llkk.cc",
    "https://ghproxy.cc",
)
DEFAULT_UPDATE_MIRROR_PREFIX = UPDATE_MIRROR_PRESETS[0]
DEFAULT_UPDATE_PROXY = ""

# 手动代理只支持 HTTP/HTTPS：requests 原生就能用，不必额外装 PySocks。
# SOCKS5 要引进 socks 依赖、安装包也会变大，这里刻意不支持。
PROXY_SCHEMES = ("http://", "https://")


class UpdateCancelled(Exception):
    """用户在下载过程中点了取消。"""


def mirror_url(url, prefix):
    """把原始地址换成镜像地址；前缀为空时原样返回。"""
    base = str(prefix or "").strip().rstrip("/")
    if not base:
        return url
    return f"{base}/{url}"


@dataclass(frozen=True)
class UpdateSource:
    """一次更新流程使用的网络路径。"""

    mode: str = DEFAULT_UPDATE_SOURCE
    mirror_prefix: str = DEFAULT_UPDATE_MIRROR_PREFIX
    proxy: str = DEFAULT_UPDATE_PROXY

    @property
    def proxies(self):
        """manual 模式下交给 requests 的 proxies；其它档位不挂代理。"""
        if self.mode != UPDATE_SOURCE_MANUAL:
            return None
        text = str(self.proxy or "").strip()
        if not text:
            return None
        return {"http": text, "https": text}

    def resolve(self, url):
        """当前档位下这个上游地址实际要请求的地址。"""
        if self.mode == UPDATE_SOURCE_MIRROR:
            return mirror_url(url, self.mirror_prefix)
        return url


@dataclass
class UpdateResult:
    """一次更新检查的结果。"""

    current_version: str
    latest_version: Optional[str] = None
    has_update: bool = False
    release_url: str = RELEASES_PAGE
    download_url: Optional[str] = None
    error: Optional[str] = None
    source_mode: str = DEFAULT_UPDATE_SOURCE


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


def _request_kwargs(source):
    """只有真正设置了代理才传 proxies，避免把 None 塞给 requests。"""
    proxies = source.proxies if source is not None else None
    return {"proxies": proxies} if proxies else {}


def _stream_download(url, destination, progress, timeout, chunk_size,
                     source, cancel_event):
    downloaded = 0
    total = 0
    with requests.get(
        url, stream=True, timeout=timeout, **_request_kwargs(source)
    ) as response:
        response.raise_for_status()
        total = int(response.headers.get("Content-Length") or 0)
        with open(destination, "wb") as target:
            for chunk in response.iter_content(chunk_size=chunk_size):
                # 取消检查放在写入前：大文件下到一半点取消，能立刻停下
                if cancel_event is not None and cancel_event.is_set():
                    raise UpdateCancelled("已取消更新")
                if not chunk:
                    continue
                target.write(chunk)
                downloaded += len(chunk)
                if progress is not None:
                    progress(downloaded, total)
    return downloaded


def download_file(url, destination, progress=None, timeout=REQUEST_TIMEOUT * 6,
                  chunk_size=64 * 1024, source=None, cancel_event=None):
    """流式下载文件，progress(downloaded, total) 用于汇报进度。

    镜像模式下如果镜像地址连不上，会退回原始地址再试一次；手动代理不回退
    （代理是用户明确指定的路径，退回直连只会白等一轮超时）。
    用户取消时抛 UpdateCancelled，不会自己清理半截文件 —— 那是调用方
    （更新流程）负责连同整个临时目录一起删掉。
    """
    source = source or UpdateSource()
    attempts = [source.resolve(url)]
    if source.mode == UPDATE_SOURCE_MIRROR and attempts[0] != url:
        attempts.append(url)

    last_error = None
    for index, attempt in enumerate(attempts, 1):
        try:
            return _stream_download(
                attempt, destination, progress, timeout, chunk_size,
                source, cancel_event,
            )
        except UpdateCancelled:
            raise
        except Exception as exc:  # noqa: BLE001 - 交给最后一个地址统一上报
            last_error = exc
            logger.warning("更新包下载失败（地址 %s/%s）: %s",
                           index, len(attempts), exc)
    raise last_error


def _fetch_latest_release(timeout, source):
    return requests.get(
        source.resolve(RELEASES_API),
        timeout=timeout,
        headers={"Accept": "application/vnd.github+json"},
        **_request_kwargs(source),
    )


def check_for_update(current_version=APP_VERSION, timeout=REQUEST_TIMEOUT, source=None):
    """查询最新 Release，返回更新结果；网络异常时通过 error 字段反馈。

    镜像档失败会自动退回直连一次，因为镜像前缀是第三方服务、随时可能挂，
    不该让用户手动切回「直连」才能继续检查。
    """
    source = source or UpdateSource()
    payload = None
    try:
        response = _fetch_latest_release(timeout, source)
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:  # noqa: BLE001 - 网络异常统一转成可读文案
        logger.warning("检查更新失败（%s）: %s", source.mode, exc)
        if source.mode == UPDATE_SOURCE_MIRROR:
            try:
                response = requests.get(
                    RELEASES_API,
                    timeout=timeout,
                    headers={"Accept": "application/vnd.github+json"},
                )
                response.raise_for_status()
                payload = response.json()
                logger.info("镜像不可用，已通过直连获取版本信息")
            except Exception as direct_exc:  # noqa: BLE001
                logger.warning("直连回退也失败: %s", direct_exc)
                payload = None

    if payload is None:
        return UpdateResult(
            current_version=current_version,
            error="检查更新失败，请检查网络或换一个更新源后重试",
            source_mode=source.mode,
        )

    tag = str(payload.get("tag_name") or "").strip()
    if not tag:
        logger.warning("更新接口未返回版本号")
        return UpdateResult(
            current_version=current_version,
            error="未能获取最新版本信息",
            source_mode=source.mode,
        )

    latest = tag.lstrip("vV")
    return UpdateResult(
        current_version=current_version,
        latest_version=latest,
        has_update=is_newer(latest, current_version),
        release_url=payload.get("html_url") or RELEASES_PAGE,
        download_url=select_windows_asset(payload.get("assets")),
        source_mode=source.mode,
    )
