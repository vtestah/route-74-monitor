from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from tempfile import TemporaryDirectory
from pathlib import Path

from route74.cli.yandex_collect import _collector_lock
from route74.domain.eta import EtaConfidence
from route74.domain.profiles import EVENING, MORNING
from route74.domain.reporting import REPORT_WINDOWS
from route74.models import NOVOSIBIRSK_TZ
from route74.services.yandex_telemetry import YandexTelemetryCollector
from route74.sources.yandex.cache import CachedYandexForecastSource
from route74.sources.yandex.models import (
    YandexLiveForecast,
    YandexSourceMethod,
    YandexSourceStatus,
    YandexVehicle,
)
from route74.sources.yandex.line import (
    YandexLinePoint,
    YandexLineStop,
    YandexLineThread,
    YandexLineTopology,
)
from route74.cli.yandex_canary import (
    format_yandex_canary_runs,
    strict_yandex_canary_message,
    yandex_canary_has_warnings,
)
from route74.storage import (
    connect,
    count_arrival_events,
    count_prediction_evaluations,
    count_prediction_events,
    count_yandex_forecast_samples,
    count_yandex_observations,
    count_yandex_snapshots,
    count_report_window_snapshots,
    init_db,
    insert_yandex_canary_run,
    insert_yandex_snapshot,
    latest_yandex_snapshot_sampled_at,
    load_collector_heartbeat,
    load_yandex_observations,
    RouteTrafficSnapshot,
    summarize_forecast_health,
    summarize_collector_runs,
    summarize_collector_runs_for_report_window,
    summarize_yandex_canary_health,
    summarize_yandex_telemetry,
    update_collector_heartbeat,
    upsert_route_geometry,
)
from route74.storage.forecast_health import (
    ForecastCollectorHealth,
    ForecastHealthSummary,
    ForecastWindowHealth,
)
from route74.storage.yandex_canary import YandexCanaryHealth, YandexCanaryRun


