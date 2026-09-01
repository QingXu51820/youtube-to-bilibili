"""自测：B站合集匹配/映射/请求构造纯逻辑 + 占位封面。"""

import asyncio
import base64
import contextlib
import io
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from PIL import Image

from yt2bili import config
from yt2bili import profile as profile_mod
import yt2bili.bilibili.collection as collection_mod
from yt2bili.bilibili.collection import (
    BilibiliApiError,
    ChannelCollectionMatch,
    CollectionInfo,
    _check_response,
    add_uploaded_video_to_collection,
    add_video_to_collection,
    build_episodes,
    create_collection,
    ensure_collection,
    fetch_video_pages,
    find_collection_by_name,
    list_collections,
    make_placeholder_cover,
    normalize_name,
    resolve_channel_collections,
    sync_list_collections,
    upload_cover,
)


class NormalizeNameTests(unittest.TestCase):
    def test_lowercases_and_strips_non_alnum(self):
        self.assertEqual(normalize_name("Bynx_Plays"), "bynxplays")
        self.assertEqual(normalize_name("KM Best: In A Snap"), "kmbestinasnap")

    def test_keeps_cjk(self):
        self.assertEqual(normalize_name("其客座遊戲"), "其客座遊戲")

    def test_empty(self):
        self.assertEqual(normalize_name(""), "")
        self.assertEqual(normalize_name(None), "")


class FindCollectionTests(unittest.TestCase):
    def _collections(self):
        return [
            CollectionInfo(season_id=1, title="Bynx", section_id=11, total=3),
            CollectionInfo(season_id=2, title="其客座遊戲", section_id=22, total=4),
        ]

    def test_finds_case_insensitive(self):
        found = find_collection_by_name(self._collections(), "bynx")
        self.assertIsNotNone(found)
        self.assertEqual(found.season_id, 1)

    def test_missing_returns_none(self):
        self.assertIsNone(find_collection_by_name(self._collections(), "MarvelSnap"))


class ResolveChannelCollectionsTests(unittest.TestCase):
    def _collections(self):
        return [
            CollectionInfo(season_id=10, title="Bynx", section_id=11),
            CollectionInfo(season_id=20, title="Judgments", section_id=21),
        ]

    def test_matched_and_to_create(self):
        pairs = [
            ("Bynx_Plays", "Bynx"),
            ("Snap Judgments", "Judgments"),
            ("MarvelSnap", "MarvelSnap"),
            ("notmydance", ""),
        ]
        result = resolve_channel_collections(pairs, self._collections())
        self.assertEqual(
            [(m.channel_title, m.status, m.season_id) for m in result],
            [
                ("Bynx_Plays", "matched", 10),
                ("Snap Judgments", "matched", 20),
                ("MarvelSnap", "to_create", None),
                ("notmydance", "to_create", None),
            ],
        )
        self.assertEqual(result[3].collection_name, "notmydance")


class BuildEpisodesTests(unittest.TestCase):
    def test_uses_part_title_overrides(self):
        pages = [{"cid": 1, "part": "P1"}, {"cid": 2, "part": "P2"}]
        eps = build_episodes(123, pages, ["标题1", "标题2"])
        self.assertEqual(eps, [
            {"aid": 123, "cid": 1, "title": "标题1", "charging_pay": 0},
            {"aid": 123, "cid": 2, "title": "标题2", "charging_pay": 0},
        ])

    def test_falls_back_to_part_names(self):
        pages = [{"cid": 7, "part": "正片"}]
        eps = build_episodes(9, pages)
        self.assertEqual(eps[0]["title"], "正片")
        self.assertEqual(eps[0]["cid"], 7)

    def test_fewer_overrides_than_pages(self):
        pages = [{"cid": 1, "part": "P1"}, {"cid": 2, "part": "P2"}]
        eps = build_episodes(5, pages, ["只有P1"])
        self.assertEqual([e["title"] for e in eps], ["只有P1", "P2"])


