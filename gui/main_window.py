"""客户端设置的纯逻辑校验。

界面已经迁到 PySide6（gui.qt_main_window），这里只保留不依赖任何
图形库的校验函数，便于在无显示环境下单测。
"""

from common.sync_modes import MODE_BOTH, VALID_MODES
from common.updater import (
    DEFAULT_UPDATE_MIRROR_PREFIX,
    DEFAULT_UPDATE_PROXY,
    DEFAULT_UPDATE_SOURCE,
    PROXY_SCHEMES,
    UPDATE_SOURCE_MANUAL,
    UPDATE_SOURCE_MIRROR,
    VALID_UPDATE_SOURCES,
)

# 目录共享确认阈值：逐文件传输下风险来自「总大小」和「文件数」两个维度，
# 任一超阈值都要先问用户。这是确认阈值，不是硬拒绝上限。
# `folder_zip_confirm_gb` 是历史配置键名，改名会破坏老配置，故只改显示名。
DEFAULT_FOLDER_ZIP_CONFIRM_GB = 10
MIN_FOLDER_ZIP_CONFIRM_GB = 0.1
MAX_FOLDER_ZIP_CONFIRM_GB = 10240

DEFAULT_FOLDER_FILE_COUNT_CONFIRM = 3000
MIN_FOLDER_FILE_COUNT_CONFIRM = 100
# 与 client.folder_manifest.MANIFEST_MAX_ENTRIES 对齐：文件数阈值不能越过
# 清单硬上限，否则确认框会承诺一个根本传不完的目录。
MAX_FOLDER_FILE_COUNT_CONFIRM = 20000

# 共享存活时间（分钟）：从电脑上复制那一刻起算的「首次获取等待窗口」。
# 目录逐文件下载开始后，每次成功请求会把窗口往后推（FileServer._touch_share），
# 避免文件多时下到一半被清理。默认 30 分钟给两端留足余量。
DEFAULT_FILE_SHARE_TTL_MINUTES = 30
MIN_FILE_SHARE_TTL_MINUTES = 1
MAX_FILE_SHARE_TTL_MINUTES = 24 * 60

# 共享可被完整下载的次数。0 = 不限制（存活期内所有已配对设备都能取），
# 1 = 第一个设备下完就停止共享（默认），N = 允许 N 个设备各自完整下载一次。
# 「完整」由发送端实际发出的字节数判定，按设备计次，重试 / 断点续传不重复占名额。
DEFAULT_SHARE_DOWNLOAD_LIMIT = 1
MIN_SHARE_DOWNLOAD_LIMIT = 0
MAX_SHARE_DOWNLOAD_LIMIT = 99

# 目录超限确认弹窗的存活时间（秒）。到点没有点「允许共享」就按不共享处理，
# 避免复制完大目录后界面一直挂着等一个不会来的点击。
DEFAULT_FOLDER_CONFIRM_TIMEOUT_SECONDS = 15
MIN_FOLDER_CONFIRM_TIMEOUT_SECONDS = 3
MAX_FOLDER_CONFIRM_TIMEOUT_SECONDS = 300

# 服务器派发的同步是否也弹通知（默认开）。
# 手机直接推给「本机服务器」时本来就会弹；但服务器再把内容派发到另一台
# 电脑的客户端时，那条链路原来只在后台写剪贴板，用户看不到任何提示。
DEFAULT_NOTIFY_REMOTE_PUSH = True


def validate_client_settings(
    server_host,
    server_port,
    local_name,
    file_server_port,
    key,
    save_path,
    text_sync_mode=MODE_BOTH,
    file_sync_mode=MODE_BOTH,
    folder_sync_enabled=True,
    folder_zip_confirm_gb=DEFAULT_FOLDER_ZIP_CONFIRM_GB,
    folder_file_count_confirm=DEFAULT_FOLDER_FILE_COUNT_CONFIRM,
    file_share_ttl_minutes=DEFAULT_FILE_SHARE_TTL_MINUTES,
    share_download_limit=DEFAULT_SHARE_DOWNLOAD_LIMIT,
    folder_confirm_timeout_seconds=DEFAULT_FOLDER_CONFIRM_TIMEOUT_SECONDS,
    notify_remote_push=DEFAULT_NOTIFY_REMOTE_PUSH,
    update_source=DEFAULT_UPDATE_SOURCE,
    update_mirror_prefix=DEFAULT_UPDATE_MIRROR_PREFIX,
    update_proxy=DEFAULT_UPDATE_PROXY,
):
    """校验并规范化客户端设置；不合法时抛 ValueError。"""
    host = str(server_host or "").strip()
    name = str(local_name or "").strip()
    secret = str(key or "")
    directory = str(save_path or "").strip()

    if not host:
        raise ValueError("请输入服务器地址")
    if not name:
        raise ValueError("请输入本机名称")
    if not secret.strip():
        raise ValueError("请输入共享密钥")
    if not directory:
        raise ValueError("请选择文件保存目录")

    try:
        server_port_value = int(server_port)
        file_port_value = int(file_server_port)
    except (TypeError, ValueError) as exc:
        raise ValueError("端口必须是数字") from exc

    if not 1 <= server_port_value <= 65535:
        raise ValueError("服务器端口必须在 1 到 65535 之间")
    if not 1 <= file_port_value <= 65535:
        raise ValueError("文件服务端口必须在 1 到 65535 之间")

    source_mode, mirror_prefix, proxy = _normalize_update_source(
        update_source, update_mirror_prefix, update_proxy
    )

    return {
        "server_host": host,
        "server_port": server_port_value,
        "local_name": name,
        "file_server_port": file_port_value,
        "key": secret,
        "save_path": directory,
        "text_sync_mode": _normalize_mode(text_sync_mode, "文字同步模式"),
        "file_sync_mode": _normalize_mode(file_sync_mode, "文件同步模式"),
        "folder_sync_enabled": _normalize_bool(folder_sync_enabled),
        "folder_zip_confirm_gb": _normalize_confirm_gb(folder_zip_confirm_gb),
        "folder_file_count_confirm": _normalize_folder_count(folder_file_count_confirm),
        "file_share_ttl_minutes": _normalize_ttl(file_share_ttl_minutes),
        "share_download_limit": _normalize_download_limit(share_download_limit),
        "folder_confirm_timeout_seconds": _normalize_folder_confirm_timeout(
            folder_confirm_timeout_seconds
        ),
        "notify_remote_push": _normalize_bool(notify_remote_push),
        "update_source": source_mode,
        "update_mirror_prefix": mirror_prefix,
        "update_proxy": proxy,
    }


