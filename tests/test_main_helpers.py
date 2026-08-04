"""自测：main.py 辅助逻辑（run 报告、清理、dry-run 守卫、monitor 参数接线）。"""

import json
import os
import re
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from yt2bili import config
from yt2bili import main as main_mod
from yt2bili.main import ProcessResult, _cleanup_old_runs, _write_run_report


def make_result(url="https://youtu.be/abc", success=True, stage="complete"):
    return ProcessResult(
        url=url, success=success, stage=stage,
        bvid="BV1" if success else "",
        original_title="T", translated_title="译",
    )


class WriteRunReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.runs = Path(self.tmp.name) / "runs"
        patcher = patch.object(config, "RUNS_DIR", str(self.runs))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_writes_latest_and_timestamped(self):
        report = _write_run_report([make_result(), make_result(url="u2", success=False)])
        self.assertTrue(report.exists())
        latest = self.runs / "latest.json"
        self.assertTrue(latest.exists())
        data = json.loads(latest.read_text(encoding="utf-8"))
        self.assertEqual(data["total"], 2)
        self.assertEqual(data["success"], 1)
        self.assertEqual(data["failed"], 1)
        self.assertEqual(data["results"][0]["bvid"], "BV1")

    def test_filename_has_millisecond_precision(self):
        """回归：同一秒两次批处理不得互相覆盖。"""
        r1 = _write_run_report([make_result()])
        r2 = _write_run_report([make_result()])
        self.assertNotEqual(r1.name, r2.name)
        self.assertRegex(r1.name, r"^\d{8}-\d{6}-\d{6}\.json$")
        self.assertRegex(r2.name, r"^\d{8}-\d{6}-\d{6}\.json$")
        # 文件名必须按秒毫秒可排序（与 strptime 无关）
        self.assertLess(r1.name, r2.name)

    def test_cleanup_removes_only_old_reports(self):
        (self.runs).mkdir(parents=True, exist_ok=True)
        old = self.runs / "20200101-000000-000000.json"
        new = self.runs / "20260804-000000-000000.json"
        latest = self.runs / "latest.json"
        for f in (old, new, latest):
            f.write_text("{}", encoding="utf-8")
        past = time.time() - 400 * 86400  # 400 天前
        future = time.time() + 100 * 86400
        os.utime(old, (past, past))
        os.utime(new, (time.time(), time.time()))
        os.utime(latest, (past, past))  # latest.json 永不清理

        deleted = _cleanup_old_runs(self.runs, keep_days=90)

        self.assertEqual(deleted, 1)
        self.assertFalse(old.exists())
        self.assertTrue(new.exists())
        self.assertTrue(latest.exists())

    def test_cleanup_missing_dir(self):
        self.assertEqual(_cleanup_old_runs(self.runs / "missing", keep_days=90), 0)


class DryRunGuardTests(unittest.TestCase):
    """回归：--dry-run 不带 --monitor 必须拒绝执行，不能真实下载上传。"""

    def test_dry_run_without_monitor_returns_zero(self):
        with patch.object(main_mod, "_check_external_tools"), \
             patch.object(main_mod, "_cleanup_old_runs"), \
             patch.object(main_mod, "setup_profile"), \
             patch.object(main_mod, "process_video") as process:
            with patch.object(sys, "argv", ["main.py", "--dry-run"]):
                code = main_mod.main()
        self.assertEqual(code, 0)
        process.assert_not_called()


class MonitorArgWiringTests(unittest.TestCase):
    """--monitor --once --dry-run 的参数接线（monitor_source 回退、dry_run 传递）。"""

    def test_monitor_dry_run_wires_params(self):
        with patch.object(main_mod, "_check_external_tools"), \
             patch.object(main_mod, "_cleanup_old_runs"), \
             patch.object(main_mod, "setup_profile"), \
             patch("yt2bili.youtube.monitor.run_monitor_loop", return_value=0) as loop, \
             patch.object(config, "YOUTUBE_MONITOR_SOURCE", "rss"):
            with patch.object(sys, "argv",
                              ["main.py", "--monitor", "--once", "--dry-run"]):
                code = main_mod.main()
        self.assertEqual(code, 0)
        self.assertEqual(loop.call_count, 1)
        kwargs = loop.call_args.kwargs
        self.assertTrue(kwargs["dry_run"])
        self.assertIsNone(kwargs["write_run_report"])
        self.assertEqual(kwargs["source"], "rss")  # .env 回退
        self.assertTrue(kwargs["once"])


if __name__ == "__main__":
    unittest.main()