class PlaceholderCoverTests(unittest.TestCase):
    def test_creates_valid_1920x1080_jpeg(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(make_placeholder_cover("MarvelSnap"))
            self.assertTrue(path.exists())
            with Image.open(path) as img:
                self.assertEqual(img.size, (1920, 1080))


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code

    def json(self):
        return self.payload


class FakeAsyncClient:
    """Minimal httpx.AsyncClient stand-in that records calls."""

    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get(self, url, **kwargs):
        self.calls.append(("get", url, kwargs))
        result = self.handler("get", url, kwargs)
        if asyncio.iscoroutine(result):
            return await result
        return result

    async def post(self, url, **kwargs):
        self.calls.append(("post", url, kwargs))
        result = self.handler("post", url, kwargs)
        if asyncio.iscoroutine(result):
            return await result
        return result


def _credential():
    return SimpleNamespace(sessdata="sess", bili_jct="jct", buvid3="b3")


class CheckResponseTests(unittest.TestCase):
    def test_auth_error_raises_relogin_hint(self):
        for status in (401, 403):
            with self.assertRaises(RuntimeError) as ctx:
                _check_response(FakeResponse({}, status_code=status), "合集")
            self.assertIn("重新扫码登录", str(ctx.exception))

    def test_nonzero_code_raises(self):
        with self.assertRaises(RuntimeError) as ctx:
            _check_response(FakeResponse({"code": -400, "message": "bad"}), "创建合集")
        self.assertIn("创建合集", str(ctx.exception))
        self.assertIn("bad", str(ctx.exception))

    def test_ok_returns_data(self):
        data = _check_response(FakeResponse({"code": 0, "data": {"url": "x"}}), "上传封面")
        self.assertEqual(data["data"]["url"], "x")


class PendingCollectionQueueTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.queue = Path(self._tmp.name) / "pending_collections.json"

    def test_enqueue_writes_entry(self):
        with patch.object(collection_mod, "pending_collections_path",
                          return_value=self.queue):
            collection_mod.enqueue_collection(
                collection="Bynx", bvid="BV1", aid=1,
                video_id="vid1", channel_title="Chan",
            )
        entries = json.loads(self.queue.read_text(encoding="utf-8"))
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["collection_name"], "Bynx")
        self.assertEqual(entries[0]["bvid"], "BV1")
        self.assertEqual(entries[0]["status"], "pending")
        self.assertEqual(entries[0]["video_id"], "vid1")

    def test_enqueue_upserts_by_video_id(self):
        with patch.object(collection_mod, "pending_collections_path",
                          return_value=self.queue):
            collection_mod.enqueue_collection(
                collection="A", bvid="BV1", aid=1, video_id="v1"
            )
            collection_mod.enqueue_collection(
                collection="B", bvid="BV2", aid=2, video_id="v1"
            )
        entries = json.loads(self.queue.read_text(encoding="utf-8"))
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["collection_name"], "B")

    def test_enqueue_skips_when_already_added(self):
        self.queue.write_text(json.dumps([
            {"video_id": "v1", "status": "added", "bvid": "BV1",
             "collection_name": "Bynx"},
        ]), encoding="utf-8")
        with patch.object(collection_mod, "pending_collections_path",
                          return_value=self.queue):
            collection_mod.enqueue_collection(
                collection="Bynx", bvid="BV1", aid=1, video_id="v1"
            )
        entries = json.loads(self.queue.read_text(encoding="utf-8"))
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["status"], "added")

    def test_load_corrupt_backs_up_and_returns_empty(self):
        self.queue.write_text("{broken", encoding="utf-8")
        entries = collection_mod.load_pending_collections(self.queue)
        self.assertEqual(entries, [])
        self.assertTrue(self.queue.with_suffix(".json.bak").exists())

    def test_pending_collections_path_legacy_and_profile(self):
        with patch.object(profile_mod, "is_profile_state_active",
                          return_value=False), \
             patch.object(config, "PROJECT_ROOT", Path("/proj")):
            self.assertEqual(
                collection_mod.pending_collections_path(),
                Path("/proj/state/pending_collections.json"),
            )
        with patch.object(profile_mod, "is_profile_state_active",
                          return_value=True), \
             patch.object(profile_mod, "get_active_profile_name",
                          return_value="snap"), \
             patch.object(config, "PROJECT_ROOT", Path("/proj")):
            self.assertEqual(
                collection_mod.pending_collections_path(),
                Path("/proj/state/snap/pending_collections.json"),
            )


class BackfillCollectionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.queue = Path(self._tmp.name) / "pending_collections.json"
        self.state = Path(self._tmp.name) / "processed_videos.json"

    def _write_state(self, videos):
        self.state.write_text(
            json.dumps({"videos": videos}, ensure_ascii=False),
            encoding="utf-8",
        )

    def test_backfills_uploaded_videos_only(self):
        self._write_state({
            "v1": {"status": "uploaded", "bvid": "BV1", "aid": 1,
                   "channel_title": "Bynx_Plays", "title": "T1"},
            "v2": {"status": "failed", "bvid": "BV2", "aid": 2,
                   "channel_title": "Bynx_Plays", "title": "T2"},
        })
        n = collection_mod.backfill_collections(
            self.queue, self.state,
            resolve_collection_name=lambda title: "Bynx",
        )
        self.assertEqual(n, 1)
        entries = json.loads(self.queue.read_text(encoding="utf-8"))
        self.assertEqual([e["video_id"] for e in entries], ["v1"])
        self.assertEqual(entries[0]["collection_name"], "Bynx")
        self.assertEqual(entries[0]["status"], "pending")

    def test_skips_queued_and_missing_bvid(self):
        self._write_state({
            "v1": {"status": "uploaded", "bvid": "BV1",
                   "channel_title": "A", "title": "T1"},
            "v2": {"status": "uploaded", "bvid": "",
                   "channel_title": "A", "title": "T2"},
        })
        self.queue.write_text(json.dumps([
            {"video_id": "v1", "status": "pending", "bvid": "BV1"},
        ]), encoding="utf-8")
        n = collection_mod.backfill_collections(
            self.queue, self.state,
            resolve_collection_name=lambda title: "Col",
        )
        self.assertEqual(n, 0)

    def test_unresolved_channel_skipped(self):
        self._write_state({
            "v1": {"status": "uploaded", "bvid": "BV1",
                   "channel_title": "Ghost", "title": "T1"},
        })
        n = collection_mod.backfill_collections(
            self.queue, self.state,
            resolve_collection_name=lambda title: "",
        )
        self.assertEqual(n, 0)
        self.assertFalse(self.queue.exists())

    def test_unresolved_channel_prints_aggregate_warning(self):
        """回归：跳过无法归属的记录时只输出聚合统计，不逐条刷屏。"""
        self._write_state({
            "v1": {"status": "uploaded", "bvid": "BV1",
                   "channel_title": "Ghost1", "title": "T1"},
            "v2": {"status": "uploaded", "bvid": "BV2",
                   "channel_title": "Ghost2", "title": "T2"},
        })
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            n = collection_mod.backfill_collections(
                self.queue, self.state,
                resolve_collection_name=lambda title: "",
            )
        self.assertEqual(n, 0)
        self.assertIn("跳过 2 条无法确定归属频道", buf.getvalue())
        self.assertNotIn("无法确定频道「Ghost1」", buf.getvalue())

    def test_missing_state_returns_zero(self):
        n = collection_mod.backfill_collections(
            self.queue, self.state,
            resolve_collection_name=lambda title: "Col",
        )
        self.assertEqual(n, 0)


class EnrichMissingChannelsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state = Path(self._tmp.name) / "processed_videos.json"

    def _write(self, videos):
        self.state.write_text(
            json.dumps({"videos": videos}, ensure_ascii=False),
            encoding="utf-8",
        )

    def test_enriches_empty_channel_and_saves(self):
        self._write({
            "v1": {"status": "uploaded", "bvid": "BV1",
                   "url": "https://youtu.be/v1", "channel_title": ""},
            "v2": {"status": "uploaded", "bvid": "BV2",
                   "url": "https://youtu.be/v2", "channel_title": "Known"},
            "v3": {"status": "failed", "bvid": "BV3",
                   "url": "https://youtu.be/v3", "channel_title": ""},
        })
        n = collection_mod.enrich_missing_channels(
            self.state, resolve_channel=lambda vid: ("Chan", "UC1")
        )
        self.assertEqual(n, 1)
        state2 = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(state2["videos"]["v1"]["channel_title"], "Chan")
        self.assertEqual(state2["videos"]["v1"]["channel_id"], "UC1")
        self.assertEqual(state2["videos"]["v2"]["channel_title"], "Known")
        self.assertEqual(state2["videos"]["v3"]["channel_title"], "")

    def test_resolver_error_skips_entry(self):
        self._write({
            "v1": {"status": "uploaded", "bvid": "BV1",
                   "url": "https://youtu.be/v1", "channel_title": ""},
        })
        n = collection_mod.enrich_missing_channels(
            self.state, resolve_channel=lambda vid: (_ for _ in ()).throw(
                RuntimeError("bot check")
            )
        )
        self.assertEqual(n, 0)
        state2 = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(state2["videos"]["v1"]["channel_title"], "")

    def test_missing_state_returns_zero(self):
        n = collection_mod.enrich_missing_channels(
            Path(self._tmp.name) / "nope.json",
            resolve_channel=lambda vid: ("Chan", "UC1"),
        )
        self.assertEqual(n, 0)


class SweepLockTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.queue = Path(self._tmp.name) / "pending_collections.json"

    def test_lock_excludes_concurrent(self):
        self.assertTrue(collection_mod._acquire_sweep_lock(self.queue))
        self.assertFalse(collection_mod._acquire_sweep_lock(self.queue))
        collection_mod._release_sweep_lock(self.queue)
        self.assertTrue(collection_mod._acquire_sweep_lock(self.queue))
        collection_mod._release_sweep_lock(self.queue)

    def test_process_skips_when_locked(self):
        self.queue.write_text(json.dumps([{
            "video_id": "v1", "bvid": "BV1", "aid": 1,
            "collection_name": "Bynx", "status": "pending",
        }]), encoding="utf-8")
        self.assertTrue(collection_mod._acquire_sweep_lock(self.queue))
        try:
            with patch.object(collection_mod, "pending_collections_path",
                              return_value=self.queue):
                result = collection_mod.process_pending_collections(
                    SimpleNamespace(sessdata="s", bili_jct="j"),
                    retry_interval_seconds=0,
                )
            self.assertEqual(result, (0, 0, 0))
        finally:
            collection_mod._release_sweep_lock(self.queue)


class ListCollectionsTests(unittest.TestCase):
    def _payload(self, season_id, title, section_id, ep_count, total):
        return {
            "code": 0,
            "data": {
                "total": total,
                "seasons": [{
                    "season": {"id": season_id, "title": title},
                    "sections": {"sections": [
                        {"id": section_id, "epCount": ep_count}
                    ]},
                }],
            },
        }

    def test_parses_collections_and_paginates(self):
        pages = {
            1: self._payload(10, "Bynx", 11, 3, 60),
            2: self._payload(20, "KMB", 21, 5, 60),
        }

        async def handler(method, url, kwargs):
            pn = kwargs["params"]["pn"]
            return FakeResponse(pages[pn])

        client = FakeAsyncClient(handler)
        with patch("yt2bili.bilibili.collection.httpx.AsyncClient",
                   return_value=client):
            result = asyncio.run(list_collections(_credential()))

        self.assertEqual(
            [(c.season_id, c.title, c.section_id, c.total) for c in result],
            [(10, "Bynx", 11, 3), (20, "KMB", 21, 5)],
        )
        self.assertEqual([c[0] for c in client.calls], ["get", "get"])

    def test_sync_wrapper(self):
        client = FakeAsyncClient(
            lambda method, url, kwargs: FakeResponse({
                "code": 0,
                "data": {"total": 1, "seasons": [{
                    "season": {"id": 1, "title": "A"},
                    "sections": {"sections": [{"id": 2, "epCount": 0}]},
                }]},
            })
        )
        with patch("yt2bili.bilibili.collection.httpx.AsyncClient",
                   return_value=client):
            result = sync_list_collections(_credential())
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].season_id, 1)


class UploadCoverTests(unittest.TestCase):
    def test_posts_base64_cover_with_csrf(self):
        with tempfile.TemporaryDirectory() as tmp:
            cover = Path(tmp) / "c.jpg"
            cover.write_bytes(b"\xff\xd8fake")

            async def handler(method, url, kwargs):
                body = kwargs.get("data", {})
                self.assertEqual(body["csrf"], "jct")
                self.assertTrue(
                    body["cover"].startswith("data:image/jpeg;base64,")
                )
                self.assertEqual(
                    base64.b64decode(body["cover"].split(",", 1)[1]),
                    b"\xff\xd8fake",
                )
                self.assertIn("ts", kwargs.get("params", {}))
                return FakeResponse({"code": 0, "data": {"url": "http://cov"}})

            client = FakeAsyncClient(handler)
            with patch("yt2bili.bilibili.collection.httpx.AsyncClient",
                       return_value=client):
                url = asyncio.run(upload_cover(_credential(), str(cover)))
        self.assertEqual(url, "http://cov")


class CreateCollectionTests(unittest.TestCase):
    def test_returns_season_id(self):
        async def handler(method, url, kwargs):
            body = kwargs.get("data", {})
            self.assertEqual(body["title"], "MarvelSnap")
            self.assertEqual(body["cover"], "http://cov")
            self.assertEqual(body["csrf"], "jct")
            return FakeResponse({"code": 0, "data": 42})

        client = FakeAsyncClient(handler)
        with patch("yt2bili.bilibili.collection.httpx.AsyncClient",
                   return_value=client):
            season_id = asyncio.run(
                create_collection(_credential(), "MarvelSnap", "http://cov")
            )
        self.assertEqual(season_id, 42)


