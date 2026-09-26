from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import jdatetime

_PERSIAN = str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789")
_ARABIC = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")


def normalize_digits(value: str) -> str:
    return value.translate(_PERSIAN).translate(_ARABIC)


def parse_date_input(value: str, tz_name: str) -> tuple[date, str]:
    """Return Gregorian date and canonical Jalali YYYY-MM-DD label."""
    raw = normalize_digits(value.strip())
    raw = re.sub(r"[.\\\\_]+", "/", raw)
    raw = re.sub(r"[-–—]+", "/", raw)
    raw = re.sub(r"\s+", "/", raw)
    parts = [p for p in raw.split("/") if p]
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        raise ValueError("تاریخ را مثل ۱۴۰۵/۰۷/۰۳ وارد کنید.")

    y, m, d = map(int, parts)
    # Persian/Jalali is the default for years in the normal LMS working range.
    if 1200 <= y <= 1600:
        try:
            g = jdatetime.date(y, m, d).togregorian()
        except ValueError as exc:
            raise ValueError("تاریخ شمسی واردشده معتبر نیست.") from exc
        return g, f"{y:04d}/{m:02d}/{d:02d}"

    # Also accept Gregorian dates for debugging/automation.
    if 1900 <= y <= 2200:
        try:
            g = date(y, m, d)
        except ValueError as exc:
            raise ValueError("تاریخ میلادی واردشده معتبر نیست.") from exc
        j = jdatetime.date.fromgregorian(date=g)
        return g, f"{j.year:04d}/{j.month:02d}/{j.day:02d}"

    raise ValueError("سال واردشده در محدوده قابل پشتیبانی نیست.")


def today_gregorian(tz_name: str) -> date:
    return datetime.now(ZoneInfo(tz_name)).date()


def now_local(tz_name: str) -> datetime:
    return datetime.now(ZoneInfo(tz_name))


def jalali_label(gregorian_date: date) -> str:
    j = jdatetime.date.fromgregorian(date=gregorian_date)
    return f"{j.year:04d}/{j.month:02d}/{j.day:02d}"


def day_bounds_timestamps(gregorian_date: date, tz_name: str) -> tuple[int, int]:
    tz = ZoneInfo(tz_name)
    start = datetime.combine(gregorian_date, time.min, tzinfo=tz)
    end = start + timedelta(days=1)
    return int(start.timestamp()), int(end.timestamp())


def expected_quiz_timestamps(gregorian_date: date, tz_name: str) -> tuple[int, int]:
    tz = ZoneInfo(tz_name)
    open_dt = datetime.combine(gregorian_date, time(14, 45), tzinfo=tz)
    close_dt = datetime.combine(gregorian_date, time(21, 0), tzinfo=tz)
    return int(open_dt.timestamp()), int(close_dt.timestamp())


def format_local_timestamp(timestamp: int | None, tz_name: str) -> str:
    if not timestamp:
        return "—"
    dt = datetime.fromtimestamp(int(timestamp), ZoneInfo(tz_name))
    return dt.strftime("%Y/%m/%d %H:%M:%S")
