from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from math import asin, cos, isfinite, radians, sin, sqrt

from route74.sources.yandex.constants import ROUTE_TRAFFIC_POINTS_BY_PROFILE


EARTH_RADIUS_METERS = 6_371_000
ROUTE_GEOMETRY_MAX_AGE_DAYS = 14


@dataclass(frozen=True)
class RouteGeometry:
    thread_id: str
    points: tuple[tuple[float, float], ...]
    measures: tuple[float, ...]
    target_measure: float


@dataclass(frozen=True)
class RouteProjection:
    measure: float
    distance_meters: float


@dataclass(frozen=True)
class VehiclePosition:
    observed_at: datetime
    lat: float
    lng: float
    age_seconds: int | None


def route_geometry_cache_status(
    connection: sqlite3.Connection,
    *,
    profile_key: str,
    sampled_at: datetime,
) -> str | None:
    row = connection.execute(
        """
        SELECT route_polyline_json, updated_at
        FROM route_geometry
        WHERE profile_key = ?
        """,
        (profile_key,),
    ).fetchone()
    if row is None:
        return None
    try:
        updated_at = datetime.fromisoformat(str(row["updated_at"]))
    except ValueError:
        return "invalid"
    if updated_at.tzinfo is None and sampled_at.tzinfo is not None:
        updated_at = updated_at.replace(tzinfo=sampled_at.tzinfo)
    if updated_at < sampled_at - timedelta(days=ROUTE_GEOMETRY_MAX_AGE_DAYS):
        return "stale"
    if len(polyline_points(str(row["route_polyline_json"]))) < 2:
        return "invalid"
    return "cached"


def load_route_geometry(
    connection: sqlite3.Connection,
    profile_key: str,
    *,
    sampled_at: datetime,
) -> RouteGeometry | None:
    row = connection.execute(
        """
        SELECT thread_id, route_polyline_json, stops_json, target_stop_id, updated_at
        FROM route_geometry
        WHERE profile_key = ?
        """,
        (profile_key,),
    ).fetchone()
    if row is None:
        return None
    try:
        updated_at = datetime.fromisoformat(str(row["updated_at"]))
    except ValueError:
        return None
    if updated_at.tzinfo is None and sampled_at.tzinfo is not None:
        updated_at = updated_at.replace(tzinfo=sampled_at.tzinfo)
    if updated_at < sampled_at - timedelta(days=ROUTE_GEOMETRY_MAX_AGE_DAYS):
        return None
    points = polyline_points(str(row["route_polyline_json"]))
    if len(points) < 2:
        return None
    target = _target_point_from_stops(str(row["stops_json"]), str(row["target_stop_id"]))
    if target is None:
        target_const = ROUTE_TRAFFIC_POINTS_BY_PROFILE.get(profile_key, ROUTE_TRAFFIC_POINTS_BY_PROFILE["morning"])[0]
        target = (target_const.lat, target_const.lng)
    measures = route_measures(points)
    target_measure = route_measure_for_point(points, measures, target[0], target[1])
    if target_measure is None:
        return None
    return RouteGeometry(
        thread_id=str(row["thread_id"]),
        points=points,
        measures=measures,
        target_measure=target_measure,
    )


def previous_vehicle_positions(
    connection: sqlite3.Connection,
    profile_key: str,
    vehicle_id: str,
    sampled_at: datetime,
    *,
    route_thread_id: str,
    lookback_minutes: int,
    max_age_seconds: int,
    limit: int,
) -> tuple[VehiclePosition, ...]:
    since = sampled_at - timedelta(minutes=lookback_minutes)
    rows = connection.execute(
        """
        SELECT
            yandex_snapshots.sampled_at,
            yandex_vehicle_observations.lat,
            yandex_vehicle_observations.lng,
            yandex_vehicle_observations.age_seconds
        FROM yandex_vehicle_observations
        JOIN yandex_snapshots ON yandex_snapshots.id = yandex_vehicle_observations.snapshot_id
        WHERE yandex_vehicle_observations.profile_key = ?
          AND yandex_vehicle_observations.vehicle_id = ?
          AND yandex_vehicle_observations.thread_id = ?
          AND yandex_snapshots.sampled_at >= ?
          AND yandex_snapshots.sampled_at < ?
          AND yandex_vehicle_observations.lat IS NOT NULL
          AND yandex_vehicle_observations.lng IS NOT NULL
          AND (
              yandex_vehicle_observations.age_seconds IS NULL
              OR (
                  yandex_vehicle_observations.age_seconds >= 0
                  AND yandex_vehicle_observations.age_seconds <= ?
              )
          )
        ORDER BY yandex_snapshots.sampled_at DESC
        LIMIT ?
        """,
        (
            profile_key,
            vehicle_id,
            route_thread_id,
            since.isoformat(),
            sampled_at.isoformat(),
            max_age_seconds,
            limit,
        ),
    ).fetchall()
    positions: list[VehiclePosition] = []
    for row in rows:
        point = _valid_point(row["lat"], row["lng"])
        if point is None:
            continue
        age_seconds = _optional_int(row["age_seconds"])
        if age_seconds is not None and age_seconds < 0:
            continue
        row_sampled_at = _optional_datetime(row["sampled_at"])
        if row_sampled_at is None:
            continue
        positions.append(
            VehiclePosition(
                observed_at=position_observed_at(row_sampled_at, age_seconds),
                lat=point[0],
                lng=point[1],
                age_seconds=age_seconds,
            )
        )
    return tuple(positions)


