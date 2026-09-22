"""
Bilibili subtitle API: CID lookup and soft-subtitle submission.

Uses direct HTTP requests (``httpx``) for subtitle-specific endpoints
that are not covered by ``bilibili-api-python``.
"""

import json
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
import httpx
from yt2bili import atomic_io, config
from yt2bili.bilibili.api import (
    GONE_CODES,
    VIDEO_INFO_URL,
    check_response,
    extract_code,
    is_gone_code,
    is_gone_error,
)
from yt2bili import profile as profile_mod
from yt2bili.subtitles.paths import (
    is_translated_name,
    translated_srt_path,
    video_id_from_filename,
)
from yt2bili.timestamps import parse_iso, utc_now

# ── Constants ────────────────────────────────────────────────────────

_BILIBILI_SUBTITLE_DRAFT_URL = "https://api.bilibili.com/x/v2/dm/subtitle/draft/save"
_BILIBILI_SUBTITLE_DEL_URL = "https://api.bilibili.com/x/v2/dm/subtitle/del"

_DEFAULT_TIMEOUT = 15.0
_UPLOAD_TIMEOUT = 30.0

# 稿件消失后连续确认多少次才从字幕队列移除。刚上传的视频在审核期间同样会
# 返回 62002，所以需要跨轮次确认，而不是一次判定就放弃。
_GONE_GIVE_UP_ATTEMPTS = 3
# 队列条目至少存活这么久（小时）才允许走"消失"判定，保护刚上传还在审核的视频
_GONE_MIN_AGE_HOURS = 2.0

# 源字幕当时拿不到（defer_kind 条目）后的重试策略：按**次数**放弃，不按墙钟 ——
# WORK_HOURS_ONLY 下周五 19:50 入队的条目要到周一早上才有第一次机会，纯小时窗口会让它
# 一次都没试就过期。_DEFER_MAX_AGE_HOURS 只作粗兜底（一周），防止条目无限期占位。
_DEFER_MAX_AGE_HOURS = 168.0
# 单次 sweep 最多重建几条 defer 条目：每次重建要重新下字幕+重译，
# 不限制的话队列一长就会把监控周期拖住。
_MAX_DEFER_REGEN_PER_RUN = 2

# pending_subtitles.json 现在有三个写入者：翻译 worker 线程、pipeline 线程（新入队）
# 和 sweep 自身。同一进程内读-改-写必须串行化（跨进程仍靠 atomic_io 的原子替换）。
_PENDING_LOCK = threading.Lock()


# ── Profile helpers ──────────────────────────────────────────────────

def _active_credentials() -> tuple[str, str, str]:
    """
    ``(sessdata, bili_jct, buvid3)`` for the active profile.

    In legacy .env mode returns the module-level config values. In profile
    mode returns the profile's own credentials — never silently falls back to
    .env, otherwise subtitles would be checked/uploaded on the wrong account.
    """
    name = profile_mod.get_active_profile_name()
    if profile_mod.is_profile_state_active():
        prof = profile_mod.resolve_profile(name)
        if prof is not None and prof.bilibili.sessdata and prof.bilibili.bili_jct:
            return (
                prof.bilibili.sessdata,
                prof.bilibili.bili_jct,
                prof.bilibili.buvid3 or "",
            )
        raise RuntimeError(
            f"账号 '{name}' 未配置 B站 登录凭据（sessdata/bili_jct），无法提交字幕。\n"
            f"运行: python main.py --login --profile {name}"
        )
    return config.BILI_SESSDATA, config.BILI_BILI_JCT, config.BILI_BUVID3


def _active_profile_channel_titles() -> set[str] | None:
    """Set of channel titles for the active profile; None in legacy mode.

    与 monitor 用同一个 ``profile.channel_titles()``（小写去空白）：两处各自折叠
    大小写时曾经不一致，导致 monitor 认得的频道被字幕补偿扫描静默跳过。
    """
    if not profile_mod.is_profile_state_active():
        return None
    prof = profile_mod.resolve_profile(profile_mod.get_active_profile_name())
    if prof is None:
        return None
    return profile_mod.channel_titles(prof)


def _channel_in_scope(entry: dict, channel_titles: set[str] | None) -> bool:
    """该条记录是否属于当前账号的频道（legacy 模式不做限制）。

    两侧都折叠大小写：profiles.json 里手写的频道名与上传日志里的原标题不一定同
    大小写，精确比对会把本账号的条目静默漏掉。
    """
    if channel_titles is None:
        return True
    title = str(entry.get("channel_title") or "").strip().lower()
    if not title:
        return False
    return any(title == str(t or "").strip().lower() for t in channel_titles)


# ── Helpers ──────────────────────────────────────────────────────────

def _build_client(timeout: float = _DEFAULT_TIMEOUT) -> httpx.Client:
    """Build an httpx client with Bilibili cookie auth."""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Referer": "https://member.bilibili.com",
        "Origin": "https://member.bilibili.com",
    }
    cookies = {}
    sessdata, _bili_jct, buvid3 = _active_credentials()
    if sessdata:
        cookies["SESSDATA"] = sessdata
    if buvid3:
        cookies["buvid3"] = buvid3
    cookies["opus-goback"] = "1"

    return httpx.Client(
        headers=headers,
        cookies=cookies,
        timeout=timeout,
    )


