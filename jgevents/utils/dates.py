"""Date/time helpers.

Conventions
-----------
* Audit/record timestamps (created_at, approved_at, ...) are stored as
  timezone-aware UTC and displayed in IST.
* Event date / start / end are *wall-clock IST* values (a batch launch at
  10:00 in Bhopal is 10:00 IST) stored as plain DATE / TIME columns. All
  centres are in India, so a single business timezone is correct.
* UI shows dates as DD-MM-YYYY.
"""
from __future__ import annotations

import calendar
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
DATE_FMT = "%d-%m-%Y"
DATETIME_FMT = "%d-%m-%Y %H:%M"
TIME_FMT = "%H:%M"


def now_utc() -> datetime:
    return datetime.now(UTC)


def now_ist() -> datetime:
    return datetime.now(IST)


def today_ist() -> date:
    return now_ist().date()


def to_ist(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:  # SQLite returns naive values; we always write UTC
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(IST)


def fmt_date(d: date | datetime | None) -> str:
    if d is None:
        return ""
    if isinstance(d, datetime):
        d = to_ist(d)
    return d.strftime(DATE_FMT)


def fmt_dt(dt: datetime | None) -> str:
    dt = to_ist(dt)
    return dt.strftime(DATETIME_FMT) if dt else ""


def fmt_time(t: time | None) -> str:
    return t.strftime(TIME_FMT) if t else ""


def parse_date(value: str | date | None) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    for fmt in (DATE_FMT, "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(str(value), fmt).date()
        except ValueError:
            continue
    raise ValueError(f"Invalid date '{value}'. Use DD-MM-YYYY.")


def parse_time(value: str | time | None) -> time | None:
    if value in (None, ""):
        return None
    if isinstance(value, time):
        return value
    for fmt in ("%H:%M", "%H:%M:%S"):
        try:
            return datetime.strptime(str(value), fmt).time()
        except ValueError:
            continue
    raise ValueError(f"Invalid time '{value}'. Use HH:MM (24h).")


def month_bounds(year: int, month: int) -> tuple[date, date]:
    last = calendar.monthrange(year, month)[1]
    return date(year, month, 1), date(year, month, last)


def add_months(year: int, month: int, delta: int) -> tuple[int, int]:
    idx = year * 12 + (month - 1) + delta
    return idx // 12, idx % 12 + 1


def next_month(d: date | None = None) -> tuple[int, int]:
    d = d or today_ist()
    return add_months(d.year, d.month, 1)


def month_label(year: int, month: int) -> str:
    return f"{calendar.month_name[month]} {year}"


def month_grid(year: int, month: int) -> list[list[date | None]]:
    """Weeks (Mon..Sun) for a month; days outside the month are None."""
    cal = calendar.Calendar(firstweekday=0)
    weeks = []
    for week in cal.monthdatescalendar(year, month):
        weeks.append([d if d.month == month else None for d in week])
    return weeks


def last_weekday_of_month(year: int, month: int, weekday: int) -> date:
    """weekday: 0=Monday .. 6=Sunday."""
    _, last = month_bounds(year, month)
    offset = (last.weekday() - weekday) % 7
    return last - timedelta(days=offset)


def nth_weekday_of_month(year: int, month: int, weekday: int, n: int) -> date | None:
    first, last = month_bounds(year, month)
    offset = (weekday - first.weekday()) % 7
    d = first + timedelta(days=offset + 7 * (n - 1))
    return d if d <= last else None


def clamp_day(year: int, month: int, day: int) -> date:
    _, last = month_bounds(year, month)
    return date(year, month, min(day, last.day))


def times_overlap(s1: time | None, e1: time | None, s2: time | None, e2: time | None) -> bool:
    """Half-open interval overlap. Missing times are treated as all-day."""
    if not (s1 and s2):
        return True
    e1 = e1 or s1
    e2 = e2 or s2
    if e1 == s1:  # zero-length: treat as a point-in-time
        e1 = (datetime.combine(date.min, s1) + timedelta(minutes=1)).time()
    if e2 == s2:
        e2 = (datetime.combine(date.min, s2) + timedelta(minutes=1)).time()
    return s1 < e2 and s2 < e1
