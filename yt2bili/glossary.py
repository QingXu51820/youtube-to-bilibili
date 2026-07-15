"""
Marvel SNAP card/location glossary — EN→CN name mapping.

Fetches official translations from untapped.gg's public JSON API,
caches locally, and refreshes periodically in background.

Usage:
    from yt2bili.glossary import get_glossary
    glossary = get_glossary()  # dict[str, str] — {"Abomination": "恶型怪", ...}
"""

import json
import os
import threading
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

import requests

from yt2bili import config

_CARDS_URL = "https://snapjson.untapped.gg/v2/latest/zh/cards.json"
_LOCATIONS_URL = "https://snapjson.untapped.gg/v2/latest/zh/locations.json"

# ── Module-level cache ────────────────────────────────────────────────
_glossary: dict[str, str] | None = None
_glossary_lock = threading.Lock()
_last_fetch_time: float = 0.0
_fetch_in_progress: bool = False  # prevents concurrent background fetches


def _load_cache(path: Path) -> dict[str, str] | None:
    """Load glossary from a local cache file. Returns None on any failure."""
    try:
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            glossary = data.get("glossary", {})
            if glossary:
                return {str(k): str(v) for k, v in glossary.items()}
    except (json.JSONDecodeError, OSError, KeyError):
        pass
    return None


