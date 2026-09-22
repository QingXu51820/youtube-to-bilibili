"""ffprobe 探测（媒体时长等只读元数据查询）。

视频切分要判断是否需要切割，下载器要记录时长，两处各写了一份逐字相同的 ffprobe
调用 —— 连"探测失败返回 0.0"这个约定也各抄了一遍。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from yt2bili import config


def probe_duration(file_path: Path | str) -> float:
    """用 ffprobe 读取媒体时长（秒）；探测不了时返回 ``0.0``。

    找不到 ffprobe、文件打不开、超时、输出不是数字 —— 一律当作"未知时长"，
    由调用方决定是跳过还是按无时长处理。
    """
    ffprobe = config.find_tool("ffprobe")
    if ffprobe is None:
        return 0.0

    command = [
        ffprobe,
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "csv=p=0",
        str(file_path),
    ]
    try:
        proc = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return 0.0
    if proc.returncode != 0:
        return 0.0
    try:
        return float(proc.stdout.strip())
    except (ValueError, TypeError):
        return 0.0
