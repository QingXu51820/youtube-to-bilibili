"""自测：Bilibili 字幕 API（分P 查询、CID 轮询、延迟上传队列）。"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import httpx

from yt2bili import config
from yt2bili import profile as profile_mod
from yt2bili.bilibili import subtitle as bsub
from yt2bili.profile import BiliCredentials, Profile, YouTubeChannel, YouTubeSettings


def _ok_response(pages=None):
    return httpx.Response(200, json={"code": 0, "data": {"pages": pages or []}})


class GetVideoPagesTests(unittest.TestCase):
    """get_video_pages：bvid/aid 参数、分P 列表解析、错误包装。"""

    def test_requires_bvid_or_aid(self):
        with self.assertRaises(ValueError):
            bsub.get_video_pages()

    def _client(self):
        # get_video_pages 用 `with _build_client() as client` — __enter__ 必须返回自身
        client = MagicMock()
        client.__enter__.return_value = client
        return client

    def test_bvid_request_and_pages_returned(self):
        client = self._client()
        client.get.return_value = _ok_response([{"cid": 1, "part": "P1"}])
        with patch.object(bsub, "_build_client", return_value=client):
            pages = bsub.get_video_pages(bvid="BV1x")
        self.assertEqual(pages[0]["cid"], 1)
        self.assertEqual(client.get.call_args.kwargs["params"]["bvid"], "BV1x")

    def test_aid_fallback(self):
        client = self._client()
        client.get.return_value = _ok_response([{"cid": 2}])
        with patch.object(bsub, "_build_client", return_value=client):
            bsub.get_video_pages(aid=123)
        self.assertEqual(client.get.call_args.kwargs["params"]["aid"], 123)

    def test_non_list_pages_raises(self):
        client = self._client()
        client.get.return_value = httpx.Response(
            200, json={"code": 0, "data": {"pages": "nope"}})
        with patch.object(bsub, "_build_client", return_value=client):
            with self.assertRaises(RuntimeError):
                bsub.get_video_pages(bvid="BV1x")

    def test_api_error_raises(self):
        client = self._client()
        client.get.return_value = httpx.Response(
            200, json={"code": -400, "message": "bad"})
        with patch.object(bsub, "_build_client", return_value=client):
            with self.assertRaises(RuntimeError) as ctx:
                bsub.get_video_pages(bvid="BV1x")
        self.assertIn("code=-400", str(ctx.exception))

    def test_network_error_wrapped(self):
        client = self._client()
        client.get.side_effect = httpx.ConnectError("down")
        with patch.object(bsub, "_build_client", return_value=client):
            with self.assertRaises(RuntimeError) as ctx:
                bsub.get_video_pages(bvid="BV1x")
        self.assertIn("网络错误", str(ctx.exception))


class WaitForCidTests(unittest.TestCase):
    """wait_for_cid：轮询直到 CID 可用或超时。"""

    def _run(self, *pages_results, timeout=0.2, interval=0.01):
        with patch.object(bsub, "get_video_pages", side_effect=pages_results), \
             patch.object(bsub.time, "sleep"):
            return bsub.wait_for_cid(bvid="BV1", timeout=timeout, interval=interval)

    def test_immediate_success(self):
        self.assertEqual(self._run([{"cid": 42}]), 42)

    def test_polls_until_cid_appears(self):
        self.assertEqual(self._run([], [{"cid": 7}]), 7)

    def test_transient_error_then_success(self):
        self.assertEqual(self._run(RuntimeError("boom"), [{"cid": 3}]), 3)

    def test_timeout_raises(self):
        with self.assertRaises(TimeoutError):
            self._run([], timeout=0.05, interval=0.01)

    def test_timeout_message_includes_last_error(self):
        def always_boom(*a, **k):
            raise RuntimeError("boom")
        with patch.object(bsub, "get_video_pages", side_effect=always_boom), \
             patch.object(bsub.time, "sleep"):
            with self.assertRaises(TimeoutError) as ctx:
                bsub.wait_for_cid(bvid="BV1", timeout=0.05, interval=0.01)
        self.assertIn("boom", str(ctx.exception))

    def test_zero_cid_not_considered_available(self):
        self.assertEqual(self._run([{"cid": 0}], [{"cid": 5}]), 5)


class UploadPendingSubtitlesTests(unittest.TestCase):
    """upload_pending_subtitles：延迟上传队列处理。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.pending = Path(self.tmp.name) / "pending_subtitles.json"
        self.srt = Path(self.tmp.name) / "abc123.zh-CN.srt"
        self.srt.write_text(
            "1\n00:00:01,000 --> 00:00:02,000\nhello\n", encoding="utf-8")

    def _write_pending(self, entries):
        self.pending.write_text(
            json.dumps(entries, ensure_ascii=False), encoding="utf-8")

    def _patch_pipeline(self, *, submit_side_effect=None, wait_side_effect=None):
        """mock 上传管线：pending 路径、恢复扫描、解析、提交、清理。"""
        patchers = [
            patch.object(bsub, "_pending_subtitles_path", return_value=self.pending),
            patch.object(bsub, "_migrate_legacy_pending_queue"),
            patch.object(bsub, "_regenerate_missing_subtitle", return_value=None),
            patch.object(bsub, "_recover_orphaned_subtitles", return_value=[]),
            patch("yt2bili.subtitles.parser.parse_subtitle",
                  return_value=[{"index": 1, "start": 1.0, "end": 2.0, "text": "hi"}]),
            patch("yt2bili.subtitles.bilibili_format.cues_to_bilibili_json",
                  return_value={"body": []}),
            patch.object(bsub, "submit_subtitle", return_value={"code": 0}),
            patch.object(bsub, "_cleanup_subtitle_files"),
            patch.object(bsub, "wait_for_cid", return_value=42),
            patch.object(bsub, "get_video_pages",
                         return_value=[{"cid": 42, "duration": 60}]),
        ]
        mocks = {}
        for patcher in patchers:
            m = patcher.start()
            self.addCleanup(patcher.stop)
            mocks[patcher.attribute] = m
        if submit_side_effect is not None:
            mocks["submit_subtitle"].side_effect = submit_side_effect
        if wait_side_effect is not None:
            mocks["wait_for_cid"].side_effect = wait_side_effect
        return mocks

    def test_no_pending_entries_returns_zero(self):
        self._patch_pipeline()
        self.assertEqual(bsub.upload_pending_subtitles(), 0)

    def test_success_uploads_and_cleans_queue(self):
        self._write_pending([{"bvid": "BV1", "aid": 1, "translated_path": str(self.srt)}])
        self._patch_pipeline()
        self.assertEqual(bsub.upload_pending_subtitles(), 1)
        self.assertFalse(self.pending.exists())  # 全部成功后队列文件被删除

    def test_missing_file_regenerated_and_uploaded(self):
        """文件缺失时重新生成成功 → 正常上传并清空队列。"""
        self._write_pending([
            {"bvid": "BV1", "aid": 1, "translated_path": str(Path(self.tmp.name) / "gone.srt")},
        ])
        mocks = self._patch_pipeline()

        def _regenerate(entry):
            self.srt.write_text(
                "1\n00:00:01,000 --> 00:00:02,000\nhello\n", encoding="utf-8")
            return str(self.srt)

        mocks["_regenerate_missing_subtitle"].side_effect = _regenerate
        self.assertEqual(bsub.upload_pending_subtitles(), 1)
        self.assertFalse(self.pending.exists())

    def test_missing_file_regeneration_failure_keeps_entry(self):
        """文件缺失且重新生成失败 → 条目保留在队列中，下轮重试。"""
        self._write_pending([
            {"bvid": "BV1", "aid": 1, "translated_path": str(Path(self.tmp.name) / "gone.srt")},
        ])
        self._patch_pipeline()  # _regenerate_missing_subtitle 默认返回 None
        self.assertEqual(bsub.upload_pending_subtitles(), 0)
        self.assertTrue(self.pending.exists())
        remaining = json.loads(self.pending.read_text(encoding="utf-8"))
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0]["bvid"], "BV1")
        self.assertEqual(remaining[0]["regen_failures"], 1)  # 记录失败次数

    def test_regen_failures_give_up_after_max(self):
        """回归：重新生成连续失败超过阈值 → 永久放弃，不再每轮白试。"""
        self._write_pending([
            {"bvid": "BV1", "aid": 1, "translated_path": str(Path(self.tmp.name) / "gone.srt")},
        ])
        self._patch_pipeline()  # _regenerate_missing_subtitle 默认返回 None
        with patch.object(bsub.config, "SUBTITLE_REGEN_MAX_FAILURES", 2):
            self.assertEqual(bsub.upload_pending_subtitles(), 0)  # 第 1 次失败
            self.assertTrue(self.pending.exists())
            self.assertEqual(bsub.upload_pending_subtitles(), 0)  # 第 2 次失败 → 放弃
        self.assertFalse(self.pending.exists())  # 队列清空，条目被永久移除

    def test_regen_success_resets_failure_counter(self):
        """重新生成成功 → 清零失败计数并正常上传。"""
        self._write_pending([
            {"bvid": "BV1", "aid": 1, "translated_path": str(Path(self.tmp.name) / "gone.srt"),
             "regen_failures": 2},
        ])
        mocks = self._patch_pipeline()

        def _regenerate(entry):
            self.srt.write_text(
                "1\n00:00:01,000 --> 00:00:02,000\nhello\n", encoding="utf-8")
            return str(self.srt)

        mocks["_regenerate_missing_subtitle"].side_effect = _regenerate
        self.assertEqual(bsub.upload_pending_subtitles(), 1)
        self.assertFalse(self.pending.exists())

    def test_permanent_error_not_retried(self):
        self._write_pending([{"bvid": "BV1", "aid": 1, "translated_path": str(self.srt)}])
        self._patch_pipeline(
            submit_side_effect=RuntimeError("79014 字幕时间点超过视频时间长度"))
        self.assertEqual(bsub.upload_pending_subtitles(), 0)
        self.assertFalse(self.pending.exists())  # 79014 永久错误，不保留

    def test_transient_error_kept_in_queue(self):
        self._write_pending([{"bvid": "BV1", "aid": 1, "translated_path": str(self.srt)}])
        self._patch_pipeline(submit_side_effect=RuntimeError("网络波动"))
        self.assertEqual(bsub.upload_pending_subtitles(), 0)
        self.assertTrue(self.pending.exists())
        remaining = json.loads(self.pending.read_text(encoding="utf-8"))
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0]["bvid"], "BV1")

    def test_cid_timeout_kept_in_queue(self):
        self._write_pending([{"bvid": "BV1", "aid": 1, "translated_path": str(self.srt)}])
        self._patch_pipeline(wait_side_effect=TimeoutError("超时"))
        self.assertEqual(bsub.upload_pending_subtitles(), 0)
        self.assertTrue(self.pending.exists())
        remaining = json.loads(self.pending.read_text(encoding="utf-8"))
        self.assertEqual(len(remaining), 1)

    def test_cached_cid_skips_wait(self):
        """恢复扫描时已缓存的 cid 直接使用，不调用 wait_for_cid。"""
        self._write_pending([
            {"bvid": "BV1", "aid": 1, "translated_path": str(self.srt), "cid": 99},
        ])
        self._patch_pipeline(wait_side_effect=AssertionError("不应调用"))
        self.assertEqual(bsub.upload_pending_subtitles(), 1)


