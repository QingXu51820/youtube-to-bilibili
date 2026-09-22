"""字幕文件命名约定：``{video_id}.{lang}.srt``。

这个约定被抄在好几处：翻译队列、``--subtitle-url``、延迟上传 sweep 各拼了一遍
``{id}.{target_lang}.srt``，video_id 的提取也有 ``split(".")[0]`` 与
``split(".", 1)[0]`` 两种写法。约定只在这里写一次。

本模块是叶子（只依赖 config），所以任何一侧都能直接 import，不必再写函数内 import。
"""

from __future__ import annotations

from pathlib import Path

from yt2bili import config


def video_id_from_filename(name: str) -> str:
    """从字幕文件名取出 video_id：``abc123.zh-CN.srt`` → ``abc123``。

    YouTube 的 video_id 本身不含点，所以只切第一段。
    """
    return Path(str(name)).name.split(".", 1)[0]


def subtitle_path(video_id: str, lang: str) -> Path:
    """某个语言的字幕文件路径。"""
    return Path(config.SUBTITLE_DIR) / f"{video_id}.{lang}.srt"


def source_srt_path(video_id: str, lang: str = "en-orig") -> Path:
    """下载回来的源字幕（默认 yt-dlp 的 ``en-orig``）。"""
    return subtitle_path(video_id, lang)


def translated_srt_path(video_id: str) -> Path:
    """译文字幕 ``{id}.{target_lang}.srt`` —— 队列、sweep 与手动入口共用同一个。"""
    return subtitle_path(video_id, config.SUBTITLE_TARGET_LANG)


def is_translated_name(name: str) -> bool:
    """该文件名是否是目标语言的译文字幕。"""
    return str(name).endswith(f".{config.SUBTITLE_TARGET_LANG}.srt")