def _normalize_mode(value, field):
    mode = str(value or "").strip().lower()
    if mode not in VALID_MODES:
        raise ValueError(f"{field}只能是双向、只发送或只接收")
    return mode


def _normalize_bool(value):
    if isinstance(value, str):
        return value.strip().lower() not in {"false", "0", "no", "off", ""}
    return bool(value)


def _normalize_confirm_gb(value):
    try:
        size = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("目录大小阈值必须是数字（GB）") from exc
    if not MIN_FOLDER_ZIP_CONFIRM_GB <= size <= MAX_FOLDER_ZIP_CONFIRM_GB:
        raise ValueError(
            f"目录大小阈值必须在 {MIN_FOLDER_ZIP_CONFIRM_GB:g} 到 "
            f"{MAX_FOLDER_ZIP_CONFIRM_GB:g} GB 之间"
        )
    size = round(size, 1)
    # 整数不写成 10.0，配置文件里更直观
    return int(size) if float(size).is_integer() else size


def _normalize_folder_count(value):
    try:
        count = int(str(value).strip()) if isinstance(value, str) else int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("目录文件数阈值必须是整数") from exc
    if not MIN_FOLDER_FILE_COUNT_CONFIRM <= count <= MAX_FOLDER_FILE_COUNT_CONFIRM:
        raise ValueError(
            f"目录文件数阈值必须在 {MIN_FOLDER_FILE_COUNT_CONFIRM} 到 "
            f"{MAX_FOLDER_FILE_COUNT_CONFIRM} 之间"
        )
    return count


def _normalize_ttl(value):
    try:
        minutes = int(str(value).strip()) if isinstance(value, str) else int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("共享存活时间必须是整数分钟") from exc
    if not MIN_FILE_SHARE_TTL_MINUTES <= minutes <= MAX_FILE_SHARE_TTL_MINUTES:
        raise ValueError(
            f"共享存活时间必须在 {MIN_FILE_SHARE_TTL_MINUTES} 到 "
            f"{MAX_FILE_SHARE_TTL_MINUTES} 分钟之间"
        )
    return minutes


def _normalize_download_limit(value):
    try:
        count = int(str(value).strip()) if isinstance(value, str) else int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("共享下载次数必须是整数（0 表示不限制）") from exc
    if not MIN_SHARE_DOWNLOAD_LIMIT <= count <= MAX_SHARE_DOWNLOAD_LIMIT:
        raise ValueError(
            f"共享下载次数必须在 {MIN_SHARE_DOWNLOAD_LIMIT} 到 "
            f"{MAX_SHARE_DOWNLOAD_LIMIT} 之间（0 = 不限制）"
        )
    return count


def _normalize_folder_confirm_timeout(value):
    try:
        seconds = int(str(value).strip()) if isinstance(value, str) else int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("确认弹窗持续时间必须是整数秒") from exc
    if not (
        MIN_FOLDER_CONFIRM_TIMEOUT_SECONDS
        <= seconds
        <= MAX_FOLDER_CONFIRM_TIMEOUT_SECONDS
    ):
        raise ValueError(
            f"确认弹窗持续时间必须在 {MIN_FOLDER_CONFIRM_TIMEOUT_SECONDS} 到 "
            f"{MAX_FOLDER_CONFIRM_TIMEOUT_SECONDS} 秒之间"
        )
    return seconds


def _normalize_update_source(mode, mirror_prefix, proxy):
    """校验更新源三档，返回 (模式, 镜像前缀, 代理地址)。

    只有当前选中的那一档必须填好：直连不校验另两个输入框，切过去时保留
    用户上次填的值，省得来回切换要重填。
    """
    text = str(mode or "").strip().lower()
    if text not in VALID_UPDATE_SOURCES:
        raise ValueError("更新源只能是 GitHub 直连、镜像加速或手动代理")

    prefix = str(mirror_prefix or "").strip().rstrip("/")
    proxy_text = str(proxy or "").strip()

    if text == UPDATE_SOURCE_MIRROR:
        if not prefix:
            raise ValueError("镜像加速需要填写镜像地址，例如 https://gh-proxy.com")
        if not prefix.lower().startswith(("http://", "https://")):
            raise ValueError("镜像地址必须以 http:// 或 https:// 开头")
    elif text == UPDATE_SOURCE_MANUAL:
        if not proxy_text:
            raise ValueError("手动代理需要填写代理地址，例如 http://127.0.0.1:7890")
        if not proxy_text.lower().startswith(PROXY_SCHEMES):
            raise ValueError(
                "代理地址必须以 http:// 或 https:// 开头（不支持 SOCKS5）"
            )

    return text, prefix, proxy_text
