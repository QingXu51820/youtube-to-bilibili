"""
Bilibili 合集 (season) management.

Wraps the creator-center endpoints that ``bilibili-api-python`` does not
cover: list collections, create a collection, and add uploaded videos to a
collection.  Pure matching/mapping helpers live here too so they can be
unit-tested without network access.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import httpx
from yt2bili import config


# ── Constants ──────────────────────────────────────────────────────────

_COLLECTIONS_URL = "https://member.bilibili.com/x2/creative/web/seasons"
_CREATE_COLLECTION_URL = "https://member.bilibili.com/x2/creative/web/season/add"
_SECTION_URL = "https://member.bilibili.com/x2/creative/web/season/section"
_SECTION_EDIT_URL = "https://member.bilibili.com/x2/creative/web/season/section/edit"
_ADD_EPISODES_URL = (
    "https://member.bilibili.com/x2/creative/web/season/section/episodes/add"
)
_COVER_UP_URL = "https://member.bilibili.com/x/vu/web/cover/up"
_PAGELIST_URL = "https://api.bilibili.com/x/player/pagelist"
_VIDEO_VIEW_URL = "https://api.bilibili.com/x/web-interface/view"

_AUTH_ERROR_CODES = (401, 403)
_DEFAULT_TIMEOUT = 15.0
_COLLECTION_RETRY_INTERVAL = 3600  # 补归同一视频的最短重试间隔（秒）
_COLLECTION_ADD_DELAY = 3.0        # 每次补归提交之间的间隔（秒），避免触发 B站 限流
_COLLECTION_SWEEP_BUDGET = 30      # 单轮最多补归条数，剩余留待下一轮
_REORDER_DELAY = 3.0               # 每次合集重排提交之间的间隔（秒）
_RATE_LIMIT_CODES = (20111, 20113)  # 合集编辑过于频繁 / 手速太快啦～
_RATE_LIMIT_COOLDOWN = 300         # 限流条目重试冷却（秒）
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)


class BilibiliApiError(RuntimeError):
    """Bilibili API returned a non-zero ``code``; carries the code for retry logic."""

    def __init__(self, message: str, code: int = -1):
        super().__init__(message)
        self.code = code


# ── Data model ──────────────────────────────────────────────────────────

@dataclass
class CollectionInfo:
    """A Bilibili 合集 (season)."""
    season_id: int
    title: str
    section_id: int = 0    # 合集默认小节（"正片"）ID，加视频时使用
    total: int = 0         # 合集内视频数
    section_mtime: int = 0  # 默认小节修改时间（用于跳过未变化的合集）


@dataclass
class ChannelCollectionMatch:
    """Result of mapping one YouTube channel to a Bilibili collection."""
    channel_title: str
    collection_name: str
    season_id: int | None = None
    status: str = "to_create"  # "matched" | "to_create"


# ── Pure helpers ────────────────────────────────────────────────────────

def normalize_name(name: str | None) -> str:
    """Lowercase and strip whitespace/punctuation (CJK kept)."""
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", (name or "").lower())


def find_collection_by_name(
    collections: list[CollectionInfo], name: str
) -> CollectionInfo | None:
    """Return the collection whose normalized title equals *name*, or None."""
    target = normalize_name(name)
    for c in collections:
        if normalize_name(c.title) == target:
            return c
    return None


def resolve_channel_collections(
    pairs: list[tuple[str, str]],
    collections: list[CollectionInfo],
) -> list[ChannelCollectionMatch]:
    """
    Map ``(channel_title, configured_collection_name)`` pairs to collections.

    A channel without a configured name falls back to its channel title.
    """
    result: list[ChannelCollectionMatch] = []
    for channel_title, configured in pairs:
        name = (configured or channel_title or "").strip()
        found = find_collection_by_name(collections, name)
        result.append(
            ChannelCollectionMatch(
                channel_title=channel_title,
                collection_name=name,
                season_id=found.season_id if found else None,
                status="matched" if found else "to_create",
            )
        )
    return result


def build_episodes(
    aid: int,
    pages: list[dict],
    part_titles: list[str] | None = None,
) -> list[dict]:
    """
    Build the ``episodes`` payload for adding an uploaded video to a collection.

    ``pages`` come from the Bilibili pagelist API (``{"cid": int, "part": str}``);
    ``part_titles`` override the per-part titles when provided.
    """
    episodes = []
    for i, page in enumerate(pages):
        title = ""
        if part_titles and i < len(part_titles) and part_titles[i]:
            title = part_titles[i]
        if not title:
            title = page.get("part") or ""
        episodes.append(
            {
                "aid": aid,
                "cid": int(page.get("cid") or 0),
                "title": title,
                "charging_pay": 0,
            }
        )
    return episodes


def make_placeholder_cover(title: str) -> str:
    """
    Create a 1920x1080 placeholder JPEG for a new collection cover.

    Returns the absolute path to the generated image (cached per title).
    """
    from PIL import Image, ImageDraw, ImageFont

    out_dir = Path(tempfile.gettempdir()) / "yt2bili"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"collection_cover_{normalize_name(title) or 'untitled'}.jpg"
    if out.exists():
        return str(out)

    img = Image.new("RGB", (1920, 1080), (24, 28, 38))
    draw = ImageDraw.Draw(img)
    font = None
    for candidate in (
        "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/msyhbd.ttc",
        "C:/Windows/Fonts/simhei.ttf",
        "C:/Windows/Fonts/arial.ttf",
    ):
        try:
            font = ImageFont.truetype(candidate, 96)
            break
        except Exception:
            continue
    if font is None:
        font = ImageFont.load_default()
    text = title or "合集"
    bbox = draw.textbbox((0, 0), text, font=font)
    w = bbox[2] - bbox[0]
    h = bbox[3] - bbox[1]
    draw.text(
        ((1920 - w) / 2 - bbox[0], (1080 - h) / 2 - bbox[1]),
        text,
        fill=(245, 245, 245),
        font=font,
    )
    img.save(out, "JPEG", quality=90)
    return str(out)


# ── HTTP helpers ────────────────────────────────────────────────────────

def _headers() -> dict:
    return {
        "User-Agent": _UA,
        "Referer": "https://member.bilibili.com",
        "Origin": "https://member.bilibili.com",
    }


def _cookies(credential) -> dict:
    cookies = {}
    sessdata = getattr(credential, "sessdata", "") or ""
    bili_jct = getattr(credential, "bili_jct", "") or ""
    buvid3 = getattr(credential, "buvid3", "") or ""
    if sessdata:
        cookies["SESSDATA"] = sessdata
    if bili_jct:
        cookies["bili_jct"] = bili_jct
    if buvid3:
        cookies["buvid3"] = buvid3
    return cookies


def _check_response(resp: httpx.Response, label: str) -> dict:
    """Check a creator-center response; raise on auth/API errors."""
    if resp.status_code in _AUTH_ERROR_CODES:
        raise RuntimeError(
            f"B站登录凭据已过期（HTTP {resp.status_code}），请重新扫码登录。\n"
            f"运行: python main.py --login"
        )
    try:
        data = resp.json()
    except Exception as e:
        raise RuntimeError(f"{label} 返回非 JSON 响应: {e}")
    code = data.get("code", -1)
    if code != 0:
        raise BilibiliApiError(
            f"{label}失败: {data.get('message', str(data))} (code={code})",
            code=code,
        )
    return data


def _client(credential) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        headers=_headers(),
        cookies=_cookies(credential),
        timeout=_DEFAULT_TIMEOUT,
    )


async def list_collections(credential) -> list[CollectionInfo]:
    """Fetch all 合集 (seasons) owned by the credential's account."""
    collections: list[CollectionInfo] = []
    pn = 1
    async with _client(credential) as client:
        while True:
            resp = await client.get(
                _COLLECTIONS_URL,
                params={"pn": pn, "ps": 50, "order": "mtime", "sort": "desc"},
            )
            data = _check_response(resp, "获取合集列表")
            body = data.get("data") or {}
            seasons = body.get("seasons") or []
            for s in seasons:
                season = s.get("season") or {}
                sections = ((s.get("sections") or {}).get("sections")) or []
                collections.append(
                    CollectionInfo(
                        season_id=int(season.get("id") or 0),
                        title=str(season.get("title") or ""),
                        section_id=int(sections[0].get("id") or 0)
                        if sections else 0,
                        total=sum(
                            int(sec.get("epCount") or 0) for sec in sections
                        ),
                        section_mtime=int(sections[0].get("mtime") or 0)
                        if sections else 0,
                    )
                )
            total = int(body.get("total") or 0)
            if not seasons or pn * 50 >= total:
                break
            pn += 1
    return collections


