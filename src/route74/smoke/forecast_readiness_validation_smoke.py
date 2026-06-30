from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from route74.cli.main import build_parser
from route74.domain.profiles import MORNING
from route74.domain.reporting import REPORT_WINDOWS
from route74.models import NOVOSIBIRSK_TZ
from route74.storage.forecast_coverage import summarize_yandex_forecast_window_coverage
from route74.storage.forecast_health import summarize_forecast_health
from route74.storage.forecast_readiness import summarize_yandex_forecast_readiness
from route74.storage.history import (
    load_yandex_eta_history_for_profile_window,
    load_yandex_forecast_sample_counts,
)
from route74.storage.helpers import count_table_rows
from route74.storage.models import ForecastCoverageBucket, ForecastReadinessSummary


def main() -> None:
    _assert_count_table_rows_validation()
    _assert_history_loader_validation()
    _assert_forecast_summary_model_validation()
    with sqlite3.connect(":memory:") as connection:
        _assert_value_error(
            lambda: _readiness(connection, fallback_bucket_minutes=30, primary_bucket_minutes=60),
            "fallback_bucket_minutes must be greater than or equal to primary_bucket_minutes",
        )
        _assert_value_error(
            lambda: _readiness(connection, max_age_seconds=-1),
            "max_age_seconds must be a non-negative integer or None",
        )
        _assert_value_error(
            lambda: _readiness(connection, min_samples=2, min_distinct_days=3),
            "min_distinct_days must not exceed min_samples",
        )
        _assert_value_error(
            lambda: _readiness(connection, current_time="now"),
            "current_time needs datetime",
        )
        _assert_value_error(
            lambda: _readiness(connection, current_time=datetime(2026, 6, 4, 9, 30)),
            "current_time needs timezone-aware datetime",
        )
        _assert_value_error(
            lambda: _readiness(connection, current_time=datetime(2026, 6, 4, 2, 30, tzinfo=timezone.utc)),
            "current_time needs Asia/Novosibirsk timezone",
        )
        _assert_value_error(
            lambda: _readiness(
                connection,
                current_time=datetime(2026, 6, 4, 9, 30, tzinfo=timezone(timedelta(hours=7))),
            ),
            "current_time needs Asia/Novosibirsk timezone",
        )
        _assert_value_error(
            lambda: _coverage(connection, step_minutes=0),
            "step_minutes must be a positive integer",
        )
        _assert_value_error(
            lambda: _coverage(connection, current_date="today"),
            "current_date needs datetime",
        )
        _assert_value_error(
            lambda: _coverage(connection, current_date=datetime(2026, 6, 4, 9, 30)),
            "current_date needs timezone-aware datetime",
        )
        _assert_value_error(
            lambda: _coverage(connection, current_date=datetime(2026, 6, 4, 2, 30, tzinfo=timezone.utc)),
            "current_date needs Asia/Novosibirsk timezone",
        )
        _assert_value_error(
            lambda: _coverage(
                connection,
                current_date=datetime(2026, 6, 4, 9, 30, tzinfo=timezone(timedelta(hours=7))),
            ),
            "current_date needs Asia/Novosibirsk timezone",
        )
        _assert_value_error(
            lambda: _health(connection, current_date="today"),
            "current_date needs datetime",
        )
        _assert_value_error(
            lambda: _health(connection, current_date=datetime(2026, 6, 4, 2, 30, tzinfo=timezone.utc)),
            "current_date needs Asia/Novosibirsk timezone",
        )
        _assert_value_error(
            lambda: _health(
                connection,
                current_date=datetime(2026, 6, 4, 9, 30, tzinfo=timezone(timedelta(hours=7))),
            ),
            "current_date needs Asia/Novosibirsk timezone",
        )
    _assert_cli_rejects(
        (
            "forecast-readiness",
            "--window",
            "weekday_morning_09_12",
            "--primary-bucket",
            "60",
            "--fallback-bucket",
            "30",
        ),
        "fallback_bucket_minutes must be greater than or equal to primary_bucket_minutes",
    )
    _assert_cli_rejects(
        ("forecast-readiness", "--window", "weekday_morning_09_12", "--min-samples", "2", "--min-days", "3"),
        "min_distinct_days must not exceed min_samples",
    )
    _assert_cli_rejects(
        ("forecast-coverage", "--window", "weekday_morning_09_12", "--primary-bucket", "60", "--fallback-bucket", "30"),
        "fallback_bucket_minutes must be greater than or equal to primary_bucket_minutes",
    )
    _assert_cli_rejects(
        ("forecast-health", "--primary-bucket", "60", "--fallback-bucket", "30"),
        "fallback_bucket_minutes must be greater than or equal to primary_bucket_minutes",
    )
    print("OK | forecast readiness validation smoke passed")