def main() -> None:
    current_time = datetime(2026, 6, 4, 7, 15, tzinfo=NOVOSIBIRSK_TZ)
    _run_collector_lock_smoke()
    _assert_yandex_canary_model_guardrails(current_time)
    _assert_yandex_canary_cli_diagnostics_are_safe(current_time)

    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "route74.sqlite"
        collector = YandexTelemetryCollector(
            db_path=db_path,
            source=_FakeYandexSource(),
            profiles=(MORNING,),
            clock=lambda: current_time,
        )
        results = collector.collect_once()
        _assert_equal(len(results), 1)
        _assert_equal(results[0].profile_key, "morning")
        _assert_equal(results[0].source_method, "vehicle_prediction")
        _assert_equal(results[0].source_status, "ok")
        _assert_equal(results[0].vehicle_count, 1)
        _assert_equal(results[0].arrival_minutes, (6,))
        _assert_equal(results[0].traffic_provider, "none")
        _assert_equal(results[0].traffic_status, "not_collected")
        _assert_equal(results[0].traffic_reason, "")
        _assert_equal(results[0].route_geometry_status, "not_supported")
        _assert_equal(results[0].prediction_events_created, 2)
        _assert_equal(results[0].arrival_events_created, 0)
        _assert_equal(results[0].evaluations_created, 0)

        with connect(db_path) as connection:
            init_db(connection)
            _assert_equal(count_yandex_snapshots(connection), 1)
            _assert_equal(count_prediction_events(connection), 2)
            _assert_equal(count_arrival_events(connection), 0)
            _assert_equal(count_prediction_evaluations(connection), 0)
            canary = insert_yandex_canary_run(
                connection,
                profile=MORNING,
                forecast=_FakeYandexSource().get_forecast(),
                checked_at=current_time,
            )
            canary_health = summarize_yandex_canary_health(connection, current_time=current_time)
            _assert_equal(count_yandex_forecast_samples(connection), 1)
            _assert_equal(count_yandex_observations(connection), 1)
            observations = load_yandex_observations(connection)
            heartbeat = load_collector_heartbeat(connection, "yandex-collect")
            run = connection.execute("SELECT message, raw_json FROM collector_runs").fetchone()
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
            summary = summarize_yandex_telemetry(connection, hours=24, profile_key="morning", current_time=current_time)
            _assert_value_error(
                lambda: summarize_yandex_telemetry(connection, hours=0, current_time=current_time),
                "hours must be a positive integer",
            )
            _assert_value_error(
                lambda: summarize_yandex_telemetry(  # type: ignore[arg-type]
                    connection,
                    hours=True,
                    current_time=current_time,
                ),
                "hours must be a positive integer",
            )
            _assert_value_error(
                lambda: summarize_yandex_telemetry(  # type: ignore[arg-type]
                    connection,
                    hours=24,
                    current_time="2026-06-04T07:15:00",
                ),
                "current_time must be a datetime",
            )

        _assert_equal(len(observations), 1)
        _assert_equal(canary.status, "ok")
        _assert_equal(canary_health.status, "ok")
        _assert_equal(observations[0].profile_key, "morning")
        _assert_equal(observations[0].vehicle_id, "vehicle-1")
        _assert_equal(observations[0].thread_id, "2161326764")
        _assert_equal(observations[0].arrival_minutes, 6)
        _assert_equal(observations[0].lat, 54.84)
        _assert_equal(observations[0].lng, 83.11)
        if heartbeat is None:
            raise AssertionError("expected collector heartbeat")
        if run is None:
            raise AssertionError("expected collector run")
        _assert_equal(heartbeat.last_status, "ok")
        _assert_contains(heartbeat.last_message, "prediction_lab=p2/a0/e0")
        _assert_contains(run["message"], "prediction_lab=p2/a0/e0")
        _assert_contains(run["raw_json"], '"prediction_events_created": 2')
        _assert_contains(run["raw_json"], '"arrival_events_created": 0')
        _assert_contains(run["raw_json"], '"evaluations_created": 0')
        _assert_equal(health.collector.status, "ok")
        _assert_equal(health.collector.age_seconds, 0)
        _assert_equal(summary.total_snapshots, 1)
        _assert_equal(summary.eta_snapshots, 1)
        _assert_equal(summary.vehicle_snapshots, 1)
        _assert_equal(summary.eta_coverage_percent, 100)
        _assert_equal(summary.collector_runs.total_runs, 1)
        _assert_equal(summary.collector_runs.result_runs, 1)
        _assert_equal(summary.collector_runs.eta_runs, 1)
        _assert_equal(summary.collector_runs.traffic_ok_runs, 0)
        _assert_equal(summary.collector_runs.statuses[0].key, "ok")

    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "malformed-snapshot-time.sqlite"
        with connect(db_path) as connection:
            init_db(connection)
            insert_yandex_snapshot(connection, MORNING.key, _FakeYandexSource().get_forecast(), current_time)
            malformed_id = _insert_yandex_snapshot_row(
                connection,
                sampled_at="2026-06-04T07:16:not-a-time",
            )
            _insert_yandex_observation_row(connection, snapshot_id=malformed_id)
            snapshot_count = count_yandex_snapshots(connection)
            summary = summarize_yandex_telemetry(
                connection,
                hours=24,
                profile_key="morning",
                current_time=current_time,
            )
            observations = load_yandex_observations(connection)
            latest_sampled_at = latest_yandex_snapshot_sampled_at(connection)
        _assert_equal(snapshot_count, 2)
        _assert_equal(summary.total_snapshots, 1)
        _assert_equal(summary.total_observations, 1)
        _assert_equal(summary.eta_observations, 1)
        _assert_equal(summary.vehicle_snapshots, 1)
        _assert_equal(summary.latest_sampled_at, current_time)
        _assert_equal(len(observations), 1)
        _assert_equal(latest_sampled_at, current_time)

    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "malformed-observation-row.sqlite"
        with connect(db_path) as connection:
            init_db(connection)
            snapshot_id = insert_yandex_snapshot(
                connection,
                MORNING.key,
                _FakeYandexSource().get_forecast(),
                current_time,
            )
            _insert_yandex_observation_row(
                connection,
                snapshot_id=snapshot_id,
                vehicle_id="malformed-observation",
                arrival_minutes="bad",
            )
            observation_count = count_yandex_observations(connection)
            observations = load_yandex_observations(connection)
        _assert_equal(observation_count, 2)
        _assert_equal(len(observations), 1)
        _assert_equal(observations[0].vehicle_id, "vehicle-1")

    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "canary-risk.sqlite"
        with connect(db_path) as connection:
            init_db(connection)
            risky = insert_yandex_canary_run(
                connection,
                profile=MORNING,
                forecast=YandexLiveForecast.unavailable(
                    status=YandexSourceStatus.PARSE_ERROR,
                    source_method=YandexSourceMethod.VEHICLE_PREDICTION,
                    reason="parser failed",
                ),
                checked_at=current_time,
            )
            canary_health = summarize_yandex_canary_health(connection, current_time=current_time)
            _assert_value_error(
                lambda: insert_yandex_canary_run(
                    connection,
                    profile=MORNING,
                    forecast=_FakeYandexSource().get_forecast(),
                    checked_at=datetime(2026, 6, 4, 7, 15),
                ),
                "timezone-aware",
            )
            _assert_value_error(
                lambda: insert_yandex_canary_run(
                    connection,
                    profile=MORNING,
                    forecast=_FakeYandexSource().get_forecast(),
                    checked_at=datetime(2026, 6, 4, 7, 15, tzinfo=timezone.utc),
                ),
                "Asia/Novosibirsk",
            )
        _assert_equal(risky.status, "warning")
        _assert_equal(canary_health.status, "warning")
        _assert_equal(_forecast_ready(canary_health=canary_health, current_time=current_time), False)
        _assert_equal(
            _forecast_ready(
                canary_health=YandexCanaryHealth("ok", current_time, "latest canary runs are ok", 0),
                current_time=current_time,
            ),
            True,
        )

    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "canary-profile-coverage.sqlite"
        with connect(db_path) as connection:
            init_db(connection)
            insert_yandex_canary_run(
                connection,
                profile=MORNING,
                forecast=_FakeYandexSource().get_forecast(),
                checked_at=current_time,
            )
            missing_evening = summarize_yandex_canary_health(
                connection,
                current_time=current_time,
                required_profile_keys=("morning", "evening"),
            )
            connection.execute(
                """
                INSERT INTO yandex_canary_runs(
                    checked_at, status, source_method, profile_key, schema_hash,
                    changed_keys_json, risk_reason, raw_summary_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    current_time.isoformat(),
                    "ok",
                    "vehicle_prediction",
                    EVENING.key,
                    "corrupt-ok-row",
                    json.dumps(
                        {"changed": {"status": {"previous": "blocked", "current": "ok"}}},
                        ensure_ascii=False,
                    ),
                    "schema_changed",
                    "{}",
                ),
            )
            corrupt_evening = summarize_yandex_canary_health(
                connection,
                current_time=current_time,
                required_profile_keys=("morning", "evening"),
            )
            insert_yandex_canary_run(
                connection,
                profile=EVENING,
                forecast=_FakeYandexSource().get_forecast(),
                checked_at=current_time,
            )
            covered = summarize_yandex_canary_health(
                connection,
                current_time=current_time,
                required_profile_keys=("morning", "evening"),
            )
        _assert_equal(missing_evening.status, "missing")
        _assert_contains(missing_evening.risk_reason, "evening")
        _assert_equal(corrupt_evening.status, "missing")
        _assert_contains(corrupt_evening.risk_reason, "evening")
        _assert_equal(covered.status, "ok")

    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "canary-invalid-checked-at.sqlite"
        with connect(db_path) as connection:
            init_db(connection)
            insert_yandex_canary_run(
                connection,
                profile=MORNING,
                forecast=_FakeYandexSource().get_forecast(),
                checked_at=current_time,
            )
            connection.execute(
                """
                INSERT INTO yandex_canary_runs(
                    checked_at, status, source_method, profile_key, schema_hash,
                    changed_keys_json, risk_reason, raw_summary_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "2026-06-04T07:16:not-a-time",
                    "ok",
                    "vehicle_prediction",
                    EVENING.key,
                    "invalid-time",
                    "{}",
                    "ok",
                    "{}",
                ),
            )
            connection.execute(
                """
                INSERT INTO yandex_canary_runs(
                    checked_at, status, source_method, profile_key, schema_hash,
                    changed_keys_json, risk_reason, raw_summary_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    (current_time + timedelta(days=1)).isoformat(),
                    "warning",
                    "vehicle_prediction",
                    EVENING.key,
                    "future-time",
                    "{}",
                    "future clock skew",
                    "{}",
                ),
            )
            invalid_checked_at = summarize_yandex_canary_health(
                connection,
                current_time=current_time,
                required_profile_keys=("morning", "evening"),
            )
            _assert_value_error(
                lambda: summarize_yandex_canary_health(connection, current_time=current_time, hours=0),
                "hours must be a positive integer",
            )
            _assert_value_error(
                lambda: summarize_yandex_canary_health(  # type: ignore[arg-type]
                    connection,
                    current_time=current_time,
                    hours=True,
                ),
                "hours must be a positive integer",
            )
            _assert_value_error(
                lambda: summarize_yandex_canary_health(
                    connection,
                    current_time=datetime(2026, 6, 4, 7, 15),
                ),
                "timezone-aware",
            )
            _assert_value_error(
                lambda: summarize_yandex_canary_health(
                    connection,
                    current_time=datetime(2026, 6, 4, 7, 15, tzinfo=timezone.utc),
                ),
                "Asia/Novosibirsk",
            )
            _assert_value_error(
                lambda: summarize_yandex_canary_health(
                    connection,
                    current_time=current_time,
                    required_profile_keys=["morning"],  # type: ignore[arg-type]
                ),
                "required canary profile keys need tuple",
            )
            _assert_value_error(
                lambda: summarize_yandex_canary_health(
                    connection,
                    current_time=current_time,
                    required_profile_keys=("morning", "morning"),
                ),
                "duplicate required canary profile key",
            )
            _assert_value_error(
                lambda: summarize_yandex_canary_health(
                    connection,
                    current_time=current_time,
                    required_profile_keys=("morning", " "),
                ),
                "required canary profile key",
            )
            _assert_value_error(
                lambda: summarize_yandex_canary_health(
                    connection,
                    current_time=current_time,
                    required_profile_keys=("morning", "unknown"),
                ),
                "must be one of",
            )
        _assert_equal(invalid_checked_at.status, "missing")
        _assert_equal(invalid_checked_at.latest_checked_at, current_time)
        _assert_contains(invalid_checked_at.risk_reason, "evening")

    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "canary-schema-diff.sqlite"
        with connect(db_path) as connection:
            init_db(connection)
            baseline = insert_yandex_canary_run(
                connection,
                profile=MORNING,
                forecast=_canary_forecast_with_fields(lat=54.84, lng=83.11),
                checked_at=current_time,
            )
            changed = insert_yandex_canary_run(
                connection,
                profile=MORNING,
                forecast=_canary_forecast_with_fields(lat=None, lng=None),
                checked_at=current_time,
            )
            row = connection.execute(
                "SELECT changed_keys_json FROM yandex_canary_runs WHERE id = ?",
                (changed.id,),
            ).fetchone()
            canary_health = summarize_yandex_canary_health(connection, current_time=current_time)
        diff = json.loads(row["changed_keys_json"])
        _assert_equal(baseline.status, "ok")
        _assert_equal(changed.status, "warning")
        _assert_equal(changed.risk_reason, "schema_changed")
        _assert_equal(changed.changed_keys, ("vehicle_fields",))
        _assert_contains(canary_health.risk_reason, "changed=vehicle_fields")
        _assert_contains(format_yandex_canary_runs((changed,), db_path), "changed=vehicle_fields")
        _assert_equal(yandex_canary_has_warnings((baseline,)), False)
        _assert_equal(yandex_canary_has_warnings((changed,)), True)
        _assert_contains(strict_yandex_canary_message((changed,)), "morning:warning:schema_changed")
        _assert_equal(
            diff["changed"]["vehicle_fields"]["previous"],
            ["age_seconds", "arrival_minutes", "lat", "lng", "thread_id", "vehicle_id"],
        )
        _assert_equal(
            diff["changed"]["vehicle_fields"]["current"],
            ["age_seconds", "arrival_minutes", "thread_id", "vehicle_id"],
        )

    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "canary-future-baseline.sqlite"
        with connect(db_path) as connection:
            init_db(connection)
            connection.execute(
                """
                INSERT INTO yandex_canary_runs(
                    checked_at, status, source_method, profile_key, schema_hash,
                    changed_keys_json, risk_reason, raw_summary_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    (current_time + timedelta(days=1)).isoformat(),
                    "ok",
                    "vehicle_prediction",
                    MORNING.key,
                    "future-schema-hash",
                    "{}",
                    "ok",
                    _canary_summary_json(vehicle_fields=("vehicle_id",)),
                ),
            )
            current = insert_yandex_canary_run(
                connection,
                profile=MORNING,
                forecast=_canary_forecast_with_fields(lat=54.84, lng=83.11),
                checked_at=current_time,
            )
            canary_health = summarize_yandex_canary_health(connection, current_time=current_time)
        _assert_equal(current.status, "ok")
        _assert_equal(current.risk_reason, "ok")
        _assert_equal(canary_health.status, "ok")

    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "route-geometry.sqlite"
        collector = YandexTelemetryCollector(
            db_path=db_path,
            source=_LineTopologyYandexSource(),
            profiles=(MORNING,),
            clock=lambda: current_time,
        )
        results = collector.collect_once()
        _assert_equal(results[0].route_geometry_status, "saved")
        _assert_contains(results[0].route_geometry_reason, "expected=2161326768,selected=2161326768")
        with connect(db_path) as connection:
            init_db(connection)
            row = connection.execute(
                """
                SELECT profile_key, line_id, thread_id, target_stop_id, updated_at
                FROM route_geometry
                """
            ).fetchone()
            run = connection.execute("SELECT message, raw_json FROM collector_runs").fetchone()
            heartbeat = load_collector_heartbeat(connection, "yandex-collect")
        if row is None:
            raise AssertionError("expected route_geometry row")
        _assert_equal(tuple(row), ("morning", "line-74", "2161326768", "stop__9982194", current_time.isoformat()))
        _assert_contains(run["message"], "geometry=saved")
        _assert_contains(run["message"], "expected=2161326768")
        _assert_contains(run["raw_json"], '"route_geometry_status": "saved"')
        _assert_contains(run["raw_json"], '"route_geometry_reason": "expected=2161326768')
        if heartbeat is None:
            raise AssertionError("expected collector heartbeat")
        _assert_contains(heartbeat.last_message, "geometry=saved")

    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "route-geometry-touch.sqlite"
        stale_time = datetime(2026, 5, 1, 9, 15, tzinfo=NOVOSIBIRSK_TZ)
        with connect(db_path) as connection:
            init_db(connection)
            upsert_route_geometry(
                connection,
                profile_key=MORNING.key,
                target_stop_id="stop__9982194",
                topology=_line_topology(),
                updated_at=stale_time,
            )
        collector = YandexTelemetryCollector(
            db_path=db_path,
            source=_NoLineTopologyYandexSource(),
            profiles=(MORNING,),
            clock=lambda: current_time,
        )
        results = collector.collect_once()
        _assert_equal(results[0].route_geometry_status, "touched")
        with connect(db_path) as connection:
            init_db(connection)
            row = connection.execute(
                "SELECT updated_at FROM route_geometry WHERE profile_key = ?", (MORNING.key,)
            ).fetchone()
            run = connection.execute("SELECT message FROM collector_runs ORDER BY id DESC LIMIT 1").fetchone()
        _assert_equal(row["updated_at"], current_time.isoformat())
        _assert_contains(run["message"], "geometry=touched")

    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "route-geometry-cached.sqlite"
        cached_time = datetime(2026, 6, 4, 6, 15, tzinfo=NOVOSIBIRSK_TZ)
        with connect(db_path) as connection:
            init_db(connection)
            upsert_route_geometry(
                connection,
                profile_key=MORNING.key,
                target_stop_id="stop__9982194",
                topology=_line_topology(),
                updated_at=cached_time,
            )
        collector = YandexTelemetryCollector(
            db_path=db_path,
            source=_NoVehicleNoLineTopologySource(),
            profiles=(MORNING,),
            clock=lambda: current_time,
        )
        results = collector.collect_once()
        _assert_equal(results[0].route_geometry_status, "cached")
        with connect(db_path) as connection:
            init_db(connection)
            row = connection.execute(
                "SELECT updated_at FROM route_geometry WHERE profile_key = ?", (MORNING.key,)
            ).fetchone()
            run = connection.execute("SELECT message FROM collector_runs ORDER BY id DESC LIMIT 1").fetchone()
        _assert_equal(row["updated_at"], cached_time.isoformat())
        _assert_contains(run["message"], "geometry=cached")

    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "source-error.sqlite"
        collector = YandexTelemetryCollector(
            db_path=db_path,
            source=_FailingYandexSource(),
            profiles=(MORNING,),
            clock=lambda: current_time,
        )
        results = collector.collect_once()
        _assert_equal(results[0].source_status, "unavailable")
        _assert_contains(results[0].fallback_reason, "collector_error:RuntimeError")
        with connect(db_path) as connection:
            init_db(connection)
            heartbeat = load_collector_heartbeat(connection, "yandex-collect")
            _assert_equal(count_yandex_forecast_samples(connection), 1)
        if heartbeat is None:
            raise AssertionError("expected collector heartbeat")
        _assert_equal(heartbeat.last_status, "unavailable")
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
            summary = summarize_yandex_telemetry(connection, hours=24, profile_key="morning", current_time=current_time)
        _assert_equal(health.collector.healthy, False)
        _assert_equal(summary.collector_runs.total_runs, 1)
        _assert_equal(summary.collector_runs.statuses[0].key, "unavailable")

    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "traffic-ok.sqlite"
        collector = YandexTelemetryCollector(
            db_path=db_path,
            source=_FakeYandexSource(),
            traffic_source=_ok_traffic_source,
            profiles=(MORNING,),
            clock=lambda: current_time,
        )
        results = collector.collect_once()
        _assert_equal(results[0].traffic_provider, "fake_route")
        _assert_equal(results[0].traffic_status, "ok")
        _assert_equal(results[0].traffic_reason, "")
        with connect(db_path) as connection:
            init_db(connection)
            row = connection.execute(
                """
                SELECT traffic_provider, traffic_status, route_duration_seconds, traffic_distance_meters
                FROM yandex_forecast_samples
                """
            ).fetchone()
            heartbeat = load_collector_heartbeat(connection, "yandex-collect")
            summary = summarize_yandex_telemetry(
                connection,
                hours=24,
                profile_key="morning",
                current_time=current_time,
            )
        _assert_equal(tuple(row), ("fake_route", "ok", 1800, 12000))
        if heartbeat is None:
            raise AssertionError("expected collector heartbeat")
        _assert_equal(heartbeat.last_status, "ok")
        _assert_equal(summary.collector_runs.traffic_ok_runs, 1)
        _assert_equal(summary.collector_runs.statuses[0].key, "ok")

    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "traffic-error.sqlite"
        collector = YandexTelemetryCollector(
            db_path=db_path,
            source=_FakeYandexSource(),
            traffic_source=_failing_traffic_source,
            profiles=(MORNING,),
            clock=lambda: current_time,
        )
        results = collector.collect_once()
        _assert_equal(results[0].traffic_provider, "collector")
        _assert_equal(results[0].traffic_status, "error")
        _assert_contains(results[0].traffic_reason, "traffic_error:RuntimeError")
        with connect(db_path) as connection:
            init_db(connection)
            row = connection.execute(
                "SELECT traffic_provider, traffic_status, traffic_raw_json FROM yandex_forecast_samples"
            ).fetchone()
            run = connection.execute("SELECT raw_json FROM collector_runs").fetchone()
            heartbeat = load_collector_heartbeat(connection, "yandex-collect")
        _assert_equal((row["traffic_provider"], row["traffic_status"]), ("collector", "error"))
        _assert_contains(row["traffic_raw_json"], "traffic_error:RuntimeError")
        _assert_contains(run["raw_json"], "traffic_reason")
        if heartbeat is None:
            raise AssertionError("expected collector heartbeat")
        _assert_equal(heartbeat.last_status, "partial")
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
            summary = summarize_yandex_telemetry(connection, hours=24, profile_key="morning", current_time=current_time)
        _assert_equal(health.collector.healthy, False)
        _assert_equal(summary.collector_runs.total_runs, 1)
        _assert_equal(summary.collector_runs.statuses[0].key, "partial")

    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "skipped-run.sqlite"
        weekend_time = datetime(2026, 6, 6, 9, 15, tzinfo=NOVOSIBIRSK_TZ)
        collector = YandexTelemetryCollector(
            db_path=db_path,
            source=_FakeYandexSource(),
            profiles=(MORNING,),
            report_windows_only=True,
            clock=lambda: weekend_time,
        )
        results = collector.collect_once()
        _assert_equal(results, ())
        with connect(db_path) as connection:
            init_db(connection)
            summary = summarize_yandex_telemetry(connection, hours=24, profile_key="morning", current_time=weekend_time)
            heartbeat = load_collector_heartbeat(connection, "yandex-collect")
        if heartbeat is None:
            raise AssertionError("expected collector heartbeat")
        _assert_equal(heartbeat.last_status, "skipped")
        _assert_equal(summary.collector_runs.total_runs, 1)
        _assert_equal(summary.collector_runs.result_runs, 0)
        _assert_equal(summary.collector_runs.skipped_runs, 1)
        _assert_equal(summary.collector_runs.statuses[0].key, "skipped")

    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "malformed-collector-runs.sqlite"
        active_time = datetime(2026, 6, 4, 9, 15, tzinfo=NOVOSIBIRSK_TZ)
        with connect(db_path) as connection:
            init_db(connection)
            _insert_collector_run_row(
                connection,
                started_at=active_time.replace(minute=13).isoformat(),
                result_count=1,
                eta_result_count=1,
                traffic_ok_count=1,
            )
            _insert_collector_run_row(
                connection,
                started_at=active_time.replace(minute=14).isoformat(),
                result_count=-1,
                eta_result_count=1,
                traffic_ok_count=1,
            )
            _insert_collector_run_row(
                connection,
                started_at=active_time.replace(minute=15).isoformat(),
                result_count=1,
                eta_result_count="bad",
                traffic_ok_count=1,
            )
            connection.commit()
            summary = summarize_collector_runs(connection, hours=24, current_time=active_time)
            window = summarize_collector_runs_for_report_window(
                connection,
                report_window=REPORT_WINDOWS[0],
                current_date=active_time,
                days=14,
            )
        _assert_equal(summary.total_runs, 1)
        _assert_equal(summary.result_runs, 1)
        _assert_equal(summary.eta_runs, 1)
        _assert_equal(summary.traffic_ok_runs, 1)
        _assert_equal(summary.latest_started_at, active_time.replace(minute=13))
        _assert_equal(summary.statuses[0].key, "ok")
        _assert_equal(window.total_runs, 1)
        _assert_equal(window.result_runs, 1)
        _assert_equal(window.latest_started_at, active_time.replace(minute=13))

    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "skipped-run-prunes-telemetry.sqlite"
        active_time = datetime(2026, 6, 4, 9, 15, tzinfo=NOVOSIBIRSK_TZ)
        weekend_time = datetime(2026, 6, 6, 9, 15, tzinfo=NOVOSIBIRSK_TZ)
        with connect(db_path) as connection:
            init_db(connection)
            insert_yandex_snapshot(connection, MORNING.key, _FakeYandexSource().get_forecast(), active_time)
            _assert_equal(count_yandex_snapshots(connection), 1)
            _assert_equal(count_yandex_forecast_samples(connection), 1)
            _assert_equal(count_report_window_snapshots(connection), 1)
        collector = YandexTelemetryCollector(
            db_path=db_path,
            source=_FakeYandexSource(),
            profiles=(MORNING,),
            report_windows_only=True,
            retention_days=1,
            clock=lambda: weekend_time,
        )
        results = collector.collect_once()
        _assert_equal(results, ())
        with connect(db_path) as connection:
            init_db(connection)
            _assert_equal(count_yandex_snapshots(connection), 0)
            _assert_equal(count_yandex_forecast_samples(connection), 0)
            _assert_equal(count_report_window_snapshots(connection), 0)

    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "active-report-window-run.sqlite"
        active_time = datetime(2026, 6, 4, 9, 15, tzinfo=NOVOSIBIRSK_TZ)
        collector = YandexTelemetryCollector(
            db_path=db_path,
            source=_FakeYandexSource(),
            profiles=(MORNING,),
            report_windows_only=True,
            clock=lambda: active_time,
        )
        results = collector.collect_once()
        _assert_equal(len(results), 1)
        with connect(db_path) as connection:
            init_db(connection)
            health = summarize_forecast_health(
                connection,
                current_date=active_time,
                days=14,
                min_samples=20,
                min_distinct_days=3,
                primary_bucket_minutes=30,
                fallback_bucket_minutes=60,
                max_age_seconds=180,
                step_minutes=30,
            )
        _assert_equal(health.collector.status, "ok")
        _assert_equal(health.windows[0].collector_runs, 1)
        _assert_equal(health.windows[0].collector_eta_runs, 1)
        _assert_equal(health.windows[0].collector_run_statuses[0].key, "ok")
        _assert_equal(health.windows[0].status, "insufficient_bucket_coverage")
        _assert_equal(health.windows[1].collector_runs, 0)
        _assert_equal(health.windows[1].status, "no_collector_runs")

    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "active-window-skipped.sqlite"
        active_time = datetime(2026, 6, 4, 9, 15, tzinfo=NOVOSIBIRSK_TZ)
        with connect(db_path) as connection:
            init_db(connection)
            update_collector_heartbeat(
                connection,
                name="yandex-collect",
                pid=123,
                profile_filter="all",
                last_status="skipped",
                last_message="outside_report_window",
                updated_at=active_time,
            )
            health = summarize_forecast_health(
                connection,
                current_date=active_time,
                days=14,
                min_samples=20,
                min_distinct_days=3,
                primary_bucket_minutes=30,
                fallback_bucket_minutes=60,
                max_age_seconds=180,
                step_minutes=30,
            )
        _assert_equal(health.collector.status, "unexpected_skipped")
        _assert_equal(health.collector.healthy, False)
        _assert_equal(health.windows[0].status, "no_collector_runs")

    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "api-contract-risk.sqlite"
        active_time = datetime(2026, 6, 4, 9, 15, tzinfo=NOVOSIBIRSK_TZ)
        collector = YandexTelemetryCollector(
            db_path=db_path,
            source=_MissingThreadSource(),
            profiles=(MORNING,),
            report_windows_only=True,
            clock=lambda: active_time,
        )
        results = collector.collect_once()
        _assert_equal(len(results), 1)
        _assert_equal(results[0].source_status, "no_target")
        _assert_equal(results[0].fallback_reason, "direction_thread_missing")
        with connect(db_path) as connection:
            init_db(connection)
            health = summarize_forecast_health(
                connection,
                current_date=active_time,
                days=14,
                min_samples=20,
                min_distinct_days=3,
                primary_bucket_minutes=30,
                fallback_bucket_minutes=60,
                max_age_seconds=180,
                step_minutes=30,
            )
        _assert_equal(health.windows[0].status, "api_contract_risk")
        _assert_equal(health.windows[0].api_risk_samples, 1)
        _assert_equal(health.windows[0].api_risk_reasons[0].key, "direction_thread_missing")
        _assert_equal(health.windows[0].api_risk_reasons[0].count, 1)

    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "aggregate-api-contract-risk.sqlite"
        active_time = datetime(2026, 6, 4, 9, 15, tzinfo=NOVOSIBIRSK_TZ)
        collector = YandexTelemetryCollector(
            db_path=db_path,
            source=_AggregateDirectionRiskSource(),
            profiles=(MORNING,),
            report_windows_only=True,
            clock=lambda: active_time,
        )
        results = collector.collect_once()
        _assert_equal(len(results), 1)
        _assert_equal(results[0].source_status, "unavailable")
        _assert_contains(results[0].fallback_reason, "browser:no_target:direction_thread_not_found")
        with connect(db_path) as connection:
            init_db(connection)
            health = summarize_forecast_health(
                connection,
                current_date=active_time,
                days=14,
                min_samples=20,
                min_distinct_days=3,
                primary_bucket_minutes=30,
                fallback_bucket_minutes=60,
                max_age_seconds=180,
                step_minutes=30,
            )
        _assert_equal(health.windows[0].status, "api_contract_risk")
        _assert_equal(health.windows[0].api_risk_samples, 1)
        _assert_equal(health.windows[0].api_risk_reasons[0].key, "direction_thread_not_found")

    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "api-risk-normalized-slots.sqlite"
        active_time = datetime(2026, 6, 4, 9, 15, tzinfo=NOVOSIBIRSK_TZ)
        with connect(db_path) as connection:
            init_db(connection)
            insert_yandex_snapshot(connection, MORNING.key, _AggregateDirectionRiskSource().get_forecast(), active_time)
            insert_yandex_snapshot(
                connection,
                MORNING.key,
                _AggregateDirectionRiskSource().get_forecast(),
                active_time.replace(minute=16),
            )
            health = summarize_forecast_health(
                connection,
                current_date=active_time,
                days=14,
                min_samples=20,
                min_distinct_days=3,
                primary_bucket_minutes=30,
                fallback_bucket_minutes=60,
                max_age_seconds=180,
                step_minutes=30,
            )
        _assert_equal(health.windows[0].total_samples, 1)
        _assert_equal(health.windows[0].api_risk_samples, 1)
        _assert_equal(health.windows[0].api_risk_reasons[0].count, 1)

    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "coordinates-direction-degraded.sqlite"
        active_time = datetime(2026, 6, 4, 9, 15, tzinfo=NOVOSIBIRSK_TZ)
        collector = YandexTelemetryCollector(
            db_path=db_path,
            source=_CoordinatesOnlyDirectionRiskSource(),
            profiles=(MORNING,),
            report_windows_only=True,
            clock=lambda: active_time,
        )
        results = collector.collect_once()
        _assert_equal(len(results), 1)
        _assert_equal(results[0].source_status, "coordinates_only")
        _assert_equal(results[0].route_geometry_status, "not_supported")
        _assert_equal(results[0].fallback_reason, "direction_thread_not_found")
        with connect(db_path) as connection:
            init_db(connection)
            health = summarize_forecast_health(
                connection,
                current_date=active_time,
                days=14,
                min_samples=20,
                min_distinct_days=3,
                primary_bucket_minutes=30,
                fallback_bucket_minutes=60,
                max_age_seconds=180,
                step_minutes=30,
            )
        _assert_equal(health.windows[0].status, "no_eta")
        _assert_equal(health.windows[0].api_risk_samples, 0)
        _assert_equal(health.windows[0].coordinate_fallback_samples, 1)
        _assert_equal(health.windows[0].coordinate_fallback_reasons[0].key, "direction_thread_not_found")

    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "coordinate-fallback-normalized-slots.sqlite"
        active_time = datetime(2026, 6, 4, 9, 15, tzinfo=NOVOSIBIRSK_TZ)
        with connect(db_path) as connection:
            init_db(connection)
            insert_yandex_snapshot(
                connection, MORNING.key, _CoordinatesOnlyDirectionRiskSource().get_forecast(), active_time
            )
            insert_yandex_snapshot(
                connection,
                MORNING.key,
                _CoordinatesOnlyDirectionRiskSource().get_forecast(),
                active_time.replace(minute=16),
            )
            health = summarize_forecast_health(
                connection,
                current_date=active_time,
                days=14,
                min_samples=20,
                min_distinct_days=3,
                primary_bucket_minutes=30,
                fallback_bucket_minutes=60,
                max_age_seconds=180,
                step_minutes=30,
            )
        _assert_equal(health.windows[0].total_samples, 1)
        _assert_equal(health.windows[0].coordinate_fallback_samples, 1)
        _assert_equal(health.windows[0].coordinate_fallback_reasons[0].count, 1)

    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "cached-geometry-softens-api-risk.sqlite"
        active_time = datetime(2026, 6, 4, 9, 15, tzinfo=NOVOSIBIRSK_TZ)
        with connect(db_path) as connection:
            init_db(connection)
            upsert_route_geometry(
                connection,
                profile_key=MORNING.key,
                target_stop_id="stop__9982194",
                topology=_line_topology(),
                updated_at=active_time,
            )
        collector = YandexTelemetryCollector(
            db_path=db_path,
            source=_AggregateDirectionRiskSource(),
            profiles=(MORNING,),
            report_windows_only=True,
            clock=lambda: active_time,
        )
        results = collector.collect_once()
        _assert_equal(len(results), 1)
        _assert_equal(results[0].route_geometry_status, "cached")
        with connect(db_path) as connection:
            init_db(connection)
            health = summarize_forecast_health(
                connection,
                current_date=active_time,
                days=14,
                min_samples=20,
                min_distinct_days=3,
                primary_bucket_minutes=30,
                fallback_bucket_minutes=60,
                max_age_seconds=180,
                step_minutes=30,
            )
            row = connection.execute("SELECT raw_json FROM yandex_forecast_samples LIMIT 1").fetchone()
        _assert_equal(health.windows[0].status, "no_eta")
        _assert_equal(health.windows[0].api_risk_samples, 0)
        _assert_equal(health.windows[0].coordinate_fallback_samples, 1)
        _assert_equal(health.windows[0].coordinate_fallback_reasons[0].key, "direction_thread_not_found")
        _assert_contains(row["raw_json"], '"route_geometry_status": "cached"')

    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "route-thread-drift.sqlite"
        active_time = datetime(2026, 6, 4, 9, 15, tzinfo=NOVOSIBIRSK_TZ)
        collector = YandexTelemetryCollector(
            db_path=db_path,
            source=_ThreadDriftLineTopologySource(),
            profiles=(MORNING,),
            report_windows_only=True,
            clock=lambda: active_time,
        )
        results = collector.collect_once()
        _assert_equal(len(results), 1)
        _assert_equal(results[0].source_status, "coordinates_only")
        _assert_equal(results[0].route_geometry_status, "thread_drift")
        _assert_contains(results[0].route_geometry_reason, "expected=2161326768,selected=2161326764")
        with connect(db_path) as connection:
            init_db(connection)
            route_row = connection.execute("SELECT id FROM route_geometry").fetchone()
            run = connection.execute("SELECT message, raw_json FROM collector_runs").fetchone()
            sample = connection.execute("SELECT raw_json FROM yandex_forecast_samples").fetchone()
            health = summarize_forecast_health(
                connection,
                current_date=active_time,
                days=14,
                min_samples=20,
                min_distinct_days=3,
                primary_bucket_minutes=30,
                fallback_bucket_minutes=60,
                max_age_seconds=180,
                step_minutes=30,
            )
        _assert_equal(route_row, None)
        _assert_contains(run["message"], "geometry=thread_drift")
        _assert_contains(run["raw_json"], '"route_geometry_reason": "expected=2161326768')
        _assert_contains(sample["raw_json"], '"route_geometry_reason": "expected=2161326768')
        _assert_equal(health.windows[0].status, "api_contract_risk")
        _assert_equal(health.windows[0].api_risk_samples, 1)
        _assert_equal(health.windows[0].api_risk_reasons[0].key, "route_geometry_thread_drift")
        _assert_equal(health.windows[0].coordinate_fallback_samples, 0)

    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "cache-source.sqlite"
        active_time = datetime(2026, 6, 4, 9, 15, tzinfo=NOVOSIBIRSK_TZ)
        with connect(db_path) as connection:
            init_db(connection)
            insert_yandex_snapshot(connection, MORNING.key, _FakeYandexSource().get_forecast(), active_time)
        source = CachedYandexForecastSource(db_path, max_age_seconds=600)
        fresh = source.get_forecast(MORNING, active_time)
        aged = source.get_forecast(MORNING, active_time.replace(hour=9, minute=17))
        vehicle_stale = source.get_forecast(MORNING, active_time.replace(hour=9, minute=19))
        stale = CachedYandexForecastSource(db_path, max_age_seconds=90).get_forecast(
            MORNING,
            active_time.replace(hour=9, minute=17),
        )
        _assert_equal(fresh.available, True)
        _assert_equal(fresh.arrival_minutes, (6,))
        _assert_equal(fresh.source_method, YandexSourceMethod.VEHICLE_PREDICTION)
        _assert_equal(aged.available, True)
        _assert_equal(aged.arrival_minutes, (4,))
        _assert_equal(aged.newest_age_seconds, 135)
        _assert_equal(aged.confidence, EtaConfidence.MEDIUM)
        _assert_equal(vehicle_stale.available, False)
        _assert_equal(vehicle_stale.status, YandexSourceStatus.STALE)
        _assert_equal(vehicle_stale.newest_age_seconds, 255)
        _assert_contains(vehicle_stale.fallback_reason, "cache_vehicle_stale")
        _assert_equal(stale.available, False)
        _assert_equal(stale.status, YandexSourceStatus.STALE)
        _assert_contains(stale.fallback_reason, "cache_stale")

    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "future-cache-sample.sqlite"
        active_time = datetime(2026, 6, 4, 9, 15, tzinfo=NOVOSIBIRSK_TZ)
        with connect(db_path) as connection:
            init_db(connection)
            insert_yandex_snapshot(connection, MORNING.key, _EtaYandexSource(6).get_forecast(), active_time)
            insert_yandex_snapshot(
                connection,
                MORNING.key,
                _EtaYandexSource(1).get_forecast(),
                active_time + timedelta(minutes=5),
            )

        cached = CachedYandexForecastSource(db_path, max_age_seconds=600).get_forecast(MORNING, active_time)
        _assert_equal(cached.available, True)
        _assert_equal(cached.arrival_minutes, (6,))
        _assert_equal(cached.raw_status, "cached_snapshot:0s")

    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "future-only-cache-sample.sqlite"
        active_time = datetime(2026, 6, 4, 9, 15, tzinfo=NOVOSIBIRSK_TZ)
        with connect(db_path) as connection:
            init_db(connection)
            insert_yandex_snapshot(
                connection,
                MORNING.key,
                _EtaYandexSource(1).get_forecast(),
                active_time + timedelta(minutes=5),
            )

        cached = CachedYandexForecastSource(db_path, max_age_seconds=600).get_forecast(MORNING, active_time)
        _assert_equal(cached.available, False)
        _assert_equal(cached.status, YandexSourceStatus.STALE)
        _assert_equal(cached.fallback_reason, "cache_future_only")

    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "bad-cache-vehicle-row.sqlite"
        active_time = datetime(2026, 6, 4, 9, 15, tzinfo=NOVOSIBIRSK_TZ)
        with connect(db_path) as connection:
            init_db(connection)
            insert_yandex_snapshot(connection, MORNING.key, _FakeYandexSource().get_forecast(), active_time)
            connection.execute("UPDATE yandex_vehicle_observations SET arrival_minutes = 'bad'")
            connection.commit()

        cached = CachedYandexForecastSource(db_path, max_age_seconds=600).get_forecast(MORNING, active_time)
        _assert_equal(cached.available, True)
        _assert_equal(cached.arrival_minutes, (6,))
        _assert_equal(cached.vehicles, ())
        _assert_equal(cached.vehicle_count, 1)

    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "bad-cache-row.sqlite"
        active_time = datetime(2026, 6, 4, 9, 15, tzinfo=NOVOSIBIRSK_TZ)
        with connect(db_path) as connection:
            init_db(connection)
            insert_yandex_snapshot(connection, MORNING.key, _FakeYandexSource().get_forecast(), active_time)
            connection.execute("UPDATE yandex_forecast_samples SET sampled_at = 'not-a-date'")
            connection.commit()

        bad_cache = CachedYandexForecastSource(db_path, max_age_seconds=600).get_forecast(MORNING, active_time)
        _assert_equal(bad_cache.available, False)
        _assert_equal(bad_cache.status, YandexSourceStatus.UNAVAILABLE)
        _assert_contains(bad_cache.fallback_reason, "cache_bad_row:ValueError")

    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "route-vehicle-eta-ignored.sqlite"
        active_time = datetime(2026, 6, 4, 9, 15, tzinfo=NOVOSIBIRSK_TZ)
        with connect(db_path) as connection:
            init_db(connection)
            insert_yandex_snapshot(connection, MORNING.key, _BrowserRouteEtaSource().get_forecast(), active_time)
            connection.execute(
                """
                UPDATE yandex_forecast_samples
                SET arrival_minutes = 0,
                    next_arrival_minutes_json = '[2]'
                """
            )
            connection.execute("UPDATE report_window_snapshots SET arrival_minutes_json = '[0,2]'")
            connection.commit()
            init_db(connection)
            row = connection.execute(
                """
                SELECT arrival_minutes, next_arrival_minutes_json
                FROM yandex_forecast_samples
                LIMIT 1
                """
            ).fetchone()
            report_row = connection.execute(
                "SELECT arrival_minutes_json FROM report_window_snapshots LIMIT 1",
            ).fetchone()
            observation_row = connection.execute(
                "SELECT arrival_minutes FROM yandex_vehicle_observations LIMIT 1",
            ).fetchone()
            summary = summarize_yandex_telemetry(connection, hours=24, profile_key="morning", current_time=active_time)
        _assert_equal(row["arrival_minutes"], None)
        _assert_equal(row["next_arrival_minutes_json"], "[]")
        _assert_equal(report_row["arrival_minutes_json"], "[]")
        _assert_equal(observation_row["arrival_minutes"], None)
        _assert_equal(summary.eta_snapshots, 0)
        _assert_equal(summary.eta_observations, 0)
        _assert_equal(summary.vehicle_snapshots, 1)

        source = CachedYandexForecastSource(db_path, max_age_seconds=600)
        cached = source.get_forecast(MORNING, active_time)
        _assert_equal(cached.available, False)
        _assert_equal(cached.arrival_minutes, ())
        _assert_equal(cached.fallback_reason, "legacy_route_vehicle_eta")

    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "coordinates-only-cache-reason.sqlite"
        active_time = datetime(2026, 6, 4, 9, 15, tzinfo=NOVOSIBIRSK_TZ)
        with connect(db_path) as connection:
            init_db(connection)
            insert_yandex_snapshot(connection, MORNING.key, _CoordinatesOnlySource().get_forecast(), active_time)

        cached = CachedYandexForecastSource(db_path, max_age_seconds=600).get_forecast(MORNING, active_time)
        _assert_equal(cached.available, False)
        _assert_equal(cached.status, YandexSourceStatus.COORDINATES_ONLY)
        _assert_equal(cached.fallback_reason, "raw_eta_over_limit:60")

    print("OK | yandex telemetry smoke passed")