class FetchVideoPagesTests(unittest.TestCase):
    def test_returns_cid_and_part(self):
        async def handler(method, url, kwargs):
            self.assertEqual(kwargs["params"]["bvid"], "BV1")
            return FakeResponse({"code": 0, "data": [
                {"cid": 100, "part": "P1"},
                {"cid": 200, "part": "P2"},
            ]})

        client = FakeAsyncClient(handler)
        with patch("yt2bili.bilibili.collection.httpx.AsyncClient",
                   return_value=client):
            pages = asyncio.run(fetch_video_pages(_credential(), "BV1"))
        self.assertEqual(pages, [{"cid": 100, "part": "P1"},
                                 {"cid": 200, "part": "P2"}])


class ProcessPendingCollectionsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.queue = Path(self._tmp.name) / "pending_collections.json"
        self.cred = SimpleNamespace(sessdata="s", bili_jct="j", buvid3="b3")

    def _entry(self, status="pending", last_attempt_at="", attempts=0):
        return {
            "video_id": "v1", "bvid": "BV1", "aid": 1,
            "collection_name": "Bynx", "channel_title": "Bynx_Plays",
            "added_at": "", "last_attempt_at": last_attempt_at,
            "attempts": attempts, "status": status, "last_error": "",
        }

    def test_success_adds_and_marks_added(self):
        self.queue.write_text(json.dumps([self._entry()]), encoding="utf-8")
        with patch.object(collection_mod, "pending_collections_path",
                          return_value=self.queue), \
             patch.object(collection_mod, "backfill_collections",
                          return_value=0) as backfill, \
             patch.object(collection_mod, "fetch_video_pages",
                          AsyncMock(return_value=[{"cid": 100, "part": "P1"}])), \
             patch.object(collection_mod, "add_uploaded_video_to_collection",
                          AsyncMock(return_value={"season_id": 7, "episodes": 1})):
            added, pending, failed = collection_mod.process_pending_collections(
                self.cred, retry_interval_seconds=0
            )
        self.assertEqual((added, pending, failed), (1, 0, 0))
        backfill.assert_called_once()
        entries = json.loads(self.queue.read_text(encoding="utf-8"))
        self.assertEqual(entries[0]["status"], "added")

    def test_404_keeps_pending(self):
        self.queue.write_text(json.dumps([self._entry()]), encoding="utf-8")
        with patch.object(collection_mod, "pending_collections_path",
                          return_value=self.queue), \
             patch.object(collection_mod, "backfill_collections",
                          return_value=0), \
             patch.object(
                 collection_mod, "fetch_video_pages",
                 AsyncMock(side_effect=BilibiliApiError(
                     "获取分P信息失败: 啥都木有 (code=-404)", code=-404
                 )),
             ):
            added, pending, failed = collection_mod.process_pending_collections(
                self.cred, retry_interval_seconds=0
            )
        self.assertEqual((added, pending, failed), (0, 1, 0))
        entries = json.loads(self.queue.read_text(encoding="utf-8"))
        self.assertEqual(entries[0]["status"], "pending")
        self.assertEqual(entries[0]["attempts"], 1)

    def test_auth_error_marks_failed(self):
        self.queue.write_text(json.dumps([self._entry()]), encoding="utf-8")
        with patch.object(collection_mod, "pending_collections_path",
                          return_value=self.queue), \
             patch.object(collection_mod, "backfill_collections",
                          return_value=0), \
             patch.object(
                 collection_mod, "fetch_video_pages",
                 AsyncMock(side_effect=RuntimeError(
                     "B站登录凭据已过期（HTTP 401），请重新扫码登录。"
                 )),
             ):
            added, pending, failed = collection_mod.process_pending_collections(
                self.cred, retry_interval_seconds=0
            )
        self.assertEqual((added, pending, failed), (0, 0, 1))
        entries = json.loads(self.queue.read_text(encoding="utf-8"))
        self.assertEqual(entries[0]["status"], "failed")
        self.assertIn("登录凭据已过期", entries[0]["last_error"])

    def test_throttle_skips_recent_attempt(self):
        entry = self._entry(last_attempt_at="2099-01-01T00:00:00Z")
        self.queue.write_text(json.dumps([entry]), encoding="utf-8")
        fetch = AsyncMock(return_value=[{"cid": 1, "part": "P1"}])
        with patch.object(collection_mod, "pending_collections_path",
                          return_value=self.queue), \
             patch.object(collection_mod, "backfill_collections",
                          return_value=0), \
             patch.object(collection_mod, "fetch_video_pages", new=fetch):
            added, pending, failed = collection_mod.process_pending_collections(
                self.cred, retry_interval_seconds=3600
            )
        self.assertEqual((added, pending, failed), (0, 1, 0))
        fetch.assert_not_awaited()

    def test_rate_limited_entries_use_short_cooldown(self):
        """回归：限流条目用短冷却重试，不必等满 1 小时。"""
        recent = collection_mod._now_iso()
        rate_limited = self._entry(last_attempt_at=recent)
        rate_limited["last_error"] = "加入合集失败: 手速太快啦～ (code=20113)"
        normal = self._entry(last_attempt_at=recent)
        normal["video_id"] = "v2"
        normal["bvid"] = "BV2"
        self.queue.write_text(json.dumps([rate_limited, normal]), encoding="utf-8")
        fetch = AsyncMock(return_value=[{"cid": 1, "part": "P1"}])
        add = AsyncMock(return_value={"season_id": 7, "episodes": 1})
        with patch.object(collection_mod, "pending_collections_path",
                          return_value=self.queue), \
             patch.object(collection_mod, "backfill_collections",
                          return_value=0), \
             patch.object(collection_mod, "_COLLECTION_ADD_DELAY", 0.01), \
             patch.object(collection_mod, "fetch_video_pages", new=fetch), \
             patch.object(collection_mod, "add_uploaded_video_to_collection",
                          new=add):
            added, pending, failed = collection_mod.process_pending_collections(
                self.cred, retry_interval_seconds=3600,
                rate_limit_cooldown_seconds=0,
            )
        self.assertEqual((added, pending, failed), (1, 1, 0))
        self.assertEqual(add.await_count, 1)  # 只重试了限流那条，普通条仍受 1h 节流

    def test_process_runs_enrichment_before_backfill(self):
        """resolve_channel 传入时，先补全空频道再回填。"""
        self.queue.write_text(json.dumps([self._entry()]), encoding="utf-8")
        with patch.object(collection_mod, "pending_collections_path",
                          return_value=self.queue), \
             patch.object(collection_mod, "enrich_missing_channels",
                          return_value=2) as enrich, \
             patch.object(collection_mod, "backfill_collections",
                          return_value=0), \
             patch.object(collection_mod, "fetch_video_pages",
                          AsyncMock(return_value=[{"cid": 1, "part": "P1"}])), \
             patch.object(collection_mod, "add_uploaded_video_to_collection",
                          AsyncMock(return_value={"season_id": 7, "episodes": 1})):
            collection_mod.process_pending_collections(
                self.cred, retry_interval_seconds=0,
                resolve_channel=lambda vid: ("Chan", "UC1"),
            )
        enrich.assert_called_once()
        self.assertEqual(
            enrich.call_args.args[0],
            self.queue.parent / "processed_videos.json",
        )

    def test_network_error_keeps_pending(self):
        self.queue.write_text(json.dumps([self._entry()]), encoding="utf-8")
        with patch.object(collection_mod, "pending_collections_path",
                          return_value=self.queue), \
             patch.object(collection_mod, "backfill_collections",
                          return_value=0), \
             patch.object(
                 collection_mod, "fetch_video_pages",
                 AsyncMock(side_effect=OSError("connection reset")),
             ):
            added, pending, failed = collection_mod.process_pending_collections(
                self.cred, retry_interval_seconds=0
            )
        self.assertEqual((added, pending, failed), (0, 1, 0))

    def test_sweep_stops_after_budget(self):
        """回归：单轮补归有预算，避免一次性打爆 B站 限流。"""
        entries = []
        for i in range(4):
            entry = self._entry()
            entry["video_id"] = f"v{i}"
            entries.append(entry)
        self.queue.write_text(json.dumps(entries), encoding="utf-8")
        sleep = AsyncMock()
        with patch.object(collection_mod, "pending_collections_path",
                          return_value=self.queue), \
             patch.object(collection_mod, "backfill_collections",
                          return_value=0), \
             patch.object(collection_mod, "_COLLECTION_SWEEP_BUDGET", 2), \
             patch.object(collection_mod, "_COLLECTION_ADD_DELAY", 0.01), \
             patch.object(collection_mod, "fetch_video_pages",
                          AsyncMock(return_value=[{"cid": 1, "part": "P1"}])), \
             patch.object(collection_mod, "add_uploaded_video_to_collection",
                          AsyncMock(return_value={"season_id": 7, "episodes": 1})), \
             patch.object(collection_mod.asyncio, "sleep", new=sleep):
            added, pending, failed = collection_mod.process_pending_collections(
                self.cred, retry_interval_seconds=0
            )
        self.assertEqual((added, pending, failed), (2, 2, 0))
        self.assertEqual(sleep.await_count, 1)  # 第一次补归后短暂停顿，到预算即停

    def test_sweep_breaks_on_rate_limit(self):
        """回归：撞上 20113/20111 限流码立即停止本轮，不再继续提交。"""
        entries = []
        for i in range(4):
            entry = self._entry()
            entry["video_id"] = f"v{i}"
            entry["bvid"] = f"BV{i}"
            entries.append(entry)
        self.queue.write_text(json.dumps(entries), encoding="utf-8")

        async def add_side_effect(*args, **kwargs):
            if args[3] == "BV2":  # 第三条开始被限流
                raise BilibiliApiError(
                    "加入合集失败: 手速太快啦～休息几分钟，稍后再试！ (code=20113)",
                    code=20113,
                )
            return {"season_id": 7, "episodes": 1}

        sleep = AsyncMock()
        with patch.object(collection_mod, "pending_collections_path",
                          return_value=self.queue), \
             patch.object(collection_mod, "backfill_collections",
                          return_value=0), \
             patch.object(collection_mod, "_COLLECTION_ADD_DELAY", 0.01), \
             patch.object(collection_mod, "fetch_video_pages",
                          AsyncMock(return_value=[{"cid": 1, "part": "P1"}])), \
             patch.object(collection_mod, "add_uploaded_video_to_collection",
                          AsyncMock(side_effect=add_side_effect)) as add, \
             patch.object(collection_mod.asyncio, "sleep", new=sleep):
            added, pending, failed = collection_mod.process_pending_collections(
                self.cred, retry_interval_seconds=0
            )
        self.assertEqual((added, pending, failed), (2, 2, 0))
        self.assertEqual(add.await_count, 3)  # 第三条撞限流后不再尝试第四条


