#!/usr/bin/env python3
"""
List recent videos from the authenticated user's YouTube subscriptions.

Default mode uses YouTube Data API + OAuth to read the real subscription list.
RSS mode can reuse a cached subscription list or a local channel list without
spending YouTube Data API quota.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Iterable

import requests
from dotenv import load_dotenv
import time as _time


from yt2bili.frozen_paths import user_data_dir

PROJECT_ROOT = user_data_dir()
YOUTUBE_READONLY_SCOPE = "https://www.googleapis.com/auth/youtube.readonly"
YOUTUBE_VIDEO_URL = "https://www.youtube.com/watch?v={video_id}"
YOUTUBE_RSS_URL = "https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"

# ── Retry configuration (env overridable) ──────────────────────────
_API_MAX_RETRIES = max(0, int(os.getenv("YOUTUBE_API_MAX_RETRIES", "3")))
_API_RETRY_BASE_DELAY = max(1.0, float(os.getenv("YOUTUBE_API_RETRY_DELAY", "2.0")))


class YouTubeNetworkError(Exception):
    """Raised when a YouTube API network request fails (transient, retryable)."""


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


@dataclass(frozen=True)
class Subscription:
    channel_id: str
    channel_title: str = ""


@dataclass(frozen=True)
class VideoItem:
    title: str
    channel_title: str
    published_at: str
    url: str
    channel_id: str
    video_id: str


def _env(key: str, default: str = "") -> str:
    return os.getenv(key, default)


def _env_int(key: str, default: int) -> int:
    value = _env(key, str(default)).strip()
    try:
        return int(value)
    except ValueError:
        return default


def _env_float(key: str, default: float) -> float:
    value = _env(key, str(default)).strip()
    try:
        return float(value)
    except ValueError:
        return default


def parse_datetime(value: str) -> datetime:
    """Parse an API/RSS timestamp into a timezone-aware datetime."""
    if not value:
        return datetime.min.replace(tzinfo=timezone.utc)
    try:
        normalized = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        dt = parsedate_to_datetime(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def normalize_published_at(value: str) -> str:
    return parse_datetime(value).isoformat().replace("+00:00", "Z")


def unique_by_video_id(videos: Iterable[VideoItem]) -> list[VideoItem]:
    seen: set[str] = set()
    unique: list[VideoItem] = []
    for video in videos:
        if video.video_id in seen:
            continue
        seen.add(video.video_id)
        unique.append(video)
    return unique


def sort_videos(videos: Iterable[VideoItem]) -> list[VideoItem]:
    sorted_items = sorted(videos, key=lambda item: parse_datetime(item.published_at), reverse=True)
    return unique_by_video_id(sorted_items)


def require_file(path: Path, purpose: str) -> None:
    if not path.exists():
        raise SystemExit(
            f"Missing {purpose}: {path}\n"
            f"Create it first or pass the correct path on the command line."
        )


def _is_token_expired(exc: BaseException) -> bool:
    """Check if an exception is caused by expired/revoked OAuth token."""
    text = str(exc).lower()
    return "invalid_grant" in text or "token has been expired" in text


def _api_network_error(exc: BaseException) -> YouTubeNetworkError:
    if _is_token_expired(exc):
        return YouTubeNetworkError(
            "YouTube OAuth token 已过期或被撤销。\n"
            f"原始错误: {exc}\n\n"
            "解决方法：\n"
            "1. 删除 config/youtube_token.json\n"
            "2. 重新运行 python main.py --monitor\n"
            "3. 浏览器会弹出 Google 授权页面，重新授权即可"
        )
    return YouTubeNetworkError(
        "YouTube Data API request failed due to a network/proxy timeout.\n"
        f"Original error: {exc}\n\n"
        "Try these checks:\n"
        "1. Make sure your proxy/VPN is running and can access googleapis.com.\n"
        "2. If needed, set YOUTUBE_PROXY in .env, for example:\n"
        "   YOUTUBE_PROXY=http://127.0.0.1:7897\n"
        "3. You can also increase YOUTUBE_HTTP_TIMEOUT, for example:\n"
        "   YOUTUBE_HTTP_TIMEOUT=120\n"
        "4. To avoid YouTube Data API network calls, use RSS mode with a cache or channel list."
    )


def build_requests_session() -> requests.Session:
    """Create a requests session that honors explicit YouTube proxy settings."""
    session = requests.Session()
    proxy_url = _env("YOUTUBE_PROXY", "").strip()
    if proxy_url:
        session.proxies.update({"http": proxy_url, "https": proxy_url})
    return session


def execute_youtube_request(request):
    """Execute a YouTube API request with retry on transient network errors."""
    last_error = None
    for attempt in range(_API_MAX_RETRIES + 1):
        try:
            return request.execute()
        except YouTubeNetworkError as exc:
            last_error = exc
            if attempt >= _API_MAX_RETRIES:
                raise
            delay = _API_RETRY_BASE_DELAY * (2 ** attempt)
            print(
                f"[API] 网络错误，{delay:.0f}s 后重试 "
                f"({attempt + 1}/{_API_MAX_RETRIES}): {exc}"
            )
            _time.sleep(delay)
        except (requests.exceptions.RequestException, TimeoutError, OSError) as exc:
            last_error = exc
            if attempt >= _API_MAX_RETRIES:
                raise _api_network_error(exc) from exc
            delay = _API_RETRY_BASE_DELAY * (2 ** attempt)
            print(
                f"[API] 网络错误，{delay:.0f}s 后重试 "
                f"({attempt + 1}/{_API_MAX_RETRIES}): {exc}"
            )
            _time.sleep(delay)
    raise _api_network_error(last_error) from last_error


class YouTubeRequest:
    def __init__(self, client: "YouTubeClient", endpoint: str, params: dict):
        self.client = client
        self.endpoint = endpoint
        self.params = params

    def execute(self) -> dict:
        return self.client.get(self.endpoint, self.params)


class YouTubeResource:
    def __init__(self, client: "YouTubeClient", endpoint: str):
        self.client = client
        self.endpoint = endpoint

    def list(self, **params) -> YouTubeRequest:
        return YouTubeRequest(self.client, self.endpoint, params)


class YouTubeClient:
    """Small requests-based YouTube Data API client.

    googleapiclient uses httplib2, which can fail with some local proxy setups.
    This client keeps OAuth but sends API requests through requests instead.
    """

    BASE_URL = "https://www.googleapis.com/youtube/v3"

    def __init__(self, creds, session: requests.Session | None = None):
        from google.auth.transport.requests import Request as GoogleAuthRequest

        self.creds = creds
        self.session = session or build_requests_session()
        self.google_auth_request = GoogleAuthRequest(session=self.session)
        self.timeout = _env_float("YOUTUBE_HTTP_TIMEOUT", 60.0)

    def subscriptions(self) -> YouTubeResource:
        return YouTubeResource(self, "subscriptions")

    def channels(self) -> YouTubeResource:
        return YouTubeResource(self, "channels")

    def playlistItems(self) -> YouTubeResource:
        return YouTubeResource(self, "playlistItems")

    def videos(self) -> YouTubeResource:
        return YouTubeResource(self, "videos")

    def _ensure_valid_token(self) -> None:
        if self.creds.expired and self.creds.refresh_token:
            try:
                self.creds.refresh(self.google_auth_request)
                return
            except Exception as exc:
                if _is_token_expired(exc):
                    raise _api_network_error(exc) from exc
                raise _api_network_error(exc) from exc

    def get(self, endpoint: str, params: dict) -> dict:
        last_error = None
        for attempt in range(_API_MAX_RETRIES + 1):
            try:
                self._ensure_valid_token()
            except YouTubeNetworkError as exc:
                last_error = exc
                if attempt >= _API_MAX_RETRIES:
                    raise
                delay = _API_RETRY_BASE_DELAY * (2 ** attempt)
                print(
                    f"[API] Token 刷新失败，{delay:.0f}s 后重试 "
                    f"({attempt + 1}/{_API_MAX_RETRIES})"
                )
                _time.sleep(delay)
                continue

            url = f"{self.BASE_URL}/{endpoint}"
            headers = {"Authorization": f"Bearer {self.creds.token}"}
            try:
                response = self.session.get(url, params=params, headers=headers, timeout=self.timeout)
                response.raise_for_status()
                return response.json()
            except requests.exceptions.RequestException as exc:
                if getattr(exc, "response", None) is not None:
                    http_err = _api_http_error(exc.response)
                    if isinstance(http_err, YouTubeNetworkError):
                        # 5xx server error — retryable
                        last_error = http_err
                        if attempt >= _API_MAX_RETRIES:
                            raise http_err
                        delay = _API_RETRY_BASE_DELAY * (2 ** attempt)
                        print(
                            f"[API] 服务器错误 (HTTP {exc.response.status_code})，"
                            f"{delay:.0f}s 后重试 ({attempt + 1}/{_API_MAX_RETRIES})"
                        )
                        _time.sleep(delay)
                        continue
                    raise http_err  # 4xx — not retryable
                last_error = exc
                if attempt >= _API_MAX_RETRIES:
                    raise _api_network_error(exc) from exc
                delay = _API_RETRY_BASE_DELAY * (2 ** attempt)
                print(
                    f"[API] 请求网络错误，{delay:.0f}s 后重试 "
                    f"({attempt + 1}/{_API_MAX_RETRIES}): {exc}"
                )
                _time.sleep(delay)
        raise _api_network_error(last_error) from last_error


def _api_http_error(response: requests.Response) -> Exception:
    try:
        payload = response.json()
    except ValueError:
        payload = response.text

    message = (
        f"YouTube Data API request failed: HTTP {response.status_code}\n"
        f"{json.dumps(payload, ensure_ascii=False, indent=2) if isinstance(payload, dict) else payload}"
    )
    # 5xx errors are transient — retryable
    if response.status_code >= 500:
        return YouTubeNetworkError(message)
    return SystemExit(message)


def get_youtube_service(client_secret_file: Path, token_file: Path):
    """Create an authorized YouTube Data API service."""
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError as exc:
        raise SystemExit(
            "Missing Google API dependencies. Run: pip install -r requirements.txt"
        ) from exc

    creds = None
    if token_file.exists():
        creds = Credentials.from_authorized_user_file(str(token_file), [YOUTUBE_READONLY_SCOPE])

    session = build_requests_session()
    auth_request = Request(session=session)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(auth_request)
            except Exception as exc:
                if _is_token_expired(exc):
                    # Token expired/revoked — delete old token and re-auth from scratch
                    print("[API] Token 已过期，删除旧 token 并重新授权...")
                    token_file.unlink(missing_ok=True)
                    creds = None
                else:
                    raise _api_network_error(exc) from exc

        if not creds or not creds.refresh_token:
            require_file(client_secret_file, "OAuth client secret file")
            flow = InstalledAppFlow.from_client_secrets_file(
                str(client_secret_file), [YOUTUBE_READONLY_SCOPE]
            )
            # Apply YOUTUBE_PROXY to OAuth token-exchange requests
            # (googleapis.com is blocked without proxy in some regions)
            proxy_url = _env("YOUTUBE_PROXY", "").strip()
            _prev_http = os.environ.get("HTTP_PROXY")
            _prev_https = os.environ.get("HTTPS_PROXY")
            try:
                if proxy_url:
                    os.environ["HTTP_PROXY"] = proxy_url
                    os.environ["HTTPS_PROXY"] = proxy_url
                creds = flow.run_local_server(port=0)
            finally:
                if proxy_url:
                    if _prev_http is not None:
                        os.environ["HTTP_PROXY"] = _prev_http
                    else:
                        os.environ.pop("HTTP_PROXY", None)
                    if _prev_https is not None:
                        os.environ["HTTPS_PROXY"] = _prev_https
                    else:
                        os.environ.pop("HTTPS_PROXY", None)
        token_file.write_text(creds.to_json(), encoding="utf-8")

    return YouTubeClient(creds, session=session)


def fetch_subscriptions_api(youtube) -> list[Subscription]:
    """Read the authenticated user's subscription channels."""
    subscriptions: list[Subscription] = []
    page_token = None

    while True:
        request = youtube.subscriptions().list(
            part="snippet",
            mine=True,
            maxResults=50,
            pageToken=page_token,
        )
        response = execute_youtube_request(request)

        for item in response.get("items", []):
            snippet = item.get("snippet", {})
            resource = snippet.get("resourceId", {})
            channel_id = resource.get("channelId", "")
            if not channel_id:
                continue
            subscriptions.append(
                Subscription(
                    channel_id=channel_id,
                    channel_title=snippet.get("title", ""),
                )
            )

        page_token = response.get("nextPageToken")
        if not page_token:
            break

    return subscriptions