def _assert_yandex_canary_model_guardrails(current_time: datetime) -> None:
    run = _canary_run(current_time)
    _assert_equal(run.risk_reason, "schema changed")
    _assert_value_error(lambda: _canary_run(current_time, id=0), "id")
    _assert_value_error(
        lambda: _canary_run(current_time, checked_at=current_time.replace(tzinfo=None)), "timezone-aware"
    )
    _assert_value_error(
        lambda: _canary_run(current_time, checked_at=datetime(2026, 6, 4, 7, 15, tzinfo=timezone.utc)),
        "Asia/Novosibirsk",
    )
    _assert_value_error(lambda: _canary_run(current_time, status="bad"), "status")
    _assert_value_error(lambda: _canary_run(current_time, source_method="vehicle-prediction"), "source method")
    _assert_value_error(lambda: _canary_run(current_time, profile_key="unknown"), "one of")
    _assert_value_error(lambda: _canary_run(current_time, schema_hash="A" * 16), "schema hash")
    _assert_value_error(lambda: _canary_run(current_time, risk_reason="\x1b[31m\x1b[0m"), "risk reason")
    _assert_value_error(lambda: _canary_run(current_time, changed_keys=["vehicle_fields"]), "changed keys")
    _assert_value_error(lambda: _canary_run(current_time, changed_keys=("vehicle-fields",)), "changed key")

    health = YandexCanaryHealth("warning", current_time, "\x1b[31mcanary\nwarning\x00\x1b[0m", 1)
    _assert_equal(health.risk_reason, "canary warning")
    _assert_value_error(lambda: YandexCanaryHealth("bad", current_time, "warning", 1), "status")
    _assert_value_error(
        lambda: YandexCanaryHealth("warning", current_time.replace(tzinfo=None), "warning", 1), "timezone-aware"
    )
    _assert_value_error(
        lambda: YandexCanaryHealth("warning", datetime(2026, 6, 4, 7, 15, tzinfo=timezone.utc), "warning", 1),
        "Asia/Novosibirsk",
    )
    _assert_value_error(lambda: YandexCanaryHealth("warning", current_time, "\x1b[31m\x1b[0m", 1), "risk reason")
    _assert_value_error(lambda: YandexCanaryHealth("warning", current_time, "warning", True), "risky_runs")
    _assert_value_error(lambda: YandexCanaryHealth("warning", current_time, "warning", -1), "risky_runs")
    _assert_value_error(lambda: YandexCanaryHealth("ok", current_time, "ok", 1), "risky_runs")


