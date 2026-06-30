from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

from route74.domain.reporting import ReportWindow
from route74.storage.forecast_readiness import summarize_yandex_forecast_readiness
from route74.storage.forecast_validation import validate_forecast_window_coverage_inputs
from route74.storage.helpers import WEEKDAYS
from route74.storage.models import ForecastCoverageBucket, ForecastWindowCoverageSummary


def summarize_yandex_forecast_window_coverage(
    connection: sqlite3.Connection,
    *,
    report_window: ReportWindow,
    current_date: datetime,
    days: int,
    min_samples: int,
    min_distinct_days: int,
    primary_bucket_minutes: int,
    fallback_bucket_minutes: int,
    max_age_seconds: int | None,
    step_minutes: int,
) -> ForecastWindowCoverageSummary:
    validate_forecast_window_coverage_inputs(
        current_date=current_date,
        days=days,
        min_samples=min_samples,
        min_distinct_days=min_distinct_days,
        primary_bucket_minutes=primary_bucket_minutes,
        fallback_bucket_minutes=fallback_bucket_minutes,
        max_age_seconds=max_age_seconds,
        step_minutes=step_minutes,
    )
    summaries = [
        summarize_yandex_forecast_readiness(
            connection,
            profile_key=report_window.profile_key,
            current_time=current_time,
            days=days,
            min_samples=min_samples,
            min_distinct_days=min_distinct_days,
            primary_bucket_minutes=primary_bucket_minutes,
            fallback_bucket_minutes=fallback_bucket_minutes,
            max_age_seconds=max_age_seconds,
            weekdays=WEEKDAYS,
            report_window_key=report_window.key,
        )
        for current_time in _window_times(current_date, report_window, step_minutes)
    ]
    first = summaries[0]
    return ForecastWindowCoverageSummary(
        window_key=report_window.key,
        profile_key=report_window.profile_key,
        days=days,
        min_samples=min_samples,
        min_distinct_days=min_distinct_days,
        total_samples=first.total_samples,
        eta_samples=first.eta_samples,
        fresh_eta_samples=first.fresh_eta_samples,
        traffic_samples=first.traffic_samples,
        latest_sampled_at=first.latest_sampled_at,
        buckets=tuple(
            ForecastCoverageBucket(
                label=summary.current_time.strftime("%H:%M"),
                ready=summary.ready,
                selected_sample_count=summary.selected_sample_count,
                selected_distinct_days=summary.selected_distinct_days,
                selected_bucket_minutes=summary.selected_bucket_minutes,
                primary_samples=summary.primary_samples,
                fallback_samples=summary.fallback_samples,
                primary_distinct_days=summary.primary_distinct_days,
                fallback_distinct_days=summary.fallback_distinct_days,
            )
            for summary in summaries
        ),
    )


def _window_times(
    current_date: datetime,
    report_window: ReportWindow,
    step_minutes: int,
) -> tuple[datetime, ...]:
    current = current_date.replace(
        hour=report_window.start.hour,
        minute=report_window.start.minute,
        second=0,
        microsecond=0,
    )
    end = current_date.replace(
        hour=report_window.end.hour,
        minute=report_window.end.minute,
        second=0,
        microsecond=0,
    )
    items: list[datetime] = []
    while current < end:
        items.append(current)
        current += timedelta(minutes=step_minutes)
    return tuple(items)