def sync_list_collections(credential) -> list[CollectionInfo]:
    """Synchronous wrapper for ``list_collections`` (CLI use)."""
    return asyncio.run(list_collections(credential))


async def upload_cover(credential, cover_path: str) -> str:
    """Upload an image via the cover endpoint and return its Bilibili URL."""
    raw = Path(cover_path).read_bytes()
    mime = (
        "image/png"
        if str(cover_path).lower().endswith(".png")
        else "image/jpeg"
    )
    b64 = base64.b64encode(raw).decode("ascii")
    body = {
        "csrf": getattr(credential, "bili_jct", "") or "",
        "cover": f"data:{mime};base64,{b64}",
    }
    async with _client(credential) as client:
        resp = await client.post(
            _COVER_UP_URL,
            params={"ts": int(time.time() * 1000)},
            data=body,
        )
        data = _check_response(resp, "上传合集封面")
    return str((data.get("data") or {}).get("url") or "")


async def create_collection(credential, title: str, cover_url: str) -> int:
    """Create a new 合集 and return its season_id."""
    body = {
        "title": title,
        "desc": "",
        "cover": cover_url,
        "season_price": 0,
        "csrf": getattr(credential, "bili_jct", "") or "",
    }
    async with _client(credential) as client:
        resp = await client.post(_CREATE_COLLECTION_URL, data=body)
        data = _check_response(resp, "创建合集")
    return int(data.get("data") or 0)


async def fetch_video_pages(credential, bvid: str) -> list[dict]:
    """Return ``[{"cid": int, "part": str}]`` for an uploaded video."""
    async with _client(credential) as client:
        resp = await client.get(_PAGELIST_URL, params={"bvid": bvid})
        data = _check_response(resp, "获取分P信息")
    pages = data.get("data") or []
    return [
        {"cid": int(p.get("cid") or 0), "part": str(p.get("part") or "")}
        for p in pages
    ]


# ── Deferred collection queue ─────────────────────────────────────────

def _now_iso() -> str:
    """UTC ISO-8601 second-precision string (same format as monitor.utc_now)."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_iso(value: str) -> datetime | None:
    """Parse an ISO-8601 timestamp, or None when missing/invalid."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _is_rate_limited(entry: dict) -> bool:
    """True when the entry's last error was a B站 collection rate limit."""
    error = entry.get("last_error") or ""
    return any(str(code) in error for code in _RATE_LIMIT_CODES)


def _entry_cooldown(
    entry: dict, retry_interval_seconds: int, rate_limit_cooldown_seconds: int
) -> int:
    """Cooldown for an entry: short for rate-limited, long otherwise."""
    if _is_rate_limited(entry):
        return rate_limit_cooldown_seconds
    return retry_interval_seconds


