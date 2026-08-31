"""自测：多账号 profile 解析（防御性）与 config 基础行为。"""

import os
import sys
import tempfile
import unittest

# profile.py 失败路径打印 ⚠️ — 管道下 GBK stdout 无法编码，需与 main.py 相同处理
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from pathlib import Path
from unittest.mock import patch

from yt2bili import config
from yt2bili import profile as profile_mod
from yt2bili.profile import (
    BiliCredentials,
    Profile,
    ProfileSettings,
    YouTubeChannel,
    YouTubeSettings,
    _dict_to_profile,
    _profile_to_dict,
    get_state_file_path,
    get_cache_file_path,
)


class DictToProfileTests(unittest.TestCase):
    def test_valid_profile(self):
        p = _dict_to_profile("snap", {
            "bilibili": {"sessdata": "abc", "bili_jct": "jct", "buvid3": "b3"},
            "youtube": {
                "channels": [{"channel_id": "UC1", "channel_title": "Chan A"}],
                "monitor_source": "rss",
            },
            "settings": {"default_tid": 172, "default_tags": "a,b",
                         "content_filter_enabled": True},
        })
        self.assertEqual(p.name, "snap")
        self.assertEqual(p.bilibili.sessdata, "abc")
        self.assertEqual(p.youtube.channels[0].channel_id, "UC1")
        self.assertEqual(p.settings.default_tid, 172)
        self.assertTrue(p.settings.content_filter_enabled)

    def test_channels_not_a_list(self):
        p = _dict_to_profile("x", {"youtube": {"channels": "notalist"}})
        self.assertEqual(p.youtube.channels, [])

    def test_channel_without_id_skipped(self):
        p = _dict_to_profile("x", {"youtube": {"channels": [
            {"channel_title": "no id"},
            {"channel_id": "UC2", "channel_title": "ok"},
        ]}})
        self.assertEqual([c.channel_id for c in p.youtube.channels], ["UC2"])

    def test_channel_id_coerced_to_str(self):
        p = _dict_to_profile("x", {"youtube": {"channels": [
            {"channel_id": 123456},
        ]}})
        self.assertEqual(p.youtube.channels[0].channel_id, "123456")

    def test_channel_collection_parsed(self):
        p = _dict_to_profile("x", {"youtube": {"channels": [
            {"channel_id": "UC1", "channel_title": "A", "collection": "Bynx"},
            {"channel_id": "UC2", "channel_title": "B"},
        ]}})
        self.assertEqual(p.youtube.channels[0].collection, "Bynx")
        self.assertEqual(p.youtube.channels[1].collection, "")

    def test_tid_string_and_invalid(self):
        p1 = _dict_to_profile("x", {"settings": {"default_tid": "172"}})
        self.assertEqual(p1.settings.default_tid, 172)
        p2 = _dict_to_profile("x", {"settings": {"default_tid": "abc"}})
        self.assertIsNone(p2.settings.default_tid)

    def test_content_filter_string_bools(self):
        for val, expected in [("true", True), ("1", True), ("yes", True),
                              ("on", True), ("false", False), ("0", False), ("no", False)]:
            p = _dict_to_profile("x", {"settings": {"content_filter_enabled": val}})
            self.assertEqual(p.settings.content_filter_enabled, expected, val)

    def test_monitor_source_coerced(self):
        p = _dict_to_profile("x", {"youtube": {"monitor_source": 123}})
        self.assertEqual(p.youtube.monitor_source, "123")

    def test_settings_none_fields(self):
        p = _dict_to_profile("x", {})
        self.assertIsNone(p.settings.default_tid)
        self.assertIsNone(p.settings.default_tags)


class ProfileDictRoundtripTests(unittest.TestCase):
    def test_roundtrip_preserves_data(self):
        p = Profile(
            name="snap",
            bilibili=BiliCredentials(sessdata="s", bili_jct="j"),
            youtube=YouTubeSettings(
                channels=[YouTubeChannel("UC1", "Chan")],
                monitor_source="rss",
            ),
            settings=ProfileSettings(default_tid=172, content_filter_enabled=False),
        )
        d = _profile_to_dict(p)
        p2 = _dict_to_profile("snap", d)
        self.assertEqual(p2.bilibili.sessdata, "s")
        self.assertEqual(p2.youtube.channels[0].channel_title, "Chan")
        self.assertEqual(p2.settings.default_tid, 172)
        self.assertFalse(p2.settings.content_filter_enabled)
        # None 字段不写入 JSON
        self.assertNotIn("default_tags", d["settings"])

    def test_roundtrip_preserves_collection(self):
        p = Profile(
            name="s",
            youtube=YouTubeSettings(
                channels=[YouTubeChannel("UC1", "Chan", collection="KMB")]
            ),
        )
        d = _profile_to_dict(p)
        self.assertEqual(d["youtube"]["channels"][0]["collection"], "KMB")
        p2 = _dict_to_profile("s", d)
        self.assertEqual(p2.youtube.channels[0].collection, "KMB")


class SaveLoadProfilesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.profiles_path = Path(self.tmp.name) / "profiles.json"
        patcher = patch.object(profile_mod, "PROFILES_FILE", self.profiles_path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_roundtrip(self):
        profiles = {
            "a": Profile(name="a", bilibili=BiliCredentials(sessdata="sa")),
            "b": Profile(name="b", youtube=YouTubeSettings(
                channels=[YouTubeChannel("UCx", "X")])),
        }
        profile_mod.save_profiles(profiles)
        loaded = profile_mod.load_profiles()
        self.assertEqual(set(loaded), {"a", "b"})
        self.assertEqual(loaded["a"].bilibili.sessdata, "sa")
        self.assertEqual(loaded["b"].youtube.channels[0].channel_id, "UCx")

    def test_save_is_atomic_no_tmp_left(self):
        profile_mod.save_profiles({"a": Profile(name="a")})
        leftovers = list(Path(self.tmp.name).glob("*.tmp"))
        self.assertEqual(leftovers, [])

    def test_load_missing_returns_empty(self):
        self.assertEqual(profile_mod.load_profiles(), {})

    def test_load_corrupt_returns_empty(self):
        self.profiles_path.write_text("{broken", encoding="utf-8")
        self.assertEqual(profile_mod.load_profiles(), {})

    def test_malformed_entry_skipped_others_kept(self):
        self.profiles_path.write_text(
            '{"profiles": {"good": {"bilibili": {"sessdata": "s"}}, "bad": "notadict"}}',
            encoding="utf-8",
        )
        loaded = profile_mod.load_profiles()
        self.assertEqual(set(loaded), {"good"})


class ProfileRuntimeTests(unittest.TestCase):
    """profile.py 运行时函数：活动 profile 状态、单条读写、解析。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.profiles_path = Path(self.tmp.name) / "profiles.json"
        patcher = patch.object(profile_mod, "PROFILES_FILE", self.profiles_path)
        patcher.start()
        self.addCleanup(patcher.stop)
        # 不污染全局活动 profile 状态
        self.addCleanup(profile_mod.set_active_profile, "default")

    def test_active_profile_roundtrip(self):
        profile_mod.set_active_profile("snap")
        self.assertEqual(profile_mod.get_active_profile_name(), "snap")
        profile_mod.set_active_profile("other")
        self.assertEqual(profile_mod.get_active_profile_name(), "other")

    def test_save_profile_then_get_and_exists(self):
        profile_mod.save_profile(Profile(name="snap", bilibili=BiliCredentials(sessdata="s")))
        self.assertEqual(profile_mod.get_profile("snap").bilibili.sessdata, "s")
        self.assertTrue(profile_mod.profile_exists("snap"))
        self.assertFalse(profile_mod.profile_exists("nope"))
        self.assertIsNone(profile_mod.get_profile("nope"))

    def test_save_profile_upserts_same_name(self):
        profile_mod.save_profile(Profile(name="snap", bilibili=BiliCredentials(sessdata="s1")))
        profile_mod.save_profile(Profile(name="snap", bilibili=BiliCredentials(sessdata="s2")))
        self.assertEqual(profile_mod.get_profile("snap").bilibili.sessdata, "s2")

    def test_save_profile_keeps_others(self):
        profile_mod.save_profile(Profile(name="a"))
        profile_mod.save_profile(Profile(name="b"))
        self.assertEqual(set(profile_mod.load_profiles()), {"a", "b"})

    def test_is_multi_profile_depends_on_file(self):
        self.assertFalse(profile_mod.is_multi_profile())
        self.profiles_path.write_text('{"profiles": {}}', encoding="utf-8")
        self.assertTrue(profile_mod.is_multi_profile())

    def test_resolve_profile_by_name(self):
        profile_mod.save_profile(Profile(name="snap", bilibili=BiliCredentials(sessdata="s")))
        self.assertEqual(profile_mod.resolve_profile("snap").bilibili.sessdata, "s")

    def test_resolve_default_from_env_backward_compat(self):
        """无 profiles.json 时，default 从 .env 凭证构造（向后兼容）。"""
        with patch.object(config, "BILI_SESSDATA", "envs"), \
             patch.object(config, "BILI_BILI_JCT", "envj"), \
             patch.object(config, "BILI_BUVID3", "envb"), \
             patch.object(config, "BILI_LOGIN_TIME", "t0"):
            p = profile_mod.resolve_profile("default")
        self.assertEqual(p.name, "default")
        self.assertEqual(p.bilibili.sessdata, "envs")
        self.assertEqual(p.bilibili.bili_jct, "envj")
        self.assertEqual(p.bilibili.buvid3, "envb")

    def test_resolve_nonexistent_non_default_returns_none(self):
        self.assertIsNone(profile_mod.resolve_profile("ghost"))


class StatePathTests(unittest.TestCase):
    def test_default_state_path(self):
        p = Profile(name="snap")
        self.assertEqual(
            get_state_file_path(p),
            config.PROJECT_ROOT / "state" / "snap" / "processed_videos.json",
        )

    def test_custom_state_path(self):
        p = Profile(name="snap", youtube=YouTubeSettings(monitor_state="state/custom.json"))
        self.assertEqual(get_state_file_path(p), config.PROJECT_ROOT / "state" / "custom.json")

    def test_default_cache_path(self):
        p = Profile(name="snap")
        self.assertEqual(
            get_cache_file_path(p),
            config.PROJECT_ROOT / "config" / "snap_subscriptions_cache.json",
        )


class ConfigTests(unittest.TestCase):
    def test_project_root_contains_package(self):
        self.assertTrue((config.PROJECT_ROOT / "yt2bili").is_dir())

    def test_get_int_invalid_falls_back(self):
        with patch.dict(os.environ, {"YT2BILI_TEST_INT": "abc"}):
            self.assertEqual(config._get_int("YT2BILI_TEST_INT", 42), 42)

    def test_get_int_valid(self):
        with patch.dict(os.environ, {"YT2BILI_TEST_INT": "7"}):
            self.assertEqual(config._get_int("YT2BILI_TEST_INT", 42), 7)

    def test_get_int_empty_falls_back(self):
        with patch.dict(os.environ, {"YT2BILI_TEST_INT": ""}):
            self.assertEqual(config._get_int("YT2BILI_TEST_INT", 42), 42)

    def test_validate_reports_missing_credentials(self):
        with patch.object(config, "BILI_SESSDATA", ""), \
             patch.object(config, "BILI_BILI_JCT", ""), \
             patch.object(config, "TRANSLATE_PROVIDER", "deepseek"), \
             patch.object(config, "DEEPSEEK_API_KEY", ""), \
             patch.object(config, "DEEPSEEK_THINKING", "enabled"), \
             patch.object(config, "COVER_WIDTH", 1920), \
             patch.object(config, "COVER_HEIGHT", 1080), \
             patch.object(config, "COVER_FIT", "crop"), \
             patch.object(config, "DOWNLOAD_DIR", str(Path(tempfile.gettempdir()) / "yt2bili-test-dl")), \
             patch.object(config, "RUNS_DIR", str(Path(tempfile.gettempdir()) / "yt2bili-test-runs")), \
             patch.object(config, "SUBTITLE_DIR", str(Path(tempfile.gettempdir()) / "yt2bili-test-sub")), \
             patch.object(config, "SUBTITLE_TRANSLATE_BATCH_SIZE", 30), \
             patch.object(config, "SUBTITLE_WAIT_CID_INTERVAL", 10):
            issues = config.validate()
        joined = "\n".join(issues)
        self.assertIn("SESSDATA", joined)
        self.assertIn("BILI_JCT", joined)
        self.assertIn("DEEPSEEK_API_KEY", joined)

    def test_validate_clean(self):
        with patch.object(config, "BILI_SESSDATA", "real"), \
             patch.object(config, "BILI_BILI_JCT", "real"), \
             patch.object(config, "TRANSLATE_PROVIDER", "deepseek"), \
             patch.object(config, "DEEPSEEK_API_KEY", "k"), \
             patch.object(config, "DEEPSEEK_THINKING", "enabled"), \
             patch.object(config, "COVER_WIDTH", 1920), \
             patch.object(config, "COVER_HEIGHT", 1080), \
             patch.object(config, "COVER_FIT", "crop"), \
             patch.object(config, "DOWNLOAD_DIR", str(Path(tempfile.gettempdir()) / "yt2bili-test-dl2")), \
             patch.object(config, "RUNS_DIR", str(Path(tempfile.gettempdir()) / "yt2bili-test-runs2")), \
             patch.object(config, "SUBTITLE_DIR", str(Path(tempfile.gettempdir()) / "yt2bili-test-sub2")), \
             patch.object(config, "SUBTITLE_TRANSLATE_BATCH_SIZE", 30), \
             patch.object(config, "SUBTITLE_WAIT_CID_INTERVAL", 10):
            issues = config.validate()
        self.assertEqual(issues, [])


if __name__ == "__main__":
    unittest.main()
