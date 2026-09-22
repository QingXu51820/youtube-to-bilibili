"""自测：YouTube 源字幕下载的失败归类与重试边界。

背景：源字幕失败以前一律 `return None`，「还没生成」「语言不匹配」「网络失败」
三种情况在调用方看来一模一样，于是全部被当成非致命错误丢掉。
`download_subtitles` 现在抛 `SubtitleUnavailable`（带 `kind`），
调用方据此决定是挂进延迟队列重试还是当场放弃。
"""

import sys
import types
import unittest
from unittest.mock import patch

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from yt2bili.subtitles import downloader as dl


class _FakeYDL:
    def __init__(self, result=None, exc=None):
        self._result = result
        self._exc = exc

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def extract_info(self, url, download=False):
        if self._exc is not None:
            raise self._exc
        return self._result


def _fake_yt_dlp(result=None, exc=None):
    """把 yt_dlp 换成假模块（与 tests/test_subscriptions.py 同一手法）。"""
    module = types.ModuleType("yt_dlp")
    module.YoutubeDL = lambda opts: _FakeYDL(result, exc)
    return patch.dict(sys.modules, {"yt_dlp": module})


_NO_TRACKS_INFO = {"id": "vid", "subtitles": {}, "automatic_captions": {}}
_EN_INFO = {"id": "vid", "subtitles": {}, "automatic_captions": {"en-orig": [{}]}}


class DownloadSubtitlesClassificationTests(unittest.TestCase):
    """download_subtitles：失败抛分类异常，成功返回路径。"""

    def _call(self, list_results, *, download=None, defect=None):
        with patch.object(dl, "_list_languages", side_effect=list_results), \
             patch.object(dl, "_download_subtitles_for_lang", return_value=download), \
             patch.object(dl, "parse_subtitle", return_value=[]), \
             patch.object(dl.resegment, "timing_defect", return_value=defect):
            return dl.download_subtitles("https://youtu.be/x", "vid")

    def test_success_returns_path(self):
        path = self._call([({}, {"en-orig": {}}, True)], download="/t/vid.en-orig.srt")
        self.assertEqual(path, "/t/vid.en-orig.srt")

    def test_no_tracks(self):
        """取到了信息但一条字幕轨都没有 —— 可以稍后再试。"""
        with self.assertRaises(dl.SubtitleUnavailable) as ctx:
            self._call([({}, {}, True)])
        self.assertEqual(ctx.exception.kind, "no_tracks")
        self.assertFalse(ctx.exception.permanent)
        self.assertTrue(dl.is_retryable_kind("no_tracks"))

    def test_no_match_lists_available(self):
        """有轨但语言不匹配（该频道只有 zh-HK）—— 永久，重试无意义。"""
        with self.assertRaises(dl.SubtitleUnavailable) as ctx:
            self._call([({"zh-HK": {}}, {}, True)])
        self.assertEqual(ctx.exception.kind, "no_match")
        self.assertEqual(ctx.exception.available, ["zh-HK"])
        self.assertTrue(ctx.exception.permanent)
        self.assertIn("zh-HK", str(ctx.exception))

    def test_list_failed_retries_once(self):
        """列轨失败会立即重列一次，仍失败才抛。"""
        with patch.object(dl, "_list_languages",
                          side_effect=[({}, {}, False), ({}, {}, False)]) as mock:
            with self.assertRaises(dl.SubtitleUnavailable) as ctx:
                with patch.object(dl, "_download_subtitles_for_lang"):
                    dl.download_subtitles("https://youtu.be/x", "vid")
        self.assertEqual(ctx.exception.kind, "list_failed")
        self.assertEqual(mock.call_count, 2)

    def test_list_failed_recovers_on_second_try(self):
        """第一次列轨失败、重列成功 —— 不该白白等一轮延迟队列。"""
        path = self._call(
            [({}, {}, False), ({}, {"en-orig": {}}, True)],
            download="/t/vid.en-orig.srt",
        )
        self.assertEqual(path, "/t/vid.en-orig.srt")

    def test_download_failed(self):
        """选中了语言却没下到文件 —— 网络类，可重试。"""
        with self.assertRaises(dl.SubtitleUnavailable) as ctx:
            self._call([({}, {"en-orig": {}}, True)], download=None)
        self.assertEqual(ctx.exception.kind, "download_failed")
        self.assertFalse(ctx.exception.permanent)

    def test_bad_timing_is_permanent(self):
        """时间轴崩坏的字幕传上去比没有更糟 —— 永久放弃。"""
        with self.assertRaises(dl.SubtitleUnavailable) as ctx:
            self._call([({}, {"en-orig": {}}, True)],
                       download="/t/vid.en-orig.srt", defect="中位时长 0.40s")
        self.assertEqual(ctx.exception.kind, "bad_timing")
        self.assertTrue(ctx.exception.permanent)

    def test_unknown_kind_treated_as_permanent(self):
        """名单外的 kind 一律不重试 —— 默认安全。"""
        self.assertTrue(dl.SubtitleUnavailable("weird", "x").permanent)
        self.assertFalse(dl.is_retryable_kind(""))


class ListLanguagesTests(unittest.TestCase):
    """_list_languages：区分「问不到」和「问了，没有轨」。"""

    def _call(self, *, bare=None, bare_exc=None, cookie=None, cookie_exc=None):
        def _cookies(opts, fn, **kwargs):
            if cookie_exc is not None:
                raise cookie_exc
            return fn(_FakeYDL(cookie))

        with _fake_yt_dlp(bare, bare_exc), \
             patch.object(dl, "_with_stderr_suppressed", side_effect=lambda fn: fn()), \
             patch.object(dl, "_with_yt_dlp_cookies", side_effect=_cookies):
            return dl._list_languages("https://youtu.be/x")

    def test_bare_ok_with_tracks(self):
        subs, autos, ok = self._call(bare=_EN_INFO)
        self.assertTrue(ok)
        self.assertEqual(list(autos), ["en-orig"])

    def test_bare_ok_but_no_tracks(self):
        """裸提取成功、只是没有轨 —— 这是「没有」，不是「问不到」。"""
        subs, autos, ok = self._call(bare=_NO_TRACKS_INFO, cookie_exc=RuntimeError("boom"))
        self.assertTrue(ok)
        self.assertEqual((subs, autos), ({}, {}))

    def test_all_extractions_failed(self):
        """两次都拿不到信息 —— 必须报 ok=False，否则会被当成「视频没字幕」。"""
        subs, autos, ok = self._call(bare_exc=RuntimeError("SSL EOF"),
                                     cookie_exc=RuntimeError("SSL EOF"))
        self.assertFalse(ok)
        self.assertEqual((subs, autos), ({}, {}))

    def test_cookie_fallback_supplies_tracks(self):
        subs, autos, ok = self._call(bare_exc=RuntimeError("bot check"), cookie=_EN_INFO)
        self.assertTrue(ok)
        self.assertEqual(list(autos), ["en-orig"])


if __name__ == "__main__":
    unittest.main()
