"""
Convert subtitle Cue objects to Bilibili soft-subtitle JSON format.

The Bilibili subtitle upload API expects a JSON body containing rendering
metadata plus a ``body`` array of timed text segments.
"""

import sys

from .parser import Cue

# Sensible defaults matching typical Bilibili CC subtitle appearance
_DEFAULT_FONT_SIZE = 0.4
_DEFAULT_FONT_COLOR = "#FFFFFF"
_DEFAULT_BACKGROUND_ALPHA = 0.5
_DEFAULT_BACKGROUND_COLOR = "#9C27B0"
_DEFAULT_STROKE = "none"
_DEFAULT_LOCATION = 2  # bottom center

# Bilibili subtitle limits (enforced server-side, validate client-side to
# avoid wasted API calls and provide actionable warnings).
_MAX_CONTENT_CHARS = 80   # per-cue content length (Bilibili limit ≈100)
_MAX_CUE_COUNT = 1000     # total cues (Bilibili limit, loosely enforced)


def cues_to_bilibili_json(
    cues: list[Cue],
    *,
    font_size: float = _DEFAULT_FONT_SIZE,
    font_color: str = _DEFAULT_FONT_COLOR,
    background_alpha: float = _DEFAULT_BACKGROUND_ALPHA,
    background_color: str = _DEFAULT_BACKGROUND_COLOR,
    stroke: str = _DEFAULT_STROKE,
    location: int = _DEFAULT_LOCATION,
    video_duration: float | None = None,
    warn_overlength: bool = True,
) -> dict:
    """
    Convert SRT cues to Bilibili subtitle JSON format.

    The returned dict is suitable for direct JSON serialization and
    submission to the Bilibili subtitle upload API::

        {
            "font_size": 0.4,
            "font_color": "#FFFFFF",
            "background_alpha": 0.5,
            "background_color": "#9C27B0",
            "Stroke": "none",
            "body": [
                {"from": 1.23, "to": 4.56, "location": 2, "content": "text"}
            ]
        }

    When *video_duration* is provided (in seconds), cues whose ``start``
    time exceeds it are silently dropped, and cues that straddle the end
    have their ``to`` clamped.  Content exceeding ``_MAX_CONTENT_CHARS``
    is truncated with a trailing ``…``.

    Args:
        cues: Translated subtitle cues.
        font_size: Font size multiplier.
        font_color: Hex color for text.
        background_alpha: Background transparency (0-1).
        background_color: Hex color for background.
        stroke: Stroke style (usually ``"none"``).
        location: Display position. ``2`` = bottom center.
        video_duration: Video duration in seconds.  When set, cues
            beyond this duration are trimmed / clamped.
        warn_overlength: When True (default), print a warning to stderr
            for the first 5 cues whose content is truncated.

    Returns:
        Dict matching the Bilibili subtitle upload schema.
    """
    body: list[dict] = []
    trimmed = 0
    clamped = 0
    truncated = 0

    for cue in cues:
        # ── Timestamp validation ──────────────────────────────────
        start = cue.start
        end = cue.end

        if video_duration is not None and video_duration > 0:
            if start >= video_duration:
                trimmed += 1
                continue  # cue starts after video ends → drop
            if end > video_duration:
                end = video_duration
                clamped += 1

        # ── Content length validation ─────────────────────────────
        content = cue.text
        if len(content) > _MAX_CONTENT_CHARS:
            content = content[:_MAX_CONTENT_CHARS] + "…"
            truncated += 1
            if warn_overlength and truncated <= 5:
                print(
                    f"[字幕] [WARN] #{cue.index} 字幕过长 ({len(cue.text)} 字符)，"
                    f"已截断至 {_MAX_CONTENT_CHARS} 字符",
                    flush=True, file=sys.stderr,
                )

        body.append({
            "from": round(start, 3),
            "to": round(end, 3),
            "location": location,
            "content": content,
        })

    # ── Summary warnings ──────────────────────────────────────────
    if trimmed:
        print(
            f"[字幕] [WARN] 已跳过 {trimmed} 条超出视频时长的字幕",
            flush=True, file=sys.stderr,
        )
    if clamped:
        print(
            f"[字幕] [WARN] 已修正 {clamped} 条字幕的结束时间（超出视频时长）",
            flush=True, file=sys.stderr,
        )
    if truncated > 5:
        print(
            f"[字幕] [WARN] 共 {truncated} 条字幕过长已截断"
            f"（上限 {_MAX_CONTENT_CHARS} 字符）",
            flush=True, file=sys.stderr,
        )
    if len(body) > _MAX_CUE_COUNT:
        print(
            f"[字幕] [WARN] 字幕共 {len(body)} 条，超过 B站 {_MAX_CUE_COUNT} 条限制，"
            f"可能被拒绝",
            flush=True, file=sys.stderr,
        )

    return {
        "font_size": font_size,
        "font_color": font_color,
        "background_alpha": background_alpha,
        "background_color": background_color,
        "Stroke": stroke,
        "body": body,
    }
