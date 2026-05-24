from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parent.parent
OUT = Path(__file__).resolve().parent

WORK = OUT / "build_cache"
SPEC = OUT / "spec"

# 清理
for p in [WORK, SPEC]:
    if p.exists():
        shutil.rmtree(p)

for exe in [
    "SyncClipboardServer.exe",
    "SyncClipboardClient.exe",
    "SyncClipboardTray.exe",
]:
    f = OUT / exe
    if f.exists():
        f.unlink()

WORK.mkdir(exist_ok=True)
SPEC.mkdir(exist_ok=True)

print("开始打包...")

# Server
subprocess.run([
    "pyinstaller",
    "--noconfirm",
    "--clean",
    "--onefile",
    "--name=SyncClipboardServer",
    "--distpath", str(OUT),
    "--workpath", str(WORK),
    "--specpath", str(SPEC),
    str(ROOT / "server" / "run.py")
], check=True)

# Client
subprocess.run([
    "pyinstaller",
    "--noconfirm",
    "--clean",
    "--onefile",
    "--name=SyncClipboardClient",
    "--distpath", str(OUT),
    "--workpath", str(WORK),
    "--specpath", str(SPEC),
    str(ROOT / "client" / "run.py")
], check=True)

# Tray
subprocess.run([
    "pyinstaller",
    "--noconfirm",
    "--clean",
    "--onefile",
    "--windowed",
    "--icon=" + str(ROOT / "gui" / "icon" / "icon-active.png"),
    "--name=SyncClipboardTray",
    "--distpath", str(OUT),
    "--workpath", str(WORK),
    "--specpath", str(SPEC),
    str(ROOT / "gui" / "run.py")
], check=True)

print("复制配置...")

# config
dst_config = OUT / "config"
if dst_config.exists():
    shutil.rmtree(dst_config)

shutil.copytree(ROOT / "config", dst_config)

# icon
dst_icon = OUT / "gui" / "icon"
dst_icon.mkdir(parents=True, exist_ok=True)

for f in [
    "icon.ico",
    "icon-active.png",
    "icon-stop.png"
]:
    src = ROOT / "gui" / "icon" / f
    if src.exists():
        shutil.copy2(src, dst_icon / f)

print("完成")