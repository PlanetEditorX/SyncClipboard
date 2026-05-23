import json
from server.flask_app import app

CONFIG_FILE = "server_config.json"

def load_config():
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

if __name__ == "__main__":
    config = load_config()
    # 直接启动 Flask，不再需要剪贴板监听
    app.run(host="0.0.0.0", port=config["port"], debug=False)