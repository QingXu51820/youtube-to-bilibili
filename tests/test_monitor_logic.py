"""自测：订阅监控核心逻辑（状态、跳过分类、URL/时长解析、监控循环）。"""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from yt2bili import config
from yt2bili.youtube import monitor
from yt2bili.youtube.monitor import (
    STATUS_SKIPPED_CONTENT,
    STATUS_SKIPPED_LIVE,
    STATUS_SKIPPED_LONG,
    STATUS_SKIPPED_VERTICAL,
    STATUS_UPLOADED,
    VideoItem,
    format_duration,
    is_content_skip_result,
    is_live_skip_result,
    is_long_skip_result,
    is_vertical_skip_result,
    load_state,
    parse_iso8601_duration_seconds,
    queue_defer_reason,
    record_failure,
    record_success,
    save_state,
    should_skip_video,
    sort_candidates_for_queue,
    video_id_from_url,
)


def make_video(video_id="abc123", title="Test Video", channel="Chan",
               published="2026-08-01T00:00:00Z", url=""):
    return VideoItem(
        title=title,
        channel_title=channel,
        published_at=published,
        url=url or f"https://www.youtube.com/watch?v={video_id}",
        channel_id="UC" + video_id[:8].ljust(22, "0"),
        video_id=video_id,
    )


def make_result(success=False, stage="", error="", bvid="", translated_title=""):
    return SimpleNamespace(
        success=success, stage=stage, error=error, bvid=bvid,
        aid=0, translated_title=translated_title, original_title="Orig",
    )


class VideoIdFromUrlTests(unittest.TestCase):
    def test_watch_url(self):
        self.assertEqual(
            video_id_from_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=5s"),
            "dQw4w9WgXcQ",
        )

    def test_youtu_be(self):
        self.assertEqual(
            video_id_from_url("https://youtu.be/dQw4w9WgXcQ?si=abc"),
            "dQw4w9WgXcQ",
        )

    def test_shorts(self):
        self.assertEqual(
            video_id_from_url("https://www.youtube.com/shorts/dQw4w9WgXcQ"),
            "dQw4w9WgXcQ",
        )

    def test_embed(self):
        self.assertEqual(
            video_id_from_url("https://www.youtube.com/embed/dQw4w9WgXcQ"),
            "dQw4w9WgXcQ",
        )

    def test_live(self):
        self.assertEqual(
            video_id_from_url("https://www.youtube.com/live/dQw4w9WgXcQ"),
            "dQw4w9WgXcQ",
        )

    def test_playlist_with_v_param(self):
        self.assertEqual(
            video_id_from_url("https://www.youtube.com/playlist?list=abc&v=dQw4w9WgXcQ"),
            "dQw4w9WgXcQ",
        )

    def test_invalid(self):
        self.assertEqual(video_id_from_url("https://example.com/foo"), "")
        self.assertEqual(video_id_from_url(""), "")


class DurationTests(unittest.TestCase):
    def test_parse_iso8601(self):
        self.assertEqual(parse_iso8601_duration_seconds("PT1H02M03S"), 3723)
        self.assertEqual(parse_iso8601_duration_seconds("PT45S"), 45)
        self.assertEqual(parse_iso8601_duration_seconds("P1DT2H"), 93600)
        self.assertEqual(parse_iso8601_duration_seconds("garbage"), 0)
        self.assertEqual(parse_iso8601_duration_seconds(""), 0)

    def test_format_duration(self):
        self.assertEqual(format_duration(0), "未知时长")
        self.assertEqual(format_duration(65), "1:05")
        self.assertEqual(format_duration(3723), "1:02:03")


