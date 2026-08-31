# AGENTS.md

This file provides guidance to Codex (Codex.ai/code) when working with code in this repository.

## Project Identity

yt2bili — YouTube → Bilibili automated repost pipeline. Downloads YouTube videos (≤1080p MP4), translates titles to Chinese, generates 1920×1080 cover images, and uploads to Bilibili as 转载 (repost, copyright=2). Also supports polling YouTube subscriptions for automatic ingestion, and multi-account Bilibili profiles.

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

# ── Multi-account (profiles) ──────────────────────────────────
# List configured profiles
python main.py --list-profiles

# Login to a specific profile
python main.py --login --profile snap

# Process a video with a specific profile
python main.py --profile snap "https://youtube.com/watch?v=xxx"

# Monitor specific profile's channels (RSS-based, no API quota)
python main.py --monitor --profile snap

# Monitor all profiles in round-robin sequence
python main.py --monitor --all-profiles --once

# Resolve YouTube @handle to channel ID
python main.py --resolve-channel "@MarvelSnap"

# Build Windows EXE
tools\build_exe.bat
```

# ── Self-test (tests/) ───────────────────────────────────
# Run the full suite (500 cases, no network required)
python -m unittest discover -s tests -v

# Run only matching modules (each test file is self-contained, can run alone)
python -m unittest tests.test_cover -v

**Tests exist under `tests/`** — pure-logic unit tests covering translation
(placeholder protect/restore, truncation timing), subtitle parsing/formatting
(batch format, duration clamp), glossary, profiles, cover processing, monitor
state/skip logic, subscriptions parsing/API fetch, uploader description/
credential guards, Bilibili subtitle API (CID poll, deferred upload), auth
checker, QR login, run reports, video splitting, OAuth auto-consent robot
(fake driver), OAuth record-replay recorder (fake driver), and discord pure
logic.
All network calls are mocked; ffmpeg/probe tests mock `config.find_tool`.
Verify changes by running `python -m unittest discover -s tests`, then
`python main.py <url>` end-to-end for integration.

## Configuration

All settings in `config/.env` (copy from `config/.env.example`). Read by `yt2bili/config.py` via `python-dotenv`.

**Multi-account**: Create `config/profiles.json` (copy from `config/profiles.json.example`) to manage multiple Bilibili accounts. Each profile bundles Bilibili credentials, a YouTube channel list, and optional setting overrides. Use `--profile <name>` to select one, or `--all-profiles` with `--monitor` to cycle through all.

- **Bilibili credentials**: `BILI_SESSDATA`, `BILI_BILI_JCT` — auto-populated by QR login
- **Translation**: `TRANSLATE_PROVIDER` (deepseek/openai/google), API keys, model selection, `TRANSLATION_PRESERVE_TERMS`
- **Upload**: `DEFAULT_TID` (Bilibili zone ID), `DEFAULT_TAGS`
- **Download**: `MAX_HEIGHT` (1080), `DOWNLOAD_MIN_SPEED_KIB`, `CLEANUP_AFTER_UPLOAD`
- **Monitor**: `YOUTUBE_MONITOR_INTERVAL_SECONDS`, `YOUTUBE_MONITOR_SOURCE` (api/rss)
- **OAuth auto-consent**: `YOUTUBE_OAUTH_AUTO_CONSENT` (default true), `YOUTUBE_OAUTH_ACCOUNT_EMAIL`, `YOUTUBE_OAUTH_BROWSER_CHANNEL` (msedge), `YOUTUBE_OAUTH_BROWSER_PROFILE`, `YOUTUBE_OAUTH_TIMEOUT_SECONDS`, `YOUTUBE_OAUTH_RECORD_ENABLED` (record-once-replay-always, default true)
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
| `yt2bili/config.py` | `.env` reader with typed `_get()`/`_get_int()`, `validate()` checks credentials, `apply_profile_overrides()`, `find_tool()` (PATH + Windows registry fallback for ffmpeg/ffprobe) |
| `yt2bili/auth_checker.py` | Credential status checks — Bilibili API validity, YouTube OAuth token, YouTube cookie health, `run_auth_check()` report |
| `yt2bili/profile.py` | Multi-account profile system — `profiles.json` load/save, active profile state, state/cache path resolution |
| `yt2bili/youtube/downloader.py` | yt-dlp Python API, cookie fallback chain, slow-speed detection + restart, ffprobe probing |
| `yt2bili/translation/translator.py` | `BaseTranslator` → `GoogleTranslator` / `OpenAITranslator` / `DeepSeekTranslator`, term preservation, 80-char truncation |
| `yt2bili/media/cover.py` | Pillow-based: validate → EXIF transpose → crop or contain → resize to 1920×1080 JPEG |
| `yt2bili/bilibili/uploader.py` | Async `bilibili-api-python` wrapped synchronously, multi-part (分P) support, fallback 1×1 JPEG cover, optional `credential` param |
| `yt2bili/bilibili/auth.py` | Bilibili QR login flow, auto-saves credentials to profiles.json or .env, profile-aware `get_credential()` |
| `yt2bili/media/video_splitter.py` | ffmpeg `-c copy` lossless segmenting at keyframes |
| `yt2bili/youtube/monitor.py` | Polling loop: fetch subs → deduplicate → sort queue → process → retry → persist state; multi-profile round-robin support |
| `yt2bili/youtube/subscriptions.py` | Standalone sub fetcher (API + RSS), custom `YouTubeClient` (requests-based, avoids httplib2 proxy issues), channel handle resolution |
| `yt2bili/youtube/oauth_consent.py` | Zero-click OAuth consent — `auto_consent()` strategy chain: replay recorded flow → record human's manual flow → built-in Playwright robot (account chooser → Continue → Continue, Advanced/unsafe); `auto_consent_browser()` patches `webbrowser.open` **and** `webbrowser.get` (newer google-auth-oauthlib calls `webbrowser.get().open()`), falls back to manual browser |
| `yt2bili/youtube/oauth_recorder.py` | Record-once-replay-always OAuth — JS event capture (clicks/Enter/non-password text) into `youtube_token.recording.json`, tolerant step replay with locator-candidate fallbacks; pure logic + `RecorderDriver` protocol for fake-driver tests |
| `yt2bili/bilibili/subtitle.py` | Soft-subtitle API via httpx — CID lookup (`get_video_pages`/`wait_for_cid`), draft/save submit, deferred upload queue (`pending_subtitles.json`) |
| `yt2bili/discord/monitor.py`, `publisher.py` | Discord 消息监控（Gateway + REST 兜底）→ B站动态发布（requires `discord.py`, not installed — only pure-logic parts are tested） |

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

### YouTube OAuth Auto-Consent (`yt2bili/youtube/oauth_consent.py`)

When `youtube_token.json` is missing/revoked, `run_local_server()` re-opens
the Google consent page. `auto_consent_browser()` patches `webbrowser.open`
and `webbrowser.get` (newer google-auth-oauthlib calls
`webbrowser.get(browser).open(url, new=1, autoraise=True)`, which bypasses
the module-level function) so a Playwright robot (visible Edge/Chrome via
`channel=`, no `playwright install` needed) completes the flow
automatically; any failure falls back to a manual browser. The walking
logic is pure (`decide_action()` over a `PageSnapshot`), so tests use a
fake driver — no browser in tests.

Strategy chain when a recording file is configured (`auto_consent()`):
1. **Replay** a previously recorded flow (`yt2bili/youtube/oauth_recorder.py`)
2. **Record mode** (no recording yet): the human clicks through once — every
   click/keystroke is captured by an injected script and saved to
   `youtube_token.recording.json` (passwords never recorded)
3. **Built-in robot**: hand-coded rules (`decide_action`) for the standard flow
4. Manual browser (webbrowser fallback)

Replay is tolerant: each recorded step only fires while the page URL matches
where it was recorded, locator candidates go stable-first (data-identifier →
role+text → text → aria/id/name → proportional coordinates), and steps whose
element never appears are skipped after a per-step timeout. Re-record by
deleting the `.recording.json` file; disable with
`YOUTUBE_OAUTH_RECORD_ENABLED=false`.

- **Concurrent monitors**: all profiles share ONE `youtube_token.json`
  (profiles do not override `YOUTUBE_TOKEN_FILE`). `oauth_consent_lock()`
  is a single-flight file lock — the lock holder runs the consent flow and
  the token is written atomically (temp + `os.replace`) *while still
  holding the lock*, so concurrent `--profile snap` / `--profile deadlock`
  processes never run two consent flows or read a half-written token.
- First-ever run: the user logs into Google once in the popped-up browser
  window; the session persists in `config/oauth_browser_profile/`.
- Re-consent frequency: Google expires refresh tokens after **7 days** while
  the GCP OAuth client's publishing status is "Testing". Publishing the app
  to "In production" (no verification needed for personal use) stops the
  weekly re-authorization; the consent page then adds the
  "Advanced → Go to … (unsafe)" step, which the robot handles automatically.

### Retry Strategy (multi-layered)

| Layer | Config keys | Default |
|---|---|---|
| YouTube API HTTP | `YOUTUBE_API_MAX_RETRIES`, `_RETRY_DELAY` | 3 retries, 2s base |
| Per-video processing | `YOUTUBE_VIDEO_RETRY_MAX`, `_RETRY_DELAY` | 2 retries, 30s base |
| Monitor cycle | `YOUTUBE_MONITOR_MAX_RETRIES`, `_RETRY_DELAY` | 5 retries, 30s base |

Only `download`, `split`, `upload` stages are retryable. Translation and cover failures are fatal for that video.

### Path Resolution

Always use `config.PROJECT_ROOT` (resolved in `config.py`):
- **Dev mode**: project root (the parent of the `yt2bili/` package)
- **Frozen EXE**: directory next to `yt2bili.exe` (via `sys.frozen` — `__file__` would point into PyInstaller's temp extraction dir)

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
