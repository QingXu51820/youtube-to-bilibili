"""自测：标题翻译（占位符保护/恢复、截断时机、翻译器全链路）。"""

import sys
import types
import unittest
from unittest.mock import patch

from yt2bili import config
from yt2bili.translation import translator


class StripHashtagTests(unittest.TestCase):
    def test_strips_trailing_hashtags(self):
        self.assertEqual(
            translator.strip_trailing_hashtags("Marvel SNAP New Card #marvelsnap #shorts"),
            "Marvel SNAP New Card",
        )

    def test_strips_chinese_hashtags(self):
        self.assertEqual(
            translator.strip_trailing_hashtags("新手教程 #新手指南"),
            "新手教程",
        )

    def test_keeps_hashtag_in_middle(self):
        self.assertEqual(
            translator.strip_trailing_hashtags("Marvel SNAP #update New Card"),
            "Marvel SNAP #update New Card",
        )

    def test_strips_trailing_separator(self):
        self.assertEqual(translator.strip_trailing_hashtags("标题测试 |"), "标题测试")

    def test_none_input(self):
        self.assertEqual(translator.strip_trailing_hashtags(None), "")


class CleanTitleTests(unittest.TestCase):
    def test_collapses_whitespace_and_newlines(self):
        self.assertEqual(
            translator.clean_title("a\n  b\tc"), "a b c"
        )

    def test_strips_quotes(self):
        self.assertEqual(translator.clean_title('"标题"'), "标题")

    def test_truncates_above_80(self):
        text = "x" * 81
        result = translator.clean_title(text)
        self.assertEqual(len(result), 80)
        self.assertTrue(result.endswith("..."))

    def test_short_text_unchanged(self):
        self.assertEqual(translator.clean_title("短标题"), "短标题")


class ProtectRestoreTermsTests(unittest.TestCase):
    """占位符保护/恢复 —— 回归：__YT2BILI_TERM_N__ 不得残留进标题。"""

    def setUp(self):
        # 固定词表，避免 _preserve_terms 触发 glossary 网络加载
        patcher = patch.object(translator, "_preserve_terms", return_value=[
            "Marvel SNAP", "SNAP"
        ])
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_roundtrip_restores_term(self):
        text = "Marvel SNAP new patch notes"
        protected, replacements = translator._protect_terms(text)
        self.assertIn("__YT2BILI_TERM_", protected)
        self.assertNotIn("Marvel SNAP", protected)

        restored = translator._restore_terms(protected, replacements)
        self.assertEqual(restored, text)

    def test_restore_tolerates_spaces_inserted_by_model(self):
        protected, replacements = translator._protect_terms("Marvel SNAP is great")
        mangled = protected.replace("_TERM", "_ TERM")  # 模型在下划线后插入空格
        restored = translator._restore_terms(mangled, replacements)
        self.assertEqual(restored, "Marvel SNAP is great")

    def test_restore_tolerates_case_changes(self):
        protected, replacements = translator._protect_terms("Marvel SNAP is great")
        lowered = protected.lower()
        restored = translator._restore_terms(lowered, replacements)
        self.assertEqual(restored, "Marvel SNAP is great")

    def test_no_placeholder_residue_after_full_pipeline(self):
        """回归：标题超过 80 字符、占位符恰好跨过截断点。
        截断必须发生在占位符恢复之后（clean_title 内），否则恢复失败、
        __YT2BILI_TERM_N__ 残留进最终标题。"""
        filler = "测" * 64  # 64 + " Marvel SNAP 新卡预览"(17) = 81 字符 → 触发截断
        text = f"{filler} Marvel SNAP 新卡预览"
        self.assertGreater(len(text), 80)

        protected, replacements = translator._protect_terms(text)
        # 模拟模型原样回显（占位符在截断点附近）
        result = translator._restore_terms(protected, replacements)
        result = translator._cleanup_cjk_spaces(result)
        result = translator.clean_title(result)

        self.assertNotIn("__YT2BILI_TERM", result)
        self.assertIn("Marvel SNAP", result)
        self.assertTrue(result.endswith("..."))

    def test_placeholder_at_very_end_still_restored(self):
        text = "A" * 60 + " " + "Marvel SNAP"  # 72 字符，无需截断
        protected, replacements = translator._protect_terms(text)
        result = translator.clean_title(translator._restore_terms(protected, replacements))
        self.assertNotIn("__YT2BILI_TERM", result)
        self.assertIn("Marvel SNAP", result)
        self.assertEqual(result, text)


