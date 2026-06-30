from __future__ import annotations

import json
import sqlite3
from collections import Counter
from datetime import datetime, timedelta

from route74.domain.profiles import PROFILE_KEYS
from route74.domain.reporting import (
    REPORT_WINDOWS,
    REPORT_WINDOWS_BY_KEY,
    ReportWindow,
    validate_report_datetime,
)
from route74.models import now_local
from route74.sources.yandex.models import YandexLiveForecast
from route74.sources.yandex.trust import is_trusted_eta_observation, trusted_arrivals_for_forecast
from route74.storage.helpers import arrival_minutes_from_json, count_rows, count_table_rows, optional_int_value
from route74.storage.models import ReportWindowSummary, RouteTrafficSnapshot


def insert_report_window_snapshot(
    connection: sqlite3.Connection,
    *,
    yandex_snapshot_id: int,
    profile_key: str,
    forecast: YandexLiveForecast,
    sampled_at: datetime,
    report_window: ReportWindow,
    traffic: RouteTrafficSnapshot | None = None,
) -> int:
    arrivals = trusted_arrivals_for_forecast(forecast)
    traffic_raw = traffic.raw if traffic is not None and traffic.raw is not None else {}
    traffic_provider = traffic.provider if traffic is not None else "none"
    traffic_status = traffic.status if traffic is not None else "not_collected"
    raw = {
        "forecast": {
            "enabled": forecast.enabled,
            "available": forecast.available,
            "source_method": forecast.source_method.value,
            "status": forecast.status.value,
            "arrival_minutes": list(arrivals),
            "vehicle_count": forecast.vehicle_count,
            "fallback_reason": forecast.fallback_reason,
            "raw_status": forecast.raw_status,
        },
        "traffic": traffic_raw,
    }
    cursor = connection.execute(
        """
        INSERT INTO report_window_snapshots(
            yandex_snapshot_id, sampled_at, service_date, weekday,
            report_window_key, profile_key, window_start, window_end,
            source_method, source_status, available, vehicle_count,
            arrival_minutes_json, traffic_provider, traffic_status,
            traffic_jams_level, route_duration_seconds,
            route_duration_in_traffic_seconds, traffic_delay_seconds,
            traffic_distance_meters, traffic_raw_json, raw_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            yandex_snapshot_id,
            sampled_at.isoformat(),
            sampled_at.date().isoformat(),
            sampled_at.weekday(),
            report_window.key,
            profile_key,
            report_window.start.isoformat(timespec="minutes"),
            report_window.end.isoformat(timespec="minutes"),
            forecast.source_method.value,
            forecast.status.value,
            int(forecast.available),
            forecast.vehicle_count,
            json.dumps(list(arrivals), ensure_ascii=False),
            traffic_provider,
            traffic_status,
            traffic.jams_level if traffic is not None else None,
            traffic.route_duration_seconds if traffic is not None else None,
            traffic.route_duration_in_traffic_seconds if traffic is not None else None,
            traffic.delay_seconds if traffic is not None else None,
            traffic.distance_meters if traffic is not None else None,
            json.dumps(traffic_raw, ensure_ascii=False),
            json.dumps(raw, ensure_ascii=False),
        ),
    )
    connection.commit()
    return int(cursor.lastrowid)


def count_report_window_snapshots(connection: sqlite3.Connection) -> int:
    return count_table_rows(connection, "report_window_snapshots")


def backfill_report_window_snapshots(connection: sqlite3.Connection) -> int:
    windows = {window.key: window for window in REPORT_WINDOWS}
    rows = connection.execute(
        """
        SELECT
            forecast.*,
            report.id AS report_id
        FROM yandex_forecast_samples AS forecast
        LEFT JOIN report_window_snapshots AS report
          ON report.yandex_snapshot_id = forecast.yandex_snapshot_id
        WHERE forecast.report_window_key != ''
          AND (
              report.id IS NULL
              OR report.report_window_key != forecast.report_window_key
              OR report.profile_key != forecast.profile_key
          )
        ORDER BY forecast.id
        """
    ).fetchall()
    changed = 0
    for row in rows:
        window = windows.get(str(row["report_window_key"]))
        if window is None:
            continue
        sampled_at = _optional_datetime(row["sampled_at"])
        if sampled_at is None:
            continue
        if row["report_id"] is not None:
            connection.execute(
                "DELETE FROM report_window_snapshots WHERE id = ?",
                (int(row["report_id"]),),
            )
        _insert_report_window_row(connection, row, window, sampled_at=sampled_at)
        changed += 1
    if changed:
        connection.commit()
    return changed


def summarize_report_windows(
    connection: sqlite3.Connection,
    *,
    days: int,
    report_window_key: str | None = None,
    profile_key: str | None = None,
    current_time: datetime | None = None,
) -> ReportWindowSummary:
    _validate_report_window_summary_inputs(
        days=days,
        report_window_key=report_window_key,
        profile_key=profile_key,
    )
    current_time = current_time or now_local()
    validate_report_datetime("report window current_time", current_time)
    since = current_time - timedelta(days=days)
    filters = ["sampled_at >= ?"]
    params: list[str] = [since.isoformat()]
    if report_window_key is not None:
        filters.append("report_window_key = ?")
        params.append(report_window_key)
    if profile_key is not None:
        filters.append("profile_key = ?")
        params.append(profile_key)
    where = "WHERE " + " AND ".join(filters)
    rows = connection.execute(
        f"""
        SELECT sampled_at, source_method, source_status, arrival_minutes_json, traffic_status, raw_json
        FROM report_window_snapshots
        {where}
        ORDER BY sampled_at DESC
        """,
        tuple(params),
    ).fetchall()
    status_counts = Counter(str(row["source_status"]) for row in rows)
    return ReportWindowSummary(
        days=days,
        report_window_key=report_window_key,
        profile_key=profile_key,
        total_samples=len(rows),
        eta_samples=sum(
            1
            for row in rows
            if _report_window_has_trusted_eta(row) and arrival_minutes_from_json(row["arrival_minutes_json"])
        ),
        traffic_samples=sum(1 for row in rows if str(row["traffic_status"]) == "ok"),
        latest_sampled_at=_latest_sampled_at(tuple(row["sampled_at"] for row in rows)),
        statuses=count_rows(status_counts),
    )


def _validate_report_window_summary_inputs(
    *,
    days: int,
    report_window_key: str | None,
    profile_key: str | None,
) -> None:
    _positive_int("days", days)
    if report_window_key is not None:
        if not isinstance(report_window_key, str) or not report_window_key.strip():
            raise ValueError("report_window_key must be a non-empty string or None")
        if report_window_key not in REPORT_WINDOWS_BY_KEY:
            expected = ", ".join(REPORT_WINDOWS_BY_KEY)
            raise ValueError(f"unknown report_window_key: {report_window_key} (expected {expected})")
    if profile_key is not None:
        if not isinstance(profile_key, str) or not profile_key.strip():
            raise ValueError("profile_key must be a non-empty string or None")
        if profile_key not in PROFILE_KEYS:
            expected = ", ".join(PROFILE_KEYS)
            raise ValueError(f"profile_key must be one of {expected}")


def _positive_int(name: str, value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")


def _insert_report_window_row(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    window: ReportWindow,
    *,
    sampled_at: datetime,
) -> int:
    arrivals = _arrival_minutes(row)
    sample_raw = _json_object(str(row["raw_json"]))
    forecast_raw = sample_raw.get("forecast", sample_raw)
    raw = {
        "forecast": forecast_raw if isinstance(forecast_raw, dict) else {},
        "traffic": _json_object(str(row["traffic_raw_json"])),
        "backfilled": True,
    }
    cursor = connection.execute(
        """
        INSERT INTO report_window_snapshots(
            yandex_snapshot_id, sampled_at, service_date, weekday,
            report_window_key, profile_key, window_start, window_end,
            source_method, source_status, available, vehicle_count,
            arrival_minutes_json, traffic_provider, traffic_status,
            traffic_jams_level, route_duration_seconds,
            route_duration_in_traffic_seconds, traffic_delay_seconds,
            traffic_distance_meters, traffic_raw_json, raw_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            int(row["yandex_snapshot_id"]),
            sampled_at.isoformat(),
            str(row["service_date"]),
            int(row["weekday"]),
            window.key,
            str(row["profile_key"]),
            window.start.isoformat(timespec="minutes"),
            window.end.isoformat(timespec="minutes"),
            str(row["source_method"]),
            str(row["source_status"]),
            int(row["available"]),
            int(row["vehicle_count"]),
            json.dumps(arrivals, ensure_ascii=False),
            str(row["traffic_provider"]),
            str(row["traffic_status"]),
            row["traffic_jams_level"],
            row["route_duration_seconds"],
            row["route_duration_in_traffic_seconds"],
            row["traffic_delay_seconds"],
            row["traffic_distance_meters"],
            str(row["traffic_raw_json"]),
            json.dumps(raw, ensure_ascii=False),
        ),
    )
    return int(cursor.lastrowid)


