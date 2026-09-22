"""自测：清理 B站 已消失视频的本地记录（cleanup.py）。"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from yt2bili import profile as profile_mod
from yt2bili.bilibili import cleanup
from yt2bili.bilibili.cleanup import (
    VERDICT_ALIVE,
    VERDICT_GONE,
    VERDICT_UNKNOWN,
    Candidate,
    CleanupReport,
    apply_purge,
    collect_candidates,
    probe_bvid,
)


def _gone(*_args, **_kwargs):
    return VERDICT_GONE, 62002


def _alive(*_args, **_kwargs):
    return VERDICT_ALIVE, 0


class CandidateTests(unittest.TestCase):
    """Candidate.age_hours：新鲜度保护依赖的时间推算。"""

    def test_age_from_iso_stamp(self):
        cand = Candidate(bvid="BV1", stamps=["2026-07-16T12:36:13Z"])
        self.assertGreater(cand.age_hours(), 24)

    def test_age_is_inf_without_usable_stamp(self):
        self.assertEqual(Candidate(bvid="BV1").age_hours(), float("inf"))
        self.assertEqual(Candidate(bvid="BV1", stamps=[""]).age_hours(), float("inf"))
        self.assertEqual(
            Candidate(bvid="BV1", stamps=["不是时间"]).age_hours(), float("inf")
        )

    def test_age_uses_newest_stamp(self):
        cand = Candidate(
            bvid="BV1",
            stamps=["2020-01-01T00:00:00Z", "2026-07-16T12:36:13Z"],
        )
        fresh = Candidate(
            bvid="BV2",
            stamps=["2020-01-01T00:00:00Z", "2099-01-01T00:00:00Z"],
        )
        self.assertLess(cand.age_hours(), 10_000)
        self.assertLess(fresh.age_hours(), 0)


class CollectCandidatesTests(unittest.TestCase):
    """collect_candidates：三个状态文件合并、去重、已墓碑的跳过。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.processed = root / "processed_videos.json"
        self.collections = root / "pending_collections.json"
        self.subtitles = root / "pending_subtitles.json"

    def _write(self, path, payload):
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def test_merges_sources_by_bvid(self):
        self._write(self.processed, {"videos": {
            "vid1": {"bvid": "BV1", "status": "uploaded", "aid": 11,
                     "title": "标题", "channel_title": "频道",
                     "last_success_at": "2026-08-01T00:00:00Z"},
        }})
        self._write(self.collections, [
            {"bvid": "BV1", "video_id": "vid1", "aid": 11,
             "channel_title": "频道", "added_at": "2026-08-02T00:00:00Z"},
        ])
        self._write(self.subtitles, [
            {"bvid": "BV1", "aid": 11, "added_at": "2026-08-03T00:00:00Z"},
            {"bvid": "BV2", "aid": 22, "added_at": "2026-08-04T00:00:00Z"},
        ])
        cands = {c.bvid: c for c in collect_candidates(
            self.processed, self.collections, self.subtitles)}
        self.assertEqual(set(cands), {"BV1", "BV2"})
        self.assertEqual(
            cands["BV1"].sources, ["processed", "collections", "subtitles"])
        self.assertEqual(cands["BV1"].video_id, "vid1")
        self.assertEqual(cands["BV1"].title, "标题")
        self.assertEqual(sorted(cands["BV2"].sources), ["subtitles"])

    def test_tombstoned_and_unreferenced_is_skipped(self):
        """已标 deleted 且不在任何队列 → 不重复探测（保证重复运行很快）。"""
        self._write(self.processed, {"videos": {
            "vid1": {"bvid": "BV1", "status": "deleted"},
            "vid2": {"bvid": "BV2", "status": "uploaded", "aid": 2},
            "vid3": {"bvid": "", "status": "uploaded"},
        }})
        cands = collect_candidates(self.processed, self.collections, self.subtitles)
        self.assertEqual([c.bvid for c in cands], ["BV2"])

    def test_tombstoned_but_still_queued_is_probed(self):
        """经典卡死场景：processed 已标 deleted，但队列里还留着。"""
        self._write(self.processed, {"videos": {
            "vid1": {"bvid": "BV1", "status": "deleted"},
        }})
        self._write(self.collections, [{"bvid": "BV1", "video_id": "vid1"}])
        cands = collect_candidates(self.processed, self.collections, self.subtitles)
        self.assertEqual([c.bvid for c in cands], ["BV1"])
        self.assertEqual(cands[0].sources, ["collections"])

    def test_missing_files_are_tolerated(self):
        self.assertEqual(
            collect_candidates(self.processed, self.collections, self.subtitles), [])


