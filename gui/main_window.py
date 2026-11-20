"""客户端设置的纯逻辑校验。

界面已经迁到 PySide6（gui.qt_main_window），这里只保留不依赖任何
图形库的校验函数，便于在无显示环境下单测。
"""


def validate_client_settings(
    server_host,
    server_port,
    local_name,
    file_server_port,
    key,
    save_path,
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
    }
