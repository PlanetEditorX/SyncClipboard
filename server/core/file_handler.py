from pathlib import Path

class FileHandler:
    def __init__(self, save_path):
        self.save_path = Path(save_path)
        self.save_path.mkdir(parents=True, exist_ok=True)

    def save_file(self, filename, content_bytes):
        file_path = self.save_path / filename
        with open(file_path, "wb") as f:
            f.write(content_bytes)
        return str(file_path)