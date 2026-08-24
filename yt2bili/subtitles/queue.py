"""Background subtitle-translation queue.

Subtitle translation (DeepSeek) is comparatively slow.  Processing it on a
single worker queue keeps the main pipeline from blocking on translation while
also avoiding a burst of concurrent DeepSeek calls when many videos are queued.
"""

from __future__ import annotations

import queue
import threading
from pathlib import Path

from yt2bili import config
from yt2bili.subtitles.translator import translate_cues
from yt2bili.subtitles.writer import write_srt
from yt2bili.subtitles.bilibili_format import clamp_cues_to_duration


class SubtitleTranslationJob:
    """A unit of subtitle translation work queued for background processing."""

    def __init__(self, video_id: str, cues: list, source_path: str) -> None:
        self.video_id = video_id
        self.cues = cues
        self.source_path = source_path

        self.duration: float = 0.0
        self.bvid: str = ""
        self.aid: str = ""

        self._duration_event = threading.Event()
        self._upload_event = threading.Event()
        self._finished_event = threading.Event()
        self._cancelled = False

        self.status = "queued"
        self.error: str | None = None
        self.translated_path: str | None = None

    def set_duration(self, duration: float) -> None:
        self.duration = duration
        self._duration_event.set()

    def set_upload(self, bvid: str, aid: str) -> None:
        self.bvid = bvid
        self.aid = aid
        self._upload_event.set()

    def cancel(self) -> None:
        self._cancelled = True
        self._duration_event.set()
        self._upload_event.set()

    def wait(self, timeout: float | None = None) -> None:
        self._finished_event.wait(timeout)

    def _wait_duration(self) -> None:
        self._duration_event.wait()

    def _wait_upload(self) -> None:
        self._upload_event.wait()


_translation_queue: queue.Queue = queue.Queue()
_worker_thread: threading.Thread | None = None
_worker_lock = threading.Lock()


def _run_job(job: SubtitleTranslationJob) -> None:
    try:
        if job._cancelled:
            job.status = "cancelled"
            return

        translated = translate_cues(job.cues, batch_size=config.SUBTITLE_TRANSLATE_BATCH_SIZE)
        if not translated:
            raise RuntimeError("翻译后字幕为空")

        job._wait_duration()
        if job._cancelled:
            job.status = "cancelled"
            return

        if job.duration and job.duration > 0:
            translated, dropped_n, clamped_n = clamp_cues_to_duration(
                translated, job.duration, margin=0.5
            )
            if dropped_n:
                print(f"[字幕] 已移除 {dropped_n} 条超出视频时长的字幕")
            if clamped_n:
                print(f"[字幕] 已修正 {clamped_n} 条字幕的结束时间（不超过视频时长）")

        subtitle_dir = Path(config.SUBTITLE_DIR)
        subtitle_dir.mkdir(parents=True, exist_ok=True)
        translated_filename = f"{job.video_id}.{config.SUBTITLE_TARGET_LANG}.srt"
        translated_path = str(subtitle_dir / translated_filename)
        write_srt(translated, translated_path)
        job.translated_path = translated_path
        print(f"[字幕] 翻译完成: {len(translated)} 条字幕")
        print(f"[字幕] 已保存: {translated_filename}")

        if not config.SUBTITLE_UPLOAD_TO_BILIBILI:
            job.status = "success"
            return

        job._wait_upload()
        if job._cancelled:
            job.status = "cancelled"
            return

        if job.bvid and job.aid:
            from yt2bili.bilibili.subtitle import save_pending_subtitle

            save_pending_subtitle(bvid=job.bvid, aid=job.aid, translated_path=translated_path)
            job.status = "pending_upload"
            print("[字幕] 已加入延迟上传队列，等待 B站 CID 就绪后自动上传")
        else:
            job.status = "skipped_upload"
            print("[字幕] 已生成翻译字幕，未上传到 B站")
    except Exception as e:
        job.error = str(e)
        job.status = "failed"
        print(f"[字幕] [WARN] 字幕翻译失败: {e}")
    finally:
        job._finished_event.set()


def _ensure_worker() -> None:
    global _worker_thread
    with _worker_lock:
        if _worker_thread is not None and _worker_thread.is_alive():
            return

        def _worker_loop() -> None:
            while True:
                job = _translation_queue.get()
                try:
                    _run_job(job)
                finally:
                    _translation_queue.task_done()

        _worker_thread = threading.Thread(
            target=_worker_loop,
            name="subtitle-translation-worker",
            daemon=True,
        )
        _worker_thread.start()


def enqueue_translation(video_id: str, cues: list, source_path: str) -> SubtitleTranslationJob:
    """Enqueue subtitle translation work and return a handle to its result."""
    job = SubtitleTranslationJob(video_id=video_id, cues=cues, source_path=source_path)
    _ensure_worker()
    _translation_queue.put(job)
    return job
