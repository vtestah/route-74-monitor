from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from statistics import median

from route74.domain.profiles import PROFILE_KEYS
from route74.domain.reporting import REPORT_WINDOWS_BY_KEY
from route74.storage.helpers import (
    TRUSTED_ETA_SOURCE_METHODS,
    is_trusted_eta_source,
    optional_int_value,
    within_time_bucket,
)

FORECAST_HISTORY_SLOT_MINUTES = 5


@dataclass(frozen=True)
class YandexEtaHistory:
    arrival_minutes: tuple[int, ...]
    distinct_service_days: int


@dataclass(frozen=True)
class YandexForecastSampleCounts:
    total: int
    eta: int
    fresh_eta: int
    traffic: int
    latest_sampled_at: datetime | None


def load_yandex_eta_history_for_profile_window(
    connection: sqlite3.Connection,
    *,
    profile_key: str,
    current_time: datetime,
    days: int,
    bucket_minutes: int,
    weekdays: tuple[int, ...] | None = None,
    max_age_seconds: int | None = None,
    report_window_key: str | None = None,
    before: datetime | None = None,
) -> YandexEtaHistory:
    _validate_history_inputs(
        profile_key=profile_key,
        current_time=current_time,
        days=days,
        weekdays=weekdays,
        max_age_seconds=max_age_seconds,
        report_window_key=report_window_key,
    )
    _positive_int("bucket_minutes", bucket_minutes)
    _datetime_or_none("before", before)
    rows = connection.execute(
        _history_sql(weekdays, max_age_seconds, report_window_key, before),
        _history_params(
            profile_key,
            current_time,
            days,
            weekdays,
            max_age_seconds,
            report_window_key,
            before,
        ),
    ).fetchall()
    rows = tuple(row for row in rows if _sampled_at_within_bucket(row, current_time, bucket_minutes))
    arrivals, service_days = _normalized_arrivals(rows)
    return YandexEtaHistory(
        arrival_minutes=arrivals,
        distinct_service_days=len(service_days),
    )


def load_yandex_forecast_sample_counts(
    connection: sqlite3.Connection,
    *,
    profile_key: str,
    current_time: datetime,
    days: int,
    weekdays: tuple[int, ...] | None = None,
    max_age_seconds: int | None = None,
    report_window_key: str | None = None,
) -> YandexForecastSampleCounts:
    _validate_history_inputs(
        profile_key=profile_key,
        current_time=current_time,
        days=days,
        weekdays=weekdays,
        max_age_seconds=max_age_seconds,
        report_window_key=report_window_key,
    )
    rows = connection.execute(
        _counts_sql(weekdays, report_window_key),
        _counts_params(profile_key, current_time, days, weekdays, report_window_key),
    ).fetchall()
    eta = tuple(row for row in rows if _is_eta_sample(row))
    fresh = tuple(row for row in eta if _is_fresh(row["newest_age_seconds"], max_age_seconds))
    traffic = tuple(row for row in rows if str(row["traffic_status"]) == "ok")
    latest = _latest_sampled_at(rows)
    return YandexForecastSampleCounts(
        _normalized_count(rows),
        _normalized_count(eta),
        _normalized_count(fresh),
        _normalized_count(traffic),
        latest,
    )


def _validate_history_inputs(
    *,
    profile_key: str,
    current_time: datetime,
    days: int,
    weekdays: tuple[int, ...] | None,
    max_age_seconds: int | None,
    report_window_key: str | None,
) -> None:
    if not isinstance(profile_key, str) or profile_key not in PROFILE_KEYS:
        expected = ", ".join(PROFILE_KEYS)
        raise ValueError(f"profile_key must be one of {expected}")
    _aware_datetime("current_time", current_time)
    _positive_int("days", days)
    _weekdays_or_none("weekdays", weekdays)
    _non_negative_int_or_none("max_age_seconds", max_age_seconds)
    _report_window_key_or_none(report_window_key)