def chunked(items: list[str], size: int) -> Iterable[list[str]]:
    for index in range(0, len(items), size):
        yield items[index:index + size]


def fetch_upload_playlists(youtube, subscriptions: list[Subscription]) -> dict[str, dict[str, str]]:
    """Map channel IDs to title and uploads playlist ID."""
    by_id = {sub.channel_id: sub.channel_title for sub in subscriptions}
    result: dict[str, dict[str, str]] = {}

    for chunk in chunked(list(by_id.keys()), 50):
        request = youtube.channels().list(
            part="contentDetails,snippet",
            id=",".join(chunk),
            maxResults=50,
        )
        response = execute_youtube_request(request)

        for item in response.get("items", []):
            channel_id = item.get("id", "")
            snippet = item.get("snippet", {})
            uploads = (
                item.get("contentDetails", {})
                .get("relatedPlaylists", {})
                .get("uploads", "")
            )
            if channel_id and uploads:
                result[channel_id] = {
                    "channel_title": snippet.get("title") or by_id.get(channel_id, ""),
                    "uploads_playlist_id": uploads,
                }

    return result


def fetch_recent_videos_api(
    youtube,
    subscriptions: list[Subscription],
    max_videos_per_channel: int,
) -> list[VideoItem]:
    """Fetch recent videos from each subscribed channel's uploads playlist."""
    playlists = fetch_upload_playlists(youtube, subscriptions)
    max_results = max(1, min(max_videos_per_channel, 50))
    videos: list[VideoItem] = []

    for channel_id, info in playlists.items():
        request = youtube.playlistItems().list(
            part="snippet,contentDetails",
            playlistId=info["uploads_playlist_id"],
            maxResults=max_results,
        )
        response = execute_youtube_request(request)

        for item in response.get("items", []):
            snippet = item.get("snippet", {})
            content = item.get("contentDetails", {})
            video_id = content.get("videoId") or snippet.get("resourceId", {}).get("videoId", "")
            if not video_id:
                continue
            published_at = content.get("videoPublishedAt") or snippet.get("publishedAt", "")
            videos.append(
                VideoItem(
                    title=snippet.get("title", ""),
                    channel_title=info["channel_title"],
                    published_at=normalize_published_at(published_at),
                    url=YOUTUBE_VIDEO_URL.format(video_id=video_id),
                    channel_id=channel_id,
                    video_id=video_id,
                )
            )

    return sort_videos(videos)


