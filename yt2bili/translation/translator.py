"""
Translation module for YouTube video titles.
Supports Google Translate, OpenAI, and DeepSeek.
"""

from abc import ABC, abstractmethod
import re

from yt2bili import config


TRAILING_HASHTAG_RE = re.compile(r"(?:\s+#[-\w\u4e00-\u9fff]+)+\s*$", re.IGNORECASE)


def strip_trailing_hashtags(text: str) -> str:
    """Remove YouTube title hashtags at the end of a title."""
    cleaned = TRAILING_HASHTAG_RE.sub("", text or "").strip()
    cleaned = re.sub(r"\s+([|｜\-–—:：])\s*$", "", cleaned).strip()
    return cleaned


def clean_title(text: str) -> str:
    """Clean up translated titles for B站 display."""
    text = re.sub(r'\s+', ' ', text or '').strip()
    text = text.replace('\n', ' ').replace('\r', '')
    text = text.strip('"\'').strip()
    text = strip_trailing_hashtags(text)
    if len(text) > 80:
        text = text[:77] + '...'
    return text


def _translation_proxy() -> str:
    """Proxy for translation providers."""
    return getattr(config, "TRANSLATION_PROXY", "") or getattr(config, "YOUTUBE_PROXY", "")


def _requests_proxies() -> dict[str, str] | None:
    proxy = _translation_proxy()
    if not proxy:
        return None
    return {"http": proxy, "https": proxy}


def _openai_http_client():
    proxy = _translation_proxy()
    if not proxy:
        return None
    import httpx
    return httpx.Client(proxy=proxy, timeout=max(10, int(getattr(config, "YOUTUBE_HTTP_TIMEOUT", 60) or 60)))


def _preserve_terms() -> list[str]:
    """Terms that should stay unchanged in translated titles."""
    raw = config.TRANSLATION_PRESERVE_TERMS or ""
    terms = [term.strip() for term in raw.split(",") if term.strip()]
    return sorted(dict.fromkeys(terms), key=len, reverse=True)


def _protect_terms(text: str) -> tuple[str, dict[str, str]]:
    """Replace protected terms with placeholders before translation."""
    replacements: dict[str, str] = {}
    protected = text
    for index, term in enumerate(_preserve_terms()):
        placeholder = f"__YT2BILI_TERM_{index}__"
        pattern = re.compile(rf"(?<!\w){re.escape(term)}(?!\w)", re.IGNORECASE)
        protected, count = pattern.subn(placeholder, protected)
        if count:
            replacements[placeholder] = term
    return protected, replacements


def _restore_terms(text: str, replacements: dict[str, str]) -> str:
    """Restore protected terms after translation."""
    restored = text
    for placeholder, term in replacements.items():
        restored = re.sub(re.escape(placeholder), term, restored, flags=re.IGNORECASE)
        # Some models insert spaces around underscores.
        spaced = " ".join(placeholder)
        restored = re.sub(re.escape(spaced), term, restored, flags=re.IGNORECASE)
    return restored


def _prepare_source_title(text: str) -> str:
    """Prepare the source title before sending it to translators."""
    return strip_trailing_hashtags(text)


def _translation_prompt(target_lang: str) -> str:
    preserve_terms = _preserve_terms()
    rules = [
        "保持原标题的吸引力和风格",
        "符合中文表达习惯，不要生硬直译",
        "长度控制在80个字符以内",
        "只输出翻译结果，不要任何解释",
    ]
    if preserve_terms:
        rules.append(
            "以下专有名词必须原样保留，不要翻译、不要改大小写："
            f"{'、'.join(preserve_terms)}"
        )
    if config.TRANSLATION_EXTRA_PROMPT.strip():
        rules.append(f"额外要求：{config.TRANSLATION_EXTRA_PROMPT.strip()}")
    rules.append("如果输入中出现形如 __YT2BILI_TERM_0__ 的占位符，必须逐字原样保留")
    formatted_rules = "\n".join(f"{index}. {rule}" for index, rule in enumerate(rules, start=1))
    return (
        f"你是一个专业的YouTube视频标题翻译助手。\n"
        f"将输入的标题翻译成{target_lang}，要求：\n"
        f"{formatted_rules}"
    )


def _extract_chat_content(response, provider: str) -> str:
    """Extract and validate translated content from a chat completion."""
    if not response.choices:
        raise RuntimeError(f"{provider} 没有返回 choices")

    choice = response.choices[0]
    message = choice.message
    result = clean_title(message.content or "")
    if result:
        return result

    reasoning = getattr(message, "reasoning_content", None)
    finish_reason = getattr(choice, "finish_reason", "")
    detail = f"finish_reason={finish_reason or 'unknown'}"
    if reasoning:
        detail += "，模型只返回了 reasoning_content，未返回最终翻译"
    raise RuntimeError(f"{provider} 翻译结果为空（{detail}）")


class BaseTranslator(ABC):
    """Abstract translator interface."""

    @abstractmethod
    def translate(self, text: str, source_lang: str = "auto", target_lang: str = "zh-CN") -> str:
        """Translate text to target language."""
        ...


# ── Google Translator (free, no API key) ──────────────────────────

