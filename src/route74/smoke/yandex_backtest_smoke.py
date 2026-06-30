from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from route74.domain.eta import EtaConfidence
from route74.domain.profiles import MORNING
from route74.domain.reporting import REPORT_WINDOWS
from route74.models import NOVOSIBIRSK_TZ
from route74.services.yandex_history import YandexHistoryPredictor
from route74.sources.yandex.models import YandexLiveForecast, YandexSourceMethod, YandexSourceStatus
from route74.storage import connect, init_db, insert_yandex_snapshot
from route74.storage.forecast_backtest import ForecastBacktestSummary, summarize_yandex_forecast_backtest
from route74.smoke.yandex_backtest_validation_smoke import assert_percentile_validation


def main() -> None:
    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "backtest.sqlite"
        _insert_backtest_samples(db_path)
        summary = _backtest_summary(db_path, percentiles=(50, 90))
        _assert_equal(summary.target_cases, 4)
        p50, p90 = summary.results
        _assert_equal(p50.evaluated_cases, 1)
        _assert_equal(p50.skipped_cases, 3)
        _assert_equal(p50.miss_cases, 1)
        _assert_equal(p50.miss_minutes, 5)
        _assert_equal(p90.miss_minutes, 15)

    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "empty.sqlite"
        empty = _backtest_summary(db_path, percentiles=(80,))
        _assert_equal(empty.target_cases, 0)
        _assert_equal(empty.results[0].evaluated_cases, 0)

    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "unknown-age.sqlite"
        _insert_unknown_age_samples(db_path)
        unknown_age = _backtest_summary(db_path, percentiles=(80,))
        _assert_equal(unknown_age.target_cases, 0)
        history = YandexHistoryPredictor(db_path=db_path, min_observations=3, min_history_days=3).predict_at(
            MORNING,
            datetime(2026, 6, 4, 9, 30, tzinfo=NOVOSIBIRSK_TZ),
        )
        _assert_equal(history.sample_count, 0)

    assert_percentile_validation()

    print("OK | yandex forecast backtest smoke passed")


def _insert_backtest_samples(db_path: Path) -> None:
    base = datetime(2026, 6, 1, 9, 30, tzinfo=NOVOSIBIRSK_TZ)
    values = (10, 20, 30, 15)
    with connect(db_path) as connection:
        init_db(connection)
        for index, minutes in enumerate(values):
            insert_yandex_snapshot(
                connection,
                MORNING.key,
                _forecast(minutes),
                base + timedelta(days=index),
            )


def _insert_unknown_age_samples(db_path: Path) -> None:
    base = datetime(2026, 6, 1, 9, 30, tzinfo=NOVOSIBIRSK_TZ)
    with connect(db_path) as connection:
        init_db(connection)
        for index in range(4):
            insert_yandex_snapshot(
                connection,
                MORNING.key,
                _forecast(10 + index, newest_age_seconds=None),
                base + timedelta(days=index),
            )


def _backtest_summary(db_path: Path, *, percentiles: tuple[int, ...]) -> ForecastBacktestSummary:
    with connect(db_path) as connection:
        init_db(connection)
        return summarize_yandex_forecast_backtest(
            connection,
            profile_key=MORNING.key,
            report_window_key=REPORT_WINDOWS[0].key,
            history_days=14,
            bucket_minutes=30,
            min_samples=3,
            min_distinct_days=3,
            percentiles=percentiles,
            max_age_seconds=180,
        )


def _forecast(minutes: int, *, newest_age_seconds: int | None = 30) -> YandexLiveForecast:
    return YandexLiveForecast(
        enabled=True,
        available=True,
        source_method=YandexSourceMethod.VEHICLE_PREDICTION,
        status=YandexSourceStatus.OK,
        arrival_minutes=(minutes,),
        vehicle_count=1,
        newest_age_seconds=newest_age_seconds,
        confidence=EtaConfidence.HIGH,
    )


def _assert_equal(actual: object, expected: object) -> None:
    if actual != expected:
        raise AssertionError(f"expected {expected!r}, got {actual!r}")


if __name__ == "__main__":
    main()
