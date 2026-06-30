from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, time, timedelta

from route74.cli.common import local_time_hhmm, positive_int
from route74.cli.forecast_formatting import format_forecast_readiness_summary
from route74.domain.profiles import PROFILE_KEYS
from route74.domain.reporting import (
    REPORT_WINDOWS_BY_KEY as WINDOWS_BY_KEY,
)
from route74.domain.reporting import (
    ReportWindow,
)
from route74.models import now_local
from route74.services.yandex_history import (
    DEFAULT_FALLBACK_BUCKET_MINUTES,
    DEFAULT_HISTORY_DAYS,
    DEFAULT_HISTORY_MAX_AGE_SECONDS,
    DEFAULT_MIN_HISTORY_DAYS,
    DEFAULT_MIN_OBSERVATIONS,
    DEFAULT_PRIMARY_BUCKET_MINUTES,
)
from route74.storage import connect, init_db, summarize_yandex_forecast_readiness
from route74.storage.helpers import WEEKDAYS, WEEKENDS, day_kind_weekdays


@dataclass(frozen=True)
class ReadinessTarget:
    profile_key: str
    report_window_key: str | None
    current_time: datetime
    weekdays: tuple[int, ...] | None


def register_forecast_readiness_command(subparsers: argparse._SubParsersAction) -> None:
    readiness = subparsers.add_parser("forecast-readiness", help="Check whether Yandex history has enough samples.")
    readiness.add_argument("--window", choices=tuple(WINDOWS_BY_KEY), default=None)
    readiness.add_argument("--profile", choices=PROFILE_KEYS, default=None)
    readiness.add_argument("--at", type=local_time_hhmm, default=None, help="Local time HH:MM to evaluate.")
    readiness.add_argument("--day-kind", choices=["auto", "weekday", "weekend", "all"], default="auto")
    readiness.add_argument("--days", type=positive_int, default=DEFAULT_HISTORY_DAYS)
    readiness.add_argument("--min-samples", type=positive_int, default=DEFAULT_MIN_OBSERVATIONS)
    readiness.add_argument("--min-days", type=positive_int, default=DEFAULT_MIN_HISTORY_DAYS)
    readiness.add_argument("--primary-bucket", type=positive_int, default=DEFAULT_PRIMARY_BUCKET_MINUTES)
    readiness.add_argument("--fallback-bucket", type=positive_int, default=DEFAULT_FALLBACK_BUCKET_MINUTES)
    readiness.add_argument("--max-age-seconds", type=positive_int, default=DEFAULT_HISTORY_MAX_AGE_SECONDS)
    readiness.set_defaults(func=cmd_forecast_readiness)


def cmd_forecast_readiness(args: argparse.Namespace) -> None:
    target = _target(args.window, args.profile, args.at, args.day_kind)
    try:
        with connect(args.db) as connection:
            init_db(connection)
            summary = summarize_yandex_forecast_readiness(
                connection,
                profile_key=target.profile_key,
                current_time=target.current_time,
                days=args.days,
                min_samples=args.min_samples,
                min_distinct_days=args.min_days,
                primary_bucket_minutes=args.primary_bucket,
                fallback_bucket_minutes=args.fallback_bucket,
                max_age_seconds=args.max_age_seconds,
                weekdays=target.weekdays,
                report_window_key=target.report_window_key,
            )
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
    print(format_forecast_readiness_summary(summary, args.db))


def _target(
    window_key: str | None,
    profile_key: str | None,
    raw_time: time | None,
    day_kind: str,
) -> ReadinessTarget:
    window = WINDOWS_BY_KEY.get(window_key) if window_key is not None else None
    profile = _profile_key(window, profile_key)
    target_time = _target_time(window, raw_time)
    current_time = _readiness_time(target_time)
    report_window_key = window.key if window is not None else None
    return ReadinessTarget(
        profile,
        report_window_key,
        current_time,
        _weekdays(window, day_kind, current_time),
    )


def _profile_key(window: ReportWindow | None, profile_key: str | None) -> str:
    if window is None:
        if profile_key is None:
            raise SystemExit("forecast-readiness needs --window or --profile")
        return profile_key
    if profile_key is not None and profile_key != window.profile_key:
        raise SystemExit(f"--profile {profile_key} conflicts with --window {window.key}")
    return window.profile_key


def _target_time(window: ReportWindow | None, raw_time: time | None) -> time:
    if raw_time is not None:
        return raw_time
    if window is None:
        return now_local().timetz().replace(tzinfo=None)
    return _window_default_time(window)


def _window_default_time(window: ReportWindow) -> time:
    start = datetime.combine(now_local().date(), window.start)
    return (start + timedelta(minutes=DEFAULT_PRIMARY_BUCKET_MINUTES)).time()


def _readiness_time(target_time: time) -> datetime:
    current_time = now_local()
    return current_time.replace(hour=target_time.hour, minute=target_time.minute, second=0, microsecond=0)


def _weekdays(window: ReportWindow | None, day_kind: str, current_time: datetime) -> tuple[int, ...] | None:
    if window is not None and day_kind == "auto":
        return WEEKDAYS
    if day_kind == "weekday":
        return WEEKDAYS
    if day_kind == "weekend":
        return WEEKENDS
    if day_kind == "all":
        return None
    return day_kind_weekdays(current_time)
