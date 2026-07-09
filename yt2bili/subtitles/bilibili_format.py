"""
Convert subtitle Cue objects to Bilibili soft-subtitle JSON format.

The Bilibili subtitle upload API expects a JSON body containing rendering
metadata plus a ``body`` array of timed text segments.
"""

from .parser import Cue

# Sensible defaults matching typical Bilibili CC subtitle appearance
_DEFAULT_FONT_SIZE = 0.4
_DEFAULT_FONT_COLOR = "#FFFFFF"
_DEFAULT_BACKGROUND_ALPHA = 0.5
_DEFAULT_BACKGROUND_COLOR = "#9C27B0"
_DEFAULT_STROKE = "none"
_DEFAULT_LOCATION = 2  # bottom center


def cues_to_bilibili_json(
    cues: list[Cue],
    *,
    font_size: float = _DEFAULT_FONT_SIZE,
    font_color: str = _DEFAULT_FONT_COLOR,
    background_alpha: float = _DEFAULT_BACKGROUND_ALPHA,
    background_color: str = _DEFAULT_BACKGROUND_COLOR,
    stroke: str = _DEFAULT_STROKE,
    location: int = _DEFAULT_LOCATION,
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

    Args:
        cues: Translated subtitle cues.
        font_size: Font size multiplier.
        font_color: Hex color for text.
        background_alpha: Background transparency (0-1).
        background_color: Hex color for background.
        stroke: Stroke style (usually ``"none"``).
        location: Display position. ``2`` = bottom center.

    Returns:
        Dict matching the Bilibili subtitle upload schema.
    """
    body: list[dict] = []
    for cue in cues:
        body.append({
            "from": round(cue.start, 3),
            "to": round(cue.end, 3),
            "location": location,
            "content": cue.text,
        })

    return {
        "font_size": font_size,
        "font_color": font_color,
        "background_alpha": background_alpha,
        "background_color": background_color,
        "Stroke": stroke,
        "body": body,
    }
