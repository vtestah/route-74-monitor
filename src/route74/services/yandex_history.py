from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from math import ceil
from pathlib import Path

from route74.domain.commute import CommuteProfile
from route74.domain.reporting import matching_report_window
from route74.domain.yandex_history import (
    DEFAULT_HISTORY_PERCENTILE,
    YandexHistoryPrediction,
    YandexHistoryScope,
)
from route74.models import now_local
from route74.storage import (
    DEFAULT_DB,
    STORAGE_READ_ERRORS,
    connect,
    init_db,
    load_yandex_eta_history_for_profile_window,
)
from route74.storage.forecast_backtest import (
    summarize_yandex_forecast_backtest,
    validate_forecast_backtest_percentiles,
)
from route74.storage.helpers import day_kind_weekdays

DEFAULT_HISTORY_DAYS = 14
DEFAULT_MIN_OBSERVATIONS = 20
DEFAULT_MIN_HISTORY_DAYS = 3
DEFAULT_PRIMARY_BUCKET_MINUTES = 30
DEFAULT_FALLBACK_BUCKET_MINUTES = 60
DEFAULT_HISTORY_MAX_AGE_SECONDS = 180
DEFAULT_BACKTEST_MIN_EVALUATED = 5
DEFAULT_BACKTEST_CACHE_SECONDS = 300