class ExtractChatContentTests(unittest.TestCase):
    def _response(self, content=None, reasoning=None, finish="stop"):
        message = types.SimpleNamespace(content=content, reasoning_content=reasoning)
        choice = types.SimpleNamespace(message=message, finish_reason=finish)
        return types.SimpleNamespace(choices=[choice])

    def test_returns_untruncated_content(self):
        """回归：_extract_chat_content 不得在占位符恢复前截断。"""
        long_content = "译" * 120
        result = translator._extract_chat_content(self._response(long_content), "DeepSeek")
        self.assertEqual(result, long_content)
        self.assertGreater(len(result), 80)

    def test_empty_choices_raises(self):
        with self.assertRaises(RuntimeError):
            translator._extract_chat_content(types.SimpleNamespace(choices=[]), "DeepSeek")

    def test_empty_content_with_reasoning_raises_with_detail(self):
        with self.assertRaises(RuntimeError) as ctx:
            translator._extract_chat_content(
                self._response(content="", reasoning="思考中...", finish="length"),
                "DeepSeek",
            )
        self.assertIn("reasoning_content", str(ctx.exception))

    def test_strips_surrounding_whitespace(self):
        result = translator._extract_chat_content(self._response("  译文  \n"), "OpenAI")
        self.assertEqual(result, "译文")


class DeepSeekTranslatorPipelineTests(unittest.TestCase):
    """DeepSeek 翻译全链路（glossary → 保护 → API → 恢复 → 清理 → 截断）。"""

    def _make_translator(self, client):
        t = object.__new__(translator.DeepSeekTranslator)
        t._client = client
        t._model = "test-model"
        return t

    def _fake_client(self, reply):
        def _create(**kwargs):
            message = types.SimpleNamespace(content=reply)
            choice = types.SimpleNamespace(message=message)
            return types.SimpleNamespace(choices=[choice])
        return types.SimpleNamespace(
            chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=_create))
        )

    def test_translate_restores_terms_and_cleans(self):
        with patch.object(translator, "_preserve_terms", return_value=["Marvel SNAP"]), \
             patch.object(translator.config, "SNAP_GLOSSARY_ENABLED", False), \
             patch.object(translator.config, "DEADLOCK_GLOSSARY_ENABLED", False), \
             patch.object(translator.config, "DEEPSEEK_THINKING", "disabled"):
            t = self._make_translator(self._fake_client("Marvel SNAP 新卡评测"))
            result = t.translate("Marvel SNAP card review")

        self.assertNotIn("__YT2BILI_TERM", result)
        self.assertIn("Marvel SNAP", result)

    def test_translate_api_error_raises_runtime_error(self):
        def _create(**kwargs):
            raise RuntimeError("network down")
        client = types.SimpleNamespace(
            chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=_create))
        )
        with patch.object(translator, "_preserve_terms", return_value=[]), \
             patch.object(translator.config, "SNAP_GLOSSARY_ENABLED", False), \
             patch.object(translator.config, "DEADLOCK_GLOSSARY_ENABLED", False), \
             patch.object(translator.config, "DEEPSEEK_THINKING", "disabled"):
            t = self._make_translator(client)
            with self.assertRaises(RuntimeError) as ctx:
                t.translate("some title")
        self.assertIn("DeepSeek 翻译失败", str(ctx.exception))

    def test_empty_input_returns_as_is(self):
        with patch.object(translator.config, "SNAP_GLOSSARY_ENABLED", False):
            t = self._make_translator(self._fake_client("不会调用"))
            self.assertEqual(t.translate(""), "")
            self.assertEqual(t.translate("   "), "   ")

    def test_google_translator_source_lang_passed(self):
        """source_lang 只对 Google 生效 —— 验证参数确实被传给 GoogleTranslator。"""
        captured = {}

        class FakeGT:
            def __init__(self, **kwargs):
                captured.update(kwargs)

            def translate(self, text):
                return text

        with patch.dict(sys.modules, {"deep_translator": types.ModuleType("deep_translator")}):
            sys.modules["deep_translator"].GoogleTranslator = FakeGT
            gt = translator.GoogleTranslator()
            with patch.object(translator, "_preserve_terms", return_value=[]):
                gt.translate("hello world", source_lang="en", target_lang="zh-CN")

        self.assertEqual(captured["source"], "en")
        self.assertEqual(captured["target"], "zh-CN")


