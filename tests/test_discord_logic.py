"""自测：Discord 模块纯逻辑（状态文件、频道解析、消息解析、动态文本）。

注意：discord.py 库未安装，Gateway 事件循环部分（on_ready/on_message）不可测；
这里只覆盖不依赖 discord.py 的纯逻辑函数。
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from yt2bili import config
from yt2bili.discord import monitor as dmon
from yt2bili.discord import publisher as dpub


class DiscordStateTests(unittest.TestCase):
    """_load_state/_save_state/_is_processed/_mark_processed。"""

    def test_load_missing_returns_default(self):
        state = dmon._load_state(Path("no/such/state.json"))
        self.assertEqual(state, {"version": 1, "messages": {}})

    def test_load_corrupt_returns_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "state.json"
            p.write_text("{broken", encoding="utf-8")
            state = dmon._load_state(p)
        self.assertEqual(state["messages"], {})

    def test_save_and_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "state.json"
            dmon._save_state(p, {"version": 1, "messages": {"m1": {"status": "published"}}})
            state = dmon._load_state(p)
            leftovers = list(Path(tmp).glob("*.tmp"))
        self.assertEqual(state["messages"]["m1"]["status"], "published")
        self.assertEqual(leftovers, [])

    def test_mark_processed_statuses(self):
        state = {"messages": {}}
        dmon._mark_processed(state, "m1", dyn_id="DY1")       # published
        dmon._mark_processed(state, "m2", error="boom")       # failed
        dmon._mark_processed(state, 12345)                    # id 强制 str + processed
        self.assertEqual(state["messages"]["m1"]["status"], "published")
        self.assertEqual(state["messages"]["m1"]["bilibili_dyn_id"], "DY1")
        self.assertEqual(state["messages"]["m2"]["status"], "failed")
        self.assertEqual(state["messages"]["m2"]["error"], "boom")
        self.assertEqual(state["messages"]["12345"]["status"], "processed")
        self.assertTrue(dmon._is_processed(state, "m1"))
        self.assertTrue(dmon._is_processed(state, "12345"))
        self.assertFalse(dmon._is_processed(state, "missing"))


class ChannelIdsTests(unittest.TestCase):
    def test_empty(self):
        with patch.object(config, "DISCORD_CHANNEL_IDS", ""):
            self.assertEqual(dmon._channel_ids(), [])

    def test_parses_int_list(self):
        with patch.object(config, "DISCORD_CHANNEL_IDS", "123, 456,abc, ,789"):
            self.assertEqual(dmon._channel_ids(), [123, 456, 789])

    def test_single(self):
        with patch.object(config, "DISCORD_CHANNEL_IDS", "42"):
            self.assertEqual(dmon._channel_ids(), [42])


class ParseMessageTests(unittest.TestCase):
    """_parse_message：Discord API 消息 → 内部格式。"""

    def test_missing_id_returns_none(self):
        self.assertIsNone(dmon._parse_message({"author": {}}))

    def test_collects_attachments_and_embeds(self):
        raw = {
            "id": "m1",
            "channel_id": "c1",
            "content": "  hello  ",
            "author": {"global_name": "Alice", "username": "alice", "bot": False},
            "timestamp": "2026-01-01T00:00:00.000Z",
            "attachments": [{"url": "https://cdn/a.png"}, {"url": ""}],
            "embeds": [
                {"image": {"url": "https://cdn/embed.png"}},
                {"thumbnail": {"url": "https://cdn/thumb.png"}},
                {},
            ],
        }
        parsed = dmon._parse_message(raw)
        self.assertEqual(parsed["message_id"], "m1")
        self.assertEqual(parsed["content"], "hello")  # strip
        self.assertEqual(parsed["author_name"], "Alice")  # global_name 优先
        self.assertFalse(parsed["author_bot"])
        self.assertEqual(parsed["attachment_urls"],
                         ["https://cdn/a.png", "https://cdn/embed.png",
                          "https://cdn/thumb.png"])

    def test_author_bot_detected(self):
        raw = {"id": "m2", "author": {"username": "bot", "bot": True},
               "attachments": [], "embeds": []}
        self.assertTrue(dmon._parse_message(raw)["author_bot"])

    def test_username_fallback(self):
        raw = {"id": "m3", "author": {"username": "anon"},
               "attachments": [], "embeds": []}
        self.assertEqual(dmon._parse_message(raw)["author_name"], "anon")


class BuildDynamicTextTests(unittest.TestCase):
    """_build_dynamic_text：翻译文本 + 署名。"""

    def test_with_translation(self):
        msg = dpub.DiscordMessage(
            message_id="1", channel_id="c", channel_name="chan",
            author_name="Author", content="原文")
        text = dpub._build_dynamic_text("翻译内容", msg)
        self.assertIn("翻译内容", text)

    def test_empty_translation(self):
        msg = dpub.DiscordMessage(
            message_id="1", channel_id="c", channel_name="chan",
            author_name="Author", content="原文")
        self.assertEqual(dpub._build_dynamic_text("", msg), "")


if __name__ == "__main__":
    unittest.main()
