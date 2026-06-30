from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from route74.domain.eta import EtaConfidence
from route74.domain.profiles import MORNING
from route74.domain.yandex_history import YandexHistoryPrediction, YandexHistoryScope
from route74.models import NOVOSIBIRSK_TZ
from route74.services.yandex_history import YandexHistoryPredictor
from route74.sources.yandex.models import (
    YandexLiveForecast,
    YandexSourceMethod,
    YandexSourceStatus,
    YandexVehicle,
)
from route74.storage import (
    connect,
    count_yandex_forecast_samples,
    init_db,
    insert_yandex_snapshot,
    summarize_forecast_health,
    summarize_yandex_forecast_window_coverage,
    summarize_yandex_forecast_readiness,
)
from route74.domain.reporting import REPORT_WINDOWS
from route74.storage.helpers import arrival_minutes_from_json


def main() -> None:
    current_time = datetime(2026, 6, 4, 9, 30, tzinfo=NOVOSIBIRSK_TZ)
    _run_arrival_minutes_json_smoke()
    _run_prediction_contract_smoke()
    _run_predictor_config_smoke()

    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "primary.sqlite"
        _insert_values(db_path, current_time, 0, tuple(range(1, 21)))
        prediction = YandexHistoryPredictor(db_path=db_path).predict_at(MORNING, current_time)
        _assert_equal(prediction.available, True)
        _assert_equal(prediction.arrival_minutes, 16)
        _assert_equal(prediction.sample_count, 20)
        _assert_equal(prediction.bucket_minutes, 30)
        _assert_equal(prediction.scope, YandexHistoryScope.REPORT_WINDOW)
        _assert_equal(prediction.report_window_key, REPORT_WINDOWS[0].key)

    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "single-day-overfit.sqlite"
        _insert_same_day_values(db_path, current_time, tuple(range(1, 21)))
        prediction = YandexHistoryPredictor(db_path=db_path).predict_at(MORNING, current_time)
        _assert_equal(prediction.available, False)
        _assert_equal(prediction.sample_count, 1)
        _assert_contains(prediction.fallback_reason, "days:1/3")

    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "dense-sampling-overfit.sqlite"
        _insert_dense_sampling_values(db_path, current_time)
        prediction = YandexHistoryPredictor(db_path=db_path).predict_at(MORNING, current_time)
        _assert_equal(prediction.available, False)
        _assert_equal(prediction.sample_count, 3)
        _assert_contains(prediction.fallback_reason, "insufficient_history:3/20")

    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "fallback.sqlite"
        _insert_values(db_path, current_time, 45, tuple(range(10, 30)))
        prediction = YandexHistoryPredictor(db_path=db_path).predict_at(MORNING, current_time)
        _assert_equal(prediction.available, True)
        _assert_equal(prediction.arrival_minutes, 25)
        _assert_equal(prediction.bucket_minutes, 60)

    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "empty.sqlite"
        prediction = YandexHistoryPredictor(db_path=db_path).predict_at(MORNING, current_time)
        _assert_equal(prediction.available, False)
        _assert_equal(prediction.arrival_minutes, None)

    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "future-samples.sqlite"
        _insert_future_value(db_path, current_time)
        prediction = YandexHistoryPredictor(
            db_path=db_path,
            min_observations=1,
            min_history_days=1,
        ).predict_at(MORNING, current_time)
        _assert_equal(prediction.available, False)
        _assert_equal(prediction.sample_count, 0)
        _assert_contains(prediction.fallback_reason, "insufficient_history:0/1")

    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "malformed-sample-time.sqlite"
        valid_sampled_at = _normalized_sampled_at(current_time, 0)
        _insert_values(db_path, current_time, 0, (20,))
        with connect(db_path) as connection:
            init_db(connection)
            _insert_malformed_history_sample(connection, sampled_at="2026-06-04T09:29:not-a-time", minutes=1)
        prediction = YandexHistoryPredictor(
            db_path=db_path,
            min_observations=1,
            min_history_days=1,
        ).predict_at(MORNING, current_time)
        _assert_equal(prediction.available, True)
        _assert_equal(prediction.arrival_minutes, 20)
        _assert_equal(prediction.sample_count, 1)
        with connect(db_path) as connection:
            init_db(connection)
            readiness = summarize_yandex_forecast_readiness(
                connection,
                profile_key=MORNING.key,
                current_time=current_time,
                days=14,
                min_samples=1,
                min_distinct_days=1,
                primary_bucket_minutes=30,
                fallback_bucket_minutes=60,
                max_age_seconds=180,
            )
        _assert_equal(readiness.ready, True)
        _assert_equal(readiness.primary_samples, 1)
        _assert_equal(readiness.latest_sampled_at, valid_sampled_at)

    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "malformed-history-metrics.sqlite"
        _insert_values(db_path, current_time, 0, (20,))
        with connect(db_path) as connection:
            init_db(connection)
            _insert_malformed_history_sample(
                connection,
                sampled_at=(_normalized_sampled_at(current_time, 0) + timedelta(seconds=1)).isoformat(),
                minutes=9,
            )
            connection.execute(
                """
                UPDATE yandex_forecast_samples
                SET arrival_minutes = 'bad',
                    minute_of_day = 'bad'
                WHERE arrival_minutes = 9
                """,
            )
            connection.commit()
        prediction = YandexHistoryPredictor(
            db_path=db_path,
            min_observations=1,
            min_history_days=1,
        ).predict_at(MORNING, current_time)
        _assert_equal(prediction.available, True)
        _assert_equal(prediction.arrival_minutes, 20)
        _assert_equal(prediction.sample_count, 1)
        with connect(db_path) as connection:
            init_db(connection)
            readiness = summarize_yandex_forecast_readiness(
                connection,
                profile_key=MORNING.key,
                current_time=current_time,
                days=14,
                min_samples=1,
                min_distinct_days=1,
                primary_bucket_minutes=30,
                fallback_bucket_minutes=60,
                max_age_seconds=180,
            )
        _assert_equal(readiness.ready, True)
        _assert_equal(readiness.primary_samples, 1)
        _assert_equal(readiness.fresh_eta_samples, 1)

    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "malformed-health-metrics.sqlite"
        _insert_values(db_path, current_time, 0, (20,))
        with connect(db_path) as connection:
            init_db(connection)
            _insert_malformed_history_sample(
                connection,
                sampled_at=(_normalized_sampled_at(current_time, 0) + timedelta(seconds=1)).isoformat(),
                minutes=9,
            )
            _insert_malformed_history_sample(
                connection,
                sampled_at=(_normalized_sampled_at(current_time, 0) + timedelta(seconds=2)).isoformat(),
                minutes=10,
            )
            connection.execute(
                """
                UPDATE yandex_forecast_samples
                SET arrival_minutes = NULL,
                    minute_of_day = 'bad',
                    vehicle_count = 'bad',
                    source_status = 'parse_error',
                    fallback_reason = ''
                WHERE arrival_minutes = 9
                """,
            )
            connection.execute(
                """
                UPDATE yandex_forecast_samples
                SET arrival_minutes = NULL,
                    minute_of_day = 'bad',
                    vehicle_count = 'bad',
                    source_status = 'coordinates_only',
                    fallback_reason = 'coordinates_only',
                    raw_json = '{"route_geometry_status": "cached"}'
                WHERE arrival_minutes = 10
                """,
            )
            connection.commit()
            health = summarize_forecast_health(
                connection,
                current_date=current_time,
                days=14,
                min_samples=1,
                min_distinct_days=1,
                primary_bucket_minutes=30,
                fallback_bucket_minutes=60,
                max_age_seconds=180,
                step_minutes=30,
            )
        _assert_equal(health.windows[0].api_risk_samples, 0)
        _assert_equal(health.windows[0].coordinate_fallback_samples, 0)

    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "one-sample-per-snapshot.sqlite"
        _insert_multi_vehicle_values(db_path, current_time)
        prediction = YandexHistoryPredictor(db_path=db_path).predict_at(MORNING, current_time)
        _assert_equal(prediction.available, True)
        _assert_equal(prediction.arrival_minutes, 5)
        _assert_equal(prediction.sample_count, 20)
        with connect(db_path) as connection:
            init_db(connection)
            _assert_equal(count_yandex_forecast_samples(connection), 20)
            readiness = summarize_yandex_forecast_readiness(
                connection,
                profile_key=MORNING.key,
                current_time=current_time,
                days=14,
                min_samples=20,
                min_distinct_days=3,
                primary_bucket_minutes=30,
                fallback_bucket_minutes=60,
                max_age_seconds=180,
            )
        _assert_equal(readiness.ready, True)
        _assert_equal(readiness.primary_samples, 20)
        with connect(db_path) as connection:
            init_db(connection)
            coverage = summarize_yandex_forecast_window_coverage(
                connection,
                report_window=REPORT_WINDOWS[0],
                current_date=current_time,
                days=14,
                min_samples=20,
                min_distinct_days=3,
                primary_bucket_minutes=30,
                fallback_bucket_minutes=60,
                max_age_seconds=180,
                step_minutes=30,
            )
        _assert_equal(coverage.total_buckets, 6)
        _assert_equal(coverage.ready_buckets, 4)
        with connect(db_path) as connection:
            init_db(connection)
            health = summarize_forecast_health(
                connection,
                current_date=current_time,
                days=14,
                min_samples=20,
                min_distinct_days=3,
                primary_bucket_minutes=30,
                fallback_bucket_minutes=60,
                max_age_seconds=180,
                step_minutes=30,
            )
        _assert_equal(health.ready, False)
        _assert_equal(health.collector.status, "missing")
        _assert_equal(health.windows[0].status, "insufficient_bucket_coverage")
        _assert_equal(health.windows[0].missing_bucket_labels, ("11:00", "11:30"))
        _assert_equal(tuple(gap.label for gap in health.windows[0].bucket_gaps), ("11:00", "11:30"))
        _assert_equal(health.windows[0].bucket_gaps[0].selected_sample_count, 0)
        _assert_equal(health.windows[0].bucket_gaps[0].min_samples, 20)
        _assert_equal(health.windows[0].bucket_gaps[0].selected_distinct_days, 0)
        _assert_equal(health.windows[0].bucket_gaps[0].min_distinct_days, 3)
        _assert_equal(health.windows[1].status, "no_collector_runs")
        weekend_time = datetime(2026, 6, 6, 9, 30, tzinfo=NOVOSIBIRSK_TZ)
        with connect(db_path) as connection:
            init_db(connection)
            weekend_readiness = summarize_yandex_forecast_readiness(
                connection,
                profile_key=MORNING.key,
                current_time=weekend_time,
                days=14,
                min_samples=20,
                min_distinct_days=3,
                primary_bucket_minutes=30,
                fallback_bucket_minutes=60,
                max_age_seconds=180,
                weekdays=(0, 1, 2, 3, 4),
            )
        _assert_equal(weekend_readiness.ready, True)

    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "stale.sqlite"
        _insert_stale_values(db_path, current_time)
        prediction = YandexHistoryPredictor(db_path=db_path).predict_at(MORNING, current_time)
        _assert_equal(prediction.available, False)
        unfiltered = YandexHistoryPredictor(db_path=db_path, max_age_seconds=None).predict_at(
            MORNING,
            current_time,
        )
        _assert_equal(unfiltered.available, True)
        with connect(db_path) as connection:
            init_db(connection)
            readiness = summarize_yandex_forecast_readiness(
                connection,
                profile_key=MORNING.key,
                current_time=current_time,
                days=14,
                min_samples=20,
                min_distinct_days=3,
                primary_bucket_minutes=30,
                fallback_bucket_minutes=60,
                max_age_seconds=180,
            )
        _assert_equal(readiness.ready, False)
        _assert_equal(readiness.fresh_eta_samples, 0)

    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "report-window-scope.sqlite"
        _insert_values(db_path, current_time, -60, tuple(range(1, 21)))
        prediction = YandexHistoryPredictor(db_path=db_path).predict_at(MORNING, current_time)
        _assert_equal(prediction.available, False)
        _assert_contains(prediction.fallback_reason, "insufficient_history:0/20")
        _assert_equal(prediction.scope, YandexHistoryScope.REPORT_WINDOW)
        _assert_equal(prediction.report_window_key, REPORT_WINDOWS[0].key)
        unscoped_prediction = YandexHistoryPredictor(db_path=db_path, report_window_scope=False).predict_at(
            MORNING,
            current_time,
        )
        _assert_equal(unscoped_prediction.available, True)
        _assert_equal(unscoped_prediction.bucket_minutes, 60)
        _assert_equal(unscoped_prediction.scope, YandexHistoryScope.PROFILE_TIME)
        _assert_equal(unscoped_prediction.report_window_key, "")
        with connect(db_path) as connection:
            init_db(connection)
            unscoped = summarize_yandex_forecast_readiness(
                connection,
                profile_key=MORNING.key,
                current_time=current_time,
                days=14,
                min_samples=20,
                min_distinct_days=3,
                primary_bucket_minutes=30,
                fallback_bucket_minutes=60,
                max_age_seconds=180,
                weekdays=(0, 1, 2, 3, 4),
            )
            scoped = summarize_yandex_forecast_readiness(
                connection,
                profile_key=MORNING.key,
                current_time=current_time,
                days=14,
                min_samples=20,
                min_distinct_days=3,
                primary_bucket_minutes=30,
                fallback_bucket_minutes=60,
                max_age_seconds=180,
                weekdays=(0, 1, 2, 3, 4),
                report_window_key=REPORT_WINDOWS[0].key,
            )
        _assert_equal(unscoped.ready, True)
        _assert_equal(unscoped.fallback_samples, 20)
        _assert_equal(scoped.ready, False)
        _assert_equal(scoped.total_samples, 0)

    with TemporaryDirectory() as temp_dir:
        outside_window_time = datetime(2026, 6, 4, 7, 30, tzinfo=NOVOSIBIRSK_TZ)
        db_path = Path(temp_dir) / "outside-report-window-history.sqlite"
        _insert_values(db_path, outside_window_time, 0, tuple(range(1, 21)))
        prediction = YandexHistoryPredictor(db_path=db_path).predict_at(MORNING, outside_window_time)
        _assert_equal(prediction.available, True)
        _assert_equal(prediction.arrival_minutes, 16)
        _assert_equal(prediction.sample_count, 20)
        _assert_equal(prediction.bucket_minutes, 30)
        _assert_equal(prediction.scope, YandexHistoryScope.PROFILE_TIME)
        _assert_equal(prediction.report_window_key, "")

    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "adaptive-percentile.sqlite"
        _insert_adaptive_percentile_values(db_path, current_time)
        baseline = YandexHistoryPredictor(
            db_path=db_path,
            window_days=30,
            min_observations=4,
            min_history_days=3,
        ).predict_at(MORNING, current_time)
        adaptive = YandexHistoryPredictor(
            db_path=db_path,
            window_days=30,
            min_observations=4,
            min_history_days=3,
            backtest_percentiles=(50, 80),
        ).predict_at(MORNING, current_time)
        _assert_equal(baseline.available, True)
        _assert_equal(baseline.percentile, 80)
        _assert_equal(baseline.arrival_minutes, 30)
        _assert_equal(adaptive.available, True)
        _assert_equal(adaptive.percentile, 50)
        _assert_equal(adaptive.arrival_minutes, 10)

    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "report-window-backfill.sqlite"
        _insert_values(db_path, current_time, 0, (7,))
        with connect(db_path) as connection:
            connection.execute("UPDATE yandex_forecast_samples SET report_window_key = ''")
            connection.commit()
            init_db(connection)
            row = connection.execute(
                "SELECT report_window_key FROM yandex_forecast_samples LIMIT 1",
            ).fetchone()
        _assert_equal(row["report_window_key"], REPORT_WINDOWS[0].key)

    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "untrusted-route-eta.sqlite"
        _insert_browser_route_values(db_path, current_time)
        prediction = YandexHistoryPredictor(db_path=db_path).predict_at(MORNING, current_time)
        _assert_equal(prediction.available, False)
        _assert_equal(prediction.sample_count, 0)
        with connect(db_path) as connection:
            init_db(connection)
            readiness = summarize_yandex_forecast_readiness(
                connection,
                profile_key=MORNING.key,
                current_time=current_time,
                days=14,
                min_samples=20,
                min_distinct_days=3,
                primary_bucket_minutes=30,
                fallback_bucket_minutes=60,
                max_age_seconds=180,
            )
        _assert_equal(readiness.total_samples, 20)
        _assert_equal(readiness.eta_samples, 0)
        _assert_equal(readiness.fresh_eta_samples, 0)

    print("OK | yandex history smoke passed")


