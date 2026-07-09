"""
Write translated subtitle cues to a standard SRT file.
"""

from pathlib import Path
from .parser import Cue


def _seconds_to_srt_timestamp(seconds: float) -> str:
    """Convert float seconds to ``HH:MM:SS,mmm`` SRT format."""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int(round((seconds - int(seconds)) * 1000))
    # Clamp ms to 999 (floating precision edge case)
    if ms >= 1000:
        ms = 0
        s += 1
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def write_srt(cues: list[Cue], output_path: str | Path) -> str:
    """
    Write a list of Cue objects to an SRT file.

    Output format::

        1
        00:00:01,230 --> 00:00:04,560
        Translated text

        2
        00:00:05,000 --> 00:00:08,000
        More text

    Args:
        cues: List of Cue objects (with translated text).
        output_path: Path to output .srt file.

    Returns:
        Absolute path to the written file (as string).

    Raises:
        OSError: If the file cannot be written.
    """
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    lines: list[str] = []
    for i, cue in enumerate(cues, start=1):
        lines.append(str(i))
        lines.append(
            f"{_seconds_to_srt_timestamp(cue.start)} --> "
            f"{_seconds_to_srt_timestamp(cue.end)}"
        )
        lines.append(cue.text)
        lines.append("")  # blank separator line

    content = "\n".join(lines)
    path.write_text(content, encoding="utf-8")
    return str(path.resolve())