def _sweep_lock_path(queue_path: Path) -> Path:
    return queue_path.with_suffix(queue_path.suffix + ".lock")


def _acquire_sweep_lock(queue_path: Path, stale_seconds: int = 1800) -> bool:
    """Exclusive lock so concurrent sweeps (monitor + manual loop) don't collide."""
    path = _sweep_lock_path(queue_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        try:
            age = time.time() - path.stat().st_mtime
        except OSError:
            age = 0.0
        if age < stale_seconds:
            return False
        try:
            path.unlink()
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except OSError:
            return False
    try:
        os.write(fd, f"{os.getpid()} {_now_iso()}\n".encode("utf-8"))
    finally:
        os.close(fd)
    return True


def _release_sweep_lock(queue_path: Path) -> None:
    try:
        _sweep_lock_path(queue_path).unlink()
    except OSError:
        pass


def pending_collections_path() -> Path:
    """
    Path of the pending-collection queue for the active profile.

    Named profiles keep their own queue under ``state/{profile}/``; legacy
    .env mode keeps the shared ``state/pending_collections.json``.
    """
    from yt2bili import profile as profile_mod
    root = Path(config.PROJECT_ROOT)
    if not profile_mod.is_profile_state_active():
        return root / "state" / "pending_collections.json"
    return root / "state" / profile_mod.get_active_profile_name() / "pending_collections.json"


def load_pending_collections(path: Path) -> list[dict]:
    """Read the queue; back up and rebuild when corrupted."""
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, OSError) as e:
        try:
            backup = path.with_suffix(path.suffix + ".bak")
            backup.write_bytes(path.read_bytes())
            print(f"[合集] ⚠️ 待补归队列损坏（{e}），已备份到 {backup.name} 并重建空队列")
        except OSError:
            print(f"[合集] ⚠️ 待补归队列损坏（{e}），重建空队列")
        return []
    return data if isinstance(data, list) else []


