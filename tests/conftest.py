"""Shared pytest fixtures for the route74 test layer.

These fixtures reuse the project's existing fakes so the pytest layer stays in
sync with the smoke harness instead of duplicating test doubles.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import pytest
from route74.reporting_smoke_fixtures import FakeYandexSource, fake_traffic_source

from route74.models import NOVOSIBIRSK_TZ
from route74.sources.yandex.models import YandexLiveForecast
from route74.storage import RouteTrafficSnapshot, connect, init_db


@pytest.fixture
def tmp_db(tmp_path: Path) -> Path:
    """Return a freshly initialised, empty SQLite database path."""
    db_path = tmp_path / "route74.sqlite"
    with connect(db_path) as connection:
        init_db(connection)
    return db_path


@pytest.fixture
def watch_state_path(tmp_path: Path) -> Path:
    """Return a path for an (initially absent) watch-state JSON file."""
    return tmp_path / "web_watches.json"


@pytest.fixture
def fake_yandex_forecast() -> YandexLiveForecast:
    """Return a deterministic live forecast from the shared fake source."""
    return FakeYandexSource().get_forecast()


@pytest.fixture
def fake_traffic() -> RouteTrafficSnapshot:
    """Return a deterministic traffic snapshot from the shared fake source."""
    return fake_traffic_source()


@pytest.fixture
def nsk_now() -> datetime:
    """Return a fixed, timezone-aware Novosibirsk timestamp for determinism."""
    return datetime(2026, 6, 4, 9, 10, tzinfo=NOVOSIBIRSK_TZ)


@pytest.fixture
def report_row() -> Callable[..., dict[str, object]]:
    """Return a factory building a report-window snapshot row dict.

    Mirrors the row shape consumed by ``route74.dashboard.data._series_row``.
    """

    def _build(
        *,
        arrivals: str = "[5]",
        delay: object = None,
        jams: object = None,
        traffic_status: str = "ok",
    ) -> dict[str, object]:
        return {
            "sampled_at": "2026-06-04T09:15:00+07:00",
            "report_window_key": "weekday_morning_09_12",
            "profile_key": "morning",
            "source_method": "vehicle_prediction",
            "source_status": "ok",
            "arrival_minutes_json": arrivals,
            "traffic_status": traffic_status,
            "traffic_delay_seconds": delay,
            "traffic_jams_level": jams,
            "raw_json": "{}",
        }

    return _build
