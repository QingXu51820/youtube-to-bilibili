"""自测：SNAP / Deadlock / 荒野乱斗词表（缓存容错、空键污染回归、构建逻辑）。"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from yt2bili import glossary as gl


class LoadCacheTests(unittest.TestCase):
    def test_valid_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "glossary.json"
            path.write_text(
                json.dumps({"glossary": {"Abomination": "恶型怪"}}), encoding="utf-8"
            )
            result = gl._load_cache(path)
        self.assertEqual(result, {"Abomination": "恶型怪"})

    def test_corrupted_json_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "glossary.json"
            path.write_text("{broken json", encoding="utf-8")
            self.assertIsNone(gl._load_cache(path))

    def test_empty_glossary_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "glossary.json"
            path.write_text(json.dumps({"glossary": {}}), encoding="utf-8")
            self.assertIsNone(gl._load_cache(path))

    def test_wrong_glossary_type_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "glossary.json"
            path.write_text(json.dumps({"glossary": "not-a-dict"}), encoding="utf-8")
            self.assertIsNone(gl._load_cache(path))

    def test_missing_file_returns_none(self):
        self.assertIsNone(gl._load_cache(Path("nonexistent.json")))

    def test_coerces_keys_and_values_to_str(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "glossary.json"
            path.write_text(
                json.dumps({"glossary": {123: 456}}), encoding="utf-8"
            )
            result = gl._load_cache(path)
        self.assertEqual(result, {"123": "456"})


class SaveLoadRoundtripTests(unittest.TestCase):
    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sub" / "glossary.json"
            gl._save_cache(path, {"Abomination": "恶型怪", "Sera": "塞拉"})
            result = gl._load_cache(path)
        self.assertEqual(result, {"Abomination": "恶型怪", "Sera": "塞拉"})

    def test_no_tmp_left_behind(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "glossary.json"
            gl._save_cache(path, {"A": "B"})
            leftovers = list(Path(tmp).glob("*.tmp"))
        self.assertEqual(leftovers, [])


class BuildDeadlockGlossaryTests(unittest.TestCase):
    """回归：EN 词表拉取失败 → 空词表（防止 \b\b 空键污染标题）。"""

    def test_missing_en_data_returns_empty(self):
        """EN 文件失败必须返回空词表，绝不带空键继续构建。"""
        with patch.object(gl, "_fetch_json_dict", side_effect=[
            {"hero_atlas": "Abrams|阿布兰"},  # zh OK
            {},                                # en 失败
        ]):
            result = gl._build_deadlock_glossary()
        self.assertEqual(result, {})
        self.assertNotIn("", result)

    def test_missing_zh_data_returns_empty(self):
        with patch.object(gl, "_fetch_json_dict", side_effect=[{}, {"hero_atlas": "Abrams"}]):
            result = gl._build_deadlock_glossary()
        self.assertEqual(result, {})

    def test_builds_heroes_items_and_aliases(self):
        data_zh = {
            "hero_atlas": "Abrams|阿布兰",
            "hero_haze": "Haze|海泽",
            "upgrade_frenzy": "Frenzy|狂乱",
            "upgrade_frenzy_desc": "description text",
            "upgrade_frenzy_active": "active desc",
            "random_key": "should be ignored",
        }
        data_en = {
            "upgrade_frenzy": "Frenzy|Frenzy",
            "upgrade_frenzy_desc": "desc",
        }
        with patch.object(gl, "_fetch_json_dict", side_effect=[data_zh, data_en]):
            result = gl._build_deadlock_glossary()

        self.assertEqual(result.get("Abrams"), "阿布兰")
        self.assertEqual(result.get("Haze"), "海泽")
        self.assertEqual(result.get("Frenzy"), "狂乱")
        self.assertEqual(result.get("Mo and Krill"), result.get("Mo & Krill"))
        self.assertEqual(result.get("Deadlock"), "死锁")
        self.assertEqual(result.get("Hero Labs"), "英雄实验室")
        self.assertNotIn("upgrade_frenzy_desc", result)
        self.assertNotIn("random_key", result)
        self.assertNotIn("", result)

    def test_item_same_name_skipped(self):
        data_zh = {"upgrade_foo": "Foo|Foo"}
        data_en = {"upgrade_foo": "Foo|Foo"}
        with patch.object(gl, "_fetch_json_dict", side_effect=[data_zh, data_en]):
            result = gl._build_deadlock_glossary()
        self.assertNotIn("Foo", result)


class BuildSnapGlossaryTests(unittest.TestCase):
    def _card(self, def_id, description):
        return {"defId": def_id, "originalName": def_id, "name": "中文名", "description": description}

    def _location(self, def_id, description):
        return {"defId": def_id, "originalName": def_id, "name": "中文地点", "description": description}

    def test_partial_location_fetch_returns_empty(self):
        """一张卡牌接口失败时不得保存残缺词表，避免丢地点。"""
        cards = [self._card("Abomination", "On Reveal: Afflict cards here.")]
        with patch.object(gl, "_fetch_json", side_effect=[cards, []]):
            self.assertEqual(gl._build_glossary(), {})

    def test_adds_only_present_auto_terms(self):
        cards = [self._card("OnRevealExample", "On Reveal: Give your Ongoing cards +2 Power.")]
        locations = [self._location("ExampleLocation", "Ongoing: Cards here cannot be destroyed.")]
        with patch.object(gl, "_fetch_json", side_effect=[cards, locations, cards, locations]):
            result = gl._build_glossary()

        self.assertIn("On Reveal", result)
        self.assertEqual(result["On Reveal"], "揭示")
        self.assertIn("Ongoing", result)
        self.assertNotIn("Activate", result)  # not present in the corpus

    def test_extract_game_terms_from_items(self):
        items = [
            self._card("A", "On Reveal: Destroy a card."),
            self._location("B", "When a card moves here, +1 Power."),
        ]
        result = gl._extract_game_terms_from_items(items)
        self.assertIn("On Reveal", result)
        self.assertIn("Destroy", result)
        self.assertNotIn("Ongoing", result)


class GetGlossaryTests(unittest.TestCase):
    def tearDown(self):
        gl._glossary = None
        gl._last_fetch_time = 0.0
        gl._fetch_in_progress = False
        gl._deadlock_glossary = None
        gl._deadlock_last_fetch_time = 0.0
        gl._deadlock_fetch_in_progress = False

    def test_disabled_returns_empty(self):
        with patch.object(gl.config, "SNAP_GLOSSARY_ENABLED", False):
            self.assertEqual(gl.get_glossary(), {})

    def test_loads_from_cache(self):
        with patch.object(gl.config, "SNAP_GLOSSARY_ENABLED", True), \
             patch.object(gl.config, "SNAP_GLOSSARY_TTL", 3600), \
             patch.object(gl, "_load_cache", return_value={"Abomination": "恶型怪"}), \
             patch.object(gl, "_build_glossary", side_effect=AssertionError("不应触发网络拉取")), \
             tempfile.TemporaryDirectory() as tmp:
            with patch.object(gl.config, "SNAP_GLOSSARY_CACHE", str(Path(tmp) / "g.json")):
                result = gl.get_glossary()
        self.assertEqual(result, {"Abomination": "恶型怪"})

    def test_fetch_when_no_cache(self):
        with patch.object(gl.config, "SNAP_GLOSSARY_ENABLED", True), \
             patch.object(gl.config, "SNAP_GLOSSARY_TTL", 3600), \
             patch.object(gl, "_load_cache", return_value=None), \
             patch.object(gl, "_build_glossary", return_value={"Sera": "塞拉"}), \
             patch.object(gl, "_save_cache") as save, \
             tempfile.TemporaryDirectory() as tmp:
            with patch.object(gl.config, "SNAP_GLOSSARY_CACHE", str(Path(tmp) / "g.json")):
                result = gl.get_glossary()
        self.assertEqual(result, {"Sera": "塞拉"})
        save.assert_called_once()

    def test_deadlock_disabled_returns_empty(self):
        with patch.object(gl.config, "DEADLOCK_GLOSSARY_ENABLED", False):
            self.assertEqual(gl.get_deadlock_glossary(), {})

    def test_get_snap_game_terms_falls_back_to_seeds(self):
        with patch.object(gl.config, "SNAP_GLOSSARY_ENABLED", True), \
             patch.object(gl, "_load_game_terms", return_value={"On Reveal": "揭示"}):
            result = gl.get_snap_game_terms()
        self.assertEqual(result, {"On Reveal": "揭示"})


class BrawlStarsGlossaryTests(unittest.TestCase):
    """回归：荒野乱斗术语的复数形式必须命中官方简中名。

    字幕里说的是 "Starr Drops" / "Brawl Boxes"，而术语表键是单数
    "Starr Drop" / "Brawl Box"。_apply_glossary 按整词匹配（\\b 边界），
    复数一律匹配不上 → 官方译名静默失效，模型只好自己翻（星妙掉落）。
    """

    def tearDown(self):
        gl._brawl_glossary = None

    def _load(self, glossary=None, game_terms=None):
        """按给定词表内容跑一遍 get_brawl_stars_glossary()。"""
        payload = {"glossary": glossary or {}, "game_terms": game_terms or {}}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bs.json"
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            with patch.object(gl.config, "BRAWL_STARS_GLOSSARY_ENABLED", True), \
                 patch.object(gl.config, "BRAWL_STARS_GLOSSARY_CACHE", str(path)), \
                 patch.object(gl, "_add_bs_multiword_abilities"):
                return gl.get_brawl_stars_glossary()

    def test_english_plural(self):
        self.assertEqual(gl._english_plural("Starr Drop"), "Starr Drops")
        self.assertEqual(gl._english_plural("Chaos Drop"), "Chaos Drops")
        self.assertEqual(gl._english_plural("Brawl Box"), "Brawl Boxes")
        self.assertEqual(gl._english_plural("Bounty"), "Bounties")
        self.assertEqual(gl._english_plural("Gear"), "Gears")

    def test_english_plural_skips_already_plural_terms(self):
        # 已是复数或不可数：不能再叠一层（Gemses / Blinges）
        for term in ("Gems", "Coins", "Power Points", "Balance Changes", "Duels"):
            self.assertIsNone(gl._english_plural(term), term)

    def test_auto_apply_terms_get_plural_keys(self):
        result = self._load(game_terms={
            "Starr Drop": "星妙惊喜", "Chaos Drop": "混沌惊喜",
            "Brawl Box": "乱斗宝箱", "Bounty": "赏金猎人",
        })
        self.assertEqual(result["Starr Drops"], "星妙惊喜")
        self.assertEqual(result["Chaos Drops"], "混沌惊喜")
        self.assertEqual(result["Brawl Boxes"], "乱斗宝箱")
        self.assertEqual(result["Bounties"], "赏金猎人")

    def test_credits_is_auto_applied(self):
        # 英雄券 之前漏在白名单外，字幕里原样残留英文 "Credits"
        self.assertEqual(self._load(game_terms={"Credits": "英雄券"})["Credits"], "英雄券")

    def test_terms_outside_whitelist_get_no_plural(self):
        # 白名单外的词（英雄名）不加复数：Berry 不能认领水果 "Berries"
        result = self._load(
            glossary={"Berry": "贝里"},
            game_terms={"Berry": "贝里", "Starr Drop": "星妙惊喜"},
        )
        self.assertEqual(result["Berry"], "贝里")
        self.assertNotIn("Berries", result)

    def test_plural_colliding_with_hero_name_keeps_hero(self):
        # 复数与英雄名撞车时英雄名优先，不被术语覆盖
        result = self._load(glossary={"Gears": "吉尔斯"}, game_terms={"Gear": "装备"})
        self.assertEqual(result["Gears"], "吉尔斯")

    def test_apply_glossary_replaces_plural_terms(self):
        """端到端：字幕里的复数术语替换成官方简中名。"""
        from yt2bili.translation import translator  # 匹配逻辑在翻译模块

        game_terms = {
            "Starr Drop": "星妙惊喜", "Chaos Drop": "混沌惊喜", "Credits": "英雄券",
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bs.json"
            path.write_text(json.dumps({"game_terms": game_terms}, ensure_ascii=False),
                            encoding="utf-8")
            with patch.object(gl.config, "BRAWL_STARS_GLOSSARY_ENABLED", True), \
                 patch.object(gl.config, "BRAWL_STARS_GLOSSARY_CACHE", str(path)), \
                 patch.object(gl.config, "SNAP_GLOSSARY_ENABLED", False), \
                 patch.object(gl.config, "DEADLOCK_GLOSSARY_ENABLED", False), \
                 patch.object(gl, "_add_bs_multiword_abilities"):
                out = translator._apply_glossary(
                    "5 Chaos Drops, 10 random Starr Drops, 1,000 Credits"
                )
        self.assertEqual(out, "5 混沌惊喜, 10 random 星妙惊喜, 1,000 英雄券")


if __name__ == "__main__":
    unittest.main()
