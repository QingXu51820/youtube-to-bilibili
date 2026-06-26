# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Identity

yt2bili — YouTube → Bilibili automated repost pipeline. Downloads YouTube videos (≤1080p MP4), translates titles to Chinese, generates 1920×1080 cover images, and uploads to Bilibili as 转载 (repost, copyright=2). Also supports polling YouTube subscriptions for automatic ingestion.

## Essential Commands

```bash
# Single video
python main.py "https://www.youtube.com/watch?v=xxxxx"

# Batch from file
python main.py --file config/urls.txt

# Subscription monitor (continuous polling)
python main.py --monitor

# Monitor once, dry-run (check what's new without downloading)
python main.py --monitor --once --dry-run

# QR code re-login to Bilibili
python main.py --login

# Refresh YouTube cookies from browser
python main.py --refresh-youtube-cookies

# Fetch YouTube subscription list (standalone)
python youtube_subscriptions.py --source api --limit 50
python youtube_subscriptions.py --source rss --limit 50

# Build Windows EXE
tools\build_exe.bat
```

**No tests exist** — verify changes by running `python main.py <url>` end-to-end or `python -c "import <module>"` for syntax checks.

## Configuration

All settings in `config/.env` (copy from `config/.env.example`). Read by `yt2bili/config.py` via `python-dotenv`. Key groups:

- **Bilibili credentials**: `BILI_SESSDATA`, `BILI_BILI_JCT` — auto-populated by QR login
- **Translation**: `TRANSLATE_PROVIDER` (deepseek/openai/google), API keys, model selection, `TRANSLATION_PRESERVE_TERMS`
- **Upload**: `DEFAULT_TID` (Bilibili zone ID), `DEFAULT_TAGS`
- **Download**: `MAX_HEIGHT` (1080), `DOWNLOAD_MIN_SPEED_KIB`, `CLEANUP_AFTER_UPLOAD`
- **Monitor**: `YOUTUBE_MONITOR_INTERVAL_SECONDS`, `YOUTUBE_MONITOR_SOURCE` (api/rss)
- **Splitting**: `MAX_VIDEO_DURATION_SECONDS` (36000 = 10h, Bilibili's limit)

## Architecture

### Pipeline (single video, `yt2bili/main.py:process_video`)

```
Download → Split (if >10h) → Translate title → Prepare cover → Upload → Cleanup
```

Each stage is a separate `try/except` block. Failure at any stage records the error and returns immediately — batch processing continues.

### Module Map

| Module | Role |
|---|---|
| `main.py`, `youtube_subscriptions.py` | Root compatibility CLI wrappers |
| `yt2bili/main.py` | CLI entry, arg parsing, pipeline orchestrator, run reports (`runs/`) |
| `yt2bili/config.py` | `.env` reader with typed `_get()`/`_get_int()`, `validate()` checks credentials |
| `yt2bili/youtube/downloader.py` | yt-dlp Python API, cookie fallback chain, slow-speed detection + restart, ffprobe probing |
| `yt2bili/translation/translator.py` | `BaseTranslator` → `GoogleTranslator` / `OpenAITranslator` / `DeepSeekTranslator`, term preservation, 80-char truncation |
| `yt2bili/media/cover.py` | Pillow-based: validate → EXIF transpose → crop or contain → resize to 1920×1080 JPEG |
| `yt2bili/bilibili/uploader.py` | Async `bilibili-api-python` wrapped synchronously, multi-part (分P) support, fallback 1×1 JPEG cover |
| `yt2bili/bilibili/auth.py` | Bilibili QR login flow, auto-saves credentials to `config/.env` |
| `yt2bili/media/video_splitter.py` | ffmpeg `-c copy` lossless segmenting at keyframes |
| `yt2bili/youtube/monitor.py` | Polling loop: fetch subs → deduplicate → sort queue → process → retry → persist state |
| `yt2bili/youtube/subscriptions.py` | Standalone sub fetcher (API + RSS), custom `YouTubeClient` (requests-based, avoids httplib2 proxy issues) |
| `yt2bili/frozen_paths.py` | `is_frozen()` + `user_data_dir()` — EXE-relative paths when PyInstaller-bundled, project root in dev |

### Monitor State Flow

```
state/processed_videos.json      ← persisted per-video status (uploaded/failed/skipped_live/skipped_long)
runs/*.json                      ← historical batch reports, seeded into state to avoid re-processing
config/subscriptions_cache.json  ← cached channel list for RSS mode
```

## Key Patterns & Gotchas

### Cookie Fallback Chain (`yt2bili/youtube/downloader.py:_with_yt_dlp_cookies`)

1. Try `config/cookies.txt` → if bot-detected, auto-refresh and retry
2. Try each browser in `YOUTUBE_COOKIES_FROM_BROWSER` (chrome, edge, firefox)
3. Fall back to bare yt-dlp (no cookies)
4. Wrap bot-detection errors with Chinese-language hint about browser login

### Retry Strategy (multi-layered)

| Layer | Config keys | Default |
|---|---|---|
| YouTube API HTTP | `YOUTUBE_API_MAX_RETRIES`, `_RETRY_DELAY` | 3 retries, 2s base |
| Per-video processing | `YOUTUBE_VIDEO_RETRY_MAX`, `_RETRY_DELAY` | 2 retries, 30s base |
| Monitor cycle | `YOUTUBE_MONITOR_MAX_RETRIES`, `_RETRY_DELAY` | 5 retries, 30s base |

Only `download`, `split`, `upload` stages are retryable. Translation and cover failures are fatal for that video.

### Path Resolution

Always use `config.PROJECT_ROOT` (set by `frozen_paths.user_data_dir()`):
- **Dev mode**: project root (the parent of the `yt2bili/` package)
- **Frozen EXE**: directory next to `yt2bili.exe`

`yt2bili/youtube/monitor.py` has its own `project_path()` helper that resolves relative paths against `config.PROJECT_ROOT`.

### Config Access

Access config values via `config.KEY` directly (not `os.getenv`). The `yt2bili/config.py` module sets defaults at import time. Do not use `os.getenv` — it bypasses runtime modifications (e.g., `--no-speed-protection` sets `config.DOWNLOAD_MIN_SPEED_KIB = 0`).

### Translation: `source_lang` Parameter

The `source_lang` parameter in `translate()` is **only used by `GoogleTranslator`**. OpenAI and DeepSeek translators rely on the system prompt to auto-detect the source language and ignore this parameter entirely.

### Upload: Async-Sync Bridge

`yt2bili/bilibili/uploader.py:upload_video()` handles two scenarios:
1. No event loop running → `asyncio.run()`
2. Event loop already running (Jupyter) → `nest_asyncio.apply()` + `loop.run_until_complete()`

### Video Splitting

Triggered when `video.duration > config.MAX_VIDEO_DURATION_SECONDS` (10h). Uses ffmpeg `-c copy` for lossless keyframe-based splitting. Output files named `_P001.mp4`, `_P002.mp4`, etc. Uploaded as Bilibili 分P (multi-part).

### Frozen EXE Stderr

`yt2bili/youtube/downloader.py:_with_stderr_suppressed()` skips fd-2 manipulation when `is_frozen()` returns True — PyInstaller's bootloader breaks on `os.close(2)`.

## PyInstaller Build

Spec file: `packaging/yt2bili.spec`. Critical: dynamically collects yt-dlp extractor submodules and lists hidden imports for all major libraries. Excludes tkinter, matplotlib, scipy, numpy, unittest, test, pydoc to reduce EXE size (~150-200 MB).
