import pyperclip
import hashlib
from datetime import datetime

def generate_id(item):
    """
    生成唯一id
    """
    s = f'{item["type"]}|{item.get("content","")}|{item.get("path","")}|{item["timestamp"]}|{item["source"]}'
    return hashlib.md5(s.encode('utf-8')).hexdigest()

def get_clipboard_text():
    """
    获取剪贴板
    """
    return pyperclip.paste()

def set_clipboard_text(text):
    """
    写入剪贴板
    """
    pyperclip.copy(text)

def generate_id(item):
    """
    旧的每次生成唯一 ID（包含 timestamp）
    """
    s = f'{item["type"]}|{item.get("content","")}|{item.get("path","")}|{item["timestamp"]}|{item["source"]}'
    return hashlib.md5(s.encode('utf-8')).hexdigest()

def generate_stable_id(item):
    """
    新的稳定 ID，忽略 timestamp，同一内容+来源只生成一个 ID
    """
    s = f'{item["type"]}|{item.get("content","")}|{item.get("path","")}|{item["source"]}'
    return hashlib.md5(s.encode('utf-8')).hexdigest()