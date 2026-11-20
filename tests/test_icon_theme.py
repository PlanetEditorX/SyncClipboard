import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from gui import icon_theme


def _mean_luminance(path):
    """不透明像素的平均亮度（0-255）。"""
    with Image.open(path).convert("RGBA") as image:
        raw = image.tobytes()
    total = 0.0
    count = 0
    for i in range(0, len(raw), 4):
        r, g, b, a = raw[i], raw[i + 1], raw[i + 2], raw[i + 3]
        if a <= 128:
            continue
        total += 0.299 * r + 0.587 * g + 0.114 * b
        count += 1
    return total / count if count else None


class TestIconTheme(unittest.TestCase):
    def test_light_taskbar_uses_dark_icons(self):
        active = icon_theme.icon_path("active", light_taskbar=True)
        stop = icon_theme.icon_path("stop", light_taskbar=True)

        self.assertEqual(active.name, "icon-active-black.png")
        self.assertEqual(stop.name, "icon-stop-black.png")

    def test_dark_taskbar_uses_light_icons(self):
        active = icon_theme.icon_path("active", light_taskbar=False)
        stop = icon_theme.icon_path("stop", light_taskbar=False)

        self.assertEqual(active.name, "icon-active.png")
        self.assertEqual(stop.name, "icon-stop-white.png")

    def test_dark_variants_are_actually_dark(self):
        """回归：浅色任务栏用的图标必须是深色，否则又变成看不见的白图标。"""
        for state in ("active", "stop"):
            path = icon_theme.icon_path(state, light_taskbar=True)
            luminance = _mean_luminance(path)
            self.assertIsNotNone(luminance)
            self.assertLess(luminance, 80, f"{path.name} 太亮了（{luminance:.0f}）")

    def test_all_icon_files_exist(self):
        icon_dir = Path(icon_theme.BASE_DIR) / "gui" / "icon"
        for name in (
            "icon-active.png",
            "icon-active-black.png",
            "icon-stop.png",
            "icon-stop-black.png",
            "icon-stop-white.png",
        ):
            self.assertTrue((icon_dir / name).exists(), f"缺少图标文件 {name}")

    def test_missing_variant_falls_back_to_original(self):
        with tempfile.TemporaryDirectory() as tmp:
            icon_dir = Path(tmp) / "gui" / "icon"
            icon_dir.mkdir(parents=True)
            Image.new("RGBA", (8, 8), "#FFFFFF").save(icon_dir / "icon-active.png")

            with patch.object(icon_theme, "BASE_DIR", Path(tmp)):
                active = icon_theme.icon_path("active", light_taskbar=True)

        self.assertEqual(active.name, "icon-active.png")

    def test_service_state_maps_to_distinct_icons(self):
        """运行态与停止态必须落在不同文件上，否则托盘状态看不出变化。"""
        active = icon_theme.is_active_state({"server_alive": True, "client_alive": False})
        stopped = icon_theme.is_active_state({"server_alive": False, "client_alive": False})
        self.assertEqual(active, "active")
        self.assertEqual(stopped, "stop")

        self.assertNotEqual(
            icon_theme.icon_path(active, light_taskbar=True),
            icon_theme.icon_path(stopped, light_taskbar=True),
        )

    def test_missing_icon_files_return_none(self):
        """文件全缺时返回 None，由托盘侧退回内置图标；不能抛异常。"""
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(icon_theme, "BASE_DIR", Path(tmp)):
                self.assertIsNone(icon_theme.icon_path("active", light_taskbar=True))
                self.assertIsNone(icon_theme.icon_path("stop", light_taskbar=False))


if __name__ == "__main__":
    unittest.main()
