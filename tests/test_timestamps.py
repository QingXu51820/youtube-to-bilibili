"""自测：状态文件时间戳的唯一格式与解析。

回归背景：同一个格式曾有四份生成实现、五份解析实现，而它们并不等价 ——
collection 的那份缺 naive→UTC 回退，读到无时区值时会拿 naive 与 aware 相减抛
TypeError 打断整轮 sweep；subscriptions 的那份漏了秒精度。
"""

import sys
import unittest
from datetime import datetime, timedelta, timezone

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from yt2bili.timestamps import BEIJING_TZ, beijing_now, parse_iso, utc_now


class UtcNowTests(unittest.TestCase):
    def test_second_precision_with_z_suffix(self):
        self.assertRegex(utc_now(), r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

    def test_is_utc_and_recent(self):
        stamp = parse_iso(utc_now())
        self.assertEqual(stamp.tzinfo, timezone.utc)
        self.assertLess(abs((datetime.now(timezone.utc) - stamp).total_seconds()), 5)


class BeijingNowTests(unittest.TestCase):
    def test_offset_is_plus_eight(self):
        stamp = parse_iso(beijing_now())
        self.assertEqual(stamp.utcoffset(), timedelta(hours=8))
        self.assertEqual(BEIJING_TZ.utcoffset(None), timedelta(hours=8))

    def test_same_instant_as_utc_now(self):
        utc = parse_iso(utc_now())
        beijing = parse_iso(beijing_now())
        self.assertLess(abs((utc - beijing).total_seconds()), 5)


class ParseIsoTests(unittest.TestCase):
    def test_parses_z_suffix(self):
        stamp = parse_iso("2026-07-16T12:36:13Z")
        self.assertEqual(stamp, datetime(2026, 7, 16, 12, 36, 13, tzinfo=timezone.utc))

    def test_parses_explicit_offset(self):
        stamp = parse_iso("2026-07-16T20:36:13+08:00")
        self.assertEqual(stamp.astimezone(timezone.utc).hour, 12)

    def test_naive_value_is_treated_as_utc(self):
        """回归：naive 值直接与 aware 相减会抛 TypeError。"""
        stamp = parse_iso("2026-07-16T12:36:13")
        self.assertEqual(stamp.tzinfo, timezone.utc)
        self.assertLess((datetime.now(timezone.utc) - stamp).total_seconds(), 10**12)

    def test_empty_and_missing_return_none(self):
        for value in ("", None, "   "):
            with self.subTest(value=value):
                self.assertIsNone(parse_iso(value))

    def test_garbage_returns_none(self):
        for value in ("不是时间", "2026-13-45T99:99:99Z", 12345):
            with self.subTest(value=value):
                self.assertIsNone(parse_iso(value))


if __name__ == "__main__":
    unittest.main()