def position_observed_at(sampled_at: datetime, age_seconds: int | None) -> datetime:
    if age_seconds is None:
        return sampled_at
    return sampled_at - timedelta(seconds=max(0, age_seconds))


def polyline_points(raw_json: str) -> tuple[tuple[float, float], ...]:
    try:
        raw = json.loads(raw_json)
    except json.JSONDecodeError:
        return ()
    points: list[tuple[float, float]] = []
    if not isinstance(raw, list):
        return ()
    for item in raw:
        if isinstance(item, list) and len(item) >= 2:
            point = _valid_point(item[1], item[0])
            if point is not None:
                points.append(point)
    return tuple(points)


def route_measures(points: tuple[tuple[float, float], ...]) -> tuple[float, ...]:
    measures = [0.0]
    for previous, current in zip(points, points[1:]):
        measures.append(measures[-1] + haversine_meters(previous[0], previous[1], current[0], current[1]))
    return tuple(measures)


def route_measure_for_point(
    points: tuple[tuple[float, float], ...],
    measures: tuple[float, ...],
    lat: float,
    lng: float,
) -> float | None:
    projection = route_projection_for_point(points, measures, lat, lng)
    return projection.measure if projection is not None else None


def route_projection_for_point(
    points: tuple[tuple[float, float], ...],
    measures: tuple[float, ...],
    lat: float,
    lng: float,
) -> RouteProjection | None:
    point = _valid_point(lat, lng)
    if point is None:
        return None
    lat, lng = point
    if not points:
        return None
    if len(measures) != len(points):
        return None
    if len(points) == 1:
        distance = haversine_meters(lat, lng, points[0][0], points[0][1])
        if not isfinite(distance):
            return None
        return RouteProjection(measures[0], distance)
    best: tuple[float, float] | None = None
    for index, (start, end) in enumerate(zip(points, points[1:])):
        segment_length = measures[index + 1] - measures[index]
        ratio, distance_squared = _point_segment_projection(lat, lng, start, end)
        measure = measures[index] + segment_length * ratio
        if not isfinite(distance_squared) or not isfinite(measure):
            continue
        candidate = (distance_squared, measure)
        if best is None or candidate[0] < best[0]:
            best = candidate
    if best is None:
        return None
    return RouteProjection(best[1], sqrt(best[0]))


def haversine_meters(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    first = _valid_point(lat1, lng1)
    second = _valid_point(lat2, lng2)
    if first is None or second is None:
        return float("nan")
    dlat = radians(second[0] - first[0])
    dlng = radians(second[1] - first[1])
    a = sin(dlat / 2) ** 2 + cos(radians(first[0])) * cos(radians(second[0])) * sin(dlng / 2) ** 2
    return 2 * EARTH_RADIUS_METERS * asin(sqrt(a))


def _target_point_from_stops(raw_json: str, target_stop_id: str) -> tuple[float, float] | None:
    try:
        raw = json.loads(raw_json)
    except json.JSONDecodeError:
        return None
    if not isinstance(raw, list):
        return None
    for item in raw:
        if not isinstance(item, dict) or item.get("stop_id") != target_stop_id:
            continue
        lat = item.get("lat")
        lng = item.get("lng")
        if lat is not None and lng is not None:
            return _valid_point(lat, lng)
    return None


def _valid_point(lat: object, lng: object) -> tuple[float, float] | None:
    parsed_lat = _optional_float(lat)
    parsed_lng = _optional_float(lng)
    if parsed_lat is None or parsed_lng is None:
        return None
    lat = parsed_lat
    lng = parsed_lng
    if not isfinite(lat) or not isfinite(lng):
        return None
    if not -90 <= lat <= 90 or not -180 <= lng <= 180:
        return None
    return lat, lng


def _point_segment_projection(
    lat: float,
    lng: float,
    start: tuple[float, float],
    end: tuple[float, float],
) -> tuple[float, float]:
    start_x, start_y = _local_xy_meters(start[0], start[1], lat, lng)
    end_x, end_y = _local_xy_meters(end[0], end[1], lat, lng)
    vector_x = end_x - start_x
    vector_y = end_y - start_y
    segment_squared = vector_x * vector_x + vector_y * vector_y
    if segment_squared == 0:
        return 0.0, start_x * start_x + start_y * start_y
    ratio = max(0.0, min(1.0, -(start_x * vector_x + start_y * vector_y) / segment_squared))
    projected_x = start_x + ratio * vector_x
    projected_y = start_y + ratio * vector_y
    return ratio, projected_x * projected_x + projected_y * projected_y


def _local_xy_meters(lat: float, lng: float, origin_lat: float, origin_lng: float) -> tuple[float, float]:
    x = radians(lng - origin_lng) * cos(radians(origin_lat)) * EARTH_RADIUS_METERS
    y = radians(lat - origin_lat) * EARTH_RADIUS_METERS
    return x, y


def _optional_int(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _optional_float(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if isfinite(parsed) else None


def _optional_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None