def _insert_values(db_path: Path, current_time: datetime, offset_minutes: int, values: tuple[int, ...]) -> None:
    with connect(db_path) as connection:
        init_db(connection)
        for index, minutes in enumerate(values):
            sampled_at = _normalized_sampled_at(current_time, index) + timedelta(minutes=offset_minutes)
            insert_yandex_snapshot(connection, MORNING.key, _forecast(index, minutes), sampled_at)


def _insert_adaptive_percentile_values(db_path: Path, current_time: datetime) -> None:
    values = (30, 30, 30, 10, 10, 10, 10, 10, 10, 10)
    with connect(db_path) as connection:
        init_db(connection)
        for index, minutes in enumerate(values):
            sampled_at = _nth_weekday_before(current_time, len(values) - index)
            insert_yandex_snapshot(connection, MORNING.key, _forecast(index, minutes), sampled_at)


def _insert_future_value(db_path: Path, current_time: datetime) -> None:
    with connect(db_path) as connection:
        init_db(connection)
        insert_yandex_snapshot(connection, MORNING.key, _forecast(1, 4), current_time + timedelta(minutes=5))


def _insert_malformed_history_sample(connection: sqlite3.Connection, *, sampled_at: str, minutes: int) -> None:
    cursor = connection.execute(
        """
        INSERT INTO yandex_snapshots(
            sampled_at, profile_key, source_method, source_status,
            available, vehicle_count, arrival_minutes_json, fallback_reason, raw_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            sampled_at,
            MORNING.key,
            "vehicle_prediction",
            "ok",
            1,
            1,
            f"[{minutes}]",
            "",
            '{"newest_age_seconds": 10, "confidence": "high"}',
        ),
    )
    connection.execute(
        """
        INSERT INTO yandex_forecast_samples(
            yandex_snapshot_id, sampled_at, service_date, weekday, minute_of_day,
            profile_key, source_method, source_status, available, arrival_minutes,
            next_arrival_minutes_json, vehicle_count, newest_age_seconds, confidence,
            fallback_reason, report_window_key, raw_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            int(cursor.lastrowid),
            sampled_at,
            "2026-06-04",
            3,
            9 * 60 + 29,
            MORNING.key,
            "vehicle_prediction",
            "ok",
            1,
            minutes,
            "[]",
            1,
            10,
            "high",
            "",
            REPORT_WINDOWS[0].key,
            "{}",
        ),
    )
    connection.commit()