class ProbeBvidTests(unittest.TestCase):
    """probe_bvid：存活 / 消失 / 未知（含限流退避）三类判定。"""

    def test_alive(self):
        with patch.object(cleanup.subtitle_mod, "get_video_pages",
                          return_value=[{"cid": 5}]):
            self.assertEqual(probe_bvid("BV1", 1), (VERDICT_ALIVE, 0))

    def test_gone_codes(self):
        for code in (-404, 62002):
            with self.subTest(code=code):
                err = RuntimeError(f"get_video_pages 返回错误 (code={code}): x")
                with patch.object(cleanup.subtitle_mod, "get_video_pages",
                                  side_effect=err):
                    verdict, got = probe_bvid("BV1", 1)
                self.assertEqual(verdict, VERDICT_GONE)
                self.assertEqual(got, code)

    def test_owner_only_visibility_is_not_gone(self):
        """62012（仅自己可见）不是删除：UP主 自己查得到，不能清理。"""
        err = RuntimeError("get_video_pages 返回错误 (code=62012): x")
        with patch.object(cleanup.subtitle_mod, "get_video_pages", side_effect=err):
            verdict, code = probe_bvid("BV1", 1)
        self.assertEqual(verdict, VERDICT_UNKNOWN)
        self.assertEqual(code, 62012)

    def test_unknown_on_network_error(self):
        with patch.object(cleanup.subtitle_mod, "get_video_pages",
                          side_effect=RuntimeError("B站视频信息查询网络错误: x")):
            verdict, code = probe_bvid("BV1", 1)
        self.assertEqual(verdict, VERDICT_UNKNOWN)
        self.assertIsNone(code)

    def test_unknown_when_pages_not_ready(self):
        with patch.object(cleanup.subtitle_mod, "get_video_pages",
                          return_value=[]):
            self.assertEqual(probe_bvid("BV1", 1), (VERDICT_UNKNOWN, None))

    def test_rate_limit_backs_off_then_returns_unknown(self):
        err = RuntimeError("get_video_pages 返回错误 (code=-412): 请求被拦截")
        with patch.object(cleanup.subtitle_mod, "get_video_pages",
                          side_effect=err) as fake, \
             patch.object(cleanup.time, "sleep") as sleeper:
            verdict, code = probe_bvid("BV1", 1)
        self.assertEqual(verdict, VERDICT_UNKNOWN)
        self.assertEqual(code, -412)
        self.assertEqual(fake.call_count, len(cleanup.RATE_LIMIT_BACKOFF) + 1)
        self.assertEqual(sleeper.call_count, len(cleanup.RATE_LIMIT_BACKOFF))


