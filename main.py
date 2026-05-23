# main.py —— 服务端入口
import json
import threading
from pathlib import Path
from server.flask_app import app, cache, tracker
from server.clipboard_watcher import ClipboardWatcher
from server.cache_manager import CacheManager

CONFIG_FILE = "server_config.json"

def load_config():
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

if __name__ == "__main__":
    config = load_config()

    # 初始化缓存并注入 local_name
    cache.cache["local_name"] = config["local_name"]

    # # 启动剪贴板监听线程
    # watcher = ClipboardWatcher(cache, on_new_item=tracker.update)
    # t = threading.Thread(target=watcher.start, daemon=True)
    # t.start()

    # 启动 Flask
    app.run(host="0.0.0.0", port=config["port"], debug=False)