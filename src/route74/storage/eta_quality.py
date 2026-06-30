from __future__ import annotations

import sqlite3

from route74.storage.helpers import TRUSTED_ETA_SOURCE_METHODS


def sanitize_untrusted_eta(connection: sqlite3.Connection) -> int:
    trusted = _trusted_source_filter()
    changed = 0
    bad_prediction_filter = """
        raw_json LIKE '%vehicle_prediction_thread_fallback:%'
        OR (
            source = 'ensemble'
            AND yandex_snapshot_id IN (
                SELECT yandex_snapshot_id
                FROM prediction_events
                WHERE raw_json LIKE '%vehicle_prediction_thread_fallback:%'
            )
        )
    """
    unsafe_coordinate_arrival_filter = """
        source = 'coordinate_stop'
        AND raw_json NOT LIKE '%"route_evidence"%'
    """
    changed += connection.execute(
        f"""
        DELETE FROM prediction_evaluations
        WHERE prediction_event_id IN (
            SELECT id
            FROM prediction_events
            WHERE {bad_prediction_filter}
        )
           OR arrival_event_id IN (
            SELECT id
            FROM arrival_events
            WHERE raw_json LIKE '%vehicle_prediction_thread_fallback:%'
        )
        """
    ).rowcount
    changed += connection.execute(
        f"""
        DELETE FROM prediction_evaluations
        WHERE arrival_event_id IN (
            SELECT id
            FROM arrival_events
            WHERE {unsafe_coordinate_arrival_filter}
        )
        """
    ).rowcount
    changed += connection.execute(
        f"""
        DELETE FROM prediction_events
        WHERE {bad_prediction_filter}
        """
    ).rowcount
    changed += connection.execute(
        """
        DELETE FROM arrival_events
        WHERE raw_json LIKE '%vehicle_prediction_thread_fallback:%'
        """
    ).rowcount
    changed += connection.execute(
        f"""
        DELETE FROM arrival_events
        WHERE {unsafe_coordinate_arrival_filter}
        """
    ).rowcount
    changed += connection.execute(
        f"""
        UPDATE yandex_forecast_samples
        SET arrival_minutes = NULL,
            next_arrival_minutes_json = '[]'
        WHERE source_method NOT IN ({trusted})
          AND (arrival_minutes IS NOT NULL OR next_arrival_minutes_json != '[]')
        """
    ).rowcount
    changed += connection.execute(
        """
        UPDATE yandex_forecast_samples
        SET arrival_minutes = NULL,
            next_arrival_minutes_json = '[]'
        WHERE fallback_reason LIKE 'vehicle_prediction_thread_fallback:%'
          AND (arrival_minutes IS NOT NULL OR next_arrival_minutes_json != '[]')
        """
    ).rowcount
    changed += connection.execute(
        f"""
        UPDATE report_window_snapshots
        SET arrival_minutes_json = '[]'
        WHERE source_method NOT IN ({trusted})
          AND arrival_minutes_json != '[]'
        """
    ).rowcount
    changed += connection.execute(
        """
        UPDATE report_window_snapshots
        SET arrival_minutes_json = '[]'
        WHERE raw_json LIKE '%vehicle_prediction_thread_fallback:%'
          AND arrival_minutes_json != '[]'
        """
    ).rowcount
    changed += connection.execute(
        f"""
        UPDATE yandex_snapshots
        SET arrival_minutes_json = '[]'
        WHERE source_method NOT IN ({trusted})
          AND arrival_minutes_json != '[]'
        """
    ).rowcount
    changed += connection.execute(
        """
        UPDATE yandex_snapshots
        SET arrival_minutes_json = '[]'
        WHERE fallback_reason LIKE 'vehicle_prediction_thread_fallback:%'
          AND arrival_minutes_json != '[]'
        """
    ).rowcount
    changed += connection.execute(
        f"""
        UPDATE yandex_vehicle_observations
        SET arrival_minutes = NULL
        WHERE source_method NOT IN ({trusted})
          AND arrival_minutes IS NOT NULL
        """
    ).rowcount
    changed += connection.execute(
        """
        UPDATE yandex_vehicle_observations
        SET arrival_minutes = NULL
        WHERE snapshot_id IN (
            SELECT id
            FROM yandex_snapshots
            WHERE fallback_reason LIKE 'vehicle_prediction_thread_fallback:%'
        )
          AND arrival_minutes IS NOT NULL
        """
    ).rowcount
    if changed:
        connection.commit()
    return changed


def _trusted_source_filter() -> str:
    return ",".join(repr(item) for item in TRUSTED_ETA_SOURCE_METHODS)
