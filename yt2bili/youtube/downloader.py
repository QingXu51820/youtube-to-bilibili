"""
YouTube video downloader using yt-dlp.
Downloads best quality up to 1080p, thumbnail, and extracts metadata.
"""

import json
import subprocess
import sys
import threading
import time
import urllib.request
from copy import deepcopy
from pathlib import Path
from dataclasses import dataclass

from yt2bili import config
from yt2bili.media.cover import is_valid_image


BLOCKED_LIVE_STATUSES = {
    "is_live": "正在直播",
    "is_upcoming": "预约直播",
    "post_live": "直播刚结束，仍在处理",
}


class SlowDownloadError(RuntimeError):
    """Raised when yt-dlp download speed stays below the configured threshold."""


SLOW_DOWNLOAD_MARKER = "__YT2BILI_SLOW_DOWNLOAD__"


def _is_slow_download_exception(exc: BaseException) -> bool:
    text = str(exc)
    return (
        isinstance(exc, SlowDownloadError)
        or SLOW_DOWNLOAD_MARKER in text
        or "下载速度低于" in text
        or "SlowDownloadError" in text
    )


def _slow_download_message(exc: BaseException) -> str:
    return str(exc).replace(SLOW_DOWNLOAD_MARKER, "").strip()


def _is_range_not_satisfiable(exc: BaseException) -> bool:
    """Check if an exception is HTTP 416: Requested range not satisfiable."""
    text = str(exc).lower()
    return "416" in text or "range not satisfiable" in text


def _clean_partial_files(download_dir: Path, video_id: str) -> None:
    """Remove stale .part/.ytdl files for a given video ID so retry starts fresh."""
    if not download_dir or not download_dir.exists():
        return
    if not video_id:
        return
    for pattern in (f"*{video_id}*.part", f"*{video_id}*.ytdl"):
        for f in download_dir.glob(pattern):
            try:
                f.unlink()
                print(f"[下载] 清理残留文件: {f.name}")
            except OSError:
                pass


@dataclass
class VideoInfo:
    """Metadata and file info for a downloaded video."""
    file_path: str        # absolute path to downloaded video
    title: str            # original YouTube title
    description: str      # video description
    original_url: str     # the YouTube URL
    video_id: str = ""    # YouTube video ID
    thumbnail_path: str = ""  # path to downloaded thumbnail
    width: int = 0
    height: int = 0
    duration: float = 0.0  # seconds, probed from merged file


def _download_proxy() -> str:
    """Proxy used by yt-dlp and thumbnail downloads."""
    return getattr(config, "DOWNLOAD_PROXY", "") or getattr(config, "YOUTUBE_PROXY", "")


def _remote_components() -> list[str]:
    raw = str(getattr(config, "YTDLP_REMOTE_COMPONENTS", "ejs:github") or "")
    return [
        part.strip()
        for part in raw.split(",")
        if part.strip() and part.strip().lower() not in {"none", "false", "0"}
    ]


def _yt_dlp_network_opts() -> dict:
    """Shared yt-dlp network options."""
    min_speed_bps = max(0, int(getattr(config, "DOWNLOAD_MIN_SPEED_KIB", 100) or 0)) * 1024
    opts = {
        "socket_timeout": max(1, int(getattr(config, "YOUTUBE_HTTP_TIMEOUT", 60) or 60)),
        "retries": 5,
        "fragment_retries": 5,
        "extractor_retries": 3,
    }
    if min_speed_bps:
        opts["throttledratelimit"] = min_speed_bps
    proxy = _download_proxy()
    if proxy:
        opts["proxy"] = proxy
    remote_components = _remote_components()
    if remote_components:
        opts["remote_components"] = remote_components
    return opts


def _cookie_file() -> str:
    return str(getattr(config, "YOUTUBE_COOKIE_FILE", "config/cookies.txt") or "").strip()


def _cookie_file_path() -> Path | None:
    cookie_file = _cookie_file()
    if not cookie_file:
        return None
    path = Path(cookie_file).expanduser()
    if not path.is_absolute():
        path = Path(config.PROJECT_ROOT) / path
    return path


