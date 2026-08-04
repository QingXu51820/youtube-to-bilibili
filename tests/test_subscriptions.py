"""自测：YouTube 订阅模块（时间解析、排序去重、频道解析、RSS/API 错误分类）。"""

import json
import sys
import tempfile
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from yt2bili.youtube.subscriptions import (
    Subscription,
    VideoItem,
    YouTubeNetworkError,
    _api_http_error,
    load_channels_file,
    load_subscriptions_cache,
    normalize_published_at,
    parse_datetime,
    resolve_channel_handle_ytdlp,
    sort_videos,
    unique_by_video_id,
)


def make_video(video_id, published):
    return VideoItem(
        title="T", channel_title="C", published_at=published,
        url=f"https://www.youtube.com/watch?v={video_id}",
        channel_id="UCx", video_id=video_id,
    )


class ParseDatetimeTests(unittest.TestCase):
    MIN = datetime.min.replace(tzinfo=timezone.utc)

    def test_iso_with_z(self):
        dt = parse_datetime("2026-08-04T10:00:00Z")
        self.assertEqual(dt, datetime(2026, 8, 4, 10, 0, tzinfo=timezone.utc))

    def test_iso_with_offset_converted_to_utc(self):
        dt = parse_datetime("2026-08-04T18:00:00+08:00")
        self.assertEqual(dt, datetime(2026, 8, 4, 10, 0, tzinfo=timezone.utc))

    def test_rfc822(self):
        dt = parse_datetime("Tue, 04 Aug 2026 10:00:00 +0000")
        self.assertEqual(dt, datetime(2026, 8, 4, 10, 0, tzinfo=timezone.utc))

    def test_garbage_returns_min_utc(self):
        """回归：一条坏日期不能中断整个轮询 —— 返回 datetime.min。"""
        self.assertEqual(parse_datetime("not a date"), self.MIN)
        self.assertEqual(parse_datetime(""), self.MIN)
        self.assertEqual(parse_datetime(None), self.MIN)

    def test_naive_gets_utc(self):
        dt = parse_datetime("2026-08-04T10:00:00")
        self.assertEqual(dt.tzinfo, timezone.utc)

    def test_normalize_published_at(self):
        self.assertEqual(
            normalize_published_at("2026-08-04T18:00:00+08:00"),
            "2026-08-04T10:00:00Z",
        )


class SortAndDedupTests(unittest.TestCase):
    def test_unique_by_id_keeps_first(self):
        videos = [make_video("a", "2026-01-01T00:00:00Z"),
                  make_video("a", "2026-02-01T00:00:00Z"),
                  make_video("b", "2026-01-01T00:00:00Z")]
        result = unique_by_video_id(videos)
        self.assertEqual([v.video_id for v in result], ["a", "b"])

    def test_sort_videos_newest_first(self):
        videos = [make_video("old", "2026-01-01T00:00:00Z"),
                  make_video("new", "2026-06-01T00:00:00Z"),
                  make_video("mid", "2026-03-01T00:00:00Z")]
        result = sort_videos(videos)
        self.assertEqual([v.video_id for v in result], ["new", "mid", "old"])

    def test_sort_bad_dates_sort_last(self):
        videos = [make_video("bad", "garbage"),
                  make_video("good", "2026-01-01T00:00:00Z")]
        result = sort_videos(videos)
        self.assertEqual([v.video_id for v in result], ["good", "bad"])


