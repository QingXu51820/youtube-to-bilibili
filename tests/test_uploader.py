"""自测：B站上传器（描述构建、封面兜底、凭据缺失保护）+ B站字幕 API。"""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from yt2bili import config
from yt2bili.bilibili import subtitle as bsub
from yt2bili.bilibili.uploader import (
    UploadResult,
    _build_description,
    _ensure_cover,
    _make_minimal_jpeg,
    _upload_async,
    is_valid_image,
    upload_video,
)


class BuildDescriptionTests(unittest.TestCase):
    def test_header_only_when_no_description(self):
        result = _build_description("", "翻译标题", "原标题")
        self.assertEqual(result, "原标题: 原标题\n翻译标题: 翻译标题")

    def test_header_without_original_title(self):
        result = _build_description("", "翻译标题")
        self.assertEqual(result, "翻译标题: 翻译标题")

    def test_whole_description_fits(self):
        desc = "Line one\nLine two"
        result = _build_description(desc, "译", "原")
        self.assertEqual(
            result, "原标题: 原\n翻译标题: 译\n\n原视频简介:\nLine one\nLine two"
        )

    def test_byte_budget_respected_with_cjk(self):
        """CJK 每字 3 字节 —— 总输出不得超过 2000 字节。"""
        long_line = "中" * 900  # 2700 字节，单行超预算
        desc = long_line + "\n短行"
        result = _build_description(desc, "译", "原")
        self.assertLessEqual(len(result.encode("utf-8")), 2000)
        self.assertNotIn(long_line, result)  # 放不下就整行省略

    def test_truncation_suffix_when_partial(self):
        desc = "line1\nline2\nline3\nline4\nline5\n" + "padding " * 500
        result = _build_description(desc, "译", "原")
        self.assertIn("...", result)
        # 截断后仍是完整行：最后一行要么是原输入行，要么是 "..." 后缀
        body = result.split("原视频简介:\n", 1)[1].rstrip()
        lines = body.split("\n")
        last_line = lines[-1]
        if last_line != "...":
            self.assertIn(last_line, desc.split("\n"))

    def test_desc_section_omitted_if_nothing_fits(self):
        desc = "x" * 3000  # 单行就超预算
        result = _build_description(desc, "译", "原")
        self.assertNotIn("原视频简介", result)
        self.assertNotIn("...", result)

    def test_rare_header_over_budget_returns_prefix(self):
        huge_title = "中" * 2000
        result = _build_description("desc", huge_title, "原")
        self.assertTrue(result.startswith("原标题: 原"))


class EnsureCoverTests(unittest.TestCase):
    def test_valid_cover_returns_as_is(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "cover.jpg"
            from PIL import Image
            Image.new("RGB", (10, 10)).save(p)
            self.assertEqual(_ensure_cover(str(p)), str(p))

    def test_invalid_cover_creates_placeholder(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "bad.jpg"
            p.write_bytes(b"garbage")
            result = _ensure_cover(str(p))
        self.assertTrue(Path(result).exists())
        self.assertTrue(is_valid_image(result))

    def test_none_cover_creates_placeholder(self):
        result = _ensure_cover(None)
        self.assertTrue(Path(result).exists())

    def test_minimal_jpeg_is_valid(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "min.jpg"
            p.write_bytes(_make_minimal_jpeg())
            self.assertTrue(is_valid_image(p))
        self.assertTrue(_make_minimal_jpeg().startswith(b"\xff\xd8"))
        self.assertTrue(_make_minimal_jpeg().endswith(b"\xff\xd9"))


class UploadCredentialGuardTests(unittest.TestCase):
    def test_upload_async_without_credential_raises(self):
        """回归：凭据构建必须在同步上下文完成，_upload_async 内部禁止再建。"""
        async def _run():
            return await _upload_async(["a.mp4"], "title")

        with self.assertRaises(RuntimeError) as ctx:
            asyncio.run(_run())
        self.assertIn("凭据缺失", str(ctx.exception))

    def test_upload_video_empty_files_returns_early(self):
        result = upload_video([], "title")
        self.assertFalse(result.success)
        self.assertEqual(result.message, "没有视频文件可上传")

    def test_upload_video_empty_string(self):
        result = upload_video("", "title")
        self.assertFalse(result.success)


class BilibiliSubtitleApiTests(unittest.TestCase):
    def test_check_response_auth_error(self):
        resp = SimpleNamespace(status_code=401, json=lambda: {})
        with self.assertRaises(RuntimeError) as ctx:
            bsub._check_response(resp)
        self.assertIn("重新扫码登录", str(ctx.exception))

    def test_check_response_nonzero_code_with_detail(self):
        resp = SimpleNamespace(
            status_code=200,
            json=lambda: {"code": -400, "message": "bad",
                          "data": [{"line": 3, "error_msg": "时间超长"}]},
        )
        with self.assertRaises(RuntimeError) as ctx:
            bsub._check_response(resp, "submit_subtitle")
        self.assertIn("L3: 时间超长", str(ctx.exception))

    def test_check_response_ok(self):
        resp = SimpleNamespace(status_code=200, json=lambda: {"code": 0, "data": {}})
        self.assertEqual(bsub._check_response(resp), {"code": 0, "data": {}})

    def test_submit_subtitle_without_sessdata_raises(self):
        with patch.object(config, "BILI_SESSDATA", ""), \
             patch.object(config, "BILI_BILI_JCT", ""):
            with self.assertRaises(RuntimeError) as ctx:
                bsub.submit_subtitle("BV1", 1, {"body": []})
        self.assertIn("SESSDATA", str(ctx.exception))

    def test_save_pending_subtitle_merges_and_dedups(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pending.json"
            with patch.object(bsub, "_pending_subtitles_path", return_value=path):
                bsub.save_pending_subtitle("BV1", 1, "/tmp/a.srt")
                bsub.save_pending_subtitle("BV1", 2, "/tmp/b.srt")  # 同 bvid 覆盖
                bsub.save_pending_subtitle("BV2", 3, "/tmp/c.srt")
                entries = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(len(entries), 2)
        by_bvid = {e["bvid"]: e for e in entries}
        self.assertEqual(by_bvid["BV1"]["aid"], 2)
        self.assertEqual(by_bvid["BV1"]["translated_path"], "/tmp/b.srt")
        self.assertIn("added_at", entries[0])

    def test_cleanup_subtitle_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "abc123.zh-CN.srt").write_text("x", encoding="utf-8")
            (d / "abc123.en.srt").write_text("x", encoding="utf-8")
            (d / "other.srt").write_text("x", encoding="utf-8")
            with patch.object(config, "CLEANUP_AFTER_UPLOAD", True):
                bsub._cleanup_subtitle_files(str(d / "abc123.zh-CN.srt"))
            remaining = sorted(p.name for p in d.glob("*.srt"))
        self.assertEqual(remaining, ["other.srt"])

    def test_cleanup_disabled_keeps_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "abc123.zh-CN.srt").write_text("x", encoding="utf-8")
            with patch.object(config, "CLEANUP_AFTER_UPLOAD", False):
                bsub._cleanup_subtitle_files(str(d / "abc123.zh-CN.srt"))
            self.assertTrue((d / "abc123.zh-CN.srt").exists())


if __name__ == "__main__":
    unittest.main()
