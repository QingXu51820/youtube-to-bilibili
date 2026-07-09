"""
Profile system for multi-account Bilibili support.

Each profile bundles Bilibili credentials, a YouTube channel list, and optional
setting overrides. Profiles are stored in ``config/profiles.json``.

When no profiles file exists and no ``--profile`` flag is passed, the system
uses credentials from ``config/.env`` exactly as before (full backward compat).
"""

import json
from dataclasses import dataclass, field
from pathlib import Path

from yt2bili import config


# ── Data model ──────────────────────────────────────────────────────────────

@dataclass
class BiliCredentials:
    """Bilibili session credentials for one account."""
    sessdata: str = ""
    bili_jct: str = ""
    buvid3: str = ""
    login_time: str = ""  # ISO 8601


@dataclass
class YouTubeChannel:
    """A YouTube channel to monitor."""
    channel_id: str
    channel_title: str = ""


@dataclass
class YouTubeSettings:
    """YouTube-related profile settings."""
    channels: list[YouTubeChannel] = field(default_factory=list)
    monitor_source: str = ""       # "" → use global default
    monitor_state: str = ""        # "" → auto: state/{profile}/processed_videos.json
    subscriptions_cache: str = ""  # "" → auto: config/{profile}_subscriptions_cache.json


@dataclass
class ProfileSettings:
    """Optional overrides for pipeline config (None = use global default)."""
    default_tid: int | None = None
    default_tags: str | None = None
    content_filter_enabled: bool | None = None
    content_filter_keywords: str | None = None


@dataclass
class Profile:
    """A named configuration bundle for a Bilibili account + its YouTube channels."""
    name: str
    bilibili: BiliCredentials = field(default_factory=BiliCredentials)
    youtube: YouTubeSettings = field(default_factory=YouTubeSettings)
    settings: ProfileSettings = field(default_factory=ProfileSettings)


# ── Paths ───────────────────────────────────────────────────────────────────

PROFILES_FILE: Path = config.PROJECT_ROOT / "config" / "profiles.json"


# ── Active profile state ────────────────────────────────────────────────────

_active_profile_name: str = "default"


def set_active_profile(name: str) -> None:
    """Set the globally active profile name."""
    global _active_profile_name
    _active_profile_name = name


def get_active_profile_name() -> str:
    """Return the currently active profile name."""
    return _active_profile_name


# ── Persistence ─────────────────────────────────────────────────────────────

