from __future__ import annotations

import sqlite3
from collections import Counter
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from route74.models import NOVOSIBIRSK_TZ
from route74.storage.helpers import (
    WEEKDAYS,
    WEEKENDS,
    count_rows,
    count_table_rows,
    day_kind_weekdays,
    within_time_bucket,
)
from route74.storage.models import (
    CollectorRunSummary,
    CountByKey,
    ForecastCoverageBucket,
    ForecastReadinessSummary,
    ForecastWindowCoverageSummary,
    ReportWindowSummary,
    YandexTelemetrySummary,
    percent,
)


def main() -> None:
    _assert_count_by_key_contract()
    _assert_count_rows_contract()
    _assert_summary_count_contract()
    _assert_count_table_rows_identifier_contract()
    _assert_time_bucket_contract()
    _assert_day_kind_contract()
    _assert_equal(percent(0, 0), 0)
    _assert_equal(percent(0, 5), 0)
    _assert_equal(percent(1, 2), 50)
    _assert_equal(percent(2, 3), 67)
    _assert_equal(percent(5, 5), 100)

    _assert_rejects(lambda: percent(True, 1), "non-negative integer")
    _assert_rejects(lambda: percent(1, False), "non-negative integer")
    _assert_rejects(lambda: percent(-1, 5), "non-negative integer")
    _assert_rejects(lambda: percent(1, -5), "non-negative integer")
    _assert_rejects(lambda: percent(1, 0), "must not exceed")
    _assert_rejects(lambda: percent(6, 5), "must not exceed")
    print("OK | storage models smoke passed")


def _assert_count_by_key_contract() -> None:
    _assert_equal(CountByKey("  parse_error\nblocked  ", 2), CountByKey("parse_error blocked", 2))
    _assert_equal(CountByKey("", 1).key, "-")
    _assert_rejects(lambda: CountByKey(42, 1), "count key needs text")
    _assert_rejects(lambda: CountByKey("ok", -1), "non-negative integer")
    _assert_rejects(lambda: CountByKey("ok", True), "non-negative integer")


def _assert_count_rows_contract() -> None:
    counts = Counter(("ok", "blocked", "ok", "blocked", "parse_error", "parse_error", "parse_error"))
    _assert_equal(
        count_rows(counts),
        (
            CountByKey("parse_error", 3),
            CountByKey("blocked", 2),
            CountByKey("ok", 2),
        ),
    )