def _assert_yandex_canary_cli_diagnostics_are_safe(current_time: datetime) -> None:
    run = _canary_run(current_time)
    formatted = format_yandex_canary_runs((run,), Path("data/\x1b[31mcanary.sqlite"))
    strict = strict_yandex_canary_message((run,))
    _assert_no_control_characters(formatted)
    _assert_no_control_characters(strict)
    _assert_contains(formatted, "db=data/canary.sqlite")
    _assert_contains(formatted, "reason=schema changed")
    _assert_contains(strict, "morning:warning:schema changed")
    _assert_not_contains(formatted, "[31m")
    _assert_not_contains(strict, "[31m")


def _canary_run(current_time: datetime, **overrides: object) -> YandexCanaryRun:
    params = {
        "id": 1,
        "checked_at": current_time,
        "status": "warning",
        "source_method": "vehicle_prediction",
        "profile_key": "morning",
        "schema_hash": "a" * 16,
        "risk_reason": "\x1b[31mschema\nchanged\x00\x1b[0m",
        "changed_keys": ("vehicle_fields",),
    }
    params.update(overrides)
    return YandexCanaryRun(**params)  # type: ignore[arg-type]


def _forecast_ready(*, canary_health: YandexCanaryHealth, current_time: datetime) -> bool:
    summary = ForecastHealthSummary(
        days=14,
        min_samples=20,
        min_distinct_days=3,
        collector=ForecastCollectorHealth(
            name="yandex-collect",
            status="ok",
            message="ok",
            updated_at=current_time,
            age_seconds=0,
            max_age_seconds=120,
        ),
        canary=canary_health,
        windows=(_ready_window(current_time),),
    )
    return summary.ready


