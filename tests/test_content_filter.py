import sys
import types
import unittest
from unittest.mock import patch

from yt2bili.translation import translator


class ContentFilterTests(unittest.TestCase):
    def _fake_openai_module(self, content=None, error=None):
        class FakeOpenAI:
            last_create_kwargs = None

            def __init__(self, *args, **kwargs):
                self.chat = types.SimpleNamespace(
                    completions=types.SimpleNamespace(create=self._create)
                )

            def _create(self, **kwargs):
                FakeOpenAI.last_create_kwargs = kwargs
                if error:
                    raise error
                message = types.SimpleNamespace(content=content)
                choice = types.SimpleNamespace(message=message)
                return types.SimpleNamespace(choices=[choice])

        module = types.ModuleType("openai")
        module.OpenAI = FakeOpenAI
        return module, FakeOpenAI

    def _classify_with_fake_ai(self, title, ai_content=None, ai_error=None):
        module, fake_openai = self._fake_openai_module(ai_content, ai_error)
        with patch.dict(sys.modules, {"openai": module}), patch.object(
            translator, "_translation_proxy", return_value=""
        ):
            result = translator.classify_content(title, "", "Marvel SNAP")
        return result, fake_openai

    def test_marvel_snap_title_keyword_passes_without_ai(self):
        with patch.dict(sys.modules, {"openai": None}):
            self.assertTrue(
                translator.classify_content(
                    "Mary Jane Marvel SNAP Card Review And Top Decks",
                    "",
                    "Marvel SNAP",
                )
            )

    def test_marvelsnap_hashtag_keyword_passes_without_ai(self):
        with patch.dict(sys.modules, {"openai": None}):
            self.assertTrue(
                translator.classify_content(
                    "The Pool Of Snap Packs #marvelsnap",
                    "",
                    "Marvel SNAP",
                )
            )

    def test_marvel_snap_keyword_does_not_match_plain_snap_word(self):
        result, fake_openai = self._classify_with_fake_ai("The Pool Of Snap Packs", "NO")

        self.assertFalse(result)
        self.assertIsNotNone(fake_openai.last_create_kwargs)

    def test_yes_with_punctuation_is_relevant(self):
        result, fake_openai = self._classify_with_fake_ai("Ambiguous game title", "YES.")

        self.assertTrue(result)
        self.assertEqual(
            {"thinking": {"type": translator.config.DEEPSEEK_THINKING}},
            fake_openai.last_create_kwargs["extra_body"],
        )

    def test_no_is_not_relevant(self):
        result, _ = self._classify_with_fake_ai("Ambiguous game title", "NO")

        self.assertFalse(result)

    def test_empty_ai_reply_is_allowed(self):
        result, _ = self._classify_with_fake_ai("Ambiguous game title", "")

        self.assertTrue(result)

    def test_ai_exception_is_allowed(self):
        result, _ = self._classify_with_fake_ai(
            "Ambiguous game title",
            ai_error=RuntimeError("network down"),
        )

        self.assertTrue(result)


if __name__ == "__main__":
    unittest.main()