def _positive_int(name: str, value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")


def _non_negative_int_or_none(name: str, value: int | None) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer or None")


def _weekdays_or_none(name: str, value: tuple[int, ...] | None) -> None:
    if value is None:
        return
    if not isinstance(value, tuple) or not value:
        raise ValueError(f"{name} must be a non-empty tuple of weekday integers or None")
    if any(isinstance(item, bool) or not isinstance(item, int) or not 0 <= item <= 6 for item in value):
        raise ValueError(f"{name} must contain weekday integers from 0 to 6")


def _report_window_key_or_none(value: str | None) -> None:
    if value is None:
        return
    if not isinstance(value, str) or not value.strip():
        raise ValueError("report_window_key must be a non-empty string or None")
    if value not in REPORT_WINDOWS_BY_KEY:
        expected = ", ".join(REPORT_WINDOWS_BY_KEY)
        raise ValueError(f"unknown report_window_key: {value} (expected {expected})")


def _datetime_or_none(name: str, value: datetime | None) -> None:
    if value is None:
        return
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be a timezone-aware datetime or None")


def _aware_datetime(name: str, value: object) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be a timezone-aware datetime")


def _history_sql(
    weekdays: tuple[int, ...] | None,
    max_age_seconds: int | None,
    report_window_key: str | None,
    before: datetime | None,
) -> str:
    filters = [
        "profile_key = ?",
        _trusted_source_filter(),
        "fallback_reason NOT LIKE 'vehicle_prediction_thread_fallback:%'",
        "arrival_minutes IS NOT NULL",
        "available = 1",
        "sampled_at >= ?",
    ]
    if report_window_key is not None:
        filters.append("report_window_key = ?")
    if before is not None:
        filters.append("sampled_at < ?")
    if weekdays is not None:
        filters.append(f"weekday IN ({','.join('?' for _ in weekdays)})")
    if max_age_seconds is not None:
        filters.append("newest_age_seconds <= ?")
    return f"""
        SELECT arrival_minutes, service_date, minute_of_day, sampled_at
        FROM yandex_forecast_samples
        WHERE {" AND ".join(filters)}
        """


def _history_params(
    profile_key: str,
    current_time: datetime,
    days: int,
    weekdays: tuple[int, ...] | None,
    max_age_seconds: int | None,
    report_window_key: str | None,
    before: datetime | None,
) -> tuple[object, ...]:
    params: list[object] = [
        profile_key,
        (current_time - timedelta(days=days)).isoformat(),
    ]
    if report_window_key is not None:
        params.append(report_window_key)
    if before is not None:
        params.append(before.isoformat())
    if weekdays is not None:
        params.extend(weekdays)
    if max_age_seconds is not None:
        params.append(max_age_seconds)
    return tuple(params)


def _counts_sql(
    weekdays: tuple[int, ...] | None,
    report_window_key: str | None,
) -> str:
    filters = ["profile_key = ?", "sampled_at >= ?"]
    if report_window_key is not None:
        filters.append("report_window_key = ?")
    if weekdays is not None:
        filters.append(f"weekday IN ({','.join('?' for _ in weekdays)})")
    return f"""
        SELECT
            arrival_minutes, available, newest_age_seconds, traffic_status, source_method, fallback_reason,
            service_date, minute_of_day, sampled_at
        FROM yandex_forecast_samples
        WHERE {" AND ".join(filters)}
        ORDER BY sampled_at DESC
        """


def _counts_params(
    profile_key: str,
    current_time: datetime,
    days: int,
    weekdays: tuple[int, ...] | None,
    report_window_key: str | None,
) -> tuple[object, ...]:
    params: list[object] = [
        profile_key,
        (current_time - timedelta(days=days)).isoformat(),
    ]
    if report_window_key is not None:
        params.append(report_window_key)
    if weekdays is not None:
        params.extend(weekdays)
    return tuple(params)


def _trusted_source_filter() -> str:
    return f"source_method IN ({','.join(repr(item) for item in TRUSTED_ETA_SOURCE_METHODS)})"


def _is_fresh(age_seconds: object, max_age_seconds: int | None) -> bool:
    if max_age_seconds is None:
        return True
    age = optional_int_value(age_seconds)
    if age is None or age < 0:
        return False
    return age <= max_age_seconds


def _is_eta_sample(row: sqlite3.Row) -> bool:
    arrival_minutes = optional_int_value(row["arrival_minutes"])
    available = optional_int_value(row["available"])
    return (
        arrival_minutes is not None
        and arrival_minutes >= 0
        and available == 1
        and is_trusted_eta_source(row["source_method"])
        and not str(row["fallback_reason"]).startswith("vehicle_prediction_thread_fallback:")
    )


def _sampled_at_within_bucket(row: sqlite3.Row, current_time: datetime, bucket_minutes: int) -> bool:
    sampled_at = _optional_datetime(row["sampled_at"])
    if sampled_at is None:
        return False
    return within_time_bucket(sampled_at, current_time, bucket_minutes)


def _latest_sampled_at(rows: tuple[sqlite3.Row, ...]) -> datetime | None:
    for row in rows:
        sampled_at = _optional_datetime(row["sampled_at"])
        if sampled_at is not None:
            return sampled_at
    return None


def _optional_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _normalized_arrivals(
    rows: tuple[sqlite3.Row, ...],
) -> tuple[tuple[int, ...], set[str]]:
    groups: dict[tuple[str, int], list[int]] = {}
    for row in rows:
        key = _forecast_slot_key(row)
        arrival_minutes = optional_int_value(row["arrival_minutes"])
        if key is None or arrival_minutes is None or arrival_minutes < 0:
            continue
        groups.setdefault(key, []).append(arrival_minutes)
    arrivals = tuple(round(median(values)) for key, values in sorted(groups.items()))
    service_days = {service_date for service_date, _slot in groups}
    return arrivals, service_days


def _normalized_count(rows: tuple[sqlite3.Row, ...]) -> int:
    return len({key for row in rows if (key := _forecast_slot_key(row)) is not None})


def _forecast_slot_key(row: sqlite3.Row) -> tuple[str, int] | None:
    minute_of_day = optional_int_value(row["minute_of_day"])
    if minute_of_day is None or not 0 <= minute_of_day < 24 * 60:
        return None
    service_date = str(row["service_date"]).strip()
    if not service_date:
        return None
    return service_date, minute_of_day // FORECAST_HISTORY_SLOT_MINUTES
