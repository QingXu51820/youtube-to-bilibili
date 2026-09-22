"""
Retire local records for videos that no longer exist on Bilibili.

When a 稿件 is deleted on B站 (by the user or by the platform), the local
state files keep pointing at it: the 合集 queue retries ``-404`` every round
and the subtitle queue keeps polling a CID that will never appear (30s per
video per cycle). :func:`scan_profile` probes every bvid the profile still
references and retires the ones B站 reports as gone:

* ``processed_videos.json`` → ``status = "deleted"`` (tombstone: keeps the
  record so the video is never re-uploaded if it resurfaces in a feed)
* the 合集 and 字幕 queues → the entry is dropped
* local subtitle files / bvid date caches → cleaned up

Only codes in :data:`yt2bili.bilibili.subtitle.GONE_CODES` count as gone;
anything else (network errors, rate limits, unknown codes) leaves the record
untouched. Entries younger than :data:`FRESH_GUARD_HOURS` are skipped — a
freshly uploaded video is reported as ``62002`` while it is still being
reviewed, and must not be retired.
"""

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from yt2bili import atomic_io
from yt2bili.subtitles.paths import source_srt_path, translated_srt_path
from yt2bili.timestamps import parse_iso, utc_now
from yt2bili.bilibili import subtitle as subtitle_mod
from yt2bili.bilibili.collection import (
    load_pending_collections,
    pending_collections_path,
    save_pending_collections,
)

# 每次探测之间的间隔，避免触发 B站 限流
PROBE_INTERVAL_SECONDS = 0.25
# 探测返回 -412（请求被拦截）时的退避秒数
RATE_LIMIT_BACKOFF = (5.0, 15.0, 30.0)
_RATE_LIMIT_CODE = -412
# 上传不满这段时间的条目不做判定（审核中的稿件同样返回 62002）
FRESH_GUARD_HOURS = 24.0

VERDICT_ALIVE = "alive"
VERDICT_GONE = "gone"
VERDICT_UNKNOWN = "unknown"


@dataclass
class Candidate:
    """A bvid still referenced by the profile's local records."""

    bvid: str
    aid: int = 0
    video_id: str = ""
    title: str = ""
    channel_title: str = ""
    sources: list[str] = field(default_factory=list)
    stamps: list[str] = field(default_factory=list)

    def age_hours(self) -> float:
        """Hours since the newest known timestamp; ``inf`` when none is usable."""
        newest: datetime | None = None
        for raw in self.stamps:
            parsed = parse_iso(raw)
            if parsed is not None and (newest is None or parsed > newest):
                newest = parsed
        if newest is None:
            return float("inf")
        return (datetime.now(timezone.utc) - newest).total_seconds() / 3600.0


@dataclass
class CleanupReport:
    """Outcome of one profile scan."""

    profile: str = ""
    checked: int = 0
    alive: int = 0
    gone: list[Candidate] = field(default_factory=list)
    unknown: list[tuple[Candidate, str]] = field(default_factory=list)
    skipped_fresh: list[Candidate] = field(default_factory=list)
    dry_run: bool = False

    @property
    def cleaned(self) -> int:
        return len(self.gone)


# ── Candidate collection ──────────────────────────────────────────────

def _processed_path(profile=None) -> Path:
    """
    Path of the profile's processed-videos state.

    Mirrors the collection sweep's resolution (``state_path or
    queue_path.parent / "processed_videos.json"``): the state file always sits
    next to the profile's 合集 queue, unless the profile overrides
    ``youtube.monitor_state``.
    """
    from yt2bili import profile as profile_mod
    if profile is not None and profile_mod.is_profile_state_active():
        return profile_mod.get_state_file_path(profile)
    return profile_mod.state_file_path("processed_videos.json")


