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
        "Referer": "https://www.bilibili.com",
    }
    cookies = {}
    if config.BILI_SESSDATA:
        cookies["SESSDATA"] = config.BILI_SESSDATA
    if config.BILI_BUVID3:
        cookies["buvid3"] = config.BILI_BUVID3

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
    aid: int,
    cid: int,
    subtitle_json: dict,
    lan: str = "zh",
    lan_doc: str = "",
) -> dict:
    """
    Submit soft subtitles to Bilibili for a specific video page.

    Uses the Bilibili CC subtitle draft/save API.

    Args:
        aid: Video aid (AV number).
        cid: Video page cid.
        subtitle_json: Dict in Bilibili subtitle JSON format
            (see :func:`yt2bili.subtitles.bilibili_format.cues_to_bilibili_json`).
        lan: Language code (default ``"zh"`` for Chinese).
        lan_doc: Ignored — Bilibili derives this from ``lan``.

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
    _dedup_subtitles(aid, cid, lan)

    # Serialize the subtitle body as JSON
    data_str = json.dumps(subtitle_json, ensure_ascii=False)

    form_data = {
        "type": "1",                  # required: subtitle type
        "oid": str(cid),              # cid is sent as "oid", not "cid"
        "aid": str(aid),
        "lan": lan,                   # e.g. "zh" not "zh-CN"
        "data": data_str,             # URL-encoded JSON body
        "submit": "true",
        "sign": "false",              # must be "false"
        "csrf": config.BILI_BILI_JCT,
    }

    # Debug: log the request (truncate data for readability)
    debug_form = {k: (v[:80] + "...") if k == "data" and len(v) > 80 else v for k, v in form_data.items()}
    print(f"[字幕] 请求: POST {_BILIBILI_SUBTITLE_DRAFT_URL}")
    print(f"[字幕] 参数: {json.dumps(debug_form, ensure_ascii=False)}")

    with _build_client(timeout=_UPLOAD_TIMEOUT) as client:
        try:
            resp = client.post(_BILIBILI_SUBTITLE_DRAFT_URL, data=form_data)
            # Log raw response for debugging
            ct = resp.headers.get("content-type", "")
            if "json" not in ct:
                print(f"[字幕] [WARN] 响应非 JSON (Content-Type: {ct})")
                print(f"[字幕] HTTP {resp.status_code}: {resp.text[:300]}")
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


def upload_pending_subtitles() -> int:
    """Try to upload pending subtitles. Returns count of successfully uploaded."""
    path = _pending_subtitles_path()
    if not path.exists():
        return 0

    try:
        entries = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(entries, list) or not entries:
            return 0
    except (json.JSONDecodeError, OSError):
        return 0

    from yt2bili.subtitles.parser import parse_subtitle
    from yt2bili.subtitles.bilibili_format import cues_to_bilibili_json

    print(f"[字幕] 检查 {len(entries)} 条待上传字幕...")
    remaining: list[dict] = []
    uploaded = 0

    for entry in entries:
        bvid = entry.get("bvid", "")
        aid = entry.get("aid", 0)
        translated_path = entry.get("translated_path", "")

        if not bvid or not translated_path:
            continue

        try:
            cid = wait_for_cid(bvid=bvid, aid=aid, timeout=30, interval=5)
        except TimeoutError:
            remaining.append(entry)
            continue
        except Exception:
            remaining.append(entry)
            continue

        try:
            cues = parse_subtitle(translated_path)
            if not cues:
                remaining.append(entry)
                continue
            subtitle_json = cues_to_bilibili_json(cues)
            submit_subtitle(aid=aid, cid=cid, subtitle_json=subtitle_json, lan="zh")
            uploaded += 1
        except Exception as e:
            err_str = str(e)
            # Permanent Bilibili errors — don't retry
            if "79006" in err_str or "79014" in err_str or "79019" in err_str:
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