def save_subscriptions_cache(path: Path, subscriptions: list[Subscription]) -> None:
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "subscriptions": [asdict(sub) for sub in subscriptions],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_subscriptions_cache(path: Path) -> list[Subscription]:
    require_file(path, "subscription cache")
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        raw_subscriptions = data
    else:
        raw_subscriptions = data.get("subscriptions", [])
    subscriptions: list[Subscription] = []
    for item in raw_subscriptions:
        channel_id = item.get("channel_id") or item.get("channelId") or item.get("id")
        if not channel_id:
            continue
        subscriptions.append(
            Subscription(
                channel_id=channel_id,
                channel_title=item.get("channel_title") or item.get("channelTitle") or item.get("title", ""),
            )
        )
    return subscriptions


def load_channels_file(path: Path) -> list[Subscription]:
    """Load channel IDs from a JSON or text file."""
    require_file(path, "channels file")
    if path.suffix.lower() == ".json":
        return load_subscriptions_cache(path)

    subscriptions: list[Subscription] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "," in stripped:
            channel_id, channel_title = stripped.split(",", 1)
        elif "\t" in stripped:
            channel_id, channel_title = stripped.split("\t", 1)
        else:
            channel_id, channel_title = stripped, ""
        subscriptions.append(Subscription(channel_id=channel_id.strip(), channel_title=channel_title.strip()))
    return subscriptions


