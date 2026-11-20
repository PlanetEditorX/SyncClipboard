# item_builder.py
import os
import hashlib
import uuid
from datetime import datetime

def generate_stable_id(item):
    """
    内容指纹：忽略 timestamp，同一内容+来源只生成一个指纹。

    注意：这**不再**作为事件 ID 使用。用户手动把同一段文字复制两次，
    是两次独立的同步事件，必须能各自广播一次；用内容指纹当 ID 会被
    去重逻辑吞掉（历史 bug：A→B→A 时手机收不到第二个 A）。
    指纹只用于“判断是不是同一份内容”，供回音溯源使用。
    """
    s = f'{item["type"]}|{item.get("content","")}|{item.get("path","")}|{item["source"]}'
    return hashlib.md5(s.encode('utf-8')).hexdigest()

def generate_event_id(fingerprint, timestamp, nonce=None):
    """为每一次推送生成唯一事件 ID（同一内容重复复制会得到不同 ID）。"""
    nonce = nonce or uuid.uuid4().hex
    return hashlib.md5(f"{fingerprint}|{timestamp}|{nonce}".encode('utf-8')).hexdigest()

def build_text_item(text, source, pasted=False, timestamp=None, origin=None):
    """
    构建一个剪贴板文本条目字典
    :param text: 文本内容
    :param source: 来源标识（如 "PC", "iPhone" 等）
    :param pasted: 是否已粘贴
    :param timestamp: 时间戳，不传则自动生成 ISO 格式
    :param origin: 内容的首发客户端（溯源用），不传则等于 source
    :return: 符合缓存结构的 item 字典
    """
    if timestamp is None:
        timestamp = datetime.now().isoformat()

    fingerprint = generate_stable_id({
        "type": "text",
        "content": text,
        "source": source
    })

    item = {
        # 事件 ID：每次复制事件都是新的，重复复制同一内容也能再次广播
        "id": generate_event_id(fingerprint, timestamp),
        # 内容指纹：同一内容在同一来源下的稳定标识，用于回音/去重判定
        "fingerprint": fingerprint,
        "type": "text",
        "content": text,
        "timestamp": timestamp,
        "source": source,
        # 溯源：这份内容最初是哪台设备复制的
        "origin": origin or source,
        "pasted": pasted
    }
    return item

def build_file_item(file_paths, source, pasted=False):
    """根据文件路径列表构建文件条目（仅元数据）"""
    files_info = []
    for path in file_paths:
        if os.path.isfile(path):
            stat = os.stat(path)
            files_info.append({
                "path": path,
                "name": os.path.basename(path),
                "size": stat.st_size
            })
    if not files_info:
        return None

    # 生成唯一 id：基于所有文件的路径+大小+来源
    id_str = "".join([f"{f['path']}{f['size']}" for f in files_info]) + source
    item_id = hashlib.md5(id_str.encode()).hexdigest()
    return {
        "id": item_id,
        "type": "file",
        "content": files_info,          # 文件元数据列表
        "timestamp": datetime.now().isoformat(),
        "source": source,
        "pasted": False
    }