def _run_arrival_minutes_json_smoke() -> None:
    _assert_equal(arrival_minutes_from_json(None), ())
    _assert_equal(arrival_minutes_from_json(12), ())
    _assert_equal(arrival_minutes_from_json("[3, true, -1, 0, 12, 181]"), (3, 0, 12))
    _assert_equal(arrival_minutes_from_json('["5", 8.5, false, null]'), ())
    _assert_equal(arrival_minutes_from_json("{bad json"), ())


def _run_predictor_config_smoke() -> None:
    cases = (
        ({"window_days": 0}, "window_days must be a positive integer"),
        ({"min_observations": 0}, "min_observations must be a positive integer"),
        ({"min_history_days": 0}, "min_history_days must be a positive integer"),
        (
            {"min_observations": 2, "min_history_days": 3},
            "min_history_days must not exceed min_observations",
        ),
        ({"primary_bucket_minutes": 0}, "primary_bucket_minutes must be a positive integer"),
        (
            {"primary_bucket_minutes": 60, "fallback_bucket_minutes": 30},
            "fallback_bucket_minutes must be greater than or equal to primary_bucket_minutes",
        ),
        ({"percentile": 101}, "percentile must be an integer from 1 to 100"),
        ({"backtest_percentiles": (80, 80)}, "expected unique percentiles"),
        ({"backtest_percentiles": (0, 80)}, "expected percentiles from 1 to 100"),
        ({"max_age_seconds": -1}, "max_age_seconds must be a non-negative integer or None"),
        ({"same_day_kind": "yes"}, "same_day_kind must be a boolean"),
        ({"report_window_scope": 1}, "report_window_scope must be a boolean"),
    )
    for kwargs, expected in cases:
        try:
            YandexHistoryPredictor(**kwargs)
        except ValueError as error:
            _assert_contains(str(error), expected)
        else:
            raise AssertionError(f"expected ValueError containing {expected!r}")