def save_pending_collections(path: Path, entries: list[dict]) -> None:
    """Atomically write the queue."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(entries, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    tmp.replace(path)


def enqueue_collection(
    *,
    collection: str,
    bvid: str,
    aid: int,
    video_id: str = "",
    channel_title: str = "",
    published_at: str = "",
) -> None:
    """Record a just-uploaded video that still needs to join a 合集."""
    path = pending_collections_path()
    entries = load_pending_collections(path)
    entry = {
        "video_id": video_id,
        "bvid": bvid,
        "aid": int(aid or 0),
        "collection_name": collection,
        "channel_title": channel_title,
        "published_at": published_at or "",
        "added_at": _now_iso(),
        "last_attempt_at": "",
        "attempts": 0,
        "status": "pending",
        "last_error": "",
    }
    if video_id:
        for i, existing in enumerate(entries):
            if existing.get("video_id") == video_id:
                if existing.get("status") == "added":
                    return  # already in a collection
                entry["published_at"] = (
                    existing.get("published_at") or published_at or ""
                )
                entries[i] = entry
                save_pending_collections(path, entries)
                return
    entries.append(entry)
    save_pending_collections(path, entries)


def backfill_collections(
    queue_path: Path,
    state_path: Path,
    resolve_collection_name=None,
) -> int:
    """
    Queue every ``uploaded`` video from *state_path* that has a bvid and is
    not already queued/added.  Returns the number of new queue entries.
    """
    from yt2bili import profile as profile_mod
    if resolve_collection_name is None:
        resolve_collection_name = profile_mod.resolve_collection_name
    if not state_path.exists():
        return 0
    try:
        state = json.loads(state_path.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, OSError) as e:
        print(f"[合集] ⚠️ 读取历史记录失败（{e}），跳过回填: {state_path}")
        return 0
    videos = (state or {}).get("videos", {}) if isinstance(state, dict) else {}

    entries = load_pending_collections(queue_path)
    queued_ids = {e.get("video_id", "") for e in entries}
    added = 0
    skipped_unattributed = 0
    for video_id, v in videos.items():
        if not video_id or video_id in queued_ids:
            continue
        if str(v.get("status", "")) != "uploaded":
            continue
        bvid = str(v.get("bvid", "") or "")
        if not bvid:
            continue
        channel_title = str(v.get("channel_title", "") or "")
        collection_name = (resolve_collection_name(channel_title) or "").strip()
        if not collection_name:
            skipped_unattributed += 1
            continue
        entries.append({
            "video_id": video_id,
            "bvid": bvid,
            "aid": int(v.get("aid", 0) or 0),
            "collection_name": collection_name,
            "channel_title": channel_title,
            "published_at": str(v.get("published_at", "") or ""),
            "added_at": _now_iso(),
            "last_attempt_at": "",
            "attempts": 0,
            "status": "pending",
            "last_error": "",
        })
        queued_ids.add(video_id)
        added += 1
        print(f"[合集] 回填: {v.get('title', '')} → 合集「{collection_name}」 ({bvid})")
    if added:
        save_pending_collections(queue_path, entries)
    if skipped_unattributed:
        print(
            f"[合集] 跳过 {skipped_unattributed} 条无法确定归属频道的历史记录"
            "（不在当前账号频道列表或缺少频道信息）"
        )
    return added


def enrich_queue_dates(queue_path: Path, state_path: Path) -> int:
    """
    Copy ``published_at`` from the processed-videos state into queue entries
    that are missing it (keyed by video_id / bvid).  Returns the count filled.
    """
    if not state_path.exists():
        return 0
    try:
        state = json.loads(state_path.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, OSError) as e:
        print(f"[合集] ⚠️ 读取历史记录失败（{e}），跳过发布时间回填: {state_path}")
        return 0
    videos = (state or {}).get("videos", {}) if isinstance(state, dict) else {}

    entries = load_pending_collections(queue_path)
    by_id = {
        vid: str(v.get("published_at", "") or "")
        for vid, v in videos.items()
        if v.get("published_at")
    }
    by_bvid = {
        str(v.get("bvid", "") or ""): str(v.get("published_at", "") or "")
        for v in videos.values()
        if v.get("bvid") and v.get("published_at")
    }

    filled = 0
    for entry in entries:
        if entry.get("published_at"):
            continue
        date = (
            by_id.get(str(entry.get("video_id", "") or ""), "")
            or by_bvid.get(str(entry.get("bvid", "") or ""), "")
        )
        if date:
            entry["published_at"] = date
            filled += 1
    if filled:
        save_pending_collections(queue_path, entries)
    return filled


def enrich_missing_channels(state_path: Path, resolve_channel) -> int:
    """
    Fill empty ``channel_title`` for uploaded state entries via YouTube metadata.

    *resolve_channel* takes a YouTube video id and returns
    ``(channel_title, channel_id)``.  Results are persisted back into the
    state file so later sweeps never re-fetch the same video.
    """
    if not state_path.exists():
        return 0
    try:
        state = json.loads(state_path.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, OSError) as e:
        print(f"[合集] ⚠️ 读取历史记录失败（{e}），跳过频道反查: {state_path}")
        return 0
    videos = (state or {}).get("videos", {}) if isinstance(state, dict) else {}

    enriched = 0
    for video_id, v in videos.items():
        if not video_id:
            continue
        if str(v.get("status", "")) != "uploaded":
            continue
        if (v.get("channel_title") or "").strip():
            continue
        if not str(v.get("url", "") or ""):
            continue
        try:
            title, channel_id = resolve_channel(video_id)
        except Exception as e:
            print(f"[合集] ⚠️ 反查频道失败 {video_id}: {e}")
            continue
        v["channel_title"] = str(title or "").strip()
        if channel_id:
            v["channel_id"] = str(channel_id)
        enriched += 1
        print(f"[合集] 反查频道: {video_id} → {v['channel_title']}")

    if enriched:
        tmp = state_path.with_suffix(state_path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(state, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        tmp.replace(state_path)
    return enriched


# ── 合集内视频排序（按发布时间） ────────────────────────────────

def _normalize_publish_date(value) -> str:
    """
    Normalize a publish date to ``YYYY-MM-DD`` for lexicographic sorting.

    Accepts yt-dlp ``upload_date`` (YYYYMMDD), ISO-8601 timestamps, and
    epoch seconds (B站 pubdate).  Returns "" when the value is unusable.
    """
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    match = re.fullmatch(r"(\d{4})[-/]?(\d{2})[-/]?(\d{2})", text)
    if match:
        return f"{match.group(1)}-{match.group(2)}-{match.group(3)}"
    dt = _parse_iso(text)
    if dt is not None:
        return dt.date().isoformat()
    if text.isdigit():
        try:
            return (
                datetime.fromtimestamp(int(text), tz=timezone.utc)
                .date()
                .isoformat()
            )
        except (OverflowError, OSError, ValueError):
            return ""
    return ""


def build_reorder_sorts(
    episodes: list[dict],
    published_at_map: dict | None = None,
    reverse: bool = False,
) -> list[dict] | None:
    """
    Build the ``sorts`` payload that orders episodes by publish date.

    Episodes without a known date keep their relative order and move to the
    end.  Returns None when the collection is already in the target order
    (or episode ids are missing) so callers can skip the edit API call.
    """
    published_at_map = published_at_map or {}

    def date_of(ep: dict) -> str:
        bvid = str(ep.get("bvid", "") or "")
        aid = str(ep.get("aid", 0) or 0)
        return (
            published_at_map.get(bvid)
            or published_at_map.get(aid)
            or _normalize_publish_date(ep.get("published_at"))
            or "9999-99-99"
        )

    current = [int(ep.get("id") or 0) for ep in episodes]
    if any(not cid for cid in current):
        return None  # no episode ids → cannot reorder safely

    indexed = list(enumerate(episodes))
    known = [t for t in indexed if date_of(t[1]) != "9999-99-99"]
    unknown = [t for t in indexed if date_of(t[1]) == "9999-99-99"]
    known.sort(key=lambda t: (date_of(t[1]), t[0]), reverse=reverse)
    ordered = known + unknown
    target = [int(ep.get("id") or 0) for _, ep in ordered]
    if target == current:
        return None
    return [{"id": ep_id, "sort": i + 1} for i, ep_id in enumerate(target)]


def _reorder_cache_path(queue_path: Path) -> Path:
    """Cache of bvid → YouTube publish date (from yt-dlp metadata)."""
    return queue_path.parent / "bvid_dates.json"


def _reorder_markers_path(queue_path: Path) -> Path:
    """Per-season mtime markers so full reorder passes skip unchanged 合集."""
    return queue_path.parent / "collections_reorder.json"


def _collect_published_dates(
    state_path: Path, queue_path: Path
) -> tuple[dict, set, dict]:
    """
    Gather per-video date knowledge for reordering.

    Returns ``(dates, yt_bvids, bvid_to_video_id)``:
    - *dates*: bvid/aid → ``YYYY-MM-DD`` (YouTube dates preferred, cached)
    - *yt_bvids*: bvids whose date is YouTube-derived (state/queue/cache)
    - *bvid_to_video_id*: bvid → YouTube video id, used to look up dates
    """
    dates: dict[str, str] = {}
    yt_bvids: set[str] = set()
    bvid_to_video_id: dict[str, str] = {}

    def add(key, value) -> None:
        date = _normalize_publish_date(value)
        if key and date:
            dates.setdefault(str(key), date)

    if state_path.exists():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8-sig"))
        except (json.JSONDecodeError, OSError):
            state = {}
        if isinstance(state, dict):
            for v in (state.get("videos") or {}).values():
                if not isinstance(v, dict):
                    continue
                add(v.get("bvid"), v.get("published_at"))
                if v.get("published_at") and v.get("bvid"):
                    yt_bvids.add(str(v.get("bvid")))
                if v.get("bvid") and v.get("video_id"):
                    bvid_to_video_id.setdefault(
                        str(v.get("bvid")), str(v.get("video_id"))
                    )
    for entry in load_pending_collections(queue_path):
        add(entry.get("bvid"), entry.get("published_at"))
        if entry.get("published_at") and entry.get("bvid"):
            yt_bvids.add(str(entry.get("bvid")))
        if entry.get("bvid") and entry.get("video_id"):
            bvid_to_video_id.setdefault(
                str(entry.get("bvid")), str(entry.get("video_id"))
            )
    cache = _reorder_cache_path(queue_path)
    if cache.exists():
        try:
            cached = json.loads(cache.read_text(encoding="utf-8-sig"))
        except (json.JSONDecodeError, OSError):
            cached = {}
        if isinstance(cached, dict):
            for key, value in cached.items():
                add(key, value)
                if str(key).startswith("BV"):
                    yt_bvids.add(str(key))
    return dates, yt_bvids, bvid_to_video_id


async def fetch_collection_section(
    credential,
    section_id: int,
    client=None,
) -> tuple[dict, list[dict]]:
    """Return ``(section, episodes)`` for a collection's default section."""

    async def _fetch(active_client):
        resp = await active_client.get(
            _SECTION_URL, params={"id": section_id}
        )
        data = _check_response(resp, "获取合集小节")
        body = data.get("data") or {}
        return (body.get("section") or {}), (body.get("episodes") or [])

    if client is not None:
        return await _fetch(client)
    async with _client(credential) as own:
        return await _fetch(own)