# ── Public API ────────────────────────────────────────────────────────

def get_video_pages(bvid: str = "", aid: int = 0) -> list[dict]:
    """
    Query Bilibili video info to get pages (each containing a ``cid``).

    Calls ``GET https://api.bilibili.com/x/web-interface/view``.

    Args:
        bvid: Bilibili BV ID (e.g. ``"BV1xxxx"``).
        aid: Bilibili AV ID (used as fallback if no bvid provided).

    Returns:
        List of page dicts, each containing at least ``"cid"`` and ``"part"``.

    Raises:
        RuntimeError: If the API returns an error or the request fails.
    """
    params: dict[str, str | int] = {}
    if bvid:
        params["bvid"] = bvid
    elif aid:
        params["aid"] = aid
    else:
        raise ValueError("Either bvid or aid must be provided")

    with _build_client() as client:
        try:
            resp = client.get(VIDEO_INFO_URL, params=params)
            data = check_response(resp, "get_video_pages")
        except httpx.RequestError as e:
            raise RuntimeError(f"B站视频信息查询网络错误: {e}")

    pages = data.get("data", {}).get("pages", [])
    if not isinstance(pages, list):
        raise RuntimeError(f"B站返回的 pages 字段格式异常: {type(pages)}")
    return pages


def wait_for_cid(
    bvid: str = "",
    aid: int = 0,
    timeout: int = 300,
    interval: int = 10,
) -> int:
    """
    Poll Bilibili until a ``cid`` is available for the video's first page.

    After upload, Bilibili processes the video asynchronously — the ``cid``
    may not be immediately queryable.  This function polls until it appears
    or the timeout elapses.

    Args:
        bvid: Bilibili BV ID.
        aid: Bilibili AV ID.
        timeout: Maximum total wait time in seconds.
        interval: Poll interval in seconds.

    Returns:
        CID (int) of the first page.

    Raises:
        TimeoutError: If the CID is not available within ``timeout`` seconds.
        RuntimeError: If the API consistently returns errors.
    """
    start = time.monotonic()
    last_error = None

    while True:
        elapsed = time.monotonic() - start
        if elapsed >= timeout:
            raise TimeoutError(
                f"等待 B站 CID 超时（{timeout}s 内未获取到）"
                + (f"，最后一次错误: {last_error}" if last_error else "")
            )

        try:
            pages = get_video_pages(bvid=bvid, aid=aid)
            if pages and pages[0].get("cid", 0) > 0:
                cid = int(pages[0]["cid"])
                print(f"[字幕] 获取到 cid={cid}（等待 {elapsed:.0f}s）")
                return cid
        except Exception as e:
            last_error = e
            # Continue polling on transient errors

        print(".", end="", flush=True)
        time.sleep(interval)


def _probe_video(bvid: str, aid: int) -> tuple[int, bool]:
    """
    One-shot CID lookup.

    Returns ``(cid, gone)``: ``cid > 0`` when the first page is already
    queryable, ``gone=True`` when B站 reports the 稿件 as vanished. Any other
    failure returns ``(0, False)`` so the caller falls back to polling.
    """
    try:
        pages = get_video_pages(bvid=bvid, aid=aid)
    except Exception as e:
        return 0, is_gone_error(e)
    if pages and int(pages[0].get("cid", 0) or 0) > 0:
        return int(pages[0]["cid"]), False
    return 0, False


def _dedup_subtitles(aid: int, cid: int, lan: str) -> int:
    """
    Remove existing manually-uploaded subtitles for the same language.

    Each call to draft/save creates a *new* subtitle track even when one
    already exists for the same language.  To avoid clutter, we delete any
    existing type-0 (manual) subtitle for ``lan`` before uploading a fresh one.

    Args:
        aid: Video AV number.
        cid: Video page ``oid``.
        lan: Language code (e.g. ``"zh"``).

    Returns:
        Number of deleted subtitle tracks.
    """
    try:
        pages = get_video_pages(aid=aid)
    except Exception:
        return 0

    if not pages:
        return 0

    # Get subtitle list (use view API with bvid from first page doesn't
    # directly give us subtitles — use the generic view endpoint).
    try:
        import httpx
        with _build_client(timeout=_DEFAULT_TIMEOUT) as client:
            resp = client.get(
                VIDEO_INFO_URL,
                params={"aid": aid},
            )
            if resp.status_code != 200:
                return 0
            data = resp.json()
            subtitle_list = (
                data.get("data", {}).get("subtitle", {}).get("list", [])
            )
    except Exception:
        return 0

    deleted = 0
    for sub in subtitle_list:
        if sub.get("lan") == lan and sub.get("type") == 0:
            sub_id = str(sub.get("id", ""))
            if not sub_id:
                continue
            try:
                with _build_client(timeout=_DEFAULT_TIMEOUT) as c2:
                    r = c2.post(
                        _BILIBILI_SUBTITLE_DEL_URL,
                        data={
                            "subtitle_id": sub_id,
                            "oid": str(cid),
                            "csrf": _active_credentials()[1],
                        },
                    )
                    if r.status_code == 200:
                        rd = r.json()
                        if rd.get("code") == 0:
                            print(f"[字幕] 已删除旧字幕 id={sub_id}")
                            deleted += 1
            except Exception:
                pass

    return deleted