def _assert_summary_count_contract() -> None:
    _assert_rejects(
        lambda: _telemetry_summary(hours=0),
        "Yandex telemetry hours needs positive integer",
    )
    _assert_rejects(
        lambda: _telemetry_summary(profile_key=42),
        "Yandex telemetry profile key needs text",
    )
    _assert_rejects(
        lambda: _telemetry_summary(profile_key=" "),
        "Yandex telemetry profile key is required",
    )
    _assert_rejects(
        lambda: _telemetry_summary(eta_snapshots=2),
        "Yandex telemetry ETA snapshots must not exceed total snapshots",
    )
    _assert_rejects(
        lambda: _telemetry_summary(eta_observations=2),
        "Yandex telemetry ETA observations must not exceed total observations",
    )
    _assert_rejects(
        lambda: _telemetry_summary(latest_sampled_at=datetime(2026, 6, 4, 9, 0)),
        "Yandex telemetry latest_sampled_at must be timezone-aware",
    )
    _assert_rejects(
        lambda: _telemetry_summary(heartbeat=object()),
        "Yandex telemetry heartbeat needs CollectorHeartbeat",
    )
    _assert_rejects(
        lambda: _telemetry_summary(collector_runs=object()),
        "Yandex telemetry collector runs need CollectorRunSummary",
    )
    _assert_rejects(
        lambda: _telemetry_summary(collector_runs=_collector_runs(hours=12)),
        "Yandex telemetry collector runs hours must match hours",
    )
    _assert_rejects(
        lambda: _telemetry_summary(statuses=(object(),)),
        "Yandex telemetry statuses needs CountByKey tuple",
    )
    _assert_rejects(
        lambda: _telemetry_summary(methods=[CountByKey("ok", 1)]),
        "Yandex telemetry methods needs CountByKey tuple",
    )
    _assert_rejects(
        lambda: _report_window_summary(traffic_samples=2),
        "report window traffic samples must not exceed total samples",
    )
    _assert_rejects(lambda: _report_window_summary(days=0), "report window days needs positive integer")
    _assert_rejects(
        lambda: _report_window_summary(latest_sampled_at=datetime(2026, 6, 4, 9, 0)),
        "report window latest_sampled_at must be timezone-aware",
    )
    _assert_rejects(
        lambda: _report_window_summary(statuses=(object(),)),
        "report window statuses needs CountByKey tuple",
    )
    _assert_rejects(
        lambda: _readiness_summary(current_time=datetime(2026, 6, 4, 9, 0)),
        "forecast readiness current_time must be timezone-aware",
    )
    _assert_rejects(lambda: _readiness_summary(days=0), "forecast readiness days needs positive integer")
    _assert_rejects(
        lambda: _readiness_summary(min_samples=0),
        "forecast readiness min samples needs positive integer",
    )
    _assert_rejects(
        lambda: _readiness_summary(min_samples=1, min_distinct_days=2),
        "forecast readiness min distinct days must not exceed min samples",
    )
    _assert_rejects(
        lambda: _readiness_summary(primary_bucket_minutes=0),
        "forecast readiness primary bucket minutes needs positive integer",
    )
    _assert_rejects(
        lambda: _readiness_summary(primary_bucket_minutes=60, fallback_bucket_minutes=30),
        "forecast readiness fallback bucket must not be below primary bucket",
    )
    _assert_rejects(
        lambda: _readiness_summary(max_age_seconds=True),
        "forecast readiness max age seconds needs non-negative integer",
    )
    _assert_rejects(
        lambda: _readiness_summary(latest_sampled_at=datetime(2026, 6, 4, 9, 0)),
        "forecast readiness latest_sampled_at must be timezone-aware",
    )
    _assert_rejects(
        lambda: _readiness_summary(eta_samples=1, fresh_eta_samples=2),
        "forecast readiness fresh ETA samples must not exceed ETA samples",
    )
    _assert_rejects(
        lambda: _coverage_bucket(ready=1),
        "forecast coverage ready needs bool",
    )
    _assert_rejects(
        lambda: _coverage_bucket(selected_bucket_minutes=0),
        "selected forecast coverage bucket minutes needs positive integer",
    )
    _assert_rejects(
        lambda: _readiness_summary(fresh_eta_samples=1, primary_samples=2),
        "forecast readiness primary samples must not exceed fresh ETA samples",
    )
    _assert_rejects(
        lambda: _coverage_summary(
            buckets=(
                ForecastCoverageBucket(
                    label="09:00",
                    ready=False,
                    selected_sample_count=2,
                    selected_distinct_days=1,
                    selected_bucket_minutes=30,
                    primary_samples=1,
                    fallback_samples=1,
                    primary_distinct_days=1,
                    fallback_distinct_days=1,
                ),
            ),
        ),
        "forecast window coverage selected samples must not exceed fresh ETA samples",
    )
    _assert_rejects(
        lambda: _coverage_summary(buckets=[]),  # type: ignore[arg-type]
        "forecast window coverage buckets need ForecastCoverageBucket tuple",
    )
    _assert_rejects(
        lambda: _coverage_summary(days=0),
        "forecast window coverage days needs positive integer",
    )
    _assert_rejects(
        lambda: _coverage_summary(min_samples=1, min_distinct_days=2),
        "forecast window coverage min distinct days must not exceed min samples",
    )
    _assert_rejects(
        lambda: _coverage_summary(latest_sampled_at=datetime(2026, 6, 4, 9, 0)),
        "forecast window coverage latest_sampled_at must be timezone-aware",
    )