def _save_cache(path: Path, glossary: dict[str, str]) -> None:
    """Persist glossary to a local cache file."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "updated_at": datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S (北京时间)"),
            "count": len(glossary),
            "glossary": glossary,
        }
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        tmp.replace(path)
    except OSError:
        pass  # non-critical — will retry next time


def _fetch_json(url: str) -> list[dict[str, Any]]:
    """Fetch a JSON array from a URL. Returns empty list on failure."""
    try:
        timeout = max(5, int(getattr(config, "DISCORD_HTTP_TIMEOUT", None) or 30))
        resp = requests.get(url, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _build_glossary() -> dict[str, str]:
    """Fetch cards and locations from API, build EN→CN mapping."""
    glossary: dict[str, str] = {}

    # Cards
    cards = _fetch_json(_CARDS_URL)
    for card in cards:
        en = (card.get("originalName") or "").strip()
        cn = (card.get("name") or "").strip()
        if en and cn and en.lower() != cn.lower():
            glossary[en] = cn

    # Locations
    locations = _fetch_json(_LOCATIONS_URL)
    for loc in locations:
        en = (loc.get("originalName") or "").strip()
        cn = (loc.get("name") or "").strip()
        if en and cn and en.lower() != cn.lower():
            glossary[en] = cn

    return glossary


def _background_refresh(cache_path: Path) -> None:
    """Fetch fresh glossary data and update the module-level cache."""
    global _glossary, _last_fetch_time, _fetch_in_progress
    try:
        glossary = _build_glossary()
        if glossary:
            with _glossary_lock:
                _glossary = glossary
                _last_fetch_time = time.time()
            _save_cache(cache_path, glossary)
    except Exception:
        pass  # keep using old cache
    finally:
        _fetch_in_progress = False


def get_glossary() -> dict[str, str]:
    """Return the current EN→CN glossary, refreshing if needed.

    On first call: loads from cache or fetches synchronously.
    On subsequent calls: returns cached data; if TTL expired, triggers
    a background refresh while continuing to serve the stale cache.
    """
    global _glossary, _last_fetch_time, _fetch_in_progress

    if not config.SNAP_GLOSSARY_ENABLED:
        return {}

    ttl = max(3600, config.SNAP_GLOSSARY_TTL)
    cache_path = Path(config.SNAP_GLOSSARY_CACHE)

    with _glossary_lock:
        # First load: try cache, then fetch
        if _glossary is None:
            _glossary = _load_cache(cache_path)
            if _glossary is not None:
                _last_fetch_time = time.time()
                # Check cache file mtime for TTL tracking
                try:
                    _last_fetch_time = max(_last_fetch_time, cache_path.stat().st_mtime)
                except OSError:
                    pass
            else:
                # No cache — must fetch synchronously
                glossary = _build_glossary()
                if glossary:
                    _glossary = glossary
                    _last_fetch_time = time.time()
                    _save_cache(cache_path, glossary)
                return _glossary or {}

        # Check if refresh is needed
        age = time.time() - _last_fetch_time
        if age >= ttl and not _fetch_in_progress:
            _fetch_in_progress = True
            t = threading.Thread(target=_background_refresh, args=(cache_path,), daemon=True)
            t.start()

        return _glossary or {}


# ── Deadlock Hero/Item Glossary (fetched from deadlock.wiki) ──────────

_LANG_ZH_URL = "https://deadlock.wiki/index.php?title=Data:Lang_zh-hans.json&action=raw"
_LANG_EN_URL = "https://deadlock.wiki/index.php?title=Data:Lang_en.json&action=raw"

# Known hero keys in Lang_zh-hans.json (hero_<codename>)
_HERO_KEYS = {
    "hero_atlas": "Abrams", "hero_bebop": "Bebop", "hero_dynamo": "Dynamo",
    "hero_orion": "Grey Talon", "hero_haze": "Haze", "hero_inferno": "Infernus",
    "hero_tengu": "Ivy", "hero_kelvin": "Kelvin", "hero_ghost": "Lady Geist",
    "hero_lash": "Lash", "hero_forge": "McGinnis", "hero_mirage": "Mirage",
    "hero_krill": "Mo & Krill", "hero_chrono": "Paradox", "hero_synth": "Pocket",
    "hero_gigawatt": "Seven", "hero_shiv": "Shiv", "hero_hornet": "Vindicta",
    "hero_viscous": "Viscous", "hero_warden": "Warden", "hero_wraith": "Wraith",
    "hero_yamato": "Yamato",
    "hero_astro": "Holliday", "hero_nano": "Calico", "hero_viper": "Vyper",
    "hero_magician": "Sinclair", "hero_bookworm": "Paige", "hero_drifter": "Drifter",
    "hero_vampirebat": "Mina", "hero_doorman": "The Doorman",
    "hero_punkgoat": "Billy", "hero_frank": "Victor",
    "hero_familiar": "Rem", "hero_fencer": "Apollo", "hero_unicorn": "Celeste",
    "hero_necro": "Graves", "hero_werewolf": "Silver", "hero_priest": "Venator",
    "hero_slork": "Fathom", "hero_operative": "Raven", "hero_trapper": "Trapper",
    "hero_wrecker": "Wrecker",
    "hero_boho": "Boho", "hero_skyrunner": "Skyrunner", "hero_swan": "Swan",
    "hero_genericperson": "Generic Person", "hero_shieldguy": "Shield Guy",
    "hero_akimbo": "Akimbo", "hero_yakuza": "The Boss",
}

# Additional EN aliases that map to existing hero keys
_HERO_ALIASES = {
    "Mo and Krill": "Mo & Krill",
    "The Magnificent Sinclair": "Sinclair",
    "Doorman": "The Doorman",
}

_deadlock_glossary: dict[str, str] | None = None
_deadlock_glossary_lock = threading.Lock()
_deadlock_last_fetch_time: float = 0.0
_deadlock_fetch_in_progress: bool = False


def _build_deadlock_glossary() -> dict[str, str]:
    """Fetch hero and item names from deadlock.wiki Lang JSON files.

    Returns an EN→CN mapping for all heroes, items, and game terms.
    """
    glossary: dict[str, str] = {}

    # 1. Fetch lang data from wiki
    data_zh = _fetch_json_dict(_LANG_ZH_URL)
    data_en = _fetch_json_dict(_LANG_EN_URL)

    if not data_zh:
        return glossary  # network failure — caller should keep old cache

    # 2. Heroes
    for key, en_name in _HERO_KEYS.items():
        cn = (data_zh.get(key, "") or "").split("|")[-1].split("#")[-1].strip()
        if cn and cn != en_name:
            glossary[en_name] = cn

    for alias, target in _HERO_ALIASES.items():
        if target in glossary:
            glossary[alias] = glossary[target]

    # 3. Items (upgrade_* keys from Lang files)
    _ITEM_SKIP_SUFFIXES = (
        "_desc", "_search", "_active", "_active_desc",
        "_buildup", "_pull", "_2", "_plus1", "_v2",
    )
    for key, value in data_zh.items():
        if not key.startswith("upgrade_") or ":" in key:
            continue
        if any(key.endswith(s) for s in _ITEM_SKIP_SUFFIXES):
            continue
        if "<" in value:
            continue
        cn_name = value.split("|")[-1].split("#")[-1].strip()
        en_name = (data_en.get(key, "") or "").split("|")[-1].split("#")[-1].strip()
        if not cn_name or cn_name == en_name or len(cn_name) > 50:
            continue
        glossary[en_name] = cn_name

    # 4. Game terms
    glossary["Deadlock"] = "死锁"
    glossary["Hero Labs"] = "英雄实验室"

    return glossary


def _fetch_json_dict(url: str) -> dict[str, Any]:
    """Fetch a JSON object from a URL. Returns empty dict on failure."""
    try:
        timeout = max(5, int(getattr(config, "DISCORD_HTTP_TIMEOUT", None) or 30))
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
        }
        resp = requests.get(url, timeout=timeout, headers=headers)
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _deadlock_background_refresh(cache_path: Path) -> None:
    """Fetch fresh Deadlock glossary and update the module-level cache."""
    global _deadlock_glossary, _deadlock_last_fetch_time, _deadlock_fetch_in_progress
    try:
        glossary = _build_deadlock_glossary()
        if glossary:
            with _deadlock_glossary_lock:
                _deadlock_glossary = glossary
                _deadlock_last_fetch_time = time.time()
            _save_cache(cache_path, glossary)
    except Exception:
        pass
    finally:
        _deadlock_fetch_in_progress = False


def get_deadlock_glossary() -> dict[str, str]:
    """Return the Deadlock hero/item EN→CN glossary.

    On first call: loads from cache or fetches from deadlock.wiki synchronously.
    On subsequent calls: returns cached data; if TTL expired, triggers
    a background refresh while continuing to serve the stale cache.
    """
    global _deadlock_glossary, _deadlock_last_fetch_time, _deadlock_fetch_in_progress

    if not config.DEADLOCK_GLOSSARY_ENABLED:
        return {}

    ttl = max(3600, getattr(config, "DEADLOCK_GLOSSARY_TTL", config.SNAP_GLOSSARY_TTL))
    cache_path = Path(config.DEADLOCK_GLOSSARY_CACHE)

    with _deadlock_glossary_lock:
        # First load: try cache, then fetch
        if _deadlock_glossary is None:
            _deadlock_glossary = _load_cache(cache_path)
            if _deadlock_glossary is not None:
                _deadlock_last_fetch_time = time.time()
                try:
                    _deadlock_last_fetch_time = max(
                        _deadlock_last_fetch_time, cache_path.stat().st_mtime
                    )
                except OSError:
                    pass
            else:
                # No cache — must fetch synchronously
                glossary = _build_deadlock_glossary()
                if glossary:
                    _deadlock_glossary = glossary
                    _deadlock_last_fetch_time = time.time()
                    _save_cache(cache_path, glossary)
                return _deadlock_glossary or {}

        # Check if refresh is needed
        age = time.time() - _deadlock_last_fetch_time
        if age >= ttl and not _deadlock_fetch_in_progress:
            _deadlock_fetch_in_progress = True
            t = threading.Thread(
                target=_deadlock_background_refresh, args=(cache_path,), daemon=True
            )
            t.start()

        return _deadlock_glossary or {}
