#!/usr/bin/env python3
"""Subscription polling glue for the YouTube -> Bilibili pipeline."""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from yt2bili import config
from yt2bili.youtube.subscriptions import (
    VideoItem,
    YouTubeNetworkError,
    chunked,
    execute_youtube_request,
    fetch_recent_videos_api,
    fetch_recent_videos_rss,
    fetch_subscriptions_api,
    get_youtube_service,
    load_channels_file,
    load_subscriptions_cache,
    save_subscriptions_cache,
    sort_videos,
)


STATE_VERSION = 1
STATUS_UPLOADED = "uploaded"
STATUS_FAILED = "failed"
STATUS_SKIPPED_LIVE = "skipped_live"
STATUS_SKIPPED_LONG = "skipped_long"
STATUS_SKIPPED_VERTICAL = "skipped_vertical"
STATUS_SKIPPED_CONTENT = "skipped_content"
LIVE_SKIP_MARKERS = (
    "不是可下载的普通视频",
    "正在直播",
    "预约直播",
    "直播刚结束",
    "is live",
    "upcoming",
)
VERTICAL_SKIP_MARKERS = (
    "检测到竖屏视频",
    "竖屏",
)
CONTENT_SKIP_MARKERS = (
    "内容筛选已跳过",
)
# ── Per-video retry ────────────────────────────────────────────────
_VIDEO_RETRY_MAX = max(0, int(getattr(config, "YOUTUBE_VIDEO_RETRY_MAX", None) or 2))
_VIDEO_RETRY_DELAY = max(10.0, float(getattr(config, "YOUTUBE_VIDEO_RETRY_DELAY", None) or 30))
# Stages whose failures are considered transient (retryable)
_RETRYABLE_STAGES = frozenset({"download", "split", "upload"})
ISO_DURATION_RE = re.compile(
    r"^P"
    r"(?:(?P<days>\d+)D)?"
    r"(?:T"
    r"(?:(?P<hours>\d+)H)?"
    r"(?:(?P<minutes>\d+)M)?"
    r"(?:(?P<seconds>\d+)S)?"
    r")?$"
)

ProcessVideoFunc = Callable[[str], Any]
WriteRunReportFunc = Callable[[list[Any]], Path]


BEIJING_TZ = timezone(timedelta(hours=8))


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def beijing_now() -> str:
    return datetime.now(BEIJING_TZ).isoformat(timespec="seconds")


def project_path(path: str | Path) -> Path:
    resolved = Path(path)
    if resolved.is_absolute():
        return resolved
    return config.PROJECT_ROOT / resolved


def video_id_from_url(url: str) -> str:
    parsed = urlparse(url)
    host = parsed.netloc.lower()

    if host.endswith("youtu.be"):
        return parsed.path.strip("/").split("/", 1)[0]

    query_id = parse_qs(parsed.query).get("v", [""])[0]
    if query_id:
        return query_id

    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) >= 2 and parts[0] in {"embed", "shorts", "live"}:
        return parts[1]

    return ""


def parse_iso8601_duration_seconds(value: str) -> int:
    """Parse YouTube ISO-8601 duration like PT1H02M03S."""
    match = ISO_DURATION_RE.match(value or "")
    if not match:
        return 0
    days = int(match.group("days") or 0)
    hours = int(match.group("hours") or 0)
    minutes = int(match.group("minutes") or 0)
    seconds = int(match.group("seconds") or 0)
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


def format_duration(seconds: int) -> str:
    if seconds <= 0:
        return "未知时长"
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


