"""
Bilibili video uploader using bilibili-api-python.
Uploads videos with copyright=2 (转载/repost).
"""

import asyncio
import tempfile
from pathlib import Path
from dataclasses import dataclass

from bilibili_api import video_uploader

import config
import auth
from cover import image_size, is_valid_image


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


def _build_credential():
    """Get Bilibili Credential (auto-login via QR if first time)."""
    return auth.get_credential()


def _build_description(
    original_url: str,
    original_description: str,
    translated_title: str,
    original_title: str = "",
) -> str:
    """Build the video description for B站 upload."""
    parts = []
    # Source attribution (required for 转载)
    parts.append(f"原视频链接: {original_url}")
    if original_title:
        parts.append(f"原标题: {original_title}")
    parts.append(f"翻译标题: {translated_title}")

    # Include original description if not too long
    if original_description:
        desc_lines = original_description.strip().split("\n")
        # Limit to first 10 lines of original description
        short_desc = "\n".join(desc_lines[:10])
        if len(desc_lines) > 10:
            short_desc += "\n..."
        parts.append(f"\n原视频简介:\n{short_desc}")

    return "\n".join(parts)


async def _upload_async(
    file_path: str,
    title: str,
    desc: str = "",
    tags: list[str] | None = None,
    tid: int | None = None,
    source_url: str = "",
    cover_path: str | None = None,
) -> UploadResult:
    """
    Async upload a video to Bilibili.

    Args:
        file_path: Path to the video file (.mp4)
        title: Video title on B站
        desc: Video description
        tags: List of tags
        tid: Category ID (default from config)
        source_url: Original source URL (for 转载)
        cover_path: Optional cover image path

    Returns:
        UploadResult with status and BV/AV numbers
    """
    credential = _build_credential()

    # Truncate description to B站 limit (2000 chars)
    if len(desc) > 2000:
        desc = desc[:1997] + "..."

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

    # Create upload page (single-part video)
    page = video_uploader.VideoUploaderPage(
        path=file_path,
        title=title,
    )

    # Create uploader and start
    uploader = video_uploader.VideoUploader(
        pages=[page],
        meta=vu_meta,
        credential=credential,
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
    except Exception as e:
        return UploadResult(
            success=False,
            message=f"上传失败: {e}",
        )


def upload_video(
    file_path: str,
    title: str,
    original_url: str = "",
    original_description: str = "",
    original_title: str = "",
    tags: list[str] | None = None,
    tid: int | None = None,
    cover_path: str | None = None,
) -> UploadResult:
    """
    Upload a video to Bilibili (synchronous wrapper).

    Args:
        file_path: Path to the video file
        title: Translated title for B站
        original_url: Original YouTube URL (shown in description as source)
        original_description: Original video description
        original_title: Original YouTube title
        tags: Tags list
        tid: Category ID
        cover_path: Optional cover image path

    Returns:
        UploadResult
    """
    # Build description with source attribution
    desc = _build_description(original_url, original_description, title, original_title)
    cover = _ensure_cover(cover_path)
    cover_size = image_size(cover)

    print(f"[上传] 标题: {title}")
    upload_tid = tid or config.DEFAULT_TID
    print(f"[上传] 分区: {_format_tid(upload_tid)}")
    print(f"[上传] 标签: {tags or config.DEFAULT_TAGS.split(',')}")
    print(f"[上传] 版权: 转载 (original=False)")
    print(f"[上传] 来源: {original_url}")
    print(f"[上传] 文件: {Path(file_path).name}")
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
            _upload_async(file_path, title, desc, tags, tid, original_url, cover)
        )
    except RuntimeError:
        # No event loop running, use asyncio.run
        result = asyncio.run(
            _upload_async(file_path, title, desc, tags, tid, original_url, cover)
        )

    return result
