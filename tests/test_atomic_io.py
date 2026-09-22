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
    path_lock,
    read_json,
    read_text_with_retry,
    remove_best_effort,
    write_json,
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


class WriteJsonTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.path = self.dir / "pending_collections.json"

    def _leftovers(self):
        return sorted(p.name for p in self.dir.iterdir() if p.name != self.path.name)

    def test_writes_repo_style_json(self):
        """仓内状态文件的统一格式：不转义中文 + 2 空格缩进 + 结尾换行。"""
        write_json(self.path, [{"bvid": "BV1", "标题": "中文"}])
        self.assertEqual(
            self.path.read_text(encoding="utf-8"),
            '[\n  {\n    "bvid": "BV1",\n    "标题": "中文"\n  }\n]\n',
        )

    def test_creates_missing_parent_dir_and_leaves_no_temp(self):
        nested = self.dir / "state" / "snap" / "pending_subtitles.json"
        write_json(nested, {"a": 1})
        self.assertEqual(nested.read_text(encoding="utf-8"), '{\n  "a": 1\n}\n')
        self.assertEqual(sorted(self.dir.rglob("*.tmp")), [])

    def test_overwrites_previous_content(self):
        write_json(self.path, {"new": True})
        write_json(self.path, {"new": False})
        self.assertEqual(read_json(self.path), {"new": False})


class ReadJsonTests(unittest.TestCase):
    """队列/状态文件的读取：缺失、BOM、空文件、损坏都不该让长跑进程崩掉。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.path = self.dir / "processed_videos.json"

    def _backups(self):
        return sorted(p.name for p in self.dir.iterdir() if ".bak-" in p.name)

    def test_missing_file_returns_default(self):
        self.assertEqual(read_json(self.path, []), [])
        self.assertIsNone(read_json(self.path))

    def test_tolerates_bom(self):
        self.path.write_text('﻿{"a": 1}', encoding="utf-8")
        self.assertEqual(read_json(self.path), {"a": 1})

    def test_empty_file_is_not_corruption(self):
        """写了一半就退出的空文件 = 没有数据，不该留下备份。"""
        self.path.write_text("   \n", encoding="utf-8")
        self.assertEqual(read_json(self.path, [], backup_corrupt=True), [])
        self.assertEqual(self._backups(), [])

    def test_corrupt_file_returns_default_without_backup(self):
        self.path.write_text("{not json", encoding="utf-8")
        self.assertEqual(read_json(self.path, []), [])
        self.assertEqual(self._backups(), [])
        self.assertTrue(self.path.exists())

    def test_corrupt_file_is_backed_up_before_rebuild(self):
        self.path.write_text("{not json", encoding="utf-8")
        self.assertEqual(read_json(self.path, [], backup_corrupt=True), [])
        backups = self._backups()
        self.assertEqual(len(backups), 1)
        self.assertEqual(
            (self.dir / backups[0]).read_text(encoding="utf-8"), "{not json"
        )
        self.assertFalse(self.path.exists())

    def test_wrong_top_level_type_counts_as_corrupt(self):
        self.path.write_text('{"videos": {}}', encoding="utf-8")
        self.assertEqual(read_json(self.path, [], expect=list, backup_corrupt=True), [])
        self.assertEqual(len(self._backups()), 1)

    def test_matching_type_is_returned_unchanged(self):
        self.path.write_text('[{"bvid": "BV1"}]', encoding="utf-8")
        self.assertEqual(read_json(self.path, [], expect=list), [{"bvid": "BV1"}])
        self.assertEqual(self._backups(), [])

    def test_roundtrip_through_write_json(self):
        payload = {"videos": {"abc": {"status": "uploaded"}}, "版本": 2}
        write_json(self.path, payload)
        self.assertEqual(read_json(self.path, {}, expect=dict), payload)


class PathLockTests(unittest.TestCase):
    """回归：同一进程内多个写者读-改-写同一份队列文件时必须串行化。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def test_same_path_returns_same_lock(self):
        a = self.dir / "pending_subtitles.json"
        self.assertIs(path_lock(a), path_lock(a))

    def test_relative_and_absolute_spelling_share_one_lock(self):
        relative = self.dir / "pending_collections.json"
        self.assertIs(path_lock(relative), path_lock(Path(str(relative))))

    def test_different_paths_get_different_locks(self):
        self.assertIsNot(
            path_lock(self.dir / "a.json"), path_lock(self.dir / "b.json")
        )

    def test_lock_actually_serializes(self):
        lock = path_lock(self.dir / "pending_subtitles.json")
        with lock:
            self.assertFalse(path_lock(self.dir / "pending_subtitles.json").acquire(
                blocking=False
            ))
        self.assertTrue(lock.acquire(blocking=False))
        lock.release()

    def test_held_lock_blocks_another_thread(self):
        lock = path_lock(self.dir / "upload_log.json")
        entered = threading.Event()

        def worker():
            with lock:
                entered.set()

        with lock:
            thread = threading.Thread(target=worker)
            thread.start()
            self.assertFalse(entered.wait(0.1))   # 拿着锁时另一个线程进不来
        thread.join(2)
        self.assertTrue(entered.is_set())          # 释放后立刻进入


if __name__ == "__main__":
    unittest.main()
