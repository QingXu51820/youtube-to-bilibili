"""自测：config 工具函数（find_tool 环境探测、apply_profile_overrides 覆盖逻辑）。"""

import os
import sys
import unittest
from unittest.mock import patch

# 被测模块在失败路径打印 ⚠️ — 管道下 GBK stdout 无法编码，需与 main.py 相同处理
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from yt2bili import config
from yt2bili.profile import Profile, ProfileSettings


class FindToolTests(unittest.TestCase):
    """find_tool：shutil.which 优先，Windows 注册表 PATH 兜底。"""

    def test_which_hit_returns_directly(self):
        with patch("shutil.which", return_value="/usr/bin/ffmpeg") as mock_which:
            self.assertEqual(config.find_tool("ffmpeg"), "/usr/bin/ffmpeg")
        mock_which.assert_called_once_with("ffmpeg")

    def test_which_miss_non_windows_returns_none(self):
        with patch("shutil.which", return_value=None), \
             patch("os.name", "posix"):
            self.assertIsNone(config.find_tool("ffmpeg"))

    def test_which_miss_windows_no_registry_hit_returns_none(self):
        with patch("shutil.which", return_value=None), \
             patch("winreg.OpenKey", side_effect=OSError), \
             patch("os.path.isfile", return_value=True):
            self.assertIsNone(config.find_tool("ffmpeg"))

    @unittest.skipUnless(os.name == "nt", "Windows-only registry fallback")
    def test_registry_hit_returns_normpath_candidate(self):
        with patch("shutil.which", return_value=None), \
             patch("winreg.OpenKey") as mock_open, \
             patch("winreg.QueryValueEx", return_value=("C:\\tools", 1)), \
             patch("winreg.CloseKey"), \
             patch.dict(os.environ, {"PATHEXT": ".EXE;.CMD;.BAT"}), \
             patch("os.path.isfile", return_value=True):
            result = config.find_tool("ffmpeg")
        self.assertEqual(result, os.path.normpath(r"C:\tools\ffmpeg.exe"))
        # Machine + User 两个 hive 都会被查询
        self.assertEqual(mock_open.call_count, 2)

    @unittest.skipUnless(os.name == "nt", "Windows-only registry fallback")
    def test_registry_expands_env_vars(self):
        with patch("shutil.which", return_value=None), \
             patch("winreg.QueryValueEx", return_value=("%SystemRoot%\\tools", 2)), \
             patch("winreg.CloseKey"), \
             patch.dict(os.environ, {"PATHEXT": ".EXE;.CMD;.BAT"}), \
             patch("os.path.isfile", return_value=True):
            result = config.find_tool("ffprobe")
        expected = os.path.normpath(
            os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "tools", "ffprobe.exe"))
        self.assertEqual(result, expected)

    @unittest.skipUnless(os.name == "nt", "Windows-only registry fallback")
    def test_pathext_second_extension_hit(self):
        """首个扩展名 (.EXE) 未命中时继续尝试 .CMD/.BAT。"""
        cmd_candidate = os.path.normpath(r"C:\tools\ffmpeg.cmd")
        with patch("shutil.which", return_value=None), \
             patch("winreg.QueryValueEx", return_value=("C:\\tools", 1)), \
             patch("winreg.CloseKey"), \
             patch.dict(os.environ, {"PATHEXT": ".EXE;.CMD;.BAT"}), \
             patch("os.path.isfile",
                   side_effect=lambda p: os.path.normpath(p) == cmd_candidate):
            result = config.find_tool("ffmpeg")
        self.assertEqual(result, cmd_candidate)

    @unittest.skipUnless(os.name == "nt", "Windows-only registry fallback")
    def test_upper_case_extension_hit(self):
        """PATHEXT 扩展名同时尝试小写/大写两种写法。"""
        upper_candidate = os.path.normpath(r"C:\tools\ffmpeg.EXE")
        with patch("shutil.which", return_value=None), \
             patch("winreg.QueryValueEx", return_value=("C:\\tools", 1)), \
             patch("winreg.CloseKey"), \
             patch.dict(os.environ, {"PATHEXT": ".EXE;.CMD;.BAT"}), \
             patch("os.path.isfile",
                   side_effect=lambda p: os.path.normpath(p) == upper_candidate):
            result = config.find_tool("ffmpeg")
        self.assertEqual(result, upper_candidate)


class ApplyProfileOverridesTests(unittest.TestCase):
    """apply_profile_overrides：仅覆盖 profile 显式设置的项。"""

    def _profile(self, **settings_kw):
        return Profile(name="snap", settings=ProfileSettings(**settings_kw))

    def test_default_single_account_noop(self):
        with patch("yt2bili.profile.is_multi_profile", return_value=False) as mock_multi, \
             patch("yt2bili.profile.resolve_profile") as mock_resolve:
            config.apply_profile_overrides("default")
        mock_multi.assert_called_once()
        mock_resolve.assert_not_called()

    def test_overrides_all_set_fields(self):
        p = self._profile(default_tid=172, default_tags="a,b",
                          content_filter_enabled=True, content_filter_keywords="snap")
        with patch("yt2bili.profile.is_multi_profile", return_value=True), \
             patch("yt2bili.profile.resolve_profile", return_value=p), \
             patch.object(config, "DEFAULT_TID", 0), \
             patch.object(config, "DEFAULT_TAGS", "orig"), \
             patch.object(config, "CONTENT_FILTER_ENABLED", False), \
             patch.object(config, "CONTENT_FILTER_KEYWORDS", "orig"):
            config.apply_profile_overrides("snap")
            self.assertEqual(config.DEFAULT_TID, 172)
            self.assertEqual(config.DEFAULT_TAGS, "a,b")
            self.assertTrue(config.CONTENT_FILTER_ENABLED)
            self.assertEqual(config.CONTENT_FILTER_KEYWORDS, "snap")

    def test_none_fields_left_untouched(self):
        p = self._profile(default_tid=172)  # 其余字段为 None
        with patch("yt2bili.profile.is_multi_profile", return_value=True), \
             patch("yt2bili.profile.resolve_profile", return_value=p), \
             patch.object(config, "DEFAULT_TID", 0), \
             patch.object(config, "DEFAULT_TAGS", "orig"), \
             patch.object(config, "CONTENT_FILTER_ENABLED", False), \
             patch.object(config, "CONTENT_FILTER_KEYWORDS", "orig"):
            config.apply_profile_overrides("snap")
            self.assertEqual(config.DEFAULT_TID, 172)
            self.assertEqual(config.DEFAULT_TAGS, "orig")
            self.assertFalse(config.CONTENT_FILTER_ENABLED)
            self.assertEqual(config.CONTENT_FILTER_KEYWORDS, "orig")

    def test_unknown_profile_noop(self):
        with patch("yt2bili.profile.is_multi_profile", return_value=True), \
             patch("yt2bili.profile.resolve_profile", return_value=None), \
             patch.object(config, "DEFAULT_TID", 0):
            config.apply_profile_overrides("nope")
            self.assertEqual(config.DEFAULT_TID, 0)


if __name__ == "__main__":
    unittest.main()
