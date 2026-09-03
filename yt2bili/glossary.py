"""
Marvel SNAP card/location/term glossary — EN→CN name mapping.

Fetches official translations from untapped.gg's public JSON API,
caches locally, and refreshes periodically in background.  In addition to
card/location names, it records game terms extracted from English
card/location descriptions and the official news feed.

Usage:
    from yt2bili.glossary import get_glossary, get_snap_game_terms
    glossary = get_glossary()  # dict[str, str] — {"Abomination": "恶型怪", ...}
    game_terms = get_snap_game_terms()  # {"On Reveal": "揭示", ...}
"""

import csv
import json
import os
import re
import threading
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

import requests

from yt2bili import config

_CARDS_URL = "https://snapjson.untapped.gg/v2/latest/zh/cards.json"
_LOCATIONS_URL = "https://snapjson.untapped.gg/v2/latest/zh/locations.json"
_CARDS_EN_URL = "https://snapjson.untapped.gg/v2/latest/en/cards.json"
_LOCATIONS_EN_URL = "https://snapjson.untapped.gg/v2/latest/en/locations.json"

# Game-term seed dictionary.  These are the translations used to enrich the
# card/location name glossary.  Keys are checked against English card/location
# descriptions (and official news) so only terms actually used by the current
# card pool/news feed are persisted.  Values follow the official Simplified
# Chinese localization used by untapped.gg / marvelsnap.com.
_SNAP_GAME_TERMS: dict[str, str] = {
    "On Reveal": "揭示",
    "Ongoing": "持续",
    "Activate": "激活",
    "End of Turn": "回合结束",
    "Start of Game": "对战开始",
    "Game Start": "对战开始",
    "Move": "移动",
    "Moveable": "可移动",
    "Empowered": "强化",
    "Horde": "军团",
    "Charged": "充能",
    "Volts": "伏特",
    "Regenerate": "再生",
    "Quickdraw": "速抽",
    "Destroy": "摧毁",
    "Destroyed": "被摧毁",
    "Discard": "丢弃",
    "Discarded": "被丢弃",
    "Banish": "放逐",
    "Banished": "被放逐",
    "Merge": "合并",
    "Copy": "复制",
    "Replace": "替换",
    "Add": "添加",
    "Draw": "抽牌",
    "Shuffle": "洗入",
    "Steal": "偷取",
    "Return to Hand": "返回手牌",
    "Put Back": "放回",
    "Bring Back": "带回",
    "Resurrect": "复活",
    "Set Power": "将战力设为",
    "Set Cost": "将能量消耗设为",
    "Double": "翻倍",
    "Objective": "目标",
    "Front Row": "前排",
    "Back Row": "后排",
    "Adjacent": "相邻",
    "Highest-Power": "战力最高",
    "Lowest-Power": "战力最低",
    "Highest-Cost": "能量消耗最高",
    "Lowest-Cost": "能量消耗最低",
    "Unrevealed": "未揭示",
    "Revealed": "已揭示",
    "Bonus Energy": "额外能量",
    "Max Energy": "最大能量",
    "Unspent Energy": "未消耗能量",
    "Power": "战力",
    "Cost": "能量消耗",
    "Energy": "能量",
    "Card": "卡牌",
    "Character": "角色",
    "Location": "区域",
    "Deck": "牌库",
    "Hand": "手牌",
    "Fill": "填满",
    "Swap": "交换",
    "Switch Sides": "换边",
    "Retreat": "撤退",
    "Snap": "加倍",
    "Conquest": "征服",
    "Ranked": "排位",
    "Alliance": "联盟",
    "Bounty": "赏金",
    "Collector's Tokens": "收藏家代币",
    "Boosters": "强化套组",
    "Wild Boosters": "万能强化套组",
    "Variant": "变体",
    "Premium Mystery Variant": "高级神秘变体",
    "Avatar": "头像",
    "Emote": "表情",
    "Album": "图鉴",
    "Border": "边框",
    "Season Pass": "赛季通行证",
    "Premium Season Pass": "高级赛季通行证",
    "Super Premium": "超高级",
    "SNAP Pack": "SNAP卡包",
    "Golden Gauntlet": "金光手套",
    "Twitch Drops": "Twitch掉宝",
    "High Voltage": "高压电",
    "Overdrive": "超载",
    "Grand Arena": "大竞技场",
    "Draft": "选牌模式",
    "Augments": "强化效果",
    "Legacy Variants": "经典变体",
    "Daily Offer Shop": "今日推荐商店",
    "Character Mastery": "角色专精",
    "Google Play Achievements": "Google Play成就",
    "Web Shop": "网页商店",
}

