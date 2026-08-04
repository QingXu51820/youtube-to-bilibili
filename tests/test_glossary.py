"""自测：SNAP / Deadlock 词表（缓存容错、空键污染回归、构建逻辑）。"""

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


if __name__ == "__main__":
    unittest.main()