def fetch_video_queue_details(
    *,
    video_ids: list[str],
    client_secret_file: Path,
    token_file: Path,
) -> dict[str, dict[str, Any]]:
    """Fetch duration and live-archive hints for queue ordering."""
    if not video_ids:
        return {}

    youtube = get_youtube_service(client_secret_file, token_file)
    details: dict[str, dict[str, Any]] = {}
    for chunk in chunked(video_ids, 50):
        request = youtube.videos().list(
            part="contentDetails,snippet,liveStreamingDetails",
            id=",".join(chunk),
            maxResults=50,
        )
        response = execute_youtube_request(request)
        for item in response.get("items", []):
            video_id = item.get("id", "")
            content = item.get("contentDetails", {})
            snippet = item.get("snippet", {})
            live_details = item.get("liveStreamingDetails", {})
            duration_seconds = parse_iso8601_duration_seconds(content.get("duration", ""))
            live_broadcast_content = snippet.get("liveBroadcastContent", "")
            is_live_archive = bool(
                live_details.get("actualStartTime")
                and live_details.get("actualEndTime")
            )
            details[video_id] = {
                "duration_seconds": duration_seconds,
                "is_live_archive": is_live_archive,
                "live_broadcast_content": live_broadcast_content,
            }
    return details


def queue_defer_reason(detail: dict[str, Any], threshold_minutes: int) -> str:
    if not detail:
        return ""
    if detail.get("is_live_archive"):
        return "直播回放"
    duration_seconds = int(detail.get("duration_seconds") or 0)
    threshold_seconds = max(1, threshold_minutes) * 60
    if duration_seconds >= threshold_seconds:
        return f"超长视频≥{threshold_minutes}分钟"
    return ""


def sort_candidates_for_queue(
    candidates: list[VideoItem],
    details: dict[str, dict[str, Any]],
    threshold_minutes: int,
) -> list[VideoItem]:
    """Move likely large videos to the end while preserving relative order."""
    if threshold_minutes <= 0:
        return candidates

    deferred: list[tuple[VideoItem, str, int]] = []
    normal: list[VideoItem] = []
    for video in candidates:
        detail = details.get(video.video_id, {})
        reason = queue_defer_reason(detail, threshold_minutes)
        if reason:
            deferred.append((video, reason, int(detail.get("duration_seconds") or 0)))
        else:
            normal.append(video)

    if deferred:
        print(
            f"[订阅] 已将 {len(deferred)} 条直播回放/超长视频排到队尾"
            f"（阈值: {threshold_minutes} 分钟）"
        )
        for video, reason, duration_seconds in deferred:
            print(
                f"[订阅] 队尾: {video.channel_title} | {video.title} "
                f"({reason}, {format_duration(duration_seconds)})"
            )

    return normal + [video for video, _, _ in deferred]


def empty_state() -> dict[str, Any]:
    return {
        "version": STATE_VERSION,
        "generated_at": utc_now(),
        "videos": {},
    }


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return empty_state()

    try:
        state = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"状态文件不是有效 JSON: {path}\n{exc}") from exc

    if not isinstance(state, dict):
        raise SystemExit(f"状态文件格式错误: {path}")

    state.setdefault("version", STATE_VERSION)
    state.setdefault("generated_at", utc_now())
    state.setdefault("videos", {})
    if not isinstance(state["videos"], dict):
        raise SystemExit(f"状态文件 videos 字段格式错误: {path}")
    return state


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    state["generated_at"] = utc_now()
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp_path.replace(path)


def seed_state_from_runs(state: dict[str, Any], runs_dir: Path) -> int:
    """Import successful historical uploads so cleaned files are not reprocessed."""
    if not runs_dir.exists():
        return 0

    seeded = 0
    videos = state["videos"]
    for report_path in sorted(runs_dir.glob("*.json")):
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except Exception:
            continue

        for row in report.get("results", []):
            if not row.get("success"):
                continue
            video_id = video_id_from_url(row.get("url", ""))
            if not video_id:
                continue
            existing = videos.get(video_id, {})
            if existing.get("status") == STATUS_UPLOADED:
                continue
            videos[video_id] = {
                "video_id": video_id,
                "url": row.get("url", ""),
                "title": row.get("original_title", ""),
                "channel_title": existing.get("channel_title", ""),
                "published_at": existing.get("published_at", ""),
                "status": STATUS_UPLOADED,
                "stage": "complete",
                "bvid": row.get("bvid", ""),
                "aid": row.get("aid", 0),
                "translated_title": row.get("translated_title", ""),
                "attempt_count": max(int(existing.get("attempt_count", 0) or 0), 1),
                "first_seen_at": existing.get("first_seen_at", utc_now()),
                "last_attempt_at": report.get("generated_at", utc_now()),
                "last_success_at": report.get("generated_at", utc_now()),
                "error": "",
                "source": "runs",
                "run_report": report_path.name,
            }
            seeded += 1
    return seeded


