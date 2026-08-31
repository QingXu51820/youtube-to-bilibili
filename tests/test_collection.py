"""自测：B站合集匹配/映射/请求构造纯逻辑 + 占位封面。"""

import tempfile
import unittest
from pathlib import Path

from PIL import Image

from yt2bili.bilibili.collection import (
    ChannelCollectionMatch,
    CollectionInfo,
    build_episodes,
    find_collection_by_name,
    make_placeholder_cover,
    normalize_name,
    resolve_channel_collections,
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


if __name__ == "__main__":
    unittest.main()