def _ready_window(current_time: datetime) -> ForecastWindowHealth:
    return ForecastWindowHealth(
        window_key="weekday_morning_09_12",
        profile_key="morning",
        status="ready",
        reason="all report-window buckets have enough fresh ETA samples",
        total_samples=20,
        eta_samples=20,
        fresh_eta_samples=20,
        traffic_samples=20,
        ready_buckets=2,
        total_buckets=2,
        forecast_without_report_samples=0,
        report_without_forecast_samples=0,
        collector_runs=1,
        collector_eta_runs=1,
        collector_traffic_ok_runs=1,
        collector_run_statuses=(),
        api_risk_samples=0,
        api_risk_reasons=(),
        coordinate_fallback_samples=0,
        coordinate_fallback_reasons=(),
        arrival_events=5,
        prediction_events=10,
        prediction_evaluations=10,
        prediction_miss_cases=0,
        bot_prediction_events=10,
        bot_prediction_evaluations=10,
        bot_prediction_miss_cases=0,
        truth_status="ready",
        truth_reason="enough truth events",
        latest_arrival_at=current_time,
        collector_latest_started_at=current_time,
        missing_bucket_labels=(),
        bucket_gaps=(),
        latest_sampled_at=current_time,
    )


class _FakeYandexSource:
    def get_forecast(self, *_args: object) -> YandexLiveForecast:
        return YandexLiveForecast(
            enabled=True,
            available=True,
            source_method=YandexSourceMethod.VEHICLE_PREDICTION,
            status=YandexSourceStatus.OK,
            arrival_minutes=(6,),
            vehicles=(
                YandexVehicle(
                    vehicle_id="vehicle-1",
                    lat=54.84,
                    lng=83.11,
                    arrival_minutes=6,
                    age_seconds=15,
                    thread_id="2161326764",
                ),
            ),
            vehicle_count=1,
            newest_age_seconds=15,
            confidence=EtaConfidence.HIGH,
            fallback_reason="vehicle_prediction",
        )


