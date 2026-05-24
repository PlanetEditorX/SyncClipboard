# release/build.py
import shutil
import subprocess
from pathlib import Path

# ---------- 路径定义 ----------
RELEASE_DIR = Path(__file__).resolve().parent          # release/
PROJECT_ROOT = RELEASE_DIR.parent                      # 项目根目录

DIST_DIR = RELEASE_DIR / "dist"                        # 最终输出
WORK_DIR = RELEASE_DIR / "build_cache"                 # PyInstaller 临时文件
SPEC_DIR = RELEASE_DIR / "spec"                        # spec 文件

# 入口：直接给 gui/run.py 的完整路径
ENTRY_SCRIPT = PROJECT_ROOT / "gui" / "run.py"
ICON_FILE = PROJECT_ROOT / "gui" / "icon" / "icon-active.png"

# ---------- 清理旧的构建产物 ----------
print("🧹 清理 release 下的旧构建文件...")
for d in [DIST_DIR, WORK_DIR, SPEC_DIR]:
    if d.exists():
        shutil.rmtree(d)
        print(f"  已删除: {d}")

# ---------- PyInstaller 打包 ----------
print("\n📦 开始 PyInstaller 打包...")
cmd = [
    "pyinstaller",
    "--noconfirm",
    "--clean",
    "--onefile",
    "--name=SyncClipboard",
    "--distpath", str(DIST_DIR),
    "--workpath", str(WORK_DIR),
    "--specpath", str(SPEC_DIR),
    f"--icon={ICON_FILE}",
    # 重要：添加项目根目录到搜索路径，保证 server/client/common 等包能被找到
    "--paths", str(PROJECT_ROOT),
    # 入口脚本（gui/run.py）
    str(ENTRY_SCRIPT)
]

subprocess.run(cmd, check=True)
print("PyInstaller 打包完成。")

# ---------- 复制运行时需要的资源 ----------
# 你的程序里用 Path(sys.executable).parent 定位外部文件，
# 因此需要在 exe 同级放 config 和 gui/icon 文件夹。
print("\n📁 复制配置文件和图标到 dist/ ...")

exe_path = DIST_DIR / "SyncClipboard.exe"
if not exe_path.exists():
    raise RuntimeError("未找到生成的 exe，打包可能失败！")

# 1. config 文件夹
src_config = PROJECT_ROOT / "config"
dst_config = DIST_DIR / "config"
if dst_config.exists():
    shutil.rmtree(dst_config)
shutil.copytree(src_config, dst_config)
print(f"  ✓ config -> {dst_config}")

# 2. 图标文件夹
dst_icon = DIST_DIR / "gui" / "icon"
dst_icon.mkdir(parents=True, exist_ok=True)
for fname in ["icon.ico", "icon-active.png", "icon-stop.png"]:
    src = PROJECT_ROOT / "gui" / "icon" / fname
    if src.exists():
        shutil.copy2(src, dst_icon / fname)
print(f"  ✓ 图标 -> {dst_icon}")

print(f"\n✅ 打包成功！")
print(f"单文件 exe 位于: {exe_path}")
print(f"发布时请将整个 {DIST_DIR} 文件夹（exe + config/ + gui/icon/）一起分发。")