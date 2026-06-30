from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from route74.domain.eta import EtaConfidence
from route74.domain.profiles import MORNING
from route74.domain.reporting import REPORT_WINDOWS
from route74.models import NOVOSIBIRSK_TZ
from route74.sources.yandex.models import YandexLiveForecast, YandexSourceMethod, YandexSourceStatus
from route74.storage import connect, init_db, insert_yandex_snapshot
from route74.storage.forecast_backtest import (
    ForecastBacktestSummary,
    summarize_yandex_forecast_backtest,
    validate_forecast_backtest_percentiles,
)
from route74.storage.forecast_backtest_cases import normalized_forecast_cases


def assert_percentile_validation() -> None:
    with TemporaryDirectory() as temp_dir:
        _assert_value_error(
            lambda: _summary(Path(temp_dir) / "invalid-percentiles.sqlite", percentiles=(101,)),
            "expected percentiles from 1 to 100",
        )
    _assert_value_error(
        lambda: validate_forecast_backtest_percentiles(()),
        "expected percentiles from 1 to 100",
    )
    _assert_value_error(
        lambda: validate_forecast_backtest_percentiles((0, 80)),
        "expected percentiles from 1 to 100",
    )
    _assert_value_error(
        lambda: validate_forecast_backtest_percentiles((True, 80)),  # type: ignore[arg-type]
        "expected percentiles from 1 to 100",
    )
    _assert_value_error(
        lambda: validate_forecast_backtest_percentiles((80, 80)),
        "expected unique percentiles",
    )
    _assert_positive_parameter_validation()
    _assert_threshold_relationship_validation()
    _assert_slot_minutes_validation()
    _assert_malformed_samples_are_ignored()
    _assert_invalid_age_samples_are_ignored()


def _assert_positive_parameter_validation() -> None:
    bad_values = (
        ("history_days", 0),
        ("bucket_minutes", 0),
        ("min_samples", 0),
        ("min_distinct_days", 0),
        ("max_age_seconds", 0),
    )
    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "invalid-parameters.sqlite"
        for name, value in bad_values:
            _assert_value_error(
                lambda name=name, value=value: _summary(db_path, **{name: value}),
                f"expected positive {name}",
            )


def _assert_threshold_relationship_validation() -> None:
    with TemporaryDirectory() as temp_dir:
        _assert_value_error(
            lambda: _summary(Path(temp_dir) / "impossible-thresholds.sqlite", min_samples=2, min_distinct_days=3),
            "min_distinct_days must not exceed min_samples",
        )


def _assert_slot_minutes_validation() -> None:
    _assert_value_error(
        lambda: normalized_forecast_cases((), slot_minutes=0),
        "expected positive slot_minutes",
    )
    _assert_value_error(
        lambda: normalized_forecast_cases((), slot_minutes=True),  # type: ignore[arg-type]
        "expected positive slot_minutes",
    )


def _assert_malformed_samples_are_ignored() -> None:
    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "malformed-samples.sqlite"
        _insert_malformed_samples(db_path)
        summary = _summary(db_path, percentiles=(80,))
    _assert_equal(summary.target_cases, 1)
    _assert_equal(summary.results[0].evaluated_cases, 0)
    _assert_equal(summary.results[0].skipped_cases, 1)


def _assert_invalid_age_samples_are_ignored() -> None:
    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "invalid-age-samples.sqlite"
        _insert_invalid_age_samples(db_path)
        uncapped = _summary(
            db_path,
            min_samples=1,
            min_distinct_days=1,
            percentiles=(80,),
            max_age_seconds=None,
        )
        summary = _summary(
            db_path,
            min_samples=1,
            min_distinct_days=1,
            percentiles=(80,),
            max_age_seconds=180,
        )
    _assert_equal(uncapped.target_cases, 1)
    _assert_equal(uncapped.results[0].evaluated_cases, 0)
    _assert_equal(uncapped.results[0].skipped_cases, 1)
    _assert_equal(summary.target_cases, 1)
    _assert_equal(summary.results[0].evaluated_cases, 0)
    _assert_equal(summary.results[0].skipped_cases, 1)


def _summary(
    db_path: Path,
    *,
    history_days: int = 14,
    bucket_minutes: int = 30,
    min_samples: int = 3,
    min_distinct_days: int = 3,
    percentiles: tuple[int, ...] = (80,),
    max_age_seconds: int | None = 180,
) -> ForecastBacktestSummary:
    with connect(db_path) as connection:
        init_db(connection)
        return summarize_yandex_forecast_backtest(
            connection,
            profile_key=MORNING.key,
            report_window_key=REPORT_WINDOWS[0].key,
            history_days=history_days,
            bucket_minutes=bucket_minutes,
            min_samples=min_samples,
            min_distinct_days=min_distinct_days,
            percentiles=percentiles,
            max_age_seconds=max_age_seconds,
        )


