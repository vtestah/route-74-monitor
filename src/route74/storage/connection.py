from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from route74.domain.runtime_sources import BOT_EVENT_USER_REPLY, RUNTIME_SOURCE_WEB_APP


DEFAULT_DB = Path("data/route74.sqlite")
SQLITE_BUSY_TIMEOUT_MS = 30_000
SQLITE_TIMEOUT_SECONDS = SQLITE_BUSY_TIMEOUT_MS / 1000


def connect(db_path: Path = DEFAULT_DB) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path, timeout=SQLITE_TIMEOUT_SECONDS)
    return _configure_connection(connection, readonly=False)


def connect_readonly(db_path: Path = DEFAULT_DB) -> sqlite3.Connection:
    if not db_path.exists():
        raise FileNotFoundError(f"SQLite database does not exist: {db_path}")
    uri = f"{db_path.expanduser().resolve().as_uri()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=SQLITE_TIMEOUT_SECONDS)
    return _configure_connection(connection, readonly=True)


def init_db(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sampled_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS yandex_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sampled_at TEXT NOT NULL,
            profile_key TEXT NOT NULL,
            source_method TEXT NOT NULL,
            source_status TEXT NOT NULL,
            available INTEGER NOT NULL,
            vehicle_count INTEGER NOT NULL,
            arrival_minutes_json TEXT NOT NULL,
            fallback_reason TEXT NOT NULL,
            raw_json TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS yandex_vehicle_observations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_id INTEGER NOT NULL REFERENCES yandex_snapshots(id),
            profile_key TEXT NOT NULL,
            source_method TEXT NOT NULL,
            source_status TEXT NOT NULL,
            vehicle_id TEXT NOT NULL,
            thread_id TEXT NOT NULL,
            lat REAL,
            lng REAL,
            arrival_minutes INTEGER,
            age_seconds INTEGER,
            raw_json TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS collector_heartbeat (
            name TEXT PRIMARY KEY,
            updated_at TEXT NOT NULL,
            pid INTEGER NOT NULL,
            profile_filter TEXT NOT NULL,
            last_status TEXT NOT NULL,
            last_message TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS collector_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            started_at TEXT NOT NULL,
            completed_at TEXT NOT NULL,
            pid INTEGER NOT NULL,
            profile_filter TEXT NOT NULL,
            report_windows_only INTEGER NOT NULL,
            active_profiles_json TEXT NOT NULL,
            status TEXT NOT NULL,
            message TEXT NOT NULL,
            result_count INTEGER NOT NULL,
            eta_result_count INTEGER NOT NULL,
            traffic_ok_count INTEGER NOT NULL,
            raw_json TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS bot_update_offsets (
            name TEXT PRIMARY KEY,
            update_offset INTEGER NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS report_window_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            yandex_snapshot_id INTEGER NOT NULL REFERENCES yandex_snapshots(id),
            sampled_at TEXT NOT NULL,
            service_date TEXT NOT NULL,
            weekday INTEGER NOT NULL,
            report_window_key TEXT NOT NULL,
            profile_key TEXT NOT NULL,
            window_start TEXT NOT NULL,
            window_end TEXT NOT NULL,
            source_method TEXT NOT NULL,
            source_status TEXT NOT NULL,
            available INTEGER NOT NULL,
            vehicle_count INTEGER NOT NULL,
            arrival_minutes_json TEXT NOT NULL,
            traffic_provider TEXT NOT NULL,
            traffic_status TEXT NOT NULL,
            traffic_jams_level INTEGER,
            route_duration_seconds INTEGER,
            route_duration_in_traffic_seconds INTEGER,
            traffic_delay_seconds INTEGER,
            traffic_distance_meters INTEGER,
            traffic_raw_json TEXT NOT NULL,
            raw_json TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS yandex_forecast_samples (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            yandex_snapshot_id INTEGER NOT NULL REFERENCES yandex_snapshots(id),
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
            traffic_delay_seconds INTEGER,
            traffic_jams_level INTEGER,
            route_duration_seconds INTEGER,
            route_duration_in_traffic_seconds INTEGER,
            traffic_distance_meters INTEGER,
            traffic_raw_json TEXT NOT NULL DEFAULT '{}',
            raw_json TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS route_geometry (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            profile_key TEXT NOT NULL UNIQUE,
            line_id TEXT NOT NULL,
            thread_id TEXT NOT NULL,
            target_stop_id TEXT NOT NULL,
            route_polyline_json TEXT NOT NULL,
            stops_json TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            raw_json TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS arrival_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            yandex_snapshot_id INTEGER REFERENCES yandex_snapshots(id),
            profile_key TEXT NOT NULL,
            vehicle_id TEXT NOT NULL,
            thread_id TEXT NOT NULL,
            stop_id TEXT NOT NULL,
            arrived_at TEXT NOT NULL,
            source TEXT NOT NULL,
            confidence TEXT NOT NULL,
            lat REAL,
            lng REAL,
            raw_json TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS prediction_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            yandex_snapshot_id INTEGER REFERENCES yandex_snapshots(id),
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
            runtime_source TEXT NOT NULL DEFAULT '',
            raw_json TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS prediction_evaluations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            prediction_event_id INTEGER NOT NULL UNIQUE REFERENCES prediction_events(id),
            arrival_event_id INTEGER NOT NULL REFERENCES arrival_events(id),
            profile_key TEXT NOT NULL,
            evaluated_at TEXT NOT NULL,
            actual_minutes INTEGER NOT NULL,
            predicted_minutes INTEGER NOT NULL,
            error_minutes INTEGER NOT NULL,
            bucket TEXT NOT NULL,
            source TEXT NOT NULL,
            raw_json TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS yandex_canary_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            checked_at TEXT NOT NULL,
            status TEXT NOT NULL,
            source_method TEXT NOT NULL,
            profile_key TEXT NOT NULL,
            schema_hash TEXT NOT NULL,
            changed_keys_json TEXT NOT NULL,
            risk_reason TEXT NOT NULL,
            raw_summary_json TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS bot_interaction_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            received_at TEXT NOT NULL,
            chat_id_hash TEXT NOT NULL,
            update_type TEXT NOT NULL,
            command TEXT NOT NULL,
            event_kind TEXT NOT NULL DEFAULT 'user_reply',
            profile_key TEXT NOT NULL DEFAULT '',
            reply_source TEXT NOT NULL,
            yandex_source_method TEXT NOT NULL,
            forecast_ms INTEGER NOT NULL,
            render_ms INTEGER NOT NULL,
            send_ms INTEGER NOT NULL,
            total_ms INTEGER NOT NULL,
            status TEXT NOT NULL,
            error TEXT NOT NULL,
            no_eta_reason TEXT NOT NULL DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS vehicle_progress_tracks (
            profile_key TEXT NOT NULL,
            vehicle_id TEXT NOT NULL,
            thread_id TEXT NOT NULL,
            progress_meters REAL NOT NULL,
            velocity_mps REAL NOT NULL,
            updated_at TEXT NOT NULL,
            confidence TEXT NOT NULL,
            stalled_seconds INTEGER NOT NULL,
            sample_count INTEGER NOT NULL,
            raw_json TEXT NOT NULL,
            PRIMARY KEY(profile_key, vehicle_id)
        );

        CREATE INDEX IF NOT EXISTS idx_yandex_snapshots_sampled_profile
            ON yandex_snapshots(sampled_at, profile_key);

        CREATE INDEX IF NOT EXISTS idx_yandex_observations_snapshot_profile
            ON yandex_vehicle_observations(snapshot_id, profile_key);

        CREATE INDEX IF NOT EXISTS idx_collector_runs_name_started
            ON collector_runs(name, started_at);

        CREATE INDEX IF NOT EXISTS idx_report_windows_date_window_profile
            ON report_window_snapshots(service_date, report_window_key, profile_key);

        CREATE UNIQUE INDEX IF NOT EXISTS idx_report_windows_yandex_snapshot
            ON report_window_snapshots(yandex_snapshot_id);

        CREATE INDEX IF NOT EXISTS idx_report_windows_sampled
            ON report_window_snapshots(sampled_at);

        CREATE UNIQUE INDEX IF NOT EXISTS idx_yandex_forecast_samples_snapshot
            ON yandex_forecast_samples(yandex_snapshot_id);

        CREATE INDEX IF NOT EXISTS idx_yandex_forecast_samples_profile_time
            ON yandex_forecast_samples(profile_key, sampled_at);

        CREATE INDEX IF NOT EXISTS idx_yandex_forecast_samples_profile_weekday_minute
            ON yandex_forecast_samples(profile_key, weekday, minute_of_day, sampled_at);

        CREATE INDEX IF NOT EXISTS idx_yandex_forecast_samples_report_window
            ON yandex_forecast_samples(report_window_key, profile_key, sampled_at);

        CREATE INDEX IF NOT EXISTS idx_route_geometry_profile_thread
            ON route_geometry(profile_key, thread_id);

        CREATE INDEX IF NOT EXISTS idx_arrival_events_profile_time
            ON arrival_events(profile_key, arrived_at);

        CREATE INDEX IF NOT EXISTS idx_arrival_events_profile_vehicle_time
            ON arrival_events(profile_key, vehicle_id, arrived_at);

        CREATE INDEX IF NOT EXISTS idx_prediction_events_profile_time
            ON prediction_events(profile_key, sampled_at);

        CREATE INDEX IF NOT EXISTS idx_prediction_events_window_profile_time
            ON prediction_events(report_window_key, profile_key, sampled_at);

        CREATE INDEX IF NOT EXISTS idx_prediction_events_profile_vehicle_time
            ON prediction_events(profile_key, vehicle_id, sampled_at);

        CREATE INDEX IF NOT EXISTS idx_prediction_evaluations_profile_source
            ON prediction_evaluations(profile_key, source, bucket);

        """
    )
    _apply_schema_migrations(connection)
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_prediction_events_runtime_source
            ON prediction_events(runtime_source, report_window_key, profile_key, sampled_at)
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_yandex_forecast_samples_traffic_status
            ON yandex_forecast_samples(report_window_key, profile_key, traffic_status, sampled_at)
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_yandex_canary_runs_profile_time
            ON yandex_canary_runs(profile_key, checked_at)
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_bot_interaction_events_time
            ON bot_interaction_events(received_at, status)
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_bot_interaction_events_profile_time
            ON bot_interaction_events(profile_key, received_at)
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_bot_interaction_events_event_kind_time
            ON bot_interaction_events(event_kind, profile_key, received_at)
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_vehicle_progress_tracks_profile_updated
            ON vehicle_progress_tracks(profile_key, updated_at)
        """
    )
    connection.commit()
    from route74.storage.forecast_sample_windows import backfill_yandex_forecast_sample_windows
    from route74.storage.forecast_samples import backfill_yandex_forecast_samples
    from route74.storage.eta_quality import sanitize_untrusted_eta
    from route74.storage.report_windows import backfill_report_window_snapshots

    backfill_yandex_forecast_samples(connection)
    backfill_yandex_forecast_sample_windows(connection)
    backfill_report_window_snapshots(connection)
    sanitize_untrusted_eta(connection)


SchemaMigration = tuple[int, str, Callable[[sqlite3.Connection], None]]


def load_schema_migrations(connection: sqlite3.Connection) -> tuple[sqlite3.Row, ...]:
    return tuple(
        connection.execute(
            """
            SELECT version, name, applied_at
            FROM schema_migrations
            ORDER BY version
            """
        ).fetchall()
    )


def _apply_schema_migrations(connection: sqlite3.Connection) -> None:
    applied = {
        int(row["version"])
        for row in connection.execute("SELECT version FROM schema_migrations").fetchall()
    }
    for version, name, migration in _SCHEMA_MIGRATIONS:
        if version in applied:
            continue
        migration(connection)
        connection.execute(
            """
            INSERT INTO schema_migrations(version, name, applied_at)
            VALUES (?, ?, ?)
            """,
            (version, name, datetime.now().isoformat()),
        )


def _create_yandex_canary_table(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS yandex_canary_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            checked_at TEXT NOT NULL,
            status TEXT NOT NULL,
            source_method TEXT NOT NULL,
            profile_key TEXT NOT NULL,
            schema_hash TEXT NOT NULL,
            changed_keys_json TEXT NOT NULL,
            risk_reason TEXT NOT NULL,
            raw_summary_json TEXT NOT NULL
        )
        """
    )


def _create_bot_interaction_table(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS bot_interaction_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            received_at TEXT NOT NULL,
            chat_id_hash TEXT NOT NULL,
            update_type TEXT NOT NULL,
            command TEXT NOT NULL,
            event_kind TEXT NOT NULL DEFAULT 'user_reply',
            profile_key TEXT NOT NULL DEFAULT '',
            reply_source TEXT NOT NULL,
            yandex_source_method TEXT NOT NULL,
            forecast_ms INTEGER NOT NULL,
            render_ms INTEGER NOT NULL,
            send_ms INTEGER NOT NULL,
            total_ms INTEGER NOT NULL,
            status TEXT NOT NULL,
            error TEXT NOT NULL,
            no_eta_reason TEXT NOT NULL DEFAULT ''
        )
        """
    )


def _create_vehicle_progress_tracks_table(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS vehicle_progress_tracks (
            profile_key TEXT NOT NULL,
            vehicle_id TEXT NOT NULL,
            thread_id TEXT NOT NULL,
            progress_meters REAL NOT NULL,
            velocity_mps REAL NOT NULL,
            updated_at TEXT NOT NULL,
            confidence TEXT NOT NULL,
            stalled_seconds INTEGER NOT NULL,
            sample_count INTEGER NOT NULL,
            raw_json TEXT NOT NULL,
            PRIMARY KEY(profile_key, vehicle_id)
        )
        """
    )


def _ensure_yandex_forecast_sample_columns(connection: sqlite3.Connection) -> None:
    existing = {
        str(row["name"])
        for row in connection.execute("PRAGMA table_info(yandex_forecast_samples)").fetchall()
    }
    for name, definition in _YANDEX_FORECAST_SAMPLE_MIGRATIONS:
        if name not in existing:
            connection.execute(f"ALTER TABLE yandex_forecast_samples ADD COLUMN {name} {definition}")


_YANDEX_FORECAST_SAMPLE_MIGRATIONS = (
    ("traffic_provider", "TEXT NOT NULL DEFAULT 'none'"),
    ("traffic_status", "TEXT NOT NULL DEFAULT 'not_collected'"),
    ("traffic_delay_seconds", "INTEGER"),
    ("traffic_jams_level", "INTEGER"),
    ("route_duration_seconds", "INTEGER"),
    ("route_duration_in_traffic_seconds", "INTEGER"),
    ("traffic_distance_meters", "INTEGER"),
    ("traffic_raw_json", "TEXT NOT NULL DEFAULT '{}'"),
)


def _ensure_prediction_event_columns(connection: sqlite3.Connection) -> None:
    existing = {
        str(row["name"])
        for row in connection.execute("PRAGMA table_info(prediction_events)").fetchall()
    }
    if "runtime_source" not in existing:
        connection.execute("ALTER TABLE prediction_events ADD COLUMN runtime_source TEXT NOT NULL DEFAULT ''")
    rows = connection.execute(
        """
        SELECT id, raw_json
        FROM prediction_events
        WHERE runtime_source = ''
        """
    ).fetchall()
    for row in rows:
        runtime_source = _prediction_event_runtime_source(row["raw_json"])
        if runtime_source:
            connection.execute(
                "UPDATE prediction_events SET runtime_source = ? WHERE id = ?",
                (runtime_source, int(row["id"])),
            )


def _prediction_event_runtime_source(raw_json: object) -> str:
    try:
        raw = json.loads(raw_json)
    except (TypeError, json.JSONDecodeError):
        return ""
    if not isinstance(raw, dict):
        return ""
    runtime_source = raw.get("runtime_source")
    if runtime_source == RUNTIME_SOURCE_WEB_APP:
        return RUNTIME_SOURCE_WEB_APP
    return ""


def _ensure_bot_interaction_event_columns(connection: sqlite3.Connection) -> None:
    existing = {
        str(row["name"])
        for row in connection.execute("PRAGMA table_info(bot_interaction_events)").fetchall()
    }
    if "event_kind" not in existing:
        connection.execute(
            f"ALTER TABLE bot_interaction_events ADD COLUMN event_kind TEXT NOT NULL DEFAULT '{BOT_EVENT_USER_REPLY}'"
        )
    if "profile_key" not in existing:
        connection.execute("ALTER TABLE bot_interaction_events ADD COLUMN profile_key TEXT NOT NULL DEFAULT ''")
    if "no_eta_reason" not in existing:
        connection.execute("ALTER TABLE bot_interaction_events ADD COLUMN no_eta_reason TEXT NOT NULL DEFAULT ''")
    connection.execute(
        "UPDATE bot_interaction_events SET event_kind = ? WHERE event_kind = ''",
        (BOT_EVENT_USER_REPLY,),
    )


_SCHEMA_MIGRATIONS: tuple[SchemaMigration, ...] = (
    (1, "ensure_yandex_forecast_sample_traffic_columns", _ensure_yandex_forecast_sample_columns),
    (2, "ensure_prediction_event_runtime_source", _ensure_prediction_event_columns),
    (3, "create_yandex_canary_runs", _create_yandex_canary_table),
    (4, "create_bot_interaction_events", _create_bot_interaction_table),
    (5, "create_vehicle_progress_tracks", _create_vehicle_progress_tracks_table),
    (6, "ensure_bot_interaction_profile_key", _ensure_bot_interaction_event_columns),
    (7, "ensure_bot_interaction_no_eta_reason", _ensure_bot_interaction_event_columns),
    (8, "ensure_bot_interaction_event_kind", _ensure_bot_interaction_event_columns),
)


def _configure_connection(connection: sqlite3.Connection, *, readonly: bool) -> sqlite3.Connection:
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")
    if readonly:
        connection.execute("PRAGMA query_only = ON")
    else:
        connection.execute("PRAGMA journal_mode = WAL")
    return connection
