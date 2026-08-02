"""仅用于说明 SyncClipboard Android 新增 multipart 上传字段的最小 Flask 示例。"""
from pathlib import Path
from flask import Flask, jsonify, request

app = Flask(__name__)
UPLOAD_DIR = Path("uploads")
UPLOAD_DIR.mkdir(exist_ok=True)
EXPECTED_KEY = "replace-me"


@app.post("/upload_file")
def upload_file():
    if request.form.get("key") != EXPECTED_KEY:
        return jsonify(error="invalid key"), 403
    file = request.files.get("file")
    if file is None or not file.filename:
        return jsonify(error="missing file"), 400
    safe_name = Path(file.filename).name
    file.save(UPLOAD_DIR / safe_name)
    return jsonify(ok=True, filename=safe_name, source=request.form.get("source"))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