def _extract_youtube_url(desc: str) -> str:
    """Extract a YouTube video id from a B站 description, or "" when absent."""
    match = re.search(
        r"(?:https?://)?(?:www\.|m\.)?youtu\.?be(?:\.com)?/"
        r"(?:watch\?v=|shorts/|embed/)?([A-Za-z0-9_-]{11})",
        desc or "",
    )
    if not match:
        return ""
    return match.group(1)


async def _fill_youtube_api_pubdates(
    dates: dict,
    yt_bvids: set,
    video_id_to_bvid: dict,
    youtube=None,
) -> int:
    """
    Fill YouTube publish dates via the YouTube Data API (batches of 50).

    *video_id_to_bvid* maps YouTube video ids to B站 bvids.  Returns the
    number of dates filled.  Best-effort: API errors are swallowed so the
    reorder can fall back to B站 upload dates.
    """
    if youtube is None or not video_id_to_bvid:
        return 0
    video_ids = list(video_id_to_bvid)
    filled = 0
    for start in range(0, len(video_ids), 50):
        chunk = video_ids[start:start + 50]

        def _call():
            return (
                youtube.videos()
                .list(part="snippet", id=",".join(chunk), maxResults=50)
                .execute()
            )

        try:
            resp = await asyncio.to_thread(_call)
        except Exception:
            continue
        for item in resp.get("items", []):
            video_id = str(item.get("id", "") or "")
            bvid = video_id_to_bvid.get(video_id, "")
            published = (item.get("snippet") or {}).get("publishedAt", "")
            date = _normalize_publish_date(published)
            if bvid and date:
                dates[bvid] = date
                yt_bvids.add(bvid)
                filled += 1
    return filled


async def _fill_bili_pubdates(
    client,
    episodes: list[dict],
    dates: dict,
) -> tuple[set, dict]:
    """
    Fetch B站 pubdate for episodes whose bvid/aid has no known date yet.

    Results are written into *dates* (bvid and aid keys).  Paced to avoid
    hammering the public view API.  Returns ``(bili_only, desc_video_ids)``:
    the bvids that only got a B站 date, and a bvid → YouTube video-id map
    recovered from each description.
    """
    missing: list[tuple[str, str]] = []
    for ep in episodes:
        bvid = str(ep.get("bvid", "") or "")
        aid = str(ep.get("aid", 0) or 0)
        if not bvid:
            continue
        if dates.get(bvid) or dates.get(aid):
            continue
        missing.append((bvid, aid))

    bili_only: set[str] = set()
    desc_video_ids: dict[str, str] = {}
    semaphore = asyncio.Semaphore(6)

    async def _fetch_one(bvid: str, aid: str) -> None:
        async with semaphore:
            pub = None
            desc = ""
            try:
                resp = await client.get(
                    _VIDEO_VIEW_URL, params={"bvid": bvid}
                )
                payload = resp.json() if resp.status_code == 200 else {}
                if payload.get("code") == 0:
                    body = payload.get("data") or {}
                    pub = body.get("pubdate")
                    desc = str(body.get("desc") or "")
            except Exception:
                pub = None
            date = _normalize_publish_date(pub)
            if date:
                dates[bvid] = date
                if aid:
                    dates[aid] = date
                bili_only.add(bvid)
            video_id = _extract_youtube_url(desc)
            if video_id:
                desc_video_ids[bvid] = video_id

    if missing:
        await asyncio.gather(*(_fetch_one(b, a) for b, a in missing))
    return bili_only, desc_video_ids