def _run_prediction_contract_smoke() -> None:
    available = YandexHistoryPrediction(**_prediction_kwargs())
    _assert_equal(available.available, True)
    report_window_available = YandexHistoryPrediction(
        **_prediction_kwargs(
            scope=YandexHistoryScope.REPORT_WINDOW,
            report_window_key=REPORT_WINDOWS[0].key,
        )
    )
    _assert_equal(report_window_available.scope, YandexHistoryScope.REPORT_WINDOW)
    _assert_equal(report_window_available.report_window_key, REPORT_WINDOWS[0].key)
    unavailable = YandexHistoryPrediction.unavailable()
    _assert_equal(unavailable.available, False)

    cases = (
        (_prediction_kwargs(arrival_minutes=None), "available history prediction needs arrival_minutes"),
        (_prediction_kwargs(arrival_minutes=-1), "arrival_minutes needs non-negative integer"),
        (_prediction_kwargs(sample_count=0), "available history prediction needs positive sample_count"),
        (_prediction_kwargs(bucket_minutes=0), "available history prediction needs positive bucket_minutes"),
        (_prediction_kwargs(percentile=101), "percentile must be an integer from 1 to 100"),
        (
            _prediction_kwargs(available=False, fallback_reason="history_unavailable"),
            "unavailable history prediction must not have arrival_minutes",
        ),
        (
            _prediction_kwargs(available=False, arrival_minutes=None, fallback_reason=""),
            "unavailable history prediction needs fallback_reason",
        ),
        (_prediction_kwargs(fallback_reason="bad\nreason"), "fallback_reason must be compact text"),
        (_prediction_kwargs(fallback_reason="x" * 121), "fallback_reason must be compact text"),
        (_prediction_kwargs(scope="report_window"), "scope needs YandexHistoryScope"),
        (
            _prediction_kwargs(scope=YandexHistoryScope.REPORT_WINDOW),
            "report-window history prediction needs report_window_key",
        ),
        (
            _prediction_kwargs(report_window_key="weekday_morning_09_12"),
            "unscoped history prediction must not have report_window_key",
        ),
        (
            _prediction_kwargs(
                scope=YandexHistoryScope.REPORT_WINDOW,
                report_window_key="bad key",
            ),
            "report_window_key needs plain key text",
        ),
    )
    for kwargs, expected in cases:
        try:
            YandexHistoryPrediction(**kwargs)
        except ValueError as error:
            _assert_contains(str(error), expected)
        else:
            raise AssertionError(f"expected ValueError containing {expected!r}")


