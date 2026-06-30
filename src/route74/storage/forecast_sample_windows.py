from __future__ import annotations

import sqlite3
from datetime import datetime

from route74.domain.reporting import matching_report_window


def backfill_yandex_forecast_sample_windows(connection: sqlite3.Connection) -> int:
    rows = connection.execute(
        """
        SELECT id, sampled_at, profile_key
        FROM yandex_forecast_samples
        WHERE report_window_key = ''
        """
    ).fetchall()
    changed = 0
    for row in rows:
        sampled_at = _optional_datetime(row["sampled_at"])
        if sampled_at is None:
            continue
        try:
            window = matching_report_window(sampled_at, row["profile_key"])
        except ValueError:
            continue
        if window is None:
            continue
        connection.execute(
            "UPDATE yandex_forecast_samples SET report_window_key = ? WHERE id = ?",
            (window.key, int(row["id"])),
        )
        changed += 1
    if changed:
        connection.commit()
    return changed


def _optional_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None