class GoogleTranslator(BaseTranslator):
    """Free translation using deep-translator (Google Translate backend)."""

    def __init__(self):
        from deep_translator import GoogleTranslator as _GoogleTranslator
        self._translator = _GoogleTranslator

    def translate(self, text: str, source_lang: str = "auto", target_lang: str = "zh-CN") -> str:
        if not text.strip():
            return text
        try:
            source_text = _prepare_source_title(text)
            protected_text, replacements = _protect_terms(source_text)
            translator = self._translator(
                source=source_lang,
                target=target_lang,
                proxies=_requests_proxies(),
            )
            result = translator.translate(protected_text)
            return clean_title(_restore_terms(result, replacements))
        except Exception as e:
            print(f"[翻译] Google 翻译失败: {e}")
            raise RuntimeError(f"Google 翻译失败: {e}") from e


# ── OpenAI Translator (OpenAI API or compatible endpoints) ────────

class OpenAITranslator(BaseTranslator):
    """Translation using OpenAI API or another OpenAI-compatible endpoint."""

    def __init__(self):
        from openai import OpenAI
        http_client = _openai_http_client()
        self._client = OpenAI(
            api_key=config.OPENAI_API_KEY,
            base_url=config.OPENAI_BASE_URL or None,
            http_client=http_client,
        )
        self._model = config.OPENAI_MODEL

    def translate(self, text: str, source_lang: str = "auto", target_lang: str = "zh-CN") -> str:
        if not text.strip():
            return text

        try:
            source_text = _prepare_source_title(text)
            protected_text, replacements = _protect_terms(source_text)
            response = self._client.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": _translation_prompt(target_lang)},
                    {"role": "user", "content": protected_text},
                ],
                temperature=0.3,
                max_tokens=200,
            )
            return clean_title(_restore_terms(_extract_chat_content(response, "OpenAI"), replacements))
        except Exception as e:
            print(f"[翻译] OpenAI 翻译失败: {e}")
            raise RuntimeError(f"OpenAI 翻译失败: {e}") from e


# ── DeepSeek Translator (OpenAI-compatible API) ───────────────────

class DeepSeekTranslator(BaseTranslator):
    """Translation using DeepSeek's OpenAI-compatible API."""

    def __init__(self):
        from openai import OpenAI
        http_client = _openai_http_client()
        self._client = OpenAI(
            api_key=config.DEEPSEEK_API_KEY,
            base_url=config.DEEPSEEK_BASE_URL,
            http_client=http_client,
        )
        self._model = config.DEEPSEEK_MODEL

    def translate(self, text: str, source_lang: str = "auto", target_lang: str = "zh-CN") -> str:
        if not text.strip():
            return text

        try:
            source_text = _prepare_source_title(text)
            protected_text, replacements = _protect_terms(source_text)
            response = self._client.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": _translation_prompt(target_lang)},
                    {"role": "user", "content": protected_text},
                ],
                temperature=0.2,
                max_tokens=512,
                extra_body={"thinking": {"type": config.DEEPSEEK_THINKING}},
            )
            return clean_title(_restore_terms(_extract_chat_content(response, "DeepSeek"), replacements))
        except Exception as e:
            print(f"[翻译] DeepSeek 翻译失败: {e}")
            raise RuntimeError(f"DeepSeek 翻译失败: {e}") from e


# ── Factory ───────────────────────────────────────────────────────

_translator_instance: BaseTranslator | None = None


def get_translator() -> BaseTranslator:
    """Get or create translator instance based on config."""
    global _translator_instance
    if _translator_instance is not None:
        return _translator_instance

    provider = config.TRANSLATE_PROVIDER.lower()
    if provider == "openai":
        print("[翻译] 使用 OpenAI 翻译")
        _translator_instance = OpenAITranslator()
    elif provider == "deepseek":
        print("[翻译] 使用 DeepSeek 翻译")
        _translator_instance = DeepSeekTranslator()
    else:
        print("[翻译] 使用 Google 翻译（免费）")
        _translator_instance = GoogleTranslator()

    return _translator_instance


def translate(text: str, source_lang: str = "auto", target_lang: str = "zh-CN") -> str:
    """Convenience function: translate text using configured backend."""
    return get_translator().translate(text, source_lang, target_lang)


def classify_content(title: str, description: str, keywords: str) -> bool:
    """Return True if the video is relevant to the given keywords (via DeepSeek)."""
    from openai import OpenAI
    import httpx

    proxy = _translation_proxy()
    http_client = None
    if proxy:
        timeout = max(10, int(getattr(config, "YOUTUBE_HTTP_TIMEOUT", 60) or 60))
        http_client = httpx.Client(proxy=proxy, timeout=timeout)

    client = OpenAI(
        api_key=config.DEEPSEEK_API_KEY,
        base_url=config.DEEPSEEK_BASE_URL,
        http_client=http_client,
    )

    prompt = (
        "你是一个内容审核助手。根据视频标题和简介，判断该视频是否与以下主题相关。\n"
        f"主题：{keywords}\n"
        "如果相关，只回复 YES；如果不相关，只回复 NO。不要回复其他内容。"
    )

    desc_snippet = (description or "")[:500]
    user_message = f"标题：{title}\n\n简介：{desc_snippet}" if desc_snippet else f"标题：{title}"

    response = client.chat.completions.create(
        model=config.DEEPSEEK_MODEL,
        messages=[
            {"role": "system", "content": prompt},
            {"role": "user", "content": user_message},
        ],
        temperature=0,
        max_tokens=10,
    )

    result = (response.choices[0].message.content or "").strip().upper()
    return "YES" in result