class _EtaYandexSource:
    def __init__(self, arrival_minutes: int) -> None:
        self._arrival_minutes = arrival_minutes

    def get_forecast(self, *_args: object) -> YandexLiveForecast:
        return YandexLiveForecast(
            enabled=True,
            available=True,
            source_method=YandexSourceMethod.VEHICLE_PREDICTION,
            status=YandexSourceStatus.OK,
            arrival_minutes=(self._arrival_minutes,),
            vehicles=(
                YandexVehicle(
                    vehicle_id=f"vehicle-{self._arrival_minutes}",
                    lat=54.84,
                    lng=83.11,
                    arrival_minutes=self._arrival_minutes,
                    age_seconds=15,
                    thread_id="2161326764",
                ),
            ),
            vehicle_count=1,
            newest_age_seconds=15,
            confidence=EtaConfidence.HIGH,
            fallback_reason="vehicle_prediction",
        )


class _LineTopologyYandexSource(_FakeYandexSource):
    def __init__(self) -> None:
        self._consumed = False

    def consume_line_topologies(self) -> tuple[YandexLineTopology, ...]:
        if self._consumed:
            return ()
        self._consumed = True
        return (_line_topology(),)


class _NoLineTopologyYandexSource(_FakeYandexSource):
    def consume_line_topologies(self) -> tuple[YandexLineTopology, ...]:
        return ()


