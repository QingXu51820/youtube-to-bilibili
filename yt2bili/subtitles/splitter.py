"""Split subtitle text and cues without losing content or overlapping time."""

from __future__ import annotations

from .parser import Cue

_PREFERRED_BREAK_CHARS = frozenset("。！？；，,.!?")


def split_text(text: str, max_chars: int) -> list[str]:
    """Split *text* into non-empty pieces whose rendered length is capped."""
    if max_chars < 1:
        raise ValueError("max_chars must be >= 1")

    remaining = text.strip()
    if not remaining:
        return []

    pieces: list[str] = []
    while len(remaining) > max_chars:
        window = remaining[:max_chars]

        punctuation_cut = max(
            (i for i, char in enumerate(window) if char in _PREFERRED_BREAK_CHARS),
            default=-1,
        )
        if punctuation_cut >= 0:
            cut = punctuation_cut + 1
        else:
            whitespace_cut = max(
                (i for i, char in enumerate(window) if char.isspace()),
                default=-1,
            )
            cut = whitespace_cut if whitespace_cut > 0 else max_chars

        piece = remaining[:cut].strip()
        if piece:
            pieces.append(piece)
        remaining = remaining[cut:].lstrip()

    if remaining:
        pieces.append(remaining)
    return pieces


def split_cue(cue: Cue, max_chars: int) -> list[Cue]:
    """Split one cue and distribute its interval by rendered character share."""
    pieces = split_text(cue.text, max_chars)
    if len(pieces) <= 1:
        return [Cue(cue.index, cue.start, cue.end, cue.text)]

    start = cue.start
    end = max(cue.start, cue.end)
    duration = end - start
    total_chars = sum(len(piece) for piece in pieces)
    if total_chars <= 0:
        return [Cue(cue.index, cue.start, cue.end, cue.text)]

    result: list[Cue] = []
    consumed = 0
    for i, piece in enumerate(pieces):
        consumed += len(piece)
        piece_start = start + duration * (consumed - len(piece)) / total_chars
        piece_end = end if i == len(pieces) - 1 else start + duration * consumed / total_chars
        result.append(
            Cue(index=i + 1, start=piece_start, end=piece_end, text=piece)
        )
    return result


def split_cues(cues: list[Cue], max_chars: int) -> list[Cue]:
    """Split all overlong cues and renumber the resulting track."""
    result: list[Cue] = []
    for cue in cues:
        result.extend(split_cue(cue, max_chars))
    return [
        Cue(index, cue.start, cue.end, cue.text)
        for index, cue in enumerate(result, start=1)
    ]
