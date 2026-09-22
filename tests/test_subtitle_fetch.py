"""自测：一条链接 → 字幕（下载 + 翻译 + 对齐时长）。

网络全部 mock：元数据、字幕下载、DeepSeek 翻译都被替换，只保留真实的分文件
写入和时长对齐逻辑。
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from yt2bili import config
from yt2bili.subtitles import fetch
from yt2bili.subtitles.downloader import SubtitleUnavailable
from yt2bili.subtitles.parser import Cue, parse_srt


def _cue(index, start, end, text):
    return Cue(index=index, start=start, end=end, text=text)


class NormalizeNumbersTests(unittest.TestCase):
    """千位分隔符统一去掉：模型时而半角 "2,000" 时而全角 "2，000" 不可靠。"""

    def test_strips_both_comma_widths(self):
        self.assertEqual(fetch.normalize_numbers("1,000 英雄券"), "1000 英雄券")
        self.assertEqual(fetch.normalize_numbers("2，500 闪闪币"), "2500 闪闪币")

    def test_handles_millions_and_multiple(self):
        self.assertEqual(
            fetch.normalize_numbers("1,000,000 金币和 2,000 宝石"),
            "1000000 金币和 2000 宝石",
        )

    def test_leaves_other_punctuation_alone(self):
        # 中文顿号、句号、标签里的逗号都不能动
        text = "5个混沌惊喜、20宝石，以及#BrawlStars"
        self.assertEqual(fetch.normalize_numbers(text), text)

    def test_does_not_touch_non_thousands_commas(self):
        # 逗号后面不是恰好三位数字 → 不是千位分隔符
        self.assertEqual(fetch.normalize_numbers("第1,2名"), "第1,2名")
        self.assertEqual(fetch.normalize_numbers("3,1415926"), "3,1415926")


class ResolveGlossaryGameTests(unittest.TestCase):
    """优先级：显式 --game > profile 配置 > 默认荒野乱斗。"""

    def test_explicit_wins(self):
        self.assertEqual(fetch.resolve_glossary_game("snap", "deadlock"), "snap")

    def test_profile_used_when_no_explicit(self):
        self.assertEqual(fetch.resolve_glossary_game("", "deadlock"), "deadlock")

    def test_defaults_to_brawl_stars(self):
        # 手动补字幕绝大多数是荒野乱斗；不能沿用 .env 里同时开着的多套开关
        self.assertEqual(fetch.resolve_glossary_game(), "brawl_stars")
        self.assertEqual(fetch.resolve_glossary_game("", ""), "brawl_stars")

    def test_blank_strings_do_not_win(self):
        self.assertEqual(fetch.resolve_glossary_game("  ", ""), "brawl_stars")


class FetchAndTranslateTests(unittest.TestCase):
    def _run(self, tmp, meta, cues, translated):
        """跑一次 fetch_and_translate，返回 (摘要, 输出目录)。"""
        src = Path(tmp) / "abc.en.srt"
        src.write_text("1\n00:00:00,000 --> 00:00:01,000\nhi\n", encoding="utf-8")
        with patch.object(fetch, "_extract_metadata", return_value=meta), \
             patch.object(fetch, "download_subtitles", return_value=str(src)), \
             patch.object(fetch, "translate_cues", return_value=translated), \
             patch.object(config, "SUBTITLE_DIR", tmp), \
             patch.object(config, "SUBTITLE_TARGET_LANG", "zh-CN"):
            return fetch.fetch_and_translate("https://youtu.be/abc")

    def test_writes_translated_srt(self):
        meta = {"id": "abc", "title": "T", "duration": 100.0}
        src_cues = [_cue(i, i * 4.0, i * 4.0 + 3.0, "en") for i in range(1, 6)]
        zh_cues = [_cue(i, i * 4.0, i * 4.0 + 3.0, "中文") for i in range(1, 6)]

        with tempfile.TemporaryDirectory() as tmp:
            result = self._run(tmp, meta, src_cues, zh_cues)
            out = Path(result["translated_path"])

            self.assertTrue(out.exists())
            self.assertEqual(out.name, "abc.zh-CN.srt")
            self.assertEqual(result["cues"], 5)
            self.assertEqual(result["dropped"], 0)
            self.assertEqual(result["clamped"], 0)
            self.assertEqual([c.text for c in parse_srt(out)], ["中文"] * 5)

    def test_cues_past_video_end_are_dropped_and_clamped(self):
        """对齐时间：超出时长的丢弃，跨界的截断（含 margin）。"""
        meta = {"id": "abc", "title": "T", "duration": 20.0}
        src = [_cue(i, i * 5.0, i * 5.0 + 4.0, "en") for i in range(1, 5)]
        # 第 4 条 start=15 end=19 正常；第 5 条 start=20 应被丢弃
        passed = [
            _cue(1, 0.0, 4.0, "一"),
            _cue(2, 5.0, 9.0, "二"),
            _cue(3, 10.0, 14.0, "三"),
            _cue(4, 15.0, 25.0, "四"),   # 跨界 → 截断到 20 - 0.5
            _cue(5, 20.0, 24.0, "五"),   # 起头就超 → 丢弃
        ]
        with tempfile.TemporaryDirectory() as tmp:
            result = self._run(tmp, meta, src, passed)

            self.assertEqual(result["dropped"], 1)
            self.assertEqual(result["clamped"], 1)
            self.assertEqual(result["cues"], 4)
            cues = parse_srt(result["translated_path"])
            self.assertEqual(cues[-1].end, 19.5)  # duration - margin

    def test_missing_video_id_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(RuntimeError):
                self._run(tmp, {"id": "", "duration": 10.0}, [], [])

    def test_no_subtitles_raises(self):
        """源字幕拿不到时原样抛出：消息由 downloader 给出，kind 保留给调用方。"""
        meta = {"id": "abc", "title": "T", "duration": 100.0}
        err = SubtitleUnavailable("no_match", "没有匹配 SUBTITLE_SOURCE_LANGS 的语言")
        with patch.object(fetch, "_extract_metadata", return_value=meta), \
             patch.object(fetch, "download_subtitles", side_effect=err):
            with self.assertRaises(RuntimeError) as ctx:
                fetch.fetch_and_translate("https://youtu.be/abc")
        self.assertIsInstance(ctx.exception, SubtitleUnavailable)
        self.assertEqual(ctx.exception.kind, "no_match")
        self.assertTrue(str(ctx.exception))

    def test_zero_duration_skips_alignment(self):
        """拿不到时长时不能把字幕清空，原样写出。"""
        meta = {"id": "abc", "title": "T", "duration": 0}
        src_cues = [_cue(i, i * 4.0, i * 4.0 + 3.0, "en") for i in range(1, 5)]
        zh = [_cue(i, i * 4.0, i * 4.0 + 3.0, "中文") for i in range(1, 5)]
        with tempfile.TemporaryDirectory() as tmp:
            result = self._run(tmp, meta, src_cues, zh)
            self.assertEqual(result["cues"], 4)
            self.assertEqual(result["dropped"], 0)
            self.assertEqual(result["clamped"], 0)


if __name__ == "__main__":
    unittest.main()
