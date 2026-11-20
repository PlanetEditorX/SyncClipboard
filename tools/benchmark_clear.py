"""验证「清理通知」的并发批量推送耗时。

`notify_clients("clear")` 会向每个已知客户端发 HTTP 请求。早期实现是串行的，
客户端一多就慢；这个脚本起 3 个假客户端（各延迟 1 秒响应）来量化优化效果。

用法（在仓库根目录）：

    python tools/benchmark_clear.py
"""
import logging
import sys
import threading
import time
from pathlib import Path

from flask import Flask

# 直接以脚本方式运行时，sys.path[0] 是 tools/，需要补上仓库根目录
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import server.api.flask_app as flask_app  # noqa: E402


def mock_client_server(port):
    """假客户端：响应慢 1 秒，用于放大串行/并行的差异。"""
    app = Flask(f"client_{port}")

    @app.route("/clear/file_latest")
    def clear():
        time.sleep(1)
        return "OK", 200

    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    app.run(port=port, debug=False, use_reloader=False)


class MockTracker:
    def get_global_latest(self):
        return None


class MockLatestFile:
    def get_all_files(self):
        return [{"source": "someone"}]


def main():
    ports = [9001, 9002, 9003]
    for port in ports:
        threading.Thread(
            target=mock_client_server, args=(port,), daemon=True
        ).start()
    time.sleep(2)  # 等假客户端起来

    flask_app.tracker = MockTracker()
    flask_app.latest_file = MockLatestFile()
    flask_app.clients.clear()
    for port in ports:
        flask_app.clients.append(
            {"local_name": f"client_{port}", "ip": "127.0.0.1", "port": port}
        )
    flask_app.LOCAL_NAME = "server"
    flask_app.KEY = "test"

    start = time.time()
    flask_app.notify_clients("clear")
    elapsed = time.time() - start

    print(f"通知 {len(ports)} 个客户端耗时: {elapsed:.2f} 秒")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
