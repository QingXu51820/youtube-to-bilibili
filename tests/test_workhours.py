"""自测：中国法定工作日 09:00-18:00 搬运时段门控。"""

import unittest
from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import patch

from yt2bili import workhours
from yt2bili.workhours import CHINA_TZ


def china_dt(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=CHINA_TZ)


class IsLegalWorkdayTests(unittest.TestCase):
    def test_fallback_monday_to_friday(self):
        """没有 chinesecalendar 时退化为周一至周五。"""
        with patch.object(workhours, "_HAS_CALENDAR", False), \
             patch.object(workhours, "_cc", None):
            self.assertTrue(workhours.is_legal_workday(date(2026, 8, 24)))  # Mon
            self.assertFalse(workhours.is_legal_workday(date(2026, 8, 29)))  # Sat
            self.assertFalse(workhours.is_legal_workday(date(2026, 8, 30)))  # Sun

    @unittest.skipUnless(workhours._HAS_CALENDAR, "chinesecalendar 未安装")
    def test_calendar_holiday(self):
        """国庆节（2026-10-01）是法定节假日，不算工作日。"""
        self.assertFalse(workhours.is_legal_workday(date(2026, 10, 1)))
        # 当日是周四，但属于节假日，必须被识别为休息日。
        self.assertEqual(date(2026, 10, 1).weekday(), 3)

    @unittest.skipUnless(workhours._HAS_CALENDAR, "chinesecalendar 未安装")
    def test_calendar_workday(self):
        self.assertTrue(workhours.is_legal_workday(date(2026, 8, 24)))


class WindowBoundaryTests(unittest.TestCase):
    def test_clock_window_boundaries(self):
        # 09:00 属于窗口内，18:00、08:59 属于窗口外。
        # 显式传入 9/18，避免依赖本机 .env 的 WORK_START/END_HOUR 配置。
        self.assertTrue(workhours.in_clock_window(china_dt(2026, 8, 26, 9, 0), 9, 18))
        self.assertTrue(workhours.in_clock_window(china_dt(2026, 8, 26, 17, 59), 9, 18))
        self.assertFalse(workhours.in_clock_window(china_dt(2026, 8, 26, 18, 0), 9, 18))
        self.assertFalse(workhours.in_clock_window(china_dt(2026, 8, 26, 8, 59), 9, 18))


class CheckGateTests(unittest.TestCase):
    def test_disabled_always_allowed(self):
        with patch.object(workhours, "gate_enabled", return_value=False):
            ok, reason = workhours.check(china_dt(2026, 8, 26, 20, 0))
        self.assertTrue(ok)
        self.assertEqual(reason, "")

    def test_off_hours_blocked(self):
        with patch.object(workhours, "gate_enabled", return_value=True), \
             patch.object(workhours, "is_legal_workday", return_value=True):
            ok, reason = workhours.check(china_dt(2026, 8, 26, 20, 0))
        self.assertFalse(ok)
        self.assertIn("已禁止搬运与翻译", reason)

    def test_rest_day_blocked_even_in_window(self):
        with patch.object(workhours, "gate_enabled", return_value=True), \
             patch.object(workhours, "is_legal_workday", return_value=False):
            ok, reason = workhours.check(china_dt(2026, 8, 29, 10, 0))
        self.assertFalse(ok)
        self.assertIn("休息日", reason)

    def test_within_window_allowed(self):
        with patch.object(workhours, "gate_enabled", return_value=True), \
             patch.object(workhours, "is_legal_workday", return_value=True):
            ok, reason = workhours.check(china_dt(2026, 8, 26, 10, 0))
        self.assertTrue(ok)
        self.assertEqual(reason, "")


class SecondsUntilNextWindowTests(unittest.TestCase):
    def test_gate_disabled_returns_zero(self):
        with patch.object(workhours, "gate_enabled", return_value=False):
            self.assertEqual(workhours.seconds_until_next_window(), 0.0)

    def test_currently_allowed_returns_zero(self):
        with patch.object(workhours, "gate_enabled", return_value=True), \
             patch.object(workhours, "check", return_value=(True, "")):
            self.assertEqual(workhours.seconds_until_next_window(), 0.0)

    def test_off_hours_returns_positive(self):
        with patch.object(workhours, "gate_enabled", return_value=True), \
             patch.object(workhours, "check", return_value=(False, "blocked")), \
             patch.object(workhours, "next_window_start", return_value=china_dt(2026, 8, 27, 9, 0)):
            secs = workhours.seconds_until_next_window(china_dt(2026, 8, 26, 20, 0))
        self.assertGreater(secs, 0)


