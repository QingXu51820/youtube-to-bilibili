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
_ADD_EPISODES_URL = (
    "https://member.bilibili.com/x2/creative/web/season/section/episodes/add"
)
_COVER_UP_URL = "https://member.bilibili.com/x/vu/web/cover/up"
_PAGELIST_URL = "https://api.bilibili.com/x/player/pagelist"

_AUTH_ERROR_CODES = (401, 403)
_DEFAULT_TIMEOUT = 15.0
_PAGES_WAIT_TIMEOUT = 60   # 投稿后等待分P信息就绪的总时长（秒）
_PAGES_WAIT_INTERVAL = 5   # 轮询间隔（秒）
_COLLECTION_RETRY_INTERVAL = 3600  # 补归同一视频的最短重试间隔（秒）
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
            print(f"[合集] ⚠️ 无法确定频道「{channel_title}」的合集，跳过回填: {v.get('title', '')}")
            continue
        entries.append({
            "video_id": video_id,
            "bvid": bvid,
            "aid": int(v.get("aid", 0) or 0),
            "collection_name": collection_name,
            "channel_title": channel_title,
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
    return added


async def wait_for_video_pages(
    credential,
    bvid: str,
    timeout: int = _PAGES_WAIT_TIMEOUT,
    interval: int = _PAGES_WAIT_INTERVAL,
) -> list[dict]:
    """
    Poll the pagelist API until a freshly uploaded video's parts are ready.

    Bilibili processes uploads asynchronously — immediately after submission
    the pagelist API can return ``code=-404`` (``啥都木有``) or an empty list.
    Retry on those transient states until the pages (with cids) appear or
    *timeout* seconds elapse.

    Raises:
        TimeoutError: The pages were not available within ``timeout`` seconds.
        BilibiliApiError: The API returns a non-transient error code.
    """
    start = time.monotonic()
    last_error: Exception | None = None

    while True:
        elapsed = time.monotonic() - start
        if elapsed >= timeout:
            raise TimeoutError(
                f"等待分P信息超时（{timeout}s 内未获取到）"
                + (f"，最后一次错误: {last_error}" if last_error else "")
            )

        try:
            pages = await fetch_video_pages(credential, bvid)
            if pages:
                print(f"[合集] 获取到分P信息（等待 {elapsed:.0f}s）")
                return pages
            last_error = RuntimeError("分P信息为空")
        except BilibiliApiError as e:
            last_error = e
            if e.code != -404:
                raise

        print(".", end="", flush=True)
        await asyncio.sleep(interval)


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
    print(f"[合集] 等待分P信息就绪（视频刚投稿，B站可能需要几秒~一分钟）...")
    pages = await wait_for_video_pages(credential, bvid)
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
