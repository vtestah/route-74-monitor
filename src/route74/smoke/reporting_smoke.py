from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from route74.domain.profiles import EVENING, MORNING
from route74.domain.prediction_buckets import prediction_bucket_label
from route74.domain.prediction_sources import SOURCE_TARGET_STOP_LIVE
from route74.domain.reporting import report_window_for_profile
from route74.domain.runtime_sources import RUNTIME_SOURCE_NONE, RUNTIME_SOURCE_WEB_APP
from route74.models import NOVOSIBIRSK_TZ
from route74.reporting_smoke_fixtures import FakeYandexSource, fake_traffic_source
from route74.services.yandex_telemetry import YandexTelemetryCollector
from route74.storage import (
    backfill_yandex_forecast_samples,
    connect,
    count_report_window_snapshots,
    count_yandex_forecast_samples,
    count_yandex_snapshots,
    init_db,
    insert_yandex_snapshot,
    load_bot_update_offset,
    load_collector_heartbeat,
    save_bot_update_offset,
    summarize_forecast_health,
    summarize_report_windows,
    update_collector_heartbeat,
)
from route74.storage.forecast_sample_windows import backfill_yandex_forecast_sample_windows
from route74.storage.helpers import optional_int_value


def main() -> None:
    _assert_optional_int_value()
    _assert_heartbeat_name_contracts()
    _assert_equal(report_window_for_profile(MORNING.key).key, "weekday_morning_09_12")
    _assert_equal(report_window_for_profile(EVENING.key).key, "weekday_evening_19_22")
    _assert_rejects(lambda: report_window_for_profile("night"), "profile key")

    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "route74.sqlite"
        weekday_morning = datetime(2026, 6, 4, 9, 15, tzinfo=NOVOSIBIRSK_TZ)
        collector = YandexTelemetryCollector(
            db_path=db_path,
            source=FakeYandexSource(),
            traffic_source=fake_traffic_source,
            profiles=(MORNING, EVENING),
            report_windows_only=True,
            clock=lambda: weekday_morning,
        )
        results = collector.collect_once()
        _assert_equal(len(results), 1)
        _assert_equal(results[0].profile_key, "morning")

        with connect(db_path) as connection:
            init_db(connection)
            _assert_equal(count_yandex_snapshots(connection), 1)
            _assert_equal(count_yandex_forecast_samples(connection), 1)
            _assert_equal(count_report_window_snapshots(connection), 1)
            morning_summary = summarize_report_windows(
                connection,
                days=7,
                report_window_key="weekday_morning_09_12",
                profile_key="morning",
                current_time=weekday_morning,
            )
            traffic = connection.execute(
                """
                SELECT
                    traffic_provider, traffic_status, traffic_jams_level,
                    route_duration_seconds, route_duration_in_traffic_seconds,
                    traffic_delay_seconds, traffic_distance_meters, traffic_raw_json
                FROM yandex_forecast_samples
                LIMIT 1
                """
            ).fetchone()
        _assert_equal(morning_summary.total_samples, 1)
        _assert_equal(morning_summary.eta_samples, 1)
        _assert_equal(morning_summary.traffic_samples, 1)
        _assert_equal(
            _traffic_values(traffic),
            ("fake", "ok", 4, 1200, 1500, 300, 8200),
        )
        _assert_equal('"source": "smoke"' in traffic["traffic_raw_json"], True)

    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "route74.sqlite"
        weekend_morning = datetime(2026, 6, 6, 9, 15, tzinfo=NOVOSIBIRSK_TZ)
        collector = YandexTelemetryCollector(
            db_path=db_path,
            source=FakeYandexSource(),
            profiles=(MORNING, EVENING),
            report_windows_only=True,
            clock=lambda: weekend_morning,
        )
        results = collector.collect_once()
        _assert_equal(results, ())
        with connect(db_path) as connection:
            init_db(connection)
            _assert_equal(count_yandex_snapshots(connection), 0)
            _assert_equal(count_yandex_forecast_samples(connection), 0)
            _assert_equal(count_report_window_snapshots(connection), 0)
            heartbeat = load_collector_heartbeat(connection, "yandex-collect")
        if heartbeat is None:
            raise AssertionError("expected skipped heartbeat")
        _assert_equal(heartbeat.last_status, "skipped")
        _assert_equal(heartbeat.last_message, "outside_report_window")

    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "profile-filter-skip.sqlite"
        weekday_morning = datetime(2026, 6, 4, 9, 15, tzinfo=NOVOSIBIRSK_TZ)
        collector = YandexTelemetryCollector(
            db_path=db_path,
            source=FakeYandexSource(),
            profiles=(EVENING,),
            profile_filter="evening",
            report_windows_only=True,
            clock=lambda: weekday_morning,
        )
        results = collector.collect_once()
        _assert_equal(results, ())
        with connect(db_path) as connection:
            init_db(connection)
            heartbeat = load_collector_heartbeat(connection, "yandex-collect")
            health = summarize_forecast_health(
                connection,
                current_date=weekday_morning,
                days=14,
                min_samples=1,
                min_distinct_days=1,
                primary_bucket_minutes=30,
                fallback_bucket_minutes=60,
                max_age_seconds=180,
                step_minutes=30,
            )
        if heartbeat is None:
            raise AssertionError("expected skipped heartbeat")
        _assert_equal(heartbeat.last_status, "skipped")
        _assert_equal(heartbeat.last_message, "profile_filter_inactive")
        _assert_equal(health.collector.status, "skipped")
        _assert_equal(health.collector.healthy, True)
        _assert_equal(health.windows[0].collector_runs, 0)
        _assert_equal(health.windows[0].status, "no_collector_runs")

    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "malformed-heartbeat.sqlite"
        weekday_morning = datetime(2026, 6, 4, 9, 15, tzinfo=NOVOSIBIRSK_TZ)
        with connect(db_path) as connection:
            init_db(connection)
            connection.execute(
                """
                INSERT INTO collector_heartbeat(
                    name, updated_at, pid, profile_filter, last_status, last_message
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                ("yandex-collect", "not-a-date", "bad-pid", "all", "ok", "malformed"),
            )
            connection.execute(
                """
                INSERT INTO collector_heartbeat(
                    name, updated_at, pid, profile_filter, last_status, last_message
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                ("zero-pid", weekday_morning.isoformat(), 0, "all", "ok", "malformed"),
            )
            connection.commit()
            heartbeat = load_collector_heartbeat(connection, "yandex-collect")
            zero_pid = load_collector_heartbeat(connection, "zero-pid")
            health = summarize_forecast_health(
                connection,
                current_date=weekday_morning,
                days=14,
                min_samples=1,
                min_distinct_days=1,
                primary_bucket_minutes=30,
                fallback_bucket_minutes=60,
                max_age_seconds=180,
                step_minutes=30,
            )
        _assert_equal(heartbeat, None)
        _assert_equal(zero_pid, None)
        _assert_equal(health.collector.status, "missing")

    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "legacy-traffic.sqlite"
        weekday_morning = datetime(2026, 6, 4, 9, 15, tzinfo=NOVOSIBIRSK_TZ)
        with connect(db_path) as connection:
            _create_legacy_forecast_samples_without_delay(connection)
            init_db(connection)
            columns = _table_columns(connection, "yandex_forecast_samples")
        _assert_equal("traffic_delay_seconds" in columns, True)
        collector = YandexTelemetryCollector(
            db_path=db_path,
            source=FakeYandexSource(),
            traffic_source=fake_traffic_source,
            profiles=(MORNING,),
            report_windows_only=True,
            clock=lambda: weekday_morning,
        )
        collector.collect_once()
        with connect(db_path) as connection:
            init_db(connection)
            row = connection.execute(
                "SELECT traffic_delay_seconds FROM yandex_forecast_samples LIMIT 1",
            ).fetchone()
        _assert_equal(row["traffic_delay_seconds"], 300)

    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "legacy-bool-age.sqlite"
        weekday_morning = datetime(2026, 6, 4, 9, 15, tzinfo=NOVOSIBIRSK_TZ)
        with connect(db_path) as connection:
            init_db(connection)
            connection.execute(
                """
                INSERT INTO yandex_snapshots(
                    sampled_at, profile_key, source_method, source_status,
                    available, vehicle_count, arrival_minutes_json, fallback_reason, raw_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    weekday_morning.isoformat(),
                    MORNING.key,
                    "vehicle_prediction",
                    "ok",
                    1,
                    1,
                    "[8]",
                    "",
                    '{"newest_age_seconds": true, "confidence": "high"}',
                ),
            )
            _assert_equal(backfill_yandex_forecast_samples(connection), 1)
            row = connection.execute(
                "SELECT newest_age_seconds FROM yandex_forecast_samples LIMIT 1",
            ).fetchone()
        _assert_equal(row["newest_age_seconds"], None)

    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "legacy-invalid-available.sqlite"
        weekday_morning = datetime(2026, 6, 4, 9, 15, tzinfo=NOVOSIBIRSK_TZ)
        with connect(db_path) as connection:
            init_db(connection)
            connection.execute(
                """
                INSERT INTO yandex_snapshots(
                    sampled_at, profile_key, source_method, source_status,
                    available, vehicle_count, arrival_minutes_json, fallback_reason, raw_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    weekday_morning.isoformat(),
                    MORNING.key,
                    "vehicle_prediction",
                    "ok",
                    2,
                    1,
                    "[8]",
                    "",
                    '{"newest_age_seconds": 30, "confidence": "high"}',
                ),
            )
            _assert_equal(backfill_yandex_forecast_samples(connection), 1)
            row = connection.execute(
                """
                SELECT available, arrival_minutes, next_arrival_minutes_json
                FROM yandex_forecast_samples
                LIMIT 1
                """,
            ).fetchone()
        _assert_equal(row["available"], 0)
        _assert_equal(row["arrival_minutes"], None)
        _assert_equal(row["next_arrival_minutes_json"], "[]")

    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "malformed-snapshot-backfill.sqlite"
        weekday_morning = datetime(2026, 6, 4, 9, 15, tzinfo=NOVOSIBIRSK_TZ)
        with connect(db_path) as connection:
            init_db(connection)
            invalid_id = _insert_yandex_snapshot_row(connection, sampled_at="2026-06-04T09:bad")
            valid_id = _insert_yandex_snapshot_row(connection, sampled_at=weekday_morning.isoformat())
            changed = backfill_yandex_forecast_samples(connection)
            rows = {
                int(row["yandex_snapshot_id"])
                for row in connection.execute("SELECT yandex_snapshot_id FROM yandex_forecast_samples").fetchall()
            }
        _assert_equal(changed, 1)
        _assert_equal(invalid_id in rows, False)
        _assert_equal(valid_id in rows, True)

    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "atomic-window.sqlite"
        weekday_morning = datetime(2026, 6, 4, 9, 15, tzinfo=NOVOSIBIRSK_TZ)
        try:
            with connect(db_path) as connection:
                init_db(connection)
                connection.execute(
                    """
                    CREATE TRIGGER fail_report_window_insert
                    BEFORE INSERT ON report_window_snapshots
                    BEGIN
                        SELECT RAISE(FAIL, 'report window insert failed');
                    END
                    """
                )
                connection.commit()
                insert_yandex_snapshot(
                    connection,
                    MORNING.key,
                    FakeYandexSource().get_forecast(),
                    weekday_morning,
                )
        except sqlite3.IntegrityError:
            pass
        else:
            raise AssertionError("expected report-window insert failure")
        with connect(db_path) as connection:
            init_db(connection)
            _assert_equal(count_yandex_snapshots(connection), 0)
            _assert_equal(count_yandex_forecast_samples(connection), 0)
            _assert_equal(count_report_window_snapshots(connection), 0)

    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "integrity-health.sqlite"
        weekday_morning = datetime(2026, 6, 4, 9, 15, tzinfo=NOVOSIBIRSK_TZ)
        with connect(db_path) as connection:
            init_db(connection)
            insert_yandex_snapshot(connection, MORNING.key, FakeYandexSource().get_forecast(), weekday_morning)
            connection.execute("DELETE FROM report_window_snapshots")
            connection.commit()
            health = summarize_forecast_health(
                connection,
                current_date=weekday_morning,
                days=14,
                min_samples=1,
                min_distinct_days=1,
                primary_bucket_minutes=30,
                fallback_bucket_minutes=60,
                max_age_seconds=180,
                step_minutes=30,
            )
        _assert_equal(health.windows[0].status, "integrity_gap")
        _assert_equal(health.windows[0].forecast_without_report_samples, 1)
        _assert_equal(health.windows[0].report_without_forecast_samples, 0)

    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "report-window-backfill.sqlite"
        weekday_morning = datetime(2026, 6, 4, 9, 15, tzinfo=NOVOSIBIRSK_TZ)
        with connect(db_path) as connection:
            init_db(connection)
            insert_yandex_snapshot(
                connection,
                MORNING.key,
                FakeYandexSource().get_forecast(),
                weekday_morning,
                traffic=fake_traffic_source(),
            )
            connection.execute("DELETE FROM report_window_snapshots")
            connection.commit()
            health_before = summarize_forecast_health(
                connection,
                current_date=weekday_morning,
                days=14,
                min_samples=1,
                min_distinct_days=1,
                primary_bucket_minutes=30,
                fallback_bucket_minutes=60,
                max_age_seconds=180,
                step_minutes=30,
            )
            init_db(connection)
            traffic = connection.execute(
                """
                SELECT
                    traffic_provider, traffic_status, traffic_jams_level,
                    route_duration_seconds, route_duration_in_traffic_seconds,
                    traffic_delay_seconds, traffic_distance_meters, traffic_raw_json
                FROM report_window_snapshots
                LIMIT 1
                """
            ).fetchone()
            report_window_count = count_report_window_snapshots(connection)
            health_after = summarize_forecast_health(
                connection,
                current_date=weekday_morning,
                days=14,
                min_samples=1,
                min_distinct_days=1,
                primary_bucket_minutes=30,
                fallback_bucket_minutes=60,
                max_age_seconds=180,
                step_minutes=30,
            )
        _assert_equal(health_before.windows[0].status, "integrity_gap")
        _assert_equal(report_window_count, 1)
        _assert_equal(health_after.windows[0].forecast_without_report_samples, 0)
        _assert_equal(health_after.windows[0].report_without_forecast_samples, 0)
        _assert_equal(
            _traffic_values(traffic),
            ("fake", "ok", 4, 1200, 1500, 300, 8200),
        )
        _assert_equal('"source": "smoke"' in traffic["traffic_raw_json"], True)

    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "forecast-window-backfill.sqlite"
        weekday_morning = datetime(2026, 6, 4, 9, 15, tzinfo=NOVOSIBIRSK_TZ)
        with connect(db_path) as connection:
            init_db(connection)
            invalid_id = _insert_forecast_sample_window_fixture(
                connection,
                sampled_at="2026-06-04T09:10:not-a-time",
            )
            invalid_profile_id = _insert_forecast_sample_window_fixture(
                connection,
                sampled_at=weekday_morning.isoformat(),
                profile_key="unknown",
            )
            valid_id = _insert_forecast_sample_window_fixture(
                connection,
                sampled_at=weekday_morning.isoformat(),
            )
            changed = backfill_yandex_forecast_sample_windows(connection)
            rows = {
                int(row["id"]): str(row["report_window_key"])
                for row in connection.execute(
                    "SELECT id, report_window_key FROM yandex_forecast_samples",
                ).fetchall()
            }
        _assert_equal(changed, 1)
        _assert_equal(rows[invalid_id], "")
        _assert_equal(rows[invalid_profile_id], "")
        _assert_equal(rows[valid_id], "weekday_morning_09_12")

    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "bot-health.sqlite"
        weekday_morning = datetime(2026, 6, 4, 9, 15, tzinfo=NOVOSIBIRSK_TZ)
        with connect(db_path) as connection:
            init_db(connection)
            _insert_evaluated_prediction(
                connection,
                weekday_morning,
                '{"runtime_source": "web_app"}',
                RUNTIME_SOURCE_WEB_APP,
            )
            _insert_evaluated_prediction(connection, weekday_morning, "{}")
            health = summarize_forecast_health(
                connection,
                current_date=weekday_morning,
                days=14,
                min_samples=1,
                min_distinct_days=1,
                primary_bucket_minutes=30,
                fallback_bucket_minutes=60,
                max_age_seconds=180,
                step_minutes=30,
            )
        morning = health.windows[0]
        _assert_equal(morning.prediction_events, 2)
        _assert_equal(morning.prediction_evaluations, 2)
        _assert_equal(morning.prediction_miss_cases, 2)
        _assert_equal(morning.bot_prediction_events, 1)
        _assert_equal(morning.bot_prediction_evaluations, 1)
        _assert_equal(morning.bot_prediction_miss_cases, 1)

    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "legacy-prediction-events.sqlite"
        weekday_morning = datetime(2026, 6, 4, 9, 15, tzinfo=NOVOSIBIRSK_TZ)
        with connect(db_path) as connection:
            _create_legacy_prediction_events_without_runtime_source(connection)
            true_bot_id = _insert_legacy_prediction(
                connection,
                weekday_morning,
                '{"runtime_source": "web_app"}',
            )
            false_positive_id = _insert_legacy_prediction(
                connection,
                weekday_morning + timedelta(minutes=1),
                '{"note": "web_app appears in operator text"}',
            )
            malformed_id = _insert_legacy_prediction(
                connection,
                weekday_morning + timedelta(minutes=2),
                '{"runtime_source": ',
            )
            init_db(connection)
            rows = {
                int(row["id"]): str(row["runtime_source"])
                for row in connection.execute("SELECT id, runtime_source FROM prediction_events ORDER BY id").fetchall()
            }
        _assert_equal(rows[true_bot_id], RUNTIME_SOURCE_WEB_APP)
        _assert_equal(rows[false_positive_id], RUNTIME_SOURCE_NONE)
        _assert_equal(rows[malformed_id], RUNTIME_SOURCE_NONE)

    print("OK | reporting smoke passed")


def _assert_optional_int_value() -> None:
    _assert_equal(optional_int_value(None), None)
    _assert_equal(optional_int_value(True), None)
    _assert_equal(optional_int_value(False), None)
    _assert_equal(optional_int_value(0), 0)
    _assert_equal(optional_int_value(" 12 "), 12)
    _assert_equal(optional_int_value("12.5"), None)


def _assert_heartbeat_name_contracts() -> None:
    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "heartbeat-names.sqlite"
        with connect(db_path) as connection:
            init_db(connection)
            _assert_rejects(
                lambda: update_collector_heartbeat(
                    connection,
                    name=" ",
                    pid=1,
                    profile_filter="all",
                    last_status="ok",
                    last_message="ok",
                ),
                "name is required",
            )
            _assert_rejects(
                lambda: load_collector_heartbeat(connection, True),  # type: ignore[arg-type]
                "name is required",
            )
            _assert_rejects(
                lambda: save_bot_update_offset(connection, name="", update_offset=1),
                "name is required",
            )
            _assert_rejects(
                lambda: load_bot_update_offset(connection, False),  # type: ignore[arg-type]
                "name is required",
            )
            _assert_rejects(
                lambda: update_collector_heartbeat(
                    connection,
                    name="yandex-collect",
                    pid=True,  # type: ignore[arg-type]
                    profile_filter="all",
                    last_status="ok",
                    last_message="ok",
                ),
                "positive integer",
            )
            _assert_rejects(
                lambda: update_collector_heartbeat(
                    connection,
                    name="yandex-collect",
                    pid=0,
                    profile_filter="all",
                    last_status="ok",
                    last_message="ok",
                ),
                "positive integer",
            )
            _assert_rejects(
                lambda: update_collector_heartbeat(
                    connection,
                    name="yandex-collect",
                    pid=123,
                    profile_filter=" ",
                    last_status="ok",
                    last_message="ok",
                ),
                "profile filter is required",
            )
            _assert_rejects(
                lambda: update_collector_heartbeat(
                    connection,
                    name="yandex-collect",
                    pid=123,
                    profile_filter="all",
                    last_status=True,  # type: ignore[arg-type]
                    last_message="ok",
                ),
                "status is required",
            )
            _assert_rejects(
                lambda: update_collector_heartbeat(
                    connection,
                    name="yandex-collect",
                    pid=123,
                    profile_filter="all",
                    last_status="ok",
                    last_message="",
                ),
                "message is required",
            )
            _assert_rejects(
                lambda: update_collector_heartbeat(
                    connection,
                    name="yandex-collect",
                    pid=123,
                    profile_filter="all",
                    last_status="ok",
                    last_message="ok",
                    updated_at="now",  # type: ignore[arg-type]
                ),
                "needs datetime",
            )
            _assert_rejects(
                lambda: update_collector_heartbeat(
                    connection,
                    name="yandex-collect",
                    pid=123,
                    profile_filter="all",
                    last_status="ok",
                    last_message="ok",
                    updated_at=datetime(2026, 6, 4, 9, 15),
                ),
                "timezone-aware datetime",
            )
            update_collector_heartbeat(
                connection,
                name=" yandex-collect ",
                pid=123,
                profile_filter=" all ",
                last_status=" ok ",
                last_message=" ok ",
            )
            save_bot_update_offset(connection, name=" web-runtime ", update_offset=42)
            heartbeat = load_collector_heartbeat(connection, "yandex-collect")
            if heartbeat is None:
                raise AssertionError("expected normalized collector heartbeat")
            _assert_equal(heartbeat.name, "yandex-collect")
            _assert_equal(heartbeat.profile_filter, "all")
            _assert_equal(heartbeat.last_status, "ok")
            _assert_equal(heartbeat.last_message, "ok")
            _assert_equal(load_bot_update_offset(connection, "web-runtime"), 42)


def _insert_forecast_sample_window_fixture(
    connection: sqlite3.Connection,
    *,
    sampled_at: str,
    profile_key: str = MORNING.key,
) -> int:
    snapshot_id = _insert_yandex_snapshot_row(connection, sampled_at=sampled_at)
    cursor = connection.execute(
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
            snapshot_id,
            sampled_at,
            "2026-06-04",
            3,
            9 * 60 + 15,
            profile_key,
            "vehicle_prediction",
            "ok",
            1,
            8,
            "[]",
            1,
            20,
            "high",
            "",
            "",
            "{}",
        ),
    )
    connection.commit()
    return int(cursor.lastrowid)


