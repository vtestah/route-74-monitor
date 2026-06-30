from __future__ import annotations

import json
import sqlite3
from collections import Counter
from datetime import datetime

from route74.models import NOVOSIBIRSK_TZ
from route74.sources.yandex.constants import max_raw_eta_minutes
from route74.storage.models import CountByKey

WEEKDAYS = (0, 1, 2, 3, 4)
WEEKENDS = (5, 6)


def arrival_minutes_from_json(raw_json: object) -> tuple[int, ...]:
    try:
        raw = json.loads(raw_json)
    except (TypeError, json.JSONDecodeError):
        return ()
    if not isinstance(raw, list):
        return ()
    max_minutes = max_raw_eta_minutes(None)
    return tuple(
        item for item in raw if isinstance(item, int) and not isinstance(item, bool) and 0 <= item <= max_minutes
    )


def optional_int_value(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        try:
            return int(stripped)
        except ValueError:
            return None
    return None


def count_rows(counts: Counter[str]) -> tuple[CountByKey, ...]:
    return tuple(CountByKey(key, count) for key, count in sorted(counts.items(), key=lambda item: (-item[1], item[0])))


def count_table_rows(connection: sqlite3.Connection, table: str) -> int:
    table_name = _table_identifier(table)
    row = connection.execute(f'SELECT COUNT(*) AS count FROM "{table_name}"').fetchone()
    return int(row[0])


def _table_identifier(table: str) -> str:
    if not isinstance(table, str) or not table:
        raise ValueError("table name is required")
    if not table.isascii() or table[0].isdigit() or any(not (char.isalnum() or char == "_") for char in table):
        raise ValueError("table name must be a simple SQLite identifier")
    return table


def within_time_bucket(sampled_at: datetime, current_time: datetime, bucket_minutes: int) -> bool:
    _aware_datetime("sampled_at", sampled_at)
    _aware_datetime("current_time", current_time)
    _non_negative_bucket_minutes(bucket_minutes)
    local_sampled_at = sampled_at.astimezone(current_time.tzinfo)
    sample_minutes = local_sampled_at.hour * 60 + local_sampled_at.minute
    current_minutes = current_time.hour * 60 + current_time.minute
    diff = abs(sample_minutes - current_minutes)
    diff = min(diff, 24 * 60 - diff)
    return diff <= bucket_minutes


def _aware_datetime(name: str, value: object) -> None:
    if not isinstance(value, datetime):
        raise ValueError(f"{name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


def _non_negative_bucket_minutes(value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("bucket_minutes must be a non-negative integer")


def day_kind_weekdays(current_time: datetime) -> tuple[int, ...]:
    _aware_datetime("day-kind time", current_time)
    local_time = current_time.astimezone(NOVOSIBIRSK_TZ)
    if local_time.weekday() < 5:
        return WEEKDAYS
    return WEEKENDS