# Subset that is safe to auto-apply to arbitrary English title/subtitle text.
# Common verbs/nouns such as "Move", "Draw", "Add", "Power" are deliberately
# excluded — replacing every occurrence would corrupt normal English text.
_SNAP_AUTO_APPLY_TERMS: frozenset[str] = frozenset({
    "On Reveal", "Ongoing", "Activate", "End of Turn", "Start of Game",
    "Game Start", "Moveable", "Empowered", "Horde", "Regenerate", "Quickdraw",
    "Destroyed", "Discarded", "Banish", "Banished",
    "Return to Hand", "Put Back", "Bring Back", "Set Power", "Set Cost",
    "Objective", "Front Row", "Back Row", "Adjacent", "Highest-Power",
    "Lowest-Power", "Highest-Cost", "Lowest-Cost", "Unrevealed", "Revealed",
    "Bonus Energy", "Max Energy", "Unspent Energy", "Switch Sides",
    "Conquest", "Alliance", "Bounty", "Collector's Tokens", "Boosters",
    "Wild Boosters", "Premium Mystery Variant", "Season Pass",
    "Premium Season Pass", "Super Premium", "SNAP Pack", "Golden Gauntlet",
    "Twitch Drops", "High Voltage", "Overdrive", "Grand Arena", "Draft",
    "Augments", "Legacy Variants", "Daily Offer Shop", "Character Mastery",
    "Google Play Achievements", "Web Shop",
})

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
    except (json.JSONDecodeError, OSError, KeyError, TypeError, AttributeError):
        pass  # corrupted/edited cache — treat as missing, will refetch
    return None