class _NoVehicleNoLineTopologySource:
    def get_forecast(self, *_args: object) -> YandexLiveForecast:
        return YandexLiveForecast.unavailable(
            status=YandexSourceStatus.UNAVAILABLE,
            source_method=YandexSourceMethod.NONE,
            reason="no_vehicles",
        )

    def consume_line_topologies(self) -> tuple[YandexLineTopology, ...]:
        return ()


class _FailingYandexSource:
    def get_forecast(self, *_args: object) -> YandexLiveForecast:
        raise RuntimeError("source boom")


class _BrowserRouteEtaSource:
    def get_forecast(self, *_args: object) -> YandexLiveForecast:
        return YandexLiveForecast(
            enabled=True,
            available=True,
            source_method=YandexSourceMethod.BROWSER,
            status=YandexSourceStatus.OK,
            arrival_minutes=(0,),
            vehicles=(
                YandexVehicle(
                    vehicle_id="route-vehicle-1",
                    lat=54.84,
                    lng=83.11,
                    arrival_minutes=0,
                    age_seconds=15,
                    thread_id="2161326768",
                ),
            ),
            vehicle_count=1,
            newest_age_seconds=15,
            confidence=EtaConfidence.HIGH,
            fallback_reason="legacy_route_vehicle_eta",
        )


class _CoordinatesOnlySource:
    def get_forecast(self, *_args: object) -> YandexLiveForecast:
        return YandexLiveForecast(
            enabled=True,
            available=False,
            source_method=YandexSourceMethod.BROWSER,
            status=YandexSourceStatus.COORDINATES_ONLY,
            vehicles=(
                YandexVehicle(
                    vehicle_id="coordinates-only-1",
                    lat=54.84,
                    lng=83.11,
                    age_seconds=15,
                    thread_id="2161326768",
                ),
            ),
            vehicle_count=1,
            newest_age_seconds=15,
            confidence=EtaConfidence.LOW,
            fallback_reason="raw_eta_over_limit:60",
        )


class _CoordinatesOnlyDirectionRiskSource:
    def get_forecast(self, *_args: object) -> YandexLiveForecast:
        return YandexLiveForecast(
            enabled=True,
            available=False,
            source_method=YandexSourceMethod.BROWSER,
            status=YandexSourceStatus.COORDINATES_ONLY,
            vehicles=(
                YandexVehicle(
                    vehicle_id="coordinates-direction-1",
                    lat=54.84,
                    lng=83.11,
                    age_seconds=15,
                    thread_id="",
                ),
            ),
            vehicle_count=1,
            newest_age_seconds=15,
            confidence=EtaConfidence.LOW,
            fallback_reason="direction_thread_not_found",
        )


class _ThreadDriftLineTopologySource(_CoordinatesOnlyDirectionRiskSource):
    def consume_line_topologies(self) -> tuple[YandexLineTopology, ...]:
        return (_thread_drift_topology(),)


class _MissingThreadSource:
    def get_forecast(self, *_args: object) -> YandexLiveForecast:
        return YandexLiveForecast.unavailable(
            status=YandexSourceStatus.NO_TARGET,
            source_method=YandexSourceMethod.BROWSER,
            reason="direction_thread_missing",
        )