def fetch_subscription_videos(
    *,
    source: str,
    limit: int,
    max_videos_per_channel: int,
    client_secret_file: Path,
    token_file: Path,
    cache_file: Path,
    channels_file: Path | None = None,
) -> list[VideoItem]:
    source = source.lower()
    if source == "api":
        youtube = get_youtube_service(client_secret_file, token_file)
        subscriptions = fetch_subscriptions_api(youtube)
        save_subscriptions_cache(cache_file, subscriptions)
        videos = fetch_recent_videos_api(youtube, subscriptions, max_videos_per_channel)
    elif source == "rss":
        if channels_file:
            subscriptions = load_channels_file(channels_file)
        else:
            subscriptions = load_subscriptions_cache(cache_file)
        videos = fetch_recent_videos_rss(subscriptions, max_videos_per_channel)
    else:
        raise SystemExit("YOUTUBE_MONITOR_SOURCE / --monitor-source must be api or rss")

    return sort_videos(videos)[:limit]


def is_live_skip_result(result: Any) -> bool:
    if getattr(result, "stage", "") != "download":
        return False
    error = str(getattr(result, "error", "")).lower()
    return any(marker.lower() in error for marker in LIVE_SKIP_MARKERS)


def is_vertical_skip_result(result: Any) -> bool:
    if getattr(result, "stage", "") != "download":
        return False
    error = str(getattr(result, "error", "")).lower()
    return any(marker.lower() in error for marker in VERTICAL_SKIP_MARKERS)


def is_content_skip_result(result: Any) -> bool:
    if getattr(result, "stage", "") != "download":
        return False
    error = str(getattr(result, "error", "")).lower()
    return any(marker.lower() in error for marker in CONTENT_SKIP_MARKERS)


def should_skip_video(state: dict[str, Any], video: VideoItem) -> tuple[bool, str]:
    entry = state["videos"].get(video.video_id)
    if not entry:
        return False, ""
    status = entry.get("status", "")
    if status == STATUS_UPLOADED:
        return True, f"已上传 {entry.get('bvid', '')}".strip()
    if status == STATUS_SKIPPED_LIVE:
        return True, "直播内容已永久跳过"
    if status == STATUS_SKIPPED_LONG:
        return True, "超长视频已永久跳过"
    if status == STATUS_SKIPPED_VERTICAL:
        return True, "竖屏视频已永久跳过"
    if status == STATUS_SKIPPED_CONTENT:
        return True, "内容筛选已跳过"
    return False, ""


def _attempt_count(state: dict[str, Any], video_id: str) -> int:
    current = state["videos"].get(video_id, {})
    return int(current.get("attempt_count", 0) or 0) + 1


def _base_entry(state: dict[str, Any], video: VideoItem, status: str) -> dict[str, Any]:
    current = state["videos"].get(video.video_id, {})
    return {
        "video_id": video.video_id,
        "url": video.url,
        "title": video.title,
        "channel_title": video.channel_title,
        "published_at": video.published_at,
        "status": status,
        "stage": "",
        "bvid": current.get("bvid", ""),
        "aid": current.get("aid", 0),
        "translated_title": current.get("translated_title", ""),
        "attempt_count": _attempt_count(state, video.video_id),
        "first_seen_at": current.get("first_seen_at", utc_now()),
        "last_attempt_at": utc_now(),
        "last_success_at": current.get("last_success_at", ""),
        "error": "",
        "source": "monitor",
    }


