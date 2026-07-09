"""
Batch subtitle translator using the DeepSeek OpenAI-compatible API.

Unlike the title translator (``yt2bili.translation.translator``), this module:
- Translates multiple subtitle segments in a single API call (batch mode).
- Uses an index-based input/output format (``index|text``) with count verification.
- Applies the SNAP glossary to pre-replace card/location names before translation.
- Does NOT use term protection/restore placeholders (not needed for subtitles).
- Always uses DeepSeek (not configurable per-provider).
"""

from openai import OpenAI

from yt2bili import config
from yt2bili.translation.translator import _apply_glossary
from .parser import Cue

# ── Prompt templates ──────────────────────────────────────────────────

_SYSTEM_PROMPT = """\
You are a professional subtitle translator. Translate the following \
subtitle segments from their source language to {target_lang}.

Rules:
1. Each line is formatted as NUMBER|TEXT. Translate ONLY the TEXT part.
2. Keep the NUMBER| prefix exactly the same — do not change numbers.
3. Return EXACTLY the same number of lines as the input.
4. Preserve the original tone and style. Make the translation natural.
5. Do NOT add explanations, notes, or extra text.
6. If a line is already in the target language or is untranslatable \
(sound effects, names), keep it as-is but still output NUMBER|TEXT."""

_USER_PREFIX = "Translate these subtitle segments to {target_lang}:\n\n"


def _build_client() -> OpenAI:
    """Create an OpenAI client configured for the DeepSeek endpoint."""
    kwargs: dict = dict(
        api_key=config.DEEPSEEK_API_KEY,
        base_url=config.DEEPSEEK_BASE_URL,
    )

    # If TRANSLATION_PROXY is set, route through it
    proxy = config.TRANSLATION_PROXY
    if proxy:
        import httpx
        kwargs["http_client"] = httpx.Client(proxy=proxy)

    return OpenAI(**kwargs)


def _format_batch(batch: list[Cue]) -> str:
    """Format a batch of cues as ``index|text`` lines for the API."""
    return "\n".join(f"{cue.index}|{cue.text}" for cue in batch)


def _parse_batch_response(raw: str, expected_count: int) -> list[tuple[int, str]]:
    """
    Parse the model's response back into (index, text) pairs.

    Args:
        raw: Raw response text from the model.
        expected_count: Number of entries expected.

    Returns:
        List of (index, translated_text) tuples.  If count mismatches
        a warning is printed and the best-effort result returned.
    """
    lines = [l.strip() for l in raw.strip().splitlines() if l.strip()]
    result: list[tuple[int, str]] = []

    for line in lines:
        # Skip lines that don't match the "number|text" format
        if "|" not in line:
            continue
        try:
            idx_str, text = line.split("|", 1)
            idx = int(idx_str.strip())
            result.append((idx, text.strip()))
        except ValueError:
            continue

    if len(result) != expected_count:
        print(
            f"[字幕] [WARN] 翻译批次返回 {len(result)} 条，"
            f"预期 {expected_count} 条"
        )

    return result


def _translate_batch(
    client: OpenAI,
    batch: list[Cue],
    batch_num: int,
    total_batches: int,
) -> list[Cue]:
    """
    Translate one batch of subtitle cues via DeepSeek.

    Args:
        client: OpenAI client pointing at DeepSeek.
        batch: Cues to translate (typically up to ``batch_size`` items).
        batch_num: 1-based batch number (for logging).
        total_batches: Total number of batches (for logging).

    Returns:
        List of Cue objects with translated text.  On partial failure,
        untranslated segments retain their original text.
    """
    target = config.SUBTITLE_TARGET_LANG

    # Apply SNAP glossary: pre-replace English card/location names with
    # official Chinese translations so DeepSeek uses the correct terms.
    batch_with_glossary: list[Cue] = []
    for cue in batch:
        replaced_text = _apply_glossary(cue.text)
        batch_with_glossary.append(
            Cue(index=cue.index, start=cue.start, end=cue.end, text=replaced_text)
        )

    input_text = _format_batch(batch_with_glossary)
    prompt = _USER_PREFIX.format(target_lang=target) + input_text

    extra_body: dict = {}
    if config.DEEPSEEK_THINKING == "enabled":
        extra_body = {"thinking": {"type": "enabled"}}

    max_tokens = max(4096, len(batch) * 150)

    print(
        f"[字幕] 翻译批次 {batch_num}/{total_batches} "
        f"({len(batch)} 条) ... ",
        end="",
        flush=True,
    )

    try:
        response = client.chat.completions.create(
            model=config.DEEPSEEK_MODEL,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT.format(target_lang=target)},
                {"role": "user", "content": prompt},
            ],
            temperature=0.3,
            max_tokens=max_tokens,
            extra_body=extra_body if extra_body else None,
        )
    except Exception as e:
        print(f"失败: {e}")
        # Return cues with original text on API failure
        return [
            Cue(index=c.index, start=c.start, end=c.end, text=c.text)
            for c in batch
        ]

    raw = response.choices[0].message.content or ""
    parsed = _parse_batch_response(raw, len(batch))

    # Build lookup from index → translated text
    trans_map: dict[int, str] = {idx: text for idx, text in parsed}

    results: list[Cue] = []
    for cue in batch:
        translated = trans_map.get(cue.index, cue.text)
        results.append(
            Cue(index=cue.index, start=cue.start, end=cue.end, text=translated)
        )

    translated_count = sum(1 for c in results if c.text != next(
        (orig.text for orig in batch if orig.index == c.index), c.text
    ))
    print(f"完成 ({translated_count}/{len(batch)} 条已翻译)")

    return results


def translate_cues(
    cues: list[Cue],
    batch_size: int | None = None,
) -> list[Cue]:
    """
    Translate subtitle cue text in batches using the DeepSeek API.

    The translation is done in batches.  Each batch's input and output
    use an index-based format so ordering is preserved even if the model
    reorders lines.

    Args:
        cues: Source cues with original-language text.
        batch_size: Number of cues per API batch (defaults to
            ``config.SUBTITLE_TRANSLATE_BATCH_SIZE``).

    Returns:
        New list of Cue objects with translated text.  Timing and index
        values are preserved from the input.

    Raises:
        RuntimeError: If cues are empty or the DeepSeek API key is missing.
    """
    if not cues:
        raise RuntimeError("没有字幕可翻译")

    if not config.DEEPSEEK_API_KEY:
        raise RuntimeError("DEEPSEEK_API_KEY 未设置，无法翻译字幕")

    bs = batch_size if batch_size is not None else config.SUBTITLE_TRANSLATE_BATCH_SIZE
    if bs < 1:
        bs = 80

    # Split into batches
    batches: list[list[Cue]] = []
    for i in range(0, len(cues), bs):
        batches.append(cues[i:i + bs])

    total = len(batches)
    print(f"[字幕] 共 {len(cues)} 条字幕，分 {total} 批翻译")

    client = _build_client()
    translated: list[Cue] = []

    for i, batch in enumerate(batches, start=1):
        result = _translate_batch(client, batch, i, total)
        translated.extend(result)

    # Sort by original index to ensure correct ordering
    translated.sort(key=lambda c: c.index)

    print(f"[字幕] 翻译完成: {len(translated)} 条")
    return translated