def _cookie_browsers() -> list[tuple[str, ...]]:
    raw = str(getattr(config, "YOUTUBE_COOKIES_FROM_BROWSER", "chrome,edge,firefox") or "")
    browsers = []
    for item in raw.split(","):
        parts = tuple(part.strip() for part in item.strip().split(":") if part.strip())
        if not parts:
            continue
        browser = parts[0].lower()
        if browser not in {"none", "false", "0"}:
            browsers.append((browser, *parts[1:]))
    return browsers


def _format_cookie_browser(browser: tuple[str, ...]) -> str:
    return ":".join(browser)


def _is_youtube_cookie_domain(domain: str) -> bool:
    normalized = domain.lstrip(".").lower()
    return (
        normalized == "youtube.com"
        or normalized.endswith(".youtube.com")
        or normalized == "youtube-nocookie.com"
        or normalized.endswith(".youtube-nocookie.com")
        or normalized == "google.com"
        or normalized.endswith(".google.com")
    )


class _CookieExportLogger:
    def __init__(self, *, quiet: bool = False):
        self.quiet = quiet

    def debug(self, message: str) -> None:
        pass

    def info(self, message: str) -> None:
        if not self.quiet:
            print(f"[Cookie] {message}")

    def warning(self, message: str) -> None:
        if not self.quiet:
            print(f"[Cookie] ⚠️ {message}")

    def error(self, message: str) -> None:
        if not self.quiet:
            print(f"[Cookie] ❌ {message}")


def refresh_youtube_cookies(cookie_file: str | Path | None = None, *, quiet: bool = False) -> Path | None:
    """Export YouTube/Google cookies from configured browsers into cookies.txt."""
    from yt_dlp.cookies import YoutubeDLCookieJar, extract_cookies_from_browser

    output_path = Path(cookie_file).expanduser() if cookie_file else _cookie_file_path()
    if output_path is None:
        if not quiet:
            print("[Cookie] 未配置 YOUTUBE_COOKIE_FILE，跳过自动生成 Cookie 文件")
        return None
    if not output_path.is_absolute():
        output_path = Path(config.PROJECT_ROOT) / output_path

    output_path.parent.mkdir(parents=True, exist_ok=True)
    browsers = _cookie_browsers()
    if not browsers:
        if not quiet:
            print("[Cookie] 未配置可读取的浏览器，跳过自动生成 Cookie 文件")
        return None

    last_error = None
    for browser in browsers:
        browser_name = browser[0]
        profile = browser[1] if len(browser) > 1 else None
        keyring = browser[2] if len(browser) > 2 else None
        container = browser[3] if len(browser) > 3 else None
        browser_label = _format_cookie_browser(browser)
        try:
            if not quiet:
                print(f"[Cookie] 尝试从 {browser_label} 导出 YouTube Cookie")
            jar = extract_cookies_from_browser(
                browser_name,
                profile=profile,
                logger=_CookieExportLogger(quiet=quiet),
                keyring=keyring,
                container=container,
            )
            filtered = YoutubeDLCookieJar(str(output_path))
            for cookie in jar:
                if _is_youtube_cookie_domain(cookie.domain):
                    filtered.set_cookie(cookie)
            if len(filtered) == 0:
                last_error = RuntimeError(f"{browser_label} 中没有找到 YouTube/Google Cookie")
                if not quiet:
                    print(f"[Cookie] 读取 {browser_label} 成功，但没有找到 YouTube 登录 Cookie")
                continue
            filtered.save(str(output_path), ignore_discard=True, ignore_expires=True)
            if not quiet:
                print(f"[Cookie] ✅ 已生成: {output_path} ({len(filtered)} 条 YouTube/Google Cookie)")
            return output_path
        except Exception as e:
            last_error = e
            if not quiet:
                print(f"[Cookie] 读取 {browser_label} 失败，继续尝试下一个: {e}")

    if not quiet:
        print(f"[Cookie] ❌ 自动生成 Cookie 文件失败: {last_error}")
    return None


def _ensure_cookie_file_exists(*, quiet: bool = False) -> bool:
    cookie_path = _cookie_file_path()
    if cookie_path is None:
        return False
    if cookie_path.exists() and cookie_path.stat().st_size > 0:
        return True
    if not quiet:
        print(f"[下载] Cookie 文件不存在，尝试自动生成: {cookie_path}")
    return refresh_youtube_cookies(cookie_path, quiet=quiet) is not None


