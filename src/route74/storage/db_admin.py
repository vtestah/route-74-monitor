from __future__ import annotations

import os
import sqlite3
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from tempfile import NamedTemporaryFile

from route74.storage.connection import DEFAULT_DB, connect, connect_readonly
from route74.storage.helpers import count_table_rows
from route74.storage.models import CountByKey


BACKUP_KEEP_COUNT = 14
DB_TABLES = (
    "yandex_snapshots",
    "yandex_vehicle_observations",
    "yandex_forecast_samples",
    "report_window_snapshots",
    "route_geometry",
    "arrival_events",
    "prediction_events",
    "prediction_evaluations",
    "yandex_canary_runs",
    "bot_interaction_events",
    "vehicle_progress_tracks",
    "schema_migrations",
    "collector_runs",
    "collector_heartbeat",
    "bot_update_offsets",
)


@dataclass(frozen=True)
class DbLatestTimestamp:
    key: str
    value: str | None


@dataclass(frozen=True)
class DbHealthSummary:
    db_path: Path
    db_size_bytes: int
    wal_size_bytes: int
    shm_size_bytes: int
    sqlite_version: str
    journal_mode: str
    busy_timeout_ms: int
    foreign_keys: bool
    integrity_check: str
    quick_check: str
    table_counts: tuple[CountByKey, ...]
    latest_timestamps: tuple[DbLatestTimestamp, ...]

    @property
    def healthy(self) -> bool:
        return self.integrity_check == "ok" and self.quick_check == "ok"


@dataclass(frozen=True)
class DbBackupResult:
    source_path: Path
    output_path: Path
    output_size_bytes: int
    integrity_check: str
    quick_check: str
    pruned_paths: tuple[Path, ...]

    @property
    def healthy(self) -> bool:
        return self.integrity_check == "ok" and self.quick_check == "ok"


def summarize_db_health(db_path: Path = DEFAULT_DB) -> DbHealthSummary:
    with connect(db_path) as connection:
        from route74.storage.connection import init_db

        init_db(connection)
        return _summarize_db_health(connection, db_path)


def summarize_db_health_readonly(db_path: Path = DEFAULT_DB) -> DbHealthSummary:
    with connect_readonly(db_path) as connection:
        return _summarize_db_health(connection, db_path)


def backup_database(
    db_path: Path = DEFAULT_DB,
    *,
    output_path: Path | None = None,
    keep: int = BACKUP_KEEP_COUNT,
    current_time: datetime | None = None,
) -> DbBackupResult:
    if keep < 1:
        raise ValueError("keep must be at least 1")
    if not db_path.exists():
        raise FileNotFoundError(f"SQLite database does not exist: {db_path}")
    output_path = output_path or _default_backup_path(db_path, current_time or datetime.now())
    if output_path.exists():
        raise FileExistsError(f"Backup file already exists: {output_path}")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = _temporary_backup_path(output_path)
    try:
        with connect(db_path) as source:
            with sqlite3.connect(temp_path) as destination:
                source.backup(destination)
        integrity_check, quick_check = _backup_checks(temp_path)
        output_size_bytes = _file_size(temp_path)
        _publish_backup(temp_path, output_path)
    except Exception:
        with suppress(FileNotFoundError):
            temp_path.unlink()
        raise

    pruned = _prune_backups(output_path.parent, keep=keep)
    return DbBackupResult(
        source_path=db_path,
        output_path=output_path,
        output_size_bytes=output_size_bytes,
        integrity_check=integrity_check,
        quick_check=quick_check,
        pruned_paths=pruned,
    )


def _backup_checks(output_path: Path) -> tuple[str, str]:
    with connect_readonly(output_path) as connection:
        return _pragma_text(connection, "integrity_check"), _pragma_text(connection, "quick_check")


def _temporary_backup_path(output_path: Path) -> Path:
    with NamedTemporaryFile(
        prefix=f".{output_path.name}.",
        suffix=".tmp",
        dir=output_path.parent,
        delete=False,
    ) as temporary:
        return Path(temporary.name)


def _publish_backup(temp_path: Path, output_path: Path) -> None:
    try:
        os.link(temp_path, output_path)
    finally:
        with suppress(FileNotFoundError):
            temp_path.unlink()


def _summarize_db_health(connection: sqlite3.Connection, db_path: Path) -> DbHealthSummary:
    return DbHealthSummary(
        db_path=db_path,
        db_size_bytes=_file_size(db_path),
        wal_size_bytes=_file_size(_wal_path(db_path)),
        shm_size_bytes=_file_size(_shm_path(db_path)),
        sqlite_version=sqlite3.sqlite_version,
        journal_mode=_pragma_text(connection, "journal_mode"),
        busy_timeout_ms=_pragma_int(connection, "busy_timeout"),
        foreign_keys=bool(_pragma_int(connection, "foreign_keys")),
        integrity_check=_pragma_text(connection, "integrity_check"),
        quick_check=_pragma_text(connection, "quick_check"),
        table_counts=tuple(CountByKey(table, count_table_rows(connection, table)) for table in DB_TABLES),
        latest_timestamps=_latest_timestamps(connection),
    )


def _default_backup_path(db_path: Path, current_time: datetime) -> Path:
    return db_path.parent / "backups" / f"route74-{current_time:%Y%m%d-%H%M%S}.sqlite"


def _prune_backups(directory: Path, *, keep: int) -> tuple[Path, ...]:
    backups = sorted(directory.glob("route74-*.sqlite"), key=lambda path: path.name, reverse=True)
    pruned: list[Path] = []
    for path in backups[keep:]:
        path.unlink()
        pruned.append(path)
    return tuple(pruned)


def _latest_timestamps(connection: sqlite3.Connection) -> tuple[DbLatestTimestamp, ...]:
    return (
        DbLatestTimestamp("yandex_snapshots", _latest_value(connection, "yandex_snapshots", "sampled_at")),
        DbLatestTimestamp(
            "yandex_forecast_samples",
            _latest_value(connection, "yandex_forecast_samples", "sampled_at"),
        ),
        DbLatestTimestamp(
            "report_window_snapshots",
            _latest_value(connection, "report_window_snapshots", "sampled_at"),
        ),
        DbLatestTimestamp("arrival_events", _latest_value(connection, "arrival_events", "arrived_at")),
        DbLatestTimestamp("prediction_events", _latest_value(connection, "prediction_events", "sampled_at")),
        DbLatestTimestamp("yandex_canary_runs", _latest_value(connection, "yandex_canary_runs", "checked_at")),
        DbLatestTimestamp("bot_interaction_events", _latest_value(connection, "bot_interaction_events", "received_at")),
        DbLatestTimestamp("collector_runs", _latest_value(connection, "collector_runs", "started_at")),
    )


def _latest_value(connection: sqlite3.Connection, table: str, column: str) -> str | None:
    row = connection.execute(f"SELECT MAX({column}) AS value FROM {table}").fetchone()
    value = row["value"]
    return str(value) if value is not None else None


def _pragma_text(connection: sqlite3.Connection, key: str) -> str:
    row = connection.execute(f"PRAGMA {key}").fetchone()
    return str(row[0]) if row is not None else ""


def _pragma_int(connection: sqlite3.Connection, key: str) -> int:
    value = _pragma_text(connection, key)
    return int(value) if value else 0


def _file_size(path: Path) -> int:
    return path.stat().st_size if path.exists() else 0


def _wal_path(db_path: Path) -> Path:
    return db_path.with_name(f"{db_path.name}-wal")


def _shm_path(db_path: Path) -> Path:
    return db_path.with_name(f"{db_path.name}-shm")