async def _fill_missing_dates(
    client,
    episodes: list[dict],
    dates: dict,
    yt_bvids: set,
    bvid_to_video_id: dict,
    youtube=None,
) -> tuple[set, dict]:
    """
    Fill dates for every episode: YouTube Data API first (local + desc
    mappings), then B站 upload date as fallback.  Returns the bvids that
    only have a B站 date, plus the desc-recovered video-id map.
    """
    local_map = {
        bvid: video_id for bvid, video_id in bvid_to_video_id.items()
        if not (dates.get(bvid) or "")
    }
    video_id_to_bvid = {video_id: bvid for bvid, video_id in local_map.items()}
    await _fill_youtube_api_pubdates(dates, yt_bvids, video_id_to_bvid, youtube)

    bili_only, desc_video_ids = await _fill_bili_pubdates(
        client, episodes, dates
    )
    desc_map = {
        bvid: video_id for bvid, video_id in desc_video_ids.items()
        if not (dates.get(bvid) or "")
    }
    video_id_to_bvid = {video_id: bvid for bvid, video_id in desc_map.items()}
    await _fill_youtube_api_pubdates(dates, yt_bvids, video_id_to_bvid, youtube)
    return bili_only, desc_video_ids


async def reorder_collection_section(
    credential,
    section: dict,
    episodes: list[dict],
    published_at_map: dict | None = None,
    reverse: bool = False,
    client=None,
) -> dict:
    """
    Reorder a collection's default section by publish date (newest last
    unless *reverse*).  Returns ``{"changed": bool, "season_id", "episodes"}``.
    """
    sorts = build_reorder_sorts(episodes, published_at_map, reverse=reverse)
    season_id = int(section.get("seasonId") or 0)
    if not sorts:
        return {"changed": False, "season_id": season_id, "episodes": len(episodes)}

    payload = {
        "section": {
            "id": int(section.get("id") or 0),
            "type": 1,
            "seasonId": season_id,
            "title": str(section.get("title") or "正片"),
        },
        "sorts": sorts,
        "captcha_token": "",
        "csrf": getattr(credential, "bili_jct", "") or "",
    }
    csrf = getattr(credential, "bili_jct", "") or ""

    async def _submit(active_client):
        resp = await active_client.post(
            _SECTION_EDIT_URL,
            params={"csrf": csrf},
            json=payload,
        )
        return _check_response(resp, "重排合集")

    if client is not None:
        await _submit(client)
    else:
        async with _client(credential) as own:
            await _submit(own)
    return {"changed": True, "season_id": season_id, "episodes": len(episodes)}