def submit_subtitle(
    bvid: str,
    cid: int,
    subtitle_json: dict,
    lan: str = "zh",
    aid: int = 0,
) -> dict:
    """
    Submit soft subtitles to Bilibili for a specific video page.

    Uses the Bilibili CC subtitle draft/save API.

    Args:
        bvid: Bilibili BV ID (e.g. ``"BV1xxxx"``).
        cid: Video page cid.
        subtitle_json: Dict in Bilibili subtitle JSON format
            (see :func:`yt2bili.subtitles.bilibili_format.cues_to_bilibili_json`).
        lan: Language code (default ``"zh-CN"`` for Chinese).
        aid: Video aid (AV number), used only for dedup.

    Returns:
        JSON response dict from the Bilibili API.

    Raises:
        RuntimeError: If the API returns an error or the request fails.
    """
    sessdata, bili_jct, _buvid3 = _active_credentials()
    if not sessdata:
        raise RuntimeError("BILI_SESSDATA 未设置，无法提交字幕")
    if not bili_jct:
        raise RuntimeError("BILI_BILI_JCT 未设置，无法提交字幕")

    # Dedup: remove existing same-language subtitle before creating a new one.
    # Each draft/save creates a new track; we want exactly one per language.
    if aid:
        _dedup_subtitles(aid, cid, lan)

    # Serialize the subtitle body as JSON
    data_str = json.dumps(subtitle_json, ensure_ascii=False)

    form_data = {
        "type": 1,                    # subtitle type: 1=manual upload
        "oid": cid,                   # cid is sent as "oid", not "cid"
        "lan": lan,                   # language code, e.g. "zh"
        "data": data_str,             # URL-encoded JSON body
        "submit": "true",
        "sign": "false",
        "bvid": bvid,
        "csrf": bili_jct,
        "csrf_token": bili_jct,
    }

    # Debug: log the request (truncate data for readability)
    debug_form = {k: (str(v)[:80] + "...") if k == "data" and len(str(v)) > 80 else v for k, v in form_data.items()}
    print(f"[字幕] 请求: POST {_BILIBILI_SUBTITLE_DRAFT_URL}")
    print(f"[字幕] 参数: {json.dumps(debug_form, ensure_ascii=False, default=str)}")

    with _build_client(timeout=_UPLOAD_TIMEOUT) as client:
        try:
            resp = client.post(_BILIBILI_SUBTITLE_DRAFT_URL, data=form_data)
            # Log raw response (only when non-JSON Content-Type)
            ct = resp.headers.get("content-type", "")
            if "json" not in ct:
                print(f"[字幕] [DEBUG] HTTP {resp.status_code}: {resp.text[:300]}")
            data = check_response(resp, "submit_subtitle")
        except httpx.RequestError as e:
            raise RuntimeError(f"B站字幕上传网络错误: {e}")

    code = data.get("code", -1)
    if code == 0:
        print(f"[字幕] [OK] 字幕提交成功")
    else:
        print(
            f"[字幕] [WARN] B站返回 code={code}: {data.get('message', '')}\n"
            f"[字幕] 完整响应: {json.dumps(data, ensure_ascii=False)}"
        )

    return data


def _cleanup_subtitle_files(translated_path: str) -> None:
    """
    Delete subtitle files after successful upload to Bilibili.

    Removes all ``.srt`` files with the same video ID prefix.
    Controlled by ``config.CLEANUP_AFTER_UPLOAD``.
    """
    if not config.CLEANUP_AFTER_UPLOAD:
        return

    translated = Path(translated_path)
    # Derive video_id by stripping the target lang suffix: {video_id}.{lang}.srt
    # e.g. "hPXnQ-hO6S8.zh-CN.srt" → video_id = "hPXnQ-hO6S8"
    stem = translated.name  # "hPXnQ-hO6S8.zh-CN.srt"
    video_id = stem.split(".")[0]  # everything before the first dot
    subtitle_dir = translated.parent

    deleted = []
    for f in subtitle_dir.glob(f"{video_id}.*.srt"):
        try:
            f.unlink()
            deleted.append(str(f.name))
        except OSError as e:
            print(f"[字幕] [WARN] 无法删除字幕文件 {f.name}: {e}")

    if deleted:
        print(f"[字幕] 已清理: {', '.join(deleted)}")


# ── Deferred subtitle upload ────────────────────────────────────────────

def pending_subtitles_path() -> Path:
    """
    Path of the pending-subtitle queue for the active profile.

    Named profiles keep their own queue under ``state/{profile}/`` so that
    monitor cycles for one account never check/upload another account's
    subtitles. Legacy .env mode keeps the shared ``state/pending_subtitles.json``.
    """
    return profile_mod.state_file_path("pending_subtitles.json")


def _entry_age_hours(entry: dict) -> float:
    """Hours since the queue entry was added; ``inf`` when the stamp is unusable."""
    added = parse_iso(entry.get("added_at", ""))
    if added is None:
        return float("inf")
    return (datetime.now(timezone.utc) - added).total_seconds() / 3600.0


