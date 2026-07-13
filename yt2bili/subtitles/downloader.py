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
from yt2bili.youtube.downloader import (
    _with_yt_dlp_cookies,
    _yt_dlp_network_opts,
    _with_stderr_suppressed,
)


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


def _list_languages(video_url: str) -> tuple[dict, dict]:
    """
    Extract video info to inspect available subtitle languages.

    Tries bare yt-dlp first — cookie-authenticated requests often fail
    for subtitle metadata (YouTube returns "Requested format is not
    available").  Falls back to cookies only if bare extraction returns
    no subtitle tracks at all.

    Returns:
        Tuple of ``(subtitles, automatic_captions)`` dicts keyed by language code.
    """
    from yt_dlp import YoutubeDL

    base_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
    }
    base_opts["socket_timeout"] = max(
        1, int(getattr(config, "YOUTUBE_HTTP_TIMEOUT", 60) or 60)
    )
    base_opts["retries"] = 3

    # Phase 1: bare yt-dlp (avoids cookie-induced "format not available" errors)
    try:
        with YoutubeDL(base_opts) as ydl:
            info = ydl.extract_info(video_url, download=False)
    except Exception:
        info = None

    # Phase 2: retry with cookies only when bare gave us no subtitle tracks
    subtitles = (info or {}).get("subtitles") or {}
    auto_captions = (info or {}).get("automatic_captions") or {}
    if not subtitles and not auto_captions:
        try:
            ydl_opts = copy.deepcopy(base_opts)
            _with_yt_dlp_cookies(
                ydl_opts,
                lambda ydl: ydl.extract_info(video_url, download=False),
                label="字幕语言检测",
            )
            # Re-extract with bare yt-dlp after cookie warm-up
            with YoutubeDL(base_opts) as ydl:
                info = ydl.extract_info(video_url, download=False)
        except Exception:
            pass  # keep whatever we had from bare extraction

    if not info:
        return {}, {}

    subtitles = info.get("subtitles") or {}
    auto_captions = info.get("automatic_captions") or {}
    return subtitles, auto_captions


def _download_subtitles_for_lang(video_url: str, lang: str, output_template: str) -> str | None:
    """
    Run yt-dlp to download subtitles for a specific language.

    Args:
        video_url: YouTube video URL.
        lang: Language code to download.
        output_template: yt-dlp output template.

    Returns:
        Path to the downloaded subtitle file, or ``None`` if download failed.
    """
    from yt_dlp import YoutubeDL

    subtitle_dir = str(Path(output_template).parent)

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,       # Only download subtitles
        "noplaylist": True,
        "writesubtitles": True,       # Download manual subtitles
        "writeautomaticsub": True,   # Also download auto-generated
        "subtitleslangs": [lang],
        "subtitlesformat": "srt",    # Prefer SRT format
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
        info = ydl.extract_info(video_url, download=True)
        # yt-dlp saves subtitles next to the video; try to find the .srt file
        if info:
            info_id = info.get("id", "")
            # yt-dlp naming for subtitles: {id}.{lang}.srt
            expected = Path(subtitle_dir) / f"{info_id}.{lang}.srt"
            if expected.exists():
                downloaded_path[0] = str(expected)

    # Phase 1: try without cookies first (avoids "format not available" errors)
    try:
        with YoutubeDL(ydl_opts) as ydl:
            ydl.extract_info(video_url, download=True)
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

    return downloaded_path[0]


def download_subtitles(video_url: str, video_id: str) -> str | None:
    """
    Download the best-matching subtitle for a YouTube video.

    Steps:
    1. Extract video info to list available subtitle languages.
    2. Match against ``SUBTITLE_SOURCE_LANGS`` regex patterns.
    3. Prefer manual (author-uploaded) over auto-generated captions.
    4. Download the matched language as SRT.

    Args:
        video_url: YouTube video URL.
        video_id: YouTube video ID (for file naming).

    Returns:
        Absolute path to the downloaded ``.srt`` file, or ``None`` if no
        matching subtitle was found.
    """
    output_template = str(Path(config.SUBTITLE_DIR) / f"{video_id}.%(ext)s")

    print(f"[字幕] 查询可用字幕语言...")
    patterns = _compile_lang_patterns()

    try:
        subtitles, auto_captions = _list_languages(video_url)
    except Exception as e:
        print(f"[字幕] [WARN]获取字幕列表失败: {e}")
        return None

    lang = _pick_language(subtitles, auto_captions, patterns)
    if not lang:
        return None

    print(f"[字幕] 下载 {lang} 字幕...")
    path = _download_subtitles_for_lang(video_url, lang, output_template)
    if path:
        print(f"[字幕] 下载完成: {Path(path).name}")
    else:
        print(f"[字幕] [WARN]字幕下载未产生文件")

    return path