def _collector_runs(**overrides: object) -> CollectorRunSummary:
    values = {
        "name": "yandex-collect",
        "hours": 24,
        "total_runs": 1,
        "result_runs": 1,
        "eta_runs": 1,
        "traffic_ok_runs": 1,
        "skipped_runs": 0,
        "latest_started_at": None,
        "statuses": (),
    } | overrides
    return CollectorRunSummary(**values)  # type: ignore[arg-type]


def _report_window_summary(**overrides: object) -> ReportWindowSummary:
    values = {
        "days": 14,
        "report_window_key": None,
        "profile_key": None,
        "total_samples": 1,
        "eta_samples": 1,
        "traffic_samples": 1,
        "latest_sampled_at": None,
        "statuses": (),
    } | overrides
    return ReportWindowSummary(**values)  # type: ignore[arg-type]


def _telemetry_summary(**overrides: object) -> YandexTelemetrySummary:
    values = {
        "profile_key": None,
        "hours": 24,
        "total_snapshots": 1,
        "eta_snapshots": 1,
        "vehicle_snapshots": 1,
        "total_observations": 1,
        "eta_observations": 1,
        "latest_sampled_at": None,
        "heartbeat": None,
        "collector_runs": _collector_runs(),
        "statuses": (),
        "methods": (),
    } | overrides
    return YandexTelemetrySummary(**values)  # type: ignore[arg-type]


def _readiness_summary(**overrides: object) -> ForecastReadinessSummary:
    values = {
        "profile_key": "morning",
        "report_window_key": None,
        "current_time": datetime(2026, 6, 4, 9, 0, tzinfo=NOVOSIBIRSK_TZ),
        "days": 14,
        "min_samples": 20,
        "min_distinct_days": 3,
        "primary_bucket_minutes": 30,
        "fallback_bucket_minutes": 60,
        "max_age_seconds": 180,
        "total_samples": 2,
        "eta_samples": 2,
        "fresh_eta_samples": 2,
        "traffic_samples": 1,
        "primary_samples": 1,
        "fallback_samples": 1,
        "primary_distinct_days": 1,
        "fallback_distinct_days": 1,
        "latest_sampled_at": None,
    } | overrides
    return ForecastReadinessSummary(**values)  # type: ignore[arg-type]


def _coverage_bucket(**overrides: object) -> ForecastCoverageBucket:
    values = {
        "label": "09:00",
        "ready": False,
        "selected_sample_count": 1,
        "selected_distinct_days": 1,
        "selected_bucket_minutes": 30,
        "primary_samples": 1,
        "fallback_samples": 1,
        "primary_distinct_days": 1,
        "fallback_distinct_days": 1,
    } | overrides
    return ForecastCoverageBucket(**values)  # type: ignore[arg-type]


def _coverage_summary(**overrides: object) -> ForecastWindowCoverageSummary:
    values = {
        "window_key": "weekday_morning_09_12",
        "profile_key": "morning",
        "days": 14,
        "min_samples": 20,
        "min_distinct_days": 3,
        "total_samples": 2,
        "eta_samples": 2,
        "fresh_eta_samples": 1,
        "traffic_samples": 1,
        "latest_sampled_at": None,
        "buckets": (_coverage_bucket(),),
    } | overrides
    return ForecastWindowCoverageSummary(**values)  # type: ignore[arg-type]


def _assert_count_table_rows_identifier_contract() -> None:
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("CREATE TABLE sample_table(id INTEGER PRIMARY KEY)")
        connection.execute("INSERT INTO sample_table DEFAULT VALUES")
        _assert_equal(count_table_rows(connection, "sample_table"), 1)
        _assert_rejects(lambda: count_table_rows(connection, "таблица"), "simple SQLite identifier")
        _assert_rejects(lambda: count_table_rows(connection, "sample-table"), "simple SQLite identifier")
        _assert_rejects(lambda: count_table_rows(connection, "1sample_table"), "simple SQLite identifier")
    finally:
        connection.close()


