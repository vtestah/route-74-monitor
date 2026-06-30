from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

import route74.storage.db_admin as db_admin_module
from route74.domain.profiles import MORNING
from route74.models import NOVOSIBIRSK_TZ
from route74.reporting_smoke_fixtures import FakeYandexSource
from route74.storage import (
    backup_database,
    connect,
    connect_readonly,
    count_yandex_snapshots,
    init_db,
    insert_yandex_snapshot,
    summarize_db_health,
)


def main() -> None:
    sampled_at = datetime(2026, 6, 4, 9, 15, tzinfo=NOVOSIBIRSK_TZ)
    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "route74.sqlite"
        with connect(db_path) as connection:
            init_db(connection)
            _assert_foreign_keys(connection)
            insert_yandex_snapshot(connection, MORNING.key, FakeYandexSource().get_forecast(), sampled_at)

        health = summarize_db_health(db_path)
        _assert_equal(health.healthy, True)
        _assert_equal(health.journal_mode, "wal")
        _assert_equal(health.busy_timeout_ms, 30_000)
        _assert_equal(health.foreign_keys, True)
        _assert_equal(_count_for(health, "yandex_snapshots"), 1)

        with connect_readonly(db_path) as connection:
            _assert_equal(count_yandex_snapshots(connection), 1)
            _assert_readonly(connection)

        try:
            connect_readonly(Path(temp_dir) / "missing.sqlite")
        except FileNotFoundError:
            pass
        else:
            raise AssertionError("expected missing read-only DB to fail")

        first = backup_database(db_path, keep=2, current_time=sampled_at)
        second = backup_database(db_path, keep=2, current_time=sampled_at + timedelta(seconds=1))
        third = backup_database(db_path, keep=2, current_time=sampled_at + timedelta(seconds=2))
        _assert_equal(first.output_path.exists(), False)
        _assert_equal(second.output_path.exists(), True)
        _assert_equal(third.output_path.exists(), True)
        _assert_equal(third.healthy, True)
        _assert_equal(len(third.pruned_paths), 1)
        _assert_equal(_temporary_backups(third.output_path.parent), ())
        with connect_readonly(third.output_path) as connection:
            _assert_equal(count_yandex_snapshots(connection), 1)
        _assert_backup_publish_failure_cleans_temp(db_path, third.output_path.parent)

    print("OK | db admin smoke passed")


def _assert_foreign_keys(connection: sqlite3.Connection) -> None:
    try:
        connection.execute(
            """
            INSERT INTO yandex_vehicle_observations(
                snapshot_id, profile_key, source_method, source_status,
                vehicle_id, thread_id, raw_json
            )
            VALUES (999, 'morning', 'fake', 'ok', 'vehicle', 'thread', '{}')
            """
        )
    except sqlite3.IntegrityError:
        connection.rollback()
        return
    raise AssertionError("expected foreign key violation")


def _assert_readonly(connection: sqlite3.Connection) -> None:
    try:
        connection.execute("CREATE TABLE should_not_write(id INTEGER)")
    except sqlite3.OperationalError:
        return
    raise AssertionError("expected read-only connection to reject writes")


def _assert_backup_publish_failure_cleans_temp(db_path: Path, backups_dir: Path) -> None:
    output_path = backups_dir / "route74-race.sqlite"
    original_link = db_admin_module.os.link

    def fail_link(source: object, destination: object) -> None:
        raise FileExistsError("simulated publish race")

    db_admin_module.os.link = fail_link
    try:
        try:
            backup_database(db_path, output_path=output_path)
        except FileExistsError:
            pass
        else:
            raise AssertionError("expected publish race to fail")
    finally:
        db_admin_module.os.link = original_link

    _assert_equal(output_path.exists(), False)
    _assert_equal(_temporary_backups(backups_dir), ())


def _temporary_backups(backups_dir: Path) -> tuple[Path, ...]:
    return tuple(backups_dir.glob(".route74-*.sqlite.*.tmp"))


def _count_for(health: object, key: str) -> int:
    for item in health.table_counts:
        if item.key == key:
            return item.count
    raise AssertionError(f"missing table count: {key}")


def _assert_equal(actual: object, expected: object) -> None:
    if actual != expected:
        raise AssertionError(f"expected {expected!r}, got {actual!r}")


if __name__ == "__main__":
    main()
