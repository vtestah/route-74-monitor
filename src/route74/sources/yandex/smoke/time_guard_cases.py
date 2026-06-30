from __future__ import annotations

from datetime import UTC, datetime

from route74.domain.profiles import EVENING
from route74.sources.yandex.parser.time_fields import arrival_minutes
from route74.sources.yandex.smoke.assertions import assert_rejects
from route74.sources.yandex.vehicle_prediction import parse_vehicle_prediction_payload


def run_yandex_time_guard_smoke() -> None:
    assert_rejects(
        lambda: arrival_minutes({"secondsLeft": 120}, datetime(2026, 6, 4, 7, 0)),
        "timezone-aware",
    )
    assert_rejects(
        lambda: arrival_minutes({"secondsLeft": 120}, datetime(2026, 6, 4, 0, 0, tzinfo=UTC)),
        "Asia/Novosibirsk",
    )
    assert_rejects(
        lambda: parse_vehicle_prediction_payload(
            {"data": {"predictions": []}},
            profile=EVENING,
            current_time=datetime(2026, 6, 4, 20, 12),
        ),
        "timezone-aware",
    )
    assert_rejects(
        lambda: parse_vehicle_prediction_payload(
            {"data": {"predictions": []}},
            profile=EVENING,
            current_time=datetime(2026, 6, 4, 13, 12, tzinfo=UTC),
        ),
        "Asia/Novosibirsk",
    )