def collect_candidates(
    processed_path: Path,
    collections_path: Path,
    subtitles_path: Path,
) -> list[Candidate]:
    """
    Gather every bvid the profile still references, merged by bvid.

    Videos already tombstoned (``status == "deleted"``) and absent from both
    queues are skipped: a re-run of the cleanup is a no-op for them.
    """
    by_bvid: dict[str, Candidate] = {}

    def add(bvid: str, source: str, **fields) -> None:
        bvid = str(bvid or "")
        if not bvid:
            return
        cand = by_bvid.setdefault(bvid, Candidate(bvid=bvid))
        if source not in cand.sources:
            cand.sources.append(source)
        for key, value in fields.items():
            if key == "stamp":
                if value and value not in cand.stamps:
                    cand.stamps.append(str(value))
            elif value and not getattr(cand, key, ""):
                setattr(cand, key, value)

    state = atomic_io.read_json(processed_path, {})
    videos = state.get("videos", {}) if isinstance(state, dict) else {}
    if isinstance(videos, dict):
        for video_id, entry in videos.items():
            if not isinstance(entry, dict):
                continue
            if str(entry.get("status", "")) == "deleted":
                continue
            add(
                entry.get("bvid", ""), "processed",
                video_id=video_id,
                aid=int(entry.get("aid", 0) or 0),
                title=str(entry.get("title", "") or ""),
                channel_title=str(entry.get("channel_title", "") or ""),
                stamp=entry.get("last_success_at") or entry.get("last_attempt_at")
                or entry.get("first_seen_at") or "",
            )

    for entry in load_pending_collections(collections_path):
        if not isinstance(entry, dict):
            continue
        add(
            entry.get("bvid", ""), "collections",
            video_id=str(entry.get("video_id", "") or ""),
            aid=int(entry.get("aid", 0) or 0),
            channel_title=str(entry.get("channel_title", "") or ""),
            stamp=entry.get("added_at", ""),
        )

    queue = atomic_io.read_json(subtitles_path, [])
    if isinstance(queue, list):
        for entry in queue:
            if not isinstance(entry, dict):
                continue
            add(
                entry.get("bvid", ""), "subtitles",
                aid=int(entry.get("aid", 0) or 0),
                stamp=entry.get("added_at", ""),
            )

    return list(by_bvid.values())


# ── Liveness probe ────────────────────────────────────────────────────

def probe_bvid(bvid: str, aid: int) -> tuple[str, int | None]:
    """
    Ask B站 whether the 稿件 is still there.

    Returns ``(verdict, code)``. ``verdict`` is one of :data:`VERDICT_ALIVE`,
    :data:`VERDICT_GONE`, :data:`VERDICT_UNKNOWN`; ``code`` is the B站 error
    code when one was reported.
    """
    for attempt in range(len(RATE_LIMIT_BACKOFF) + 1):
        try:
            pages = subtitle_mod.get_video_pages(bvid=bvid, aid=aid)
        except Exception as e:
            code = subtitle_mod.extract_code(str(e))
            if code == _RATE_LIMIT_CODE and attempt < len(RATE_LIMIT_BACKOFF):
                time.sleep(RATE_LIMIT_BACKOFF[attempt])
                continue
            return (
                VERDICT_GONE if subtitle_mod.is_gone_code(code) else VERDICT_UNKNOWN,
                code,
            )
        if pages and int(pages[0].get("cid", 0) or 0) > 0:
            return VERDICT_ALIVE, 0
        # 稿件存在但分 P 还没就绪：不算消失，也不算确认存活
        return VERDICT_UNKNOWN, None
    return VERDICT_UNKNOWN, _RATE_LIMIT_CODE


# ── Purge ─────────────────────────────────────────────────────────────

def _backup(path: Path, stamp: str) -> None:
    """Copy *path* to ``<name>.bak-clean-<stamp>`` before a destructive write."""
    if not path.exists():
        return
    backup = path.with_name(f"{path.name}.bak-clean-{stamp}")
    try:
        backup.write_bytes(path.read_bytes())
    except OSError as e:
        print(f"[清理] [WARN] 备份失败 {path.name}: {e}")


def _write_json(path: Path, payload) -> None:
    atomic_io.write_json(path, payload)


def _drop_cache_keys(path: Path, bvids: set[str]) -> int:
    """Remove *bvids* from a ``{bvid: date}`` cache file. Returns keys dropped."""
    data = atomic_io.read_json(path, None)
    if not isinstance(data, dict):
        return 0
    dropped = [k for k in data if k in bvids]
    if not dropped:
        return 0
    for key in dropped:
        del data[key]
    _write_json(path, data)
    return len(dropped)


def _drop_subtitle_files(video_id: str) -> list[str]:
    """Delete the downloaded/translated subtitle files of a retired video."""
    if not video_id:
        return []
    removed = []
    for path in (translated_srt_path(video_id), source_srt_path(video_id)):
        try:
            if path.exists():
                path.unlink()
                removed.append(path.name)
        except OSError as e:
            print(f"[清理] [WARN] 无法删除 {path.name}: {e}")
    return removed