def _assert_count_table_rows_validation() -> None:
    with sqlite3.connect(":memory:") as connection:
        connection.execute("CREATE TABLE sample_table(id INTEGER)")
        connection.executemany("INSERT INTO sample_table(id) VALUES (?)", ((1,), (2,)))
        if count_table_rows(connection, "sample_table") != 2:
            raise AssertionError("expected sample_table row count to be 2")
        _assert_value_error(
            lambda: count_table_rows(connection, "sample_table; DROP TABLE sample_table"),
            "simple SQLite identifier",
        )
        if count_table_rows(connection, "sample_table") != 2:
            raise AssertionError("sample_table should survive rejected table name")


def _assert_history_loader_validation() -> None:
    current_time = datetime(2026, 6, 4, 9, 30, tzinfo=NOVOSIBIRSK_TZ)
    with sqlite3.connect(":memory:") as connection:
        _assert_value_error(
            lambda: load_yandex_eta_history_for_profile_window(
                connection,
                profile_key=MORNING.key,
                current_time=current_time,
                days=0,
                bucket_minutes=30,
            ),
            "days must be a positive integer",
        )
        _assert_value_error(
            lambda: load_yandex_eta_history_for_profile_window(
                connection,
                profile_key=MORNING.key,
                current_time=current_time,
                days=14,
                bucket_minutes=True,  # type: ignore[arg-type]
            ),
            "bucket_minutes must be a positive integer",
        )
        _assert_value_error(
            lambda: load_yandex_eta_history_for_profile_window(
                connection,
                profile_key=MORNING.key,
                current_time=current_time,
                days=14,
                bucket_minutes=30,
                weekdays=(),
            ),
            "weekdays must be a non-empty tuple",
        )
        _assert_value_error(
            lambda: load_yandex_eta_history_for_profile_window(
                connection,
                profile_key=MORNING.key,
                current_time=current_time,
                days=14,
                bucket_minutes=30,
                weekdays=(0, 7),
            ),
            "weekdays must contain weekday integers",
        )
        _assert_value_error(
            lambda: load_yandex_eta_history_for_profile_window(
                connection,
                profile_key=MORNING.key,
                current_time=current_time,
                days=14,
                bucket_minutes=30,
                report_window_key="night",
            ),
            "unknown report_window_key",
        )
        _assert_value_error(
            lambda: load_yandex_eta_history_for_profile_window(
                connection,
                profile_key=MORNING.key,
                current_time=datetime(2026, 6, 4, 9, 30),
                days=14,
                bucket_minutes=30,
            ),
            "current_time must be a timezone-aware datetime",
        )
        _assert_value_error(
            lambda: load_yandex_eta_history_for_profile_window(
                connection,
                profile_key=MORNING.key,
                current_time=current_time,
                days=14,
                bucket_minutes=30,
                before="now",  # type: ignore[arg-type]
            ),
            "before must be a timezone-aware datetime or None",
        )
        _assert_value_error(
            lambda: load_yandex_eta_history_for_profile_window(
                connection,
                profile_key=MORNING.key,
                current_time=current_time,
                days=14,
                bucket_minutes=30,
                before=datetime(2026, 6, 4, 9, 30),
            ),
            "before must be a timezone-aware datetime or None",
        )
        _assert_value_error(
            lambda: load_yandex_forecast_sample_counts(
                connection,
                profile_key="night",
                current_time=current_time,
                days=14,
            ),
            "profile_key must be one of",
        )
        _assert_value_error(
            lambda: load_yandex_forecast_sample_counts(
                connection,
                profile_key=MORNING.key,
                current_time="now",  # type: ignore[arg-type]
                days=14,
            ),
            "current_time must be a timezone-aware datetime",
        )
        _assert_value_error(
            lambda: load_yandex_forecast_sample_counts(
                connection,
                profile_key=MORNING.key,
                current_time=datetime(2026, 6, 4, 9, 30),
                days=14,
            ),
            "current_time must be a timezone-aware datetime",
        )
        _assert_value_error(
            lambda: load_yandex_forecast_sample_counts(
                connection,
                profile_key=MORNING.key,
                current_time=current_time,
                days=14,
                max_age_seconds=-1,
            ),
            "max_age_seconds must be a non-negative integer or None",
        )


