"""
Cover image preparation for Bilibili uploads.
Validates YouTube thumbnails and converts them to a 1920x1080 JPEG cover.
"""

from pathlib import Path
from PIL import Image, ImageOps, UnidentifiedImageError

import config


def is_valid_image(path: str | Path | None) -> bool:
    """Return True when path points to a readable, non-empty image."""
    if not path:
        return False

    image_path = Path(path)
    if not image_path.exists() or image_path.stat().st_size <= 0:
        return False

    try:
        with Image.open(image_path) as img:
            img.verify()
        return True
    except (OSError, UnidentifiedImageError):
        return False


def _crop_to_ratio(img: Image.Image, width: int, height: int) -> Image.Image:
    """Center-crop an image to the requested aspect ratio."""
    source_width, source_height = img.size
    target_ratio = width / height
    source_ratio = source_width / source_height

    if source_ratio > target_ratio:
        new_width = int(source_height * target_ratio)
        left = (source_width - new_width) // 2
        box = (left, 0, left + new_width, source_height)
    else:
        new_height = int(source_width / target_ratio)
        top = (source_height - new_height) // 2
        box = (0, top, source_width, top + new_height)

    return img.crop(box)


def _pad_to_size(img: Image.Image, width: int, height: int) -> Image.Image:
    """Scale an image to fit within target dimensions, padding with black bars."""
    source_width, source_height = img.size
    target_ratio = width / height
    source_ratio = source_width / source_height

    if source_ratio > target_ratio:
        # Image is wider — fit to width, pad top/bottom
        new_width = width
        new_height = int(width / source_ratio)
    else:
        # Image is taller — fit to height, pad left/right
        new_height = height
        new_width = int(height * source_ratio)

    scaled = img.resize((new_width, new_height), Image.Resampling.LANCZOS)
    canvas = Image.new("RGB", (width, height), (0, 0, 0))
    paste_x = (width - new_width) // 2
    paste_y = (height - new_height) // 2
    canvas.paste(scaled, (paste_x, paste_y))
    return canvas


def prepare_cover(cover_path: str | Path | None, video_id: str = "") -> str:
    """
    Validate and convert a thumbnail into the configured Bilibili cover size.

    Returns an absolute path to the prepared JPEG, or an empty string if the
    source image is missing or invalid.
    """
    if not is_valid_image(cover_path):
        return ""

    source = Path(cover_path)
    covers_dir = Path(config.DOWNLOAD_DIR) / "covers"
    covers_dir.mkdir(parents=True, exist_ok=True)

    stem = video_id or source.stem
    output = covers_dir / f"{stem}_{config.COVER_WIDTH}x{config.COVER_HEIGHT}.jpg"

    with Image.open(source) as img:
        img = ImageOps.exif_transpose(img)
        img = img.convert("RGB")
        fit_mode = config.COVER_FIT
        if fit_mode == "crop":
            img = _crop_to_ratio(img, config.COVER_WIDTH, config.COVER_HEIGHT)
            img = img.resize((config.COVER_WIDTH, config.COVER_HEIGHT), Image.Resampling.LANCZOS)
        elif fit_mode == "contain":
            img = _pad_to_size(img, config.COVER_WIDTH, config.COVER_HEIGHT)
        else:
            # Fallback: treat unknown mode as crop
            img = _crop_to_ratio(img, config.COVER_WIDTH, config.COVER_HEIGHT)
            img = img.resize((config.COVER_WIDTH, config.COVER_HEIGHT), Image.Resampling.LANCZOS)
        img.save(output, format="JPEG", quality=95, optimize=True)

    return str(output.resolve())


def image_size(path: str | Path | None) -> tuple[int, int] | None:
    """Return image dimensions for logging, or None for invalid images."""
    if not is_valid_image(path):
        return None
    with Image.open(path) as img:
        return img.size