def _apply_cookie_file(ydl_opts: dict) -> bool:
    cookie_path = _cookie_file_path()
    if cookie_path is None:
        return False
    if not cookie_path.exists():
        print(f"[下载] ⚠️ Cookie 文件不存在，已忽略: {cookie_path}")
        return False
    ydl_opts["cookiefile"] = str(cookie_path)
    return True


def _is_youtube_bot_exception(exc: BaseException) -> bool:
    text = str(exc).lower()
    return "sign in to confirm" in text or "not a bot" in text or "cookies-from-browser" in text


def _is_cookie_source_exception(exc: BaseException) -> bool:
    text = str(exc).lower()
    needles = (
        "cookie",
        "cookies",
        "dpapi",
        "decrypt",
        "permission denied",
        "could not copy chrome",
        "could not find firefox",
        "could not find chrome",
    )
    return any(needle in text for needle in needles)


def _cookie_hint() -> str:
    return (
        "YouTube 要求登录验证。请确认你在 Chrome/Edge/Firefox 中已登录 YouTube，"
        "并关闭正在使用该浏览器配置文件的全部窗口后重试；"
        "或者运行 python main.py --refresh-youtube-cookies 自动生成 config/cookies.txt。"
    )


def _with_stderr_suppressed(callback):
    """Run a callback while suppressing noisy yt-dlp browser-cookie stderr."""
    import os

    stderr_fd = os.dup(2)
    try:
        os.close(2)
        os.open(os.devnull, os.O_WRONLY)
        return callback()
    finally:
        os.close(2)
        os.dup2(stderr_fd, 2)
        os.close(stderr_fd)


def _with_yt_dlp_cookies(ydl_opts: dict, operation, *, label: str):
    """Run a yt-dlp operation with cookie file/browser fallback."""
    from yt_dlp import YoutubeDL

    _ensure_cookie_file_exists(quiet=False)
    cookie_file_opts = dict(ydl_opts)
    if _apply_cookie_file(cookie_file_opts):
        try:
            print("[下载] 使用 Cookie 文件")
            def _run_cookie_file():
                with YoutubeDL(cookie_file_opts) as ydl:
                    return operation(ydl)

            return _with_stderr_suppressed(_run_cookie_file)
        except Exception as e:
            if _is_slow_download_exception(e):
                raise
            if _is_youtube_bot_exception(e):
                print("[下载] Cookie 文件可能已失效，尝试自动刷新")
                if refresh_youtube_cookies(quiet=True):
                    try:
                        refreshed_opts = dict(ydl_opts)
                        if _apply_cookie_file(refreshed_opts):
                            print("[下载] 使用刷新后的 Cookie 文件")
                            def _run_refreshed_cookie_file():
                                with YoutubeDL(refreshed_opts) as ydl:
                                    return operation(ydl)

                            return _with_stderr_suppressed(_run_refreshed_cookie_file)
                    except Exception as retry_error:
                        if _is_slow_download_exception(retry_error):
                            raise
                        e = retry_error
            if not _is_youtube_bot_exception(e) and not _is_cookie_source_exception(e):
                raise RuntimeError(f"{label}失败:\n{e}") from e
            print(f"[下载] Cookie 文件读取失败，尝试浏览器 Cookie: {e}")

    last_cookie_error = None
    for browser in _cookie_browsers():
        browser_opts = dict(ydl_opts)
        browser_opts["cookiesfrombrowser"] = browser
        browser_label = _format_cookie_browser(browser)
        try:
            print(f"[下载] 使用浏览器 Cookie: {browser_label}")
            def _run_browser_cookie():
                with YoutubeDL(browser_opts) as ydl:
                    return operation(ydl)

            return _with_stderr_suppressed(_run_browser_cookie)
        except Exception as e:
            if _is_slow_download_exception(e):
                raise
            if not _is_youtube_bot_exception(e) and not _is_cookie_source_exception(e):
                raise RuntimeError(f"{label}失败:\n{e}") from e
            last_cookie_error = e
            print(f"[下载] 读取 {browser_label} Cookie 失败，继续尝试下一个")

    try:
        with YoutubeDL(ydl_opts) as ydl:
            return operation(ydl)
    except Exception as e:
        if _is_youtube_bot_exception(e):
            detail = f"\n最后一次 Cookie 错误: {last_cookie_error}" if last_cookie_error else ""
            raise RuntimeError(f"{e}\n{_cookie_hint()}{detail}") from e
        raise RuntimeError(f"{label}失败:\n{e}") from e