def record_success(state: dict[str, Any], video: VideoItem, result: Any) -> None:
    entry = _base_entry(state, video, STATUS_UPLOADED)
    entry.update(
        {
            "stage": "complete",
            "bvid": getattr(result, "bvid", ""),
            "aid": getattr(result, "aid", 0),
            "title": getattr(result, "original_title", "") or video.title,
            "translated_title": getattr(result, "translated_title", ""),
            "last_success_at": utc_now(),
            "error": "",
        }
    )
    state["videos"][video.video_id] = entry


def record_failure(state: dict[str, Any], video: VideoItem, result: Any) -> None:
    if is_live_skip_result(result):
        status = STATUS_SKIPPED_LIVE
    elif is_vertical_skip_result(result):
        status = STATUS_SKIPPED_VERTICAL
    elif is_content_skip_result(result):
        status = STATUS_SKIPPED_CONTENT
    else:
        status = STATUS_FAILED
    entry = _base_entry(state, video, status)
    entry.update(
        {
            "stage": getattr(result, "stage", "unknown"),
            "title": getattr(result, "original_title", "") or video.title,
            "translated_title": getattr(result, "translated_title", ""),
            "bvid": getattr(result, "bvid", ""),
            "aid": getattr(result, "aid", 0),
            "error": getattr(result, "error", ""),
        }
    )
    state["videos"][video.video_id] = entry


