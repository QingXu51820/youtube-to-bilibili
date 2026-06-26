"""
Discord message monitor — listens to channel messages via Gateway WebSocket,
filters, deduplicates via persistent state, and publishes to Bilibili dynamics.

Two entry modes:
    1. Real-time (default) — on_message Gateway event
    2. Fallback poll — REST API GET /channels/{id}/messages at startup
"""

import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from yt2bili import config

# ── State file helpers ────────────────────────────────────────────────

def _load_state(path: Path) -> dict[str, Any]:
    """Load the Discord message state file, returning a bare dict."""
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and "messages" in data:
                return data
        except (json.JSONDecodeError, OSError):
            pass
    return {"version": 1, "messages": {}}


def _save_state(path: Path, state: dict[str, Any]) -> None:
    """Persist state to disk."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    tmp.replace(path)


def _is_processed(state: dict[str, Any], message_id: str) -> bool:
    """Check if a message has already been processed."""
    return str(message_id) in state.get("messages", {})


def _mark_processed(state: dict[str, Any], message_id: str, dyn_id: str = "",
                    error: str = "") -> None:
    """Record a message in the state file."""
    status = "published" if dyn_id else ("failed" if error else "processed")
    state["messages"][str(message_id)] = {
        "message_id": str(message_id),
        "status": status,
        "bilibili_dyn_id": dyn_id,
        "error": error,
        "processed_at": datetime.now(timezone.utc).isoformat(),
    }


def _channel_ids() -> list[int]:
    """Parse DISCORD_CHANNEL_IDS config into a list of int channel IDs."""
    raw = config.DISCORD_CHANNEL_IDS.strip()
    if not raw:
        return []
    ids = []
    for part in raw.split(","):
        part = part.strip()
        if part.isdigit():
            ids.append(int(part))
    return ids


# ── Fallback REST polling ─────────────────────────────────────────────

async def _fetch_recent_messages(
    http_session, channel_id: int, limit: int
) -> list[dict[str, Any]]:
    """Fetch recent messages from a Discord channel via REST API."""
    import aiohttp
    url = f"https://discord.com/api/v10/channels/{channel_id}/messages?limit={limit}"
    headers = {"Authorization": f"Bot {config.DISCORD_BOT_TOKEN}"}
    try:
        async with http_session.get(url, headers=headers) as resp:
            if resp.status == 200:
                return await resp.json()
            else:
                text = await resp.text()
                print(f"[Discord] ⚠️ REST 获取频道 {channel_id} 消息失败: HTTP {resp.status} — {text[:200]}")
    except Exception as e:
        print(f"[Discord] ⚠️ REST 请求异常: {e}")
    return []


def _parse_message(raw: dict[str, Any]) -> dict[str, Any] | None:
    """Parse a raw Discord API message dict into our internal format."""
    msg_id = raw.get("id", "")
    author = raw.get("author", {})
    if not msg_id:
        return None

    # Attachments
    attachment_urls = []
    for att in raw.get("attachments", []):
        url = att.get("url", "")
        if url:
            attachment_urls.append(url)

    # Embeds may contain image URLs
    for embed in raw.get("embeds", []):
        img = embed.get("image", {}).get("url", "")
        if img:
            attachment_urls.append(img)
        thumb = embed.get("thumbnail", {}).get("url", "")
        if thumb:
            attachment_urls.append(thumb)

    content = (raw.get("content") or "").strip()
    return {
        "message_id": str(msg_id),
        "channel_id": str(raw.get("channel_id", "")),
        "author_name": author.get("global_name") or author.get("username", ""),
        "content": content,
        "attachment_urls": attachment_urls,
        "published_at": raw.get("timestamp", ""),
        "author_bot": author.get("bot", False),
    }


# ── Message processing entry point ────────────────────────────────────

async def _process_message(raw_msg: dict[str, Any], state: dict[str, Any],
                           state_path: Path) -> None:
    """Process a single Discord message through filter → publish → record."""
    from yt2bili.discord.publisher import DiscordMessage, publish_message

    parsed = _parse_message(raw_msg)
    if not parsed:
        return

    msg_id = parsed["message_id"]

    # Deduplicate
    if _is_processed(state, msg_id):
        return

    # Filter: skip bots
    if config.DISCORD_SKIP_BOTS and parsed.get("author_bot"):
        _mark_processed(state, msg_id, error="bot 消息已跳过")
        _save_state(state_path, state)
        return

    # Filter: skip empty messages without attachments
    if config.DISCORD_SKIP_EMPTY and not parsed["content"] and not parsed["attachment_urls"]:
        _mark_processed(state, msg_id, error="空消息已跳过")
        _save_state(state_path, state)
        return

    print(
        f"[Discord] 处理消息 {msg_id}: "
        f"{parsed['author_name']} — {parsed['content'][:60]}"
    )

    # Build DiscordMessage and publish
    msg = DiscordMessage(
        message_id=parsed["message_id"],
        channel_id=parsed["channel_id"],
        channel_name="",  # populated by channel cache if available
        author_name=parsed["author_name"],
        content=parsed["content"],
        attachment_urls=parsed["attachment_urls"],
        published_at=parsed["published_at"],
    )

    result = await publish_message(msg)

    if result.success:
        print(f"[Discord] ✅ 动态发布成功: {result.bilibili_dyn_id}")
        _mark_processed(state, msg_id, dyn_id=result.bilibili_dyn_id)
    else:
        print(f"[Discord] ❌ 发布失败: {result.error}")
        _mark_processed(state, msg_id, error=result.error)

    _save_state(state_path, state)


# ── Bot creation ───────────────────────────────────────────────────────

def _create_bot(state: dict[str, Any], state_path: Path, channel_ids: list[int]):
    """Create and return a discord.py Bot instance with handlers wired."""
    import discord

    intents = discord.Intents.default()
    intents.message_content = True
    intents.guild_messages = True

    # discord.py 2.x natively supports proxy via the Client constructor
    bot_kwargs: dict[str, Any] = {"intents": intents}
    if config.DISCORD_PROXY:
        bot_kwargs["proxy"] = config.DISCORD_PROXY
        print(f"[Discord] 使用代理: {config.DISCORD_PROXY}")

    bot = discord.Client(**bot_kwargs)

    @bot.event
    async def on_ready():
        print(f"[Discord] ✅ 已登录: {bot.user.name} ({bot.user.id})")
        channels_str = ", ".join(str(cid) for cid in channel_ids)
        print(f"[Discord] 监听频道: {channels_str}")

        # Fallback: poll recent messages for each channel
        session_kwargs: dict[str, Any] = {}
        if config.DISCORD_PROXY:
            session_kwargs["proxy"] = config.DISCORD_PROXY
        async with aiohttp.ClientSession(**session_kwargs) as session:
            for cid in channel_ids:
                msgs = await _fetch_recent_messages(session, cid, config.DISCORD_FALLBACK_LIMIT)
                print(f"[Discord] 频道 {cid} 回溯获取 {len(msgs)} 条历史消息")
                for raw in reversed(msgs):  # oldest first
                    if not _is_processed(state, str(raw.get("id", ""))):
                        await _process_message(raw, state, state_path)

    @bot.event
    async def on_message(message: discord.Message):
        # Ignore own messages
        if message.author == bot.user:
            return

        # Channel filter
        if message.channel.id not in channel_ids:
            return

        # Convert to raw-like dict and process
        raw = _message_to_dict(message)
        await _process_message(raw, state, state_path)

    return bot


def _message_to_dict(message) -> dict[str, Any]:
    """Convert a discord.py Message to a dict matching the REST API format."""
    attachment_urls = [att.url for att in message.attachments]
    for embed in message.embeds:
        if embed.image and embed.image.url:
            attachment_urls.append(embed.image.url)
        if embed.thumbnail and embed.thumbnail.url:
            attachment_urls.append(embed.thumbnail.url)

    return {
        "id": str(message.id),
        "channel_id": str(message.channel.id),
        "author": {
            "global_name": message.author.global_name,
            "username": message.author.name,
            "bot": message.author.bot,
        },
        "content": message.content or "",
        "attachments": [{"url": url} for url in attachment_urls],
        "embeds": [],
        "timestamp": message.created_at.isoformat() if message.created_at else "",
    }


# ── Channel name resolution ───────────────────────────────────────────

async def _resolve_channel_names(bot, channel_ids: list[int]) -> dict[int, str]:
    """Build a channel_id → channel_name cache."""
    import discord
    names: dict[int, str] = {}
    for cid in channel_ids:
        channel = bot.get_channel(cid)
        if channel is None:
            try:
                channel = await bot.fetch_channel(cid)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                names[cid] = f"unknown-{cid}"
                continue
        names[cid] = getattr(channel, "name", f"unknown-{cid}")
    return names


# ── Public API ─────────────────────────────────────────────────────────

async def run_discord_monitor() -> None:
    """Run the Discord message monitor indefinitely.

    Connects via Gateway WebSocket. Messages matching the configured
    channel filter are translated and published to Bilibili dynamics.
    State is persisted to DISCORD_STATE_FILE for deduplication across
    restarts.
    """
    import aiohttp

    channel_ids = _channel_ids()
    if not channel_ids:
        print("[Discord] ❌ 未配置 DISCORD_CHANNEL_IDS，无法启动监控")
        print("   请在 .env 中设置 DISCORD_CHANNEL_IDS=频道ID1,频道ID2")
        return

    if not config.DISCORD_BOT_TOKEN:
        print("[Discord] ❌ 未配置 DISCORD_BOT_TOKEN，无法启动监控")
        print("   请在 .env 中设置 DISCORD_BOT_TOKEN 或在 Discord Developer Portal 创建 Bot")
        return

    # Check Bilibili credentials
    if not config.BILI_SESSDATA or not config.BILI_BILI_JCT:
        print("[Discord] ❌ 未配置 B站 登录凭据 (BILI_SESSDATA / BILI_BILI_JCT)")
        print("   请先运行 python main.py --login 登录 B站")
        return

    state_path = Path(config.DISCORD_STATE_FILE)
    state = _load_state(state_path)
    processed_count = len(state.get("messages", {}))
    print(f"[Discord] 已加载状态: {processed_count} 条已处理消息")
    print(f"[Discord] 状态文件: {state_path}")

    bot = _create_bot(state, state_path, channel_ids)

    try:
        await bot.start(config.DISCORD_BOT_TOKEN)
    except KeyboardInterrupt:
        print("\n[Discord] 收到中断信号，正在关闭...")
    finally:
        if not bot.is_closed():
            await bot.close()
        _save_state(state_path, state)
        print("[Discord] 状态已保存，退出。")