def _open_url(req: urllib.request.Request, timeout: int):
    """Open a URL through the configured download proxy when present."""
    proxy = _download_proxy()
    if not proxy:
        return urllib.request.urlopen(req, timeout=timeout)
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": proxy, "https": proxy})
    )
    return opener.open(req, timeout=timeout)


def _format_resolution(width: int | None, height: int | None) -> str:
    if not width or not height:
        return ""
    return f"{width}x{height}"


def _best_available_resolution(info: dict) -> str:
    """Estimate the highest downloadable video resolution under MAX_HEIGHT."""
    candidates = []
    for fmt in info.get("formats", []) or []:
        height = fmt.get("height")
        width = fmt.get("width")
        vcodec = fmt.get("vcodec")
        if not height or vcodec == "none":
            continue
        if int(height) > config.MAX_HEIGHT:
            continue
        candidates.append((int(height), int(width or 0)))

    if not candidates:
        return _format_resolution(info.get("width"), info.get("height"))

    height, width = max(candidates, key=lambda item: (item[0], item[1]))
    return _format_resolution(width, height)


def _probe_video_resolution(file_path: Path) -> tuple[int, int] | None:
    """Probe the merged video file resolution with ffprobe."""
    command = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=width,height",
        "-of",
        "json",
        str(file_path),
    ]
    try:
        proc = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None

    if proc.returncode != 0:
        return None

    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None

    streams = data.get("streams") or []
    if not streams:
        return None

    width = int(streams[0].get("width") or 0)
    height = int(streams[0].get("height") or 0)
    if not width or not height:
        return None
    return width, height


def _probe_video_duration(file_path: Path) -> float:
    """Probe video file duration in seconds using ffprobe."""
    command = [
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "csv=p=0",
        str(file_path),
    ]
    try:
        proc = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return 0.0
    if proc.returncode != 0:
        return 0.0
    try:
        return float(proc.stdout.strip())
    except (ValueError, TypeError):
        return 0.0


def _convert_webm_to_mp4(webm_path: Path) -> Path:
    """Convert a WebM video to H.264+AAC MP4 using ffmpeg.

    Returns the path to the converted MP4 file, or the original path
    if conversion fails (caller should handle the original gracefully).
    """
    mp4_path = webm_path.with_suffix(".mp4")
    print(f"[下载] 转换 WebM → MP4 (H.264/AAC)...")
    file_size_mb = webm_path.stat().st_size / 1024 / 1024
    print(f"[下载] 源文件: {file_size_mb:.1f} MB，可能需要几分钟...")

    command = [
        "ffmpeg",
        "-y",                       # overwrite output
        "-i", str(webm_path),
        "-c:v", "libx264",
        "-preset", "fast",
        "-crf", "23",
        "-c:a", "aac",
        "-b:a", "192k",
        "-movflags", "+faststart",
        "-loglevel", "error",
        "-stats",
        str(mp4_path),
    ]
    try:
        proc = subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=None,  # show ffmpeg progress directly in terminal
        )
        proc.wait(timeout=7200)  # 2h max
        if proc.returncode != 0:
            raise subprocess.CalledProcessError(proc.returncode, command)

        if mp4_path.exists() and mp4_path.stat().st_size > 0:
            new_size_mb = mp4_path.stat().st_size / 1024 / 1024
            print(f"[下载] 转换完成: {mp4_path.name} ({new_size_mb:.1f} MB)")
            # Remove original WebM
            webm_path.unlink(missing_ok=True)
            return mp4_path
        else:
            raise RuntimeError("ffmpeg 完成但未生成 MP4 文件")
    except Exception as e:
        print(f"\n[下载] ⚠️ WebM→MP4 转换失败: {e}，将尝试直接上传原始文件")
        return webm_path