def fetch_recent_videos_rss(
    subscriptions: list[Subscription],
    max_videos_per_channel: int,
) -> list[VideoItem]:
    try:
        import feedparser
    except ImportError as exc:
        raise SystemExit("Missing RSS dependency. Run: pip install -r requirements.txt") from exc

    videos: list[VideoItem] = []
    max_results = max(1, max_videos_per_channel)
    session = build_requests_session()
    timeout = _env_float("YOUTUBE_HTTP_TIMEOUT", 60.0)

    for sub in subscriptions:
        feed_url = YOUTUBE_RSS_URL.format(channel_id=sub.channel_id)
        last_error = None
        response = None
        for attempt in range(_API_MAX_RETRIES + 1):
            try:
                response = session.get(feed_url, timeout=timeout)
                response.raise_for_status()
                break
            except requests.exceptions.RequestException as exc:
                last_error = exc
                if attempt >= _API_MAX_RETRIES:
                    raise _api_network_error(exc) from exc
                delay = _API_RETRY_BASE_DELAY * (2 ** attempt)
                print(
                    f"[RSS] 网络错误，{delay:.0f}s 后重试 "
                    f"({attempt + 1}/{_API_MAX_RETRIES}): {exc}"
                )
                _time.sleep(delay)

        feed = feedparser.parse(response.content)
        channel_title = sub.channel_title or getattr(feed.feed, "title", "")

        for entry in feed.entries[:max_results]:
            video_id = (
                getattr(entry, "yt_videoid", "")
                or getattr(entry, "id", "").rsplit(":", 1)[-1]
            )
            if not video_id:
                continue
            published = getattr(entry, "published", "") or getattr(entry, "updated", "")
            videos.append(
                VideoItem(
                    title=getattr(entry, "title", ""),
                    channel_title=channel_title,
                    published_at=normalize_published_at(published),
                    url=YOUTUBE_VIDEO_URL.format(video_id=video_id),
                    channel_id=sub.channel_id,
                    video_id=video_id,
                )
            )

    return sort_videos(videos)


