"""
Re-segment YouTube json3 (word-level) captions into sentence-based cues.

YouTube's auto-caption chunks are ~2-4s and cut mid-sentence, which hurts
machine translation (missing context).  json3 has per-word timestamps, so
we can rebuild cues at real sentence boundaries:

    - sentence-ending punctuation (``. ! ? …`` plus CJK ``。！？``)
      followed by a capitalised word
    - speaker-change markers (``>>`` prefix in auto-caption text)
    - ambient markers ("[Music]", "[Laughter]", ...) become their own cues
    - long inter-word pauses (> ``PAUSE_S``)
    - hard cap: ``MAX_CHARS`` characters / ``MAX_DUR_S`` seconds, split
      at commas

Caption-window line breaks (json3 ``\\n`` segs) are ignored — they exist once
per display window and would just reproduce the original chunking.

All functions are pure (strings/lists in, lists out) so tests run without
network or yt-dlp.  Only ``resegment_file`` touches the filesystem.
"""

import json
import re
import statistics
from pathlib import Path

from .parser import Cue
from .writer import write_srt

# ── Tunables ──────────────────────────────────────────────────────────

PAUSE_S = 0.8        # split when the inter-word gap exceeds this
MAX_CHARS = 140      # max characters per cue (comma-split when exceeded)
MAX_DUR_S = 12.0     # max cue duration in seconds
PAD_S = 0.08         # trailing padding added to each cue
MIN_CUE_S = 0.4      # minimum cue duration
NEXT_GAP_S = 0.02    # gap kept before the next cue starts
MIN_WORD_S = 0.2     # degenerate-word fallback duration
DEFAULT_GAP_S = 0.35  # window-end word duration when no intra-window gaps

_BRACKET_RE = re.compile(r"^\[[^\]]+\]$")
_END_PUNCT_RE = re.compile(r"[.!?…。！？]['\")\]]?$")
# "Mr." / "Dr." etc. — a period here does NOT end a sentence
_ABBREV_RE = re.compile(
    r"^(?:mr|mrs|ms|dr|prof|sr|jr|st|vs|etc|eg|ie|aka)\.$", re.IGNORECASE
)

# Hiragana/Katakana + CJK unified ideographs + Hangul — spaces between two
# adjacent CJK characters are artifacts of " ".join and must be removed.
_CJK_RE = re.compile(
    r"(?<=[぀-ヿ㐀-䶿一-鿿豈-﫿가-힯])"
    r"\s+"
    r"(?=[぀-ヿ㐀-䶿一-鿿豈-﫿가-힯])"
)


def _load_words(json_text: str) -> list[tuple[float, float, str, str]]:
    """Return tokens: ``(start_s, end_s, text, kind)`` kind ∈ word|break|marker.

    json3 gives per-word start offsets only; a word's end is the next word's
    start.  The caption window's own end (tStartMs+dDurationMs) includes
    display padding, so the LAST word of a window uses the window's median
    inter-word gap as its estimated duration instead — otherwise cue
    durations get inflated and total speech exceeds the video length.
    """
    data = json.loads(json_text)
    events = sorted(data.get("events", []), key=lambda e: e.get("tStartMs", 0))

    tokens: list[tuple[float, float, str, str]] = []
    for ev in events:
        base = ev.get("tStartMs", 0)
        ev_end = base + ev.get("dDurationMs", 0)
        segs = [s for s in ev.get("segs", []) if s.get("utf8", "").strip() != "\n"]
        if not segs:
            continue
        word_starts = [(base + (s.get("tOffsetMs") or 0), s.get("utf8", "")) for s in segs]
        gaps = [
            word_starts[j + 1][0] - word_starts[j][0]
            for j in range(len(word_starts) - 1)
            if word_starts[j + 1][0] > word_starts[j][0]
        ]
        median_gap = statistics.median(gaps) / 1000 if gaps else DEFAULT_GAP_S
        for i, (st, text) in enumerate(word_starts):
            if i + 1 < len(word_starts):
                en = word_starts[i + 1][0]
            else:
                en = min(st + max(median_gap, MIN_WORD_S) * 1000, ev_end)
            if st >= en:
                en = st + MIN_WORD_S * 1000
            # ">> " marks a speaker change in YouTube auto-captions
            speaker_change = text.strip().startswith(">>")
            stripped = text.replace(">>", "").strip()
            if speaker_change and not stripped:
                tokens.append((st / 1000, en / 1000, "", "break"))
                continue
            if not stripped:
                continue
            if _BRACKET_RE.match(stripped):
                # marker segs can carry a display-time offset placing them
                # AFTER the next word — swap so start < end
                if en < st:
                    st, en = en, st
                tokens.append((st / 1000, en / 1000, stripped, "marker"))
            elif speaker_change:
                # new speaker's line — flush the current sentence first
                tokens.append((st / 1000, en / 1000, "", "break"))
                tokens.append((st / 1000, en / 1000, stripped, "word"))
            else:
                # words stored stripped; sentences are rebuilt space-joined
                tokens.append((st / 1000, en / 1000, stripped, "word"))
    return tokens


