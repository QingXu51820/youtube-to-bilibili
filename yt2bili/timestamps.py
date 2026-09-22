"""统一的状态文件时间戳（生成 + 解析）。

仓里同一个时间戳格式曾有四份生成实现（``monitor.utc_now``、``collection._now_iso``、
``cleanup._now_iso``、``subtitle._now_stamp``，其中后三份的 docstring 互相承认"与
monitor 一致"）和五份解析实现 —— 而且它们并不都等价：collection 的那份缺
naive→UTC 回退，读到无时区值时会拿 naive 与 aware 相减，抛 ``TypeError`` 打断整轮
sweep；``subscriptions`` 还漏了 ``timespec="seconds"``，写出全仓唯一的微秒精度时间戳。

状态文件的时间戳统一走这里，格式只有一种。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

#: 北京时区 —— B站 后台与监控日志按这个时区显示。
BEIJING_TZ = timezone(timedelta(hours=8))


def utc_now() -> str:
    """当前 UTC 时间，秒精度、``Z`` 结尾。"""
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def beijing_now() -> str:
    """当前北京时间（秒精度，带 ``+08:00`` 偏移）—— 用于给人看的日志。"""
    return datetime.now(BEIJING_TZ).isoformat(timespec="seconds")


def parse_iso(value) -> datetime | None:
    """把时间戳解析为 aware ``datetime``；空值或非法值返回 ``None``。

    无时区的值按 UTC 处理：队列里混着手写时间戳与早期版本写下的 naive 值，而调用方
    要拿它和 ``datetime.now(timezone.utc)`` 相减 —— naive 值直接相减会抛 ``TypeError``。
    """
    raw = str(value or "")
    if not raw:
        return None
    try:
        stamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp
