"""自测：字幕文件命名约定（{video_id}.{lang}.srt）。

回归背景：这个约定曾被三个模块各拼一遍，video_id 的提取也有
``split(".")[0]`` 与 ``split(".", 1)[0]`` 两种写法。
"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from yt2bili import config
from yt2bili.subtitles import paths


class VideoIdFromFilenameTests(unittest.TestCase):
    def test_strips_language_and_extension(self):
        self.assertEqual(paths.video_id_from_filename("abc123.zh-CN.srt"), "abc123")

    def test_accepts_a_full_path(self):
        self.assertEqual(
            paths.video_id_from_filename(r"D:\dl\subtitles\abc123.en.srt"), "abc123"
        )

    def test_video_id_containing_dashes_and_underscores(self):
        self.assertEqual(
            paths.video_id_from_filename("hPXnQ-hO6S8.en-orig.srt"), "hPXnQ-hO6S8"
        )

    def test_only_the_first_dot_separates(self):
        """只切第一段：语言标记里的点不该影响 video_id。"""
        self.assertEqual(paths.video_id_from_filename("abc.en.srt"), "abc")

    def test_empty_input(self):
        self.assertEqual(paths.video_id_from_filename(""), "")


class PathTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        patcher = patch.object(config, "SUBTITLE_DIR", self._tmp.name)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_translated_path_uses_target_lang(self):
        with patch.object(config, "SUBTITLE_TARGET_LANG", "zh-CN"):
            self.assertEqual(
                paths.translated_srt_path("abc"),
                Path(self._tmp.name) / "abc.zh-CN.srt",
            )

    def test_source_path_defaults_to_en_orig(self):
        self.assertEqual(
            paths.source_srt_path("abc"), Path(self._tmp.name) / "abc.en-orig.srt"
        )

    def test_is_translated_name_follows_the_target_lang(self):
        with patch.object(config, "SUBTITLE_TARGET_LANG", "zh-CN"):
            self.assertTrue(paths.is_translated_name("abc.zh-CN.srt"))
            self.assertFalse(paths.is_translated_name("abc.en-orig.srt"))

    def test_roundtrip_of_a_translated_path(self):
        """translated_srt_path 的产物必须能被 video_id_from_filename 还原。"""
        with patch.object(config, "SUBTITLE_TARGET_LANG", "zh-CN"):
            path = paths.translated_srt_path("abc123")
        self.assertEqual(paths.video_id_from_filename(path), "abc123")


if __name__ == "__main__":
    unittest.main()