def _defer_throttled(entry: dict) -> bool:
    """延迟条目距上次尝试不足 ``SUBTITLE_DEFER_RETRY_MINUTES`` 时为 True。

    第一次机会不节流（``defer_attempts == 0``）：监控一轮一小时，本来就够慢；
    节流是为了挡住 ``--subtitle-only`` 那种 10 分钟一轮的轮询。
    """
    if int(entry.get("defer_attempts", 0) or 0) <= 0:
        return False
    last = parse_iso(entry.get("last_defer_at", ""))
    if last is None:
        return False
    minutes = (datetime.now(timezone.utc) - last).total_seconds() / 60.0
    return minutes < config.SUBTITLE_DEFER_RETRY_MINUTES


def _load_upload_log() -> list[dict]:
    """读取全局上传日志（video_id → bvid/aid/channel_title 映射的来源）。

    这个文件在本模块有四个读取点，以前各自内联了一遍「读 + isinstance 检查 +
    静默吞错」；缺失/损坏一律按空列表处理，调用方各自决定要不要提前返回。
    """
    return atomic_io.read_json(
        profile_mod.shared_state_path("upload_log.json"), [], expect=list
    )


def _read_pending_entries(path: Path) -> list[dict]:
    """Read the pending queue, tolerating missing/corrupt/BOM files.

    损坏时先把文件备份留档再当空队列处理：队列里是等着上传的字幕，静默丢弃
    会让操作者事后完全看不到线索。
    """
    return atomic_io.read_json(
        path, [], expect=list, backup_corrupt=True, label="[字幕]"
    )


def _write_pending_entries(path: Path, entries: list[dict]) -> None:
    """Persist the pending queue atomically (Windows-safe: file may be read by peers)."""
    atomic_io.write_json(path, entries)


def save_pending_subtitle(bvid: str, aid: int, translated_path: str) -> None:
    """Record a subtitle that needs deferred upload (Bilibili CID not ready yet)."""
    path = pending_subtitles_path()
    with _PENDING_LOCK:
        entries = _read_pending_entries(path)
        existing = {e.get("bvid", ""): i for i, e in enumerate(entries)}
        entry = {
            "bvid": bvid,
            "aid": aid,
            "translated_path": translated_path,
            "added_at": utc_now(),
        }
        if bvid in existing:
            entries[existing[bvid]] = entry
        else:
            entries.append(entry)
        _write_pending_entries(path, entries)


def save_deferred_subtitle(
    bvid: str, aid: int, translated_path: str, kind: str
) -> None:
    """
    源字幕当时拿不到（``SubtitleUnavailable``）时把视频挂进延迟队列。

    与 :func:`save_pending_subtitle` 的区别在于**合并而非覆盖**：同 bvid 的条目
    若已带着同一个 ``defer_kind`` 在队列里，保留 ``added_at`` 与各类计数 ——
    否则每次重新处理视频都会把 ``added_at`` 刷新，重试预算永远用不完。
    """
    if not bvid or not translated_path:
        return
    path = pending_subtitles_path()
    with _PENDING_LOCK:
        entries = _read_pending_entries(path)
        existing = {e.get("bvid", ""): i for i, e in enumerate(entries)}
        entry = {
            "bvid": bvid,
            "aid": aid,
            "translated_path": translated_path,
            "added_at": utc_now(),
            "defer_kind": kind,
            "defer_attempts": 0,
        }
        idx = existing.get(bvid)

        if idx is None:
            entries.append(entry)
        elif entries[idx].get("defer_kind") == kind:
            # 同因重入队：保留已消耗的重试预算和上次尝试时间
            merged = dict(entries[idx])
            merged.update(entry)
            merged["added_at"] = entries[idx].get("added_at", entry["added_at"])
            merged["defer_attempts"] = int(entries[idx].get("defer_attempts", 0) or 0)
            if entries[idx].get("last_defer_at"):
                merged["last_defer_at"] = entries[idx]["last_defer_at"]
            entries[idx] = merged
        else:
            entries[idx] = entry
        _write_pending_entries(path, entries)


def _has_zh_subtitle(data: dict) -> bool:
    """B站 返回的稿件信息里是否已有中文字幕轨。"""
    subtitle_list = data.get("data", {}).get("subtitle", {}).get("list", [])
    return any(s.get("lan", "").startswith("zh") for s in subtitle_list)


def _iter_subtitle_status(candidates: list[dict]):
    """逐个查询候选稿件在 B站 的中文字幕状态。

    产出 ``(entry, kind, data)``，``kind`` 取值：

    * ``missing``      —— 查得到且没有中文字幕轨（需要处理）
    * ``has_zh``       —— B站 已有中文字幕
    * ``not_visible``  —— code != 0（稿件已删除 / 不可见）
    * ``http_error``   —— HTTP 状态不是 200
    * ``request_error``—— 请求本身抛异常

    两种调用方（扫描残留字幕文件 / 重新入队）对失败的处理不同：一个宁可重试，
    另一个直接跳过，所以这里只负责分类，不做取舍。每次请求之间固定 sleep，
    避免被 B站 限流。
    """
    client = _build_client(timeout=_DEFAULT_TIMEOUT)
    try:
        for i, entry in enumerate(candidates, 1):
            try:
                resp = client.get(VIDEO_INFO_URL, params={"bvid": entry["bvid"]})
                time.sleep(0.3)  # avoid rate limiting
                if resp.status_code != 200:
                    yield entry, "http_error", None
                else:
                    data = resp.json()
                    if data.get("code") != 0:
                        yield entry, "not_visible", None
                    elif _has_zh_subtitle(data):
                        yield entry, "has_zh", data
                    else:
                        yield entry, "missing", data
            except Exception:
                yield entry, "request_error", None
            if i % 10 == 0:
                print(f"[字幕]   已检查 {i}/{len(candidates)}...")
    finally:
        client.close()


