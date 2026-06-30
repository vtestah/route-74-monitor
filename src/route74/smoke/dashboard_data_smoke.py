from __future__ import annotations

import sqlite3
from datetime import datetime

from route74.dashboard.data import (
    _profile_forecast_readiness_summaries,
    _profile_latency_summaries,
    _recent_row,
    _series_row,
)
from route74.models import NOVOSIBIRSK_TZ


def main() -> None:
    rows = [
        _row(arrivals="[5]", delay="bad-delay", jams="bad-jams"),
        _row(arrivals="[7]", delay=300, jams=4),
        _row(arrivals="[]", delay=-60, jams=-1),
    ]
    series = _series_row("2026-06-04", rows)
    _assert_equal(series["samples"], 3)
    _assert_equal(series["eta_samples"], 2)
    _assert_equal(series["avg_traffic_delay_minutes"], 5)
    _assert_equal(series["max_traffic_delay_minutes"], 5)
    _assert_equal(series["avg_jams_level"], 4)
    # ETA stats are suppressed below the minimum sample threshold to avoid noisy values.
    _assert_equal(series["p80_eta_minutes"], None)
    _assert_equal(series["avg_eta_minutes"], None)
    _assert_series_eta_stats_with_enough_samples()

    malformed = _recent_row(rows[0])
    _assert_equal(malformed["traffic_delay_minutes"], None)
    _assert_equal(malformed["traffic_jams_level"], None)
    _assert_equal(malformed["source_reason"], "browser_no_prediction_response")

    string_values = _recent_row(_row(delay="120", jams="3"))
    _assert_equal(string_values["traffic_delay_minutes"], 2)
    _assert_equal(string_values["traffic_jams_level"], 3)
    _assert_equal(string_values["source_reason"], "browser_no_prediction_response")
    _assert_profile_latency_falls_back_without_profile_column()
    _assert_forecast_readiness_falls_back_without_samples_table()
    print("OK | dashboard data smoke passed")


def _assert_series_eta_stats_with_enough_samples() -> None:
    rows = [
        _row(arrivals="[5]", delay=40, jams=1),
        _row(arrivals="[7]", delay=40, jams=2),
        _row(arrivals="[12]", delay=100, jams=3),
    ]
    series = _series_row("2026-06-04", rows)
    _assert_equal(series["eta_samples"], 3)
    _assert_equal(series["p80_eta_minutes"], 12)
    _assert_equal(series["avg_eta_minutes"], 8)
    # Delays [40, 40, 100] average to 60s -> 1 minute via single rounding.
    # Per-sample rounding would give round((1+1+2)/3)=1 here, but with e.g. 20s
    # values per-sample rounding collapses to 0; single rounding stays correct.
    _assert_equal(series["avg_traffic_delay_minutes"], 1)


def _assert_profile_latency_falls_back_without_profile_column() -> None:
    with sqlite3.connect(":memory:") as connection:
        connection.row_factory = sqlite3.Row
        connection.execute(
            """
            CREATE TABLE bot_interaction_events(
                received_at TEXT,
                total_ms INTEGER,
                forecast_ms INTEGER,
                render_ms INTEGER,
                send_ms INTEGER,
                status TEXT,
                update_type TEXT,
                reply_source TEXT,
                yandex_source_method TEXT,
                error TEXT
            )
            """
        )
        current_time = datetime(2026, 6, 4, 9, 0, tzinfo=NOVOSIBIRSK_TZ)
        _assert_equal(_profile_latency_summaries(connection, current_time=current_time), {})


def _assert_forecast_readiness_falls_back_without_samples_table() -> None:
    with sqlite3.connect(":memory:") as connection:
        connection.row_factory = sqlite3.Row
        current_time = datetime(2026, 6, 4, 9, 0, tzinfo=NOVOSIBIRSK_TZ)
        _assert_equal(_profile_forecast_readiness_summaries(connection, current_time=current_time), {})


def _row(
    *,
    arrivals: str = "[5]",
    delay: object = None,
    jams: object = None,
) -> dict[str, object]:
    return {
        "sampled_at": "2026-06-04T09:15:00+07:00",
        "report_window_key": "weekday_morning_09_12",
        "profile_key": "morning",
        "source_method": "vehicle_prediction",
        "source_status": "ok",
        "arrival_minutes_json": arrivals,
        "traffic_status": "ok",
        "traffic_delay_seconds": delay,
        "traffic_jams_level": jams,
        "raw_json": '{"forecast":{"fallback_reason":"browser_no_prediction_response","raw_status":"unavailable"}}',
    }


def _assert_equal(actual: object, expected: object) -> None:
    if actual != expected:
        raise AssertionError(f"expected {expected!r}, got {actual!r}")


if __name__ == "__main__":
    main()
