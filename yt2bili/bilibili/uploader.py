"""
Bilibili video uploader using bilibili-api-python.
Uploads videos with copyright=2 (转载/repost).
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from dataclasses import dataclass
from typing import TYPE_CHECKING

from bilibili_api import video_uploader

from yt2bili import config
from yt2bili.bilibili import auth
from yt2bili.media.cover import image_size, is_valid_image

if TYPE_CHECKING:
    from bilibili_api import Credential

# HTTP status codes that indicate credential / authentication issues
_AUTH_ERROR_CODES = (401, 403)


def _make_minimal_jpeg() -> bytes:
    """Generate a minimal valid 1x1 JPEG byte sequence (valid everywhere)."""
    jpeg = b'\xff\xd8'  # SOI
    jpeg += b'\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00'  # APP0
    jpeg += b'\xff\xdb\x00\x43\x00\x08\x06\x06\x07\x06\x05\x08\x07\x07\x07\x09\x09\x08\x0a\x0c\x14\x0d\x0c\x0b\x0b\x0c\x19\x12\x13\x0f\x14\x1d\x1a\x1f\x1e\x1d\x1a\x1c\x1c\x20\x24\x2e\x27\x20\x22\x2c\x23\x1c\x1c\x28\x37\x29\x2c\x30\x31\x34\x34\x34\x1f\x27\x39\x3d\x38\x32\x3c\x2e\x33\x34\x32'  # DQT
    jpeg += b'\xff\xc0\x00\x0b\x08\x00\x01\x00\x01\x01\x01\x11\x00'  # SOF0
    jpeg += b'\xff\xc4\x00\x1f\x00\x00\x01\x05\x01\x01\x01\x01\x01\x01\x00\x00\x00\x00\x00\x00\x00\x00\x01\x02\x03\x04\x05\x06\x07\x08\x09\x0a\x0b'  # DHT
    jpeg += b'\xff\xda\x00\x08\x01\x01\x00\x00\x3f\x00'  # SOS
    jpeg += b'\x00'  # 1 pixel data
    jpeg += b'\xff\xd9'  # EOI
    return jpeg


_MINIMAL_JPEG = _make_minimal_jpeg()


def _ensure_cover(cover_path: str | None) -> str:
    """
    Ensure a valid cover image is available.
    Returns path to a valid cover image, creating a minimal one if needed.
    """
    if cover_path and is_valid_image(cover_path):
        return cover_path

    # Create a minimal placeholder JPEG
    tmp_dir = Path(tempfile.gettempdir()) / "yt2bili"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    placeholder = tmp_dir / "cover_placeholder.jpg"
    if not placeholder.exists():
        placeholder.write_bytes(_MINIMAL_JPEG)
    return str(placeholder)


@dataclass
class UploadResult:
    """Result of a Bilibili upload."""
    success: bool
    bvid: str = ""       # B站视频 BV 号
    aid: int = 0          # B站视频 AV 号
    message: str = ""     # success or error message


def _format_tid(tid: int) -> str:
    """Format a Bilibili tid with its zone name when known."""
    try:
        from bilibili_api import video_zone
        parent, child = video_zone.get_zone_info_by_tid(tid)
    except Exception:
        parent = child = None

    if parent and child:
        return f"{tid} ({parent.get('name', '')}-{child.get('name', '')})"
    if child:
        return f"{tid} ({child.get('name', '')})"
    if parent:
        return f"{tid} ({parent.get('name', '')})"
    return str(tid)


def _build_credential(credential: Credential | None = None):
    """Get Bilibili Credential (auto-login via QR if first time).

    When *credential* is provided it is used directly;
    otherwise the active profile's credential is resolved via auth.
    """
    if credential is not None:
        return credential
    return auth.get_credential()


_TRUNCATION_SUFFIX = "..."
_RESERVE_BYTES = len(_TRUNCATION_SUFFIX.encode("utf-8"))  # 3


def _build_description(
    original_description: str,
    translated_title: str,
    original_title: str = "",
    *,
    byte_limit: int = 2000,
) -> str:
    """Build the video description for B站 upload.

    Note: the source URL is set via VideoMeta.source and displayed by B站
    automatically; we don't duplicate it in the description text.

    The header (原标题 + 翻译标题) is always preserved.  After the header,
    as many *complete* lines of the original description as fit within
    `byte_limit` UTF-8 bytes are appended.  Lines are never split mid-way;
    if truncation is needed a trailing "..." is added.
    """
    # ── header (title lines only, no description section yet) ──────────
    header_parts = []
    if original_title:
        header_parts.append(f"原标题: {original_title}")
    header_parts.append(f"翻译标题: {translated_title}")
    header = "\n".join(header_parts)

    if not original_description:
        return header

    desc_lines = original_description.strip().split("\n")

    # ── prefix = header + section label ──────────────────────────────
    prefix = header + "\n\n原视频简介:\n"
    prefix_bytes = len(prefix.encode("utf-8"))
    budget = byte_limit - prefix_bytes

    if budget <= 0:
        # Header + label alone exceeds the limit (extremely rare).
        # Return the prefix as-is; the safety net in _upload_async will
        # handle the final byte-level truncation.
        return prefix

    # ── greedily pack complete lines into the remaining budget ───────
    selected: list[str] = []
    used = 0
    all_fit = True

    for line in desc_lines:
        separator_cost = 1 if selected else 0  # "\n" between lines
        line_content_bytes = len(line.encode("utf-8"))
        total_line_cost = separator_cost + line_content_bytes

        if used + total_line_cost + _RESERVE_BYTES > budget:
            all_fit = False
            break

        selected.append(line)
        used += total_line_cost

    if not selected:
        # Even the first line of the description doesn't fit → omit the
        # description section entirely rather than showing an empty label.
        return header

    body = "\n".join(selected)
    if not all_fit:
        body += "\n" + _TRUNCATION_SUFFIX

    return prefix + body


async def _upload_async(
    file_paths: list[str],
    title: str,
    desc: str = "",
    tags: list[str] | None = None,
    tid: int | None = None,
    source_url: str = "",
    cover_path: str | None = None,
    credential: Credential | None = None,
) -> UploadResult:
    """
    Async upload a video to Bilibili (single or multi-part).

    Args:
        file_paths: Paths to video files (.mp4).  Multiple paths → 分P upload.
        title: Video title on B站
        desc: Video description
        tags: List of tags
        tid: Category ID (default from config)
        source_url: Original source URL (for 转载)
        cover_path: Optional cover image path

    Returns:
        UploadResult with status and BV/AV numbers
    """
    from bilibili_api.exceptions.NetworkException import NetworkException

    cred = _build_credential(credential)

    # Truncate description to B站 byte-length limit (2000 bytes in UTF-8).
    # This is a last-resort safety net; _build_description already ensures
    # the budget is respected.  Only triggers when the header alone exceeds
    # 2000 bytes (extremely rare).
    desc_bytes = desc.encode("utf-8")
    if len(desc_bytes) > 2000:
        # Trim to fit within 2000 bytes, preserving full UTF-8 sequences
        trimmed = desc_bytes[:1997]
        desc = trimmed.decode("utf-8", errors="ignore") + "..."

    # Ensure we have a valid cover image
    cover = _ensure_cover(cover_path)

    # Build metadata
    # original=False → 转载 (copyright=2), must provide source URL
    # original=True  → 自制 (copyright=1)
    vu_meta = video_uploader.VideoMeta(
        tid=tid or config.DEFAULT_TID,
        title=title,
        desc=desc,
        cover=cover,                # cover image path (required)
        tags=tags or [t.strip() for t in config.DEFAULT_TAGS.split(",")],
        original=False,             # False = 转载
        source=source_url,          # 转载来源链接
    )

    # Build upload pages (one per segment for 分P, single page otherwise)
    pages = []
    for i, fp in enumerate(file_paths):
        if len(file_paths) == 1:
            page_title = title
        elif i == 0:
            page_title = title  # P1: no suffix
        else:
            page_title = f"{title} P{i+1}"
        pages.append(video_uploader.VideoUploaderPage(path=fp, title=page_title))

    # Log part info
    if len(pages) == 1:
        print(f"  [上传] 文件: {Path(file_paths[0]).name}")
    else:
        print(f"  [上传] {len(pages)} 个分P:")
        for i, p in enumerate(pages):
            print(f"         P{i+1}: {Path(p.path).name} — \"{p.title}\"")

    # Create uploader and start
    uploader = video_uploader.VideoUploader(
        pages=pages,
        meta=vu_meta,
        credential=cred,
    )

    # Add progress listeners. bilibili-api-python passes one event_data
    # argument to named event handlers.
    @uploader.on(video_uploader.VideoUploaderEvents.PREUPLOAD.value)
    async def on_preupload(event_data=None):
        print(f"[上传] 准备上传...")

    @uploader.on(video_uploader.VideoUploaderEvents.PRE_COVER.value)
    async def on_pre_cover(event_data=None):
        print(f"[上传] 上传封面...")

    @uploader.on(video_uploader.VideoUploaderEvents.AFTER_COVER.value)
    async def on_after_cover(event_data=None):
        print(f"[上传] 封面上传完成")

    @uploader.on(video_uploader.VideoUploaderEvents.PRE_SUBMIT.value)
    async def on_pre_submit(event_data=None):
        print(f"[上传] 提交视频信息，等待审核...")

    @uploader.on(video_uploader.VideoUploaderEvents.COMPLETED.value)
    async def on_completed(event_data=None):
        print(f"[上传] ✅ 投稿成功!")

    @uploader.on(video_uploader.VideoUploaderEvents.ABORTED.value)
    async def on_aborted(event_data=None):
        print(f"[上传] ⚠️ 上传被中止")

    @uploader.on(video_uploader.VideoUploaderEvents.FAILED.value)
    async def on_failed(event_data=None):
        err = (event_data or {}).get("err")
        if err is not None:
            code = getattr(err, 'code', 0) or getattr(err, 'status', 0)
            if code in _AUTH_ERROR_CODES:
                print(f"[上传] 🔐 B站登录凭据已过期（HTTP {code}），需要重新登录")
            else:
                print(f"[上传] ❌ 上传失败: {event_data}")
        else:
            print(f"[上传] ❌ 上传失败: {event_data}")

    try:
        result = await uploader.start()
        bvid = result.get("bvid", "")
        aid = result.get("aid", 0)

        return UploadResult(
            success=True,
            bvid=bvid,
            aid=aid,
            message=f"上传成功! BV: {bvid}, AV: {aid}",
        )
    except NetworkException as e:
        code = getattr(e, 'code', 0) or getattr(e, 'status', 0)
        if code in _AUTH_ERROR_CODES:
            msg = (
                f"B站登录凭据已过期（HTTP {code}），请重新扫码登录。\n"
                f"运行: python main.py --login"
            )
            print(f"\n[上传] 🔐 {msg}")
            return UploadResult(success=False, message=msg)
        return UploadResult(
            success=False,
            message=f"上传失败: {e}",
        )
    except Exception as e:
        return UploadResult(
            success=False,
            message=f"上传失败: {e}",
        )


def upload_video(
    file_paths: str | list[str],
    title: str,
    original_url: str = "",
    original_description: str = "",
    original_title: str = "",
    tags: list[str] | None = None,
    tid: int | None = None,
    cover_path: str | None = None,
    credential: Credential | None = None,
) -> UploadResult:
    """
    Upload a video to Bilibili (synchronous wrapper).

    Args:
        file_paths: Path to the video file, or a list of paths for 分P upload.
        title: Translated title for B站
        original_url: Original YouTube URL (shown in description as source)
        original_description: Original video description
        original_title: Original YouTube title
        tags: Tags list
        tid: Category ID
        cover_path: Optional cover image path
        credential: Optional pre-built Credential (uses active profile when omitted)

    Returns:
        UploadResult
    """
    # Normalize to list
    if isinstance(file_paths, str):
        file_paths = [file_paths]
    if not file_paths:
        return UploadResult(success=False, message="没有视频文件可上传")

    # Build description with source attribution
    desc = _build_description(original_description, title, original_title)
    cover = _ensure_cover(cover_path)
    cover_size = image_size(cover)

    print(f"[上传] 标题: {title}")
    upload_tid = tid or config.DEFAULT_TID
    print(f"[上传] 分区: {_format_tid(upload_tid)}")
    print(f"[上传] 标签: {tags or config.DEFAULT_TAGS.split(',')}")
    print(f"[上传] 版权: 转载 (original=False)")
    print(f"[上传] 来源: {original_url}")
    if len(file_paths) == 1:
        print(f"[上传] 文件: {Path(file_paths[0]).name}")
    else:
        print(f"[上传] {len(file_paths)} 个分P:")
        for i, fp in enumerate(file_paths):
            print(f"       P{i+1}: {Path(fp).name}")
    print(f"[上传] 封面: {cover}")
    if cover_size:
        print(f"[上传] 封面尺寸: {cover_size[0]}x{cover_size[1]}")
    print(f"[上传] 开始上传到 B站...")

    # Run async upload
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # Running in an async context (Jupyter, etc.)
            import nest_asyncio
            nest_asyncio.apply()
        result = loop.run_until_complete(
            _upload_async(file_paths, title, desc, tags, tid, original_url, cover, credential)
        )
    except RuntimeError:
        # No event loop running, use asyncio.run
        result = asyncio.run(
            _upload_async(file_paths, title, desc, tags, tid, original_url, cover, credential)
        )

    return result