class PendingSubtitlePathTests(unittest.TestCase):
    """_pending_subtitles_path：待上传队列按账号隔离。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_legacy_path_default_no_profiles_file(self):
        with patch.object(bsub, "_active_profile_name", return_value="default"), \
             patch.object(profile_mod, "is_multi_profile", return_value=False), \
             patch.object(config, "PROJECT_ROOT", Path(self.tmp.name)):
            path = bsub._pending_subtitles_path()
        self.assertEqual(path, Path(self.tmp.name) / "state" / "pending_subtitles.json")

    def test_legacy_path_default_multi_without_default_profile(self):
        """profiles.json 存在但没有 'default' 账号 → 仍是传统 .env 模式。"""
        with patch.object(bsub, "_active_profile_name", return_value="default"), \
             patch.object(profile_mod, "is_multi_profile", return_value=True), \
             patch.object(profile_mod, "profile_exists", return_value=False), \
             patch.object(config, "PROJECT_ROOT", Path(self.tmp.name)):
            path = bsub._pending_subtitles_path()
        self.assertEqual(path, Path(self.tmp.name) / "state" / "pending_subtitles.json")

    def test_named_profile_path(self):
        with patch.object(bsub, "_active_profile_name", return_value="snap"), \
             patch.object(config, "PROJECT_ROOT", Path(self.tmp.name)):
            path = bsub._pending_subtitles_path()
        self.assertEqual(path, Path(self.tmp.name) / "state" / "snap" / "pending_subtitles.json")

    def test_default_profile_in_file_uses_profile_dir(self):
        """profiles.json 里存在名为 'default' 的账号 → 用账号自己的队列。"""
        with patch.object(bsub, "_active_profile_name", return_value="default"), \
             patch.object(profile_mod, "is_multi_profile", return_value=True), \
             patch.object(profile_mod, "profile_exists", return_value=True), \
             patch.object(config, "PROJECT_ROOT", Path(self.tmp.name)):
            path = bsub._pending_subtitles_path()
        self.assertEqual(path, Path(self.tmp.name) / "state" / "default" / "pending_subtitles.json")


class SavePendingSubtitleProfileIsolationTests(unittest.TestCase):
    """save_pending_subtitle：写入当前账号自己的队列，不落公共队列。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_saves_to_active_profile_queue(self):
        with patch.object(bsub, "_active_profile_name", return_value="deadlock"), \
             patch.object(config, "PROJECT_ROOT", Path(self.tmp.name)):
            bsub.save_pending_subtitle("BV1", 1, "/tmp/a.zh-CN.srt")
        queue = Path(self.tmp.name) / "state" / "deadlock" / "pending_subtitles.json"
        self.assertTrue(queue.exists())
        entries = json.loads(queue.read_text(encoding="utf-8"))
        self.assertEqual([e["bvid"] for e in entries], ["BV1"])
        self.assertFalse(
            (Path(self.tmp.name) / "state" / "pending_subtitles.json").exists()
        )


