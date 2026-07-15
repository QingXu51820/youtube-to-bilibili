"""
Batch subtitle translator using the DeepSeek OpenAI-compatible API.

Unlike the title translator (``yt2bili.translation.translator``), this module:
- Translates multiple subtitle segments in a single API call (batch mode).
- Uses an index-based input/output format (``index|text``) with count verification.
- Applies the SNAP glossary to pre-replace card/location names before translation.
- Does NOT use term protection/restore placeholders (not needed for subtitles).
- Always uses DeepSeek (not configurable per-provider).
"""

import sys

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
3. Return EXACTLY the same number of lines as the input. NO EXCEPTIONS.
4. ONE translation per line — never merge multiple segments into one line.
5. Preserve the original tone and style. Make the translation natural.
6. Do NOT add explanations, notes, or extra text.
7. If a line is already in the target language or is untranslatable \
(sound effects, names), keep it as-is but still output NUMBER|TEXT.
8. CRITICAL: Every NUMBER from the input MUST appear once as a NUMBER| prefix in the output."""


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
            f"预期 {expected_count} 条",
            flush=True, file=sys.stderr,
        )

    return result


def _is_untranslated(text: str) -> bool:
    """
    Check if text appears to be untranslated (mostly ASCII alphabetic chars).

    For Chinese-target translations, a cue that is mostly Latin letters
    was likely not translated by the model.
    """
    if not text:
        return False
    alpha_chars = [c for c in text if c.isalpha()]
    if not alpha_chars:
        return False
    ascii_alpha = sum(1 for c in alpha_chars if c.isascii())
    return ascii_alpha / len(alpha_chars) > 0.7


def _needs_retranslate(cue: Cue) -> bool:
    """
    Check if a translated cue needs re-translation.

    Detects two failure modes from batch translation:
    1. Oversized text (>200 chars) — model merged multiple translations.
    2. Still in English — model skipped this cue.
    """
    if len(cue.text) > 200:
        return True
    if _is_untranslated(cue.text):
        return True
    return False


def _retranslate_small_batch(
    client: OpenAI,
    cues: list[Cue],
    batch_num: int,
    total_batches: int,
) -> list[Cue]:
    """
    Re-translate a small batch of problematic cues.

    Uses a stricter prompt and very small batch to maximize format compliance.
    Falls back to original text on any failure.
    """
    if not cues:
        return []

    target = config.SUBTITLE_TARGET_LANG

    # Apply glossary
    batch_with_glossary: list[Cue] = []
    for cue in cues:
        replaced_text = _apply_glossary(cue.text)
        batch_with_glossary.append(
            Cue(index=cue.index, start=cue.start, end=cue.end, text=replaced_text)
        )

    input_text = _format_batch(batch_with_glossary)
    prompt = _USER_PREFIX.format(target_lang=target) + input_text

    extra_body: dict = {}
    if config.DEEPSEEK_THINKING == "enabled":
        extra_body = {"thinking": {"type": "enabled"}}

    max_tokens = max(1024, len(cues) * 200)

    try:
        response = client.chat.completions.create(
            model=config.DEEPSEEK_MODEL,
            messages=[
                {
                    "role": "system",
                    "content": _SYSTEM_PROMPT.format(target_lang=target)
                    + "\nThis is a re-translation of previously failed segments. "
                    + "Be EXTREMELY careful with the NUMBER|TEXT format.",
                },
                {"role": "user", "content": prompt},
            ],
            temperature=0.1,  # lower temperature for more deterministic output
            max_tokens=max_tokens,
            extra_body=extra_body if extra_body else None,
        )
    except Exception as e:
        print(f"  重试失败: {e}", flush=True, file=sys.stderr)
        return [
            Cue(index=c.index, start=c.start, end=c.end, text=c.text)
            for c in cues
        ]

    raw = response.choices[0].message.content or ""
    parsed = _parse_batch_response(raw, len(cues))
    trans_map: dict[int, str] = {idx: text for idx, text in parsed}

    results: list[Cue] = []
    for cue in cues:
        translated = trans_map.get(cue.index, cue.text)
        results.append(
            Cue(index=cue.index, start=cue.start, end=cue.end, text=translated)
        )

    return results


def _validate_and_retry(
    client: OpenAI,
    results: list[Cue],
    original_batch: list[Cue],
    batch_num: int,
    total_batches: int,
) -> list[Cue]:
    """
    Validate translated cues and re-translate any problematic ones.

    Returns corrected list of cues (same length and order as *results*).
    """
    # Find cues that need re-translation
    failed_indices: set[int] = set()
    for i, cue in enumerate(results):
        if _needs_retranslate(cue):
            failed_indices.add(i)

    if not failed_indices:
        return results

    # Collect original cues that failed
    failed_cues: list[Cue] = []
    for i in failed_indices:
        if i < len(original_batch):
            failed_cues.append(original_batch[i])

    if not failed_cues:
        return results

    n_failed = len(failed_cues)
    n_oversized = sum(1 for i in failed_indices if len(results[i].text) > 200)
    n_untrans = n_failed - n_oversized

    print(
        f"  [WARN] {n_failed} 条异常"
        + (f"（{n_oversized} 条合并, {n_untrans} 条未翻译）" if n_oversized else "")
        + f"，逐个重试...",
        flush=True, file=sys.stderr,
    )

    # Re-translate in very small groups (5 at a time) for speed
    retry_batch_size = 5
    retry_map: dict[int, str] = {}

    for start in range(0, len(failed_cues), retry_batch_size):
        group = failed_cues[start:start + retry_batch_size]
        retried = _retranslate_small_batch(client, group, batch_num, total_batches)
        for cue in retried:
            retry_map[cue.index] = cue.text

    # Merge retry results back
    corrected = 0
    for cue in results:
        if cue.index in retry_map:
            new_text = retry_map[cue.index]
            if not _needs_retranslate(
                Cue(index=cue.index, start=0, end=0, text=new_text)
            ):
                cue.text = new_text
                corrected += 1

    if corrected > 0:
        print(f"  重试修复 {corrected}/{n_failed} 条", flush=True, file=sys.stderr)
    else:
        print(f"  重试未能修复，保留原文", flush=True, file=sys.stderr)

    return results


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

    # Print BEFORE glossary/processing so user sees immediate feedback.
    # (Glossary loads from network on first call — can take seconds.)
    print(
        f"[字幕] 翻译批次 {batch_num}/{total_batches} "
        f"({len(batch)} 条) 开始...",
        flush=True,
        file=sys.stderr,
    )

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
        print(f"失败: {e}", flush=True, file=sys.stderr)
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
    print(f"[字幕] 批次 {batch_num}/{total_batches} 完成 ({translated_count}/{len(batch)} 条已翻译)", flush=True, file=sys.stderr)

    # Validate and retry problematic cues
    results = _validate_and_retry(client, results, batch, batch_num, total_batches)

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
        bs = 30

    workers = max(1, int(getattr(config, "SUBTITLE_TRANSLATE_WORKERS", 3) or 3))

    # Split into batches
    batches: list[list[Cue]] = []
    for i in range(0, len(cues), bs):
        batches.append(cues[i:i + bs])

    total = len(batches)
    print(f"[字幕] 共 {len(cues)} 条字幕，分 {total} 批翻译 ({workers} 线程并行)", flush=True, file=sys.stderr)

    translated: list[Cue] = []

    if total <= 1 or workers <= 1:
        # Single batch or single worker — sequential
        client = _build_client()
        for i, batch in enumerate(batches, start=1):
            result = _translate_batch(client, batch, i, total)
            translated.extend(result)
    else:
        from concurrent.futures import ThreadPoolExecutor, as_completed

        def _do_batch(batch: list[Cue], num: int) -> list[Cue]:
            """Each thread gets its own client (not safe to share across threads)."""
            client = _build_client()
            return _translate_batch(client, batch, num, total)

        with ThreadPoolExecutor(max_workers=min(workers, total)) as executor:
            futures = {
                executor.submit(_do_batch, batch, i): i
                for i, batch in enumerate(batches, start=1)
            }
            for future in as_completed(futures):
                result = future.result()
                translated.extend(result)

    # Sort by original index to ensure correct ordering
    translated.sort(key=lambda c: c.index)

    print(f"[字幕] 翻译完成: {len(translated)} 条", flush=True, file=sys.stderr)
    return translated
