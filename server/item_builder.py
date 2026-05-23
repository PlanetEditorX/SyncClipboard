# item_builder.py
from datetime import datetime
from clipboard_manager import generate_stable_id

def build_text_item(text, source, pasted=False, timestamp=None):
    """
    构建一个剪贴板文本条目字典
    :param text: 文本内容
    :param source: 来源标识（如 "PC", "iPhone" 等）
    :param pasted: 是否已粘贴
    :param timestamp: 时间戳，不传则自动生成 ISO 格式
    :return: 符合缓存结构的 item 字典
    """
    if timestamp is None:
        timestamp = datetime.now().isoformat()

    item = {
        "id": generate_stable_id({
            "type": "text",
            "content": text,
            "source": source
        }),
        "type": "text",
        "content": text,
        "timestamp": timestamp,
        "source": source,
        "pasted": pasted
    }
    return item