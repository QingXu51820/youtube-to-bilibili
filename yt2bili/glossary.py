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