def _save_reorder_markers(queue_path: Path, markers: dict) -> None:
    path = _reorder_markers_path(queue_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(markers, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    tmp.replace(path)


def _save_bvid_date_cache(queue_path: Path, dates: dict, yt_bvids: set) -> None:
    """Persist bvid → YouTube date entries so later reorders skip refetches."""
    bvid_dates = {
        key: value for key, value in dates.items()
        if str(key).startswith("BV") and str(key) in yt_bvids and value
    }
    if not bvid_dates:
        return
    path = _reorder_cache_path(queue_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        existing = json.loads(path.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, OSError):
        existing = {}
    if not isinstance(existing, dict):
        existing = {}
    existing.update(bvid_dates)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(existing, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    tmp.replace(path)


async def _reorder_touched_collections(
    credential,
    touched: dict,
    state_path: Path,
    queue_path: Path,
    reverse: bool = False,
    youtube=None,
) -> None:
    """Reorder every collection that received a new episode this sweep."""
    if not touched:
        return
    dates, yt_bvids, bvid_to_video_id = _collect_published_dates(
        state_path, queue_path
    )
    async with _client(credential) as client:
        for season_id, (section_id, name) in touched.items():
            try:
                section, episodes = await fetch_collection_section(
                    credential, section_id, client=client
                )
                if not section.get("id"):
                    print(f"[合集] ⚠️ 合集「{name}」小节信息缺失，跳过重排")
                    continue
                await _fill_missing_dates(
                    client, episodes, dates, yt_bvids, bvid_to_video_id,
                    youtube=youtube,
                )
                result = await reorder_collection_section(
                    credential, section, episodes, dates,
                    reverse=reverse, client=client,
                )
            except BilibiliApiError as e:
                if e.code in _RATE_LIMIT_CODES:
                    print(
                        f"[合集] ⏸️ 重排限流（code={e.code}），剩余合集留待下轮"
                    )
                    return
                print(f"[合集] ⚠️ 重排「{name}」失败: {e}")
                continue
            except Exception as e:
                print(f"[合集] ⚠️ 重排「{name}」失败: {e}")
                continue
            if result.get("changed"):
                print(
                    f"[合集] 🔀 已按发布时间重排「{name}」"
                    f"({result.get('episodes', 0)} 集)"
                )
                await asyncio.sleep(_REORDER_DELAY)
            else:
                print(f"[合集] ✓ 「{name}」顺序已正确")
    _save_bvid_date_cache(queue_path, dates, yt_bvids)


def reorder_collections(
    credential,
    *,
    state_path: Path | None = None,
    reverse: bool = False,
    youtube=None,
) -> tuple[int, int]:
    """
    Full reorder pass over every collection owned by the active profile.

    Skips collections whose default-section mtime is unchanged since the
    last pass (recorded in ``collections_reorder.json``).  Returns
    ``(reordered, already_ordered)``.
    """
    queue_path = pending_collections_path()
    if not _acquire_sweep_lock(queue_path):
        print("[合集] ⏸️ 另一进程正在执行补归，跳过全量重排")
        return 0, 0
    try:
        resolved_state = state_path or (queue_path.parent / "processed_videos.json")
        dates, yt_bvids, bvid_to_video_id = _collect_published_dates(
            resolved_state, queue_path
        )
        markers_path = _reorder_markers_path(queue_path)
        markers: dict = {}
        if markers_path.exists():
            try:
                markers = json.loads(
                    markers_path.read_text(encoding="utf-8-sig")
                ) or {}
            except (json.JSONDecodeError, OSError):
                markers = {}
        collections = sync_list_collections(credential)

        async def run() -> tuple[int, int]:
            reordered = already = 0
            async with _client(credential) as client:
                for c in collections:
                    if not c.section_id:
                        continue
                    marker = markers.get(str(c.season_id))
                    if (
                        isinstance(marker, dict)
                        and marker.get("mtime") == c.section_mtime
                        and marker.get("complete")
                    ):
                        continue
                    section_mtime = c.section_mtime
                    try:
                        section, episodes = await fetch_collection_section(
                            credential, c.section_id, client=client
                        )
                        if not section.get("id"):
                            print(
                                f"[合集] ⚠️ 「{c.title}」小节信息缺失，跳过重排"
                            )
                            markers[str(c.season_id)] = {
                                "mtime": section_mtime, "complete": True,
                            }
                            continue
                        _, desc_ids = await _fill_missing_dates(
                            client, episodes, dates, yt_bvids,
                            bvid_to_video_id, youtube=youtube,
                        )
                        missing_after = [
                            e for e in episodes
                            if not (
                                dates.get(str(e.get("bvid", "") or ""))
                                or dates.get(str(e.get("aid", 0) or 0))
                            )
                        ]
                        fillable_remaining = [
                            e for e in episodes
                            if (
                                str(e.get("bvid", "") or "")
                                in bvid_to_video_id
                                or str(e.get("bvid", "") or "")
                                in desc_ids
                            )
                            and str(e.get("bvid", "") or "") not in yt_bvids
                        ]
                        result = await reorder_collection_section(
                            credential, section, episodes, dates,
                            reverse=reverse, client=client,
                        )
                    except BilibiliApiError as e:
                        if e.code in _RATE_LIMIT_CODES:
                            print(
                                f"[合集] ⏸️ 重排限流（code={e.code}），"
                                "剩余合集留待下轮"
                            )
                            break
                        print(f"[合集] ⚠️ 重排「{c.title}」失败: {e}")
                        markers[str(c.season_id)] = {
                            "mtime": section_mtime, "complete": False,
                        }
                        continue
                    except Exception as e:
                        print(f"[合集] ⚠️ 重排「{c.title}」失败: {e}")
                        markers[str(c.season_id)] = {
                            "mtime": section_mtime, "complete": False,
                        }
                        continue
                    markers[str(c.season_id)] = {
                        "mtime": section_mtime,
                        "complete": (
                            not missing_after and not fillable_remaining
                        ),
                    }
                    if result.get("changed"):
                        reordered += 1
                        print(
                            f"[合集] 🔀 已按发布时间重排「{c.title}」"
                            f"({result.get('episodes', 0)} 集)"
                        )
                        await asyncio.sleep(_REORDER_DELAY)
                    else:
                        already += 1
                        print(f"[合集] ✓ 「{c.title}」顺序已正确")
            return reordered, already

        reordered, already = asyncio.run(run())
        _save_reorder_markers(queue_path, markers)
        _save_bvid_date_cache(queue_path, dates, yt_bvids)
        return reordered, already
    finally:
        _release_sweep_lock(queue_path)


async def add_video_to_collection(
    credential, section_id: int, episodes: list[dict]
) -> dict:
    """Add one or more episodes to a collection's default section."""
    payload = {
        "sectionId": section_id,
        "episodes": episodes,
        "csrf": getattr(credential, "bili_jct", "") or "",
    }
    async with _client(credential) as client:
        resp = await client.post(
            _ADD_EPISODES_URL,
            params={"csrf": getattr(credential, "bili_jct", "") or ""},
            json=payload,
        )
        return _check_response(resp, "加入合集")


async def ensure_collection(
    credential, name: str, cover_path: str | None
) -> tuple[CollectionInfo, bool]:
    """
    Return the collection matching *name*, creating it when missing.

    Returns ``(info, created)``.  New collections use *cover_path* (or a
    generated placeholder) as their cover.
    """
    found = find_collection_by_name(await list_collections(credential), name)
    if found is not None:
        return found, False

    cover_path = cover_path or make_placeholder_cover(name)
    cover_url = await upload_cover(credential, cover_path)
    season_id = await create_collection(credential, name, cover_url)
    found = find_collection_by_name(await list_collections(credential), name)
    if found is None:
        raise RuntimeError(
            f"合集「{name}」已创建但重新查询失败（season_id={season_id}）"
        )
    return found, True


async def add_uploaded_video_to_collection(
    credential,
    collection_name: str,
    cover_path: str | None,
    bvid: str,
    aid: int,
    part_titles: list[str] | None = None,
) -> dict:
    """
    Ensure *collection_name* exists, then add the uploaded video to it.

    Returns a summary dict with season_id/section_id/created/episodes.
    """
    info, created = await ensure_collection(
        credential, collection_name, cover_path
    )
    pages = await fetch_video_pages(credential, bvid)
    if not pages:
        raise RuntimeError("无法获取视频分P信息（cid），未加入合集")
    episodes = build_episodes(aid, pages, part_titles)
    await add_video_to_collection(credential, info.section_id, episodes)
    return {
        "season_id": info.season_id,
        "section_id": info.section_id,
        "created": created,
        "episodes": len(episodes),
    }


async def _sweep_pending_collections(
    credential,
    queue_path: Path,
    entries: list[dict],
    retry_interval_seconds: int,
    rate_limit_cooldown_seconds: int = _RATE_LIMIT_COOLDOWN,
    state_path: Path | None = None,
    reorder_touched: bool = True,
    reverse: bool = False,
    youtube=None,
) -> tuple[int, int, int]:
    """One sweep over the queue; updates entries in place and persists."""
    now = datetime.now(timezone.utc)
    changed = False
    added_count = 0
    touched: dict[int, tuple[int, str]] = {}
    for entry in entries:
        if entry.get("status") == "added":
            continue
        last = _parse_iso(entry.get("last_attempt_at") or "")
        if last is not None and \
                (now - last).total_seconds() < _entry_cooldown(
                    entry, retry_interval_seconds, rate_limit_cooldown_seconds
                ):
            continue

        bvid = str(entry.get("bvid", "") or "")
        if not bvid:
            entry["status"] = "failed"
            entry["last_error"] = "缺少 bvid"
            entry["last_attempt_at"] = _now_iso()
            changed = True
            continue

        entry["attempts"] = int(entry.get("attempts", 0) or 0) + 1
        entry["last_attempt_at"] = _now_iso()
        try:
            pages = await fetch_video_pages(credential, bvid)
        except BilibiliApiError as e:
            if e.code == -404:
                entry["status"] = "pending"
                entry["last_error"] = ""
            else:
                entry["status"] = "failed"
                entry["last_error"] = str(e)
            changed = True
            continue
        except RuntimeError as e:
            # HTTP 401/403 → _check_response raises with re-login hint
            entry["status"] = "failed"
            entry["last_error"] = str(e)
            changed = True
            if "重新扫码登录" in str(e):
                print(f"[合集] 🔐 {e}")
            continue
        except Exception as e:
            entry["status"] = "pending"  # transient network errors → retry later
            entry["last_error"] = str(e)
            changed = True
            continue

        if not pages:
            entry["status"] = "pending"
            entry["last_error"] = ""
            changed = True
            continue

        try:
            info = await add_uploaded_video_to_collection(
                credential,
                str(entry.get("collection_name", "") or ""),
                None,
                bvid,
                int(entry.get("aid", 0) or 0),
            )
        except BilibiliApiError as e:
            entry["status"] = "pending"
            entry["last_error"] = str(e)
            changed = True
            if e.code in _RATE_LIMIT_CODES:
                print(
                    f"[合集] ⏸️ B站补归限流（code={e.code}），本轮暂停，"
                    "剩余留待下轮重试"
                )
                break
            continue
        except Exception as e:
            entry["status"] = "pending"
            entry["last_error"] = str(e)
            changed = True
            continue

        entry["status"] = "added"
        entry["last_error"] = ""
        changed = True
        added_count += 1
        if info.get("section_id"):
            touched.setdefault(
                info["season_id"],
                (
                    info["section_id"],
                    str(entry.get("collection_name", "") or ""),
                ),
            )
        print(
            f"[合集] ✅ 已归入合集「{entry.get('collection_name', '')}」"
            f" ({bvid}, id={info['season_id']})"
        )
        if added_count >= _COLLECTION_SWEEP_BUDGET:
            print(
                f"[合集] 本轮已达补归上限 {_COLLECTION_SWEEP_BUDGET} 条，"
                "剩余留待下轮"
            )
            break
        await asyncio.sleep(_COLLECTION_ADD_DELAY)

    if changed:
        save_pending_collections(queue_path, entries)
    if reorder_touched and touched:
        await _reorder_touched_collections(
            credential,
            touched,
            state_path or (queue_path.parent / "processed_videos.json"),
            queue_path,
            reverse=reverse,
            youtube=youtube,
        )
    added = sum(1 for e in entries if e.get("status") == "added")
    pending = sum(1 for e in entries if e.get("status") == "pending")
    failed = sum(1 for e in entries if e.get("status") == "failed")
    return added, pending, failed


def process_pending_collections(
    credential,
    *,
    backfill: bool = True,
    retry_interval_seconds: int = _COLLECTION_RETRY_INTERVAL,
    rate_limit_cooldown_seconds: int = _RATE_LIMIT_COOLDOWN,
    state_path: Path | None = None,
    resolve_channel=None,
    reorder_touched: bool = True,
    reverse: bool = False,
    youtube=None,
) -> tuple[int, int, int]:
    """
    Enrich missing channels (optional), backfill history, then attempt to add
    every queued video to its 合集.

    Returns ``(added, pending, failed)`` counts after one sweep.
    """
    queue_path = pending_collections_path()
    if not _acquire_sweep_lock(queue_path):
        print("[合集] ⏸️ 另一进程正在执行补归，跳过本轮")
        return 0, 0, 0
    try:
        resolved_state = state_path or (queue_path.parent / "processed_videos.json")
        if backfill:
            if resolve_channel is not None:
                n = enrich_missing_channels(resolved_state, resolve_channel)
                if n:
                    print(f"[合集] 频道反查: 补全 {n} 条")
            n = backfill_collections(queue_path, resolved_state)
            if n:
                print(f"[合集] 历史回填: {n} 条")
        n = enrich_queue_dates(queue_path, resolved_state)
        if n:
            print(f"[合集] 发布时间回填: {n} 条")
        entries = load_pending_collections(queue_path)
        if not entries:
            return 0, 0, 0
        return asyncio.run(
            _sweep_pending_collections(
                credential, queue_path, entries, retry_interval_seconds,
                rate_limit_cooldown_seconds,
                state_path=resolved_state,
                reorder_touched=reorder_touched,
                reverse=reverse,
                youtube=youtube,
            )
        )
    finally:
        _release_sweep_lock(queue_path)
