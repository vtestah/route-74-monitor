from __future__ import annotations

import json
import sqlite3
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timedelta
from math import isfinite

from route74.domain.reporting import matching_report_window
from route74.models import now_local
from route74.sources.yandex.freshness import effective_forecast_age_seconds
from route74.sources.yandex.models import YandexLiveForecast
from route74.sources.yandex.trust import (
    is_trusted_eta_observation,
    trusted_arrivals_for_forecast,
)
from route74.storage.collector_runs import summarize_collector_runs
from route74.storage.forecast_samples import insert_yandex_forecast_sample
from route74.storage.heartbeat import load_collector_heartbeat
from route74.storage.helpers import (
    arrival_minutes_from_json,
    count_rows,
    count_table_rows,
    optional_int_value,
)
from route74.storage.models import (
    RouteTrafficSnapshot,
    YandexObservation,
    YandexTelemetrySummary,
)
from route74.storage.report_windows import insert_report_window_snapshot

SQLITE_IN_CHUNK_SIZE = 500


def insert_yandex_snapshot(
    connection: sqlite3.Connection,
    profile_key: str,
    forecast: YandexLiveForecast,
    sampled_at: datetime | None = None,
    traffic: RouteTrafficSnapshot | None = None,
    route_geometry_status: str = "",
    route_geometry_reason: str = "",
) -> int:
    sampled_at = sampled_at or now_local()
    newest_age_seconds = effective_forecast_age_seconds(forecast)
    raw = {
        "enabled": forecast.enabled,
        "available": forecast.available,
        "source_method": forecast.source_method.value,
        "status": forecast.status.value,
        "arrival_minutes": list(forecast.arrival_minutes),
        "vehicle_count": forecast.vehicle_count,
        "newest_age_seconds": newest_age_seconds,
        "confidence": forecast.confidence.value,
        "fallback_reason": forecast.fallback_reason,
        "raw_status": forecast.raw_status,
        "diagnostics": list(forecast.diagnostics),
        "route_geometry_status": route_geometry_status,
        "route_geometry_reason": route_geometry_reason,
    }
    trusted_arrivals = trusted_arrivals_for_forecast(forecast)
    cursor = connection.execute(
        """
        INSERT INTO yandex_snapshots(
            sampled_at, profile_key, source_method, source_status, available,
            vehicle_count, arrival_minutes_json, fallback_reason, raw_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            sampled_at.isoformat(),
            profile_key,
            forecast.source_method.value,
            forecast.status.value,
            int(forecast.available),
            forecast.vehicle_count,
            json.dumps(list(trusted_arrivals), ensure_ascii=False),
            forecast.fallback_reason,
            json.dumps(raw, ensure_ascii=False),
        ),
    )
    snapshot_id = int(cursor.lastrowid)
    for vehicle in forecast.vehicles:
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
                profile_key,
                forecast.source_method.value,
                forecast.status.value,
                vehicle.vehicle_id,
                vehicle.thread_id,
                vehicle.lat,
                vehicle.lng,
                vehicle.arrival_minutes if trusted_arrivals else None,
                vehicle.age_seconds,
                json.dumps(asdict(vehicle), ensure_ascii=False),
            ),
        )
    report_window = matching_report_window(sampled_at, profile_key)
    insert_yandex_forecast_sample(
        connection,
        yandex_snapshot_id=snapshot_id,
        profile_key=profile_key,
        forecast=forecast,
        sampled_at=sampled_at,
        report_window=report_window,
        traffic=traffic,
        route_geometry_status=route_geometry_status,
        route_geometry_reason=route_geometry_reason,
    )
    if report_window is not None:
        insert_report_window_snapshot(
            connection,
            yandex_snapshot_id=snapshot_id,
            profile_key=profile_key,
            forecast=forecast,
            sampled_at=sampled_at,
            report_window=report_window,
            traffic=traffic,
        )
    from route74.storage.prediction_lab import (
        process_yandex_snapshot_for_prediction_lab,
    )

    process_yandex_snapshot_for_prediction_lab(
        connection,
        yandex_snapshot_id=snapshot_id,
        profile_key=profile_key,
        forecast=forecast,
        sampled_at=sampled_at,
        traffic=traffic,
    )
    connection.commit()
    return snapshot_id


def load_yandex_observations(connection: sqlite3.Connection) -> list[YandexObservation]:
    rows = connection.execute(
        """
        SELECT
            yandex_vehicle_observations.profile_key,
            yandex_vehicle_observations.source_method,
            yandex_vehicle_observations.source_status,
            vehicle_id,
            thread_id,
            lat,
            lng,
            arrival_minutes,
            age_seconds,
            yandex_snapshots.sampled_at
        FROM yandex_vehicle_observations
        JOIN yandex_snapshots ON yandex_snapshots.id = yandex_vehicle_observations.snapshot_id
        ORDER BY yandex_snapshots.sampled_at, vehicle_id
        """
    ).fetchall()
    observations: list[YandexObservation] = []
    for row in rows:
        observation = _yandex_observation_from_row(row)
        if observation is not None:
            observations.append(observation)
    return observations


def count_yandex_snapshots(connection: sqlite3.Connection) -> int:
    return count_table_rows(connection, "yandex_snapshots")


def count_yandex_observations(connection: sqlite3.Connection) -> int:
    return count_table_rows(connection, "yandex_vehicle_observations")


def latest_yandex_snapshot_sampled_at(
    connection: sqlite3.Connection,
) -> datetime | None:
    rows = connection.execute(
        """
        SELECT sampled_at
        FROM yandex_snapshots
        ORDER BY sampled_at DESC
        """
    ).fetchall()
    for row in rows:
        sampled_at = _datetime_value(row["sampled_at"])
        if sampled_at is not None:
            return sampled_at
    return None


def summarize_yandex_telemetry(
    connection: sqlite3.Connection,
    *,
    hours: int,
    profile_key: str | None = None,
    heartbeat_name: str = "yandex-collect",
    current_time: datetime | None = None,
) -> YandexTelemetrySummary:
    current_time = current_time or now_local()
    if not isinstance(current_time, datetime):
        raise ValueError("current_time must be a datetime")
    window_hours = _positive_int("hours", hours)
    since = current_time - timedelta(hours=window_hours)
    where, params = _snapshot_filter(since, profile_key)
    raw_rows = tuple(
        connection.execute(
            f"""
            SELECT id, source_method, source_status, available, vehicle_count, arrival_minutes_json, sampled_at, fallback_reason, raw_json
            FROM yandex_snapshots
            {where}
            ORDER BY sampled_at DESC
            """,
            params,
        ).fetchall()
    )
    rows = _valid_snapshot_rows(raw_rows, since=since, until=current_time)
    snapshot_ids = tuple(int(row["id"]) for row in rows)
    observation_count = _count_observations_for_snapshots(
        connection,
        snapshot_ids,
    )
    eta_observation_count = _count_observations_for_snapshots(
        connection,
        snapshot_ids,
        extra=(
            "yandex_vehicle_observations.arrival_minutes IS NOT NULL "
            "AND yandex_vehicle_observations.source_method IN ('vehicle_prediction','stop_info') "
            "AND yandex_vehicle_observations.snapshot_id NOT IN ("
            "SELECT id FROM yandex_snapshots WHERE fallback_reason LIKE 'vehicle_prediction_thread_fallback:%'"
            ")"
        ),
    )
    status_counts = Counter(str(row["source_status"]) for row in rows)
    method_counts = Counter(str(row["source_method"]) for row in rows)
    return YandexTelemetrySummary(
        profile_key=profile_key,
        hours=window_hours,
        total_snapshots=len(rows),
        eta_snapshots=sum(
            1
            for row in rows
            if _snapshot_has_trusted_eta(row) and arrival_minutes_from_json(row["arrival_minutes_json"])
        ),
        vehicle_snapshots=sum(1 for row in rows if _is_positive_int_value(row["vehicle_count"])),
        total_observations=observation_count,
        eta_observations=eta_observation_count,
        latest_sampled_at=_datetime_value(rows[0]["sampled_at"]) if rows else None,
        heartbeat=load_collector_heartbeat(connection, heartbeat_name),
        collector_runs=summarize_collector_runs(
            connection,
            hours=window_hours,
            name=heartbeat_name,
            current_time=current_time,
        ),
        statuses=count_rows(status_counts),
        methods=count_rows(method_counts),
    )


def prune_yandex_telemetry(
    connection: sqlite3.Connection,
    *,
    older_than: datetime,
) -> int:
    snapshot_ids = [
        int(row["id"])
        for row in connection.execute(
            "SELECT id FROM yandex_snapshots WHERE sampled_at < ?",
            (older_than.isoformat(),),
        ).fetchall()
    ]
    if not snapshot_ids:
        return 0
    placeholders = ",".join("?" for _ in snapshot_ids)
    prediction_ids = [
        int(row["id"])
        for row in connection.execute(
            f"SELECT id FROM prediction_events WHERE yandex_snapshot_id IN ({placeholders})",
            snapshot_ids,
        ).fetchall()
    ]
    arrival_ids = [
        int(row["id"])
        for row in connection.execute(
            f"SELECT id FROM arrival_events WHERE yandex_snapshot_id IN ({placeholders})",
            snapshot_ids,
        ).fetchall()
    ]
    if prediction_ids:
        prediction_placeholders = ",".join("?" for _ in prediction_ids)
        connection.execute(
            f"DELETE FROM prediction_evaluations WHERE prediction_event_id IN ({prediction_placeholders})",
            prediction_ids,
        )
    if arrival_ids:
        arrival_placeholders = ",".join("?" for _ in arrival_ids)
        connection.execute(
            f"DELETE FROM prediction_evaluations WHERE arrival_event_id IN ({arrival_placeholders})",
            arrival_ids,
        )
    connection.execute(
        f"DELETE FROM prediction_events WHERE yandex_snapshot_id IN ({placeholders})",
        snapshot_ids,
    )
    connection.execute(
        f"DELETE FROM arrival_events WHERE yandex_snapshot_id IN ({placeholders})",
        snapshot_ids,
    )
    connection.execute(
        f"DELETE FROM report_window_snapshots WHERE yandex_snapshot_id IN ({placeholders})",
        snapshot_ids,
    )
    connection.execute(
        f"DELETE FROM yandex_forecast_samples WHERE yandex_snapshot_id IN ({placeholders})",
        snapshot_ids,
    )
    connection.execute(
        f"DELETE FROM yandex_vehicle_observations WHERE snapshot_id IN ({placeholders})",
        snapshot_ids,
    )
    connection.execute(f"DELETE FROM yandex_snapshots WHERE id IN ({placeholders})", snapshot_ids)
    connection.commit()
    return len(snapshot_ids)


def _yandex_observation_from_row(row: sqlite3.Row) -> YandexObservation | None:
    sampled_at = _datetime_value(row["sampled_at"])
    if sampled_at is None:
        return None
    try:
        return YandexObservation(
            profile_key=row["profile_key"],
            source_method=row["source_method"],
            source_status=row["source_status"],
            vehicle_id=row["vehicle_id"],
            thread_id=row["thread_id"],
            lat=_optional_observation_float(row["lat"]),
            lng=_optional_observation_float(row["lng"]),
            arrival_minutes=_optional_observation_int(row["arrival_minutes"]),
            age_seconds=_optional_observation_int(row["age_seconds"]),
            sampled_at=sampled_at,
        )
    except (TypeError, ValueError, OverflowError):
        return None


def _snapshot_filter(since: datetime, profile_key: str | None) -> tuple[str, tuple[str, ...]]:
    if profile_key is None:
        return "WHERE sampled_at >= ?", (since.isoformat(),)
    return "WHERE sampled_at >= ? AND profile_key = ?", (since.isoformat(), profile_key)


def _valid_snapshot_rows(
    rows: tuple[sqlite3.Row, ...],
    *,
    since: datetime,
    until: datetime,
) -> tuple[sqlite3.Row, ...]:
    valid: list[sqlite3.Row] = []
    for row in rows:
        sampled_at = _datetime_value(row["sampled_at"])
        if sampled_at is not None and _datetime_in_range(sampled_at, since=since, until=until):
            valid.append(row)
    return tuple(valid)


def _count_observations_for_snapshots(
    connection: sqlite3.Connection,
    snapshot_ids: tuple[int, ...],
    *,
    extra: str = "",
) -> int:
    if not snapshot_ids:
        return 0
    extra_filter = f" AND {extra}" if extra else ""
    total = 0
    for chunk in _chunks(snapshot_ids, SQLITE_IN_CHUNK_SIZE):
        placeholders = ",".join("?" for _ in chunk)
        row = connection.execute(
            f"""
            SELECT COUNT(*) AS count
            FROM yandex_vehicle_observations
            JOIN yandex_snapshots ON yandex_snapshots.id = yandex_vehicle_observations.snapshot_id
            WHERE yandex_vehicle_observations.snapshot_id IN ({placeholders}){extra_filter}
            """,
            chunk,
        ).fetchone()
        total += int(row["count"])
    return total


def _datetime_value(value: object) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _optional_observation_int(value: object) -> int | None:
    if value is None:
        return None
    parsed = optional_int_value(value)
    if parsed is None or parsed < 0:
        raise ValueError("Yandex observation integer field is invalid")
    return parsed


def _optional_observation_float(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("Yandex observation coordinate field is invalid")
    parsed = float(value)
    if not isfinite(parsed):
        raise ValueError("Yandex observation coordinate field is invalid")
    return parsed


def _datetime_in_range(value: datetime, *, since: datetime, until: datetime) -> bool:
    try:
        return since <= value <= until
    except TypeError:
        return False


def _positive_int(name: str, value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _is_positive_int_value(value: object) -> bool:
    parsed = optional_int_value(value)
    return parsed is not None and parsed > 0


def _chunks(values: tuple[int, ...], size: int) -> tuple[tuple[int, ...], ...]:
    return tuple(values[index : index + size] for index in range(0, len(values), size))


def _snapshot_has_trusted_eta(row: sqlite3.Row) -> bool:
    raw = _json_object(str(row["raw_json"]))
    return is_trusted_eta_observation(
        row["source_method"],
        fallback_reason=row["fallback_reason"],
        raw_status=raw.get("raw_status", ""),
    )


def _json_object(raw_json: str) -> dict[str, object]:
    try:
        raw = json.loads(raw_json)
    except json.JSONDecodeError:
        return {}
    if isinstance(raw, dict):
        return raw
    return {}
