"""自测：Windows 友好的原子写入。

回归背景：`--profile A` / `--profile B` 两个监控进程共用同一个
``config/youtube_token.json``。Windows 上 ``os.replace``（MoveFileEx）在目标文件
被任何其他进程打开时都会失败（WinError 32 / 5），CPython 的 ``open()`` 不共享
删除权限，所以「另一个进程正在读 token」就足以让写回失败；旧实现还固定使用
``<name>.tmp``，两个进程会互相搬走对方的临时文件。
"""

import errno
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from yt2bili.atomic_io import (
    atomic_write_text,
    read_text_with_retry,
    remove_best_effort,
)

WINDOWS = sys.platform == "win32"


class AtomicWriteTextTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.path = self.dir / "youtube_token.json"
        self.path.write_text("old", encoding="utf-8")

    def _leftovers(self):
        """目标文件之外还留在目录里的东西（临时文件泄漏 = 这里非空）。"""
        return sorted(p.name for p in self.dir.iterdir() if p.name != self.path.name)

    def test_writes_content_and_leaves_no_temp_file(self):
        atomic_write_text(self.path, "new")
        self.assertEqual(self.path.read_text(encoding="utf-8"), "new")
        self.assertEqual(self._leftovers(), [])

    def test_creates_missing_parent_dir(self):
        nested = self.dir / "nested" / "youtube_token.json"
        atomic_write_text(nested, "x")
        self.assertEqual(nested.read_text(encoding="utf-8"), "x")

    def test_does_not_consume_another_processes_temp_file(self):
        """回归：固定 ``<name>.tmp`` 名称会让并发进程互相顶掉临时文件。"""
        foreign = self.dir / "youtube_token.json.tmp"
        foreign.write_text("other-process", encoding="utf-8")

        atomic_write_text(self.path, "new")

        self.assertEqual(self.path.read_text(encoding="utf-8"), "new")
        self.assertEqual(foreign.read_text(encoding="utf-8"), "other-process")
        self.assertEqual(self._leftovers(), ["youtube_token.json.tmp"])

    def test_retries_rename_after_sharing_violation(self):
        """回归：另一个进程短暂占用目标文件时应重试而不是直接抛出。"""
        real_replace = os.replace
        attempts = []

        def flaky_replace(src, dst, *args, **kwargs):
            attempts.append(dst)
            if len(attempts) < 3:
                raise PermissionError(
                    errno.EACCES, "另一个程序正在使用此文件", str(src), str(dst)
                )
            return real_replace(src, dst, *args, **kwargs)

        with patch("yt2bili.atomic_io.os.replace", side_effect=flaky_replace):
            atomic_write_text(self.path, "new", retry_delay=0)

        self.assertEqual(len(attempts), 3)
        self.assertEqual(self.path.read_text(encoding="utf-8"), "new")
        self.assertEqual(self._leftovers(), [])

    def test_gives_up_after_persistent_lock_and_keeps_old_content(self):
        def always_locked(src, dst, *args, **kwargs):
            raise PermissionError(
                errno.EACCES, "另一个程序正在使用此文件", str(src), str(dst)
            )

        with patch("yt2bili.atomic_io.os.replace", side_effect=always_locked) as rep:
            with self.assertRaises(PermissionError):
                atomic_write_text(self.path, "new", attempts=3, retry_delay=0)

        self.assertEqual(rep.call_count, 3)
        self.assertEqual(self.path.read_text(encoding="utf-8"), "old")
        self.assertEqual(self._leftovers(), [])

    def test_unrelated_oserror_is_not_retried(self):
        def broken(src, dst, *args, **kwargs):
            raise OSError(errno.ENOSPC, "no space left on device")

        with patch("yt2bili.atomic_io.os.replace", side_effect=broken) as rep:
            with self.assertRaises(OSError):
                atomic_write_text(self.path, "new", attempts=5, retry_delay=0)

        self.assertEqual(rep.call_count, 1)
        self.assertEqual(self.path.read_text(encoding="utf-8"), "old")
        self.assertEqual(self._leftovers(), [])

    @unittest.skipUnless(WINDOWS, "Windows 独占的分享语义")
    def test_real_locked_destination_succeeds_once_released(self):
        handle = open(self.path, "rb")
        timer = threading.Timer(0.3, handle.close)
        timer.start()
        try:
            atomic_write_text(self.path, "new", retry_delay=0.05)
        finally:
            timer.cancel()
            handle.close()
        self.assertEqual(self.path.read_text(encoding="utf-8"), "new")

    @unittest.skipUnless(WINDOWS, "Windows 独占的分享语义")
    def test_real_locked_destination_raises_and_keeps_old_content(self):
        with open(self.path, "rb"):
            with self.assertRaises(OSError):
                atomic_write_text(self.path, "new", attempts=2, retry_delay=0)
        self.assertEqual(self.path.read_text(encoding="utf-8"), "old")
        self.assertEqual(self._leftovers(), [])


class RemoveBestEffortTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "youtube_token.json"
        self.path.write_text("old", encoding="utf-8")

    def test_removes_existing_file(self):
        self.assertTrue(remove_best_effort(self.path))
        self.assertFalse(self.path.exists())

    def test_already_missing_file_is_not_an_error(self):
        self.path.unlink()
        self.assertTrue(remove_best_effort(self.path))

    def test_returns_false_when_file_stays_locked(self):
        with patch(
            "yt2bili.atomic_io.os.unlink",
            side_effect=PermissionError(errno.EACCES, "另一个程序正在使用此文件"),
        ):
            self.assertFalse(remove_best_effort(self.path, attempts=2, retry_delay=0))
        self.assertTrue(self.path.exists())

    @unittest.skipUnless(WINDOWS, "Windows 独占的分享语义")
    def test_real_locked_file(self):
        with open(self.path, "rb"):
            self.assertFalse(remove_best_effort(self.path, attempts=2, retry_delay=0))
        self.assertTrue(self.path.exists())


class ReadTextWithRetryTests(unittest.TestCase):
    """回归：另一个进程正在替换文件时，读取也会撞上 WinError 32/5。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "youtube_token.json"
        self.path.write_text("content", encoding="utf-8")

    def test_returns_content(self):
        self.assertEqual(read_text_with_retry(self.path), "content")

    def test_missing_file_raises_file_not_found(self):
        with self.assertRaises(FileNotFoundError):
            read_text_with_retry(self.path.with_name("nope.json"))

    def test_retries_after_sharing_violation(self):
        real_read = Path.read_text
        attempts = []

        def flaky_read(self, *args, **kwargs):
            attempts.append(self)
            if len(attempts) < 3:
                raise PermissionError(errno.EACCES, "另一个程序正在使用此文件")
            return real_read(self, *args, **kwargs)

        with patch("pathlib.Path.read_text", flaky_read):
            self.assertEqual(read_text_with_retry(self.path, retry_delay=0), "content")
        self.assertEqual(len(attempts), 3)

    def test_gives_up_after_persistent_lock(self):
        with patch(
            "pathlib.Path.read_text",
            side_effect=PermissionError(errno.EACCES, "另一个程序正在使用此文件"),
        ) as rep:
            with self.assertRaises(PermissionError):
                read_text_with_retry(self.path, attempts=3, retry_delay=0)
        self.assertEqual(rep.call_count, 3)

    def test_unrelated_oserror_is_not_retried(self):
        with patch(
            "pathlib.Path.read_text", side_effect=OSError(errno.EIO, "io error")
        ) as rep:
            with self.assertRaises(OSError):
                read_text_with_retry(self.path, attempts=5, retry_delay=0)
        self.assertEqual(rep.call_count, 1)


if __name__ == "__main__":
    unittest.main()
