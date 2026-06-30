from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from route74.models import NOVOSIBIRSK_TZ
from route74.storage import connect, init_db
from route74.storage.route_geometry import (
    polyline_points,
    previous_vehicle_positions,
    route_measure_for_point,
    route_projection_for_point,
)


def main() -> None:
    sampled_at = datetime(2026, 6, 4, 8, 30, tzinfo=NOVOSIBIRSK_TZ)

    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "route-geometry.sqlite"
        with connect(db_path) as connection:
            init_db(connection)
            _insert_observation(
                connection,
                sampled_at="2026-06-04T08:29:not-a-time",
                lat=54.86,
                lng=83.08,
                age_seconds=15,
            )
            _insert_observation(
                connection,
                sampled_at=(sampled_at - timedelta(minutes=2)).isoformat(),
                lat=54.87,
                lng=83.09,
                age_seconds=30,
            )
            _insert_observation(
                connection,
                sampled_at=(sampled_at - timedelta(minutes=1)).isoformat(),
                lat=54.88,
                lng=83.10,
                age_seconds=-10,
            )

            positions = previous_vehicle_positions(
                connection,
                "morning",
                "vehicle-1",
                sampled_at,
                route_thread_id="thread-1",
                lookback_minutes=10,
                max_age_seconds=120,
                limit=5,
            )

    _assert_equal(len(positions), 1)
    _assert_equal(positions[0].observed_at, sampled_at - timedelta(minutes=2, seconds=30))
    _assert_equal(positions[0].lat, 54.87)
    _assert_equal(positions[0].lng, 83.09)
    _assert_equal(positions[0].age_seconds, 30)
    _assert_projection_rejects_mismatched_measures()
    _assert_boolean_coordinates_are_rejected()
    print("OK | route geometry smoke passed")


def _assert_projection_rejects_mismatched_measures() -> None:
    points = ((54.87, 83.09), (54.88, 83.10))
    _assert_equal(route_projection_for_point(points, (), 54.875, 83.095), None)
    _assert_equal(route_projection_for_point(points, (0.0,), 54.875, 83.095), None)
    _assert_equal(route_measure_for_point(points, (0.0,), 54.875, 83.095), None)


def _assert_boolean_coordinates_are_rejected() -> None:
    points = ((54.87, 83.09), (54.88, 83.10))
    measures = (0.0, 100.0)
    _assert_equal(
        polyline_points("[[83.09, true], [83.1, 54.88], [false, 54.89]]"),
        ((54.88, 83.1),),
    )
    _assert_equal(route_projection_for_point(points, measures, True, 83.095), None)
    _assert_equal(route_projection_for_point(points, measures, 54.875, False), None)


def _insert_observation(
    connection: sqlite3.Connection,
    *,
    sampled_at: str,
    lat: float,
    lng: float,
    age_seconds: int,
) -> None:
    cursor = connection.execute(
        """
        INSERT INTO yandex_snapshots(
            sampled_at, profile_key, source_method, source_status,
            available, vehicle_count, arrival_minutes_json, fallback_reason, raw_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            sampled_at,
            "morning",
            "vehicle_prediction",
            "ok",
            1,
            1,
            "[8]",
            "",
            "{}",
        ),
    )
    snapshot_id = cursor.lastrowid
    connection.execute(
        """
        INSERT INTO yandex_vehicle_observations(
            snapshot_id, profile_key, source_method, source_status, vehicle_id,
            thread_id, lat, lng, arrival_minutes, age_seconds, raw_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            snapshot_id,
            "morning",
            "vehicle_prediction",
            "ok",
            "vehicle-1",
            "thread-1",
            lat,
            lng,
            8,
            age_seconds,
            "{}",
        ),
    )
    connection.commit()


def _assert_equal(actual: object, expected: object) -> None:
    if actual != expected:
        raise AssertionError(f"expected {expected!r}, got {actual!r}")


if __name__ == "__main__":
    main()