def _assert_time_bucket_contract() -> None:
    current_time = datetime(2026, 6, 4, 9, 0, tzinfo=NOVOSIBIRSK_TZ)
    _assert_equal(
        within_time_bucket(datetime(2026, 6, 4, 8, 55, tzinfo=NOVOSIBIRSK_TZ), current_time, 5),
        True,
    )
    _assert_equal(
        within_time_bucket(datetime(2026, 6, 4, 8, 54, tzinfo=NOVOSIBIRSK_TZ), current_time, 5),
        False,
    )
    _assert_equal(
        within_time_bucket(
            datetime(2026, 6, 4, 23, 58, tzinfo=NOVOSIBIRSK_TZ),
            datetime(2026, 6, 5, 0, 2, tzinfo=NOVOSIBIRSK_TZ),
            4,
        ),
        True,
    )
    _assert_equal(within_time_bucket(current_time, current_time, 0), True)
    _assert_equal(
        within_time_bucket(datetime(2026, 6, 4, 2, 3, tzinfo=timezone.utc), current_time, 3),
        True,
    )
    _assert_equal(
        within_time_bucket(datetime(2026, 6, 4, 2, 3, tzinfo=timezone.utc), current_time, 2),
        False,
    )

    _assert_rejects(
        lambda: within_time_bucket(datetime(2026, 6, 4, 9, 0), current_time, 5),
        "timezone-aware",
    )
    _assert_rejects(
        lambda: within_time_bucket(current_time, datetime(2026, 6, 4, 9, 0), 5),
        "timezone-aware",
    )
    _assert_rejects(
        lambda: within_time_bucket("now", current_time, 5),  # type: ignore[arg-type]
        "datetime",
    )
    _assert_rejects(
        lambda: within_time_bucket(current_time, current_time, -1),
        "non-negative integer",
    )
    _assert_rejects(
        lambda: within_time_bucket(current_time, current_time, True),
        "non-negative integer",
    )


def _assert_day_kind_contract() -> None:
    _assert_equal(
        day_kind_weekdays(datetime(2026, 6, 4, 9, 0, tzinfo=NOVOSIBIRSK_TZ)),
        WEEKDAYS,
    )
    _assert_equal(
        day_kind_weekdays(datetime(2026, 6, 7, 9, 0, tzinfo=NOVOSIBIRSK_TZ)),
        WEEKENDS,
    )
    _assert_equal(
        day_kind_weekdays(datetime(2026, 6, 5, 20, 0, tzinfo=timezone.utc)),
        WEEKENDS,
    )
    _assert_equal(
        day_kind_weekdays(
            datetime(2026, 6, 7, 9, 0, tzinfo=timezone(timedelta(hours=7))),
        ),
        WEEKENDS,
    )
    _assert_rejects(
        lambda: day_kind_weekdays("2026-06-04 09:00"),  # type: ignore[arg-type]
        "must be a datetime",
    )
    _assert_rejects(
        lambda: day_kind_weekdays(datetime(2026, 6, 4, 9, 0)),
        "timezone-aware",
    )


def _assert_equal(actual: object, expected: object) -> None:
    if actual != expected:
        raise AssertionError(f"expected {expected!r}, got {actual!r}")


def _assert_rejects(call: Callable[[], object], expected_message: str) -> None:
    try:
        call()
    except ValueError as error:
        if expected_message not in str(error):
            raise AssertionError(f"expected {expected_message!r} in {error!s}") from error
        return
    raise AssertionError(f"expected validation error: {expected_message}")


if __name__ == "__main__":
    main()
