"""China legal-workday repost-window gate.

Provides helpers that decide whether the repost pipeline may run.  When
``WORK_HOURS_ONLY`` is enabled, reposting (and any translation that feeds
it) is only allowed on China's legal working days between
``WORK_START_HOUR``:00 and ``WORK_END_HOUR``:00 (Asia/Shanghai / UTC+8).
Outside that window the pipeline refuses to download, translate, or upload.

Holiday / make-up-workday resolution prefers the optional ``chinese_calendar``
package and falls back to a plain Mon-Fri rule when the calendar data for the
current year is unavailable (e.g. a future year not yet published).
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Optional, Tuple

CHINA_TZ = timezone(timedelta(hours=8))

# ── Optional China holiday calendar ────────────────────────────────
try:  # pragma: no cover - optional dependency
    import chinese_calendar as _cc
    _HAS_CALENDAR = True
except Exception:  # pragma: no cover - optional dependency
    _cc = None
    _HAS_CALENDAR = False


def _config() -> "object":
    """Lazy import to avoid a circular import at module load time."""
    from yt2bili import config
    return config


def china_now() -> datetime:
    """Return the current time in China Standard Time (UTC+8)."""
    return datetime.now(CHINA_TZ)


def gate_enabled() -> bool:
    """Whether the work-hours gate is switched on (from config)."""
    return bool(getattr(_config(), "WORK_HOURS_ONLY", False))


def start_hour() -> int:
    return int(getattr(_config(), "WORK_START_HOUR", 9))


def end_hour() -> int:
    return int(getattr(_config(), "WORK_END_HOUR", 18))


def is_legal_workday(day: date) -> bool:
    """Return True when *day* is a China legal working day.

    Uses ``chinese_calendar`` when it supports the year (which accounts for
    public holidays and make-up workdays).  Falls back to Mon-Fri for years
    outside the package's supported range.
    """
    if _HAS_CALENDAR:
        try:
            return bool(_cc.is_workday(day))
        except NotImplementedError:
            # Calendar data not published for this year yet.
            pass
    return day.weekday() != 5 and day.weekday() != 6


def in_clock_window(now: datetime, start: int = None, end: int = None) -> bool:
    """Return True when ``now.hour`` is within [start, end)."""
    if start is None:
        start = start_hour()
    if end is None:
        end = end_hour()
    return start <= now.hour < end


def check(now: Optional[datetime] = None) -> Tuple[bool, str]:
    """Return ``(allowed, reason)`` for the current repost window.

    When the gate is disabled this always returns ``(True, "")``.  When the
    gate is enabled it returns ``(False, reason)`` unless *now* falls on a
    China legal working day within [WORK_START_HOUR, WORK_END_HOUR).
    """
    if not gate_enabled():
        return True, ""
    now = now or china_now()
    if now.tzinfo is None:
        # Naive input is treated as already being China local time.
        now = now.replace(tzinfo=CHINA_TZ)
    else:
        now = now.astimezone(CHINA_TZ)
    start, end = start_hour(), end_hour()
    if not in_clock_window(now, start, end):
        return (
            False,
            f"当前不在中国法定工作日 {start:02d}:00-{end:02d}:00 搬运时段，"
            "已禁止搬运与翻译",
        )
    if not is_legal_workday(now.date()):
        return (
            False,
            f"今天是休息日，不是中国法定工作日，已禁止搬运与翻译",
        )
    return True, ""


def next_window_start(after: Optional[datetime] = None) -> datetime:
    """Return the earliest :class:`datetime` opening of a legal window after *after*."""
    after = after or china_now()
    if after.tzinfo is None:
        after = after.replace(tzinfo=CHINA_TZ)
    else:
        after = after.astimezone(CHINA_TZ)
    start = start_hour()
    # Check today first, then scan forward up to one week.
    for offset in range(0, 8):
        day = after.date() + timedelta(days=offset)
        if not is_legal_workday(day):
            continue
        candidate = datetime.combine(day, time(start, tzinfo=CHINA_TZ))
        if candidate > after:
            return candidate
    # Defensive fallback: next Monday 09:00.
    days_until_monday = (7 - after.weekday()) % 7 or 7
    day = after.date() + timedelta(days=days_until_monday)
    return datetime.combine(day, time(start, tzinfo=CHINA_TZ))


def seconds_until_next_window(after: Optional[datetime] = None) -> float:
    """Seconds to wait before the gate next permits reposting.

    Returns 0.0 when the gate is disabled or the window is currently open.
    """
    if not gate_enabled():
        return 0.0
    after = after or china_now()
    _, reason = check(after)
    if not reason:
        return 0.0  # currently allowed
    nxt = next_window_start(after)
    delta = nxt - after
    return max(0.0, delta.total_seconds())