class LoadStateTests(unittest.TestCase):
    def test_missing_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = load_state(Path(tmp) / "nope.json")
        self.assertEqual(state["videos"], {})

    def test_valid_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.json"
            save_state(path, {"version": 1, "videos": {}})
            state = load_state(path)
        self.assertEqual(state["videos"], {})

    def test_corrupt_json_recovered_with_backup(self):
        """回归：损坏状态文件必须备份+重建空状态，而不是 SystemExit 杀死 monitor。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.json"
            path.write_text("{broken json", encoding="utf-8")
            state = load_state(path)
            backups = list(Path(tmp).glob("s.json.corrupt-*"))
        self.assertEqual(state["videos"], {})
        self.assertEqual(len(backups), 1)

    def test_videos_not_dict_recovered(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.json"
            path.write_text(json.dumps({"videos": "nope"}), encoding="utf-8")
            state = load_state(path)
        self.assertEqual(state["videos"], {})

    def test_utf8_bom_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.json"
            path.write_bytes(
                b"\xef\xbb\xbf" + json.dumps({"videos": {}}).encode("utf-8")
            )
            state = load_state(path)
        self.assertEqual(state["videos"], {})


class SkipClassificationTests(unittest.TestCase):
    def setUp(self):
        self.state = {"version": 1, "videos": {}}

    def _seed(self, video_id, status):
        self.state["videos"][video_id] = {"status": status}

    def test_no_entry_not_skipped(self):
        self.assertEqual(should_skip_video(self.state, make_video("v1")), (False, ""))

    def test_each_skip_status(self):
        cases = [
            (STATUS_UPLOADED, "已上传"),
            (STATUS_SKIPPED_LIVE, "直播内容已永久跳过"),
            (STATUS_SKIPPED_LONG, "超长视频已永久跳过"),
            (STATUS_SKIPPED_VERTICAL, "竖屏视频已永久跳过"),
            (STATUS_SKIPPED_CONTENT, "内容筛选已跳过"),
        ]
        for status, expected in cases:
            with self.subTest(status=status):
                self._seed("v1", status)
                skip, reason = should_skip_video(self.state, make_video("v1"))
                self.assertTrue(skip)
                self.assertIn(expected, reason)

    def test_failed_status_not_skipped(self):
        self._seed("v1", "failed")
        self.assertEqual(should_skip_video(self.state, make_video("v1")), (False, ""))

    def test_markers(self):
        live = make_result(stage="download", error="不是可下载的普通视频")
        self.assertTrue(is_live_skip_result(live))
        self.assertTrue(is_live_skip_result(make_result(stage="download", error="正在直播")))
        self.assertFalse(is_live_skip_result(make_result(stage="translate", error="正在直播")))
        self.assertTrue(is_vertical_skip_result(make_result(stage="download", error="检测到竖屏视频")))
        self.assertTrue(is_content_skip_result(make_result(stage="download", error="内容筛选已跳过")))
        self.assertTrue(is_long_skip_result(make_result(stage="download", error="视频超过最大时长限制")))

    def test_record_failure_classification(self):
        self.state = {"version": 1, "videos": {}}
        cases = [
            (make_result(stage="download", error="正在直播"), STATUS_SKIPPED_LIVE),
            (make_result(stage="download", error="检测到竖屏视频"), STATUS_SKIPPED_VERTICAL),
            (make_result(stage="download", error="内容筛选已跳过"), STATUS_SKIPPED_CONTENT),
            (make_result(stage="download", error="视频超过最大时长限制"), STATUS_SKIPPED_LONG),
            (make_result(stage="upload", error="网络错误"), "failed"),
        ]
        for result, expected in cases:
            with self.subTest(error=result.error):
                record_failure(self.state, make_video("v1"), result)
                entry = self.state["videos"]["v1"]
                self.assertEqual(entry["status"], expected)
                self.assertEqual(entry["error"], result.error)

    def test_record_success(self):
        record_success(
            self.state,
            make_video("v1"),
            make_result(success=True, bvid="BV1", translated_title="译"),
        )
        entry = self.state["videos"]["v1"]
        self.assertEqual(entry["status"], STATUS_UPLOADED)
        self.assertEqual(entry["bvid"], "BV1")
        self.assertEqual(entry["translated_title"], "译")


class QueueSortTests(unittest.TestCase):
    def test_defer_reason(self):
        self.assertEqual(queue_defer_reason({}, 60), "")
        self.assertEqual(queue_defer_reason({"is_live_archive": True}, 60), "直播回放")
        self.assertEqual(queue_defer_reason({"duration_seconds": 7200}, 60), "超长视频≥60分钟")
        self.assertEqual(queue_defer_reason({"duration_seconds": 7200}, 0), "")

    def test_sort_moves_long_to_end(self):
        videos = [
            make_video("a", title="A"),
            make_video("b", title="B"),
            make_video("c", title="C"),
        ]
        details = {
            "a": {"duration_seconds": 60, "is_live_archive": False},
            "b": {"duration_seconds": 7200, "is_live_archive": False},
        }
        result = sort_candidates_for_queue(videos, details, 60)
        self.assertEqual([v.video_id for v in result], ["a", "c", "b"])

    def test_sort_disabled_when_threshold_zero(self):
        videos = [make_video("a"), make_video("b")]
        details = {"a": {"duration_seconds": 99999, "is_live_archive": False}}
        result = sort_candidates_for_queue(videos, details, 0)
        self.assertEqual([v.video_id for v in result], ["a", "b"])


class SeedStateTests(unittest.TestCase):
    def test_seed_from_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp) / "runs"
            runs.mkdir()
            report = {
                "generated_at": "2026-08-01T00:00:00+08:00",
                "results": [{
                    "success": True, "url": "https://youtu.be/abc123",
                    "original_title": "T", "translated_title": "译",
                    "bvid": "BV1", "aid": 1,
                }],
            }
            (runs / "r1.json").write_text(json.dumps(report), encoding="utf-8")
            state = {"version": 1, "videos": {}}
            seeded = monitor.seed_state_from_runs(state, runs)
        self.assertEqual(seeded, 1)
        entry = state["videos"]["abc123"]
        self.assertEqual(entry["status"], STATUS_UPLOADED)
        self.assertEqual(entry["source"], "runs")
        self.assertEqual(entry["run_report"], "r1.json")

    def test_seed_from_upload_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "upload_log.json"
            path.write_text(json.dumps([{
                "video_id": "xyz789", "url": "u", "title": "T",
                "channel_title": "C", "bvid": "BV2", "aid": 2,
                "translated_title": "译", "uploaded_at": "2026-08-01T00:00:00Z",
            }]), encoding="utf-8")
            with patch.object(monitor, "_upload_log_path", return_value=path):
                state = {"version": 1, "videos": {}}
                seeded = monitor.seed_state_from_upload_log(state)
        self.assertEqual(seeded, 1)
        self.assertEqual(state["videos"]["xyz789"]["bvid"], "BV2")

    def test_seed_from_upload_log_filters_by_profile(self):
        """回归：snap 账号不得导入 deadlock 的历史上传记录。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "upload_log.json"
            path.write_text(json.dumps([
                {"video_id": "snap1", "url": "u", "title": "T",
                 "channel_title": "Jeff Hoogland", "profile": "snap",
                 "bvid": "BV1", "aid": 1, "translated_title": "译",
                 "uploaded_at": "2026-08-01T00:00:00Z"},
                {"video_id": "dl1", "url": "u", "title": "T",
                 "channel_title": "Zerggy", "profile": "deadlock",
                 "bvid": "BV2", "aid": 2, "translated_title": "译",
                 "uploaded_at": "2026-08-01T00:00:00Z"},
                {"video_id": "old_snap", "url": "u", "title": "T",
                 "channel_title": "Jeff Hoogland",
                 "bvid": "BV3", "aid": 3, "translated_title": "译",
                 "uploaded_at": "2026-08-01T00:00:00Z"},
                {"video_id": "old_dl", "url": "u", "title": "T",
                 "channel_title": "Zerggy",
                 "bvid": "BV4", "aid": 4, "translated_title": "译",
                 "uploaded_at": "2026-08-01T00:00:00Z"},
                {"video_id": "old_unknown", "url": "u", "title": "T",
                 "channel_title": "",
                 "bvid": "BV5", "aid": 5, "translated_title": "译",
                 "uploaded_at": "2026-08-01T00:00:00Z"},
            ]), encoding="utf-8")
            prof = SimpleNamespace(youtube=SimpleNamespace(channels=[
                SimpleNamespace(channel_title="Jeff Hoogland"),
                SimpleNamespace(channel_title="MarvelSnap"),
            ]))
            with patch.object(monitor, "_upload_log_path", return_value=path), \
                 patch("yt2bili.profile.get_active_profile_name",
                       return_value="snap"), \
                 patch("yt2bili.profile.resolve_profile",
                       return_value=prof):
                state = {"version": 1, "videos": {}}
                seeded = monitor.seed_state_from_upload_log(state)
        self.assertEqual(seeded, 2)
        self.assertIn("snap1", state["videos"])
        self.assertIn("old_snap", state["videos"])
        self.assertNotIn("dl1", state["videos"])
        self.assertNotIn("old_dl", state["videos"])
        self.assertNotIn("old_unknown", state["videos"])

    def test_seed_from_runs_filters_by_profile(self):
        """回归：runs 报告按 profile 字段隔离，旧报告只归 legacy default。"""
        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp) / "runs"
            runs.mkdir()
            row = {
                "success": True, "original_title": "T",
                "translated_title": "译", "bvid": "BV1", "aid": 1,
            }
            reports = {
                "snap.json": {"profile": "snap", "generated_at": "now",
                              "results": [dict(row, url="https://youtu.be/snap1")]},
                "deadlock.json": {"profile": "deadlock", "generated_at": "now",
                                  "results": [dict(row, url="https://youtu.be/dl1")]},
                "legacy.json": {"generated_at": "now",
                                "results": [dict(row, url="https://youtu.be/leg1")]},
            }
            for name, content in reports.items():
                (runs / name).write_text(json.dumps(content), encoding="utf-8")
            with patch("yt2bili.profile.get_active_profile_name",
                       return_value="snap"):
                state = {"version": 1, "videos": {}}
                seeded = monitor.seed_state_from_runs(state, runs)
        self.assertEqual(seeded, 1)
        self.assertIn("snap1", state["videos"])
        self.assertNotIn("dl1", state["videos"])
        self.assertNotIn("leg1", state["videos"])

    def test_append_upload_log_entry_stores_profile(self):
        video = SimpleNamespace(video_id="v1", url="u", title="T",
                                channel_title="C")
        result = SimpleNamespace(bvid="BV1", aid=1, translated_title="译")
        entries = []
        with patch.object(monitor, "_load_upload_log", return_value=entries), \
             patch.object(monitor, "_save_upload_log",
                          side_effect=lambda e: entries.extend(e)), \
             patch("yt2bili.profile.get_active_profile_name",
                   return_value="snap"):
            monitor.append_upload_log_entry(video, result)
        self.assertEqual(entries[0]["profile"], "snap")


class RunMonitorCycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_path = Path(self.tmp.name) / "state.json"
        self.fetch_patcher = patch.object(monitor, "fetch_subscription_videos")
        self.mock_fetch = self.fetch_patcher.start()
        self.addCleanup(self.fetch_patcher.stop)
        self.log_patcher = patch.object(monitor, "append_upload_log_entry")
        self.mock_log = self.log_patcher.start()
        self.addCleanup(self.log_patcher.stop)
        self.sleep_patcher = patch.object(monitor.time, "sleep")
        self.sleep_patcher.start()
        self.addCleanup(self.sleep_patcher.stop)

    def _cycle(self, process_video, dry_run=False, write_report=None):
        return monitor.run_monitor_cycle(
            process_video=process_video,
            write_run_report=write_report,
            dry_run=dry_run,
            state_path=self.state_path,
            source="rss",
            limit=50,
            max_videos_per_channel=5,
            client_secret_file=Path("secret.json"),
            token_file=Path("token.json"),
            cache_file=Path(self.tmp.name) / "cache.json",
        )

    def test_dry_run_returns_empty_no_state_written(self):
        self.mock_fetch.return_value = [make_video("v1")]
        results = self._cycle(process_video=Mock(), dry_run=True)
        self.assertEqual(results, [])
        self.assertFalse(self.state_path.exists())

    def test_success_processes_and_records(self):
        self.mock_fetch.return_value = [make_video("v1")]
        report_path = Path(self.tmp.name) / "report.json"
        process = Mock(return_value=make_result(success=True, bvid="BV9", translated_title="译"))

        def _write_report(results):
            report_path.write_text(json.dumps([{"ok": True}]), encoding="utf-8")
            return report_path

        results = self._cycle(process, write_report=_write_report)
        self.assertTrue(results[0].success)
        process.assert_called_once()
        self.mock_log.assert_called_once()
        state = load_state(self.state_path)
        self.assertEqual(state["videos"]["v1"]["status"], STATUS_UPLOADED)
        self.assertTrue(report_path.exists())

    def test_retry_on_transient_download_failure(self):
        self.mock_fetch.return_value = [make_video("v1")]
        process = Mock(side_effect=[
            make_result(stage="download", error="临时网络错误"),
            make_result(success=True, bvid="BV8"),
        ])
        self._cycle(process)
        self.assertEqual(process.call_count, 2)
        state = load_state(self.state_path)
        self.assertEqual(state["videos"]["v1"]["status"], STATUS_UPLOADED)

    def test_no_retry_on_non_retryable_stage(self):
        self.mock_fetch.return_value = [make_video("v1")]
        process = Mock(return_value=make_result(stage="translate", error="翻译失败"))
        self._cycle(process)
        process.assert_called_once()  # translate 不在可重试阶段
        state = load_state(self.state_path)
        self.assertEqual(state["videos"]["v1"]["status"], "failed")

    def test_no_retry_on_live_skip(self):
        self.mock_fetch.return_value = [make_video("v1")]
        process = Mock(return_value=make_result(stage="download", error="正在直播"))
        self._cycle(process)
        process.assert_called_once()
        state = load_state(self.state_path)
        self.assertEqual(state["videos"]["v1"]["status"], STATUS_SKIPPED_LIVE)

    def test_uploaded_video_skipped(self):
        self.mock_fetch.return_value = [make_video("v1")]
        save_state(self.state_path, {"version": 1, "videos": {
            "v1": {"status": STATUS_UPLOADED, "bvid": "BV1"}}})
        process = Mock()
        self._cycle(process)
        process.assert_not_called()

    def test_title_filter(self):
        self.mock_fetch.return_value = [
            make_video("v1", title="Marvel SNAP Patch"),
            make_video("v2", title="Other Game News"),
        ]
        with patch.object(config, "TITLE_FILTER_KEYWORD", "snap"):
            process = Mock(return_value=make_result(success=True, bvid="BV1"))
            self._cycle(process)
        process.assert_called_once()
        called_url = process.call_args[0][0]
        self.assertIn("v1", called_url)


class FetchVideoQueueDetailsTests(unittest.TestCase):
    """fetch_video_queue_details：批量拉取时长/直播回放信息。"""

    def _mock_service(self, response):
        """youtube.videos().list(...) → request（execute() 返回 response）。"""
        request = Mock()
        request.execute.return_value = response
        service = Mock()
        service.videos.return_value.list.return_value = request
        return service, service.videos.return_value.list

    def test_empty_ids_returns_empty(self):
        with patch.object(monitor, "get_youtube_service") as mock_gs:
            result = monitor.fetch_video_queue_details(
                video_ids=[], client_secret_file=Path("x"), token_file=Path("y"))
        self.assertEqual(result, {})
        mock_gs.assert_not_called()

    def test_parses_duration_and_live_archive(self):
        response = {"items": [
            {"id": "v1",
             "contentDetails": {"duration": "PT1H2M3S"},
             "snippet": {"liveBroadcastContent": "none"},
             "liveStreamingDetails": {}},
            {"id": "v2",
             "contentDetails": {"duration": "PT30M"},
             "snippet": {"liveBroadcastContent": "live"},
             "liveStreamingDetails": {
                 "actualStartTime": "2026-01-01T00:00:00Z",
                 "actualEndTime": "2026-01-01T01:00:00Z"}},
        ]}
        service, list_mock = self._mock_service(response)
        with patch.object(monitor, "get_youtube_service", return_value=service):
            result = monitor.fetch_video_queue_details(
                video_ids=["v1", "v2"], client_secret_file=Path("x"), token_file=Path("y"))
        self.assertEqual(result["v1"]["duration_seconds"], 3723)
        self.assertFalse(result["v1"]["is_live_archive"])
        self.assertEqual(result["v1"]["live_broadcast_content"], "none")
        self.assertEqual(result["v2"]["duration_seconds"], 1800)
        self.assertTrue(result["v2"]["is_live_archive"])
        # 只请求了给定 id 列表
        called_kwargs = list_mock.call_args.kwargs
        self.assertEqual(called_kwargs["id"], "v1,v2")

    def test_chunked_requests_above_50_ids(self):
        """超过 50 个 id 时分批请求。"""
        service, list_mock = self._mock_service({"items": []})
        ids = [f"v{i}" for i in range(120)]
        with patch.object(monitor, "get_youtube_service", return_value=service):
            monitor.fetch_video_queue_details(
                video_ids=ids, client_secret_file=Path("x"), token_file=Path("y"))
        self.assertEqual(list_mock.call_count, 3)

    def test_missing_duration_field_is_zero(self):
        response = {"items": [{"id": "v1", "contentDetails": {},
                               "snippet": {}, "liveStreamingDetails": {}}]}
        service, _ = self._mock_service(response)
        with patch.object(monitor, "get_youtube_service", return_value=service):
            result = monitor.fetch_video_queue_details(
                video_ids=["v1"], client_secret_file=Path("x"), token_file=Path("y"))
        self.assertEqual(result["v1"]["duration_seconds"], 0)
        self.assertFalse(result["v1"]["is_live_archive"])


class DeferredCollectionsTests(unittest.TestCase):
    def test_try_deferred_collections_profile_mode(self):
        prof = SimpleNamespace(
            name="snap",
            bilibili=SimpleNamespace(sessdata="s", bili_jct="j"),
        )
        state = Path("state/snap/processed_videos.json")
        with patch("yt2bili.bilibili.auth.get_credential",
                   return_value="cred") as get_cred, \
             patch("yt2bili.bilibili.collection.process_pending_collections",
                   return_value=(1, 2, 0)) as sweep, \
             patch("yt2bili.profile.get_state_file_path",
                   return_value=state):
            monitor._try_deferred_collections(profile=prof)
        get_cred.assert_called_once_with(profile_name="snap")
        sweep.assert_called_once()
        self.assertEqual(sweep.call_args.kwargs["state_path"], state)

    def test_try_deferred_collections_unauthenticated_skips(self):
        prof = SimpleNamespace(
            name="snap",
            bilibili=SimpleNamespace(sessdata="", bili_jct=""),
        )
        with patch("yt2bili.bilibili.collection.process_pending_collections",
                   return_value=(0, 0, 0)) as sweep:
            monitor._try_deferred_collections(profile=prof)
        sweep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