class ActiveCredentialsTests(unittest.TestCase):
    """_active_credentials：账号凭据解析（profile 优先，.env 仅作传统模式兜底）。"""

    def test_env_fallback_legacy(self):
        with patch.object(bsub, "_active_profile_name", return_value="default"), \
             patch.object(profile_mod, "is_multi_profile", return_value=False), \
             patch.object(config, "BILI_SESSDATA", "s"), \
             patch.object(config, "BILI_BILI_JCT", "j"), \
             patch.object(config, "BILI_BUVID3", "b"):
            self.assertEqual(bsub._active_credentials(), ("s", "j", "b"))

    def test_profile_creds_used(self):
        prof = Profile(name="snap", bilibili=BiliCredentials(sessdata="S", bili_jct="J", buvid3="B"))
        with patch.object(bsub, "_active_profile_name", return_value="snap"), \
             patch.object(profile_mod, "resolve_profile", return_value=prof):
            self.assertEqual(bsub._active_credentials(), ("S", "J", "B"))

    def test_missing_profile_creds_raise(self):
        """账号缺凭据时报错，而不是回退 .env（避免上传到错误的账号）。"""
        prof = Profile(name="snap", bilibili=BiliCredentials(sessdata="", bili_jct=""))
        with patch.object(bsub, "_active_profile_name", return_value="snap"), \
             patch.object(profile_mod, "resolve_profile", return_value=prof):
            with self.assertRaises(RuntimeError) as ctx:
                bsub._active_credentials()
        self.assertIn("snap", str(ctx.exception))

    def test_build_client_uses_profile_cookies(self):
        with patch.object(bsub, "_active_credentials", return_value=("S", "J", "B")):
            client = bsub._build_client()
            try:
                self.assertEqual(client.cookies.get("SESSDATA"), "S")
                self.assertEqual(client.cookies.get("buvid3"), "B")
            finally:
                client.close()