class YandexHistoryPredictor:
    def __init__(
        self,
        *,
        db_path: Path = DEFAULT_DB,
        window_days: int = DEFAULT_HISTORY_DAYS,
        min_observations: int = DEFAULT_MIN_OBSERVATIONS,
        min_history_days: int = DEFAULT_MIN_HISTORY_DAYS,
        primary_bucket_minutes: int = DEFAULT_PRIMARY_BUCKET_MINUTES,
        fallback_bucket_minutes: int = DEFAULT_FALLBACK_BUCKET_MINUTES,
        percentile: int = DEFAULT_HISTORY_PERCENTILE,
        backtest_percentiles: tuple[int, ...] = (),
        same_day_kind: bool = True,
        max_age_seconds: int | None = DEFAULT_HISTORY_MAX_AGE_SECONDS,
        report_window_scope: bool = True,
    ) -> None:
        self._db_path = db_path
        self._window_days = _positive_int("window_days", window_days)
        self._min_observations = _positive_int("min_observations", min_observations)
        self._min_history_days = _positive_int("min_history_days", min_history_days)
        if self._min_history_days > self._min_observations:
            raise ValueError("min_history_days must not exceed min_observations")
        primary_bucket = _positive_int("primary_bucket_minutes", primary_bucket_minutes)
        fallback_bucket = _positive_int("fallback_bucket_minutes", fallback_bucket_minutes)
        if fallback_bucket < primary_bucket:
            raise ValueError("fallback_bucket_minutes must be greater than or equal to primary_bucket_minutes")
        self._buckets = (primary_bucket, fallback_bucket)
        self._percentile = _percentile_value(percentile)
        self._backtest_percentiles = _backtest_percentiles_value(backtest_percentiles)
        self._backtest_cache: dict[tuple[str, str, int], tuple[datetime, int]] = {}
        self._same_day_kind = _bool_value("same_day_kind", same_day_kind)
        self._max_age_seconds = _non_negative_int_or_none("max_age_seconds", max_age_seconds)
        self._report_window_scope = _bool_value("report_window_scope", report_window_scope)

    def predict(self, profile: CommuteProfile) -> YandexHistoryPrediction:
        current_time = now_local()
        return self.predict_at(profile, current_time)

    def predict_at(self, profile: CommuteProfile, current_time: datetime) -> YandexHistoryPrediction:
        report_window_key = self._report_window_key(profile, current_time)
        scope = _history_scope(report_window_key)
        best_count = 0
        best_days = 0
        best_bucket = self._buckets[0]
        with connect(self._db_path) as connection:
            init_db(connection)
            weekdays = day_kind_weekdays(current_time) if self._same_day_kind else None
            for bucket_minutes in self._buckets:
                history = load_yandex_eta_history_for_profile_window(
                    connection,
                    profile_key=profile.key,
                    current_time=current_time,
                    days=self._window_days,
                    bucket_minutes=bucket_minutes,
                    weekdays=weekdays,
                    max_age_seconds=self._max_age_seconds,
                    report_window_key=report_window_key,
                    before=current_time,
                )
                values = history.arrival_minutes
                if (len(values), history.distinct_service_days) > (
                    best_count,
                    best_days,
                ):
                    best_count = len(values)
                    best_days = history.distinct_service_days
                    best_bucket = bucket_minutes
                if len(values) >= self._min_observations and history.distinct_service_days >= self._min_history_days:
                    percentile = self._select_percentile(
                        connection,
                        profile=profile,
                        report_window_key=report_window_key,
                        bucket_minutes=bucket_minutes,
                        current_time=current_time,
                    )
                    return YandexHistoryPrediction(
                        available=True,
                        arrival_minutes=_percentile(values, percentile),
                        sample_count=len(values),
                        bucket_minutes=bucket_minutes,
                        window_days=self._window_days,
                        percentile=percentile,
                        fallback_reason="",
                        scope=scope,
                        report_window_key=report_window_key or "",
                    )
        return YandexHistoryPrediction.unavailable(
            sample_count=best_count,
            bucket_minutes=best_bucket,
            window_days=self._window_days,
            percentile=self._percentile,
            reason=(
                f"insufficient_history:{best_count}/{self._min_observations};days:{best_days}/{self._min_history_days}"
            ),
            scope=scope,
            report_window_key=report_window_key or "",
        )

    def _select_percentile(
        self,
        connection: sqlite3.Connection,
        *,
        profile: CommuteProfile,
        report_window_key: str | None,
        bucket_minutes: int,
        current_time: datetime,
    ) -> int:
        if not self._backtest_percentiles:
            return self._percentile
        cache_key = (profile.key, report_window_key or "", bucket_minutes)
        cached = self._backtest_cache.get(cache_key)
        if cached is not None and _cache_fresh(cached[0], current_time):
            return cached[1]
        percentile = self._select_uncached_percentile(
            connection,
            profile=profile,
            report_window_key=report_window_key,
            bucket_minutes=bucket_minutes,
        )
        self._backtest_cache[cache_key] = (current_time, percentile)
        return percentile

    def _select_uncached_percentile(
        self,
        connection: sqlite3.Connection,
        *,
        profile: CommuteProfile,
        report_window_key: str | None,
        bucket_minutes: int,
    ) -> int:
        try:
            summary = summarize_yandex_forecast_backtest(
                connection,
                profile_key=profile.key,
                report_window_key=report_window_key or "",
                history_days=self._window_days,
                bucket_minutes=bucket_minutes,
                min_samples=self._min_observations,
                min_distinct_days=self._min_history_days,
                percentiles=self._backtest_percentiles,
                max_age_seconds=self._max_age_seconds,
            )
        except STORAGE_READ_ERRORS:
            return self._percentile
        selected = summary.selected_result
        if selected is None or selected.evaluated_cases < DEFAULT_BACKTEST_MIN_EVALUATED:
            return self._percentile
        return selected.percentile

    def _report_window_key(self, profile: CommuteProfile, current_time: datetime) -> str | None:
        if not self._report_window_scope:
            return None
        window = matching_report_window(current_time, profile.key)
        return window.key if window is not None else None


def _history_scope(report_window_key: str | None) -> YandexHistoryScope:
    if report_window_key is not None:
        return YandexHistoryScope.REPORT_WINDOW
    return YandexHistoryScope.PROFILE_TIME


def _percentile(values: tuple[int, ...], percentile: int) -> int:
    ordered = sorted(values)
    index = max(0, ceil(percentile / 100 * len(ordered)) - 1)
    return ordered[index]


def _positive_int(name: str, value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _non_negative_int_or_none(name: str, value: int | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer or None")
    return value


def _bool_value(name: str, value: object) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be a boolean")
    return value


def _percentile_value(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 100:
        raise ValueError("percentile must be an integer from 1 to 100")
    return value


def _backtest_percentiles_value(value: tuple[int, ...]) -> tuple[int, ...]:
    if not value:
        return ()
    return validate_forecast_backtest_percentiles(value)


def _cache_fresh(cached_at: datetime, current_time: datetime) -> bool:
    age = current_time - cached_at
    return timedelta(0) <= age <= timedelta(seconds=DEFAULT_BACKTEST_CACHE_SECONDS)
