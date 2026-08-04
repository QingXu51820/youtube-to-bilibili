"""自测：下载器核心（download_video 全分支、refresh_youtube_cookies 浏览器导出）。"""

import http.cookiejar
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from yt2bili import config
from yt2bili.youtube import downloader


def _yt_cookie(domain=".youtube.com", name="SID"):
    return http.cookiejar.Cookie(
        version=0, name=name, value="v", port=None, port_specified=False,
        domain=domain, domain_specified=True, domain_initial_dot=True,
        path="/", path_specified=True, secure=True, expires=None,
        discard=True, comment=None, comment_url=None, rest={},
    )


class RefreshYoutubeCookiesTests(unittest.TestCase):
    """refresh_youtube_cookies：浏览器 Cookie 导出与过滤。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _patch_env(self, cookie_file="config/cookies.txt", browsers="chrome"):
        stack = ExitStack()
        stack.enter_context(patch.object(config, "YOUTUBE_COOKIE_FILE", cookie_file))
        stack.enter_context(patch.object(config, "YOUTUBE_COOKIES_FROM_BROWSER", browsers))
        return stack

    def test_unconfigured_cookie_file_returns_none(self):
        with patch.object(config, "YOUTUBE_COOKIE_FILE", ""), \
             patch.object(config, "YOUTUBE_COOKIES_FROM_BROWSER", "chrome"):
            result = downloader.refresh_youtube_cookies(quiet=True)
        self.assertIsNone(result)

    def test_no_browsers_returns_none(self):
        with patch.object(config, "YOUTUBE_COOKIE_FILE", "c.txt"), \
             patch.object(config, "YOUTUBE_COOKIES_FROM_BROWSER", ""):
            result = downloader.refresh_youtube_cookies(quiet=True)
        self.assertIsNone(result)

    def test_success_writes_filtered_cookies(self):
        out = Path(self.tmp.name) / "cookies.txt"
        jar = MagicMock()
        jar.__iter__.return_value = iter([
            _yt_cookie(domain=".youtube.com", name="SID"),
            _yt_cookie(domain=".google.com", name="NID"),
            _yt_cookie(domain="example.com", name="OTHER"),  # 应被过滤
        ])
        with self._patch_env(browsers="chrome,edge"), \
             patch.object(config, "PROJECT_ROOT", Path(self.tmp.name)), \
             patch("yt_dlp.cookies.extract_cookies_from_browser",
                   return_value=jar) as mock_extract:
            result = downloader.refresh_youtube_cookies(out, quiet=True)
        self.assertEqual(result, out)
        self.assertTrue(out.exists())
        content = out.read_text(encoding="utf-8")
        self.assertIn("SID", content)
        self.assertIn("NID", content)
        self.assertNotIn("OTHER", content)
        # 第一个浏览器成功即返回，不再尝试第二个
        self.assertEqual(mock_extract.call_count, 1)
        self.assertEqual(mock_extract.call_args.args[0], "chrome")

    def test_relative_path_resolved_under_project_root(self):
        jar = MagicMock()
        jar.__iter__.return_value = iter([_yt_cookie()])
        with patch.object(config, "YOUTUBE_COOKIE_FILE", "config/cookies.txt"), \
             patch.object(config, "YOUTUBE_COOKIES_FROM_BROWSER", "chrome"), \
             patch.object(config, "PROJECT_ROOT", Path(self.tmp.name)), \
             patch("yt_dlp.cookies.extract_cookies_from_browser", return_value=jar):
            result = downloader.refresh_youtube_cookies(quiet=True)
        self.assertEqual(result, Path(self.tmp.name) / "config" / "cookies.txt")

    def test_no_youtube_cookies_tries_next_browser(self):
        empty_jar = MagicMock()
        empty_jar.__iter__.return_value = iter([_yt_cookie(domain="example.com")])
        good_jar = MagicMock()
        good_jar.__iter__.return_value = iter([_yt_cookie()])
        out = Path(self.tmp.name) / "cookies.txt"
        with patch.object(config, "YOUTUBE_COOKIES_FROM_BROWSER", "chrome,firefox"), \
             patch("yt_dlp.cookies.extract_cookies_from_browser",
                   side_effect=[empty_jar, good_jar]) as mock_extract:
            result = downloader.refresh_youtube_cookies(out, quiet=True)
        self.assertEqual(result, out)
        self.assertEqual(mock_extract.call_count, 2)
        self.assertEqual(mock_extract.call_args_list[1].args[0], "firefox")

    def test_browser_error_falls_through_to_none(self):
        out = Path(self.tmp.name) / "cookies.txt"
        with patch.object(config, "YOUTUBE_COOKIES_FROM_BROWSER", "chrome,firefox"), \
             patch("yt_dlp.cookies.extract_cookies_from_browser",
                   side_effect=RuntimeError("could not copy chrome cookie database")):
            result = downloader.refresh_youtube_cookies(out, quiet=True)
        self.assertIsNone(result)


class DownloadVideoTests(unittest.TestCase):
    """download_video：元数据 → 筛选 → 下载/复用 → 探测 → VideoInfo。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dl_dir = Path(self.tmp.name) / "downloads"
        self.video_path = self.dl_dir / "abc123.mp4"

    def _info(self, **overrides):
        info = {
            "id": "abc123",
            "title": "Test Video",
            "description": "desc",
            "channel": "Chan",
            "formats": [{"height": 1080, "width": 1920, "vcodec": "avc1"}],
            "duration": 300,
        }
        info.update(overrides)
        return info

    def _patch_environment(self, **config_overrides):
        patchers = [
            patch.object(config, "DOWNLOAD_DIR", str(self.dl_dir)),
            patch.object(config, "CONTENT_FILTER_ENABLED", False),
            patch.object(config, "YOUTUBE_SKIP_VERTICAL_VIDEOS", False),
            patch.object(config, "YOUTUBE_SKIP_LONG_VIDEO_MINUTES", 0),
        ]
        for name, value in config_overrides.items():
            patchers.append(patch.object(config, name, value))
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    def _mock_stages(self, *, metadata=None, local_path=None, download_result=None):
        if metadata is None:
            metadata = self._info()
        if download_result is not None:
            # download_video 会检查文件存在性 + stat 大小
            download_result = Path(download_result)
            download_result.parent.mkdir(parents=True, exist_ok=True)
            download_result.write_bytes(b"fake video")
        if local_path is not None:
            local_path = Path(local_path)
            local_path.parent.mkdir(parents=True, exist_ok=True)
            local_path.write_bytes(b"fake video")
            if download_result is None:
                download_result = local_path
        mocks = {}
        with patch.object(downloader, "_extract_metadata", return_value=metadata) as m1, \
             patch.object(downloader, "_find_downloaded_video",
                          return_value=Path(local_path) if local_path else None) as m2, \
             patch.object(downloader, "_download_with_progress",
                          return_value=str(download_result) if download_result else "") as m3, \
             patch.object(downloader, "_download_thumbnail", return_value="") as m4, \
             patch.object(downloader, "_probe_video_resolution",
                          return_value=(1920, 1080)) as m5, \
             patch.object(downloader, "_probe_video_duration", return_value=100.0) as m6:
            mocks.update(m1=m1, m2=m2, m3=m3, m4=m4, m5=m5, m6=m6)
            return downloader.download_video("https://youtube.com/watch?v=abc123"), mocks

    def test_full_download_path(self):
        self._patch_environment()
        self.dl_dir.mkdir(parents=True)
        result, mocks = self._mock_stages(download_result=self.video_path)
        self.assertEqual(result.title, "Test Video")
        self.assertEqual(result.description, "desc")
        self.assertEqual(result.video_id, "abc123")
        self.assertEqual(result.width, 1920)
        self.assertEqual(result.height, 1080)
        self.assertEqual(result.duration, 100.0)
        self.assertEqual(result.original_url, "https://youtube.com/watch?v=abc123")
        mocks["m3"].assert_called_once()

    def test_reuses_local_file(self):
        self._patch_environment()
        self.dl_dir.mkdir(parents=True)
        result, mocks = self._mock_stages(local_path=self.video_path)
        self.assertEqual(result.file_path, str(self.video_path))
        mocks["m3"].assert_not_called()  # 本地已有则跳过下载

    def test_missing_video_id_raises(self):
        self._patch_environment()
        with patch.object(downloader, "_extract_metadata", return_value={"title": "x"}):
            with self.assertRaises(RuntimeError) as ctx:
                downloader.download_video("https://youtube.com/watch?v=abc123")
        self.assertIn("无法获取视频 ID", str(ctx.exception))

    def test_content_filter_rejects(self):
        self._patch_environment(CONTENT_FILTER_ENABLED=True)
        with patch.object(downloader, "_extract_metadata", return_value=self._info()), \
             patch("yt2bili.translation.translator.classify_content",
                   return_value=False):
            with self.assertRaises(RuntimeError) as ctx:
                downloader.download_video("https://youtube.com/watch?v=abc123")
        self.assertIn("内容筛选已跳过", str(ctx.exception))

    def test_vertical_video_rejected(self):
        self._patch_environment(YOUTUBE_SKIP_VERTICAL_VIDEOS=True)
        info = self._info(formats=[{"height": 640, "width": 360, "vcodec": "avc1"}])
        with patch.object(downloader, "_extract_metadata", return_value=info):
            with self.assertRaises(RuntimeError) as ctx:
                downloader.download_video("https://youtube.com/watch?v=abc123")
        self.assertIn("竖屏视频", str(ctx.exception))

    def test_overlong_video_rejected(self):
        self._patch_environment(YOUTUBE_SKIP_LONG_VIDEO_MINUTES=5)
        info = self._info(duration=600)  # 10 分钟 ≥ 5 分钟阈值
        with patch.object(downloader, "_extract_metadata", return_value=info):
            with self.assertRaises(RuntimeError) as ctx:
                downloader.download_video("https://youtube.com/watch?v=abc123")
        self.assertIn("超过最大时长限制", str(ctx.exception))

    def test_download_failure_cleans_partial_files(self):
        self._patch_environment()
        self.dl_dir.mkdir(parents=True)
        with patch.object(downloader, "_extract_metadata", return_value=self._info()), \
             patch.object(downloader, "_find_downloaded_video", return_value=None), \
             patch.object(downloader, "_download_with_progress",
                          side_effect=RuntimeError("download failed")) as m3, \
             patch.object(downloader, "_clean_partial_files") as m_clean:
            with self.assertRaises(RuntimeError):
                downloader.download_video("https://youtube.com/watch?v=abc123")
        m_clean.assert_called_once()
        self.assertEqual(m_clean.call_args.args[1], "abc123")

    def test_file_missing_after_download_raises(self):
        self._patch_environment()
        self.dl_dir.mkdir(parents=True)
        with patch.object(downloader, "_extract_metadata", return_value=self._info()), \
             patch.object(downloader, "_find_downloaded_video", return_value=None), \
             patch.object(downloader, "_download_with_progress",
                          return_value=str(self.dl_dir / "missing.mp4")):
            with self.assertRaises(RuntimeError) as ctx:
                downloader.download_video("https://youtube.com/watch?v=abc123")
        self.assertIn("找不到视频文件", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
