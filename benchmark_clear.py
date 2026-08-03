import time
import threading
from flask import Flask
import server.api.flask_app as flask_app
import requests

def mock_client_server(port):
    app = Flask(f"client_{port}")
    @app.route('/clear/file_latest')
    def clear():
        time.sleep(1) # simulate slow network
        return "OK", 200

    import logging
    log = logging.getLogger('werkzeug')
    log.setLevel(logging.ERROR)
    app.run(port=port, debug=False, use_reloader=False)

# start a few dummy clients
ports = [9001, 9002, 9003]
for port in ports:
    t = threading.Thread(target=mock_client_server, args=(port,), daemon=True)
    t.start()

# wait for servers to start
time.sleep(2)

# set up mock data
class MockTracker:
    def get_global_latest(self): return None
class MockLatestFile:
    def get_all_files(self): return [{"source": "someone"}]

flask_app.tracker = MockTracker()
flask_app.latest_file = MockLatestFile()
flask_app.clients.clear()
for port in ports:
    flask_app.clients.append({
        "local_name": f"client_{port}",
        "ip": "127.0.0.1",
        "port": port
    })
flask_app.LOCAL_NAME = "server"
flask_app.KEY = "test"

start = time.time()
flask_app.notify_clients("clear")
end = time.time()

print(f"Time taken to notify {len(ports)} clients: {end - start:.2f} seconds")