def _insert_malformed_samples(db_path: Path) -> None:
    base = datetime(2026, 6, 1, 9, 30, tzinfo=NOVOSIBIRSK_TZ)
    with connect(db_path) as connection:
        init_db(connection)
        bad_shape_snapshot_id = insert_yandex_snapshot(connection, MORNING.key, _forecast(10), base)
        bad_service_date_snapshot_id = insert_yandex_snapshot(
            connection,
            MORNING.key,
            _forecast(15),
            base + timedelta(days=1),
        )
        bad_service_date_mismatch_snapshot_id = insert_yandex_snapshot(
            connection,
            MORNING.key,
            _forecast(18),
            base + timedelta(days=2),
        )
        bad_weekday_snapshot_id = insert_yandex_snapshot(
            connection,
            MORNING.key,
            _forecast(25),
            base + timedelta(days=3),
        )
        naive_sampled_at_snapshot_id = insert_yandex_snapshot(
            connection,
            MORNING.key,
            _forecast(28),
            base + timedelta(days=4),
        )
        bad_minute_snapshot_id = insert_yandex_snapshot(
            connection,
            MORNING.key,
            _forecast(30),
            base + timedelta(days=5),
        )
        insert_yandex_snapshot(connection, MORNING.key, _forecast(20), base + timedelta(days=7))
        connection.execute(
            """
            UPDATE yandex_forecast_samples
            SET sampled_at = 'not-a-date',
                minute_of_day = 'bad',
                arrival_minutes = 'bad'
            WHERE yandex_snapshot_id = ?
            """,
            (bad_shape_snapshot_id,),
        )
        connection.execute(
            """
            UPDATE yandex_forecast_samples
            SET service_date = 'not-a-date'
            WHERE yandex_snapshot_id = ?
            """,
            (bad_service_date_snapshot_id,),
        )
        connection.execute(
            """
            UPDATE yandex_forecast_samples
            SET service_date = '2026-01-01'
            WHERE yandex_snapshot_id = ?
            """,
            (bad_service_date_mismatch_snapshot_id,),
        )
        connection.execute(
            """
            UPDATE yandex_forecast_samples
            SET weekday = 9
            WHERE yandex_snapshot_id = ?
            """,
            (bad_weekday_snapshot_id,),
        )
        connection.execute(
            """
            UPDATE yandex_forecast_samples
            SET sampled_at = '2026-06-05T09:30:00'
            WHERE yandex_snapshot_id = ?
            """,
            (naive_sampled_at_snapshot_id,),
        )
        connection.execute(
            """
            UPDATE yandex_forecast_samples
            SET minute_of_day = ?
            WHERE yandex_snapshot_id = ?
            """,
            (24 * 60, bad_minute_snapshot_id),
        )


def _insert_invalid_age_samples(db_path: Path) -> None:
    base = datetime(2026, 6, 1, 9, 30, tzinfo=NOVOSIBIRSK_TZ)
    with connect(db_path) as connection:
        init_db(connection)
        invalid_age_snapshot_id = insert_yandex_snapshot(connection, MORNING.key, _forecast(10), base)
        insert_yandex_snapshot(connection, MORNING.key, _forecast(20), base + timedelta(days=1))
        connection.execute(
            """
            UPDATE yandex_forecast_samples
            SET newest_age_seconds = -5
            WHERE yandex_snapshot_id = ?
            """,
            (invalid_age_snapshot_id,),
        )


def _forecast(minutes: int) -> YandexLiveForecast:
    return YandexLiveForecast(
        enabled=True,
        available=True,
        source_method=YandexSourceMethod.VEHICLE_PREDICTION,
        status=YandexSourceStatus.OK,
        arrival_minutes=(minutes,),
        vehicle_count=1,
        newest_age_seconds=30,
        confidence=EtaConfidence.HIGH,
    )


def _assert_value_error(action: Callable[[], object], expected: str) -> None:
    try:
        action()
    except ValueError as exc:
        _assert_contains(str(exc), expected)
        return
    raise AssertionError(f"expected ValueError mentioning {expected!r}")


def _assert_contains(value: str, expected: str) -> None:
    if expected not in value:
        raise AssertionError(f"expected {value!r} to contain {expected!r}")


def _assert_equal(actual: object, expected: object) -> None:
    if actual != expected:
        raise AssertionError(f"expected {expected!r}, got {actual!r}")


def main() -> None:
    assert_percentile_validation()
    print("OK | yandex backtest validation smoke passed")


if __name__ == "__main__":
    main()
