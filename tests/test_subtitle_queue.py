"""自测：字幕后台翻译队列 —— 复用已翻译 SRT 跳过重译、延迟上传记账。"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from yt2bili import config
from yt2bili.subtitles import queue as squeue


def _make_job(*, reuse_path=None, video_id="vid123"):
    job = squeue.SubtitleTranslationJob(
        video_id=video_id,
        cues=[],
        source_path="",
        reuse_path=reuse_path,
    )
    job.set_duration(60.0)
    return job


class ReuseTranslationTests(unittest.TestCase):
    def test_reuse_skips_translation_and_enqueues_upload(self):
        """复用路径：不调用 DeepSeek，不重写文件，只入延迟上传队列。"""
        with tempfile.TemporaryDirectory() as tmp:
            existing = Path(tmp) / "vid123.zh-CN.srt"
            existing.write_text("existing content", encoding="utf-8")

            job = _make_job(reuse_path=str(existing))
            job.set_upload("BV1xxxx", 12345)

            with patch("yt2bili.subtitles.queue.translate_cues") as mock_translate, \
                 patch("yt2bili.subtitles.queue.write_srt") as mock_write, \
                 patch("yt2bili.bilibili.subtitle.save_pending_subtitle") as mock_save:
                squeue._run_job(job)

            mock_translate.assert_not_called()
            mock_write.assert_not_called()
            mock_save.assert_called_once_with(
                bvid="BV1xxxx", aid=12345, translated_path=str(existing)
            )
            self.assertEqual(job.status, "pending_upload")
            self.assertEqual(job.translated_path, str(existing))

    def test_reuse_without_upload_target_says_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            existing = Path(tmp) / "vid123.zh-CN.srt"
            existing.write_text("x", encoding="utf-8")

            # Upload succeeded but carried no bvid/aid (e.g. no credential) →
            # the wait resolves and the job reports skipped_upload.
            job = _make_job(reuse_path=str(existing))
            job.set_upload("", 0)
            with patch("yt2bili.subtitles.queue.translate_cues") as mock_translate, \
                 patch("yt2bili.bilibili.subtitle.save_pending_subtitle") as mock_save:
                squeue._run_job(job)

            mock_translate.assert_not_called()
            mock_save.assert_not_called()
            self.assertEqual(job.status, "skipped_upload")

    def test_reuse_disabled_upload_flag_is_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            existing = Path(tmp) / "vid123.zh-CN.srt"
            existing.write_text("x", encoding="utf-8")

            job = _make_job(reuse_path=str(existing))
            with patch.object(config, "SUBTITLE_UPLOAD_TO_BILIBILI", False), \
                 patch("yt2bili.subtitles.queue.translate_cues") as mock_translate, \
                 patch("yt2bili.bilibili.subtitle.save_pending_subtitle") as mock_save:
                squeue._run_job(job)

            mock_translate.assert_not_called()
            mock_save.assert_not_called()
            self.assertEqual(job.status, "success")

    def test_reuse_missing_file_fails(self):
        job = _make_job(reuse_path="N:/no/such/vid123.zh-CN.srt")
        job.set_upload("BV1xxxx", 1)
        with patch("yt2bili.subtitles.queue.translate_cues") as mock_translate:
            squeue._run_job(job)
        mock_translate.assert_not_called()
        self.assertEqual(job.status, "failed")
        self.assertIn("不存在", job.error)


class FindExistingTranslationTests(unittest.TestCase):
    def test_finds_existing_translated_srt(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(config, "SUBTITLE_DIR", tmp), \
                 patch.object(config, "SUBTITLE_TARGET_LANG", "zh-CN"):
                Path(tmp, "vid123.zh-CN.srt").write_text("x", encoding="utf-8")
                found = squeue.find_existing_translation("vid123")
                self.assertEqual(Path(found).name, "vid123.zh-CN.srt")

    def test_absent_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(config, "SUBTITLE_DIR", tmp):
                self.assertIsNone(squeue.find_existing_translation("vid123"))

    def test_other_lang_file_does_not_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(config, "SUBTITLE_DIR", tmp), \
                 patch.object(config, "SUBTITLE_TARGET_LANG", "zh-CN"):
                Path(tmp, "vid123.en.srt").write_text("x", encoding="utf-8")
                self.assertIsNone(squeue.find_existing_translation("vid123"))


if __name__ == "__main__":
    unittest.main()
