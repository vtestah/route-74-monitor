from __future__ import annotations

import json
import os
import sqlite3
from collections import Counter
from datetime import datetime, timedelta

from route74.domain.reporting import ReportWindow, matching_report_window
from route74.models import now_local
from route74.storage.helpers import count_rows, optional_int_value
from route74.storage.models import CollectorRunSummary, CollectorWindowRunSummary


def insert_collector_run(
    connection: sqlite3.Connection,
    *,
    name: str,
    started_at: datetime,
    completed_at: datetime,
    profile_filter: str,
    report_windows_only: bool,
    active_profiles: tuple[str, ...],
    status: str,
    message: str,
    result_count: int,
    eta_result_count: int,
    traffic_ok_count: int,
    raw: dict[str, object] | None = None,
) -> int:
    cursor = connection.execute(
        """
        INSERT INTO collector_runs(
            name, started_at, completed_at, pid, profile_filter,
            report_windows_only, active_profiles_json, status, message,
            result_count, eta_result_count, traffic_ok_count, raw_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            name,
            started_at.isoformat(),
            completed_at.isoformat(),
            os.getpid(),
            profile_filter,
            int(report_windows_only),
            json.dumps(list(active_profiles), ensure_ascii=False),
            status,
            message,
            _non_negative_int("result_count", result_count),
            _non_negative_int("eta_result_count", eta_result_count),
            _non_negative_int("traffic_ok_count", traffic_ok_count),
            json.dumps(raw or {}, ensure_ascii=False),
        ),
    )
    return int(cursor.lastrowid)


def summarize_collector_runs(
    connection: sqlite3.Connection,
    *,
    hours: int,
    name: str = "yandex-collect",
    current_time: datetime | None = None,
) -> CollectorRunSummary:
    current_time = current_time or now_local()
    window_hours = _positive_int("hours", hours)
    since = current_time - timedelta(hours=window_hours)
    rows = connection.execute(
        """
        SELECT started_at, status, result_count, eta_result_count, traffic_ok_count
        FROM collector_runs
        WHERE name = ?
          AND started_at >= ?
          AND started_at <= ?
        ORDER BY started_at DESC
        """,
        (name, since.isoformat(), current_time.isoformat()),
    ).fetchall()
    valid_rows = _valid_started_rows(rows, since=since, until=current_time)
    statuses = Counter(str(row["status"]) for row, _started_at in valid_rows)
    return CollectorRunSummary(
        name=name,
        hours=window_hours,
        total_runs=len(valid_rows),
        result_runs=_positive_count(valid_rows, "result_count"),
        eta_runs=_positive_count(valid_rows, "eta_result_count"),
        traffic_ok_runs=_positive_count(valid_rows, "traffic_ok_count"),
        skipped_runs=sum(1 for row, _started_at in valid_rows if str(row["status"]) == "skipped"),
        latest_started_at=valid_rows[0][1] if valid_rows else None,
        statuses=count_rows(statuses),
    )


def summarize_collector_runs_for_report_window(
    connection: sqlite3.Connection,
    *,
    report_window: ReportWindow,
    current_date: datetime,
    days: int,
    name: str = "yandex-collect",
) -> CollectorWindowRunSummary:
    window_days = _positive_int("days", days)
    since = current_date - timedelta(days=window_days)
    rows = connection.execute(
        """
        SELECT
            started_at, profile_filter, active_profiles_json, status,
            result_count, eta_result_count, traffic_ok_count
        FROM collector_runs
        WHERE name = ?
          AND started_at >= ?
          AND started_at <= ?
          AND report_windows_only = 1
        ORDER BY started_at DESC
        """,
        (name, since.isoformat(), current_date.isoformat()),
    ).fetchall()
    window_rows = tuple(
        (row, started_at)
        for row, started_at in _valid_started_rows(rows, since=since, until=current_date)
        if _belongs_to_window(row, report_window, started_at)
    )
    statuses = Counter(str(row["status"]) for row, _started_at in window_rows)
    return CollectorWindowRunSummary(
        window_key=report_window.key,
        profile_key=report_window.profile_key,
        total_runs=len(window_rows),
        result_runs=_positive_count(window_rows, "result_count"),
        eta_runs=_positive_count(window_rows, "eta_result_count"),
        traffic_ok_runs=_positive_count(window_rows, "traffic_ok_count"),
        skipped_runs=sum(1 for row, _started_at in window_rows if str(row["status"]) == "skipped"),
        latest_started_at=window_rows[0][1] if window_rows else None,
        statuses=count_rows(statuses),
    )


def prune_collector_runs(
    connection: sqlite3.Connection,
    *,
    older_than: datetime,
    name: str | None = None,
) -> int:
    if name is None:
        cursor = connection.execute(
            "DELETE FROM collector_runs WHERE started_at < ?",
            (older_than.isoformat(),),
        )
    else:
        cursor = connection.execute(
            "DELETE FROM collector_runs WHERE name = ? AND started_at < ?",
            (name, older_than.isoformat()),
        )
    return cursor.rowcount


def _belongs_to_window(row: sqlite3.Row, report_window: ReportWindow, started_at: datetime) -> bool:
    if matching_report_window(started_at, report_window.profile_key) != report_window:
        return False
    active_profiles = _active_profiles(row["active_profiles_json"])
    if active_profiles:
        return report_window.profile_key in active_profiles
    return collector_profile_filter_includes(str(row["profile_filter"]), report_window.profile_key)


def _active_profiles(raw_json: str) -> tuple[str, ...]:
    try:
        raw = json.loads(raw_json)
    except json.JSONDecodeError:
        return ()
    if not isinstance(raw, list):
        return ()
    return tuple(item for item in raw if isinstance(item, str))


def _valid_started_rows(
    rows: list[sqlite3.Row],
    *,
    since: datetime,
    until: datetime,
) -> tuple[tuple[sqlite3.Row, datetime], ...]:
    valid: list[tuple[sqlite3.Row, datetime]] = []
    for row in rows:
        started_at = _started_at(row)
        if (
            started_at is not None
            and _datetime_at_or_after(started_at, since)
            and _datetime_at_or_before(started_at, until)
            and _has_valid_counts(row)
        ):
            valid.append((row, started_at))
    return tuple(valid)


def _started_at(row: sqlite3.Row) -> datetime | None:
    try:
        return datetime.fromisoformat(str(row["started_at"]))
    except ValueError:
        return None


def _positive_count(rows: tuple[tuple[sqlite3.Row, datetime], ...], column: str) -> int:
    return sum(1 for row, _started_at in rows if (optional_int_value(row[column]) or 0) > 0)


def _has_valid_counts(row: sqlite3.Row) -> bool:
    return all(
        _optional_non_negative_int(row[column]) is not None
        for column in ("result_count", "eta_result_count", "traffic_ok_count")
    )


def _optional_non_negative_int(value: object) -> int | None:
    parsed = optional_int_value(value)
    if parsed is None or parsed < 0:
        return None
    return parsed


def _non_negative_int(name: str, value: int) -> int:
    parsed = _optional_non_negative_int(value)
    if parsed is None:
        raise ValueError(f"{name} must be a non-negative integer")
    return parsed


def _positive_int(name: str, value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _datetime_at_or_after(value: datetime, boundary: datetime) -> bool:
    try:
        return value >= boundary
    except TypeError:
        return False


def _datetime_at_or_before(value: datetime, boundary: datetime) -> bool:
    try:
        return value <= boundary
    except TypeError:
        return False


def collector_profile_filter_includes(profile_filter: str, profile_key: str) -> bool:
    if profile_filter == "all":
        return True
    filters = {item.strip() for item in profile_filter.split(",") if item.strip()}
    return profile_key in filters
