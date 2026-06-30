from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from statistics import median

from route74.storage.helpers import optional_int_value

DAY_MINUTES = 24 * 60


@dataclass(frozen=True)
class ForecastBacktestCase:
    sampled_at: datetime
    service_date: str
    weekday: int
    minute_of_day: int
    arrival_minutes: int


@dataclass(frozen=True)
class _SampleRow:
    sampled_at: datetime
    service_date: str
    weekday: int
    minute_of_day: int
    arrival_minutes: int


def normalized_forecast_cases(
    rows: tuple[sqlite3.Row, ...],
    *,
    slot_minutes: int,
) -> tuple[ForecastBacktestCase, ...]:
    slot_minutes = _positive_slot_minutes(slot_minutes)
    groups: dict[tuple[str, int], list[_SampleRow]] = defaultdict(list)
    for row in rows:
        normalized = _sample_row(row)
        if normalized is None:
            continue
        key = normalized.service_date, normalized.minute_of_day // slot_minutes
        groups[key].append(normalized)
    cases = (
        ForecastBacktestCase(
            sampled_at=grouped_rows[0].sampled_at,
            service_date=grouped_rows[0].service_date,
            weekday=grouped_rows[0].weekday,
            minute_of_day=grouped_rows[0].minute_of_day,
            arrival_minutes=round(median(row.arrival_minutes for row in grouped_rows)),
        )
        for grouped_rows in groups.values()
    )
    return tuple(sorted(cases, key=lambda item: item.sampled_at))


def _sample_row(row: sqlite3.Row) -> _SampleRow | None:
    sampled_at = _datetime_value(row["sampled_at"])
    service_date = _service_date_value(row["service_date"])
    weekday = optional_int_value(row["weekday"])
    minute_of_day = optional_int_value(row["minute_of_day"])
    arrival_minutes = optional_int_value(row["arrival_minutes"])
    if (
        sampled_at is None
        or service_date is None
        or service_date != sampled_at.date().isoformat()
        or weekday is None
        or not 0 <= weekday <= 6
        or minute_of_day is None
        or not 0 <= minute_of_day < DAY_MINUTES
        or arrival_minutes is None
        or arrival_minutes < 0
    ):
        return None
    return _SampleRow(
        sampled_at=sampled_at,
        service_date=service_date,
        weekday=weekday,
        minute_of_day=minute_of_day,
        arrival_minutes=arrival_minutes,
    )


def _datetime_value(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


def _service_date_value(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    service_date = value.strip()
    if not service_date:
        return None
    try:
        date.fromisoformat(service_date)
    except ValueError:
        return None
    return service_date


def _positive_slot_minutes(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("expected positive slot_minutes")
    return value
