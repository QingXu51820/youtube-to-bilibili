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
_MIN_CUE_DURATION = 0.01  # seconds; Bilibili rejects 0-duration cues (79014)


def clamp_cues_to_duration(
    cues: list[Cue],
    duration: float,
    *,
    margin: float = 0.0,
) -> tuple[list[Cue], int, int]:
    """
    Clamp cue timings so nothing exceeds *duration*.

    Cues whose ``start`` is at or past the limit are dropped; cues whose
    ``end`` straddles it have ``end`` clamped to ``duration - margin``.
    *margin* guards against the source duration (e.g. ffprobe fractional
    seconds) differing from Bilibili's integer-second duration, which
    would otherwise trigger 79014 "字幕时间点超过视频时间长度".

    Returns ``(kept_cues, dropped_count, clamped_count)``.
    """
    if duration is None or duration <= 0:
        return cues, 0, 0

    limit = duration - margin
    kept: list[Cue] = []
    dropped = 0
    clamped = 0
    for cue in cues:
        if cue.start >= limit:
            dropped += 1
            continue
        if cue.end > limit:
            # Return a new Cue — don't mutate the caller's objects
            cue = Cue(index=cue.index, start=cue.start, end=limit, text=cue.text)
            clamped += 1
        kept.append(cue)
    return kept, dropped, clamped


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
    margin: float = 0.0,
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
    fixed_zero = 0
    truncated = 0

    # ── Timestamp validation ─────────────────────────────────────
    if video_duration is not None:
        cues, trimmed, clamped = clamp_cues_to_duration(
            list(cues), video_duration, margin=margin
        )

    for cue in cues:
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

        # ── Minimum duration validation ───────────────────────────
        # YouTube 自动字幕常有 start == end 的 0 时长 cue（音效标签等），
        # B站会以 79014 "字幕的持续时间必须大于0" 拒绝整个投稿。
        start = round(cue.start, 3)
        end = round(cue.end, 3)
        if end <= start:
            end = round(start + _MIN_CUE_DURATION, 3)
            fixed_zero += 1
            if fixed_zero <= 5:
                print(
                    f"[字幕] [WARN] #{cue.index} 字幕持续时间为 0，"
                    f"已延长至 {_MIN_CUE_DURATION * 1000:.0f}ms（YouTube 自动字幕常见）",
                    flush=True, file=sys.stderr,
                )

        body.append({
            "from": start,
            "to": end,
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
    if fixed_zero > 5:
        print(
            f"[字幕] [WARN] 共 {fixed_zero} 条 0 时长字幕已延长至"
            f" {_MIN_CUE_DURATION * 1000:.0f}ms",
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
