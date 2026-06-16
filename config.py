"""
Configuration management for the YouTube → Bilibili pipeline.
Reads settings from .env file and provides typed access.

All values have defaults; call validate() at startup to ensure required
credentials are present before running the pipeline.
"""

import os
from pathlib import Path
from dotenv import load_dotenv

# Load .env from the project root (if it exists)
from frozen_paths import user_data_dir

PROJECT_ROOT = user_data_dir()
load_dotenv(PROJECT_ROOT / ".env")


def _get(key: str, default: str = "") -> str:
    """Get an optional environment variable."""
    return os.getenv(key, default)


def _get_int(key: str, default: int) -> int:
    """Get an integer environment variable, falling back to default on bad input."""
    value = _get(key, str(default)).strip()
    try:
        return int(value)
    except ValueError:
        return default


# ── Bilibili Credentials ──────────────────────────────────────────
BILI_SESSDATA = _get("BILI_SESSDATA", "")
BILI_BILI_JCT = _get("BILI_BILI_JCT", "")
BILI_BUVID3 = _get("BILI_BUVID3", "")

# ── Translation ───────────────────────────────────────────────────
TRANSLATE_PROVIDER = _get("TRANSLATE_PROVIDER", "deepseek").lower()  # google | openai | deepseek
OPENAI_API_KEY = _get("OPENAI_API_KEY", "")
OPENAI_BASE_URL = _get("OPENAI_BASE_URL", "")
OPENAI_MODEL = _get("OPENAI_MODEL", "gpt-4o-mini")
DEEPSEEK_API_KEY = _get("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = _get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
DEEPSEEK_MODEL = _get("DEEPSEEK_MODEL", "deepseek-v4-flash")
DEEPSEEK_THINKING = _get("DEEPSEEK_THINKING", "disabled").lower()
TRANSLATION_PRESERVE_TERMS = _get("TRANSLATION_PRESERVE_TERMS", "Marvel SNAP,SNAP")
TRANSLATION_EXTRA_PROMPT = _get("TRANSLATION_EXTRA_PROMPT", "")
TRANSLATION_PROXY = _get("TRANSLATION_PROXY", "").strip()

# ── Upload Settings ───────────────────────────────────────────────
DEFAULT_TID = int(_get("DEFAULT_TID", "172"))  # 172 = 游戏-手机游戏
DEFAULT_TAGS = _get("DEFAULT_TAGS", "转载,YouTube")  # comma-separated

# ── Download Settings ─────────────────────────────────────────────
DOWNLOAD_DIR = _get("DOWNLOAD_DIR", str(PROJECT_ROOT / "downloads"))
CLEANUP_AFTER_UPLOAD = _get("CLEANUP_AFTER_UPLOAD", "true").lower() == "true"
MAX_HEIGHT = _get_int("MAX_HEIGHT", 1080)  # max video height
DOWNLOAD_MIN_SPEED_KIB = _get_int("DOWNLOAD_MIN_SPEED_KIB", 100)
DOWNLOAD_SLOW_SECONDS = _get_int("DOWNLOAD_SLOW_SECONDS", 60)
DOWNLOAD_SLOW_GRACE_SECONDS = _get_int("DOWNLOAD_SLOW_GRACE_SECONDS", 30)
DOWNLOAD_MAX_RESTARTS = _get_int("DOWNLOAD_MAX_RESTARTS", 3)
DOWNLOAD_STARTUP_STATUS_SECONDS = _get_int("DOWNLOAD_STARTUP_STATUS_SECONDS", 30)

# ── Cover Settings ────────────────────────────────────────────────
COVER_WIDTH = _get_int("COVER_WIDTH", 1920)
COVER_HEIGHT = _get_int("COVER_HEIGHT", 1080)
COVER_FIT = _get("COVER_FIT", "crop").lower()

# ── Run Result Settings ───────────────────────────────────────────
RUNS_DIR = _get("RUNS_DIR", str(PROJECT_ROOT / "runs"))

# ── YouTube Subscription Monitor ─────────────────────────────────
YOUTUBE_CLIENT_SECRET_FILE = _get("YOUTUBE_CLIENT_SECRET_FILE", "client_secret.json")
YOUTUBE_TOKEN_FILE = _get("YOUTUBE_TOKEN_FILE", "youtube_token.json")
YOUTUBE_MAX_VIDEOS_PER_CHANNEL = _get_int("YOUTUBE_MAX_VIDEOS_PER_CHANNEL", 5)
YOUTUBE_SUBSCRIPTIONS_CACHE = _get("YOUTUBE_SUBSCRIPTIONS_CACHE", "subscriptions_cache.json")
YOUTUBE_PROXY = _get("YOUTUBE_PROXY", "").strip()
YOUTUBE_HTTP_TIMEOUT = _get_int("YOUTUBE_HTTP_TIMEOUT", 60)
DOWNLOAD_PROXY = _get("DOWNLOAD_PROXY", YOUTUBE_PROXY).strip()
YOUTUBE_COOKIES_FROM_BROWSER = _get("YOUTUBE_COOKIES_FROM_BROWSER", "chrome,edge,firefox")
YOUTUBE_COOKIE_FILE = _get("YOUTUBE_COOKIE_FILE", "cookies.txt")
YTDLP_REMOTE_COMPONENTS = _get("YTDLP_REMOTE_COMPONENTS", "ejs:github")
YOUTUBE_MONITOR_INTERVAL_SECONDS = _get_int("YOUTUBE_MONITOR_INTERVAL_SECONDS", 3600)
YOUTUBE_MONITOR_SOURCE = _get("YOUTUBE_MONITOR_SOURCE", "api").lower()
YOUTUBE_MONITOR_LIMIT = _get_int("YOUTUBE_MONITOR_LIMIT", 50)
YOUTUBE_DEFER_LONG_VIDEO_MINUTES = _get_int("YOUTUBE_DEFER_LONG_VIDEO_MINUTES", 60)
MAX_VIDEO_DURATION_SECONDS = _get_int("MAX_VIDEO_DURATION_SECONDS", 36000)  # 10 hours
YOUTUBE_SKIP_LONG_VIDEO_MINUTES = _get_int("YOUTUBE_SKIP_LONG_VIDEO_MINUTES", 0)  # 0=disabled
YOUTUBE_API_MAX_RETRIES = _get_int("YOUTUBE_API_MAX_RETRIES", 3)
YOUTUBE_API_RETRY_DELAY = _get_int("YOUTUBE_API_RETRY_DELAY", 2)
YOUTUBE_MONITOR_MAX_RETRIES = _get_int("YOUTUBE_MONITOR_MAX_RETRIES", 5)
YOUTUBE_MONITOR_RETRY_DELAY = _get_int("YOUTUBE_MONITOR_RETRY_DELAY", 30)
YOUTUBE_VIDEO_RETRY_MAX = _get_int("YOUTUBE_VIDEO_RETRY_MAX", 2)
YOUTUBE_VIDEO_RETRY_DELAY = _get_int("YOUTUBE_VIDEO_RETRY_DELAY", 30)
YOUTUBE_MONITOR_STATE = _get(
    "YOUTUBE_MONITOR_STATE",
    str(PROJECT_ROOT / "state" / "processed_videos.json"),
)

# ── Source Language ───────────────────────────────────────────────
SOURCE_LANG = _get("SOURCE_LANG", "auto")  # source language for translation


def validate() -> list[str]:
    """
    Validate all required config.
    Returns list of issues (empty list = all OK).
    Also creates the download directory if missing.
    """
    issues = []

    if not BILI_SESSDATA:
        issues.append("Missing required config: BILI_SESSDATA. Please set it in .env file.")
    if not BILI_BILI_JCT:
        issues.append("Missing required config: BILI_BILI_JCT. Please set it in .env file.")

    if TRANSLATE_PROVIDER == "openai" and not OPENAI_API_KEY:
        issues.append("TRANSLATE_PROVIDER=openai requires OPENAI_API_KEY")
    if TRANSLATE_PROVIDER == "deepseek" and not DEEPSEEK_API_KEY:
        issues.append("TRANSLATE_PROVIDER=deepseek requires DEEPSEEK_API_KEY")

    if TRANSLATE_PROVIDER not in ("google", "openai", "deepseek"):
        issues.append(f"Unknown TRANSLATE_PROVIDER: {TRANSLATE_PROVIDER}")
    if DEEPSEEK_THINKING not in ("enabled", "disabled"):
        issues.append("DEEPSEEK_THINKING must be enabled or disabled")
    if COVER_WIDTH <= 0:
        issues.append("COVER_WIDTH must be a positive integer")
    if COVER_HEIGHT <= 0:
        issues.append("COVER_HEIGHT must be a positive integer")
    if COVER_FIT not in ("crop", "contain"):
        issues.append("COVER_FIT must be crop or contain")
    # Useless default values for SESSDATA / bili_jct check
    bogus_defaults = ("your_sessdata_here", "your_bili_jct_here", "")
    if BILI_SESSDATA.lower() in bogus_defaults:
        issues.append("BILI_SESSDATA appears to be a placeholder value. Please set your real SESSDATA.")
    if BILI_BILI_JCT.lower() in bogus_defaults:
        issues.append("BILI_BILI_JCT appears to be a placeholder value. Please set your real bili_jct.")

    download_path = Path(DOWNLOAD_DIR)
    if not download_path.exists():
        try:
            download_path.mkdir(parents=True)
        except Exception as e:
            issues.append(f"Cannot create download dir {DOWNLOAD_DIR}: {e}")

    runs_path = Path(RUNS_DIR)
    if not runs_path.exists():
        try:
            runs_path.mkdir(parents=True)
        except Exception as e:
            issues.append(f"Cannot create runs dir {RUNS_DIR}: {e}")

    return issues
