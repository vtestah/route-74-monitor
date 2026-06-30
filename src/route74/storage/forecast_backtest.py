from __future__ import annotations

import sqlite3
from datetime import timedelta
from math import ceil

from route74.domain.prediction_buckets import prediction_bucket_tolerance
from route74.storage.forecast_backtest_cases import (
    ForecastBacktestCase,
    normalized_forecast_cases,
)
from route74.storage.forecast_backtest_results import (
    DEFAULT_FORECAST_BACKTEST_PERCENTILES,  # noqa: F401
    FORECAST_BACKTEST_PERCENTILES_ERROR,  # noqa: F401
    ForecastBacktestResult,
    ForecastBacktestSummary,
    best_forecast_backtest_result,  # noqa: F401
    selected_forecast_backtest_result,  # noqa: F401
    validate_forecast_backtest_percentiles,
)
from route74.storage.helpers import (
    TRUSTED_ETA_SOURCE_METHODS,
    WEEKDAYS,
    optional_int_value,
    within_time_bucket,
)

FORECAST_BACKTEST_SLOT_MINUTES = 5


def summarize_yandex_forecast_backtest(
    connection: sqlite3.Connection,
    *,
    profile_key: str,
    report_window_key: str,
    history_days: int,
    bucket_minutes: int,
    min_samples: int,
    min_distinct_days: int,
    percentiles: tuple[int, ...],
    max_age_seconds: int | None,
) -> ForecastBacktestSummary:
    history_days = _positive_int("history_days", history_days)
    bucket_minutes = _positive_int("bucket_minutes", bucket_minutes)
    min_samples = _positive_int("min_samples", min_samples)
    min_distinct_days = _positive_int("min_distinct_days", min_distinct_days)
    if min_distinct_days > min_samples:
        raise ValueError("min_distinct_days must not exceed min_samples")
    if max_age_seconds is not None:
        max_age_seconds = _positive_int("max_age_seconds", max_age_seconds)
    percentiles = validate_forecast_backtest_percentiles(percentiles)
    cases = _load_cases(connection, profile_key, report_window_key, max_age_seconds)
    results = tuple(
        _evaluate_percentile(
            cases,
            percentile,
            history_days,
            bucket_minutes,
            min_samples,
            min_distinct_days,
        )
        for percentile in percentiles
    )
    return ForecastBacktestSummary(
        profile_key,
        report_window_key,
        history_days,
        bucket_minutes,
        min_samples,
        min_distinct_days,
        percentiles,
        len(cases),
        results,
    )


def _evaluate_percentile(
    cases: tuple[ForecastBacktestCase, ...],
    percentile: int,
    history_days: int,
    bucket_minutes: int,
    min_samples: int,
    min_distinct_days: int,
) -> ForecastBacktestResult:
    skipped = misses = accurate = miss_minutes = extra_wait = total_abs = evaluated = 0
    for case in cases:
        train = _training_values(cases, case, history_days, bucket_minutes)
        values = tuple(value for _date, value in train)
        distinct_days = len({date for date, _value in train})
        if len(values) < min_samples or distinct_days < min_distinct_days:
            skipped += 1
            continue
        predicted = _percentile(values, percentile)
        error = predicted - case.arrival_minutes
        evaluated += 1
        total_abs += abs(error)
        if abs(error) <= prediction_bucket_tolerance(predicted):
            accurate += 1
        if error > 0:
            misses += 1
            miss_minutes += error
        else:
            extra_wait += abs(error)
    mae = total_abs / evaluated if evaluated else 0.0
    return ForecastBacktestResult(percentile, evaluated, skipped, misses, accurate, miss_minutes, extra_wait, mae)


def _training_values(
    cases: tuple[ForecastBacktestCase, ...],
    target: ForecastBacktestCase,
    history_days: int,
    bucket_minutes: int,
) -> tuple[tuple[str, int], ...]:
    oldest = target.sampled_at - timedelta(days=history_days)
    return tuple(
        (case.service_date, case.arrival_minutes)
        for case in cases
        if oldest <= case.sampled_at < target.sampled_at
        and case.service_date != target.service_date
        and case.weekday in WEEKDAYS
        and within_time_bucket(case.sampled_at, target.sampled_at, bucket_minutes)
    )


def _load_cases(
    connection: sqlite3.Connection,
    profile_key: str,
    report_window_key: str,
    max_age_seconds: int | None,
) -> tuple[ForecastBacktestCase, ...]:
    filters = [
        "profile_key = ?",
        "report_window_key = ?",
        _trusted_source_filter(),
        "available = 1",
        "arrival_minutes IS NOT NULL",
    ]
    params: list[object] = [profile_key, report_window_key]
    if max_age_seconds is not None:
        filters.append("newest_age_seconds <= ?")
        params.append(max_age_seconds)
    rows = connection.execute(
        f"""
        SELECT sampled_at, service_date, weekday, minute_of_day, arrival_minutes, newest_age_seconds
        FROM yandex_forecast_samples
        WHERE {" AND ".join(filters)}
        ORDER BY sampled_at
        """,
        tuple(params),
    ).fetchall()
    return normalized_forecast_cases(
        tuple(row for row in rows if _valid_sample_age(row, max_age_seconds)),
        slot_minutes=FORECAST_BACKTEST_SLOT_MINUTES,
    )


def _percentile(values: tuple[int, ...], percentile: int) -> int:
    ordered = sorted(values)
    index = max(0, ceil(percentile / 100 * len(ordered)) - 1)
    return ordered[index]


def _positive_int(name: str, value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"expected positive {name}")
    return value


def _valid_sample_age(row: sqlite3.Row, max_age_seconds: int | None) -> bool:
    age_seconds = optional_int_value(row["newest_age_seconds"])
    if age_seconds is not None and age_seconds < 0:
        return False
    if max_age_seconds is None:
        return True
    return age_seconds is not None and age_seconds <= max_age_seconds


def _trusted_source_filter() -> str:
    return f"source_method IN ({','.join(repr(item) for item in TRUSTED_ETA_SOURCE_METHODS)})"