def _download_thumbnail(video_id: str, download_dir: Path) -> str:
    """
    Download YouTube thumbnail for a video.
    Tries maxresdefault first, falls back to hqdefault.

    Returns:
        Path to downloaded thumbnail image
    """
    thumbnail_dir = download_dir / "thumbnails"
    thumbnail_dir.mkdir(parents=True, exist_ok=True)

    thumbnail_path = thumbnail_dir / f"{video_id}.jpg"

    # Skip if already downloaded and valid
    if thumbnail_path.exists():
        if is_valid_image(thumbnail_path):
            return str(thumbnail_path)
        thumbnail_path.unlink(missing_ok=True)

    # Try different thumbnail resolutions
    urls = [
        f"https://img.youtube.com/vi/{video_id}/maxresdefault.jpg",
        f"https://img.youtube.com/vi/{video_id}/hqdefault.jpg",
        f"https://img.youtube.com/vi/{video_id}/sddefault.jpg",
    ]

    for thumb_url in urls:
        try:
            req = urllib.request.Request(thumb_url, headers={
                "User-Agent": "Mozilla/5.0"
            })
            with _open_url(req, timeout=min(30, max(1, config.YOUTUBE_HTTP_TIMEOUT))) as resp:
                data = resp.read()
                if len(data) > 1000:  # valid image
                    thumbnail_path.write_bytes(data)
                    if is_valid_image(thumbnail_path):
                        print(f"[下载] 缩略图: {thumbnail_path.name} ({len(data)//1024}KB)")
                        return str(thumbnail_path)
                    thumbnail_path.unlink(missing_ok=True)
        except Exception:
            continue

    print(f"[下载] ⚠️ 缩略图下载失败，将不使用封面")
    return ""


def _extract_metadata(url: str) -> dict:
    """Extract YouTube metadata through yt-dlp's Python API."""
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
        "ignore_no_formats_error": True,
        "js_runtimes": {"node": {}},
    }
    ydl_opts.update(_yt_dlp_network_opts())

    try:
        return _with_yt_dlp_cookies(
            ydl_opts,
            lambda ydl: ydl.extract_info(url, download=False),
            label="获取视频信息",
        )
    except Exception as e:
        raise RuntimeError(f"获取视频信息失败:\n{e}") from e


def _reject_non_video_content(info: dict) -> None:
    """Reject live/upcoming stream entries before download starts."""
    live_status = (info.get("live_status") or "").strip()
    if info.get("is_live"):
        live_status = live_status or "is_live"

    if live_status in BLOCKED_LIVE_STATUSES:
        label = BLOCKED_LIVE_STATUSES[live_status]
        title = (info.get("title") or "").strip()
        raise RuntimeError(
            f"检测到该链接是{label}，不是可下载的普通视频，已跳过: {title}"
        )


def _find_downloaded_video(download_dir: Path, video_id: str) -> Path | None:
    """Find a downloaded video by ID when yt-dlp skips an existing file."""
    for ext in (".mp4", ".mkv", ".webm", ".mov"):
        candidate = download_dir / f"{video_id}{ext}"
        if candidate.exists() and candidate.stat().st_size > 0:
            return candidate
    return None


