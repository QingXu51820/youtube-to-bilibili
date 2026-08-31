"""
Bilibili 合集 (season) management.

Wraps the creator-center endpoints that ``bilibili-api-python`` does not
cover: list collections, create a collection, and add uploaded videos to a
collection.  Pure matching/mapping helpers live here too so they can be
unit-tested without network access.
"""

from __future__ import annotations

import re
import tempfile
from dataclasses import dataclass
from pathlib import Path


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
