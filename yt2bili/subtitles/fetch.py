"""一条链接 → 字幕：下载 + 翻译 + 对齐时长（手动、一次性）。

监控流水线只在搬运视频时顺带产出字幕，``--subtitle-only`` 只轮询上传已经存在
的字幕，``tools/retranslate_subtitles.py`` 只处理已下载好的英文文件 —— 三者都
覆盖不到「手上就一条链接，要它的中文字幕」。本模块补这个口子：下载英文字幕、
按句子重分段、用锁定的游戏术语表翻译、把时间轴对齐到视频时长，产出
``{id}.en.srt`` 和 ``{id}.zh-CN.srt`` 到流水线同一个目录。

不下载视频、不上传 B 站 —— 传上去的账号往往和 ``.env`` 里配的不是同一个，交给
``--subtitle-only`` 或手动上传更合适。
"""

from __future__ import annotations

import re
from pathlib import Path

from yt2bili import config
from yt2bili.subtitles.bilibili_format import clamp_cues_to_duration
from yt2bili.subtitles.downloader import download_subtitles
from yt2bili.subtitles.parser import parse_subtitle
from yt2bili.subtitles.translator import translate_cues
from yt2bili.subtitles.writer import write_srt
from yt2bili.youtube.downloader import _extract_metadata

#: 译文字幕最多贴到视频结束前多少秒 —— 与后台队列 worker 保持一致，
#: 避免 ffprobe 的小数秒和 B站 的整数秒之差触发 79014。
DURATION_MARGIN_S = 0.5

#: 没指定 --game 也没配 profile 时默认锁定的游戏。
DEFAULT_GAME = "brawl_stars"

#: 数字千位分隔符：中文旁白里一般直接写 "2000"，而模型的输出不稳定
#: （时而半角 "2,000"、时而全角 "2，000"）。只作用于字幕正文，不碰时间轴。
_THOUSANDS_RE = re.compile(r"(?<=\d)[,，](?=\d{3}(?!\d))")


def normalize_numbers(text: str) -> str:
    """去掉数字中的千位分隔符（"2,000" / "2，000" → "2000"）。"""
    return _THOUSANDS_RE.sub("", text)


def resolve_glossary_game(explicit: str = "", profile_game: str = "") -> str:
    """决定这次用哪套游戏术语表。

    优先级：显式 ``--game`` > profile 的 ``settings.glossary_game`` > 荒野乱斗。
    手动补字幕绝大多数是为了荒野乱斗，所以兜底默认锁它，而不是沿用 ``.env``
    里可能同时开着的多套开关。
    """
    # 先 strip 再 or：纯空白是"没给"，不能靠真值判断短路掉默认值。
    return explicit.strip() or profile_game.strip() or DEFAULT_GAME


def fetch_and_translate(video_url: str) -> dict:
    """下载并翻译单条视频的字幕，返回结果摘要。

    Raises:
        RuntimeError: 元数据解析、字幕下载、解析或翻译任一步失败。
    """
    meta = _extract_metadata(video_url)
    video_id = (meta.get("id") or "").strip()
    if not video_id:
        raise RuntimeError(f"无法解析视频 ID: {video_url}")
    duration = float(meta.get("duration") or 0)

    print(f"[字幕] 视频: {meta.get('title') or video_id} ({video_id})")
    print(f"[字幕] 时长: {duration:.0f}s")

    source_path = download_subtitles(video_url, video_id)
    if not source_path:
        raise RuntimeError("YouTube 上未找到匹配的字幕语言")

    cues = parse_subtitle(source_path)
    if not cues:
        raise RuntimeError("字幕文件解析为空")

    translated = translate_cues(cues)
    for cue in translated:
        cue.text = normalize_numbers(cue.text)

    # 对齐时间轴：超出视频长度的丢弃 / 截断，和后台队列 worker 同一套规则。
    dropped = clamped = 0
    if duration > 0:
        translated, dropped, clamped = clamp_cues_to_duration(
            translated, duration, margin=DURATION_MARGIN_S
        )
    if not translated:
        raise RuntimeError("对齐视频时长后字幕为空")

    out_path = (
        Path(config.SUBTITLE_DIR) / f"{video_id}.{config.SUBTITLE_TARGET_LANG}.srt"
    )
    write_srt(translated, str(out_path))

    durations = sorted(c.end - c.start for c in translated)
    return {
        "video_id": video_id,
        "title": meta.get("title") or "",
        "duration": duration,
        "source_path": source_path,
        "translated_path": str(out_path),
        "cues": len(translated),
        "median_cue_s": durations[len(durations) // 2],
        "dropped": dropped,
        "clamped": clamped,
    }