def format_table(videos: list[VideoItem]) -> str:
    columns = ["published_at", "channel_title", "title", "url"]
    rows = [[getattr(video, column) for column in columns] for video in videos]
    widths = [len(column) for column in columns]
    for row in rows:
        for index, value in enumerate(row):
            widths[index] = min(max(widths[index], len(value)), 80)

    def truncate(value: str, width: int) -> str:
        if len(value) <= width:
            return value
        return value[:max(0, width - 3)] + "..."

    lines = []
    header = " | ".join(column.ljust(widths[index]) for index, column in enumerate(columns))
    lines.append(header)
    lines.append("-+-".join("-" * width for width in widths))
    for row in rows:
        lines.append(" | ".join(truncate(value, widths[index]).ljust(widths[index]) for index, value in enumerate(row)))
    return "\n".join(lines)


def write_output(videos: list[VideoItem], output_format: str, output_file: Path | None) -> None:
    data = [asdict(video) for video in videos]
    if output_file and output_file.parent != Path("."):
        output_file.parent.mkdir(parents=True, exist_ok=True)

    if output_format == "json":
        text = json.dumps(data, ensure_ascii=False, indent=2)
        if output_file:
            output_file.write_text(text + "\n", encoding="utf-8")
        else:
            print(text)
        return

    if output_format == "csv":
        fieldnames = ["title", "channel_title", "published_at", "url", "channel_id", "video_id"]
        if output_file:
            with output_file.open("w", encoding="utf-8-sig", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(data)
        else:
            writer = csv.DictWriter(sys.stdout, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(data)
        return

    table = format_table(videos)
    if output_file:
        output_file.write_text(table + "\n", encoding="utf-8")
    else:
        print(table)


def build_parser() -> argparse.ArgumentParser:
    load_dotenv(PROJECT_ROOT / "config" / ".env")

    parser = argparse.ArgumentParser(
        description="List recent videos from YouTube subscriptions.",
    )
    parser.add_argument("--source", choices=("api", "rss"), default="api")
    parser.add_argument("--limit", type=int, default=50, help="Maximum videos to output after sorting.")
    parser.add_argument(
        "--max-videos-per-channel",
        type=int,
        default=_env_int("YOUTUBE_MAX_VIDEOS_PER_CHANNEL", 5),
    )
    parser.add_argument(
        "--output-format",
        choices=("table", "json", "csv"),
        default=_env("YOUTUBE_OUTPUT_FORMAT", "table"),
    )
    parser.add_argument("--output", type=Path, default=None, help="Optional output file path.")
    parser.add_argument(
        "--client-secret-file",
        type=Path,
        default=Path(_env("YOUTUBE_CLIENT_SECRET_FILE", "config/client_secret.json")),
    )
    parser.add_argument(
        "--token-file",
        type=Path,
        default=Path(_env("YOUTUBE_TOKEN_FILE", "config/youtube_token.json")),
    )
    parser.add_argument(
        "--cache-file",
        type=Path,
        default=Path(_env("YOUTUBE_SUBSCRIPTIONS_CACHE", "config/subscriptions_cache.json")),
    )
    parser.add_argument(
        "--channels-file",
        type=Path,
        default=None,
        help="Local channel list for RSS mode. Text lines can be channel_id or channel_id,title.",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()

    if args.limit <= 0:
        raise SystemExit("--limit must be positive")
    if args.max_videos_per_channel <= 0:
        raise SystemExit("--max-videos-per-channel must be positive")
    if args.output_format not in ("table", "json", "csv"):
        raise SystemExit("YOUTUBE_OUTPUT_FORMAT / --output-format must be table, json, or csv")

    if args.source == "api":
        youtube = get_youtube_service(args.client_secret_file, args.token_file)
        subscriptions = fetch_subscriptions_api(youtube)
        save_subscriptions_cache(args.cache_file, subscriptions)
        videos = fetch_recent_videos_api(youtube, subscriptions, args.max_videos_per_channel)
    else:
        if args.channels_file:
            subscriptions = load_channels_file(args.channels_file)
        else:
            subscriptions = load_subscriptions_cache(args.cache_file)
        videos = fetch_recent_videos_rss(subscriptions, args.max_videos_per_channel)

    videos = sort_videos(videos)[:args.limit]
    write_output(videos, args.output_format, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