class RecoverOrphanedScopedTests(unittest.TestCase):
    """_recover_orphaned_subtitles：只扫描当前账号频道的字幕文件。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.sub_dir = Path(self.tmp.name) / "subtitles"
        self.sub_dir.mkdir(parents=True)
        (self.sub_dir / "vidA.zh-CN.srt").write_text("x", encoding="utf-8")
        (self.sub_dir / "vidB.zh-CN.srt").write_text("x", encoding="utf-8")
        state_dir = Path(self.tmp.name) / "state"
        state_dir.mkdir(parents=True)
        upload_log = [
            {"video_id": "vidA", "channel_title": "InScopeChannel", "bvid": "BV-A", "aid": 1},
            {"video_id": "vidB", "channel_title": "OtherChannel", "bvid": "BV-B", "aid": 2},
        ]
        (state_dir / "upload_log.json").write_text(json.dumps(upload_log), encoding="utf-8")

    def test_out_of_scope_channel_skipped_without_network(self):
        client = MagicMock()
        client.get.return_value = _ok_response([{"cid": 1}])
        with patch.object(bsub, "_build_client", return_value=client), \
             patch.object(config, "SUBTITLE_DIR", str(self.sub_dir)), \
             patch.object(config, "PROJECT_ROOT", Path(self.tmp.name)):
            recovered = bsub._recover_orphaned_subtitles(set(), {"InScopeChannel"})
        self.assertEqual([e["bvid"] for e in recovered], ["BV-A"])
        self.assertEqual(client.get.call_count, 1)  # 只查询作用域内的 bvid

    def test_legacy_mode_scans_all_channels(self):
        client = MagicMock()
        client.get.return_value = _ok_response([{"cid": 1}])
        with patch.object(bsub, "_build_client", return_value=client), \
             patch.object(config, "SUBTITLE_DIR", str(self.sub_dir)), \
             patch.object(config, "PROJECT_ROOT", Path(self.tmp.name)):
            recovered = bsub._recover_orphaned_subtitles(set(), None)
        self.assertEqual(sorted(e["bvid"] for e in recovered), ["BV-A", "BV-B"])


class LegacyMigrationTests(unittest.TestCase):
    """_migrate_legacy_pending_queue：把旧的公共队列按频道归属拆分到各账号。"""

    def _profiles(self):
        snap = Profile(
            name="snap",
            youtube=YouTubeSettings(channels=[YouTubeChannel("UC1", "Snap Judgments")]),
        )
        deadlock = Profile(
            name="deadlock",
            youtube=YouTubeSettings(channels=[YouTubeChannel("UC2", "Piggy")]),
        )
        return {"snap": snap, "deadlock": deadlock}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name) / "state"
        self.state.mkdir(parents=True)

    def _write_upload_log(self, entries):
        (self.state / "upload_log.json").write_text(
            json.dumps(entries), encoding="utf-8")

    def test_split_by_channel_attribution(self):
        legacy = self.state / "pending_subtitles.json"
        legacy.write_text(json.dumps([
            {"bvid": "BV-A", "aid": 1,
             "translated_path": "downloads\\subtitles\\vidA.zh-CN.srt",
             "added_at": "2026-07-01T00:00:00Z"},
            {"bvid": "BV-B", "aid": 2,
             "translated_path": "downloads\\subtitles\\vidB.zh-CN.srt",
             "added_at": "2026-07-02T00:00:00Z"},
        ]), encoding="utf-8")
        self._write_upload_log([
            {"video_id": "vidA", "channel_title": "Snap Judgments", "bvid": "BV-A"},
            {"video_id": "vidB", "channel_title": "Piggy", "bvid": "BV-B"},
        ])
        with patch.object(bsub, "_active_profile_name", return_value="snap"), \
             patch.object(profile_mod, "load_profiles", return_value=self._profiles()), \
             patch.object(config, "PROJECT_ROOT", Path(self.tmp.name)):
            bsub._migrate_legacy_pending_queue()
        snap_q = json.loads(
            (self.state / "snap" / "pending_subtitles.json").read_text(encoding="utf-8"))
        deadlock_q = json.loads(
            (self.state / "deadlock" / "pending_subtitles.json").read_text(encoding="utf-8"))
        self.assertEqual([e["bvid"] for e in snap_q], ["BV-A"])
        self.assertEqual([e["bvid"] for e in deadlock_q], ["BV-B"])
        self.assertFalse(legacy.exists())

    def test_unattributed_stays_in_legacy(self):
        legacy = self.state / "pending_subtitles.json"
        legacy.write_text(json.dumps([
            {"bvid": "BV-A", "aid": 1, "translated_path": "x\\vidA.zh-CN.srt"},
            {"bvid": "BV-B", "aid": 2, "translated_path": "x\\vidB.zh-CN.srt"},
        ]), encoding="utf-8")
        self._write_upload_log([
            {"video_id": "vidA", "channel_title": "Snap Judgments", "bvid": "BV-A"},
        ])
        with patch.object(bsub, "_active_profile_name", return_value="snap"), \
             patch.object(profile_mod, "load_profiles", return_value=self._profiles()), \
             patch.object(config, "PROJECT_ROOT", Path(self.tmp.name)):
            bsub._migrate_legacy_pending_queue()
        snap_q = json.loads(
            (self.state / "snap" / "pending_subtitles.json").read_text(encoding="utf-8"))
        self.assertEqual([e["bvid"] for e in snap_q], ["BV-A"])
        remaining = json.loads(legacy.read_text(encoding="utf-8"))
        self.assertEqual([e["bvid"] for e in remaining], ["BV-B"])

    def test_idempotent_second_run_noop(self):
        legacy = self.state / "pending_subtitles.json"
        legacy.write_text(json.dumps([
            {"bvid": "BV-A", "aid": 1, "translated_path": "x\\vidA.zh-CN.srt"},
        ]), encoding="utf-8")
        self._write_upload_log([
            {"video_id": "vidA", "channel_title": "Snap Judgments", "bvid": "BV-A"},
        ])
        with patch.object(bsub, "_active_profile_name", return_value="snap"), \
             patch.object(profile_mod, "load_profiles", return_value=self._profiles()), \
             patch.object(config, "PROJECT_ROOT", Path(self.tmp.name)):
            bsub._migrate_legacy_pending_queue()
            bsub._migrate_legacy_pending_queue()
        snap_q = json.loads(
            (self.state / "snap" / "pending_subtitles.json").read_text(encoding="utf-8"))
        self.assertEqual(len(snap_q), 1)
        self.assertFalse(legacy.exists())

    def test_merge_dedup_by_bvid(self):
        queue = self.state / "deadlock" / "pending_subtitles.json"
        queue.parent.mkdir(parents=True)
        queue.write_text(json.dumps([
            {"bvid": "BV-B", "aid": 9, "translated_path": "old.zh-CN.srt",
             "added_at": "2026-07-01T00:00:00Z"},
        ]), encoding="utf-8")
        legacy = self.state / "pending_subtitles.json"
        legacy.write_text(json.dumps([
            {"bvid": "BV-B", "aid": 2, "translated_path": "x\\vidB.zh-CN.srt",
             "added_at": "2026-07-02T00:00:00Z"},
        ]), encoding="utf-8")
        self._write_upload_log([
            {"video_id": "vidB", "channel_title": "Piggy", "bvid": "BV-B"},
        ])
        with patch.object(bsub, "_active_profile_name", return_value="snap"), \
             patch.object(profile_mod, "load_profiles", return_value=self._profiles()), \
             patch.object(config, "PROJECT_ROOT", Path(self.tmp.name)):
            bsub._migrate_legacy_pending_queue()
        merged = json.loads(queue.read_text(encoding="utf-8"))
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["aid"], 2)  # 更新的 added_at 胜出
        self.assertFalse(legacy.exists())

    def test_noop_legacy_default_mode(self):
        legacy = self.state / "pending_subtitles.json"
        legacy.write_text(json.dumps([
            {"bvid": "BV-A", "aid": 1, "translated_path": "x\\a.zh-CN.srt"},
        ]), encoding="utf-8")
        before = legacy.read_bytes()
        with patch.object(bsub, "_active_profile_name", return_value="default"), \
             patch.object(profile_mod, "is_multi_profile", return_value=False):
            bsub._migrate_legacy_pending_queue()
        self.assertEqual(legacy.read_bytes(), before)


class RequeueMissingSubtitlesTests(unittest.TestCase):
    """requeue_missing_subtitles：一次性恢复——缺中文字幕的视频重新入队。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.queue = Path(self.tmp.name) / "pending.json"
        self.state = Path(self.tmp.name) / "state"
        self.state.mkdir(parents=True)
        self.upload_log = [
            {"video_id": "vidA", "channel_title": "ChanA", "bvid": "BV-A", "aid": 1},
            {"video_id": "vidB", "channel_title": "ChanA", "bvid": "BV-B", "aid": 2},
            {"video_id": "vidC", "channel_title": "ChanB", "bvid": "BV-C", "aid": 3},
        ]
        (self.state / "upload_log.json").write_text(
            json.dumps(self.upload_log), encoding="utf-8")

    def _client(self, has_zh_bvids=()):
        responses = {}
        for item in self.upload_log:
            has_zh = item["bvid"] in has_zh_bvids
            responses[item["bvid"]] = httpx.Response(200, json={
                "code": 0,
                "data": {"subtitle": {"list": [{"lan": "zh-CN"}] if has_zh else []}},
            })
        client = MagicMock()
        client.get.side_effect = lambda *a, **kw: responses[kw["params"]["bvid"]]
        return client

    def _run(self, client, channel_titles):
        with patch.object(bsub, "_pending_subtitles_path", return_value=self.queue), \
             patch.object(bsub, "_build_client", return_value=client), \
             patch.object(bsub, "_active_profile_channel_titles", return_value=channel_titles), \
             patch.object(bsub.time, "sleep"), \
             patch.object(config, "PROJECT_ROOT", Path(self.tmp.name)):
            return bsub.requeue_missing_subtitles()

    def test_requeues_videos_without_zh_subtitle(self):
        client = self._client(has_zh_bvids=("BV-B",))
        self.assertEqual(self._run(client, {"ChanA", "ChanB"}), 2)
        entries = json.loads(self.queue.read_text(encoding="utf-8"))
        by_bvid = {e["bvid"]: e for e in entries}
        self.assertIn("BV-A", by_bvid)
        self.assertIn("BV-C", by_bvid)
        self.assertNotIn("BV-B", by_bvid)  # B站已有中文字幕 → 不入队
        # 重新入队的 translated_path 指向缺失文件 → 下轮自动重新生成
        self.assertTrue(by_bvid["BV-A"]["translated_path"].endswith("vidA.zh-CN.srt"))

    def test_skips_already_queued_without_network(self):
        self.queue.write_text(json.dumps([
            {"bvid": "BV-A", "aid": 1, "translated_path": "x\\vidA.zh-CN.srt"},
        ]), encoding="utf-8")
        client = self._client()
        # 作用域 ChanA：BV-A 已在队列不查询；BV-B 无字幕入队；BV-C 是别的频道
        self.assertEqual(self._run(client, {"ChanA"}), 1)
        self.assertEqual(client.get.call_count, 1)  # 只查询了 BV-B
        entries = json.loads(self.queue.read_text(encoding="utf-8"))
        self.assertEqual(sorted(e["bvid"] for e in entries), ["BV-A", "BV-B"])

    def test_legacy_mode_checks_all_channels(self):
        client = self._client()
        self.assertEqual(self._run(client, None), 3)
        self.assertEqual(client.get.call_count, 3)

    def test_no_candidates_returns_zero(self):
        (self.state / "upload_log.json").write_text("[]", encoding="utf-8")
        self.assertEqual(self._run(self._client(), {"ChanA"}), 0)
        self.assertFalse(self.queue.exists())


