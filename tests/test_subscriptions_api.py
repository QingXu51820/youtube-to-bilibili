"""自测：订阅 API 抓取侧（service 调用、分页、RSS、缓存、重试）。"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from yt2bili.youtube import subscriptions as subs


class ChunkedTests(unittest.TestCase):
    def test_chunks_by_size(self):
        self.assertEqual(list(subs.chunked([1, 2, 3, 4, 5], 2)), [[1, 2], [3, 4], [5]])

    def test_empty(self):
        self.assertEqual(list(subs.chunked([], 50)), [])


class RequireFileTests(unittest.TestCase):
    def test_missing_raises_system_exit(self):
        with self.assertRaises(SystemExit):
            subs.require_file(Path("no/such/file"), "test file")

    def test_existing_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "f.json"
            p.write_text("{}", encoding="utf-8")
            subs.require_file(p, "test file")  # 不抛即通过


class ExecuteYouTubeRequestTests(unittest.TestCase):
    """execute_youtube_request：成功直接返回，瞬态错误重试。"""

    def setUp(self):
        self.sleep_patcher = patch.object(subs._time, "sleep")
        self.sleep_patcher.start()
        self.addCleanup(self.sleep_patcher.stop)

    def test_success_returns_directly(self):
        request = Mock()
        request.execute.return_value = {"items": []}
        self.assertEqual(subs.execute_youtube_request(request), {"items": []})

    def test_network_error_retried_then_success(self):
        request = Mock()
        request.execute.side_effect = [
            subs.YouTubeNetworkError("timeout"),
            {"items": ["ok"]},
        ]
        self.assertEqual(subs.execute_youtube_request(request), {"items": ["ok"]})
        self.assertEqual(request.execute.call_count, 2)

    def test_request_exception_retried_then_success(self):
        import requests
        request = Mock()
        request.execute.side_effect = [
            requests.Timeout("t"),
            {"items": ["ok"]},
        ]
        self.assertEqual(subs.execute_youtube_request(request), {"items": ["ok"]})

    def test_network_error_exhausted_raises(self):
        import requests
        request = Mock()
        request.execute.side_effect = requests.Timeout("t")
        with self.assertRaises(subs.YouTubeNetworkError) as ctx:
            subs.execute_youtube_request(request)
        self.assertIn("network", str(ctx.exception))

    def test_token_expired_message(self):
        import requests
        request = Mock()
        request.execute.side_effect = requests.ConnectionError("invalid_grant")
        with self.assertRaises(subs.YouTubeNetworkError) as ctx:
            subs.execute_youtube_request(request)
        self.assertIn("OAuth token 已过期", str(ctx.exception))


class ResolveChannelHandleApiTests(unittest.TestCase):
    """resolve_channel_handle_api：@handle 与 channel URL 两种解析。"""

    def _youtube(self, items):
        request = Mock()
        request.execute.return_value = {"items": items}
        youtube = Mock()
        youtube.channels.return_value.list.return_value = request
        return youtube, request

    def test_at_handle(self):
        youtube, request = self._youtube(
            [{"id": "UC1", "snippet": {"title": "Chan"}}])
        result = subs.resolve_channel_handle_api(youtube, "@MarvelSnap")
        self.assertEqual(result, ("UC1", "Chan"))
        self.assertEqual(
            youtube.channels.return_value.list.call_args.kwargs["forHandle"],
            "MarvelSnap")

    def test_channel_url(self):
        youtube, request = self._youtube(
            [{"id": "UC1", "snippet": {"title": "Chan"}}])
        result = subs.resolve_channel_handle_api(
            youtube, "https://youtube.com/channel/UC1/extra")
        self.assertEqual(result, ("UC1", "Chan"))
        self.assertEqual(
            youtube.channels.return_value.list.call_args.kwargs["id"], "UC1")

    def test_unresolvable_raises(self):
        youtube, _ = self._youtube([])
        with self.assertRaises(ValueError):
            subs.resolve_channel_handle_api(youtube, "@Ghost")


class FetchSubscriptionsApiTests(unittest.TestCase):
    """fetch_subscriptions_api：分页拉取订阅频道。"""

    def test_paginates_and_skips_missing_channel_id(self):
        page1 = {"items": [
            {"snippet": {"title": "A", "resourceId": {"channelId": "UC1"}}},
            {"snippet": {"title": "NoId"}},  # 无 channelId → 跳过
        ], "nextPageToken": "tok2"}
        page2 = {"items": [
            {"snippet": {"title": "B", "resourceId": {"channelId": "UC2"}}},
        ]}
        request = Mock()
        request.execute.side_effect = [page1, page2]
        youtube = Mock()
        youtube.subscriptions.return_value.list.return_value = request
        result = subs.fetch_subscriptions_api(youtube)
        self.assertEqual([s.channel_id for s in result], ["UC1", "UC2"])
        list_mock = youtube.subscriptions.return_value.list
        self.assertEqual(list_mock.call_count, 2)
        # 第二页带上了 pageToken
        self.assertEqual(list_mock.call_args.kwargs["pageToken"], "tok2")


class FetchUploadPlaylistsTests(unittest.TestCase):
    def test_maps_channel_to_uploads_id(self):
        request = Mock()
        request.execute.return_value = {"items": [
            {"id": "UC1",
             "snippet": {"title": "RealTitle"},
             "contentDetails": {"relatedPlaylists": {"uploads": "UU1"}}},
            {"id": "UC2",  # 无 uploads → 跳过
             "snippet": {"title": "B"}},
        ]}
        youtube = Mock()
        youtube.channels.return_value.list.return_value = request
        subs_list = [subs.Subscription("UC1", "FallbackTitle"),
                     subs.Subscription("UC2", "B")]
        result = subs.fetch_upload_playlists(youtube, subs_list)
        self.assertEqual(result["UC1"]["uploads_playlist_id"], "UU1")
        self.assertEqual(result["UC1"]["channel_title"], "RealTitle")
        self.assertNotIn("UC2", result)


class FetchRecentVideosApiTests(unittest.TestCase):
    def test_builds_video_items(self):
        request = Mock()
        request.execute.return_value = {"items": [
            {"snippet": {"title": "V1", "publishedAt": "2026-01-02T00:00:00Z",
                         "resourceId": {"videoId": "vid1"}},
             "contentDetails": {"videoId": "vid1", "videoPublishedAt": "2026-01-02T00:00:00Z"}},
            {"snippet": {"title": "V2"},
             "contentDetails": {"videoId": "vid2", "videoPublishedAt": "2026-01-03T00:00:00Z"}},
        ]}
        youtube = Mock()
        youtube.playlistItems.return_value.list.return_value = request
        uploads = {"UC1": {"channel_title": "Chan", "uploads_playlist_id": "UU1"}}
        with patch.object(subs, "fetch_upload_playlists", return_value=uploads):
            videos = subs.fetch_recent_videos_api(youtube, [], max_videos_per_channel=5)
        self.assertEqual(len(videos), 2)
        self.assertEqual(videos[0].title, "V2")  # 最新在前
        self.assertEqual(videos[0].channel_title, "Chan")
        self.assertIn("vid2", videos[0].url)
        self.assertEqual(
            youtube.playlistItems.return_value.list.call_args.kwargs["playlistId"],
            "UU1")


class SaveSubscriptionsCacheTests(unittest.TestCase):
    def test_writes_generated_at_and_subscriptions(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cache.json"
            subs.save_subscriptions_cache(
                path, [subs.Subscription("UC1", "Chan")])
            data = json.loads(path.read_text(encoding="utf-8"))
        self.assertIn("generated_at", data)
        self.assertEqual(data["subscriptions"][0]["channel_id"], "UC1")
        self.assertEqual(data["subscriptions"][0]["channel_title"], "Chan")


class FetchRecentVideosRssTests(unittest.TestCase):
    """fetch_recent_videos_rss：RSS 抓取、404 死频道、网络错误重试。"""

    def _feed(self, entries, title="ChanTitle"):
        return SimpleNamespace(
            feed=SimpleNamespace(title=title),
            entries=entries,
        )

    def _entry(self, video_id, title, published):
        return SimpleNamespace(
            yt_videoid=video_id, title=title, published=published, id=f"yt:video:{video_id}",
        )

    def _session(self, response=None, *, side_effect=None):
        session = Mock()
        if side_effect is not None:
            session.get.side_effect = side_effect
        else:
            session.get.return_value = response
        return session

    def test_parses_entries(self):
        feed = self._feed([
            self._entry("v1", "First", "2026-01-02T00:00:00Z"),
            self._entry("v2", "Second", "2026-01-03T00:00:00Z"),
        ])
        resp = SimpleNamespace(status_code=200, raise_for_status=lambda: None,
                               content=b"<xml/>")
        session = self._session(resp)
        with patch.object(subs, "build_requests_session", return_value=session), \
             patch("feedparser.parse", return_value=feed):
            videos = subs.fetch_recent_videos_rss(
                [subs.Subscription("UC1", "")], max_videos_per_channel=10)
        self.assertEqual([v.video_id for v in videos], ["v2", "v1"])  # 最新在前
        self.assertEqual(videos[0].channel_title, "ChanTitle")  # feed 标题优先
        self.assertIn("v2", videos[0].url)

    def test_404_channel_recorded_dead(self):
        def raise_404(*a, **k):
            resp = SimpleNamespace(status_code=404)
            raise __import__("requests").exceptions.HTTPError("not found", response=resp)
        session = self._session(side_effect=raise_404)
        dead, failed = [], []
        with patch.object(subs, "build_requests_session", return_value=session):
            videos = subs.fetch_recent_videos_rss(
                [subs.Subscription("UC1", "Dead")], max_videos_per_channel=5,
                dead_channels=dead, failed_channels=failed)
        self.assertEqual(videos, [])
        self.assertEqual([d.channel_id for d in dead], ["UC1"])
        self.assertEqual(failed, [])

    def test_network_error_recorded_failed(self):
        import requests
        session = self._session(side_effect=requests.ConnectionError("down"))
        dead, failed = [], []
        with patch.object(subs, "build_requests_session", return_value=session), \
             patch.object(subs._time, "sleep"):
            videos = subs.fetch_recent_videos_rss(
                [subs.Subscription("UC1", "Flaky")], max_videos_per_channel=5,
                dead_channels=dead, failed_channels=failed)
        self.assertEqual(videos, [])
        self.assertEqual(dead, [])
        self.assertEqual([f.channel_id for f in failed], ["UC1"])

    def test_transient_error_retried_then_success(self):
        import requests
        resp = SimpleNamespace(status_code=200, raise_for_status=lambda: None,
                               content=b"<xml/>")
        feed = self._feed([self._entry("v1", "Ok", "2026-01-01T00:00:00Z")])
        session = Mock()
        session.get.side_effect = [requests.Timeout("t"), resp]
        with patch.object(subs, "build_requests_session", return_value=session), \
             patch.object(subs._time, "sleep"), \
             patch("feedparser.parse", return_value=feed):
            videos = subs.fetch_recent_videos_rss(
                [subs.Subscription("UC1", "C")], max_videos_per_channel=5)
        self.assertEqual(len(videos), 1)
        self.assertEqual(session.get.call_count, 2)


if __name__ == "__main__":
    unittest.main()