def _prediction_kwargs(**overrides: object) -> dict[str, object]:
    kwargs: dict[str, object] = {
        "available": True,
        "arrival_minutes": 12,
        "sample_count": 20,
        "bucket_minutes": 30,
        "window_days": 14,
        "percentile": 80,
        "fallback_reason": "",
    }
    kwargs.update(overrides)
    return kwargs


def _insert_same_day_values(db_path: Path, current_time: datetime, values: tuple[int, ...]) -> None:
    with connect(db_path) as connection:
        init_db(connection)
        for index, minutes in enumerate(values):
            sampled_at = current_time - timedelta(microseconds=index + 1)
            insert_yandex_snapshot(connection, MORNING.key, _forecast(index, minutes), sampled_at)


def _insert_dense_sampling_values(db_path: Path, current_time: datetime) -> None:
    with connect(db_path) as connection:
        init_db(connection)
        for day_index in range(3):
            sampled_at = _weekday_sampled_at(current_time, day_index)
            for tick in range(20):
                index = day_index * 20 + tick
                insert_yandex_snapshot(
                    connection,
                    MORNING.key,
                    _forecast(index, 5 + tick),
                    sampled_at + timedelta(seconds=tick * 10),
                )


def _insert_multi_vehicle_values(db_path: Path, current_time: datetime) -> None:
    with connect(db_path) as connection:
        init_db(connection)
        for index in range(20):
            insert_yandex_snapshot(
                connection,
                MORNING.key,
                _multi_vehicle_forecast(index),
                _normalized_sampled_at(current_time, index),
            )


