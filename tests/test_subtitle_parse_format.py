"""自测：字幕解析（SRT/VTT）与 B站字幕 JSON 格式（时长 clamp）。"""

import io
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

from yt2bili.subtitles import bilibili_format as bf
from yt2bili.subtitles import writer
from yt2bili.subtitles import parser as parser_mod
from yt2bili.subtitles.parser import (
    Cue,
    _parse_srt_text,
    _parse_vtt_text,
    _srt_timestamp_to_seconds,
    parse_subtitle,
)


# ── SRT/VTT 解析 ──────────────────────────────────────────────────────

class SrtTimestampTests(unittest.TestCase):
    def test_millisecond_comma(self):
        self.assertAlmostEqual(_srt_timestamp_to_seconds("00:01:02,345"), 62.345)

    def test_millisecond_dot(self):
        self.assertAlmostEqual(_srt_timestamp_to_seconds("01:02:03.500"), 3723.5)

    def test_invalid_raises(self):
        with self.assertRaises(ValueError):
            _srt_timestamp_to_seconds("not a timestamp")


class ParseSrtTextTests(unittest.TestCase):
    def test_multiline_cue_text_joined_with_newline(self):
        text = (
            "1\n"
            "00:00:01,000 --> 00:00:04,000\n"
            "Hello world\n"
            "This is line two\n"
            "\n"
            "2\n"
            "00:00:05,000 --> 00:00:08,000\n"
            "Second cue\n"
        )
        cues = _parse_srt_text(text)
        self.assertEqual(len(cues), 2)
        self.assertEqual(cues[0].text, "Hello world\nThis is line two")
        self.assertAlmostEqual(cues[0].start, 1.0)
        self.assertAlmostEqual(cues[0].end, 4.0)

    def test_reindexes_gaps(self):
        text = (
            "5\n"
            "00:00:01,000 --> 00:00:02,000\n"
            "A\n"
            "\n"
            "9\n"
            "00:00:03,000 --> 00:00:04,000\n"
            "B\n"
        )
        cues = _parse_srt_text(text)
        self.assertEqual([c.index for c in cues], [1, 2])

    def test_malformed_block_skipped(self):
        text = (
            "1\n"
            "garbage timestamp\n"
            "text\n"
            "\n"
            "2\n"
            "00:00:01,000 --> 00:00:02,000\n"
            "ok\n"
        )
        cues = _parse_srt_text(text)
        self.assertEqual(len(cues), 1)
        self.assertEqual(cues[0].text, "ok")

    def test_dot_millisecond_separator_accepted(self):
        text = (
            "1\n"
            "00:00:01.230 --> 00:00:04.560\n"
            "A\n"
        )
        cues = _parse_srt_text(text)
        self.assertAlmostEqual(cues[0].start, 1.23)

    def test_bom_stripped_at_file_level(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sub.srt"
            path.write_bytes(
                "﻿1\n00:00:01,000 --> 00:00:02,000\n你好\n".encode("utf-8")
            )
            cues = parse_subtitle(path)
        self.assertEqual(len(cues), 1)
        self.assertEqual(cues[0].text, "你好")


class ParseVttTextTests(unittest.TestCase):
    def test_header_stripped_and_hours_optional(self):
        text = (
            "WEBVTT\n"
            "\n"
            "00:01.000 --> 00:03.000\n"
            "First\n"
            "\n"
            "01:02:03.000 --> 01:02:04.000\n"
            "With hours\n"
        )
        cues = _parse_vtt_text(text)
        self.assertEqual(len(cues), 2)
        self.assertAlmostEqual(cues[0].start, 1.0)
        self.assertAlmostEqual(cues[1].start, 3723.0)

    def test_cue_identifier_line(self):
        text = (
            "WEBVTT\n"
            "\n"
            "intro\n"
            "00:00:01.000 --> 00:00:02.000\n"
            "Hello\n"
        )
        cues = _parse_vtt_text(text)
        self.assertEqual(len(cues), 1)
        self.assertEqual(cues[0].text, "Hello")


class ParseSubtitleDispatchTests(unittest.TestCase):
    def test_unsupported_extension(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sub.txt"
            path.write_text("x", encoding="utf-8")
            with self.assertRaises(ValueError):
                parse_subtitle(path)

    def test_missing_file_raises(self):
        with self.assertRaises(FileNotFoundError):
            parse_subtitle("does_not_exist.srt")


# ── clamp_cues_to_duration ───────────────────────────────────────────

class ClampCuesTests(unittest.TestCase):
    def test_none_duration_returns_unchanged(self):
        cues = [Cue(1, 1.0, 10.0, "a")]
        kept, dropped, clamped = bf.clamp_cues_to_duration(cues, None)
        self.assertEqual(kept, cues)
        self.assertEqual((dropped, clamped), (0, 0))

    def test_nonpositive_duration_returns_unchanged(self):
        cues = [Cue(1, 1.0, 2.0, "a")]
        kept, _, _ = bf.clamp_cues_to_duration(cues, 0)
        self.assertEqual(kept, cues)

    def test_cue_straddling_end_is_clamped(self):
        cues = [Cue(1, 1.0, 12.0, "a")]
        kept, dropped, clamped = bf.clamp_cues_to_duration(cues, 10.0)
        self.assertEqual(dropped, 0)
        self.assertEqual(clamped, 1)
        self.assertAlmostEqual(kept[0].end, 10.0)
        self.assertEqual(kept[0].text, "a")

    def test_cue_starting_past_limit_is_dropped(self):
        cues = [
            Cue(1, 1.0, 5.0, "a"),
            Cue(2, 11.0, 15.0, "b"),
            Cue(3, 10.0, 20.0, "c"),  # start == limit → drop
        ]
        kept, dropped, clamped = bf.clamp_cues_to_duration(cues, 10.0)
        self.assertEqual(len(kept), 1)
        self.assertEqual(dropped, 2)
        self.assertEqual(clamped, 0)

    def test_margin_subtracted_from_limit(self):
        cues = [Cue(1, 1.0, 12.0, "a")]
        kept, _, clamped = bf.clamp_cues_to_duration(cues, 10.0, margin=0.5)
        self.assertEqual(clamped, 1)
        self.assertAlmostEqual(kept[0].end, 9.5)

    def test_input_cues_not_mutated(self):
        """回归：clamp 必须返回新 Cue，不能修改调用方的对象。"""
        original = Cue(1, 1.0, 12.0, "a")
        kept, _, _ = bf.clamp_cues_to_duration([original], 10.0)
        self.assertNotEqual(kept[0], original)
        self.assertAlmostEqual(original.end, 12.0)  # 原对象不变


# ── cues_to_bilibili_json ────────────────────────────────────────────

class CuesToBilibiliJsonTests(unittest.TestCase):
    def test_body_format(self):
        cues = [Cue(1, 1.2345, 4.5678, "你好")]
        payload = bf.cues_to_bilibili_json(cues)
        self.assertEqual(payload["font_size"], 0.4)
        self.assertEqual(payload["Stroke"], "none")
        self.assertEqual(len(payload["body"]), 1)
        body = payload["body"][0]
        self.assertAlmostEqual(body["from"], 1.234)
        self.assertAlmostEqual(body["to"], 4.568)
        self.assertEqual(body["location"], 2)
        self.assertEqual(body["content"], "你好")

    def test_video_duration_clamps_and_warns(self):
        cues = [Cue(1, 1.0, 20.0, "a")]
        err = io.StringIO()
        with redirect_stderr(err):
            payload = bf.cues_to_bilibili_json(cues, video_duration=10.0, margin=0.5)
        self.assertAlmostEqual(payload["body"][0]["to"], 9.5)
        self.assertIn("已修正 1 条", err.getvalue())

    def test_cue_beyond_duration_dropped(self):
        cues = [Cue(1, 1.0, 2.0, "a"), Cue(2, 15.0, 16.0, "b")]
        err = io.StringIO()
        with redirect_stderr(err):
            payload = bf.cues_to_bilibili_json(cues, video_duration=10.0)
        self.assertEqual(len(payload["body"]), 1)
        self.assertIn("已跳过 1 条", err.getvalue())

    def test_content_truncated_at_80_chars(self):
        cues = [Cue(1, 0.0, 1.0, "字" * 100)]
        err = io.StringIO()
        with redirect_stderr(err):
            payload = bf.cues_to_bilibili_json(cues, warn_overlength=True)
        content = payload["body"][0]["content"]
        self.assertEqual(len(content), 81)  # 80 + "…"
        self.assertTrue(content.endswith("…"))
        self.assertIn("字幕过长", err.getvalue())

    def test_zero_duration_cue_extended_to_minimum(self):
        """回归：YouTube 自动字幕的 0 时长 cue 会被 B站 79014 拒绝，需延长。"""
        cues = [Cue(1, 5.0, 5.0, ">> 鼓掌"), Cue(2, 5.5, 6.5, "正常")]
        err = io.StringIO()
        with redirect_stderr(err):
            payload = bf.cues_to_bilibili_json(cues)
        body = payload["body"]
        self.assertGreater(body[0]["to"], body[0]["from"])
        self.assertAlmostEqual(
            body[0]["to"] - body[0]["from"], bf._MIN_CUE_DURATION, places=3
        )
        self.assertAlmostEqual(body[1]["to"], 6.5)  # 正常 cue 不受影响
        self.assertIn("持续时间为 0", err.getvalue())

    def test_negative_duration_cue_extended_to_minimum(self):
        """end < start 的反向时间轴同样修复。"""
        cues = [Cue(1, 6.0, 4.0, "异常")]
        payload = bf.cues_to_bilibili_json(cues)
        self.assertGreater(payload["body"][0]["to"], payload["body"][0]["from"])


class ParseSrtFileTests(unittest.TestCase):
    """parse_srt 文件级包装（内部 _parse_srt_text 已测，这里测文件读取）。"""

    def test_file_level_parse(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "a.srt"
            p.write_text("1\n00:00:01,000 --> 00:00:02,500\nHi\n", encoding="utf-8")
            cues = parser_mod.parse_srt(p)
        self.assertEqual(len(cues), 1)
        self.assertEqual(cues[0].text, "Hi")
        self.assertAlmostEqual(cues[0].start, 1.0)

    def test_bom_stripped_at_file_level(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "a.srt"
            p.write_bytes(b"\xef\xbb\xbf1\n00:00:01,000 --> 00:00:02,000\nHi\n")
            cues = parser_mod.parse_srt(p)
        self.assertEqual(len(cues), 1)

    def test_missing_file_raises(self):
        with self.assertRaises(FileNotFoundError):
            parser_mod.parse_srt("no/such.srt")

    def test_parse_vtt_file_level(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "a.vtt"
            p.write_text("WEBVTT\n\n00:01.000 --> 00:04.500\nHello VTT\n", encoding="utf-8")
            cues = parser_mod.parse_vtt(p)
        self.assertEqual(cues[0].text, "Hello VTT")
        self.assertAlmostEqual(cues[0].start, 1.0)

    def test_parse_vtt_missing_file_raises(self):
        with self.assertRaises(FileNotFoundError):
            parser_mod.parse_vtt("no/such.vtt")


class WriteSrtTests(unittest.TestCase):
    """write_srt：写出标准 SRT 文件、序号重编号、时间戳格式化。"""

    def test_writes_standard_srt_format(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out.srt"
            result = writer.write_srt([
                Cue(index=5, start=1.0, end=2.5, text="第一行"),
                Cue(index=9, start=10.0, end=11.0, text="第二行"),
            ], out)
            content = out.read_text(encoding="utf-8")
        self.assertIn("00:00:01,000 --> 00:00:02,500", content)
        self.assertIn("第一行", content)
        self.assertIn("第二行", content)
        # 序号按输出顺序重新编号，忽略输入 index
        digits = [l for l in content.splitlines() if l.strip().isdigit()]
        self.assertEqual(digits, ["1", "2"])
        # 返回绝对路径
        self.assertEqual(result, str(out.resolve()))

    def test_creates_parent_dirs(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "sub" / "deep" / "out.srt"
            writer.write_srt([Cue(index=1, start=0.0, end=1.0, text="x")], out)
            self.assertTrue(out.exists())

    def test_write_then_parse_roundtrip(self):
        """写出的 SRT 能被 parse_srt 原样读回。"""
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "rt.srt"
            writer.write_srt([Cue(index=1, start=1.5, end=4.0, text="round trip")], out)
            cues = parser_mod.parse_srt(out)
        self.assertEqual(cues[0].text, "round trip")
        self.assertAlmostEqual(cues[0].start, 1.5)
        self.assertAlmostEqual(cues[0].end, 4.0)

    def test_millisecond_overflow_clamped(self):
        """1.9996s 的毫秒进位到 1000 时归零并进秒。"""
        self.assertEqual(writer._seconds_to_srt_timestamp(1.9996), "00:00:02,000")


if __name__ == "__main__":
    unittest.main()
