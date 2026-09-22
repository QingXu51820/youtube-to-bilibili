"""
Download YouTube subtitles via yt-dlp Python API.

Two-phase approach:
1. Quick metadata-only extraction to list available subtitle languages.
2. Targeted download for the best matching language (manual preferred, then auto).
"""

import re
import copy
from pathlib import Path

from yt2bili import config
from yt2bili.subtitles import resegment
from yt2bili.subtitles.parser import parse_subtitle
from yt2bili.youtube.downloader import (
    _with_yt_dlp_cookies,
    _yt_dlp_network_opts,
    _with_stderr_suppressed,
)


#: 值得交给延迟队列重试的 kind —— 名单之外一律当永久失败
RETRYABLE_KINDS = ("no_tracks", "list_failed", "download_failed")


def is_retryable_kind(kind: str) -> bool:
    """该源失败原因是否值得挂进延迟队列稍后重试。"""
    return kind in RETRYABLE_KINDS


class SubtitleUnavailable(RuntimeError):
    """
    源字幕拿不到，并带上「为什么」——调用方据此决定值不值得重试。

    ``kind`` 取值：

    * ``no_tracks``       —— 这次没有任何字幕轨（刚发布的视频常常还没生成）
    * ``no_match``        —— 有字幕轨，但没有一条匹配 ``SUBTITLE_SOURCE_LANGS``（永久）
    * ``list_failed``     —— 列字幕轨的请求失败（网络/代理/风控）
    * ``download_failed`` —— 选中了语言却没下到文件（网络/解析）
    * ``bad_timing``      —— 下到了但时间轴崩坏（传上去比没有更糟，永久）

    只有 ``no_match`` / ``bad_timing`` 是永久失败；其余交给延迟队列重试。
    继承 ``RuntimeError``，所以既有的 ``except Exception`` / ``except RuntimeError``
    调用点行为不变。
    """

    def __init__(
        self, kind: str, message: str, available: list[str] | None = None
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.available = list(available or [])

    @property
    def permanent(self) -> bool:
        """True 表示重试没有意义（未知 kind 一律按永久处理，别拿去重试）。"""
        return not is_retryable_kind(self.kind)


def _unavailable(
    kind: str, available: list[str] | None = None, detail: str = ""
) -> SubtitleUnavailable:
    """按 kind 生成带中文说明的 :class:`SubtitleUnavailable`。"""
    labels = {
        "no_tracks": "YouTube 上还没有该视频的字幕轨（可能仍在生成中）",
        "no_match": "YouTube 有字幕轨，但没有匹配 SUBTITLE_SOURCE_LANGS 的语言",
        "list_failed": "查询 YouTube 字幕轨失败（网络/代理）",
        "download_failed": "字幕轨存在，但下载未产生文件",
        "bad_timing": "字幕时间轴不可用，放弃该字幕",
    }
    message = labels.get(kind, kind)
    if available:
        message += f"；可用语言: {', '.join(available)}"
    if detail:
        message += f"；{detail}"
    return SubtitleUnavailable(kind, message, available)


def _compile_lang_patterns() -> list[re.Pattern]:
    """Compile the SUBTITLE_SOURCE_LANGS comma-separated regexes."""
    patterns: list[re.Pattern] = []
    for raw in config.SUBTITLE_SOURCE_LANGS.split(","):
        raw = raw.strip()
        if not raw:
            continue
        try:
            patterns.append(re.compile(raw))
        except re.error as e:
            print(f"[字幕] [WARN] 忽略无效的 language pattern {raw!r}: {e}")
    if not patterns:
        # Sensible default: match English
        patterns = [re.compile(r"en.*")]
    return patterns


def _pick_language(
    subtitles: dict,
    auto_captions: dict,
    patterns: list[re.Pattern],
) -> str | None:
    """
    Pick the best language code from available subtitles.

    Priority: manual subtitles first, then auto-generated captions.
    Within each tier the first pattern match wins.

    Args:
        subtitles: Dict of manual subtitle tracks keyed by language code.
        auto_captions: Dict of auto-generated caption tracks keyed by language code.
        patterns: Compiled regex patterns from SUBTITLE_SOURCE_LANGS.

    Returns:
        Matching language code or ``None``.
    """
    for lang_code in subtitles:
        for pat in patterns:
            if pat.fullmatch(lang_code):
                print(f"[字幕] 匹配手动字幕: {lang_code}")
                return lang_code

    for lang_code in auto_captions:
        for pat in patterns:
            if pat.fullmatch(lang_code):
                print(f"[字幕] 匹配自动字幕: {lang_code}")
                return lang_code

    # List what was available for debugging
    all_langs = sorted(set(subtitles.keys()) | set(auto_captions.keys()))
    print(f"[字幕] 未找到匹配的字幕语言。可用语言: {', '.join(all_langs)}")
    print(f"[字幕] 匹配规则: {[p.pattern for p in patterns]}")
    return None


def _list_languages(video_url: str) -> tuple[dict, dict, bool]:
    """
    Extract video info to inspect available subtitle languages.

    Tries bare yt-dlp first — cookie-authenticated requests often fail
    for subtitle metadata (YouTube returns "Requested format is not
    available").  Falls back to cookies only if bare extraction returns
    no subtitle tracks at all.

    Returns:
        ``(subtitles, automatic_captions, ok)`` — dicts keyed by language code,
        plus whether *any* extraction actually succeeded.  ``ok=False`` with
        empty dicts means "we could not ask YouTube" (network/proxy), which the
        caller must not confuse with "the video has no caption tracks".
    """
    from yt_dlp import YoutubeDL

    base_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "js_runtimes": {"node": {}},
    }
    base_opts.update(_yt_dlp_network_opts())

    # Phase 1: bare yt-dlp (avoids cookie-induced "format not available" errors)
    try:
        def _bare_extract():
            with YoutubeDL(base_opts) as ydl:
                return ydl.extract_info(video_url, download=False)

        info = _with_stderr_suppressed(_bare_extract)
    except Exception:
        info = None

    # Phase 2: retry with cookies only when bare gave us no subtitle tracks
    subtitles = (info or {}).get("subtitles") or {}
    auto_captions = (info or {}).get("automatic_captions") or {}
    if not subtitles and not auto_captions:
        try:
            ydl_opts = copy.deepcopy(base_opts)
            info = _with_yt_dlp_cookies(
                ydl_opts,
                lambda ydl: ydl.extract_info(video_url, download=False),
                label="字幕语言检测",
            )
        except Exception:
            pass  # keep whatever we had from bare extraction

    if not info:
        # 两次都没问到 —— 网络/代理问题，不是"这个视频没字幕"
        return {}, {}, False

    subtitles = info.get("subtitles") or {}
    auto_captions = info.get("automatic_captions") or {}
    return subtitles, auto_captions, True


