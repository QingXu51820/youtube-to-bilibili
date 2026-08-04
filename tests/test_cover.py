"""自测：封面处理（校验、EXIF 转置、crop/contain、1920×1080 输出）。"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from yt2bili import config
from yt2bili.media.cover import (
    _crop_to_ratio,
    _pad_to_size,
    image_size,
    is_valid_image,
    prepare_cover,
)


def make_image(path, size, color=(200, 100, 50)):
    Image.new("RGB", size, color).save(path, format="JPEG")
    return path


class IsValidImageTests(unittest.TestCase):
    def test_none_and_empty(self):
        self.assertFalse(is_valid_image(None))
        self.assertFalse(is_valid_image(""))

    def test_missing_file(self):
        self.assertFalse(is_valid_image("does_not_exist.jpg"))

    def test_empty_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "empty.jpg"
            p.write_bytes(b"")
            self.assertFalse(is_valid_image(p))

    def test_garbage_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "garbage.jpg"
            p.write_bytes(b"not an image at all")
            self.assertFalse(is_valid_image(p))

    def test_valid_image(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "ok.jpg"
            make_image(p, (100, 100))
            self.assertTrue(is_valid_image(p))


class CropToRatioTests(unittest.TestCase):
    def test_wide_image_crops_sides_center(self):
        img = Image.new("RGB", (400, 100))
        out = _crop_to_ratio(img, 4, 1)
        self.assertEqual(out.size, (400, 100))  # 已是目标比例

    def test_wide_crops_height(self):
        img = Image.new("RGB", (400, 100))
        out = _crop_to_ratio(img, 16, 9)
        self.assertEqual(out.size, (int(100 * 16 / 9), 100))

    def test_tall_crops_width(self):
        img = Image.new("RGB", (100, 400))
        out = _crop_to_ratio(img, 16, 9)
        self.assertEqual(out.size, (100, int(100 * 9 / 16)))

    def test_center_crop_position(self):
        img = Image.new("RGB", (300, 100), "black")
        img.paste((255, 0, 0), (150, 0, 250, 100))  # 右侧红条
        out = _crop_to_ratio(img, 1, 1)
        self.assertEqual(out.size, (100, 100))
        # 中心裁剪取 x=100..200：红条（150..250）落在裁剪后右半（x 50..99）
        self.assertEqual(out.getpixel((75, 50))[:3], (255, 0, 0))
        self.assertEqual(out.getpixel((25, 50))[:3], (0, 0, 0))


class PadToSizeTests(unittest.TestCase):
    def test_exact_size(self):
        img = Image.new("RGB", (1920, 1080))
        out = _pad_to_size(img, 1920, 1080)
        self.assertEqual(out.size, (1920, 1080))

    def test_wide_pads_top_bottom(self):
        img = Image.new("RGB", (1920, 540), "white")
        out = _pad_to_size(img, 1920, 1080)
        self.assertEqual(out.size, (1920, 1080))
        self.assertEqual(out.getpixel((0, 0))[:3], (0, 0, 0))      # 上黑边
        self.assertEqual(out.getpixel((0, 1079))[:3], (0, 0, 0))   # 下黑边
        self.assertNotEqual(out.getpixel((960, 540))[:3], (0, 0, 0))  # 中间是图像


class PrepareCoverTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = patch.object(config, "DOWNLOAD_DIR", self.tmp.name)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.width_patcher = patch.object(config, "COVER_WIDTH", 1920)
        self.width_patcher.start()
        self.addCleanup(self.width_patcher.stop)
        self.height_patcher = patch.object(config, "COVER_HEIGHT", 1080)
        self.height_patcher.start()
        self.addCleanup(self.height_patcher.stop)

    def test_crop_mode_output_size(self):
        with patch.object(config, "COVER_FIT", "crop"):
            src = make_image(Path(self.tmp.name) / "src.jpg", (1280, 720))
            out = prepare_cover(src, video_id="v1")
        self.assertTrue(out)
        with Image.open(out) as img:
            self.assertEqual(img.size, (1920, 1080))

    def test_contain_mode_output_size(self):
        with patch.object(config, "COVER_FIT", "contain"):
            src = make_image(Path(self.tmp.name) / "src2.jpg", (1000, 500))
            out = prepare_cover(src, video_id="v2")
        with Image.open(out) as img:
            self.assertEqual(img.size, (1920, 1080))

    def test_invalid_source_returns_empty(self):
        out = prepare_cover(Path(self.tmp.name) / "missing.jpg", video_id="v3")
        self.assertEqual(out, "")

    def test_exif_orientation_transposed(self):
        """带 EXIF Orientation=6（旋转）的缩略图应被转置后再处理。"""
        with patch.object(config, "COVER_FIT", "crop"):
            src = Path(self.tmp.name) / "rot.jpg"
            img = Image.new("RGB", (100, 200), (10, 20, 30))
            exif = Image.Exif()
            exif[274] = 6  # Orientation: Rotate 90 CW
            img.save(src, exif=exif)
            out = prepare_cover(src, video_id="v4")
        with Image.open(out) as img:
            self.assertEqual(img.size, (1920, 1080))

    def test_unknown_fit_falls_back_to_crop(self):
        with patch.object(config, "COVER_FIT", "weird"):
            src = make_image(Path(self.tmp.name) / "src3.jpg", (1280, 720))
            out = prepare_cover(src, video_id="v5")
        with Image.open(out) as img:
            self.assertEqual(img.size, (1920, 1080))


class ImageSizeTests(unittest.TestCase):
    def test_valid(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "ok.jpg"
            make_image(p, (640, 480))
            self.assertEqual(image_size(p), (640, 480))

    def test_invalid(self):
        self.assertIsNone(image_size(None))


if __name__ == "__main__":
    unittest.main()