def run_monitor_cycle(
    *,
    process_video: ProcessVideoFunc,
    write_run_report: WriteRunReportFunc | None,
    state_path: Path,
    source: str,
    limit: int,
    max_videos_per_channel: int,
    client_secret_file: Path,
    token_file: Path,
    cache_file: Path,
    channels_file: Path | None = None,
    dry_run: bool = False,
) -> list[Any]:
    state = load_state(state_path)
    seeded = seed_state_from_runs(state, project_path(config.RUNS_DIR))
    if seeded:
        print(f"[订阅] 已从 runs 导入 {seeded} 条历史成功记录")

    videos = fetch_subscription_videos(
        source=source,
        limit=limit,
        max_videos_per_channel=max_videos_per_channel,
        client_secret_file=client_secret_file,
        token_file=token_file,
        cache_file=cache_file,
        channels_file=channels_file,
    )

    candidates: list[VideoItem] = []
    skipped = 0
    for video in videos:
        skip, reason = should_skip_video(state, video)
        if skip:
            skipped += 1
            print(f"[订阅] 跳过: {video.channel_title} | {video.title} ({reason})")
        else:
            candidates.append(video)

    print(
        f"[订阅] 本轮获取 {len(videos)} 条，跳过 {skipped} 条，"
        f"待处理 {len(candidates)} 条"
    )

    queue_details: dict[str, dict[str, Any]] = {}

    # ── RSS mode: warn if duration-based features are configured ──
    if source == "rss" and candidates:
        defer_minutes = max(0, int(getattr(config, "YOUTUBE_DEFER_LONG_VIDEO_MINUTES", 0) or 0))
        skip_minutes = max(0, int(getattr(config, "YOUTUBE_SKIP_LONG_VIDEO_MINUTES", 0) or 0))
        if defer_minutes > 0 or skip_minutes > 0:
            print(
                "[订阅] ⚠️ RSS 模式无法获取视频时长，"
                "YOUTUBE_DEFER_LONG_VIDEO_MINUTES / YOUTUBE_SKIP_LONG_VIDEO_MINUTES 不生效"
            )

    if source == "api" and candidates:
        queue_details = fetch_video_queue_details(
            video_ids=[video.video_id for video in candidates],
            client_secret_file=client_secret_file,
            token_file=token_file,
        )
        candidates = sort_candidates_for_queue(
            candidates,
            queue_details,
            config.YOUTUBE_DEFER_LONG_VIDEO_MINUTES,
        )

    # ── Skip videos longer than YOUTUBE_SKIP_LONG_VIDEO_MINUTES ──
    skip_threshold_minutes = max(0, int(getattr(config, "YOUTUBE_SKIP_LONG_VIDEO_MINUTES", 0) or 0))
    if skip_threshold_minutes > 0 and (source == "api" or queue_details):
        if source == "api" and not queue_details:
            queue_details = fetch_video_queue_details(
                video_ids=[video.video_id for video in candidates],
                client_secret_file=client_secret_file,
                token_file=token_file,
            )
        kept: list[VideoItem] = []
        for video in candidates:
            detail = queue_details.get(video.video_id, {})
            duration_seconds = int(detail.get("duration_seconds") or 0)
            if duration_seconds >= skip_threshold_minutes * 60:
                entry = _base_entry(state, video, STATUS_SKIPPED_LONG)
                state["videos"][video.video_id] = entry
                print(
                    f"[订阅] 跳过: {video.channel_title} | {video.title}"
                    f"（超长视频 {format_duration(duration_seconds)} ≥ {skip_threshold_minutes} 分钟，永久跳过）"
                )
            else:
                kept.append(video)
        if len(kept) < len(candidates):
            print(
                f"[订阅] 因超长阈值 ({skip_threshold_minutes} 分钟)"
                f" 跳过 {len(candidates) - len(kept)} 条"
            )
            candidates = kept
            save_state(state_path, state)

    # ── Skip live/upcoming streams (API mode only) ──
    if source == "api" and queue_details:
        kept = []
        for video in candidates:
            detail = queue_details.get(video.video_id, {})
            lbc = detail.get("live_broadcast_content", "")
            if lbc in ("live", "upcoming"):
                entry = _base_entry(state, video, STATUS_SKIPPED_LIVE)
                label = "正在直播" if lbc == "live" else "预约直播"
                entry["error"] = (
                    f"检测到该链接是{label}，"
                    f"不是可下载的普通视频，已跳过: {video.title}"
                )
                state["videos"][video.video_id] = entry
                print(
                    f"[订阅] 跳过: {video.channel_title} | {video.title}"
                    f"（{label}，永久跳过）"
                )
            else:
                kept.append(video)
        if len(kept) < len(candidates):
            print(f"[订阅] 因直播/预约跳过 {len(candidates) - len(kept)} 条")
            candidates = kept
            save_state(state_path, state)

    if dry_run:
        for video in candidates:
            detail = queue_details.get(video.video_id, {})
            reason = queue_defer_reason(detail, config.YOUTUBE_DEFER_LONG_VIDEO_MINUTES)
            suffix = ""
            if reason:
                suffix = f" | 队尾: {reason}, {format_duration(int(detail.get('duration_seconds') or 0))}"
            print(f"[dry-run] {video.published_at} | {video.channel_title} | {video.title} | {video.url}{suffix}")
        print(f"[dry-run] 不下载、不上传、不写入状态: {state_path}")
        return []

    if seeded:
        save_state(state_path, state)

    results: list[Any] = []
    for index, video in enumerate(candidates, start=1):
        print(f"\n[订阅] 处理 {index}/{len(candidates)}: {video.channel_title} | {video.title}")

        result = None
        for retry_attempt in range(_VIDEO_RETRY_MAX + 1):
            result = process_video(video.url)
            if getattr(result, "success", False):
                break

            # Don't retry if the failure is not a transient network/download issue
            stage = getattr(result, "stage", "")
            if stage not in _RETRYABLE_STAGES:
                break

            # Permanent skips (vertical/live/content) — don't retry
            if is_live_skip_result(result) or is_vertical_skip_result(result) or is_content_skip_result(result):
                break

            # Auth errors (401/403) won't self-resolve — don't retry
            error = getattr(result, "error", "")
            if "请重新扫码登录" in error:
                print(f"\n[订阅] 🔐 B站登录凭据已过期，跳过重试（请运行 --login 重新登录）")
                break

            if retry_attempt >= _VIDEO_RETRY_MAX:
                break

            delay = _VIDEO_RETRY_DELAY * (2 ** retry_attempt)
            error = getattr(result, "error", "")
            print(
                f"\n[订阅] ⚠️ 处理失败 ({stage})，{delay:.0f}s 后重试 "
                f"({retry_attempt + 1}/{_VIDEO_RETRY_MAX}): {error[:150]}"
            )
            try:
                time.sleep(delay)
            except KeyboardInterrupt:
                print("\n[订阅] 用户中断重试等待。")
                break

        results.append(result)
        if getattr(result, "success", False):
            record_success(state, video, result)
        else:
            record_failure(state, video, result)
        save_state(state_path, state)

    if results and write_run_report:
        report_path = write_run_report(results)
        success_count = sum(1 for result in results if getattr(result, "success", False))
        print(f"\n[订阅] 本轮完成: 成功 {success_count}, 失败 {len(results) - success_count}")
        print(f"[订阅] 结果记录: {report_path}")

    print(f"[订阅] 状态文件: {state_path}")
    return results


