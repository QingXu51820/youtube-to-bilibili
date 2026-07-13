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
PROJECT_ROOT = Path(__file__).resolve().parent.parent  # 项目根目录（yt2bili/ 的父目录）
load_dotenv(PROJECT_ROOT / "config" / ".env")


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
BILI_LOGIN_TIME = _get("BILI_LOGIN_TIME", "")  # ISO 8601 timestamp of last successful QR login

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
YOUTUBE_CLIENT_SECRET_FILE = _get("YOUTUBE_CLIENT_SECRET_FILE", "config/client_secret.json")
YOUTUBE_TOKEN_FILE = _get("YOUTUBE_TOKEN_FILE", "config/youtube_token.json")
YOUTUBE_MAX_VIDEOS_PER_CHANNEL = _get_int("YOUTUBE_MAX_VIDEOS_PER_CHANNEL", 5)
YOUTUBE_SUBSCRIPTIONS_CACHE = _get("YOUTUBE_SUBSCRIPTIONS_CACHE", "config/subscriptions_cache.json")
YOUTUBE_PROXY = _get("YOUTUBE_PROXY", "").strip()
YOUTUBE_HTTP_TIMEOUT = _get_int("YOUTUBE_HTTP_TIMEOUT", 60)
DOWNLOAD_PROXY = _get("DOWNLOAD_PROXY", YOUTUBE_PROXY).strip()
YOUTUBE_COOKIES_FROM_BROWSER = _get("YOUTUBE_COOKIES_FROM_BROWSER", "chrome,edge,firefox")
YOUTUBE_COOKIE_FILE = _get("YOUTUBE_COOKIE_FILE", "config/cookies.txt")
YTDLP_REMOTE_COMPONENTS = _get("YTDLP_REMOTE_COMPONENTS", "ejs:github")
YOUTUBE_MONITOR_INTERVAL_SECONDS = _get_int("YOUTUBE_MONITOR_INTERVAL_SECONDS", 3600)
YOUTUBE_MONITOR_SOURCE = _get("YOUTUBE_MONITOR_SOURCE", "api").lower()
YOUTUBE_MONITOR_LIMIT = _get_int("YOUTUBE_MONITOR_LIMIT", 50)
YOUTUBE_DEFER_LONG_VIDEO_MINUTES = _get_int("YOUTUBE_DEFER_LONG_VIDEO_MINUTES", 60)
MAX_VIDEO_DURATION_SECONDS = _get_int("MAX_VIDEO_DURATION_SECONDS", 36000)  # 10 hours
YOUTUBE_SKIP_LONG_VIDEO_MINUTES = _get_int("YOUTUBE_SKIP_LONG_VIDEO_MINUTES", 0)  # 0=disabled
YOUTUBE_SKIP_VERTICAL_VIDEOS = _get("YOUTUBE_SKIP_VERTICAL_VIDEOS", "true").lower() == "true"
CONTENT_FILTER_ENABLED = _get("CONTENT_FILTER_ENABLED", "false").lower() == "true"
CONTENT_FILTER_KEYWORDS = _get("CONTENT_FILTER_KEYWORDS", "Marvel SNAP")
# Title keyword filter for monitor mode (empty = disabled, case-insensitive substring match)
TITLE_FILTER_KEYWORD = _get("TITLE_FILTER_KEYWORD", "")
YOUTUBE_API_MAX_RETRIES = _get_int("YOUTUBE_API_MAX_RETRIES", 3)
YOUTUBE_API_RETRY_DELAY = _get_int("YOUTUBE_API_RETRY_DELAY", 2)
YOUTUBE_MONITOR_MAX_RETRIES = _get_int("YOUTUBE_MONITOR_MAX_RETRIES", 5)
YOUTUBE_MONITOR_RETRY_DELAY = _get_int("YOUTUBE_MONITOR_RETRY_DELAY", 30)
YOUTUBE_VIDEO_RETRY_MAX = _get_int("YOUTUBE_VIDEO_RETRY_MAX", 2)
YOUTUBE_VIDEO_RETRY_DELAY = _get_int("YOUTUBE_VIDEO_RETRY_DELAY", 30)
RUNS_RETENTION_DAYS = _get_int("RUNS_RETENTION_DAYS", 90)
YOUTUBE_MONITOR_STATE = _get(
    "YOUTUBE_MONITOR_STATE",
    str(PROJECT_ROOT / "state" / "processed_videos.json"),
)

# ── Discord Message Monitor ────────────────────────────────────────
DISCORD_BOT_TOKEN = _get("DISCORD_BOT_TOKEN", "")
DISCORD_CHANNEL_IDS = _get("DISCORD_CHANNEL_IDS", "")  # comma-separated channel IDs
DISCORD_SKIP_BOTS = _get("DISCORD_SKIP_BOTS", "true").lower() == "true"
DISCORD_SKIP_EMPTY = _get("DISCORD_SKIP_EMPTY", "true").lower() == "true"
DISCORD_MAX_IMAGES = _get_int("DISCORD_MAX_IMAGES", 9)
DISCORD_TRANSLATE = _get("DISCORD_TRANSLATE", "true").lower() == "true"
DISCORD_STATE_FILE = _get(
    "DISCORD_STATE_FILE",
    str(PROJECT_ROOT / "state" / "discord_messages.json"),
)
DISCORD_FALLBACK_LIMIT = _get_int("DISCORD_FALLBACK_LIMIT", 20)
DISCORD_PROXY = _get("DISCORD_PROXY", YOUTUBE_PROXY).strip()

