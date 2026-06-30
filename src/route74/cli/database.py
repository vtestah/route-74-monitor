from __future__ import annotations

import argparse
from pathlib import Path

from route74.cli.common import positive_int
from route74.storage import (
    DbBackupResult,
    DbHealthSummary,
    backup_database,
    connect,
    init_db,
    load_schema_migrations,
    summarize_db_health,
)


def register_database_commands(subparsers: argparse._SubParsersAction) -> None:
    health = subparsers.add_parser("db-health", help="Check SQLite database integrity and operational settings.")
    health.set_defaults(func=cmd_db_health)

    backup = subparsers.add_parser("db-backup", help="Create a verified SQLite database backup.")
    backup.add_argument("--output", type=Path, default=None, help="Backup output path.")
    backup.add_argument(
        "--keep",
        type=positive_int,
        default=14,
        help="Number of route74-*.sqlite backups to keep.",
    )
    backup.set_defaults(func=cmd_db_backup)

    migrations = subparsers.add_parser("db-migrations", help="List applied SQLite schema migrations.")
    migrations.set_defaults(func=cmd_db_migrations)


def cmd_db_health(args: argparse.Namespace) -> None:
    print(format_db_health_summary(summarize_db_health(args.db)))


def cmd_db_backup(args: argparse.Namespace) -> None:
    print(format_db_backup_result(backup_database(args.db, output_path=args.output, keep=args.keep)))


def cmd_db_migrations(args: argparse.Namespace) -> None:
    with connect(args.db) as connection:
        init_db(connection)
        rows = load_schema_migrations(connection)
    lines = [f"db migrations count={len(rows)} db={args.db}"]
    lines.extend(f"- {row['version']}: {row['name']} applied_at={row['applied_at']}" for row in rows)
    print("\n".join(lines))


def format_db_health_summary(summary: DbHealthSummary) -> str:
    status = "ok" if summary.healthy else "bad"
    return "\n".join(
        [
            (
                f"db health status={status} db={summary.db_path} size={_size_text(summary.db_size_bytes)} "
                f"wal={_size_text(summary.wal_size_bytes)} shm={_size_text(summary.shm_size_bytes)} "
                f"sqlite={summary.sqlite_version}"
            ),
            (
                f"settings=journal:{summary.journal_mode} busy_timeout:{summary.busy_timeout_ms}ms "
                f"foreign_keys:{int(summary.foreign_keys)}"
            ),
            f"checks=integrity:{summary.integrity_check} quick:{summary.quick_check}",
            f"tables={_counts_text(summary)}",
            f"latest={_latest_text(summary)}",
        ]
    )


def format_db_backup_result(result: DbBackupResult) -> str:
    status = "ok" if result.healthy else "bad"
    pruned = ",".join(str(path) for path in result.pruned_paths) or "-"
    return (
        f"db backup status={status} source={result.source_path} output={result.output_path} "
        f"size={_size_text(result.output_size_bytes)} integrity={result.integrity_check} "
        f"quick={result.quick_check} pruned={pruned}"
    )


def _counts_text(summary: DbHealthSummary) -> str:
    return ",".join(f"{item.key}:{item.count}" for item in summary.table_counts)


def _latest_text(summary: DbHealthSummary) -> str:
    return ",".join(f"{item.key}:{item.value or '-'}" for item in summary.latest_timestamps)


def _size_text(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f"{size_bytes}B"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f}K"
    return f"{size_bytes / 1024 / 1024:.1f}M"
