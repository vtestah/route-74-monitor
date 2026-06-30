from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from route74.cli.forecast_health import format_forecast_health_summary
from route74.domain.prediction_buckets import prediction_bucket_label
from route74.domain.prediction_sources import SOURCE_TARGET_STOP_LIVE
from route74.domain.profiles import MORNING
from route74.models import NOVOSIBIRSK_TZ
from route74.reporting_smoke_fixtures import FakeYandexSource
from route74.storage import connect, init_db, insert_yandex_snapshot, summarize_forecast_health


def main() -> None:
    _assert_truth_status_accepts_naive_arrival_timestamps()
    _assert_forecast_health_formats_coverage_action()
    print("OK | forecast health smoke passed")


def _assert_truth_status_accepts_naive_arrival_timestamps() -> None:
    sampled_at = datetime(2026, 6, 4, 9, 15, tzinfo=NOVOSIBIRSK_TZ)
    current_time = sampled_at + timedelta(days=1)
    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "forecast-health.sqlite"
        with connect(db_path) as connection:
            init_db(connection)
            snapshot_id = insert_yandex_snapshot(
                connection,
                MORNING.key,
                FakeYandexSource().get_forecast(),
                sampled_at,
            )
            arrival_ids = tuple(
                _insert_arrival(
                    connection,
                    snapshot_id=snapshot_id,
                    arrived_at=(sampled_at.replace(tzinfo=None) + timedelta(minutes=index)).isoformat(),
                )
                for index in range(5)
            )
            for index in range(10):
                prediction_id = _insert_prediction(
                    connection,
                    snapshot_id=snapshot_id,
                    sampled_at=sampled_at - timedelta(minutes=index + 1),
                    predicted_minutes=8,
                )
                _insert_evaluation(
                    connection, prediction_id=prediction_id, arrival_id=arrival_ids[index % len(arrival_ids)]
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
    morning = health.windows[0]
    _assert_equal(morning.arrival_events, 5)
    _assert_equal(morning.prediction_evaluations >= 10, True)
    _assert_equal(morning.latest_arrival_at.tzinfo if morning.latest_arrival_at else None, None)
    _assert_equal(morning.truth_status, "ready")


def _assert_forecast_health_formats_coverage_action() -> None:
    window = SimpleNamespace(
        window_key="weekday_morning_09_12",
        profile_key="morning",
        status="insufficient_bucket_coverage",
        total_samples=0,
        eta_samples=0,
        eta_coverage_percent=0,
        fresh_eta_samples=0,
        fresh_eta_coverage_percent=0,
        traffic_samples=0,
        traffic_coverage_percent=0,
        collector_runs=0,
        collector_eta_runs=0,
        collector_eta_run_percent=0,
        collector_traffic_ok_runs=0,
        collector_traffic_ok_run_percent=0,
        collector_run_statuses=(),
        api_risk_samples=0,
        api_risk_percent=0,
        api_risk_reasons=(),
        coordinate_fallback_samples=0,
        coordinate_fallback_percent=0,
        coordinate_fallback_reasons=(),
        arrival_events=0,
        prediction_events=0,
        prediction_evaluations=0,
        prediction_miss_cases=0,
        prediction_miss_rate_percent=0,
        bot_prediction_events=0,
        bot_prediction_evaluations=0,
        bot_prediction_miss_cases=0,
        bot_prediction_miss_rate_percent=0,
        truth_status="insufficient",
        truth_reason="missing samples",
        latest_arrival_at=None,
        collector_latest_started_at=None,
        ready_buckets=0,
        total_buckets=1,
        readiness_percent=0,
        missing_bucket_labels=("09:00",),
        bucket_gaps=(),
        forecast_without_report_samples=0,
        report_without_forecast_samples=0,
        reason="missing samples",
        latest_sampled_at=None,
    )
    summary = SimpleNamespace(
        ready=False,
        ready_windows=0,
        total_windows=1,
        days=14,
        min_samples=20,
        min_distinct_days=3,
        collector=SimpleNamespace(
            name="yandex-collect",
            status="ok",
            updated_at=None,
            age_seconds=12,
            max_age_seconds=120,
            message="collector ok",
        ),
        canary=SimpleNamespace(
            status="warning",
            latest_checked_at=None,
            risk_reason="canary warning",
            risky_runs=1,
        ),
        windows=(window,),
    )
    text = format_forecast_health_summary(summary, Path("data/forecast-health.sqlite"))
    _assert_contains(text, 'coverage_action="route74 forecast-coverage --window weekday_morning_09_12"')
    _assert_contains(text, "window=weekday_morning_09_12 profile=morning status=insufficient_bucket_coverage")


def _insert_arrival(connection: sqlite3.Connection, *, snapshot_id: int, arrived_at: str) -> int:
    cursor = connection.execute(
        """
        INSERT INTO arrival_events(
            yandex_snapshot_id, profile_key, vehicle_id, thread_id, stop_id,
            arrived_at, source, confidence, lat, lng, raw_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (snapshot_id, MORNING.key, "", "", MORNING.live_stop_id, arrived_at, "manual", "medium", None, None, "{}"),
    )
    return int(cursor.lastrowid)


def _insert_prediction(
    connection: sqlite3.Connection,
    *,
    snapshot_id: int,
    sampled_at: datetime,
    predicted_minutes: int,
) -> int:
    cursor = connection.execute(
        """
        INSERT INTO prediction_events(
            yandex_snapshot_id, profile_key, sampled_at, report_window_key,
            source, source_method, predicted_minutes, predicted_arrival_at,
            confidence, vehicle_id, thread_id, traffic_provider, traffic_status,
            traffic_delay_seconds, runtime_source, raw_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            snapshot_id,
            MORNING.key,
            sampled_at.isoformat(),
            "weekday_morning_09_12",
            SOURCE_TARGET_STOP_LIVE,
            "vehicle_prediction",
            predicted_minutes,
            (sampled_at + timedelta(minutes=predicted_minutes)).isoformat(),
            "medium",
            "",
            "",
            "none",
            "not_collected",
            None,
            "",
            "{}",
        ),
    )
    return int(cursor.lastrowid)


def _insert_evaluation(connection: sqlite3.Connection, *, prediction_id: int, arrival_id: int) -> None:
    predicted_minutes = 8
    connection.execute(
        """
        INSERT INTO prediction_evaluations(
            prediction_event_id, arrival_event_id, profile_key, evaluated_at,
            actual_minutes, predicted_minutes, error_minutes, bucket, source, raw_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            prediction_id,
            arrival_id,
            MORNING.key,
            datetime(2026, 6, 4, 10, 0, tzinfo=NOVOSIBIRSK_TZ).isoformat(),
            predicted_minutes,
            predicted_minutes,
            0,
            prediction_bucket_label(predicted_minutes),
            SOURCE_TARGET_STOP_LIVE,
            "{}",
        ),
    )


def _assert_equal(actual: object, expected: object) -> None:
    if actual != expected:
        raise AssertionError(f"expected {expected!r}, got {actual!r}")


def _assert_contains(text: str, expected: str) -> None:
    if expected not in text:
        raise AssertionError(f"expected {expected!r} in {text!r}")


if __name__ == "__main__":
    main()