def run_monitor_loop(
    *,
    process_video: ProcessVideoFunc,
    write_run_report: WriteRunReportFunc | None,
    interval_seconds: int,
    once: bool,
    dry_run: bool,
    **cycle_kwargs: Any,
) -> int:
    if interval_seconds <= 0:
        raise SystemExit("--monitor-interval must be positive")

    monitor_max_retries = max(0, int(getattr(config, "YOUTUBE_MONITOR_MAX_RETRIES", 5)))
    monitor_retry_base = max(10.0, float(getattr(config, "YOUTUBE_MONITOR_RETRY_DELAY", 30)))

    consecutive_failures = 0
    while True:
        print("=" * 60)
        print(f"[订阅] 开始检查: {beijing_now()}")
        print("=" * 60)

        try:
            run_monitor_cycle(
                process_video=process_video,
                write_run_report=write_run_report,
                dry_run=dry_run,
                **cycle_kwargs,
            )
            consecutive_failures = 0  # reset on success
        except YouTubeNetworkError as exc:
            consecutive_failures += 1
            if once and consecutive_failures > monitor_max_retries:
                print(f"\n[订阅] ❌ 单次检查失败，已达最大重试次数 {monitor_max_retries}: {exc}")
                return 1
            if not once and consecutive_failures > monitor_max_retries:
                print(
                    f"\n[订阅] ⚠️ 连续失败 {consecutive_failures} 次，"
                    f"已达最大重试次数 {monitor_max_retries}，等待 {interval_seconds}s 后重置计数"
                )
                try:
                    time.sleep(interval_seconds)
                except KeyboardInterrupt:
                    print("\n[订阅] 已停止轮询。")
                    return 0
                consecutive_failures = 0
                continue
            delay = min(monitor_retry_base * (2 ** (consecutive_failures - 1)), 600.0)
            print(f"\n[订阅] ⚠️ 网络错误，{delay:.0f}s 后重试 ({consecutive_failures}/{monitor_max_retries}): {exc}")
            try:
                time.sleep(delay)
            except KeyboardInterrupt:
                print("\n[订阅] 已停止轮询。")
                return 0
            continue

        if once:
            return 0

        print(f"\n[订阅] 等待 {interval_seconds} 秒后再次检查，按 Ctrl+C 停止。")
        try:
            time.sleep(interval_seconds)
        except KeyboardInterrupt:
            print("\n[订阅] 已停止轮询。")
            return 0
