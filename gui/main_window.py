"""客户端设置的纯逻辑校验。

界面已经迁到 PySide6（gui.qt_main_window），这里只保留不依赖任何
图形库的校验函数，便于在无显示环境下单测。
"""

from common.sync_modes import MODE_BOTH, VALID_MODES

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
