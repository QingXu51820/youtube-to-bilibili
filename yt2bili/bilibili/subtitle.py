"""
Bilibili subtitle API: CID lookup and soft-subtitle submission.

Uses direct HTTP requests (``httpx``) for subtitle-specific endpoints
that are not covered by ``bilibili-api-python``.
"""

import json
import time
from pathlib import Path
import httpx
from yt2bili import config

# ── Constants ────────────────────────────────────────────────────────

_BILIBILI_VIDEO_INFO_URL = "https://api.bilibili.com/x/web-interface/view"
_BILIBILI_SUBTITLE_DRAFT_URL = "https://api.bilibili.com/x/v2/dm/subtitle/draft/save"
_BILIBILI_SUBTITLE_DEL_URL = "https://api.bilibili.com/x/v2/dm/subtitle/del"

_AUTH_ERROR_CODES = (401, 403)
_DEFAULT_TIMEOUT = 15.0
_UPLOAD_TIMEOUT = 30.0


# ── Profile helpers ──────────────────────────────────────────────────

def _active_profile_name() -> str:
    """Name of the currently active profile ('default' in legacy .env mode)."""
    from yt2bili import profile as profile_mod
    return profile_mod.get_active_profile_name()


def _profile_state_active() -> bool:
    """
    True when the active profile is a real profile account (not .env legacy).

    ``"default"`` means two different modes: without a profiles.json (or without
    a ``"default"`` profile in it) it is legacy .env mode with the shared queue;
    with a ``"default"`` profile in profiles.json it is a real profile account
    with its own queue.
    """
    from yt2bili import profile as profile_mod
    name = _active_profile_name()
    if name != "default":
        return True
    return profile_mod.is_multi_profile() and profile_mod.profile_exists("default")


def _active_credentials() -> tuple[str, str, str]:
    """
    ``(sessdata, bili_jct, buvid3)`` for the active profile.

    In legacy .env mode returns the module-level config values. In profile
    mode returns the profile's own credentials — never silently falls back to
    .env, otherwise subtitles would be checked/uploaded on the wrong account.
    """
    name = _active_profile_name()
    if _profile_state_active():
        from yt2bili import profile as profile_mod
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
    """Set of channel titles for the active profile; None in legacy mode."""
    if not _profile_state_active():
        return None
    from yt2bili import profile as profile_mod
    prof = profile_mod.resolve_profile(_active_profile_name())
    if prof is None:
        return None
    return {c.channel_title for c in prof.youtube.channels if c.channel_title}


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


def _check_response(resp: httpx.Response, label: str = "Bilibili API") -> dict:
    """Check an httpx response for auth errors and JSON validity."""
    if resp.status_code in _AUTH_ERROR_CODES:
        raise RuntimeError(
            f"B站登录凭据已过期（HTTP {resp.status_code}），请重新扫码登录。\n"
            f"运行: python main.py --login"
        )
    try:
        data = resp.json()
    except Exception as e:
        raise RuntimeError(f"{label} 返回非 JSON 响应: {e}")

    code = data.get("code", -1)
    if code != 0:
        msg = data.get("message", str(data))
        # Include detailed error data when available (e.g. subtitle line errors)
        err_data = data.get("data")
        if isinstance(err_data, list) and err_data:
            details = "; ".join(
                f"L{d.get('line', '?')}: {d.get('error_msg', str(d))}"
                for d in err_data[:10]
            )
            if len(err_data) > 10:
                details += f" ...(+{len(err_data) - 10} more)"
            msg = f"{msg} [{details}]"
        raise RuntimeError(f"{label} 返回错误 (code={code}): {msg}")

    return data


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
            resp = client.get(_BILIBILI_VIDEO_INFO_URL, params=params)
            data = _check_response(resp, "get_video_pages")
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
                _BILIBILI_VIDEO_INFO_URL,
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
            data = _check_response(resp, "submit_subtitle")
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

def _pending_subtitles_path() -> Path:
    """
    Path of the pending-subtitle queue for the active profile.

    Named profiles keep their own queue under ``state/{profile}/`` so that
    monitor cycles for one account never check/upload another account's
    subtitles. Legacy .env mode keeps the shared ``state/pending_subtitles.json``.
    """
    root = Path(config.PROJECT_ROOT)
    if not _profile_state_active():
        return root / "state" / "pending_subtitles.json"
    return root / "state" / _active_profile_name() / "pending_subtitles.json"