class RegenerateMissingSubtitleTests(unittest.TestCase):
    """_regenerate_missing_subtitle：文件缺失时复用源字幕重新翻译，或重新下载。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.sub_dir = Path(self.tmp.name) / "subs"
        self.sub_dir.mkdir(parents=True)
        self.translated = str(self.sub_dir / "abc123.zh-CN.srt")
        self.src = self.sub_dir / "abc123.en.srt"
        self.entry = {"bvid": "BV1", "aid": 1, "translated_path": self.translated}

    def _patch_pipeline(self, *, parse_cues=None, translate_cues=None):
        patchers = [
            patch("yt2bili.subtitles.parser.parse_subtitle",
                  return_value=parse_cues if parse_cues is not None else [{"index": 1}]),
            patch("yt2bili.subtitles.translator.translate_cues",
                  return_value=translate_cues if translate_cues is not None else [{"index": 1, "text": "hi"}]),
            patch("yt2bili.subtitles.writer.write_srt"),
        ]
        mocks = {}
        for patcher in patchers:
            m = patcher.start()
            self.addCleanup(patcher.stop)
            mocks[patcher.attribute] = m
        return mocks

    def test_kept_source_reused_without_download(self):
        self.src.write_text("1\n00:00:01,000 --> 00:00:02,000\nhello\n", encoding="utf-8")
        mocks = self._patch_pipeline()
        with patch("yt2bili.subtitles.downloader.download_subtitles",
                   side_effect=AssertionError("保留源字幕时不应重新下载")):
            result = bsub._regenerate_missing_subtitle(self.entry)
        self.assertEqual(result, self.translated)
        mocks["write_srt"].assert_called_once_with([{"index": 1, "text": "hi"}], self.translated)

    def test_unparseable_kept_source_falls_back_to_download(self):
        self.src.write_text("garbage", encoding="utf-8")
        # 第一次解析（保留的源字幕）为空 → 触发重新下载；下载后解析成功
        with patch("yt2bili.subtitles.parser.parse_subtitle",
                   side_effect=[[], [{"index": 1}]]), \
             patch("yt2bili.subtitles.translator.translate_cues",
                   return_value=[{"index": 1, "text": "hi"}]), \
             patch("yt2bili.subtitles.writer.write_srt"), \
             patch("yt2bili.subtitles.downloader.download_subtitles",
                   return_value=str(self.src)) as mock_dl:
            result = bsub._regenerate_missing_subtitle(self.entry)
        self.assertEqual(result, self.translated)
        mock_dl.assert_called_once()
        self.assertEqual(mock_dl.call_args.args[1], "abc123")

    def test_download_url_from_upload_log(self):
        state = Path(self.tmp.name) / "state"
        state.mkdir(parents=True)
        (state / "upload_log.json").write_text(json.dumps([
            {"video_id": "abc123", "url": "https://www.youtube.com/watch?v=abc123&t=5"},
        ]), encoding="utf-8")
        self._patch_pipeline()
        with patch("yt2bili.subtitles.downloader.download_subtitles",
                   return_value=str(self.src)) as mock_dl, \
             patch.object(config, "PROJECT_ROOT", Path(self.tmp.name)):
            result = bsub._regenerate_missing_subtitle(self.entry)
        self.assertEqual(result, self.translated)
        self.assertEqual(mock_dl.call_args.args[0], "https://www.youtube.com/watch?v=abc123&t=5")

    def test_download_url_fallback_constructed(self):
        self._patch_pipeline()
        with patch("yt2bili.subtitles.downloader.download_subtitles",
                   return_value=str(self.src)) as mock_dl, \
             patch.object(config, "PROJECT_ROOT", Path(self.tmp.name)):
            result = bsub._regenerate_missing_subtitle(self.entry)
        self.assertEqual(result, self.translated)
        self.assertEqual(mock_dl.call_args.args[0], "https://www.youtube.com/watch?v=abc123")

    def test_download_failure_returns_none(self):
        with patch("yt2bili.subtitles.downloader.download_subtitles", return_value=None):
            self.assertIsNone(bsub._regenerate_missing_subtitle(self.entry))

    def test_translate_failure_returns_none(self):
        self.src.write_text("1\n00:00:01,000 --> 00:00:02,000\nhello\n", encoding="utf-8")
        self._patch_pipeline(translate_cues=[])
        with patch("yt2bili.subtitles.translator.translate_cues",
                   side_effect=RuntimeError("翻译失败")):
            self.assertIsNone(bsub._regenerate_missing_subtitle(self.entry))


if __name__ == "__main__":
    unittest.main()