def _insert_yandex_snapshot_row(connection: sqlite3.Connection, *, sampled_at: str) -> int:
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
            "[8]",
            "",
            "{}",
        ),
    )
    return int(cursor.lastrowid)


def _insert_evaluated_prediction(
    connection: sqlite3.Connection,
    sampled_at: datetime,
    raw_json: str,
    runtime_source: str = RUNTIME_SOURCE_NONE,
) -> None:
    predicted_minutes = 10
    arrival_at = sampled_at + timedelta(minutes=5)
    prediction_id = _insert_prediction(connection, sampled_at, predicted_minutes, raw_json, runtime_source)
    arrival_id = _insert_arrival(connection, arrival_at)
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
            arrival_at.isoformat(),
            5,
            predicted_minutes,
            -5,
            prediction_bucket_label(predicted_minutes),
            SOURCE_TARGET_STOP_LIVE,
            "{}",
        ),
    )
    connection.commit()


def _insert_prediction(
    connection: sqlite3.Connection,
    sampled_at: datetime,
    predicted_minutes: int,
    raw_json: str,
    runtime_source: str,
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
            None,
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
            runtime_source,
            raw_json,
        ),
    )
    return int(cursor.lastrowid)


def _insert_arrival(connection: sqlite3.Connection, arrived_at: datetime) -> int:
    cursor = connection.execute(
        """
        INSERT INTO arrival_events(
            yandex_snapshot_id, profile_key, vehicle_id, thread_id, stop_id,
            arrived_at, source, confidence, lat, lng, raw_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            None,
            MORNING.key,
            "",
            "",
            MORNING.live_stop_id,
            arrived_at.isoformat(),
            "trusted_eta",
            "high",
            None,
            None,
            "{}",
        ),
    )
    return int(cursor.lastrowid)


def _create_legacy_prediction_events_without_runtime_source(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE prediction_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            yandex_snapshot_id INTEGER,
            profile_key TEXT NOT NULL,
            sampled_at TEXT NOT NULL,
            report_window_key TEXT NOT NULL,
            source TEXT NOT NULL,
            source_method TEXT NOT NULL,
            predicted_minutes INTEGER NOT NULL,
            predicted_arrival_at TEXT NOT NULL,
            confidence TEXT NOT NULL,
            vehicle_id TEXT NOT NULL,
            thread_id TEXT NOT NULL,
            traffic_provider TEXT NOT NULL,
            traffic_status TEXT NOT NULL,
            traffic_delay_seconds INTEGER,
            raw_json TEXT NOT NULL
        )
        """
    )