def _arrival_minutes(row: sqlite3.Row) -> tuple[int, ...]:
    raw = _json_object(str(row["raw_json"]))
    forecast = raw.get("forecast", raw)
    forecast_raw = forecast if isinstance(forecast, dict) else {}
    if not is_trusted_eta_observation(
        row["source_method"],
        fallback_reason=row["fallback_reason"],
        raw_status=forecast_raw.get("raw_status", ""),
    ):
        return ()
    first_value = optional_int_value(row["arrival_minutes"])
    first = () if first_value is None or first_value < 0 else (first_value,)
    return first + arrival_minutes_from_json(str(row["next_arrival_minutes_json"]))


def _report_window_has_trusted_eta(row: sqlite3.Row) -> bool:
    raw = _json_object(str(row["raw_json"]))
    forecast = raw.get("forecast", raw)
    forecast_raw = forecast if isinstance(forecast, dict) else {}
    return is_trusted_eta_observation(
        row["source_method"],
        fallback_reason=forecast_raw.get("fallback_reason", ""),
        raw_status=forecast_raw.get("raw_status", ""),
    )


def _latest_sampled_at(values: tuple[object, ...]) -> datetime | None:
    for value in values:
        sampled_at = _optional_datetime(value)
        if sampled_at is not None:
            return sampled_at
    return None


def _optional_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _json_object(raw_json: str) -> dict[str, object]:
    try:
        raw = json.loads(raw_json)
    except json.JSONDecodeError:
        return {}
    if isinstance(raw, dict):
        return raw
    return {}
