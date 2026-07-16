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
    if config.BILI_SESSDATA:
        cookies["SESSDATA"] = config.BILI_SESSDATA
    if config.BILI_BUVID3:
        cookies["buvid3"] = config.BILI_BUVID3
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
                            "csrf": config.BILI_BILI_JCT,
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
    if not config.BILI_SESSDATA:
        raise RuntimeError("BILI_SESSDATA 未设置，无法提交字幕")
    if not config.BILI_BILI_JCT:
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
        "csrf": config.BILI_BILI_JCT,
        "csrf_token": config.BILI_BILI_JCT,
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
    return Path(config.PROJECT_ROOT) / "state" / "pending_subtitles.json"


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
) -> list[dict]:
    """
    Scan subtitle directory for ``.zh-CN.srt`` files not in the pending queue.

    Cross-references with ``upload_log.json`` to find BVID, queries Bilibili
    to confirm no zh-CN subtitles exist yet, and returns entries worth retrying.
    Also pre-fetches CID from the API response to avoid duplicate queries later.
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
            vid_to_entry[vid] = {"bvid": bv, "aid": aid}

    # Collect orphaned .zh-CN.srt files (skip ones already in pending)
    orphaned: list[dict] = []
    for srt in sorted(subtitle_dir.glob("*.zh-CN.srt")):
        video_id = srt.name.split(".", 1)[0]
        info = vid_to_entry.get(video_id)
        if not info:
            continue
        bvid = info["bvid"]
        if bvid in existing_bvids:
            continue

        orphaned.append({
            "bvid": bvid,
            "aid": info["aid"],
            "translated_path": str(srt),
        })

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


def upload_pending_subtitles() -> int:
    """Try to upload pending subtitles. Returns count of successfully uploaded."""
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

    # Recover orphaned subtitles (previously marked as permanent failures)
    existing_bvids = {e.get("bvid", "") for e in entries}
    recovered = _recover_orphaned_subtitles(existing_bvids)
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
            # If the file was already cleaned up (e.g. previous partial
            # success), there is no point keeping it in the queue.
            if not Path(translated_path).exists():
                print(
                    f"[字幕] [WARN] 永久失败，文件缺失 ({bvid}): {translated_path}",
                    flush=True,
                )
                continue  # drop from queue — don't re-add to remaining

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
