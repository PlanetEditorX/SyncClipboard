# common/path.py

from pathlib import Path
import sys

def get_base_dir():
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    else:
        return Path(__file__).resolve().parent.parent

BASE_DIR = get_base_dir()