def _download_with_progress(url: str, output_template: str, info: dict | None = None) -> str:
    """Download a YouTube video using yt-dlp Python API with a clean progress bar.
    Returns the actual downloaded file path."""
    min_speed_kib = max(0, int(getattr(config, "DOWNLOAD_MIN_SPEED_KIB", 100) or 0))
    min_speed_bps = min_speed_kib * 1024
    slow_seconds = max(1, int(getattr(config, "DOWNLOAD_SLOW_SECONDS", 60) or 60))
    grace_seconds = max(0, int(getattr(config, "DOWNLOAD_SLOW_GRACE_SECONDS", 30) or 30))
    max_restarts = max(0, int(getattr(config, "DOWNLOAD_MAX_RESTARTS", 3) or 0))
    startup_status_seconds = max(
        0,
        int(getattr(config, "DOWNLOAD_STARTUP_STATUS_SECONDS", 30) or 0),
    )

    if min_speed_bps:
        print(
            f"[下载] 低速保护: <{min_speed_kib} KiB/s 连续 {slow_seconds}s "
            f"将重启，最多 {max_restarts} 次"
        )

    def _run_download_once(ydl_opts: dict) -> None:
        """Run one yt-dlp download attempt with cookie fallback."""
        def _download_operation(ydl):
            if info:
                # Filter formats to respect MAX_HEIGHT before passing to process_info
                filtered = deepcopy(info)
                formats = filtered.get("formats") or []
                if formats:
                    kept = [f for f in formats
                            if f.get("vcodec") != "none"
                            and int(f.get("height") or 0) <= config.MAX_HEIGHT]
                    if kept:
                        filtered["formats"] = kept
                    else:
                        # No formats under height limit — fall back to original list
                        print(f"[下载] ⚠️ 无 ≤{config.MAX_HEIGHT}p 格式可用，使用全部格式")
                print("[下载] 复用已解析的视频格式，准备请求媒体流...")
                return ydl.process_info(filtered)
            print("[下载] 解析下载链接...")
            return ydl.download([url])

        _with_yt_dlp_cookies(
            ydl_opts,
            _download_operation,
            label="下载",
        )

    use_continuedl = True
    use_nopart = True

    for attempt in range(max_restarts + 1):
        final_path = [None]  # mutable container for closure
        first_progress_at = [None]
        slow_started_at = [None]
        attempt_started_at = time.monotonic()
        stop_status = threading.Event()

        def _startup_status_loop() -> None:
            while not stop_status.wait(startup_status_seconds):
                if first_progress_at[0] is None:
                    elapsed = int(time.monotonic() - attempt_started_at)
                    print(f"\n[下载] 仍在解析下载链接/等待媒体流... {elapsed}s")

        status_thread = None
        if startup_status_seconds:
            status_thread = threading.Thread(target=_startup_status_loop, daemon=True)
            status_thread.start()

        def _progress_hook(d):
            now = time.monotonic()
            if first_progress_at[0] is None:
                first_progress_at[0] = now

            if d["status"] == "downloading":
                pct = d.get("_percent_str", "???").strip()
                speed = d.get("_speed_str", "???").strip()
                eta = d.get("_eta_str", "???").strip()
                total = d.get("_total_bytes_str") or d.get("_total_bytes_estimate_str") or "???"

                speed_bps = d.get("speed")
                elapsed = d.get("elapsed")
                elapsed_seconds = elapsed if isinstance(elapsed, (int, float)) else now - first_progress_at[0]
                slow_note = ""

                if min_speed_bps and isinstance(speed_bps, (int, float)) and elapsed_seconds >= grace_seconds:
                    if speed_bps < min_speed_bps:
                        if slow_started_at[0] is None:
                            slow_started_at[0] = now
                        slow_elapsed = int(now - slow_started_at[0])
                        slow_note = f"  低速 {slow_elapsed}/{slow_seconds}s"
                        if slow_elapsed >= slow_seconds:
                            sys.stdout.write("\r" + " " * 120 + "\r")
                            sys.stdout.flush()
                            raise SlowDownloadError(
                                f"{SLOW_DOWNLOAD_MARKER} 下载速度低于 {min_speed_kib} KiB/s "
                                f"已持续 {slow_seconds} 秒"
                            )
                    else:
                        slow_started_at[0] = None

                bar_width = 30
                try:
                    pct_val = float(pct.replace("%", ""))
                    filled = int(bar_width * pct_val / 100)
                except (ValueError, AttributeError):
                    filled = 0
                bar = "█" * filled + "░" * (bar_width - filled)

                line = f"\r  {bar}  {pct}  {speed}  ETA {eta}  {total}{slow_note}"
                sys.stdout.write(line)
                sys.stdout.flush()

            elif d["status"] == "finished":
                sys.stdout.write("\r" + " " * 120 + "\r")
                sys.stdout.flush()
                print(f"  [下载] 合并音视频...")
                if d.get("info_dict"):
                    tmpl = d["info_dict"].get("_filename") or d.get("filename", "")
                    if tmpl:
                        final_path[0] = str(tmpl)

        ydl_opts = {
            "format": (
                f"bestvideo[height<={config.MAX_HEIGHT}][vcodec^=avc1]+bestaudio/"
                f"bestvideo[height<={config.MAX_HEIGHT}]+bestaudio/"
                f"best[height<={config.MAX_HEIGHT}][vcodec^=avc1]/"
                f"best[height<={config.MAX_HEIGHT}]"
            ),
            "merge_output_format": "mp4",
            "outtmpl": output_template,
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "progress_hooks": [_progress_hook],
            "concurrent_fragment_downloads": 16,
            # Resume partial downloads
            "continuedl": use_continuedl,
            "nopart": use_nopart,
            # JS runtime for better YouTube extraction
            "js_runtimes": {"node": {}},
            # Thumbnail: write separate file for B站 cover
            "writethumbnail": True,
        }
        ydl_opts.update(_yt_dlp_network_opts())

        try:
            if attempt:
                print(f"[下载] 重试下载 {attempt}/{max_restarts}...")
            _run_download_once(ydl_opts)
            stop_status.set()
            if status_thread:
                status_thread.join(timeout=1)
            print()
            return final_path[0] or ""
        except SlowDownloadError as e:
            stop_status.set()
            if status_thread:
                status_thread.join(timeout=1)
            if attempt >= max_restarts:
                raise RuntimeError(
                    f"下载失败: {_slow_download_message(e)}，已达到最大重启次数 {max_restarts}"
                ) from e
            print(f"\n[下载] ⚠️ {_slow_download_message(e)}，正在重启下载 ({attempt + 1}/{max_restarts})...")
            time.sleep(3)
        except Exception as e:
            stop_status.set()
            if status_thread:
                status_thread.join(timeout=1)
            if _is_slow_download_exception(e):
                if attempt >= max_restarts:
                    raise RuntimeError(
                        f"下载失败: {_slow_download_message(e)}，已达到最大重启次数 {max_restarts}"
                    ) from e
                print(f"\n[下载] ⚠️ {_slow_download_message(e)}，正在重启下载 ({attempt + 1}/{max_restarts})...")
                time.sleep(3)
                continue
            if _is_range_not_satisfiable(e) and ydl_opts.get("continuedl"):
                # HTTP 416: server doesn't support range resume for this file.
                # Delete partial files and retry from scratch.
                _clean_partial_files(
                    Path(config.DOWNLOAD_DIR),
                    info.get("id", "") if info else "",
                )
                ydl_opts.pop("continuedl", None)
                ydl_opts.pop("nopart", None)
                use_continuedl = False
                use_nopart = False
                if attempt >= max_restarts:
                    raise RuntimeError(
                        f"下载失败: HTTP 416 范围请求不可用，已达到最大重启次数 {max_restarts}"
                    ) from e
                print(f"\n[下载] ⚠️ 断点续传失败 (HTTP 416)，已清理残留文件，从头下载...")
                continue
            raise RuntimeError(f"下载失败: {e}") from e

    raise RuntimeError(f"下载失败: 已达到最大重启次数 {max_restarts}，下载未完成")


