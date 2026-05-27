# release/build.py
import shutil
import subprocess
from pathlib import Path
import sys
import io

# 强制 stdout 使用 utf-8，避免 CI 环境编码错误
if sys.stdout.encoding != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

# ---------- 路径定义 ----------
RELEASE_DIR = Path(__file__).resolve().parent          # release/
PROJECT_ROOT = RELEASE_DIR.parent                      # 项目根目录

DIST_DIR = RELEASE_DIR / "dist"                        # 最终输出
WORK_DIR = RELEASE_DIR / "build_cache"                 # PyInstaller 临时文件
SPEC_DIR = RELEASE_DIR / "spec"                        # spec 文件

ENTRY_SCRIPT = PROJECT_ROOT / "gui" / "run.py"
ICON_FILE = PROJECT_ROOT / "gui" / "icon" / "icon-active.png"

# ---------- 清理 release 目录下所有旧文件（保留本脚本自身）----------
print("[Clean] 清理 release 目录下所有旧文件（除 build.py）...")
for item in RELEASE_DIR.iterdir():
    if item.name == "build.py":
        continue
    if item.is_dir():
        shutil.rmtree(item, ignore_errors=True)
        print(f"  已删除目录: {item}")
    else:
        item.unlink(missing_ok=True)
        print(f"  已删除文件: {item}")

# ---------- PyInstaller 打包（隐藏黑框）----------
print("\n[Build] 开始 PyInstaller 打包（窗口模式，无控制台）...")
cmd = [
    sys.executable, "-m", "PyInstaller",
    "--noconfirm",
    "--clean",
    "--onefile",
    "--windowed",                     # ← 隐藏黑框
    "--name=SyncClipboard",
    "--distpath", str(DIST_DIR),
    "--workpath", str(WORK_DIR),
    "--specpath", str(SPEC_DIR),
    f"--icon={ICON_FILE}",
    "--paths", str(PROJECT_ROOT),
    "--hidden-import=win32clipboard",
    str(ENTRY_SCRIPT)
]

subprocess.run(cmd, check=True)
print("PyInstaller 打包完成。")

# ---------- 复制运行时需要的资源 ----------
print("\n[Copy] 复制配置文件和图标到 dist/ ...")

exe_path = DIST_DIR / "SyncClipboard.exe"
if not exe_path.exists():
    raise RuntimeError("未找到生成的 exe，打包可能失败！")

# 1. config 文件夹（复制 example 配置）
src_config = PROJECT_ROOT / "config" / "example"
dst_config = DIST_DIR / "config"
shutil.copytree(src_config, dst_config, dirs_exist_ok=True)
print(f"  ✓ config -> {dst_config}")

# 2. 图标文件夹
dst_icon = DIST_DIR / "gui" / "icon"
dst_icon.mkdir(parents=True, exist_ok=True)
for fname in ["icon.ico", "icon-active.png", "icon-stop.png"]:
    src = PROJECT_ROOT / "gui" / "icon" / fname
    if src.exists():
        shutil.copy2(src, dst_icon / fname)
print(f"  ✓ 图标 -> {dst_icon}")

print(f"\n[Success] 打包成功！")
print(f"单文件 exe 位置: {exe_path}")
print(f"发布时请将整个 {DIST_DIR} 文件夹（exe + config/ + gui/icon/）一起分发。")