def save_pending_subtitle(bvid: str, aid: int, translated_path: str) -> None:
    """Record a subtitle that needs deferred upload (Bilibili CID not ready yet)."""
    path = _pending_subtitles_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    entries: list[dict] = []
    if path.exists():
        try:
            entries = json.loads(path.read_text(encoding="utf-8-sig"))
            if not isinstance(entries, list):
                entries = []
        except (json.JSONDecodeError, OSError):
            entries = []

    existing = {e.get("bvid", ""): i for i, e in enumerate(entries)}
    entry = {
        "bvid": bvid,
        "aid": aid,
        "translated_path": translated_path,
        "added_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    if bvid in existing:
        entries[existing[bvid]] = entry
    else:
        entries.append(entry)

    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(entries, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


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
    upload_log_path = Path(config.PROJECT_ROOT) / "state" / "upload_log.json"
    try:
        if not upload_log_path.exists():
            return []
        upload_log = json.loads(upload_log_path.read_text(encoding="utf-8-sig"))
        if not isinstance(upload_log, list):
            return []
    except (json.JSONDecodeError, OSError):
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
        video_id = srt.name.split(".", 1)[0]
        info = vid_to_entry.get(video_id)
        if not info:
            continue
        bvid = info["bvid"]
        if bvid in existing_bvids:
            continue
        # Profile mode: never check/upload another account's subtitle files
        if channel_titles is not None and info.get("channel_title") not in channel_titles:
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
    client = _build_client(timeout=_DEFAULT_TIMEOUT)

    for i, entry in enumerate(orphaned, 1):
        bvid = entry["bvid"]
        try:
            resp = client.get(_BILIBILI_VIDEO_INFO_URL, params={"bvid": bvid})
            time.sleep(0.3)  # avoid rate limiting
            if resp.status_code != 200:
                recoverable.append(entry)
                if i % 10 == 0:
                    print(f"[字幕]   已扫描 {i}/{len(orphaned)}...")
                continue
            data = resp.json()
            if data.get("code") != 0:
                # Some videos may be deleted / not visible
                if i % 10 == 0:
                    print(f"[字幕]   已扫描 {i}/{len(orphaned)}...")
                continue  # skip unreachable videos

            # Check if zh-CN subtitles already exist on Bilibili
            subtitle_list = data.get("data", {}).get("subtitle", {}).get("list", [])
            has_zh = any(s.get("lan", "").startswith("zh") for s in subtitle_list)
            if has_zh:
                skipped_has_sub += 1
                continue  # already uploaded, skip

            # Pre-extract CID so upload_pending_subtitles can skip wait_for_cid
            pages = data.get("data", {}).get("pages", [])
            if pages and pages[0].get("cid", 0) > 0:
                entry["cid"] = int(pages[0]["cid"])

            recoverable.append(entry)
        except Exception:
            recoverable.append(entry)  # err on the side of retrying, but no CID

        if i % 10 == 0:
            print(f"[字幕]   已扫描 {i}/{len(orphaned)}...")

    client.close()

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

    try:
        entries = json.loads(legacy.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, OSError):
        return  # leave an unreadable legacy file alone
    if not isinstance(entries, list):
        return

    if not entries:
        try:
            legacy.unlink()
        except OSError:
            pass
        return

    from yt2bili import profile as profile_mod

    # channel_title -> profile name (first profile wins on title collision)
    channel_to_profile: dict[str, str] = {}
    for pname, prof in profile_mod.load_profiles().items():
        for c in prof.youtube.channels:
            if c.channel_title:
                channel_to_profile.setdefault(c.channel_title, pname)

    # video_id -> channel_title from the global upload log
    upload_log_path = Path(config.PROJECT_ROOT) / "state" / "upload_log.json"
    vid_to_channel: dict[str, str] = {}
    try:
        if upload_log_path.exists():
            upload_log = json.loads(upload_log_path.read_text(encoding="utf-8-sig"))
            if isinstance(upload_log, list):
                for item in upload_log:
                    vid = item.get("video_id")
                    chan = item.get("channel_title", "")
                    if vid and chan:
                        vid_to_channel[vid] = chan
    except (json.JSONDecodeError, OSError):
        pass

    per_profile: dict[str, dict[str, dict]] = {}
    unattributed: list[dict] = []
    for e in entries:
        if not isinstance(e, dict):
            unattributed.append(e)
            continue
        video_id = Path(str(e.get("translated_path", ""))).name.split(".", 1)[0]
        pname = channel_to_profile.get(vid_to_channel.get(video_id, ""), "")
        if pname:
            per_profile.setdefault(pname, {})[e.get("bvid", "")] = e
        else:
            unattributed.append(e)

    # Merge into each per-profile queue (one entry per bvid; newer added_at wins)
    for pname, by_bvid in per_profile.items():
        queue = Path(config.PROJECT_ROOT) / "state" / pname / "pending_subtitles.json"
        merged: dict[str, dict] = {}
        if queue.exists():
            try:
                existing = json.loads(queue.read_text(encoding="utf-8-sig"))
                if isinstance(existing, list):
                    for e in existing:
                        if isinstance(e, dict):
                            merged[e.get("bvid", "")] = e
            except (json.JSONDecodeError, OSError):
                pass
        for bvid, e in by_bvid.items():
            old = merged.get(bvid)
            if old and old.get("added_at", "") >= e.get("added_at", ""):
                continue
            merged[bvid] = e
        queue.parent.mkdir(parents=True, exist_ok=True)
        tmp = queue.with_suffix(queue.suffix + ".tmp")
        tmp.write_text(
            json.dumps(list(merged.values()), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        tmp.replace(queue)

    if unattributed:
        tmp = legacy.with_suffix(legacy.suffix + ".tmp")
        tmp.write_text(
            json.dumps(unattributed, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        tmp.replace(legacy)
    else:
        try:
            legacy.unlink()
        except OSError:
            pass

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
    upload_log_path = Path(config.PROJECT_ROOT) / "state" / "upload_log.json"
    try:
        if upload_log_path.exists():
            upload_log = json.loads(upload_log_path.read_text(encoding="utf-8-sig"))
            if isinstance(upload_log, list):
                for item in upload_log:
                    if item.get("video_id") == video_id and item.get("url"):
                        return str(item["url"])
    except (json.JSONDecodeError, OSError):
        pass
    return ""


def _find_source_subtitle(translated_path: str) -> str | None:
    """
    Find a kept source subtitle file next to the missing translated file.

    Looks for ``{video_id}.{lang}.srt`` where ``lang`` is not the target
    language (e.g. ``.en.srt``). Returns ``None`` when nothing is kept.
    """
    translated = Path(translated_path)
    video_id = translated.name.split(".", 1)[0]
    target_suffix = f".{config.SUBTITLE_TARGET_LANG}.srt"
    for f in sorted(translated.parent.glob(f"{video_id}.*.srt")):
        if f.name != translated.name and not f.name.endswith(target_suffix):
            return str(f)
    return None


def _regenerate_missing_subtitle(entry: dict) -> str | None:
    """
    Regenerate a missing translated subtitle file for a pending entry.

    If a source subtitle (e.g. ``{video_id}.en.srt``) is still on disk it is
    reused and only re-translated; otherwise the subtitle is re-downloaded
    from YouTube first. Returns the translated file path on success, or
    ``None`` (the entry stays in the queue and is retried next cycle).
    """
    translated_path = entry.get("translated_path", "")
    if not translated_path:
        return None
    video_id = Path(translated_path).name.split(".", 1)[0]

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
        source_path = download_subtitles(url, video_id)
        if not source_path:
            print(f"[字幕] [WARN] 重新下载字幕失败 ({video_id})")
            return None
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


def upload_pending_subtitles() -> int:
    """Try to upload pending subtitles. Returns count of successfully uploaded."""
    _migrate_legacy_pending_queue()
    path = _pending_subtitles_path()

    entries: list[dict] = []
    if path.exists():
        try:
            entries = json.loads(path.read_text(encoding="utf-8-sig"))
            if not isinstance(entries, list):
                entries = []
        except (json.JSONDecodeError, OSError):
            pass

    from yt2bili.subtitles.parser import parse_subtitle
    from yt2bili.subtitles.bilibili_format import cues_to_bilibili_json

    # Recover orphaned subtitles (previously marked as permanent failures),
    # scoped to this account's channels in profile mode
    existing_bvids = {e.get("bvid", "") for e in entries}
    recovered = _recover_orphaned_subtitles(
        existing_bvids, _active_profile_channel_titles()
    )
    if recovered:
        entries.extend(recovered)
        # Persist merged list
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(entries, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(path)

    if not entries:
        return 0

    print(f"[字幕] 检查 {len(entries)} 条待上传字幕...")
    remaining: list[dict] = []
    uploaded = 0

    for entry in entries:
        bvid = entry.get("bvid", "")
        aid = entry.get("aid", 0)
        translated_path = entry.get("translated_path", "")

        if not bvid or not translated_path:
            continue

        # Use CID from recovery scan if available; otherwise poll
        cid = entry.get("cid", 0)
        if cid and cid > 0:
            print(f"[字幕] 使用缓存 CID={cid} (BV={bvid})")
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
                if _regenerate_missing_subtitle(entry):
                    print(
                        f"[字幕] 重新生成完成: {Path(translated_path).name}",
                        flush=True,
                    )
                    entry.pop("regen_failures", None)  # 成功后清零
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
                cues, video_duration=video_duration or None, margin=0.3,
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

    if remaining:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(remaining, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(path)
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
    path = _pending_subtitles_path()

    existing_bvids: set[str] = set()
    if path.exists():
        try:
            entries = json.loads(path.read_text(encoding="utf-8-sig"))
            if isinstance(entries, list):
                existing_bvids = {
                    e.get("bvid", "") for e in entries if isinstance(e, dict)
                }
        except (json.JSONDecodeError, OSError):
            pass

    channel_titles = _active_profile_channel_titles()

    upload_log_path = Path(config.PROJECT_ROOT) / "state" / "upload_log.json"
    candidates: list[dict] = []
    try:
        if upload_log_path.exists():
            upload_log = json.loads(upload_log_path.read_text(encoding="utf-8-sig"))
            if isinstance(upload_log, list):
                for item in upload_log:
                    bvid = item.get("bvid", "")
                    if not bvid or bvid in existing_bvids:
                        continue
                    if (channel_titles is not None
                            and item.get("channel_title", "") not in channel_titles):
                        continue
                    candidates.append(item)
    except (json.JSONDecodeError, OSError):
        pass

    if not candidates:
        print("[字幕] 没有需要检查的字幕状态视频")
        return 0

    print(f"[字幕] 检查 {len(candidates)} 个视频在 B站 的中文字幕状态...")
    requeued = 0
    has_sub = 0
    skipped = 0
    client = _build_client(timeout=_DEFAULT_TIMEOUT)
    try:
        for i, item in enumerate(candidates, 1):
            bvid = item.get("bvid", "")
            video_id = item.get("video_id", "")
            try:
                resp = client.get(_BILIBILI_VIDEO_INFO_URL, params={"bvid": bvid})
                time.sleep(0.3)  # avoid rate limiting
                if resp.status_code != 200:
                    skipped += 1
                    continue
                data = resp.json()
                if data.get("code") != 0:
                    skipped += 1  # deleted / not visible — nothing to do
                    continue
                subtitle_list = (
                    data.get("data", {}).get("subtitle", {}).get("list", [])
                )
                if any(s.get("lan", "").startswith("zh") for s in subtitle_list):
                    has_sub += 1
                    continue
                # No Chinese subtitle on Bilibili — re-queue it. The translated
                # file is missing by design; upload_pending_subtitles will
                # regenerate it (re-download + re-translate) on the next run.
                if not video_id:
                    skipped += 1
                    continue
                translated_path = str(
                    Path(config.SUBTITLE_DIR)
                    / f"{video_id}.{config.SUBTITLE_TARGET_LANG}.srt"
                )
                save_pending_subtitle(
                    bvid=bvid, aid=item.get("aid", 0),
                    translated_path=translated_path,
                )
                requeued += 1
            except Exception:
                skipped += 1
            if i % 10 == 0:
                print(f"[字幕]   已检查 {i}/{len(candidates)}...")
    finally:
        client.close()

    print(
        f"[字幕] 恢复完成: 重新入队 {requeued} 个，B站已有中文字幕 {has_sub} 个，"
        f"跳过 {skipped} 个（不可达/出错）"
    )
    return requeued