def _recover_orphaned_subtitles(
    existing_bvids: set[str],
    channel_titles: set[str] | None = None,
) -> list[dict]:
    """
    Scan subtitle directory for ``.zh-CN.srt`` files not in the pending queue.

    Cross-references with ``upload_log.json`` to find BVID, queries Bilibili
    to confirm no zh-CN subtitles exist yet, and returns entries worth retrying.
    Also pre-fetches CID from the API response to avoid duplicate queries later.

    Args:
        existing_bvids: BVIDs already in the pending queue (skip those).
        channel_titles: When given (profile mode), only consider subtitle files
            whose video's channel is in this set — one account must never
            check/upload another account's subtitles. ``None`` (legacy .env
            mode) restores the previous global scan.
    """
    subtitle_dir = Path(config.SUBTITLE_DIR)
    if not subtitle_dir.exists():
        return []

    # Read upload_log mapping
    upload_log = _load_upload_log()
    if not upload_log:
        return []

    vid_to_entry: dict[str, dict] = {}
    for item in upload_log:
        vid = item.get("video_id")
        bv = item.get("bvid")
        aid = item.get("aid", 0)
        if vid and bv:
            vid_to_entry[vid] = {
                "bvid": bv,
                "aid": aid,
                "channel_title": item.get("channel_title", ""),
            }

    # Collect orphaned .zh-CN.srt files (skip ones already in pending)
    orphaned: list[dict] = []
    scoped_skipped = 0
    for srt in sorted(subtitle_dir.glob("*.zh-CN.srt")):
        video_id = video_id_from_filename(srt.name)
        info = vid_to_entry.get(video_id)
        if not info:
            continue
        bvid = info["bvid"]
        if bvid in existing_bvids:
            continue
        # Profile mode: never check/upload another account's subtitle files
        if not _channel_in_scope(info, channel_titles):
            scoped_skipped += 1
            continue

        orphaned.append({
            "bvid": bvid,
            "aid": info["aid"],
            "translated_path": str(srt),
        })

    if scoped_skipped:
        print(f"[字幕] 跳过 {scoped_skipped} 个其他账号的字幕文件")

    if not orphaned:
        return []

    print(f"[字幕] 发现 {len(orphaned)} 个被丢弃的字幕文件，检查B站状态...")
    recoverable: list[dict] = []
    skipped_has_sub = 0

    for entry, kind, data in _iter_subtitle_status(orphaned):
        if kind == "has_zh":
            skipped_has_sub += 1
            continue
        if kind == "not_visible":
            continue  # 稿件已删除 / 不可见：重试也没有意义
        if kind == "missing":
            # Pre-extract CID so upload_pending_subtitles can skip wait_for_cid
            pages = (data or {}).get("data", {}).get("pages", [])
            if pages and pages[0].get("cid", 0) > 0:
                entry["cid"] = int(pages[0]["cid"])
        # http_error / request_error 也照收：手头就是一批已经翻好的字幕文件，
        # 网络抖一下不该把它们丢掉（没有 CID 而已）
        recoverable.append(entry)

    if recoverable:
        with_cid = sum(1 for e in recoverable if e.get("cid"))
        print(f"[字幕] {len(recoverable)} 个可重试（{with_cid} 已有 CID），"
              f"{skipped_has_sub} 个B站已有，已加入上传队列")
    elif skipped_has_sub > 0:
        print(f"[字幕] 所有丢弃的字幕文件在B站已存在（{skipped_has_sub} 个），跳过")

    return recoverable


