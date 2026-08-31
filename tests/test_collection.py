"""自测：B站合集匹配/映射/请求构造纯逻辑 + 占位封面。"""

import asyncio
import base64
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from PIL import Image

import yt2bili.bilibili.collection as collection_mod
from yt2bili.bilibili.collection import (
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
        fetch.assert_awaited_once_with(_credential(), "BV1")
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

if __name__ == "__main__":
    unittest.main()
