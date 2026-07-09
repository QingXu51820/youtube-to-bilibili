"""
SRT / VTT subtitle parser.

Parses subtitle files into a list of Cue objects with time values
converted to float seconds for downstream processing.
"""

import re
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Cue:
    """A single subtitle cue with timing in seconds."""
    index: int       # sequential cue number (1-based)
    start: float     # start time in seconds
    end: float       # end time in seconds
    text: str        # subtitle text (may be multi-line)


# ── SRT timestamp parsing ────────────────────────────────────────────

_SRT_TIMESTAMP_RE = re.compile(
    r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{3})"
)


def _srt_timestamp_to_seconds(ts: str) -> float:
    """Convert 'HH:MM:SS,mmm' or 'HH:MM:SS.mmm' to float seconds."""
    m = _SRT_TIMESTAMP_RE.match(ts.strip())
    if not m:
        raise ValueError(f"Invalid SRT timestamp: {ts!r}")
    h, mi, s, ms = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))
    return h * 3600.0 + mi * 60.0 + s + ms / 1000.0


# ── SRT parser ───────────────────────────────────────────────────────

_SRT_CUE_HEADER_RE = re.compile(
    r"^(\d{1,2}):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*"
    r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{3})"
)


def parse_srt(file_path: str | Path) -> list[Cue]:
    """
    Parse an SRT file into a list of Cue objects.

    SRT format::

        1
        00:00:01,230 --> 00:00:04,560
        Hello world

        2
        00:00:05,000 --> 00:00:08,000
        Line one
        Line two

    Args:
        file_path: Path to .srt file.

    Returns:
        List of Cue objects.  Empty file returns ``[]``.
    """
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"Subtitle file not found: {file_path}")

    text = path.read_text(encoding="utf-8-sig")
    return _parse_srt_text(text)


def _parse_srt_text(text: str) -> list[Cue]:
    """Parse SRT content from a string (shared by SRT and VTT parsers)."""
    cues: list[Cue] = []
    # Split on blank lines (one or more empty lines)
    blocks = re.split(r"\n\s*\n", text.strip())

    for block in blocks:
        lines = [l.rstrip("\r").strip() for l in block.splitlines()]
        lines = [l for l in lines if l]  # remove blank lines
        if not lines:
            continue

        try:
            # First line is the index number
            idx = int(lines[0])
            # Second line is the timestamp arrow
            ts_match = _SRT_CUE_HEADER_RE.match(lines[1])
            if not ts_match:
                continue
            h1, m1, s1, ms1 = (int(ts_match.group(i)) for i in range(1, 5))
            h2, m2, s2, ms2 = (int(ts_match.group(i)) for i in range(5, 9))
            start = h1 * 3600.0 + m1 * 60.0 + s1 + ms1 / 1000.0
            end = h2 * 3600.0 + m2 * 60.0 + s2 + ms2 / 1000.0
            # Remaining lines are the text
            cue_text = "\n".join(lines[2:])
            cues.append(Cue(index=idx, start=start, end=end, text=cue_text))
        except (ValueError, IndexError):
            # Skip malformed blocks
            continue

    # Re-index sequentially (1-based) in case of gaps
    for i, cue in enumerate(cues):
        cue.index = i + 1

    return cues


# ── VTT parser ───────────────────────────────────────────────────────

_VTT_HEADER_RE = re.compile(r"^WEBVTT", re.IGNORECASE)
_VTT_CUE_TIMING_RE = re.compile(
    r"^(\d{1,2}:)?(\d{2}):(\d{2})\.(\d{3})\s*-->\s*"
    r"(\d{1,2}:)?(\d{2}):(\d{2})\.(\d{3})"
)


def parse_vtt(file_path: str | Path) -> list[Cue]:
    """
    Parse a WebVTT file into a list of Cue objects.

    WebVTT uses ``.`` as millisecond separator and has a ``WEBVTT`` header.
    Optional hours in timestamps and cue identifiers are supported.

    Args:
        file_path: Path to .vtt file.

    Returns:
        List of Cue objects.
    """
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"Subtitle file not found: {file_path}")

    text = path.read_text(encoding="utf-8-sig")
    return _parse_vtt_text(text)


def _parse_vtt_text(text: str) -> list[Cue]:
    """Parse VTT content from a string."""
    cues: list[Cue] = []
    # Strip WEBVTT header
    lines = text.strip().splitlines()
    start_idx = 0
    for i, line in enumerate(lines):
        if _VTT_HEADER_RE.match(line.strip()):
            start_idx = i + 1
            break

    # Join remaining lines and split on double newline for blocks
    body = "\n".join(lines[start_idx:])
    blocks = re.split(r"\n\s*\n", body.strip())

    index = 0
    for block in blocks:
        block_lines = [l.rstrip("\r").strip() for l in block.splitlines()]
        block_lines = [l for l in block_lines if l]
        if not block_lines:
            continue

        # Find the timing line (contains '-->')
        timing_idx = None
        for i, line in enumerate(block_lines):
            if "-->" in line:
                timing_idx = i
                break

        if timing_idx is None:
            continue

        ts_match = _VTT_CUE_TIMING_RE.match(block_lines[timing_idx])
        if not ts_match:
            continue

        h1 = int(ts_match.group(1)[:-1]) if ts_match.group(1) else 0  # strip trailing colon
        m1 = int(ts_match.group(2))
        s1 = int(ts_match.group(3))
        ms1 = int(ts_match.group(4))
        h2 = int(ts_match.group(5)[:-1]) if ts_match.group(5) else 0
        m2 = int(ts_match.group(6))
        s2 = int(ts_match.group(7))
        ms2 = int(ts_match.group(8))

        start = h1 * 3600.0 + m1 * 60.0 + s1 + ms1 / 1000.0
        end = h2 * 3600.0 + m2 * 60.0 + s2 + ms2 / 1000.0

        # Text is lines after the timing line (may include VTT tags, keep as-is)
        cue_text = "\n".join(block_lines[timing_idx + 1:])

        index += 1
        cues.append(Cue(index=index, start=start, end=end, text=cue_text))

    return cues


# ── Auto-detect parser ───────────────────────────────────────────────

def parse_subtitle(file_path: str | Path) -> list[Cue]:
    """
    Auto-detect format (by file extension) and parse a subtitle file.

    Supports ``.srt`` and ``.vtt`` files.

    Args:
        file_path: Path to a subtitle file.

    Returns:
        List of Cue objects.
    """
    ext = Path(file_path).suffix.lower()
    if ext == ".srt":
        return parse_srt(file_path)
    elif ext == ".vtt":
        return parse_vtt(file_path)
    else:
        raise ValueError(
            f"Unsupported subtitle format: {ext!r}. Expected .srt or .vtt."
        )