def _find_subtitle_file(
    subtitle_dir: str | Path,
    video_id: str,
    lang: str,
    formats: tuple[str, ...] = ("json3", "srt"),
) -> str | None:
    """Return the first existing ``{video_id}.{lang}.{fmt}`` file.

    yt-dlp naming for subtitles is ``{id}.{lang}.{ext}``.  Formats are
    checked in order — json3 (word-level) is preferred over plain srt.
    """
    base = Path(subtitle_dir) / f"{video_id}.{lang}"
    for fmt in formats:
        candidate = Path(f"{base}.{fmt}")
        if candidate.exists():
            return str(candidate)
    return None


def _resegment_json3_to_srt(json3_path: str | Path, srt_path: str | Path) -> str | None:
    """Re-segment a downloaded json3 into the canonical ``.srt`` source file.

    On success the json3 intermediate is deleted and the srt path returned.
    On any failure the json3 is kept for debugging and ``None`` returned —
    the caller falls back to a plain srt download.
    """
    try:
        path = resegment.resegment_file(json3_path, srt_path)
        Path(json3_path).unlink(missing_ok=True)
        print(f"[字幕] json3 重分段完成: {Path(path).name}")
        return path
    except Exception as e:
        print(f"[字幕] [WARN]json3 重分段失败，回退普通 srt 字幕: {e}")
        return None