# ── Marvel SNAP Glossary ─────────────────────────────────────────────
SNAP_GLOSSARY_ENABLED = _get("SNAP_GLOSSARY_ENABLED", "true").lower() == "true"
SNAP_GLOSSARY_CACHE = _get(
    "SNAP_GLOSSARY_CACHE",
    str(PROJECT_ROOT / "data" / "snap_glossary.json"),
)
SNAP_GLOSSARY_TTL = _get_int("SNAP_GLOSSARY_TTL", 86400)  # 1 day

# ── Deadlock Glossary ────────────────────────────────────────────────
DEADLOCK_GLOSSARY_ENABLED = _get("DEADLOCK_GLOSSARY_ENABLED", "false").lower() == "true"
DEADLOCK_GLOSSARY_CACHE = _get(
    "DEADLOCK_GLOSSARY_CACHE",
    str(PROJECT_ROOT / "data" / "deadlock_glossary.json"),
)
DEADLOCK_GLOSSARY_TTL = _get_int("DEADLOCK_GLOSSARY_TTL", 86400)  # 1 day

# ── Source Language ───────────────────────────────────────────────
SOURCE_LANG = _get("SOURCE_LANG", "auto")  # source language for translation

# ── Subtitle Settings ─────────────────────────────────────────────
SUBTITLE_ENABLED = _get("SUBTITLE_ENABLED", "true").lower() == "true"
SUBTITLE_REQUIRED = _get("SUBTITLE_REQUIRED", "false").lower() == "true"
SUBTITLE_SOURCE_LANGS = _get("SUBTITLE_SOURCE_LANGS", "en.*,ja,ko")
SUBTITLE_TARGET_LANG = _get("SUBTITLE_TARGET_LANG", "zh-CN")
SUBTITLE_TRANSLATE_BATCH_SIZE = _get_int("SUBTITLE_TRANSLATE_BATCH_SIZE", 80)
SUBTITLE_TRANSLATE_WORKERS = _get_int("SUBTITLE_TRANSLATE_WORKERS", 3)  # parallel API threads
SUBTITLE_UPLOAD_TO_BILIBILI = _get("SUBTITLE_UPLOAD_TO_BILIBILI", "true").lower() == "true"
SUBTITLE_LAN = _get("SUBTITLE_LAN", "zh")
SUBTITLE_LAN_DOC = _get("SUBTITLE_LAN_DOC", "中文（简体）")
SUBTITLE_WAIT_CID_SECONDS = _get_int("SUBTITLE_WAIT_CID_SECONDS", 300)
SUBTITLE_WAIT_CID_INTERVAL = _get_int("SUBTITLE_WAIT_CID_INTERVAL", 10)
SUBTITLE_DIR = _get("SUBTITLE_DIR", str(Path(DOWNLOAD_DIR) / "subtitles"))


def apply_profile_overrides(profile_name: str = "default") -> None:
    """
    Overlay profile-specific settings onto module-level config variables.

    Call once at startup after the active profile has been selected.
    Only overrides settings that are explicitly set in the profile (non-None).
    Has no effect when profiles.json doesn't exist and profile_name is "default".
    """
    from yt2bili.profile import resolve_profile, is_multi_profile

    if profile_name == "default" and not is_multi_profile():
        return  # nothing to override — using .env directly

    profile = resolve_profile(profile_name)
    if profile is None:
        return

    s = profile.settings

    if s.default_tid is not None:
        global DEFAULT_TID
        DEFAULT_TID = s.default_tid

    if s.default_tags is not None:
        global DEFAULT_TAGS
        DEFAULT_TAGS = s.default_tags

    if s.content_filter_enabled is not None:
        global CONTENT_FILTER_ENABLED
        CONTENT_FILTER_ENABLED = s.content_filter_enabled

    if s.content_filter_keywords is not None:
        global CONTENT_FILTER_KEYWORDS
        CONTENT_FILTER_KEYWORDS = s.content_filter_keywords


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

    subtitle_path = Path(SUBTITLE_DIR)
    if not subtitle_path.exists():
        try:
            subtitle_path.mkdir(parents=True)
        except Exception as e:
            issues.append(f"Cannot create subtitle dir {SUBTITLE_DIR}: {e}")

    if SUBTITLE_TRANSLATE_BATCH_SIZE < 1:
        issues.append("SUBTITLE_TRANSLATE_BATCH_SIZE must be >= 1")
    if SUBTITLE_WAIT_CID_INTERVAL < 1:
        issues.append("SUBTITLE_WAIT_CID_INTERVAL must be >= 1")

    return issues