def _join_text(words: list[str]) -> str:
    """Join stripped words with spaces, then remove spaces between CJK chars."""
    return _CJK_RE.sub("", " ".join(words))


def _split_sentence(words: list[tuple[float, float, str]], max_chars: int):
    """Split an over-long sentence at the last comma (or hard split).

    Returns ``(piece, rest)`` — two lists of words.
    """
    if sum(len(w[2].strip()) for w in words) <= max_chars:
        return words, []
    budget = 0
    cut = -1
    for i, (_, _, text) in enumerate(words):
        t = text.strip()
        budget += len(t)
        if budget > max_chars:
            break
        if t.rstrip().endswith(","):
            cut = i
    if cut > 0:
        return words[: cut + 1], words[cut + 1 :]
    budget = 0
    cut = 0
    for i, (_, _, text) in enumerate(words):
        budget += len(text.strip())
        if budget > max_chars:
            cut = i
            break
    if cut <= 0:
        return words, []
    return words[:cut], words[cut:]


def _build_sentences(
    tokens: list[tuple[float, float, str, str]],
    pause_s: float,
    max_chars: int,
    max_dur: float,
) -> list[tuple[float, float, str]]:
    """Group tokens into ``(start_s, end_s, text)`` sentences."""
    sentences: list[tuple[float, float, str]] = []

    def emit(words: list[tuple[float, float, str]]) -> None:
        if not words:
            return
        text = _join_text([w[2] for w in words])
        if not text:
            return
        sentences.append((words[0][0], words[-1][1], text))

    buf: list[tuple[float, float, str]] = []
    prev_end: float | None = None
    prev_text = ""

    def flush() -> None:
        nonlocal buf
        # Emit ALL buffered words (split at commas when over-long) — a
        # leftover "rest" must not swallow the incoming word, or a sentence
        # boundary like "…tier list." + "We're…" gets silently merged.
        while buf:
            piece, buf = _split_sentence(buf, max_chars)
            if not piece:
                break
            emit(piece)

    for st, en, text, kind in tokens:
        if kind == "break":
            flush()
            prev_end, prev_text = en, ""
            continue
        if kind == "marker":
            flush()
            sentences.append((st, en, text))
            prev_end, prev_text = en, ""
            continue

        gap = (st - prev_end) if prev_end is not None else 0.0
        prev_stripped = prev_text.strip()
        ends_sentence = (
            bool(_END_PUNCT_RE.search(prev_stripped))
            and not _ABBREV_RE.match(prev_stripped)
        )
        next_is_cap = bool(text) and text[0].isupper()
        if buf and (
            gap > pause_s
            or (ends_sentence and next_is_cap)
            or (buf[-1][1] - buf[0][0]) > max_dur
        ):
            flush()
        buf.append((st, en, text))
        prev_end, prev_text = en, text

    flush()
    return sentences


def resegment_json3(
    json_text: str,
    *,
    pause_s: float = PAUSE_S,
    max_chars: int = MAX_CHARS,
    max_dur: float = MAX_DUR_S,
    pad_s: float = PAD_S,
) -> list[Cue]:
    """Convert a json3 payload into sentence-based ``Cue`` objects.

    Args:
        json_text: Raw json3 content (as returned by yt-dlp).
        pause_s: Split when the inter-word gap exceeds this (seconds).
        max_chars: Maximum characters per cue.
        max_dur: Maximum cue duration (seconds).
        pad_s: Trailing padding added to each cue (seconds).

    Returns:
        List of ``Cue`` objects with sequential 1-based indices.

    Raises:
        json.JSONDecodeError: If ``json_text`` is not valid JSON.
    """
    tokens = _load_words(json_text)
    sentences = _build_sentences(tokens, pause_s, max_chars, max_dur)

    cues: list[Cue] = []
    for i, (st, en, text) in enumerate(sentences):
        start = st
        end = max(en + pad_s, start + MIN_CUE_S)
        if i + 1 < len(sentences):
            end = min(end, sentences[i + 1][0] - NEXT_GAP_S)
        # Safety clamp: a punctuation-split cue whose next cue starts right
        # at its raw end must not end up with end < start.
        end = max(end, start + 0.05)
        cues.append(Cue(index=i + 1, start=start, end=end, text=text))
    return cues


def resegment_file(
    json3_path: str | Path,
    out_srt_path: str | Path | None = None,
    **overrides,
) -> str:
    """Re-segment a json3 subtitle file and write the result as SRT.

    Args:
        json3_path: Path to a ``.json3`` file.
        out_srt_path: Output ``.srt`` path.  Defaults to the input path with
            suffix ``.reseg.srt``.
        **overrides: Passed through to :func:`resegment_json3`.

    Returns:
        Absolute path to the written SRT file.

    Raises:
        json.JSONDecodeError / OSError: On corrupt input or write failure.
    """
    in_path = Path(json3_path)
    out_path = Path(out_srt_path) if out_srt_path else in_path.with_suffix(".reseg.srt")
    cues = resegment_json3(in_path.read_text(encoding="utf-8-sig"), **overrides)
    return write_srt(cues, out_path)