class ContentFilterHelperTests(unittest.TestCase):
    def test_keyword_phrase_match(self):
        self.assertEqual(
            translator._match_content_keyword("Mary Jane Marvel SNAP Card", "Marvel SNAP"),
            "Marvel SNAP",
        )

    def test_keyword_compact_hashtag_match(self):
        self.assertEqual(
            translator._match_content_keyword("The Pool Of Snap Packs #marvelsnap", "Marvel SNAP"),
            "Marvel SNAP",
        )

    def test_plain_snap_word_no_match(self):
        self.assertEqual(
            translator._match_content_keyword("The Pool Of Snap Packs", "Marvel SNAP"),
            "",
        )

    def test_no_partial_word_match(self):
        self.assertEqual(
            translator._match_content_keyword("Snappy the dog", "Snap"),
            "",
        )

    def test_parse_classification(self):
        self.assertTrue(translator._parse_content_classification("YES"))
        self.assertTrue(translator._parse_content_classification("YES."))
        self.assertTrue(translator._parse_content_classification("相关"))
        self.assertFalse(translator._parse_content_classification("NO"))
        self.assertFalse(translator._parse_content_classification("不相关"))
        self.assertIsNone(translator._parse_content_classification(""))
        self.assertIsNone(translator._parse_content_classification("maybe"))

    def test_cleanup_cjk_spaces(self):
        self.assertEqual(
            translator._cleanup_cjk_spaces("恶型怪 被削弱了"),
            "恶型怪被削弱了",
        )
        # 中英文之间的空格保留，纯 CJK 相邻的空格全部塌缩
        self.assertEqual(translator._cleanup_cjk_spaces("保持 英文 空格"), "保持英文空格")


class GetTranslatorTests(unittest.TestCase):
    """get_translator：按 TRANSLATE_PROVIDER 选择实现，单例缓存。"""

    def setUp(self):
        # 重置全局单例，避免测试间相互依赖
        patcher = patch.object(translator, "_translator_instance", None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_cached_instance_returned(self):
        fake = object()
        with patch.object(translator, "_translator_instance", fake):
            self.assertIs(translator.get_translator(), fake)

    def test_openai_provider(self):
        with patch.object(config, "TRANSLATE_PROVIDER", "openai"), \
             patch.object(config, "OPENAI_API_KEY", "fake-key"), \
             patch.object(config, "OPENAI_BASE_URL", ""):
            inst = translator.get_translator()
        self.assertIsInstance(inst, translator.OpenAITranslator)

    def test_deepseek_provider(self):
        with patch.object(config, "TRANSLATE_PROVIDER", "deepseek"), \
             patch.object(config, "DEEPSEEK_API_KEY", "fake-key"), \
             patch.object(config, "DEEPSEEK_BASE_URL", ""):
            inst = translator.get_translator()
        self.assertIsInstance(inst, translator.DeepSeekTranslator)

    def test_unknown_provider_falls_back_to_google(self):
        with patch.object(config, "TRANSLATE_PROVIDER", "weird"):
            inst = translator.get_translator()
        self.assertIsInstance(inst, translator.GoogleTranslator)


if __name__ == "__main__":
    unittest.main()
