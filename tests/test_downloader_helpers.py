"""自测：yt-dlp 下载器辅助逻辑（残留清理、异常识别）。"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from yt2bili.youtube.downloader import (
    SlowDownloadError,
    _clean_partial_files,
    _is_range_not_satisfiable,
    _is_slow_download_exception,
    _slow_download_message,
)


class CleanPartialFilesTests(unittest.TestCase):
    def _make_dir_with_files(self, tmp, names):
        d = Path(tmp) / "dl"
        d.mkdir()
        for name in names:
            (d / name).write_bytes(b"x")
        return d

    def test_removes_all_intermediate_and_final_files(self):
        """回归：nopart 中断会把截断数据直接留在最终文件名里，失败路径必须清掉。"""
        with tempfile.TemporaryDirectory() as tmp:
            d = self._make_dir_with_files(tmp, [
                "abc123.part", "abc123.ytdl", "abc123.f401.mp4",
                "abc123.webp", "abc123.mp4", "abc123.mkv", "abc123.webm",
                "keep_me.mp4",
            ])
            _clean_partial_files(d, "abc123")
            remaining = sorted(p.name for p in d.iterdir())
        self.assertEqual(remaining, ["keep_me.mp4"])

    def test_empty_video_id_removes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = self._make_dir_with_files(tmp, ["abc123.part", "abc123.mp4"])
            _clean_partial_files(d, "")
            remaining = sorted(p.name for p in d.iterdir())
        self.assertEqual(remaining, ["abc123.mp4", "abc123.part"])

    def test_missing_dir_is_noop(self):
        _clean_partial_files(Path("does/not/exist"), "abc123")  # 不抛异常即可


class ExceptionDetectionTests(unittest.TestCase):
    def test_slow_download_marker(self):
        err = RuntimeError(f"{SlowDownloadError.__name__}: 下载速度低于阈值")
        self.assertTrue(_is_slow_download_exception(err))
        self.assertFalse(_is_slow_download_exception(RuntimeError("普通错误")))

    def test_slow_download_message_strips_marker(self):
        err = SlowDownloadError("__YT2BILI_SLOW_DOWNLOAD__速度太慢")
        self.assertEqual(_slow_download_message(err), "速度太慢")

    def test_range_not_satisfiable(self):
        self.assertTrue(_is_range_not_satisfiable(RuntimeError("HTTP 416")))
        self.assertTrue(_is_range_not_satisfiable(RuntimeError("range not satisfiable")))
        self.assertFalse(_is_range_not_satisfiable(RuntimeError("HTTP 404")))


if __name__ == "__main__":
    unittest.main()