class ProcessVideoGateTests(unittest.TestCase):
    """process_video 在非搬运时段必须在下载前直接拦截。"""

    def test_blocked_returns_before_download(self):
        from yt2bili import main as main_mod

        with patch.object(workhours, "check", return_value=(False, "当前不在 09:00-18:00 搬运时段，已禁止搬运与翻译")), \
             patch.object(main_mod, "download_video") as mock_download:
            rec = main_mod.process_video("https://youtu.be/abc")

        self.assertFalse(rec.success)
        self.assertEqual(rec.stage, "blocked_work_hours")
        self.assertIn("已禁止搬运与翻译", rec.error)
        mock_download.assert_not_called()


class MonitorSkipClassificationTests(unittest.TestCase):
    def test_is_work_hours_skip_result(self):
        from yt2bili.youtube.monitor import is_work_hours_skip_result

        self.assertTrue(is_work_hours_skip_result(SimpleNamespace(
            stage="blocked_work_hours", error="当前不在 09:00-18:00 搬运时段，已禁止搬运与翻译")))
        self.assertFalse(is_work_hours_skip_result(SimpleNamespace(
            stage="download", error="已禁止搬运与翻译")))
        self.assertFalse(is_work_hours_skip_result(SimpleNamespace(
            stage="blocked_work_hours", error="其他原因")))

    def test_record_failure_maps_to_deferred_skip(self):
        from yt2bili.youtube.monitor import STATUS_SKIPPED_WORK_HOURS, record_failure

        state = {"version": 1, "videos": {}}
        video = SimpleNamespace(
            video_id="v1", url="u", title="T", channel_title="C",
            published_at="2026-08-01T00:00:00Z",
        )
        record_failure(state, video, SimpleNamespace(
            success=False, stage="blocked_work_hours",
            error="当前不在 09:00-18:00 搬运时段，已禁止搬运与翻译",
            bvid="", aid=0, translated_title="", original_title="Orig",
        ))
        self.assertEqual(state["videos"]["v1"]["status"], STATUS_SKIPPED_WORK_HOURS)


class MonitorWindowWaitTests(unittest.TestCase):
    def test_gate_disabled_runs_immediately(self):
        from yt2bili.youtube import monitor

        with patch.object(workhours, "gate_enabled", return_value=False):
            self.assertTrue(monitor._respect_repost_windows(3600, once=False, dry_run=False))

    def test_window_open_runs_immediately(self):
        from yt2bili.youtube import monitor

        with patch.object(workhours, "gate_enabled", return_value=True), \
             patch.object(workhours, "seconds_until_next_window", return_value=0.0):
            self.assertTrue(monitor._respect_repost_windows(3600, once=False, dry_run=False))

    def test_once_blocked_stops(self):
        from yt2bili.youtube import monitor

        with patch.object(workhours, "gate_enabled", return_value=True), \
             patch.object(workhours, "seconds_until_next_window", return_value=7200.0):
            self.assertFalse(monitor._respect_repost_windows(3600, once=True, dry_run=False))

    def test_continuous_blocked_waits_until_window(self):
        from yt2bili.youtube import monitor

        with patch.object(workhours, "gate_enabled", return_value=True), \
             patch.object(workhours, "seconds_until_next_window", return_value=7200.0), \
             patch("yt2bili.youtube.monitor.time.sleep") as mock_sleep:
            allowed = monitor._respect_repost_windows(3600, once=False, dry_run=False)
        self.assertTrue(allowed)
        mock_sleep.assert_called_once()
        self.assertEqual(mock_sleep.call_args[0][0], 7200.0)

    def test_dry_run_never_blocked(self):
        from yt2bili.youtube import monitor

        with patch.object(workhours, "gate_enabled", return_value=True), \
             patch.object(workhours, "seconds_until_next_window", return_value=7200.0), \
             patch("yt2bili.youtube.monitor.time.sleep") as mock_sleep:
            self.assertTrue(monitor._respect_repost_windows(3600, once=False, dry_run=True))
        mock_sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
