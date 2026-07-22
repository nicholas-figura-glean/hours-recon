"""Date helpers with explicit business-day semantics."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Optional, Union
from zoneinfo import ZoneInfo

DateLike = Union[str, date]


def parse_date(value: DateLike) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def add_one_year(value: DateLike) -> date:
    source = parse_date(value)
    try:
        return source.replace(year=source.year + 1)
    except ValueError:  # February 29 expires February 28 the following year.
        return source.replace(year=source.year + 1, day=28)


def monday_of(value: DateLike) -> date:
    parsed = parse_date(value)
    return parsed - timedelta(days=parsed.weekday())


def optional_date(value: Optional[DateLike]) -> Optional[date]:
    return parse_date(value) if value else None


def business_today(timezone_name: str) -> date:
    return datetime.now(ZoneInfo(timezone_name)).date()