def _migrate_legacy_pending_queue() -> None:
    """
    One-time split of the shared ``state/pending_subtitles.json`` into
    per-profile queues (``state/{profile}/pending_subtitles.json``).

    Entries are attributed to a profile via ``upload_log.json`` (video_id →
    channel_title → profile channel list). Entries that cannot be attributed
    stay in the legacy file, which is only processed in legacy .env mode —
    they are never silently uploaded under the wrong account.

    Idempotent: once split, the legacy file is removed (or holds only
    unattributed entries), so later runs are no-ops.
    """
    legacy = Path(config.PROJECT_ROOT) / "state" / "pending_subtitles.json"
    if not legacy.exists():
        return

    entries = atomic_io.read_json(legacy, None, expect=list)
    if entries is None:
        return  # leave an unreadable legacy file alone

    if not entries:
        try:
            legacy.unlink()
        except OSError:
            pass
        return


    # channel_title -> profile name (first profile wins on title collision)
    channel_to_profile: dict[str, str] = {}
    for pname, prof in profile_mod.load_profiles().items():
        for c in prof.youtube.channels:
            if c.channel_title:
                channel_to_profile.setdefault(c.channel_title, pname)

    # video_id -> channel_title from the global upload log
    vid_to_channel: dict[str, str] = {}
    for item in _load_upload_log():
        vid = item.get("video_id")
        chan = item.get("channel_title", "")
        if vid and chan:
            vid_to_channel[vid] = chan

    per_profile: dict[str, dict[str, dict]] = {}
    unattributed: list[dict] = []
    for e in entries:
        if not isinstance(e, dict):
            unattributed.append(e)
            continue
        video_id = video_id_from_filename(e.get("translated_path", ""))
        pname = channel_to_profile.get(vid_to_channel.get(video_id, ""), "")
        if pname:
            per_profile.setdefault(pname, {})[e.get("bvid", "")] = e
        else:
            unattributed.append(e)

    # Merge into each per-profile queue (one entry per bvid; newer added_at wins)
    for pname, by_bvid in per_profile.items():
        queue = Path(config.PROJECT_ROOT) / "state" / pname / "pending_subtitles.json"
        merged: dict[str, dict] = {
            e.get("bvid", ""): e
            for e in atomic_io.read_json(queue, [], expect=list)
            if isinstance(e, dict)
        }
        for bvid, e in by_bvid.items():
            old = merged.get(bvid)
            if old and old.get("added_at", "") >= e.get("added_at", ""):
                continue
            merged[bvid] = e
        atomic_io.write_json(queue, list(merged.values()))

    if unattributed:
        atomic_io.write_json(legacy, unattributed)
    else:
        atomic_io.remove_best_effort(legacy)

    if per_profile:
        summary = "，".join(
            f"{p}: {len(v)} 条" for p, v in sorted(per_profile.items())
        )
        print(
            f"[字幕] 已将旧的公共字幕队列拆分为账号独立队列: {summary}"
            + (f"，{len(unattributed)} 条未归属保留" if unattributed else "")
        )


def _lookup_upload_log_url(video_id: str) -> str:
    """Look up the YouTube URL for a video in the global upload log."""
    for item in _load_upload_log():
        if item.get("video_id") == video_id and item.get("url"):
            return str(item["url"])
    return ""


def _find_source_subtitle(translated_path: str) -> str | None:
    """
    Find a kept source subtitle file next to the missing translated file.

    Looks for ``{video_id}.{lang}.srt`` where ``lang`` is not the target
    language (e.g. ``.en.srt``). Returns ``None`` when nothing is kept.
    """
    translated = Path(translated_path)
    video_id = video_id_from_filename(translated.name)
    for f in sorted(translated.parent.glob(f"{video_id}.*.srt")):
        if f.name != translated.name and not is_translated_name(f.name):
            return str(f)
    return None


def _regenerate_missing_subtitle(entry: dict) -> str | None:
    """
    Regenerate a missing translated subtitle file for a pending entry.

    If a source subtitle (e.g. ``{video_id}.en.srt``) is still on disk it is
    reused and only re-translated; otherwise the subtitle is re-downloaded
    from YouTube first.

    Returns:
        译文字幕文件路径；重下成功但解析为空、或翻译失败时返回 ``None``
        （条目留在队列里，下轮再试）。

    Raises:
        SubtitleUnavailable: 源字幕拿不到（仍未生成 / 网络失败 / 语言不匹配）。
            调用方需要 ``kind`` 来区分"再等等"与"别试了"，所以这里**故意不吞**。
    """
    translated_path = entry.get("translated_path", "")
    video_id = video_id_from_filename(translated_path)

    from yt2bili.subtitles.parser import parse_subtitle
    from yt2bili.subtitles.translator import translate_cues
    from yt2bili.subtitles.writer import write_srt

    # Reuse a kept source subtitle when possible — only re-translate
    source_path = _find_source_subtitle(translated_path)
    cues = parse_subtitle(source_path) if source_path else []
    if not cues:
        # No kept source (or it is unreadable) — re-download from YouTube
        url = _lookup_upload_log_url(video_id) or f"https://www.youtube.com/watch?v={video_id}"
        print(f"[字幕] 源字幕缺失，重新下载: {video_id}")
        from yt2bili.subtitles.downloader import download_subtitles
        # 拿不到源字幕时抛 SubtitleUnavailable —— 故意不吞，调用方要靠 kind
        # 区分"再等等"（可重试）与"别试了"（永久）。
        source_path = download_subtitles(url, video_id)
        cues = parse_subtitle(source_path)
        if not cues:
            print(f"[字幕] [WARN] 重新下载的字幕解析为空: {Path(source_path).name}")
            return None
    else:
        print(f"[字幕] 发现保留的源字幕 {Path(source_path).name}，直接重新翻译")

    try:
        translated = translate_cues(cues, batch_size=config.SUBTITLE_TRANSLATE_BATCH_SIZE)
    except Exception as e:
        print(f"[字幕] [WARN] 重新翻译失败: {e}")
        return None

    write_srt(translated, translated_path)
    return translated_path


def _try_regenerate(entry: dict) -> tuple[str | None, "SubtitleUnavailable | None"]:
    """
    Run :func:`_regenerate_missing_subtitle` and classify the failure.

    Returns ``(translated_path, source_error)`` — mutually exclusive: a source
    problem (YouTube has no usable caption track) comes back as the second
    element so the caller can apply the deferred-retry budget instead of the
    generic three-strike counter. Other exceptions (e.g. a translation API
    error) are logged and reported as a plain failure, which also keeps them
    out of the outer ``except Exception`` that would retry them forever.
    """
    from yt2bili.subtitles.downloader import SubtitleUnavailable

    try:
        return _regenerate_missing_subtitle(entry), None
    except SubtitleUnavailable as e:
        return None, e
    except Exception as e:  # 翻译/写盘等：按普通失败计数，别落到外层无限重试
        print(f"[字幕] [WARN] 重新生成出错: {e}", flush=True)
        return None, None