def download_video(url: str) -> VideoInfo:
    """
    Download a YouTube video at 1080p or lower, merge into mp4,
    and return metadata.

    Args:
        url: YouTube video URL

    Returns:
        VideoInfo with file path, title, description, and original URL

    Raises:
        RuntimeError: if yt-dlp is not installed or download fails
    """
    # Ensure download directory exists
    download_dir = Path(config.DOWNLOAD_DIR)
    download_dir.mkdir(parents=True, exist_ok=True)

    # Filename: use video ID for stable reruns and safe paths
    output_template = str(download_dir / "%(id)s.%(ext)s")

    # Step 1: Get metadata
    print(f"[下载] 获取视频信息...")
    if _download_proxy():
        print(f"[下载] 使用代理: {_download_proxy()}")
    info = _extract_metadata(url)

    title = info.get("title", "").strip()
    description = info.get("description", "").strip()
    video_id = info.get("id", "")
    channel_title = info.get("channel") or info.get("uploader", "") or ""

    if not video_id:
        raise RuntimeError("无法获取视频 ID")

    print(f"[下载] 标题: {title}")
    _reject_non_video_content(info)

    # Content filter via DeepSeek
    if config.CONTENT_FILTER_ENABLED:
        from yt2bili.translation.translator import classify_content
        print(f"[筛选] 检查内容相关性（关键词: {config.CONTENT_FILTER_KEYWORDS}）...")
        if not classify_content(title, description, config.CONTENT_FILTER_KEYWORDS, channel_title=channel_title):
            raise RuntimeError(
                f"内容筛选已跳过（与 {config.CONTENT_FILTER_KEYWORDS} 无关）: {title}"
            )
        print(f"[筛选] 内容相关，继续处理")

    # Reject vertical / Shorts videos before downloading
    if config.YOUTUBE_SKIP_VERTICAL_VIDEOS:
        best_w, best_h = 0, 0
        for fmt in (info.get("formats") or []):
            h = fmt.get("height")
            w = fmt.get("width")
            vcodec = fmt.get("vcodec")
            if h and w and vcodec != "none" and int(h) <= config.MAX_HEIGHT:
                if int(h) > best_h or (int(h) == best_h and int(w) > best_w):
                    best_h, best_w = int(h), int(w)
        if best_h == 0:
            best_w = info.get("width") or 0
            best_h = info.get("height") or 0
        if best_h > 0 and best_w > 0 and best_h > best_w:
            raise RuntimeError(
                f"检测到竖屏视频 {best_w}x{best_h}，已跳过"
            )

    # Reject videos longer than YOUTUBE_SKIP_LONG_VIDEO_MINUTES before downloading
    skip_long_minutes = config.YOUTUBE_SKIP_LONG_VIDEO_MINUTES
    if skip_long_minutes > 0:
        duration_seconds = info.get("duration") or 0
        if duration_seconds >= skip_long_minutes * 60:
            hours = duration_seconds / 3600
            raise RuntimeError(
                f"视频超过最大时长限制（{hours:.1f}h ≥ {skip_long_minutes} 分钟），已跳过"
            )

    available_resolution = _best_available_resolution(info)
    if available_resolution:
        print(f"[下载] 可用最高(≤{config.MAX_HEIGHT}p): {available_resolution}")

    # Step 2: Reuse local file when available, otherwise download
    actual_path = ""
    existing_path = _find_downloaded_video(download_dir, video_id)
    if existing_path:
        file_path = existing_path
        print(f"[下载] 检测到本地视频，跳过下载: {file_path.name}")
    else:
        print(f"[下载] 开始下载 (≤{config.MAX_HEIGHT}p)")
        actual_path = _download_with_progress(url, output_template, info=info)
        file_path = Path(actual_path) if actual_path else _find_downloaded_video(download_dir, video_id)

    # Sanity check
    if not file_path or not file_path.exists():
        raise RuntimeError(f"下载完成但找不到视频文件: {actual_path}")

    # Convert WebM to MP4 (Bilibili rejects VP9/WebM with HTTP 406)
    if file_path.suffix.lower() == ".webm":
        file_path = _convert_webm_to_mp4(file_path)

    # Find thumbnail (same stem as video file, different extension)
    thumbnail_path = ""
    for ext in (".jpg", ".jpeg", ".png", ".webp"):
        thumb = file_path.with_suffix(ext)
        if is_valid_image(thumb):
            thumbnail_path = str(thumb)
            break
    # Fallback: download thumbnail manually
    if not thumbnail_path:
        thumbnail_path = _download_thumbnail(video_id, download_dir) if video_id else ""

    file_size_mb = file_path.stat().st_size / 1024 / 1024
    print(f"[下载] ✅ 完成: {file_path.name} ({file_size_mb:.1f} MB)")
    probed_resolution = _probe_video_resolution(file_path)
    width = height = 0
    if probed_resolution:
        width, height = probed_resolution
        print(f"[下载] 实际分辨率: {width}x{height}")
    else:
        print("[下载] 实际分辨率: 未知（ffprobe 不可用或探测失败）")

    duration = _probe_video_duration(file_path)
    if duration > 0:
        print(f"[下载] 视频时长: {duration:.1f}s ({duration/3600:.2f}h)")
    else:
        print("[下载] 视频时长: 未知（ffprobe 不可用或探测失败）")

    return VideoInfo(
        file_path=str(file_path),
        title=title,
        description=description if description else "",
        original_url=url,
        video_id=video_id,
        thumbnail_path=thumbnail_path,
        width=width,
        height=height,
        duration=duration,
    )