def _save_cache(
    path: Path,
    glossary: dict[str, str],
    game_terms: dict[str, str] | None = None,
) -> None:
    """Persist glossary (and optional game terms) to a local cache file."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "updated_at": datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S (北京时间)"),
            "count": len(glossary),
            "glossary": glossary,
        }
        if game_terms is not None:
            payload["game_terms"] = game_terms
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        tmp.replace(path)
    except OSError:
        pass  # non-critical — will retry next time


def _load_game_terms(path: Path) -> dict[str, str]:
    """Load game terms from a cache file, falling back to built-in seeds."""
    try:
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            terms = data.get("game_terms", {})
            if isinstance(terms, dict) and terms:
                return {str(k): str(v) for k, v in terms.items()}
    except (json.JSONDecodeError, OSError, KeyError, TypeError, AttributeError):
        pass
    return dict(_SNAP_GAME_TERMS)


def _fetch_json(url: str) -> list[dict[str, Any]]:
    """Fetch a JSON array from a URL. Returns empty list on failure."""
    try:
        # Generic network timeout — must not piggyback on the Discord setting.
        timeout = max(5, int(getattr(config, "YOUTUBE_HTTP_TIMEOUT", None) or 30))
        resp = requests.get(url, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else []
    except Exception:
        return []


_DESCRIPTION_TAG_RE = re.compile(r"<[^>]+>")
_DESCRIPTION_WS_RE = re.compile(r"\s+")


def _clean_description(description: str) -> str:
    """Strip HTML and normalise whitespace in a card/location description."""
    description = _DESCRIPTION_TAG_RE.sub(" ", description or "")
    description = re.sub(r"[“”\"']", " ", description)
    description = description.replace("’", "'").replace("…", " ")
    return _DESCRIPTION_WS_RE.sub(" ", description).strip()


def _term_present(text: str, term: str) -> bool:
    """Return True if ``term`` appears as a whole phrase in ``text``."""
    term = term.strip()
    if not term:
        return False
    pattern = re.compile(
        rf"(?<![A-Za-z0-9]){re.escape(term)}(?![A-Za-z0-9])",
        re.IGNORECASE,
    )
    return bool(pattern.search(text))


def _extract_game_terms_from_items(items: list[dict[str, Any]]) -> dict[str, str]:
    """Return present game terms from a collection of card/location items."""
    descriptions: list[str] = []
    for item in items:
        description = _clean_description(item.get("description") or "")
        if description:
            descriptions.append(description)

    found: dict[str, str] = {}
    for term, cn in _SNAP_GAME_TERMS.items():
        if any(_term_present(desc, term) for desc in descriptions):
            found[term] = cn
    return found


def _build_glossary() -> dict[str, str]:
    """Fetch cards/locations and build the EN→CN name+term mapping."""
    glossary: dict[str, str] = {}

    # Cards
    cards = _fetch_json(_CARDS_URL)
    locations = _fetch_json(_LOCATIONS_URL)
    if not cards or not locations:
        # A partial glossary is worse than a stale one: card names without
        # locations (or vice versa) would silently drop the missing half on the
        # next cache refresh.  Require both sources before rebuilding.
        return {}

    for card in cards:
        en = (card.get("originalName") or "").strip()
        cn = (card.get("name") or "").strip()
        if en and cn and en.lower() != cn.lower():
            glossary[en] = cn

    # Locations
    for loc in locations:
        en = (loc.get("originalName") or "").strip()
        cn = (loc.get("name") or "").strip()
        if en and cn and en.lower() != cn.lower():
            glossary[en] = cn

    # Add safe game keywords that actually appear in English card/location
    # text, so translators get the official Chinese term rather than a literal
    # translation.  Unsafe common words are intentionally left in the
    # separate game-term dictionary, not in this auto-applied glossary.
    for term, cn in _extract_game_terms_from_items(
        _fetch_json(_CARDS_EN_URL) + _fetch_json(_LOCATIONS_EN_URL)
    ).items():
        if term in _SNAP_AUTO_APPLY_TERMS:
            glossary[term] = cn

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
            _save_cache(cache_path, glossary, game_terms=_load_game_terms(cache_path))
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
                # Track the cache file's mtime for TTL — not now. The cache
                # may be months stale; max(now, mtime) would always yield
                # now and defeat TTL tracking (stale cache served forever).
                try:
                    _last_fetch_time = cache_path.stat().st_mtime
                except OSError:
                    _last_fetch_time = time.time()
            else:
                # No cache — must fetch synchronously
                glossary = _build_glossary()
                if glossary:
                    _glossary = glossary
                    _last_fetch_time = time.time()
                    _save_cache(cache_path, glossary, game_terms=_load_game_terms(cache_path))
                return _glossary or {}

        # Check if refresh is needed
        age = time.time() - _last_fetch_time
        if age >= ttl and not _fetch_in_progress:
            _fetch_in_progress = True
            t = threading.Thread(target=_background_refresh, args=(cache_path,), daemon=True)
            t.start()

        return _glossary or {}


def get_snap_game_terms() -> dict[str, str]:
    """Return the SNAP game-term EN→CN glossary.

    Unlike :func:`get_glossary`, this is not auto-applied to arbitrary text;
    callers can use it for display, search, or targeted keyword replacement.
    """
    if not config.SNAP_GLOSSARY_ENABLED:
        return {}
    return _load_game_terms(Path(config.SNAP_GLOSSARY_CACHE))


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

    # CRITICAL: if the EN lang file failed, ``en_name`` would be "" and the
    # glossary would get an empty-string key. _apply_glossary would then
    # re.sub(r"\b\b", ...) — inserting Chinese at every word boundary,
    # corrupting every title/description, and the bad cache persists.
    if not data_en:
        return glossary

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
                # Same mtime-based TTL as get_glossary — see note there.
                try:
                    _deadlock_last_fetch_time = cache_path.stat().st_mtime
                except OSError:
                    _deadlock_last_fetch_time = time.time()
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


# ── Brawl Stars Glossary (static data/brawl_stars_glossary.json) ─────

# Subset of game_terms safe to auto-apply to arbitrary subtitle text.
# Generic common words are excluded (Tank/Support/Assassin/Rare/Epic/...,
# and game_terms like "Bolt" whose brawler-name meaning wins) — mirrors
# SNAP's curated auto-apply terms.
_BS_AUTO_APPLY_GAME_TERMS = [
    "Star Power", "Hypercharge", "Gadget", "Gear",
    "Gem Grab", "Showdown", "Solo Showdown", "Duo Showdown", "Brawl Ball",
    "Heist", "Siege", "Hot Zone", "Knockout", "Wipeout", "Duels", "Bounty",
    "Starr Drop", "Chaos Drop", "Power Cube", "Brawl Box", "Mega Box",
    "Trophy Box", "Energy Drink", "Meteor Shower", "Teleporter",
    "Tier List", "Balance Changes", "Buff", "Nerf", "Brawler",
    "Gems", "Coins", "Power Points", "Bling", "XP Doublers",
    "Damage Dealer", "Super Rare",
]

_brawl_glossary: dict[str, str] | None = None
_brawl_glossary_lock = threading.Lock()


def get_brawl_stars_glossary() -> dict[str, str]:
    """Return the Brawl Stars EN→CN glossary for translation pre-replacement.

    Reads the static data/brawl_stars_glossary.json (built by
    tools/update_brawl_stars_glossary.py) — hero names plus a curated subset
    of game terms, plus multi-word verified ability names.  No network fetch.
    """
    global _brawl_glossary

    if not config.BRAWL_STARS_GLOSSARY_ENABLED:
        return {}

    with _brawl_glossary_lock:
        if _brawl_glossary is not None:
            return _brawl_glossary

        terms: dict[str, str] = {}
        try:
            data = json.loads(
                Path(config.BRAWL_STARS_GLOSSARY_CACHE).read_text(encoding="utf-8")
            )
            terms.update(data.get("glossary", {}))
            game_terms = data.get("game_terms", {})
            for key in _BS_AUTO_APPLY_GAME_TERMS:
                if key in game_terms:
                    terms[key] = game_terms[key]
            _add_bs_multiword_abilities(terms)
        except Exception:
            pass
        _brawl_glossary = terms
        return terms


def _add_bs_multiword_abilities(terms: dict[str, str]) -> None:
    """Add verified ability names with 2+ space-separated words from the
    client TID-join table.

    Single-word abilities ("Curveball", "Band-Aid", "Stomper"...) are common
    English words — too risky for blanket replacement.  Multi-word names
    ("Fast Forward", "Silver Bullet", "Slick Boots") are Brawl-Stars-specific
    enough to auto-apply in a BS video.
    """
    csv_path = (
        config.PROJECT_ROOT / "tools" / "_bs_data" / "brawl_stars_abilities_zh_cn.csv"
    )
    if not csv_path.exists():
        return
    try:
        with open(csv_path, encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f))
    except Exception:
        return
    for r in rows:
        if r.get("status") != "client_zh_cn+en_verified":
            continue
        en = (r.get("item_en") or "").strip()
        zh = (r.get("item_zh_cn") or "").strip()
        if len(en.split()) < 2 or not zh or en == zh:
            continue
        if en in terms and terms[en] != zh:
            continue  # keep the existing (brawler-name / curated) mapping
        if en not in terms:
            terms[en] = zh
