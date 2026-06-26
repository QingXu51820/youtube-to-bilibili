"""
Pipeline: Discord message → Bilibili dynamic (动态) post.

Steps:
    1. Download image attachments from Discord CDN
    2. Optionally translate message text to Chinese
    3. Format content (translated text + attribution)
    4. Post as Bilibili dynamic via bilibili-api-python
"""

import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import aiohttp

from yt2bili import config


@dataclass
class DiscordMessage:
    """A Discord message ready for processing."""
    message_id: str
    channel_id: str
    channel_name: str
    author_name: str
    content: str
    attachment_urls: list[str] = field(default_factory=list)
    published_at: str = ""
    jump_url: str = ""


@dataclass
class PublishResult:
    """Result of publishing a message to Bilibili."""
    message_id: str
    success: bool = False
    error: str = ""
    bilibili_dyn_id: str = ""
    translated_content: str = ""


async def _download_image(session: aiohttp.ClientSession, url: str, dest_dir: str) -> str | None:
    """Download a single image from a URL to a local directory. Returns the local path."""
    try:
        async with session.get(url) as resp:
            if resp.status != 200:
                return None
            data = await resp.read()
    except Exception:
        return None

    # Derive filename from URL or fall back to a timestamp-based name
    filename = url.split("/")[-1].split("?")[0]
    if not filename or "." not in filename:
        ext = "png"
        filename = f"{datetime.now().strftime('%Y%m%d%H%M%S%f')}.{ext}"
    dest = os.path.join(dest_dir, filename)
    with open(dest, "wb") as f:
        f.write(data)
    return dest


async def _download_attachments(
    attachment_urls: list[str],
    max_images: int = 9,
) -> list[str]:
    """Download image attachments, up to max_images. Returns list of local paths."""
    if not attachment_urls:
        return []

    tmpdir = tempfile.mkdtemp(prefix="discord_")
    urls = attachment_urls[:max_images]

    async with aiohttp.ClientSession() as session:
        tasks = [_download_image(session, url, tmpdir) for url in urls]
        results = []
        for coro in tasks:
            path = await coro
            if path:
                results.append(path)
    return results


def _translate_content(content: str) -> str:
    """Translate message content to Chinese if enabled and content is non-empty."""
    if not content or not content.strip():
        return ""

    if not config.DISCORD_TRANSLATE:
        return content

    try:
        from yt2bili.translation.translator import translate
        result = translate(content, source_lang=config.SOURCE_LANG)
        if result:
            return result
    except Exception:
        pass

    return content


def _cleanup_files(paths: list[str]) -> None:
    """Remove temporary image files and their directory."""
    for p in paths:
        try:
            os.remove(p)
        except OSError:
            pass
    if paths:
        tmpdir = os.path.dirname(paths[0])
        try:
            os.rmdir(tmpdir)
        except OSError:
            pass


async def publish_message(msg: DiscordMessage) -> PublishResult:
    """Process a Discord message and publish it as a Bilibili dynamic.

    Returns:
        PublishResult with success/failure details.
    """
    result = PublishResult(message_id=msg.message_id)

    try:
        # Step 1: Translate text
        translated = _translate_content(msg.content)
        result.translated_content = translated

        # Step 2: Download attachments
        local_images = await _download_attachments(
            msg.attachment_urls,
            max_images=config.DISCORD_MAX_IMAGES,
        )

        # Step 3: Build dynamic content
        text = _build_dynamic_text(translated, msg)
        if not text and not local_images:
            result.error = "消息无文本且无附件，跳过"
            return result

        # Step 4: Post to Bilibili
        dyn_id = await _post_dynamic(text, local_images)
        if not dyn_id:
            result.error = "动态发布返回空 ID"
            return result

        result.success = True
        result.bilibili_dyn_id = dyn_id

        # Step 5: Cleanup temp files
        _cleanup_files(local_images)

    except Exception as e:
        result.error = str(e)

    return result


def _build_dynamic_text(translated: str, msg: DiscordMessage) -> str:
    """Format the dynamic text with translation and attribution."""
    parts = []

    if translated:
        parts.append(translated)

    return "\n".join(parts)


async def _post_dynamic(text: str, image_paths: list[str]) -> str:
    """Post a Bilibili dynamic with text and images. Returns dynamic_id or empty string."""
    from bilibili_api import Credential
    from bilibili_api.dynamic import BuildDynamic

    credential = Credential(
        sessdata=config.BILI_SESSDATA,
        bili_jct=config.BILI_BILI_JCT,
        buvid3=config.BILI_BUVID3,
    )

    post = BuildDynamic.empty()

    # Add images first (Bilibili renders images before text in dynamics)
    for img_path in image_paths:
        post = post.add_image(img_path)

    # Add text
    if text:
        post = post.add_text(text)

    # If nothing to post, raise
    if not text and not image_paths:
        raise ValueError("动态内容为空（无文本且无图片）")

    # Send
    resp = await post.send_dynamic(credential)
    # resp is a dict-like object with dynamic_id
    dyn_id = resp.get("dynamic_id", "") if isinstance(resp, dict) else getattr(resp, "dynamic_id", "")
    return str(dyn_id) if dyn_id else ""
