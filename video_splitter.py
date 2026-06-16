"""
Video splitting utility using FFmpeg segment muxer.

Splits videos longer than a configurable threshold (default 10 hours)
into segments suitable for Bilibili multi-part (分P) upload.

Uses ``-c copy`` for fast, lossless splitting at keyframe boundaries.
"""

import subprocess
import time
from pathlib import Path

import config


def _probe_duration(file_path: Path) -> float:
    """Probe video file duration in seconds using ffprobe."""
    command = [
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "csv=p=0",
        str(file_path),
    ]
    try:
        proc = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return 0.0
    if proc.returncode != 0:
        return 0.0
    try:
        return float(proc.stdout.strip())
    except (ValueError, TypeError):
        return 0.0


def split_video(
    file_path: str,
    segment_duration_seconds: int | None = None,
    output_dir: str | None = None,
) -> list[str]:
    """
    Split a video file into fixed-length segments using ffmpeg segment muxer.

    Uses ``-c copy`` (no re-encoding) for speed.  Split points occur at the
    nearest keyframe after each segment boundary, so segments may be slightly
    longer or shorter than the requested duration.

    Args:
        file_path: Path to the video file (typically .mp4).
        segment_duration_seconds: Segment length in seconds; defaults to
            ``config.MAX_VIDEO_DURATION_SECONDS`` (36000 = 10 hours).
        output_dir: Directory for output segments; defaults to
            ``<DOWNLOAD_DIR>/splits/<file_stem>/``.

    Returns:
        List of absolute paths to segment files, sorted alphabetically
        (which matches part order).  Returns ``[]`` on failure (so callers
        can fall back to single-file upload).  Returns ``[file_path]``
        unchanged when the video does not need splitting.
    """
    source = Path(file_path).resolve()
    if not source.exists():
        print(f"[分割] ❌ 文件不存在: {source}")
        return []

    seg_sec = segment_duration_seconds or int(config.MAX_VIDEO_DURATION_SECONDS)
    if seg_sec <= 0:
        return [str(source)]

    # Determine output directory
    if output_dir:
        out_dir = Path(output_dir)
    else:
        out_dir = Path(config.DOWNLOAD_DIR) / "splits" / source.stem
    out_dir.mkdir(parents=True, exist_ok=True)

    # Segment file pattern – ffmpeg %03d auto-numbers from 0
    stem = source.stem
    ext = source.suffix  # e.g. ".mp4"
    pattern = out_dir / f"{stem}_P%03d{ext}"

    command = [
        "ffmpeg",
        "-i", str(source),
        "-c", "copy",
        "-map", "0",
        "-segment_time", str(seg_sec),
        "-reset_timestamps", "1",
        "-f", "segment",
        "-avoid_negative_ts", "make_zero",
        str(pattern),
    ]

    print(f"[分割] 开始分割: {source.name}")
    print(f"       每段 ≤ {seg_sec}s ({seg_sec/3600:.1f}h)")
    started = time.monotonic()

    try:
        proc = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=None,  # large files may take minutes
            check=False,
        )
    except FileNotFoundError:
        print("[分割] ❌ ffmpeg 未安装或不在 PATH 中")
        return []
    except subprocess.TimeoutExpired:
        print("[分割] ❌ 分割超时")
        return []

    elapsed = time.monotonic() - started

    if proc.returncode != 0:
        stderr_tail = "\n".join(
            proc.stderr.strip().splitlines()[-5:]
        ) if proc.stderr else "(no stderr)"
        print(f"[分割] ❌ ffmpeg 返回非零退出码: {proc.returncode}")
        print(f"       stderr (最后 5 行): {stderr_tail}")
        return []

    # Collect output segments (glob for generated files)
    # ffmpeg names them from 0: <stem>_P000<ext>, <stem>_P001<ext>, ...
    segments = sorted(out_dir.glob(f"{stem}_P*{ext}"))
    if not segments:
        print(f"[分割] ⚠️ 未生成任何分段文件")
        return []

    total_mb = sum(s.stat().st_size for s in segments) / 1024 / 1024
    print(f"[分割] ✅ 分割完成: {len(segments)} 个分段 ({total_mb:.1f} MB, {elapsed:.1f}s)")
    for i, seg in enumerate(segments):
        size_mb = seg.stat().st_size / 1024 / 1024
        dur = _probe_duration(seg)
        dur_str = f"{dur:.1f}s ({dur/3600:.2f}h)" if dur > 0 else "?"
        print(f"       P{i+1}: {seg.name}  {size_mb:.1f} MB  {dur_str}")

    return [str(s) for s in segments]