def _insert_stale_values(db_path: Path, current_time: datetime) -> None:
    with connect(db_path) as connection:
        init_db(connection)
        for index in range(20):
            insert_yandex_snapshot(
                connection,
                MORNING.key,
                _stale_forecast(index),
                _normalized_sampled_at(current_time, index),
            )


def _insert_browser_route_values(db_path: Path, current_time: datetime) -> None:
    with connect(db_path) as connection:
        init_db(connection)
        for index in range(20):
            insert_yandex_snapshot(
                connection,
                MORNING.key,
                _browser_route_forecast(index),
                _normalized_sampled_at(current_time, index),
            )


def _weekday_sampled_at(current_time: datetime, index: int) -> datetime:
    target_weekdays_back = index % 5 + 1
    sampled_at = current_time
    found = 0
    while found < target_weekdays_back:
        sampled_at -= timedelta(days=1)
        if sampled_at.weekday() < 5:
            found += 1
    return sampled_at


def _nth_weekday_before(current_time: datetime, weekdays_back: int) -> datetime:
    sampled_at = current_time
    found = 0
    while found < weekdays_back:
        sampled_at -= timedelta(days=1)
        if sampled_at.weekday() < 5:
            found += 1
    return sampled_at


def _normalized_sampled_at(current_time: datetime, index: int) -> datetime:
    return _weekday_sampled_at(current_time, index) + timedelta(minutes=(index // 5) * 5)


def _forecast(index: int, minutes: int) -> YandexLiveForecast:
    return YandexLiveForecast(
        enabled=True,
        available=True,
        source_method=YandexSourceMethod.VEHICLE_PREDICTION,
        status=YandexSourceStatus.OK,
        arrival_minutes=(minutes,),
        vehicles=(
            YandexVehicle(
                vehicle_id=f"vehicle-{index}",
                thread_id="2161326764",
                lat=54.84,
                lng=83.11,
                arrival_minutes=minutes,
                age_seconds=10,
            ),
        ),
        vehicle_count=1,
        confidence=EtaConfidence.HIGH,
    )


def _multi_vehicle_forecast(index: int) -> YandexLiveForecast:
    return YandexLiveForecast(
        enabled=True,
        available=True,
        source_method=YandexSourceMethod.VEHICLE_PREDICTION,
        status=YandexSourceStatus.OK,
        arrival_minutes=(5, 55),
        vehicles=(
            YandexVehicle(
                vehicle_id=f"near-{index}",
                thread_id="2161326764",
                lat=54.84,
                lng=83.11,
                arrival_minutes=5,
                age_seconds=10,
            ),
            YandexVehicle(
                vehicle_id=f"far-{index}",
                thread_id="2161326764",
                lat=54.82,
                lng=83.10,
                arrival_minutes=55,
                age_seconds=10,
            ),
        ),
        vehicle_count=2,
        newest_age_seconds=10,
        confidence=EtaConfidence.HIGH,
    )


def _stale_forecast(index: int) -> YandexLiveForecast:
    return YandexLiveForecast(
        enabled=True,
        available=True,
        source_method=YandexSourceMethod.VEHICLE_PREDICTION,
        status=YandexSourceStatus.OK,
        arrival_minutes=(8,),
        vehicles=(
            YandexVehicle(
                vehicle_id=f"stale-{index}",
                thread_id="2161326764",
                lat=54.84,
                lng=83.11,
                arrival_minutes=8,
                age_seconds=600,
            ),
        ),
        vehicle_count=1,
        newest_age_seconds=600,
        confidence=EtaConfidence.LOW,
    )


def _browser_route_forecast(index: int) -> YandexLiveForecast:
    return YandexLiveForecast(
        enabled=True,
        available=True,
        source_method=YandexSourceMethod.BROWSER,
        status=YandexSourceStatus.OK,
        arrival_minutes=(0,),
        vehicles=(
            YandexVehicle(
                vehicle_id=f"browser-route-{index}",
                thread_id="2161326768",
                lat=54.84,
                lng=83.11,
                arrival_minutes=0,
                age_seconds=10,
            ),
        ),
        vehicle_count=1,
        newest_age_seconds=10,
        confidence=EtaConfidence.HIGH,
        fallback_reason="legacy_route_vehicle_eta",
    )


def _assert_equal(actual: object, expected: object) -> None:
    if actual != expected:
        raise AssertionError(f"expected {expected!r}, got {actual!r}")


def _assert_contains(text: str, expected: str) -> None:
    if expected not in text:
        raise AssertionError(f"expected {expected!r} in {text!r}")


if __name__ == "__main__":
    main()
