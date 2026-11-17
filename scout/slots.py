from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Optional

SLOT_TIMES: dict[str, time] = {
    "09:45": time(9, 45),
    "11:00": time(11, 0),
    "13:30": time(13, 30),
    "15:30": time(15, 30),
}
SLOTS: tuple[str, ...] = tuple(SLOT_TIMES)

# US market closures inside the competition window (organizer calendar governs).
HOLIDAYS: frozenset[date] = frozenset({date(2026, 11, 26), date(2026, 12, 25)})
EARLY_CLOSES: dict[date, time] = {
    date(2026, 11, 27): time(13, 0),
    date(2026, 12, 24): time(13, 0),
}

NO_ENTRY_MINUTES_BEFORE_CLOSE = 30


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (n - 1))


def is_us_dst(day: date) -> bool:
    start = _nth_weekday(day.year, 3, 6, 2)  # second Sunday in March
    end = _nth_weekday(day.year, 11, 6, 1)  # first Sunday in November
    return start <= day < end


def eastern(now: datetime) -> datetime:
    if now.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    utc = now.astimezone(timezone.utc)
    offset = -4 if is_us_dst(utc.date()) else -5
    return utc.astimezone(timezone(timedelta(hours=offset), "ET"))


def session_close(day: date) -> time:
    return EARLY_CLOSES.get(day, time(16, 0))


def is_trading_day(day: date) -> bool:
    return day.weekday() < 5 and day not in HOLIDAYS


def _slot_minutes(slot: str) -> int:
    moment = SLOT_TIMES[slot]
    return moment.hour * 60 + moment.minute


def _close_minutes(day: date) -> int:
    close = session_close(day)
    return close.hour * 60 + close.minute


def slot_available(slot: str, day: date) -> bool:
    return is_trading_day(day) and _slot_minutes(slot) < _close_minutes(day)


def allows_entries(slot: str, day: date) -> bool:
    return slot_available(slot, day) and _slot_minutes(slot) < (
        _close_minutes(day) - NO_ENTRY_MINUTES_BEFORE_CLOSE
    )


def due_slot(now: datetime, window_minutes: int = 15) -> Optional[str]:
    local = eastern(now)
    day = local.date()
    if not is_trading_day(day):
        return None
    minutes = local.hour * 60 + local.minute
    for slot in SLOTS:
        if slot_available(slot, day) and 0 <= minutes - _slot_minutes(slot) < window_minutes:
            return slot
    return None