class _AggregateDirectionRiskSource:
    def get_forecast(self, *_args: object) -> YandexLiveForecast:
        return YandexLiveForecast.unavailable(
            status=YandexSourceStatus.UNAVAILABLE,
            reason=(
                "http:needs_signature:bad_request_maybe_s; "
                "vehicle_prediction:empty:browser_no_prediction_response; "
                "browser:no_target:direction_thread_not_found"
            ),
        )

    def consume_line_topologies(self) -> tuple[YandexLineTopology, ...]:
        return ()


def _ok_traffic_source(*_args: object) -> RouteTrafficSnapshot:
    return RouteTrafficSnapshot(
        provider="fake_route",
        status="ok",
        route_duration_seconds=1800,
        route_duration_in_traffic_seconds=1800,
        distance_meters=12000,
    )


def _failing_traffic_source(*_args: object) -> object:
    raise RuntimeError("traffic boom")


def _canary_forecast_with_fields(*, lat: float | None, lng: float | None) -> YandexLiveForecast:
    return YandexLiveForecast(
        enabled=True,
        available=True,
        source_method=YandexSourceMethod.VEHICLE_PREDICTION,
        status=YandexSourceStatus.OK,
        arrival_minutes=(6,),
        vehicles=(
            YandexVehicle(
                vehicle_id="schema-diff-1",
                lat=lat,
                lng=lng,
                arrival_minutes=6,
                age_seconds=10,
                thread_id="2161326768",
            ),
        ),
        vehicle_count=1,
        newest_age_seconds=10,
        confidence=EtaConfidence.HIGH,
    )


def _canary_summary_json(*, vehicle_fields: tuple[str, ...]) -> str:
    return json.dumps(
        {
            "available": True,
            "status": "ok",
            "source_method": "vehicle_prediction",
            "arrival_count": 1,
            "vehicle_count": 1,
            "vehicle_fields": list(vehicle_fields),
            "has_diagnostics": False,
            "fallback_reason_prefix": "",
        },
        ensure_ascii=False,
    )


def _run_collector_lock_smoke() -> None:
    with TemporaryDirectory() as temp_dir:
        lock_path = Path(temp_dir) / "yandex-collect.lock"
        with _collector_lock(lock_path):
            owner_pid = lock_path.read_text(encoding="utf-8").strip()
            _assert_equal(owner_pid, str(os.getpid()))
            try:
                with _collector_lock(lock_path):
                    pass
            except SystemExit as exc:
                _assert_contains(str(exc), "collector already running")
                _assert_contains(str(exc), owner_pid)
            else:
                raise AssertionError("expected nested collector lock to fail")
            _assert_equal(lock_path.read_text(encoding="utf-8").strip(), owner_pid)

        with _collector_lock(lock_path):
            _assert_equal(lock_path.read_text(encoding="utf-8").strip(), str(os.getpid()))


def _line_topology() -> YandexLineTopology:
    return YandexLineTopology(
        line_id="line-74",
        active_thread_id="2161326764",
        threads=(
            YandexLineThread(
                thread_id="2161326764",
                line_id="line-74",
                name="74",
                vehicle_type="minibus",
                start_stop_id="stop-other",
                start_stop_name="Другое направление",
                end_stop_id="stop__9982194",
                end_stop_name="Цель",
                stops=(
                    YandexLineStop(stop_id="stop-other", name="Другое направление", lat=54.84, lng=83.08),
                    YandexLineStop(stop_id="stop__9982194", name="Цель", lat=54.93, lng=83.09),
                ),
                points=(
                    YandexLinePoint(lat=54.84, lng=83.08),
                    YandexLinePoint(lat=54.93, lng=83.09),
                ),
            ),
            YandexLineThread(
                thread_id="2161326768",
                line_id="line-74",
                name="74",
                vehicle_type="minibus",
                start_stop_id="stop-start",
                start_stop_name="Старт",
                end_stop_id="stop__9982194",
                end_stop_name="Цель",
                stops=(
                    YandexLineStop(stop_id="stop-start", name="Старт", lat=54.94, lng=83.10),
                    YandexLineStop(stop_id="stop__9982194", name="Цель", lat=54.93, lng=83.09),
                ),
                points=(
                    YandexLinePoint(lat=54.94, lng=83.10),
                    YandexLinePoint(lat=54.93, lng=83.09),
                ),
            ),
        ),
    )


def _thread_drift_topology() -> YandexLineTopology:
    return YandexLineTopology(
        line_id="line-74",
        active_thread_id="2161326764",
        threads=(
            YandexLineThread(
                thread_id="2161326764",
                line_id="line-74",
                name="74",
                vehicle_type="minibus",
                start_stop_id="stop-other",
                start_stop_name="Другое направление",
                end_stop_id="stop__9982194",
                end_stop_name="Цель",
                stops=(
                    YandexLineStop(stop_id="stop-other", name="Другое направление", lat=54.84, lng=83.08),
                    YandexLineStop(stop_id="stop__9982194", name="Цель", lat=54.93, lng=83.09),
                ),
                points=(
                    YandexLinePoint(lat=54.84, lng=83.08),
                    YandexLinePoint(lat=54.93, lng=83.09),
                ),
            ),
        ),
    )


def _insert_yandex_snapshot_row(connection: sqlite3.Connection, *, sampled_at: str) -> int:
    cursor = connection.execute(
        """
        INSERT INTO yandex_snapshots(
            sampled_at, profile_key, source_method, source_status, available,
            vehicle_count, arrival_minutes_json, fallback_reason, raw_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            sampled_at,
            MORNING.key,
            YandexSourceMethod.VEHICLE_PREDICTION.value,
            YandexSourceStatus.OK.value,
            1,
            1,
            "[6]",
            "",
            json.dumps({"raw_status": "estimated", "fallback_reason": ""}, ensure_ascii=False),
        ),
    )
    return int(cursor.lastrowid)


def _insert_yandex_observation_row(
    connection: sqlite3.Connection,
    *,
    snapshot_id: int,
    vehicle_id: str = "malformed-snapshot-time",
    lat: object = 54.84,
    lng: object = 83.11,
    arrival_minutes: object = 6,
    age_seconds: object = 10,
) -> None:
    connection.execute(
        """
        INSERT INTO yandex_vehicle_observations(
            snapshot_id, profile_key, source_method, source_status,
            vehicle_id, thread_id, lat, lng, arrival_minutes, age_seconds, raw_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            snapshot_id,
            MORNING.key,
            YandexSourceMethod.VEHICLE_PREDICTION.value,
            YandexSourceStatus.OK.value,
            vehicle_id,
            "2161326764",
            lat,
            lng,
            arrival_minutes,
            age_seconds,
            "{}",
        ),
    )


def _insert_collector_run_row(
    connection: sqlite3.Connection,
    *,
    started_at: str,
    result_count: object,
    eta_result_count: object,
    traffic_ok_count: object,
) -> None:
    connection.execute(
        """
        INSERT INTO collector_runs(
            name, started_at, completed_at, pid, profile_filter,
            report_windows_only, active_profiles_json, status, message,
            result_count, eta_result_count, traffic_ok_count, raw_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "yandex-collect",
            started_at,
            started_at,
            1001,
            "all",
            1,
            '["morning"]',
            "ok",
            "smoke",
            result_count,
            eta_result_count,
            traffic_ok_count,
            "{}",
        ),
    )


def _assert_equal(actual: object, expected: object) -> None:
    if actual != expected:
        raise AssertionError(f"expected {expected!r}, got {actual!r}")


def _assert_contains(text: str, expected: str) -> None:
    if expected not in text:
        raise AssertionError(f"expected {expected!r} in {text!r}")


def _assert_not_contains(text: str, unexpected: str) -> None:
    if unexpected in text:
        raise AssertionError(f"did not expect {unexpected!r} in {text!r}")


def _assert_no_control_characters(text: str) -> None:
    for character in text:
        if character != "\n" and not character.isprintable():
            raise AssertionError(f"unexpected control character {character!r} in {text!r}")


def _assert_value_error(action: Callable[[], object], expected: str) -> None:
    try:
        action()
    except ValueError as exc:
        _assert_contains(str(exc), expected)
        return
    raise AssertionError(f"expected ValueError containing {expected!r}")


if __name__ == "__main__":
    main()