def load_profiles() -> dict[str, Profile]:
    """
    Load all profiles from ``config/profiles.json``.
    Returns an empty dict if the file does not exist.
    """
    if not PROFILES_FILE.exists():
        return {}

    try:
        data = json.loads(PROFILES_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        print(f"⚠️  读取 profiles.json 失败: {exc}")
        return {}

    raw = data.get("profiles", {})
    if not isinstance(raw, dict):
        return {}

    profiles: dict[str, Profile] = {}
    for name, pdata in raw.items():
        try:
            profiles[name] = _dict_to_profile(name, pdata)
        except Exception as exc:
            print(f"⚠️  跳过无效 profile '{name}': {exc}")
    return profiles


def save_profiles(profiles: dict[str, Profile]) -> None:
    """Write all profiles to ``config/profiles.json`` (atomic via temp file)."""
    data: dict[str, object] = {"profiles": {}}
    for name, profile in profiles.items():
        data["profiles"][name] = _profile_to_dict(profile)  # type: ignore[index]

    tmp_path = PROFILES_FILE.with_suffix(".tmp")
    tmp_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    tmp_path.replace(PROFILES_FILE)


def save_profile(profile: Profile) -> None:
    """Save (upsert) a single profile into profiles.json."""
    profiles = load_profiles()
    profiles[profile.name] = profile
    save_profiles(profiles)


def get_profile(name: str) -> Profile | None:
    """Get a single profile by name, or None."""
    return load_profiles().get(name)


def profile_exists(name: str) -> bool:
    """Return True if a profile with this name exists in profiles.json."""
    return name in load_profiles()


def is_multi_profile() -> bool:
    """Return True when profiles.json exists (multi-account mode is active)."""
    return PROFILES_FILE.exists()


def resolve_profile(name: str = "default") -> Profile | None:
    """
    Return the named profile from profiles.json, or create a 'default'
    profile from .env if profiles.json doesn't exist (backward compat).

    Returns None if no profile can be resolved.
    """
    profiles = load_profiles()

    if name in profiles:
        return profiles[name]

    # Backward compat: "default" profile from .env when no profiles.json
    if name == "default":
        return Profile(
            name="default",
            bilibili=BiliCredentials(
                sessdata=config.BILI_SESSDATA,
                bili_jct=config.BILI_BILI_JCT,
                buvid3=config.BILI_BUVID3,
                login_time=config.BILI_LOGIN_TIME,
            ),
        )

    return None


# ── State / cache path helpers ──────────────────────────────────────────────

def get_state_file_path(profile: Profile) -> Path:
    """Return the state file path for a profile."""
    if profile.youtube.monitor_state:
        return config.PROJECT_ROOT / profile.youtube.monitor_state
    return config.PROJECT_ROOT / "state" / profile.name / "processed_videos.json"


def get_cache_file_path(profile: Profile) -> Path:
    """Return the subscriptions cache path for a profile."""
    if profile.youtube.subscriptions_cache:
        return config.PROJECT_ROOT / profile.youtube.subscriptions_cache
    return config.PROJECT_ROOT / "config" / f"{profile.name}_subscriptions_cache.json"


# ── Internal helpers ────────────────────────────────────────────────────────

def _dict_to_profile(name: str, d: dict) -> Profile:
    """Build a Profile from a deserialized JSON dict."""
    bili_raw = d.get("bilibili", {})
    yt_raw = d.get("youtube", {})
    settings_raw = d.get("settings", {})

    channels = [
        YouTubeChannel(channel_id=c["channel_id"], channel_title=c.get("channel_title", ""))
        for c in yt_raw.get("channels", [])
    ]

    return Profile(
        name=name,
        bilibili=BiliCredentials(
            sessdata=bili_raw.get("sessdata", ""),
            bili_jct=bili_raw.get("bili_jct", ""),
            buvid3=bili_raw.get("buvid3", ""),
            login_time=bili_raw.get("login_time", ""),
        ),
        youtube=YouTubeSettings(
            channels=channels,
            monitor_source=yt_raw.get("monitor_source", ""),
            monitor_state=yt_raw.get("monitor_state", ""),
            subscriptions_cache=yt_raw.get("subscriptions_cache", ""),
        ),
        settings=ProfileSettings(
            default_tid=settings_raw.get("default_tid"),
            default_tags=settings_raw.get("default_tags"),
            content_filter_enabled=settings_raw.get("content_filter_enabled"),
            content_filter_keywords=settings_raw.get("content_filter_keywords"),
        ),
    )


def _profile_to_dict(profile: Profile) -> dict:
    """Serialize a Profile to a JSON-compatible dict."""
    return {
        "bilibili": {
            "sessdata": profile.bilibili.sessdata,
            "bili_jct": profile.bilibili.bili_jct,
            "buvid3": profile.bilibili.buvid3,
            "login_time": profile.bilibili.login_time,
        },
        "youtube": {
            "channels": [
                {"channel_id": c.channel_id, "channel_title": c.channel_title}
                for c in profile.youtube.channels
            ],
            "monitor_source": profile.youtube.monitor_source,
            "monitor_state": profile.youtube.monitor_state,
            "subscriptions_cache": profile.youtube.subscriptions_cache,
        },
        "settings": {
            k: v for k, v in {
                "default_tid": profile.settings.default_tid,
                "default_tags": profile.settings.default_tags,
                "content_filter_enabled": profile.settings.content_filter_enabled,
                "content_filter_keywords": profile.settings.content_filter_keywords,
            }.items() if v is not None
        },
    }