def _download_subtitles_for_lang(
    video_url: str,
    lang: str,
    output_template: str,
    video_id: str,
    force_srt: bool = False,
) -> str | None:
    """
    Run yt-dlp to download subtitles for a specific language.

    When ``SUBTITLE_RESEGMENT_ENABLED`` is on (and ``force_srt`` is False),
    requests json3 word-level captions and re-segments them into
    sentence-based ``{video_id}.{lang}.srt`` — the canonical source file
    downstream consumers expect.  Falls back to a plain srt download when
    json3 is unavailable or re-segmentation fails.

    Args:
        video_url: YouTube video URL.
        lang: Language code to download.
        output_template: yt-dlp output template.
        video_id: YouTube video ID (for file naming).
        force_srt: Skip the json3/resegment path entirely.

    Returns:
        Path to the downloaded subtitle file, or ``None`` if download failed.
    """
    from yt_dlp import YoutubeDL

    subtitle_dir = str(Path(output_template).parent)

    if force_srt or not config.SUBTITLE_RESEGMENT_ENABLED:
        sub_format = "srt"
    else:
        sub_format = "json3/srt"

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,       # Only download subtitles
        "noplaylist": True,
        "js_runtimes": {"node": {}},
        "writesubtitles": True,       # Download manual subtitles
        "writeautomaticsub": True,   # Also download auto-generated
        "subtitleslangs": [lang],
        "subtitlesformat": sub_format,
        "outtmpl": output_template,
    }
    ydl_opts.update(copy.deepcopy(_yt_dlp_network_opts()))

    downloaded_path: list[str | None] = [None]

    def _progress_hook(d: dict):
        if d.get("status") == "finished":
            filename = d.get("info_dict", {}).get("_filename") or d.get("filename", "")
            if filename:
                downloaded_path[0] = filename

    ydl_opts["progress_hooks"] = [_progress_hook]

    def _download(ydl):
        ydl.extract_info(video_url, download=True)
        # yt-dlp saves subtitles next to the video; find the produced file
        downloaded_path[0] = _find_subtitle_file(subtitle_dir, video_id, lang)

    # Phase 1: try without cookies first (avoids "format not available" errors)
    try:
        def _bare_download():
            with YoutubeDL(ydl_opts) as ydl:
                ydl.extract_info(video_url, download=True)

        _with_stderr_suppressed(_bare_download)
    except Exception:
        pass  # fall through to cookie-based retry

    # Check if file was produced by bare download
    if not downloaded_path[0]:
        # Phase 2: fall back to cookie-authenticated download
        try:
            _with_yt_dlp_cookies(ydl_opts, _download, label="字幕下载")
        except Exception as e:
            print(f"[字幕] [WARN]yt-dlp 字幕下载异常: {e}")
            # Last resort: try once more without cookies
            try:
                with YoutubeDL(ydl_opts) as ydl:
                    ydl.extract_info(video_url, download=True)
            except Exception as e2:
                print(f"[字幕] [WARN]字幕下载 fallback 也失败: {e2}")

    # Authoritative post-chain check (also covers the last-resort bare retry,
    # whose progress hook may not fire reliably)
    if not downloaded_path[0]:
        downloaded_path[0] = _find_subtitle_file(subtitle_dir, video_id, lang)

    path = downloaded_path[0]
    if not path:
        return None

    # ── json3 re-segmentation post-processing ────────────────────────
    if sub_format == "srt":
        return path  # toggle off or forced srt — old behavior exactly

    if Path(path).suffix == ".json3":
        srt_path = Path(path).with_suffix(".srt")
        reseg = _resegment_json3_to_srt(path, srt_path)
        if reseg:
            return reseg
        if srt_path.exists():
            return str(srt_path)  # defensive: leftover srt from a prior run
        print("[字幕] [WARN]json3 重分段失败且无 srt，改用普通 srt 下载")
        return _download_subtitles_for_lang(
            video_url, lang, output_template, video_id, force_srt=True
        )

    return path


def _list_or_fail(video_url: str) -> tuple[dict, dict]:
    """列字幕轨；网络失败时**立即重列一次**再放弃。

    重列不 sleep：这只是一次元数据提取，而这一阶段恰好是代理抖动最容易打中的地方，
    重试一次的收益（省掉一整轮延迟队列的等待）远大于成本。下载阶段不重试 ——
    那条链内部已经有 bare→cookies→bare + 回退，再套一层会在 `before_download`
    里同步阻塞下一个视频的下载。
    """
    subtitles, auto_captions, ok = _list_languages(video_url)
    if ok:
        return subtitles, auto_captions

    print("[字幕] [WARN] 字幕轨查询失败，立即重试一次...")
    subtitles, auto_captions, ok = _list_languages(video_url)
    if ok:
        return subtitles, auto_captions

    raise _unavailable("list_failed")


def download_subtitles(video_url: str, video_id: str) -> str | None:
    """
    Download the best-matching subtitle for a YouTube video.

    Steps:
    1. Extract video info to list available subtitle languages.
    2. Match against ``SUBTITLE_SOURCE_LANGS`` regex patterns.
    3. Prefer manual (author-uploaded) over auto-generated captions.
    4. Download the matched language — json3 (re-segmented into sentence-based
       SRT) when ``SUBTITLE_RESEGMENT_ENABLED``, otherwise plain SRT.

    Args:
        video_url: YouTube video URL.
        video_id: YouTube video ID (for file naming).

    Returns:
        Absolute path to the downloaded ``.srt`` file.

    Raises:
        SubtitleUnavailable: 源字幕拿不到，``kind`` 说明原因（见该异常类）。
            不再返回 ``None`` —— 调用方需要区分"还没生成""不匹配""网络失败"。
    """
    output_template = str(Path(config.SUBTITLE_DIR) / f"{video_id}.%(ext)s")

    print(f"[字幕] 查询可用字幕语言...")
    patterns = _compile_lang_patterns()

    subtitles, auto_captions = _list_or_fail(video_url)

    lang = _pick_language(subtitles, auto_captions, patterns)
    if not lang:
        available = sorted(set(subtitles) | set(auto_captions))
        if available:
            # 有轨但语言不匹配（例如该频道只有 zh-HK 手动轨）—— 重试不会有别的结果
            raise _unavailable("no_match", available)
        raise _unavailable("no_tracks")

    print(f"[字幕] 下载 {lang} 字幕...")
    path = _download_subtitles_for_lang(video_url, lang, output_template, video_id)
    if path:
        print(f"[字幕] 下载完成: {Path(path).name}")
    else:
        print(f"[字幕] [WARN]字幕下载未产生文件")
        raise _unavailable("download_failed", detail=f"语言 {lang}")

    # 最后一道关卡：普通 srt 回退不走重分段，所以这里再验一次最终产物。
    # 时长崩坏的字幕比没有字幕更糟——它会直接传上 B 站，没人发现。
    defect = resegment.timing_defect(parse_subtitle(path))
    if defect:
        raise _unavailable("bad_timing", detail=defect)

    return path