class ApplyPurgeTests(unittest.TestCase):
    """apply_purge：三个状态文件 + 字幕文件 + 日期缓存的清理。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.processed = self.root / "processed_videos.json"
        self.collections = self.root / "pending_collections.json"
        self.subtitles = self.root / "pending_subtitles.json"
        self.subtitle_dir = self.root / "subtitles"
        self.subtitle_dir.mkdir()
        self.addCleanup(patch.stopall)
        patcher = patch.object(cleanup.config, "SUBTITLE_DIR",
                               str(self.subtitle_dir))
        patcher.start()

    def _seed(self):
        self.processed.write_text(json.dumps({"version": 1, "videos": {
            "vid1": {"video_id": "vid1", "bvid": "BV1", "aid": 1,
                     "status": "uploaded", "title": "T"},
            "vid2": {"video_id": "vid2", "bvid": "BV2", "aid": 2,
                     "status": "uploaded", "title": "K"},
        }}), encoding="utf-8")
        self.collections.write_text(json.dumps([
            {"video_id": "vid1", "bvid": "BV1", "status": "failed"},
            {"video_id": "vid2", "bvid": "BV2", "status": "pending"},
        ]), encoding="utf-8")
        self.subtitles.write_text(json.dumps([
            {"bvid": "BV1", "aid": 1, "translated_path": "x.srt"},
        ]), encoding="utf-8")
        (self.subtitle_dir / "vid1.zh-CN.srt").write_text("1\n", encoding="utf-8")
        (self.subtitle_dir / "vid1.en-orig.srt").write_text("1\n", encoding="utf-8")
        (self.root / "bvid_dates.json").write_text(json.dumps(
            {"BV1": "2026-08-01", "BV2": "2026-08-02"}), encoding="utf-8")

    def test_purge_marks_removes_and_cleans(self):
        self._seed()
        gone = [Candidate(bvid="BV1", video_id="vid1", aid=1)]
        summary = apply_purge(
            gone, self.processed, self.collections, self.subtitles,
            stamp="20260921-000000")

        state = json.loads(self.processed.read_text(encoding="utf-8"))
        self.assertEqual(state["videos"]["vid1"]["status"], "deleted")
        self.assertEqual(state["videos"]["vid2"]["status"], "uploaded")

        queue = json.loads(self.collections.read_text(encoding="utf-8"))
        self.assertEqual([e["bvid"] for e in queue], ["BV2"])
        self.assertFalse(self.subtitles.exists())  # 队列清空后文件删除
        self.assertEqual(summary["processed_marked"], 1)
        self.assertEqual(summary["collections_removed"], 1)
        self.assertEqual(summary["subtitles_removed"], 1)
        self.assertEqual(sorted(summary["files_removed"]),
                         ["vid1.en-orig.srt", "vid1.zh-CN.srt"])
        self.assertFalse((self.subtitle_dir / "vid1.zh-CN.srt").exists())

        dates = json.loads((self.root / "bvid_dates.json").read_text(encoding="utf-8"))
        self.assertEqual(dates, {"BV2": "2026-08-02"})

    def test_backups_written(self):
        self._seed()
        apply_purge([Candidate(bvid="BV1", video_id="vid1")],
                    self.processed, self.collections, self.subtitles,
                    stamp="20260921-000000")
        for path in (self.processed, self.collections, self.subtitles):
            self.assertTrue(
                path.with_name(f"{path.name}.bak-clean-20260921-000000").exists(),
                path.name,
            )

    def test_dry_run_changes_nothing(self):
        self._seed()
        before = {p.name: p.read_bytes() for p in (
            self.processed, self.collections, self.subtitles)}
        summary = apply_purge([Candidate(bvid="BV1", video_id="vid1")],
                             self.processed, self.collections, self.subtitles,
                             dry_run=True)
        self.assertEqual(summary["processed_marked"], 0)
        for path in (self.processed, self.collections, self.subtitles):
            self.assertEqual(path.read_bytes(), before[path.name])
        self.assertTrue((self.subtitle_dir / "vid1.zh-CN.srt").exists())

    def test_empty_gone_list_is_noop(self):
        self._seed()
        summary = apply_purge([], self.processed, self.collections, self.subtitles)
        self.assertEqual(summary["processed_marked"], 0)
        self.assertTrue(self.subtitles.exists())


class ScanProfileTests(unittest.TestCase):
    """scan_profile：新鲜度保护、未知状态放行、报告计数。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.processed = self.root / "processed_videos.json"
        self.collections = self.root / "pending_collections.json"
        self.subtitles = self.root / "pending_subtitles.json"
        self.subtitles.write_text("[]", encoding="utf-8")
        self.addCleanup(patch.stopall)
        patch.object(cleanup, "pending_collections_path",
                     return_value=self.collections).start()
        patch.object(cleanup.subtitle_mod, "pending_subtitles_path",
                     return_value=self.subtitles).start()
        # 状态文件位置现由 profile.state_file_path 统一解析（与队列同一个目录）
        patch.object(profile_mod, "state_file_path",
                     return_value=self.processed).start()
        patch.object(cleanup.time, "sleep").start()

    def _seed(self, entries):
        self.processed.write_text(
            json.dumps({"version": 1, "videos": entries}, ensure_ascii=False),
            encoding="utf-8")

    def _scan(self, probe, dry_run=False):
        return cleanup.scan_profile(
            profile_name="snap", probe=probe, dry_run=dry_run,
            progress_every=100)

    def test_gone_entries_retired_others_kept(self):
        self._seed({
            "old1": {"bvid": "BV1", "status": "uploaded", "aid": 1,
                     "title": "旧视频", "last_success_at": "2026-07-01T00:00:00Z"},
            "old2": {"bvid": "BV2", "status": "uploaded", "aid": 2,
                     "title": "还在", "last_success_at": "2026-07-01T00:00:00Z"},
            "old3": {"bvid": "BV3", "status": "uploaded", "aid": 3,
                     "title": "未知", "last_success_at": "2026-07-01T00:00:00Z"},
        })

        def probe(bvid, aid):
            return {
                "BV1": (VERDICT_GONE, 62002),
                "BV2": (VERDICT_ALIVE, 0),
                "BV3": (VERDICT_UNKNOWN, None),
            }[bvid]

        report = self._scan(probe)
        self.assertEqual(report.checked, 3)
        self.assertEqual(report.alive, 1)
        self.assertEqual([c.bvid for c in report.gone], ["BV1"])
        self.assertEqual(len(report.unknown), 1)

        state = json.loads(self.processed.read_text(encoding="utf-8"))
        self.assertEqual(state["videos"]["old1"]["status"], "deleted")
        self.assertEqual(state["videos"]["old2"]["status"], "uploaded")
        self.assertEqual(state["videos"]["old3"]["status"], "uploaded")

    def test_fresh_entries_are_not_probed(self):
        self._seed({
            "new1": {"bvid": "BV1", "status": "uploaded", "aid": 1,
                     "last_success_at": cleanup._now_iso()},
        })
        calls = []

        def probe(bvid, aid):
            calls.append(bvid)
            return VERDICT_GONE, 62002

        report = self._scan(probe)
        self.assertEqual(calls, [])
        self.assertEqual(len(report.skipped_fresh), 1)
        self.assertEqual(report.cleaned, 0)
        state = json.loads(self.processed.read_text(encoding="utf-8"))
        self.assertEqual(state["videos"]["new1"]["status"], "uploaded")

    def test_dry_run_reports_without_writing(self):
        self._seed({
            "old1": {"bvid": "BV1", "status": "uploaded", "aid": 1,
                     "last_success_at": "2026-07-01T00:00:00Z"},
        })
        before = self.processed.read_bytes()
        report = self._scan(_gone, dry_run=True)
        self.assertEqual(report.cleaned, 1)
        self.assertTrue(report.dry_run)
        self.assertEqual(self.processed.read_bytes(), before)

    def test_report_cleaned_matches_gone(self):
        report = CleanupReport(profile="snap")
        report.gone.append(Candidate(bvid="BV1"))
        self.assertEqual(report.cleaned, 1)


if __name__ == "__main__":
    unittest.main()