class AddVideoToCollectionTests(unittest.TestCase):
    def test_posts_episodes_json(self):
        episodes = [{"aid": 1, "cid": 2, "title": "T", "charging_pay": 0}]

        async def handler(method, url, kwargs):
            payload = kwargs.get("json", {})
            self.assertEqual(payload["sectionId"], 11)
            self.assertEqual(payload["episodes"], episodes)
            self.assertEqual(kwargs["params"]["csrf"], "jct")
            return FakeResponse({"code": 0})

        client = FakeAsyncClient(handler)
        with patch("yt2bili.bilibili.collection.httpx.AsyncClient",
                   return_value=client):
            result = asyncio.run(
                add_video_to_collection(_credential(), 11, episodes)
            )
        self.assertEqual(result["code"], 0)


class EnsureCollectionTests(unittest.TestCase):
    def test_reuses_existing(self):
        existing = [CollectionInfo(season_id=7, title="Bynx", section_id=8)]
        with patch.object(collection_mod, "list_collections",
                          AsyncMock(return_value=existing)), \
             patch.object(collection_mod, "upload_cover") as up, \
             patch.object(collection_mod, "create_collection") as create:
            info, created = asyncio.run(
                ensure_collection(_credential(), "bynx", None)
            )
        self.assertEqual((info.season_id, created), (7, False))
        up.assert_not_called()
        create.assert_not_called()

    def test_creates_missing_then_relists(self):
        empty = []
        after_create = [CollectionInfo(season_id=9, title="New", section_id=10)]
        with patch.object(collection_mod, "list_collections",
                          side_effect=[empty, after_create]) as lst, \
             patch.object(collection_mod, "upload_cover",
                          AsyncMock(return_value="http://cov")) as up, \
             patch.object(collection_mod, "create_collection",
                          AsyncMock(return_value=9)) as create:
            info, created = asyncio.run(
                ensure_collection(_credential(), "New", "/tmp/x.jpg")
            )
        self.assertEqual((info.season_id, info.section_id, created), (9, 10, True))
        up.assert_awaited_once_with(_credential(), "/tmp/x.jpg")
        create.assert_awaited_once_with(_credential(), "New", "http://cov")
        self.assertEqual(lst.call_count, 2)

    def test_raises_when_created_but_not_found(self):
        with patch.object(collection_mod, "list_collections",
                          side_effect=[[], []]), \
             patch.object(collection_mod, "upload_cover",
                          AsyncMock(return_value="http://cov")), \
             patch.object(collection_mod, "create_collection",
                          AsyncMock(return_value=9)):
            with self.assertRaises(RuntimeError):
                asyncio.run(ensure_collection(_credential(), "New", None))


