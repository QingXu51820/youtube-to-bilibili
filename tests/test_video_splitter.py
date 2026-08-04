"""自测：视频分割（ffprobe 探测、ffmpeg 分段、失败兜底）。"""

import sys
import tempfile
import unittest

# 被测模块会打印 ⚠️/❌/✅ — 管道下 GBK stdout 无法编码，需与 main.py 相同处理
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from yt2bili import config
from yt2bili.media import video_splitter as vs


FAKE_FFMPEG = "C:/fake/bin/ffmpeg.exe"  # 假路径 — subprocess 被 mock，不会真的执行


class ProbeDurationTests(unittest.TestCase):
    def setUp(self):
        # find_tool 是环境探测，测试必须绕过它（否则依赖机器上真实 ffmpeg）
        patcher = patch.object(config, "find_tool", return_value=FAKE_FFMPEG)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_valid_output(self):
        proc = SimpleNamespace(returncode=0, stdout="12.345\n")
        with patch.object(vs.subprocess, "run", return_value=proc):
            self.assertAlmostEqual(vs._probe_duration(Path("x.mp4")), 12.345)

    def test_nonzero_returncode(self):
        proc = SimpleNamespace(returncode=1, stdout="")
        with patch.object(vs.subprocess, "run", return_value=proc):
            self.assertEqual(vs._probe_duration(Path("x.mp4")), 0.0)

    def test_file_not_found(self):
        with patch.object(vs.subprocess, "run", side_effect=FileNotFoundError):
            self.assertEqual(vs._probe_duration(Path("x.mp4")), 0.0)

    def test_timeout(self):
        with patch.object(vs.subprocess, "run", side_effect=vs.subprocess.TimeoutExpired("ffprobe", 30)):
            self.assertEqual(vs._probe_duration(Path("x.mp4")), 0.0)

    def test_garbage_stdout(self):
        proc = SimpleNamespace(returncode=0, stdout="not a number\n")
        with patch.object(vs.subprocess, "run", return_value=proc):
            self.assertEqual(vs._probe_duration(Path("x.mp4")), 0.0)

    def test_ffprobe_not_found_returns_zero(self):
        """find_tool 找不到 ffprobe 时提前返回 0.0，不触发 subprocess。"""
        with patch.object(config, "find_tool", return_value=None):
            self.assertEqual(vs._probe_duration(Path("x.mp4")), 0.0)


class SplitVideoTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # 同上：绕过 find_tool 的真实环境探测
        patcher = patch.object(config, "find_tool", return_value=FAKE_FFMPEG)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _source(self, name="video.mp4"):
        p = Path(self.tmp.name) / name
        p.write_bytes(b"fake video bytes")
        return p

    def test_missing_file_returns_empty(self):
        self.assertEqual(vs.split_video(str(Path(self.tmp.name) / "nope.mp4")), [])

    def test_zero_segment_duration_returns_source(self):
        src = self._source()
        result = vs.split_video(str(src), segment_duration_seconds=0)
        self.assertEqual(result, [str(src.resolve())])

    def test_ffmpeg_success_returns_sorted_segments(self):
        src = self._source()
        with patch.object(config, "DOWNLOAD_DIR", self.tmp.name):
            out_dir = Path(self.tmp.name) / "splits" / "video"
            out_dir.mkdir(parents=True)
            (out_dir / "video_P000.mp4").write_bytes(b"seg0")
            (out_dir / "video_P001.mp4").write_bytes(b"seg1")
            proc = SimpleNamespace(returncode=0, stderr="")
            with patch.object(vs.subprocess, "run", return_value=proc), \
                 patch.object(vs, "_probe_duration", return_value=5.0):
                result = vs.split_video(str(src), segment_duration_seconds=3600)
        self.assertEqual(len(result), 2)
        self.assertTrue(all(Path(r).exists() for r in result))
        self.assertEqual(result[0], str((out_dir / "video_P000.mp4").resolve()))

    def test_ffmpeg_failure_returns_empty(self):
        src = self._source()
        with patch.object(config, "DOWNLOAD_DIR", self.tmp.name):
            proc = SimpleNamespace(returncode=1, stderr="error output")
            with patch.object(vs.subprocess, "run", return_value=proc):
                result = vs.split_video(str(src), segment_duration_seconds=3600)
        self.assertEqual(result, [])

    def test_ffmpeg_missing_returns_empty(self):
        src = self._source()
        with patch.object(config, "DOWNLOAD_DIR", self.tmp.name):
            with patch.object(vs.subprocess, "run", side_effect=FileNotFoundError):
                result = vs.split_video(str(src), segment_duration_seconds=3600)
        self.assertEqual(result, [])

    def test_ffmpeg_not_found_returns_empty(self):
        """find_tool 找不到 ffmpeg 时直接返回空列表（不执行命令）。"""
        src = self._source()
        with patch.object(config, "DOWNLOAD_DIR", self.tmp.name):
            with patch.object(config, "find_tool", return_value=None):
                result = vs.split_video(str(src), segment_duration_seconds=3600)
        self.assertEqual(result, [])

    def test_no_segments_generated_returns_empty(self):
        src = self._source()
        with patch.object(config, "DOWNLOAD_DIR", self.tmp.name):
            proc = SimpleNamespace(returncode=0, stderr="")
            with patch.object(vs.subprocess, "run", return_value=proc):
                result = vs.split_video(str(src), segment_duration_seconds=3600)
        self.assertEqual(result, [])


if __name__ == "__main__":
    unittest.main()