def upload_pending_subtitles() -> int:
    """Try to upload pending subtitles. Returns count of successfully uploaded."""
    _migrate_legacy_pending_queue()
    path = pending_subtitles_path()

    entries: list[dict] = _read_pending_entries(path)

    from yt2bili.subtitles.parser import parse_subtitle
    from yt2bili.subtitles.bilibili_format import (
        DURATION_MARGIN_S,
        cues_to_bilibili_json,
    )

    # Recover orphaned subtitles (previously marked as permanent failures),
    # scoped to this account's channels in profile mode
    existing_bvids = {e.get("bvid", "") for e in entries}
    recovered = _recover_orphaned_subtitles(
        existing_bvids, _active_profile_channel_titles()
    )
    if recovered:
        entries.extend(recovered)
        # Persist merged list
        with _PENDING_LOCK:
            _write_pending_entries(path, entries)

    if not entries:
        return 0

    print(f"[字幕] 检查 {len(entries)} 条待上传字幕...")
    remaining: list[dict] = []
    uploaded = 0
    defer_regen_used = 0   # 本轮已经为几条延迟条目重建过源字幕

    for entry in entries:
        bvid = entry.get("bvid", "")
        aid = entry.get("aid", 0)
        translated_path = entry.get("translated_path", "")

        if not bvid or not translated_path:
            continue

        # 延迟条目（源字幕当时没拿到）：节流 + 单轮上限。检查放在 CID 探测之前 ——
        # 否则每次跳过前都要先付一次 B站 请求（甚至 30 秒 wait_for_cid）。
        defer_kind = str(entry.get("defer_kind", "") or "")
        if defer_kind:
            if _defer_throttled(entry):
                remaining.append(entry)
                continue
            if defer_regen_used >= _MAX_DEFER_REGEN_PER_RUN:
                remaining.append(entry)
                continue

        # Use CID from recovery scan if available; otherwise poll
        cid = entry.get("cid", 0)
        if cid and cid > 0:
            print(f"[字幕] 使用缓存 CID={cid} (BV={bvid})")
        else:
            # 先做一次廉价探测：稿件已消失时立刻放弃，不再走 wait_for_cid
            # 轮询 30 秒（每个已删视频每轮白等 30s 是旧的空转来源）。
            probed_cid, gone = _probe_video(bvid, aid)
            if gone and _entry_age_hours(entry) >= _GONE_MIN_AGE_HOURS:
                fails = int(entry.get("gone_failures", 0) or 0) + 1
                entry["gone_failures"] = fails
                if fails >= _GONE_GIVE_UP_ATTEMPTS:
                    print(
                        f"[字幕] [WARN] 稿件已不存在，从队列移除 ({bvid})，"
                        "不再重试",
                        flush=True,
                    )
                    continue  # 放弃：不写回 remaining
                print(
                    f"[字幕] [WARN] 稿件不可见 ({bvid})"
                    f"（第 {fails}/{_GONE_GIVE_UP_ATTEMPTS} 次确认），"
                    "保留在队列中下轮复查",
                    flush=True,
                )
                remaining.append(entry)
                continue
            if probed_cid:
                cid = probed_cid
                print(f"[字幕] 使用探测 CID={cid} (BV={bvid})")
            else:
                try:
                    cid = wait_for_cid(bvid=bvid, aid=aid, timeout=30, interval=5)
                except TimeoutError:
                    remaining.append(entry)
                    continue
                except Exception:
                    remaining.append(entry)
                    continue

        try:
            # Check file still exists before attempting parse.
            # If it is missing (manually deleted, cleaned up, disk issue),
            # regenerate it: reuse a kept source subtitle if present, otherwise
            # re-download from YouTube, then re-translate.
            if not Path(translated_path).exists():
                print(
                    f"[字幕] 翻译字幕文件缺失 ({bvid}): {translated_path}",
                    flush=True,
                )
                if defer_kind:
                    defer_regen_used += 1
                    entry["defer_attempts"] = int(entry.get("defer_attempts", 0) or 0) + 1
                    entry["last_defer_at"] = utc_now()
                regenerated, source_error = _try_regenerate(entry)
                if regenerated:
                    print(
                        f"[字幕] 重新生成完成: {Path(translated_path).name}",
                        flush=True,
                    )
                    entry.pop("regen_failures", None)  # 成功后清零
                elif source_error is not None and defer_kind:
                    # 源字幕那一侧的失败：按延迟预算计，不消耗 regen_failures ——
                    # "还没生成"不是失败，只是还没轮到。
                    if source_error.permanent:
                        print(
                            f"[字幕] [WARN] 源字幕永久不可用（{source_error.kind}），"
                            f"放弃 ({bvid}): {source_error}",
                            flush=True,
                        )
                        continue  # 永久放弃：不写回 remaining
                    attempts = int(entry.get("defer_attempts", 0) or 0)
                    if (attempts >= config.SUBTITLE_DEFER_MAX_ATTEMPTS
                            or _entry_age_hours(entry) >= _DEFER_MAX_AGE_HOURS):
                        print(
                            f"[字幕] [WARN] 源字幕重试 {attempts} 次仍未拿到，"
                            f"放弃 ({bvid}): {source_error}",
                            flush=True,
                        )
                        continue  # 重试预算用完：不写回 remaining
                    print(
                        f"[字幕] [WARN] 源字幕仍不可用（{source_error.kind}，"
                        f"第 {attempts}/{config.SUBTITLE_DEFER_MAX_ATTEMPTS} 次），"
                        "保留在队列中下轮重试",
                        flush=True,
                    )
                    remaining.append(entry)
                    continue
                else:
                    # 视频被设为私有 / YouTube 没有字幕轨道时，重新生成必然失败。
                    # 连续失败超过阈值后永久放弃，避免每轮监控都白试一次。
                    fails = int(entry.get("regen_failures", 0)) + 1
                    entry["regen_failures"] = fails
                    if fails >= config.SUBTITLE_REGEN_MAX_FAILURES:
                        print(
                            f"[字幕] [WARN] 重新生成连续失败 {fails} 次，放弃 ({bvid})"
                            "（源字幕不可用或视频私有），不再重试",
                            flush=True,
                        )
                        continue  # 永久放弃：不写回 remaining
                    print(
                        f"[字幕] [WARN] 重新生成失败 ({bvid})"
                        f"（第 {fails}/{config.SUBTITLE_REGEN_MAX_FAILURES} 次），"
                        "保留在队列中下轮重试",
                        flush=True,
                    )
                    remaining.append(entry)
                    continue

            cues = parse_subtitle(translated_path)
            if not cues:
                remaining.append(entry)
                continue

            # Fetch video duration to validate cue timestamps.
            # Avoids 79014 "字幕时间点超过视频时间长度" rejections.
            video_duration: float = 0.0
            try:
                pages = get_video_pages(bvid=bvid, aid=aid)
                if pages and pages[0].get("duration", 0) > 0:
                    video_duration = float(pages[0]["duration"])
            except Exception:
                pass  # duration is best-effort; proceed without it if unavailable

            subtitle_json = cues_to_bilibili_json(
                cues, video_duration=video_duration or None,
                margin=DURATION_MARGIN_S,
            )
            submit_subtitle(bvid=bvid, cid=cid, subtitle_json=subtitle_json, aid=aid)
            uploaded += 1
            # Cleanup subtitle files after successful upload
            _cleanup_subtitle_files(translated_path)
        except Exception as e:
            err_str = str(e)
            # Permanent Bilibili errors — don't retry
            if any(code in err_str for code in ("79006", "79014", "79019")):
                print(f"[字幕] [WARN] 永久失败，放弃 ({bvid}): {e}")
            else:
                print(f"[字幕] [WARN] 延迟上传失败 ({bvid}): {e}")
                remaining.append(entry)

    with _PENDING_LOCK:
        if remaining:
            _write_pending_entries(path, remaining)
        elif path.exists():
            path.unlink()

    if uploaded:
        print(f"[字幕] 延迟上传完成: {uploaded} 条，剩余 {len(remaining)} 条待处理")
    return uploaded