def _insert_legacy_prediction(connection: sqlite3.Connection, sampled_at: datetime, raw_json: str) -> int:
    cursor = connection.execute(
        """
        INSERT INTO prediction_events(
            yandex_snapshot_id, profile_key, sampled_at, report_window_key,
            source, source_method, predicted_minutes, predicted_arrival_at,
            confidence, vehicle_id, thread_id, traffic_provider, traffic_status,
            traffic_delay_seconds, raw_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            None,
            MORNING.key,
            sampled_at.isoformat(),
            "weekday_morning_09_12",
            SOURCE_TARGET_STOP_LIVE,
            "vehicle_prediction",
            10,
            (sampled_at + timedelta(minutes=10)).isoformat(),
            "medium",
            "",
            "",
            "none",
            "not_collected",
            None,
            raw_json,
        ),
    )
    connection.commit()
    return int(cursor.lastrowid)


def _traffic_values(row: object) -> tuple[object, ...]:
    keys = (
        "traffic_provider",
        "traffic_status",
        "traffic_jams_level",
        "route_duration_seconds",
        "route_duration_in_traffic_seconds",
        "traffic_delay_seconds",
        "traffic_distance_meters",
    )
    return tuple(row[key] for key in keys)


def _assert_equal(actual: object, expected: object) -> None:
    if actual != expected:
        raise AssertionError(f"expected {expected!r}, got {actual!r}")


def _assert_rejects(action: Callable[[], object], expected: str) -> None:
    try:
        action()
    except ValueError as error:
        if expected not in str(error):
            raise AssertionError(f"expected {expected!r} in {str(error)!r}") from error
    else:
        raise AssertionError(f"expected validation error containing {expected!r}")


def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row["name"]) for row in connection.execute(f"PRAGMA table_info({table})").fetchall()}


def _create_legacy_forecast_samples_without_delay(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE yandex_forecast_samples (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            yandex_snapshot_id INTEGER NOT NULL,
            sampled_at TEXT NOT NULL,
            service_date TEXT NOT NULL,
            weekday INTEGER NOT NULL,
            minute_of_day INTEGER NOT NULL,
            profile_key TEXT NOT NULL,
            source_method TEXT NOT NULL,
            source_status TEXT NOT NULL,
            available INTEGER NOT NULL,
            arrival_minutes INTEGER,
            next_arrival_minutes_json TEXT NOT NULL,
            vehicle_count INTEGER NOT NULL,
            newest_age_seconds INTEGER,
            confidence TEXT NOT NULL,
            fallback_reason TEXT NOT NULL,
            report_window_key TEXT NOT NULL,
            traffic_provider TEXT NOT NULL DEFAULT 'none',
            traffic_status TEXT NOT NULL DEFAULT 'not_collected',
            route_duration_seconds INTEGER,
            route_duration_in_traffic_seconds INTEGER,
            traffic_distance_meters INTEGER,
            traffic_raw_json TEXT NOT NULL DEFAULT '{}',
            raw_json TEXT NOT NULL
        )
        """
    )
    connection.commit()


if __name__ == "__main__":
    main()