def _assert_forecast_summary_model_validation() -> None:
    _assert_value_error(
        lambda: _readiness_summary(primary_samples=1, primary_distinct_days=2),
        "primary forecast readiness distinct days must not exceed samples",
    )
    _assert_value_error(
        lambda: ForecastCoverageBucket(
            label="09:00",
            ready=False,
            selected_sample_count=1,
            selected_distinct_days=2,
            selected_bucket_minutes=30,
            primary_samples=1,
            fallback_samples=0,
            primary_distinct_days=1,
            fallback_distinct_days=0,
        ),
        "selected forecast coverage distinct days must not exceed samples",
    )


def _readiness_summary(**overrides: object) -> ForecastReadinessSummary:
    values = {
        "profile_key": MORNING.key,
        "report_window_key": REPORT_WINDOWS[0].key,
        "current_time": datetime(2026, 6, 4, 9, 30, tzinfo=NOVOSIBIRSK_TZ),
        "days": 14,
        "min_samples": 20,
        "min_distinct_days": 3,
        "primary_bucket_minutes": 30,
        "fallback_bucket_minutes": 60,
        "max_age_seconds": 180,
        "total_samples": 1,
        "eta_samples": 1,
        "fresh_eta_samples": 1,
        "traffic_samples": 0,
        "primary_samples": 1,
        "fallback_samples": 0,
        "primary_distinct_days": 1,
        "fallback_distinct_days": 0,
        "latest_sampled_at": None,
    } | overrides
    return ForecastReadinessSummary(**values)  # type: ignore[arg-type]


def _readiness(connection: sqlite3.Connection, **overrides: object) -> None:
    params = {
        "profile_key": MORNING.key,
        "current_time": datetime(2026, 6, 4, 9, 30, tzinfo=NOVOSIBIRSK_TZ),
        "days": 14,
        "min_samples": 20,
        "min_distinct_days": 3,
        "primary_bucket_minutes": 30,
        "fallback_bucket_minutes": 60,
        "max_age_seconds": 180,
    }
    params.update(overrides)
    summarize_yandex_forecast_readiness(connection, **params)


def _coverage(connection: sqlite3.Connection, **overrides: object) -> None:
    params = {
        "report_window": REPORT_WINDOWS[0],
        "current_date": datetime(2026, 6, 4, 9, 30, tzinfo=NOVOSIBIRSK_TZ),
        "days": 14,
        "min_samples": 20,
        "min_distinct_days": 3,
        "primary_bucket_minutes": 30,
        "fallback_bucket_minutes": 60,
        "max_age_seconds": 180,
        "step_minutes": 30,
    }
    params.update(overrides)
    summarize_yandex_forecast_window_coverage(connection, **params)


def _health(connection: sqlite3.Connection, **overrides: object) -> None:
    params = {
        "current_date": datetime(2026, 6, 4, 9, 30, tzinfo=NOVOSIBIRSK_TZ),
        "days": 14,
        "min_samples": 20,
        "min_distinct_days": 3,
        "primary_bucket_minutes": 30,
        "fallback_bucket_minutes": 60,
        "max_age_seconds": 180,
        "step_minutes": 30,
    }
    params.update(overrides)
    summarize_forecast_health(connection, **params)


def _assert_value_error(action: Callable[[], object], expected: str) -> None:
    try:
        action()
    except ValueError as exc:
        if expected in str(exc):
            return
        raise AssertionError(f"expected {expected!r}, got {str(exc)!r}") from exc
    raise AssertionError(f"expected ValueError mentioning {expected!r}")


def _assert_cli_rejects(args: tuple[str, ...], expected: str) -> None:
    parsed = build_parser().parse_args(args)
    with TemporaryDirectory() as temp_dir:
        parsed.db = Path(temp_dir) / "cli-invalid.sqlite"
        try:
            parsed.func(parsed)
        except SystemExit as exc:
            if expected in str(exc):
                return
            raise AssertionError(f"expected {expected!r}, got {str(exc)!r}") from exc
    raise AssertionError(f"expected CLI args {args!r} to fail")


if __name__ == "__main__":
    main()