class AddUploadedVideoTests(unittest.TestCase):
    def test_adds_uploaded_video_to_collection(self):
        info = CollectionInfo(season_id=1, title="Bynx", section_id=2)
        pages = [{"cid": 100, "part": "P1"}, {"cid": 200, "part": "P2"}]
        with patch.object(collection_mod, "ensure_collection",
                          AsyncMock(return_value=(info, False))) as ensure, \
             patch.object(collection_mod, "fetch_video_pages",
                          AsyncMock(return_value=pages)) as fetch, \
             patch.object(collection_mod, "add_video_to_collection",
                          AsyncMock(return_value={"code": 0})) as add:
            result = asyncio.run(
                add_uploaded_video_to_collection(
                    _credential(), "Bynx", "/tmp/c.jpg", "BV1", 55,
                    ["译1", "译2"],
                )
            )
        ensure.assert_awaited_once_with(_credential(), "Bynx", "/tmp/c.jpg")
        fetch.assert_awaited_once()
        self.assertEqual(fetch.await_args.args[1], "BV1")
        add.assert_awaited_once()
        episodes = add.await_args.args[2]
        self.assertEqual([e["title"] for e in episodes], ["译1", "译2"])
        self.assertEqual(result["season_id"], 1)
        self.assertEqual(result["episodes"], 2)

    def test_raises_when_no_pages(self):
        info = CollectionInfo(season_id=1, title="B", section_id=2)
        with patch.object(collection_mod, "ensure_collection",
                          AsyncMock(return_value=(info, False))), \
             patch.object(collection_mod, "fetch_video_pages",
                          AsyncMock(return_value=[])):
            with self.assertRaises(RuntimeError):
                asyncio.run(
                    add_uploaded_video_to_collection(
                        _credential(), "B", None, "BV1", 1
                    )
                )


class NormalizePublishDateTests(unittest.TestCase):
    def test_ytdlp_upload_date(self):
        self.assertEqual(
            collection_mod._normalize_publish_date("20260831"),
            "2026-08-31",
        )

    def test_iso_and_slashed(self):
        self.assertEqual(
            collection_mod._normalize_publish_date("2026-08-31T12:00:00Z"),
            "2026-08-31",
        )
        self.assertEqual(
            collection_mod._normalize_publish_date("2026/08/31"),
            "2026-08-31",
        )

    def test_epoch_seconds(self):
        self.assertEqual(
            collection_mod._normalize_publish_date(1788237652),
            "2026-09-01",
        )

    def test_invalid_returns_empty(self):
        self.assertEqual(collection_mod._normalize_publish_date(""), "")
        self.assertEqual(collection_mod._normalize_publish_date(None), "")
        self.assertEqual(collection_mod._normalize_publish_date("not-a-date"), "")
        self.assertEqual(collection_mod._normalize_publish_date(99999999999), "")


class BuildReorderSortsTests(unittest.TestCase):
    def _eps(self, dates):
        return [
            {"id": i + 1, "bvid": f"BV{i + 1}", "aid": 100 + i,
             "order": i + 1, "published_at": date}
            for i, date in enumerate(dates)
        ]

    def test_ascending_newest_last(self):
        eps = self._eps(["2026-08-01", "2026-07-01", "2026-09-01"])
        sorts = collection_mod.build_reorder_sorts(eps)
        self.assertEqual(sorts, [
            {"id": 2, "sort": 1},
            {"id": 1, "sort": 2},
            {"id": 3, "sort": 3},
        ])

    def test_uses_bvid_and_aid_map(self):
        eps = self._eps(["2026-01-01", "2026-02-01"])
        sorts = collection_mod.build_reorder_sorts(
            eps, {"BV2": "2025-12-31"}
        )
        self.assertEqual(sorts, [
            {"id": 2, "sort": 1},
            {"id": 1, "sort": 2},
        ])

    def test_unknown_dates_stable_at_end(self):
        eps = [
            {"id": 1, "bvid": "BV1", "aid": 1},
            {"id": 2, "bvid": "BV2", "aid": 2},
            {"id": 3, "bvid": "BV3", "aid": 3},
            {"id": 4, "bvid": "BV4", "aid": 4},
        ]
        sorts = collection_mod.build_reorder_sorts(
            eps, {"BV3": "2026-01-01"}
        )
        self.assertEqual(sorts, [
            {"id": 3, "sort": 1},
            {"id": 1, "sort": 2},
            {"id": 2, "sort": 3},
            {"id": 4, "sort": 4},
        ])

    def test_already_sorted_returns_none(self):
        eps = self._eps(["2026-01-01", "2026-02-01", "2026-03-01"])
        self.assertIsNone(collection_mod.build_reorder_sorts(eps))

    def test_missing_episode_ids_returns_none(self):
        eps = [{"id": 0, "bvid": "BV1", "aid": 1}]
        self.assertIsNone(collection_mod.build_reorder_sorts(eps))

    def test_reverse_newest_first(self):
        eps = self._eps(["2026-08-01", "2026-07-01", "2026-09-01"])
        sorts = collection_mod.build_reorder_sorts(eps, reverse=True)
        self.assertEqual(sorts, [
            {"id": 3, "sort": 1},
            {"id": 1, "sort": 2},
            {"id": 2, "sort": 3},
        ])


