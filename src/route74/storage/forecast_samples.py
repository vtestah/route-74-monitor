from __future__ import annotations

import json
import sqlite3
from datetime import datetime

from route74.domain.reporting import ReportWindow, matching_report_window
from route74.sources.yandex.freshness import effective_forecast_age_seconds
from route74.sources.yandex.models import YandexLiveForecast
from route74.sources.yandex.trust import is_trusted_eta_observation
from route74.storage.helpers import (
    arrival_minutes_from_json,
    count_table_rows,
    optional_int_value,
)
from route74.storage.models import RouteTrafficSnapshot


def insert_yandex_forecast_sample(
    connection: sqlite3.Connection,
    *,
    yandex_snapshot_id: int,
    profile_key: str,
    forecast: YandexLiveForecast,
    sampled_at: datetime,
    report_window: ReportWindow | None = None,
    traffic: RouteTrafficSnapshot | None = None,
    route_geometry_status: str = "",
    route_geometry_reason: str = "",
) -> int:
    newest_age_seconds = effective_forecast_age_seconds(forecast)
    values = _sample_values(
        yandex_snapshot_id=yandex_snapshot_id,
        profile_key=profile_key,
        sampled_at=sampled_at,
        source_method=forecast.source_method.value,
        source_status=forecast.status.value,
        available=forecast.available,
        arrival_minutes=forecast.arrival_minutes,
        vehicle_count=forecast.vehicle_count,
        newest_age_seconds=newest_age_seconds,
        confidence=forecast.confidence.value,
        fallback_reason=forecast.fallback_reason,
        raw_status=forecast.raw_status,
        report_window_key=report_window.key if report_window is not None else "",
        traffic=traffic,
        raw={
            "forecast": _forecast_raw(forecast, newest_age_seconds=newest_age_seconds),
            "traffic": _traffic_raw(traffic),
            "route_geometry_status": route_geometry_status,
            "route_geometry_reason": route_geometry_reason,
        },
    )
    cursor = connection.execute(_INSERT_SQL, values)
    return int(cursor.lastrowid)


def backfill_yandex_forecast_samples(connection: sqlite3.Connection) -> int:
    rows = connection.execute(
        """
        SELECT yandex_snapshots.*
        FROM yandex_snapshots
        LEFT JOIN yandex_forecast_samples
          ON yandex_forecast_samples.yandex_snapshot_id = yandex_snapshots.id
        WHERE yandex_forecast_samples.id IS NULL
        ORDER BY yandex_snapshots.id
        """
    ).fetchall()
    inserted = 0
    for row in rows:
        sampled_at = _optional_datetime(row["sampled_at"])
        vehicle_count = optional_int_value(row["vehicle_count"])
        if sampled_at is None or vehicle_count is None or vehicle_count < 0:
            continue
        raw = _json_object(row["raw_json"])
        arrivals = arrival_minutes_from_json(row["arrival_minutes_json"])
        report_window = matching_report_window(sampled_at, row["profile_key"])
        connection.execute(
            _INSERT_SQL,
            _sample_values(
                yandex_snapshot_id=int(row["id"]),
                profile_key=str(row["profile_key"]),
                sampled_at=sampled_at,
                source_method=str(row["source_method"]),
                source_status=str(row["source_status"]),
                available=optional_int_value(row["available"]) == 1,
                arrival_minutes=arrivals,
                vehicle_count=vehicle_count,
                newest_age_seconds=optional_int_value(raw.get("newest_age_seconds")),
                confidence=str(raw.get("confidence") or "unknown"),
                fallback_reason=str(row["fallback_reason"]),
                raw_status=str(raw.get("raw_status") or ""),
                report_window_key=report_window.key if report_window is not None else "",
                traffic=None,
                raw={
                    "forecast": raw,
                    "traffic": {},
                    "route_geometry_status": str(raw.get("route_geometry_status") or ""),
                    "route_geometry_reason": str(raw.get("route_geometry_reason") or ""),
                    "backfilled": True,
                },
            ),
        )
        inserted += 1
    if inserted:
        connection.commit()
    return inserted


def count_yandex_forecast_samples(connection: sqlite3.Connection) -> int:
    return count_table_rows(connection, "yandex_forecast_samples")


def _sample_values(
    *,
    yandex_snapshot_id: int,
    profile_key: str,
    sampled_at: datetime,
    source_method: str,
    source_status: str,
    available: bool,
    arrival_minutes: tuple[int, ...],
    vehicle_count: int,
    newest_age_seconds: int | None,
    confidence: str,
    fallback_reason: str,
    raw_status: str,
    report_window_key: str,
    traffic: RouteTrafficSnapshot | None,
    raw: dict[str, object],
) -> tuple[object, ...]:
    stored_arrivals = _trusted_arrivals(
        available=available,
        source_method=source_method,
        fallback_reason=fallback_reason,
        raw_status=raw_status,
        arrival_minutes=arrival_minutes,
    )
    return (
        yandex_snapshot_id,
        sampled_at.isoformat(),
        sampled_at.date().isoformat(),
        sampled_at.weekday(),
        sampled_at.hour * 60 + sampled_at.minute,
        profile_key,
        source_method,
        source_status,
        int(available),
        stored_arrivals[0] if stored_arrivals else None,
        json.dumps(list(stored_arrivals[1:]), ensure_ascii=False),
        vehicle_count,
        newest_age_seconds,
        confidence,
        fallback_reason,
        report_window_key,
        traffic.provider if traffic is not None else "none",
        traffic.status if traffic is not None else "not_collected",
        traffic.delay_seconds if traffic is not None else None,
        traffic.jams_level if traffic is not None else None,
        traffic.route_duration_seconds if traffic is not None else None,
        traffic.route_duration_in_traffic_seconds if traffic is not None else None,
        traffic.distance_meters if traffic is not None else None,
        json.dumps(_traffic_raw(traffic), ensure_ascii=False),
        json.dumps(raw, ensure_ascii=False),
    )


def _forecast_raw(forecast: YandexLiveForecast, *, newest_age_seconds: int | None) -> dict[str, object]:
    return {
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
    }


def _trusted_arrivals(
    *,
    available: bool,
    source_method: str,
    fallback_reason: str,
    raw_status: str,
    arrival_minutes: tuple[int, ...],
) -> tuple[int, ...]:
    if not available:
        return ()
    if is_trusted_eta_observation(source_method, fallback_reason=fallback_reason, raw_status=raw_status):
        return arrival_minutes
    return ()


def _traffic_raw(traffic: RouteTrafficSnapshot | None) -> dict[str, object]:
    return {} if traffic is None or traffic.raw is None else traffic.raw


def _json_object(raw_json: str) -> dict[str, object]:
    try:
        raw = json.loads(raw_json)
    except json.JSONDecodeError:
        return {}
    return raw if isinstance(raw, dict) else {}


def _optional_datetime(value: object) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


_INSERT_SQL = """
    INSERT INTO yandex_forecast_samples(
        yandex_snapshot_id, sampled_at, service_date, weekday, minute_of_day,
        profile_key, source_method, source_status, available, arrival_minutes,
        next_arrival_minutes_json, vehicle_count, newest_age_seconds, confidence,
        fallback_reason, report_window_key, traffic_provider,
        traffic_status, traffic_delay_seconds, traffic_jams_level,
        route_duration_seconds, route_duration_in_traffic_seconds,
        traffic_distance_meters, traffic_raw_json, raw_json
    )
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""
