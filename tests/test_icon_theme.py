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

    def test_load_icon_image_follows_service_state(self):
        active = icon_theme.load_icon_image(
            {"server_alive": True, "client_alive": False}, light_taskbar=True
        )
        stopped = icon_theme.load_icon_image(
            {"server_alive": False, "client_alive": False}, light_taskbar=True
        )

        self.assertEqual(active.mode, "RGBA")
        self.assertEqual(stopped.mode, "RGBA")
        # 运行态用近黑图标，停止态用中灰图标，两者不能是同一张图
        self.assertNotEqual(active.tobytes(), stopped.tobytes())

    def test_missing_icon_files_do_not_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(icon_theme, "BASE_DIR", Path(tmp)):
                image = icon_theme.load_icon_image(None, light_taskbar=True)

        self.assertEqual(image.mode, "RGBA")
        self.assertEqual(image.size, (64, 64))


if __name__ == "__main__":
    unittest.main()
