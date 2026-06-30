from __future__ import annotations

from datetime import datetime

from route74.models import require_local_datetime


def validate_forecast_readiness_inputs(
    *,
    current_time: datetime,
    days: int,
    min_samples: int,
    min_distinct_days: int,
    primary_bucket_minutes: int,
    fallback_bucket_minutes: int,
    max_age_seconds: int | None,
) -> None:
    _local_datetime("current_time", current_time)
    _positive_int("days", days)
    _positive_int("min_samples", min_samples)
    _positive_int("min_distinct_days", min_distinct_days)
    _positive_int("primary_bucket_minutes", primary_bucket_minutes)
    _positive_int("fallback_bucket_minutes", fallback_bucket_minutes)
    _non_negative_int_or_none("max_age_seconds", max_age_seconds)
    if min_distinct_days > min_samples:
        raise ValueError("min_distinct_days must not exceed min_samples")
    if fallback_bucket_minutes < primary_bucket_minutes:
        raise ValueError("fallback_bucket_minutes must be greater than or equal to primary_bucket_minutes")


def validate_forecast_window_coverage_inputs(
    *,
    current_date: datetime,
    days: int,
    min_samples: int,
    min_distinct_days: int,
    primary_bucket_minutes: int,
    fallback_bucket_minutes: int,
    max_age_seconds: int | None,
    step_minutes: int,
) -> None:
    _local_datetime("current_date", current_date)
    validate_forecast_readiness_inputs(
        current_time=current_date,
        days=days,
        min_samples=min_samples,
        min_distinct_days=min_distinct_days,
        primary_bucket_minutes=primary_bucket_minutes,
        fallback_bucket_minutes=fallback_bucket_minutes,
        max_age_seconds=max_age_seconds,
    )
    _positive_int("step_minutes", step_minutes)


def _local_datetime(name: str, value: datetime) -> None:
    require_local_datetime(value, name=name)


def _positive_int(name: str, value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")


def _non_negative_int_or_none(name: str, value: int | None) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer or None")