def requeue_missing_subtitles() -> int:
    """
    One-time recovery: re-queue videos whose Chinese subtitle never made it to
    Bilibili.

    Scans the global ``upload_log.json`` (scoped to the active profile's
    channels), queries Bilibili for each video not already in the pending
    queue, and re-queues those that have no zh subtitle yet. Re-queued entries
    point at a (missing) translated file — the next
    :func:`upload_pending_subtitles` run regenerates it (re-download +
    re-translate) automatically.

    Returns the number of re-queued videos.
    """
    path = pending_subtitles_path()

    existing_bvids = {
        e.get("bvid", "") for e in _read_pending_entries(path) if isinstance(e, dict)
    }

    channel_titles = _active_profile_channel_titles()

    candidates: list[dict] = []
    for item in _load_upload_log():
        bvid = item.get("bvid", "")
        if not bvid or bvid in existing_bvids:
            continue
        if not _channel_in_scope(item, channel_titles):
            continue
        candidates.append(item)

    if not candidates:
        print("[字幕] 没有需要检查的字幕状态视频")
        return 0

    print(f"[字幕] 检查 {len(candidates)} 个视频在 B站 的中文字幕状态...")
    requeued = 0
    has_sub = 0
    skipped = 0

    for item, kind, _data in _iter_subtitle_status(candidates):
        if kind == "has_zh":
            has_sub += 1
            continue
        if kind != "missing":
            # 不可达 / 请求出错：这条记录重试也是白搭，交给窗口外的下次运行
            skipped += 1
            continue

        bvid = item.get("bvid", "")
        video_id = item.get("video_id", "")
        if not video_id:
            skipped += 1
            continue
        # No Chinese subtitle on Bilibili — re-queue it. The translated file is
        # missing by design; upload_pending_subtitles will regenerate it
        # (re-download + re-translate) on the next run.
        save_pending_subtitle(
            bvid=bvid, aid=item.get("aid", 0),
            translated_path=str(translated_srt_path(video_id)),
        )
        requeued += 1

    print(
        f"[字幕] 恢复完成: 重新入队 {requeued} 个，B站已有中文字幕 {has_sub} 个，"
        f"跳过 {skipped} 个（不可达/出错）"
    )
    return requeued