def apply_purge(
    gone: list[Candidate],
    processed_path: Path,
    collections_path: Path,
    subtitles_path: Path,
    *,
    dry_run: bool = False,
    stamp: str | None = None,
) -> dict:
    """
    Retire *gone* candidates across every local record.

    Returns a summary dict with the counts of what was (or would be) changed.
    """
    stamp = stamp or datetime.now().strftime("%Y%m%d-%H%M%S")
    bvids = {c.bvid for c in gone}
    video_ids = {c.video_id for c in gone if c.video_id}
    summary = {
        "processed_marked": 0,
        "collections_removed": 0,
        "subtitles_removed": 0,
        "files_removed": [],
        "cache_keys_removed": 0,
    }
    if not bvids or dry_run:
        return summary

    # 1. processed_videos.json → tombstone
    state = atomic_io.read_json(processed_path, None)
    if isinstance(state, dict) and isinstance(state.get("videos"), dict):
        touched = 0
        for video_id, entry in state["videos"].items():
            if not isinstance(entry, dict):
                continue
            if entry.get("bvid") not in bvids:
                continue
            entry["status"] = "deleted"
            entry["error"] = "稿件已在B站消失（B站清理时标记）"
            entry["last_attempt_at"] = utc_now()
            touched += 1
        if touched:
            _backup(processed_path, stamp)
            _write_json(processed_path, state)
            summary["processed_marked"] = touched

    # 2. 合集队列
    entries = load_pending_collections(collections_path)
    kept = [e for e in entries if str(e.get("bvid", "")) not in bvids]
    if len(kept) != len(entries):
        _backup(collections_path, stamp)
        save_pending_collections(collections_path, kept)
        summary["collections_removed"] = len(entries) - len(kept)

    # 3. 字幕队列
    queue = atomic_io.read_json(subtitles_path, [])
    if isinstance(queue, list):
        kept_q = [e for e in queue if str(e.get("bvid", "")) not in bvids]
        if len(kept_q) != len(queue):
            _backup(subtitles_path, stamp)
            if kept_q:
                _write_json(subtitles_path, kept_q)
            else:
                subtitles_path.unlink()
            summary["subtitles_removed"] = len(queue) - len(kept_q)

    # 4. 本地字幕文件 + 日期缓存
    for video_id in sorted(video_ids):
        summary["files_removed"].extend(_drop_subtitle_files(video_id))
    for name in ("bvid_dates.json", "bili_dates.json"):
        summary["cache_keys_removed"] += _drop_cache_keys(
            processed_path.parent / name, bvids
        )

    return summary


# ── Entry point ───────────────────────────────────────────────────────

def scan_profile(
    *,
    dry_run: bool = False,
    profile_name: str | None = None,
    profile=None,
    progress_every: int = 25,
    probe=None,
) -> CleanupReport:
    """
    Probe every bvid the active profile references and retire the vanished ones.

    ``probe`` defaults to :func:`probe_bvid`; tests inject a fake.
    """
    from yt2bili import profile as profile_mod

    profile_name = profile_name or profile_mod.get_active_profile_name()
    probe = probe or probe_bvid
    processed_path = _processed_path(profile)
    collections_path = pending_collections_path()
    subtitles_path = subtitle_mod.pending_subtitles_path()

    report = CleanupReport(profile=profile_name, dry_run=dry_run)
    candidates = collect_candidates(processed_path, collections_path, subtitles_path)
    report.checked = len(candidates)
    print(f"[清理] 账号 '{profile_name}': 检查 {len(candidates)} 个视频在 B站 的状态...")

    for index, cand in enumerate(candidates, 1):
        if cand.age_hours() < FRESH_GUARD_HOURS:
            report.skipped_fresh.append(cand)
            continue
        verdict, code = probe(cand.bvid, cand.aid)
        if verdict == VERDICT_ALIVE:
            report.alive += 1
        elif verdict == VERDICT_GONE:
            report.gone.append(cand)
            print(
                f"[清理] ❌ {cand.bvid} ({code or '?'}) "
                f"{(cand.channel_title or '?')} | {(cand.title or '')[:40]}"
                f"  ← {', '.join(cand.sources)}"
            )
        else:
            report.unknown.append((cand, str(code) if code is not None else "无错误码"))
        if index % progress_every == 0:
            print(
                f"[清理] ... {index}/{len(candidates)}"
                f" 存活 {report.alive} 已消失 {report.cleaned} 未知 {len(report.unknown)}",
                flush=True,
            )
        if index < len(candidates):
            time.sleep(PROBE_INTERVAL_SECONDS)

    if dry_run:
        print(f"[清理] 试运行：不会修改任何文件（{report.cleaned} 条待清理）")
        return report

    summary = apply_purge(
        report.gone, processed_path, collections_path, subtitles_path
    )
    print(
        f"[清理] 完成：标记已删除 {summary['processed_marked']} 条，"
        f"合集队列移除 {summary['collections_removed']} 条，"
        f"字幕队列移除 {summary['subtitles_removed']} 条，"
        f"缓存 {summary['cache_keys_removed']} 项"
    )
    if summary["files_removed"]:
        print(f"[清理] 已删除字幕文件: {', '.join(summary['files_removed'])}")
    if report.skipped_fresh:
        print(f"[清理] 跳过 {len(report.skipped_fresh)} 条上传未满 24 小时的记录")
    if report.unknown:
        print(f"[清理] 跳过 {len(report.unknown)} 条状态未知（未确认消失，保持原样）")
    return report
