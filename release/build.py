"""Build the Windows onedir distribution without side effects on import."""

import io
import shutil
import subprocess
import sys
from pathlib import Path


RELEASE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = RELEASE_DIR.parent
DIST_DIR = RELEASE_DIR / "dist"
APP_DIR = DIST_DIR / "SyncClipboard"
WORK_DIR = RELEASE_DIR / "build_cache"
SPEC_DIR = RELEASE_DIR / "spec"
ENTRY_SCRIPT = PROJECT_ROOT / "gui" / "run.py"
ICON_FILE = PROJECT_ROOT / "gui" / "icon" / "icon-active.png"
MANIFEST_FILE = RELEASE_DIR / "app.manifest"


def _configure_stdout():
    """确保 stdout 能输出中文。

    GitHub Actions 的 PowerShell 控制台默认 cp1252，直接 print 中文会抛
    UnicodeEncodeError；本地 Git Bash 是 UTF-8，所以本地测不出来。
    这里把 stdout 重新配置成 UTF-8，已经是 UTF-8 时直接返回。

    幂等且可重复调用：测试会绕过 main() 直接调各个步骤函数，
    所以每个对外函数都要自己保证输出安全。
    """
    stream = sys.stdout
    if stream is None:
        return
    encoding = (getattr(stream, "encoding", "") or "").lower()
    if encoding.replace("-", "").replace("_", "") in {"utf8", "cp65001"}:
        return
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
        return
    except (AttributeError, ValueError, OSError):
        pass
    # 不支持 reconfigure 的对象（部分测试替身 / 重定向流）退回包装 buffer
    buffer = getattr(stream, "buffer", None)
    if buffer is not None:
        try:
            sys.stdout = io.TextIOWrapper(buffer, encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass


def build_command():
    """Return the PyInstaller command so CI can inspect it without building.

    PySide6 有官方 hook，Qt 的 DLL 与平台插件会自动收集；这里只额外排除
    确定用不到的模块（QML/Quick、tkinter、旧版 customtkinter）来控制体积。
    """
    return [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onedir",
        "--windowed",
        "--name=SyncClipboard",
        "--distpath",
        str(DIST_DIR),
        "--workpath",
        str(WORK_DIR),
        "--specpath",
        str(SPEC_DIR),
        f"--icon={ICON_FILE}",
        # PyInstaller 默认 manifest 不声明 DPI 感知，200% 缩放下窗口会被系统拉伸变糊
        f"--manifest={MANIFEST_FILE}",
        "--paths",
        str(PROJECT_ROOT),
        "--hidden-import=win32clipboard",
        "--hidden-import=watchdog",
        "--hidden-import=watchdog.observers",
        "--hidden-import=watchdog.events",
        "--exclude-module=tkinter",
        "--exclude-module=customtkinter",
        # PIL 只被图标配色的单元测试用到，界面与托盘都走 QIcon，可整体排除
        "--exclude-module=PIL",
        "--exclude-module=PySide6.QtQml",
        "--exclude-module=PySide6.QtQuick",
        "--exclude-module=PySide6.QtQuickWidgets",
        "--exclude-module=PySide6.Qt3DCore",
        "--exclude-module=PySide6.QtMultimedia",
        "--exclude-module=PySide6.QtWebEngineCore",
        "--exclude-module=PySide6.QtWebEngineWidgets",
        str(ENTRY_SCRIPT),
    ]


# release/ 目录下会被 clean 步骤保留的源文件（其余都是构建产物，可删）。
# 老实现只保留了 build.py 与 bat，导致新增的 app.manifest 被删掉后
# build_command() 再去引用它时 PyInstaller 直接报「找不到 manifest」。
SOURCE_ARTIFACTS = {"build.py", "bat", "app.manifest"}


def clean_release_outputs():
    _configure_stdout()
    print("[Clean] 清理旧构建输出...")
    for item in RELEASE_DIR.iterdir():
        if item.name in SOURCE_ARTIFACTS:
            continue
        if item.is_dir():
            shutil.rmtree(item, ignore_errors=True)
            print(f"  已删除目录: {item}")
        else:
            item.unlink(missing_ok=True)
            print(f"  已删除文件: {item}")


def copy_runtime_assets():
    _configure_stdout()
    exe_path = APP_DIR / "SyncClipboard.exe"
    if not exe_path.exists():
        raise RuntimeError("未找到生成的 exe，打包可能失败")

    # Keep templates separate from mutable runtime config, so extracting a new
    # release over an existing folder does not overwrite the user's settings.
    src_config = PROJECT_ROOT / "config" / "example"
    dst_config = APP_DIR / "config" / "example"
    shutil.copytree(src_config, dst_config, dirs_exist_ok=True)
    print(f"  [OK] 配置模板 -> {dst_config}")

    dst_icon = APP_DIR / "gui" / "icon"
    dst_icon.mkdir(parents=True, exist_ok=True)
    # 浅色 / 深色任务栏是两套图标文件，统一按目录复制，避免新增变体漏打进去
    for source in sorted((PROJECT_ROOT / "gui" / "icon").iterdir()):
        if source.suffix.lower() in {".png", ".ico"}:
            shutil.copy2(source, dst_icon / source.name)
    print(f"  [OK] 图标 -> {dst_icon}")

    src_bat = RELEASE_DIR / "bat"
    if src_bat.exists():
        for file in src_bat.glob("*.bat"):
            shutil.copy2(file, APP_DIR / file.name)
        print(f"  [OK] bat 文件 -> {APP_DIR}")

    prune_qt_payload(APP_DIR)
    return exe_path


# PyInstaller 的 PySide6 hook 会把整个 Qt 运行时搬进来，其中这些对纯
# QWidget（raster 渲染）应用毫无用处。删掉能省约 28MB。
PRUNABLE_PATHS = (
    "PySide6/opengl32sw.dll",  # 软件 OpenGL 渲染器，Widgets 不依赖
    "PySide6/translations",  # Qt 自带翻译，项目未加载 QTranslator
    "PySide6/plugins/tls",  # Qt 的 TLS 后端，网络请求走 requests
    "PySide6/plugins/networkinformation",
    "PySide6/plugins/generic",
)


def prune_qt_payload(app_dir=None):
    """裁剪打包产物里确定用不到的内容，返回释放的字节数。

    每一项独立处理：某一项删不掉（文件被占用、权限不足、外部安全策略）
    不能影响其余项，否则体积会悄悄反弹。
    """
    _configure_stdout()
    internal = Path(app_dir or APP_DIR) / "_internal"
    if not internal.is_dir():
        return 0

    removed = 0
    for relative in PRUNABLE_PATHS:
        target = internal / relative
        try:
            if target.is_dir():
                size = sum(f.stat().st_size for f in target.rglob("*") if f.is_file())
                shutil.rmtree(target)
                print(f"  [PRUNE] 目录 {relative} ({size / 1048576:.1f} MB)")
                removed += size
            elif target.is_file():
                size = target.stat().st_size
                target.unlink()
                print(f"  [PRUNE] 文件 {relative} ({size / 1048576:.1f} MB)")
                removed += size
        except OSError as exc:
            print(f"  [PRUNE] 跳过 {relative}: {exc}")
    if removed:
        print(f"  [PRUNE] 合计减少 {removed / 1048576:.1f} MB")
    return removed


def main():
    _configure_stdout()
    clean_release_outputs()
    print("\n[Build] 开始 PyInstaller onedir 打包（窗口模式）...")
    subprocess.run(build_command(), check=True)
    print("PyInstaller 打包完成。")

    print("\n[Copy] 复制运行时资源...")
    exe_path = copy_runtime_assets()
    print("\n[Success] 打包成功！")
    print(f"应用位置: {exe_path}")
    print(f"发布时请将整个 {APP_DIR} 文件夹一起分发。")


if __name__ == "__main__":
    main()