class FetchCollectionSectionTests(unittest.TestCase):
    def test_returns_section_and_episodes(self):
        payload = {"code": 0, "data": {
            "section": {"id": 5155061, "seasonId": 4616294, "title": "正片"},
            "episodes": [{"id": 1, "bvid": "BV1", "order": 1}],
        }}
        client = FakeAsyncClient(
            lambda method, url, kwargs: FakeResponse(payload)
        )
        section, episodes = asyncio.run(
            collection_mod.fetch_collection_section(
                _credential(), 5155061, client=client
            )
        )
        self.assertEqual(section["seasonId"], 4616294)
        self.assertEqual(episodes[0]["bvid"], "BV1")
        self.assertEqual(client.calls[0][0], "get")
        self.assertEqual(client.calls[0][2]["params"], {"id": 5155061})


class ReorderCollectionSectionTests(unittest.TestCase):
    def test_posts_sorts_payload(self):
        payload = {"code": 0, "data": None}
        client = FakeAsyncClient(
            lambda method, url, kwargs: FakeResponse(payload)
        )
        section = {"id": 5155061, "seasonId": 4616294, "title": "正片", "type": 1}
        episodes = [
            {"id": 1, "bvid": "BV1", "aid": 1},
            {"id": 2, "bvid": "BV2", "aid": 2},
        ]
        result = asyncio.run(
            collection_mod.reorder_collection_section(
                _credential(), section, episodes,
                {"BV1": "2026-02-01", "BV2": "2026-01-01"},
                client=client,
            )
        )
        self.assertTrue(result["changed"])
        self.assertEqual(result["season_id"], 4616294)
        method, url, kwargs = client.calls[0]
        self.assertEqual(method, "post")
        self.assertIn("/season/section/edit", url)
        self.assertEqual(kwargs["params"]["csrf"], "jct")
        body = kwargs["json"]
        self.assertEqual(body["section"]["id"], 5155061)
        self.assertEqual(body["sorts"], [
            {"id": 2, "sort": 1},
            {"id": 1, "sort": 2},
        ])

    def test_skips_post_when_already_ordered(self):
        client = FakeAsyncClient(
            lambda method, url, kwargs: FakeResponse({"code": 0})
        )
        section = {"id": 1, "seasonId": 2, "title": "正片", "type": 1}
        episodes = [
            {"id": 1, "bvid": "BV1", "aid": 1},
            {"id": 2, "bvid": "BV2", "aid": 2},
        ]
        result = asyncio.run(
            collection_mod.reorder_collection_section(
                _credential(), section, episodes,
                {"BV1": "2026-01-01", "BV2": "2026-02-01"},
                client=client,
            )
        )
        self.assertFalse(result["changed"])
        self.assertEqual(client.calls, [])


class ReorderDateCollectionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.queue = Path(self._tmp.name) / "pending_collections.json"
        self.state = Path(self._tmp.name) / "processed_videos.json"

    def _write_state(self, videos):
        self.state.write_text(
            json.dumps({"videos": videos}, ensure_ascii=False),
            encoding="utf-8",
        )

    def test_enrich_queue_dates_copies_from_state(self):
        self._write_state({
            "v1": {"status": "uploaded", "bvid": "BV1",
                   "published_at": "20260831"},
            "v2": {"status": "uploaded", "bvid": "BV2",
                   "published_at": ""},
        })
        self.queue.write_text(json.dumps([
            {"video_id": "v1", "bvid": "BV1", "published_at": ""},
            {"video_id": "v2", "bvid": "BV2", "published_at": ""},
            {"video_id": "v3", "bvid": "BV3", "published_at": "20260101"},
        ]), encoding="utf-8")
        n = collection_mod.enrich_queue_dates(self.queue, self.state)
        self.assertEqual(n, 1)
        entries = json.loads(self.queue.read_text(encoding="utf-8"))
        by_id = {e["video_id"]: e["published_at"] for e in entries}
        self.assertEqual(by_id["v1"], "20260831")
        self.assertEqual(by_id["v3"], "20260101")

    def test_collect_published_dates_merges_sources(self):
        self._write_state({
            "v1": {"status": "uploaded", "bvid": "BV1",
                   "published_at": "20260831", "video_id": "vid1"},
        })
        self.queue.write_text(json.dumps([
            {"video_id": "v2", "bvid": "BV2", "published_at": "20260701"},
        ]), encoding="utf-8")
        dates, yt_bvids, bvid_to_video_id = (
            collection_mod._collect_published_dates(
            self.state, self.queue
            )
        )
        self.assertEqual(dates["BV1"], "2026-08-31")
        self.assertEqual(dates["BV2"], "2026-07-01")
        self.assertEqual(yt_bvids, {"BV1", "BV2"})
        self.assertEqual(bvid_to_video_id, {"BV1": "vid1", "BV2": "v2"})

    def test_extract_youtube_url(self):
        self.assertEqual(
            collection_mod._extract_youtube_url(
                "来源：https://www.youtube.com/watch?v=abc123XYZ99 点赞！"
            ),
            "abc123XYZ99",
        )
        self.assertEqual(
            collection_mod._extract_youtube_url("youtu.be/abc123XYZ99"),
            "abc123XYZ99",
        )
        self.assertEqual(collection_mod._extract_youtube_url("无链接"), "")

    def test_fill_youtube_api_pubdates_batches(self):
        class FakeVideosList:
            def __init__(self, result):
                self._result = result

            def execute(self):
                return self._result

        class FakeYoutube:
            def __init__(self, result):
                self._result = result

            def videos(self):
                return SimpleNamespace(
                    list=lambda part, id, maxResults: FakeVideosList(
                        self._result
                    )
                )

        result = {"items": [
            {"id": "vid1", "snippet": {"publishedAt": "2026-08-01T00:00:00Z"}},
            {"id": "vid2", "snippet": {"publishedAt": "2026-07-01T00:00:00Z"}},
        ]}
        dates = {}
        yt_bvids = set()
        filled = asyncio.run(
            collection_mod._fill_youtube_api_pubdates(
                dates, yt_bvids,
                {"vid1": "BV1", "vid2": "BV2"},
                youtube=FakeYoutube(result),
            )
        )
        self.assertEqual(filled, 2)
        self.assertEqual(dates["BV1"], "2026-08-01")
        self.assertEqual(dates["BV2"], "2026-07-01")
        self.assertEqual(yt_bvids, {"BV1", "BV2"})

    def test_fill_youtube_api_pubdates_skips_without_service(self):
        filled = asyncio.run(
            collection_mod._fill_youtube_api_pubdates(
                {}, set(), {"vid1": "BV1"}, youtube=None
            )
        )
        self.assertEqual(filled, 0)

    def test_fill_bili_pubdates_fills_and_recovers_desc_ids(self):
        payload = {"code": 0, "data": {
            "desc": "原视频: https://www.youtube.com/watch?v=yt999YYY999",
            "pubdate": 1788237652,
        }}
        client = FakeAsyncClient(
            lambda method, url, kwargs: FakeResponse(payload)
        )
        episodes = [
            {"id": 1, "bvid": "BV2", "aid": 2},
        ]
        dates = {}
        bili_only, desc_ids = asyncio.run(
            collection_mod._fill_bili_pubdates(
                client, episodes, dates
            )
        )
        self.assertEqual(bili_only, {"BV2"})
        self.assertEqual(dates["BV2"], "2026-09-01")
        self.assertEqual(desc_ids, {"BV2": "yt999YYY999"})

    def test_fill_bili_pubdates_skips_known_dates(self):
        client = FakeAsyncClient(
            lambda method, url, kwargs: FakeResponse({"code": 0, "data": {}})
        )
        episodes = [{"id": 1, "bvid": "BV1", "aid": 1}]
        bili_only, desc_ids = asyncio.run(
            collection_mod._fill_bili_pubdates(
                client, episodes, {"BV1": "2026-08-01", "1": "2026-08-01"}
            )
        )
        self.assertEqual(bili_only, set())
        self.assertEqual(desc_ids, {})
        self.assertEqual(client.calls, [])

    def test_backfill_copies_published_at(self):
        self._write_state({
            "v1": {"status": "uploaded", "bvid": "BV1", "aid": 1,
                   "channel_title": "Bynx_Plays", "title": "T1",
                   "published_at": "20260831"},
        })
        collection_mod.backfill_collections(
            self.queue, self.state,
            resolve_collection_name=lambda title: "Bynx",
        )
        entries = json.loads(self.queue.read_text(encoding="utf-8"))
        self.assertEqual(entries[0]["published_at"], "20260831")


class ReorderCollectionsFullPassTests(unittest.TestCase):
    def test_skips_unchanged_and_reorders_changed(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            queue = tmp / "pending_collections.json"
            state = tmp / "processed_videos.json"
            queue.write_text(json.dumps([]), encoding="utf-8")
            state.write_text(json.dumps({"videos": {
                "v1": {"status": "uploaded", "bvid": "BV1",
                       "published_at": "20260701"},
            }}), encoding="utf-8")

            def fake_list(credential):
                return [
                    CollectionInfo(season_id=10, title="A", section_id=20,
                                   section_mtime=100),
                    CollectionInfo(season_id=11, title="B", section_id=21,
                                   section_mtime=200),
                ]

            calls = []

            async def fake_section(credential, section_id, client=None):
                if section_id == 21:
                    return ({"id": 21, "seasonId": 11, "title": "正片"},
                            [{"id": 1, "bvid": "BV1", "aid": 1}])
                return ({"id": 20, "seasonId": 10, "title": "正片"}, [])

            async def fake_reorder(credential, section, episodes,
                                   published_at_map=None, reverse=False,
                                   client=None):
                calls.append((section.get("seasonId"), episodes))
                return {"changed": True, "season_id": section["seasonId"],
                        "episodes": len(episodes)}

            markers = tmp / "collections_reorder.json"
            markers.write_text(json.dumps(
                {"10": {"mtime": 100, "complete": True}}
            ), encoding="utf-8")

            with patch.object(collection_mod, "pending_collections_path",
                              return_value=queue), \
                 patch.object(collection_mod, "sync_list_collections",
                              side_effect=fake_list), \
                 patch.object(collection_mod, "fetch_collection_section",
                              side_effect=fake_section), \
                 patch.object(collection_mod, "reorder_collection_section",
                              side_effect=fake_reorder):
                reordered, already = collection_mod.reorder_collections(
                    _credential(), state_path=state
                )

            self.assertEqual(reordered, 1)
            self.assertEqual(already, 0)
            self.assertEqual([c[0] for c in calls], [11])
            saved = json.loads(markers.read_text(encoding="utf-8"))
            self.assertEqual(saved["11"]["mtime"], 200)
            self.assertTrue(saved["11"]["complete"])
            self.assertEqual(saved["10"]["mtime"], 100)


if __name__ == "__main__":
    unittest.main()
