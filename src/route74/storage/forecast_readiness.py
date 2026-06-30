from __future__ import annotations

import sqlite3
from datetime import datetime

from route74.storage.forecast_validation import validate_forecast_readiness_inputs
from route74.storage.helpers import day_kind_weekdays
from route74.storage.history import (
    load_yandex_eta_history_for_profile_window,
    load_yandex_forecast_sample_counts,
)
from route74.storage.models import ForecastReadinessSummary


def summarize_yandex_forecast_readiness(
    connection: sqlite3.Connection,
    *,
    profile_key: str,
    current_time: datetime,
    days: int,
    min_samples: int,
    min_distinct_days: int,
    primary_bucket_minutes: int,
    fallback_bucket_minutes: int,
    max_age_seconds: int | None,
    same_day_kind: bool = True,
    weekdays: tuple[int, ...] | None = None,
    report_window_key: str | None = None,
) -> ForecastReadinessSummary:
    validate_forecast_readiness_inputs(
        current_time=current_time,
        days=days,
        min_samples=min_samples,
        min_distinct_days=min_distinct_days,
        primary_bucket_minutes=primary_bucket_minutes,
        fallback_bucket_minutes=fallback_bucket_minutes,
        max_age_seconds=max_age_seconds,
    )
    weekday_scope = weekdays if weekdays is not None else _weekdays(current_time, same_day_kind)
    counts = load_yandex_forecast_sample_counts(
        connection,
        profile_key=profile_key,
        current_time=current_time,
        days=days,
        weekdays=weekday_scope,
        max_age_seconds=max_age_seconds,
        report_window_key=report_window_key,
    )
    primary_samples, primary_days = _bucket_metrics(
        connection,
        profile_key=profile_key,
        current_time=current_time,
        days=days,
        bucket_minutes=primary_bucket_minutes,
        weekdays=weekday_scope,
        max_age_seconds=max_age_seconds,
        report_window_key=report_window_key,
    )
    fallback_samples, fallback_days = _bucket_metrics(
        connection,
        profile_key=profile_key,
        current_time=current_time,
        days=days,
        bucket_minutes=fallback_bucket_minutes,
        weekdays=weekday_scope,
        max_age_seconds=max_age_seconds,
        report_window_key=report_window_key,
    )
    return ForecastReadinessSummary(
        profile_key=profile_key,
        report_window_key=report_window_key,
        current_time=current_time,
        days=days,
        min_samples=min_samples,
        min_distinct_days=min_distinct_days,
        primary_bucket_minutes=primary_bucket_minutes,
        fallback_bucket_minutes=fallback_bucket_minutes,
        max_age_seconds=max_age_seconds,
        total_samples=counts.total,
        eta_samples=counts.eta,
        fresh_eta_samples=counts.fresh_eta,
        traffic_samples=counts.traffic,
        primary_samples=primary_samples,
        fallback_samples=fallback_samples,
        primary_distinct_days=primary_days,
        fallback_distinct_days=fallback_days,
        latest_sampled_at=counts.latest_sampled_at,
    )


def _bucket_metrics(
    connection: sqlite3.Connection,
    *,
    profile_key: str,
    current_time: datetime,
    days: int,
    bucket_minutes: int,
    weekdays: tuple[int, ...] | None,
    max_age_seconds: int | None,
    report_window_key: str | None,
) -> tuple[int, int]:
    history = load_yandex_eta_history_for_profile_window(
        connection,
        profile_key=profile_key,
        current_time=current_time,
        days=days,
        bucket_minutes=bucket_minutes,
        weekdays=weekdays,
        max_age_seconds=max_age_seconds,
        report_window_key=report_window_key,
    )
    return len(history.arrival_minutes), history.distinct_service_days


def _weekdays(current_time: datetime, same_day_kind: bool) -> tuple[int, ...] | None:
    if not same_day_kind:
        return None
    return day_kind_weekdays(current_time)
