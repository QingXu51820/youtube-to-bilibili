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

from yt2bili.bilibili import subtitle as bsub


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

    def test_missing_file_dropped_from_queue(self):
        self._write_pending([
            {"bvid": "BV1", "aid": 1, "translated_path": str(Path(self.tmp.name) / "gone.srt")},
        ])
        self._patch_pipeline()
        self.assertEqual(bsub.upload_pending_subtitles(), 0)
        self.assertFalse(self.pending.exists())  # 永久失败条目被丢弃

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


if __name__ == "__main__":
    unittest.main()