class ResolveChannelHandleTests(unittest.TestCase):
    """回归：25+ 字符的 UC 前缀输入不得被静默截断成 channel ID。"""

    def _install_fake_ytdlp(self):
        captured = {}

        class FakeYDL:
            def __init__(self, opts):
                self.opts = opts

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def extract_info(self, url, download=False):
                captured["url"] = url
                return {"channel_id": "UC" + "1" * 22, "channel": "Fake Chan"}

        module = types.ModuleType("yt_dlp")
        module.YoutubeDL = FakeYDL
        patcher = patch.dict(sys.modules, {"yt_dlp": module})
        patcher.start()
        self.addCleanup(patcher.stop)
        return captured

    def test_24_char_id_uses_channel_url(self):
        captured = self._install_fake_ytdlp()
        raw = "UC" + "a" * 22
        cid, title = resolve_channel_handle_ytdlp(raw)
        self.assertEqual(captured["url"], f"https://www.youtube.com/channel/{raw}")
        self.assertEqual(cid, "UC" + "1" * 22)

    def test_25_char_not_truncated(self):
        """25 字符 UC 字符串 → 不当作 channel ID，走 @ 解析路径。"""
        captured = self._install_fake_ytdlp()
        raw = "UC" + "a" * 23
        resolve_channel_handle_ytdlp(raw)
        self.assertEqual(captured["url"], f"https://www.youtube.com/@{raw}")

    def test_at_handle(self):
        captured = self._install_fake_ytdlp()
        resolve_channel_handle_ytdlp("@MarvelSnap")
        self.assertEqual(captured["url"], "https://www.youtube.com/@MarvelSnap")

    def test_full_url_preserved(self):
        captured = self._install_fake_ytdlp()
        url = "https://www.youtube.com/@SomeChannel/videos"
        resolve_channel_handle_ytdlp(url)
        self.assertEqual(captured["url"], url)

    def test_resolution_failure_raises_value_error(self):
        captured = {}

        class FakeYDL:
            def __init__(self, opts): pass
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def extract_info(self, url, download=False):
                raise Exception("video unavailable")

        module = types.ModuleType("yt_dlp")
        module.YoutubeDL = FakeYDL
        with patch.dict(sys.modules, {"yt_dlp": module}):
            with self.assertRaises(ValueError):
                resolve_channel_handle_ytdlp("@Broken")


class ChannelsFileTests(unittest.TestCase):
    def test_load_text_channels(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "channels.txt"
            path.write_text(
                "# comment\nUC111\nUC222,Channel Two\nUC333\tTabbed\n\n",
                encoding="utf-8",
            )
            subs = load_channels_file(path)
        self.assertEqual([s.channel_id for s in subs], ["UC111", "UC222", "UC333"])
        self.assertEqual(subs[1].channel_title, "Channel Two")
        self.assertEqual(subs[2].channel_title, "Tabbed")

    def test_missing_file_raises_system_exit(self):
        with self.assertRaises(SystemExit):
            load_channels_file(Path("nope.txt"))

    def test_load_json_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cache.json"
            path.write_text(json.dumps({"subscriptions": [
                {"channel_id": "UC111", "channel_title": "One"},
                {"channel_id": "UC222", "channel_title": "Two"},
            ]}), encoding="utf-8")
            subs = load_subscriptions_cache(path)
        self.assertEqual([s.channel_id for s in subs], ["UC111", "UC222"])

    def test_load_list_format_and_legacy_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cache.json"
            path.write_text(json.dumps([
                {"channelId": "UC111", "channelTitle": "One"},
                {"id": "UC222", "title": "Two"},
                {"channel_title": "missing id"},
            ]), encoding="utf-8")
            subs = load_subscriptions_cache(path)
        self.assertEqual([s.channel_id for s in subs], ["UC111", "UC222"])


class ApiHttpErrorClassificationTests(unittest.TestCase):
    def _resp(self, status, payload=None):
        if payload is None:
            payload = {"error": {"message": "x"}}
        return SimpleNamespace(status_code=status, json=lambda: payload, text="raw")

    def test_5xx_retryable(self):
        for status in (500, 502, 503):
            with self.subTest(status=status):
                err = _api_http_error(self._resp(status))
                self.assertIsInstance(err, YouTubeNetworkError)

    def test_429_and_403_retryable(self):
        """回归：配额耗尽（429/403）必须是可重试错误，不能杀 monitor。"""
        for status in (429, 403):
            with self.subTest(status=status):
                err = _api_http_error(self._resp(status))
                self.assertIsInstance(err, YouTubeNetworkError)

    def test_other_4xx_not_retryable(self):
        for status in (400, 401, 404):
            with self.subTest(status=status):
                err = _api_http_error(self._resp(status))
                self.assertIsInstance(err, SystemExit)


if __name__ == "__main__":
    unittest.main()
