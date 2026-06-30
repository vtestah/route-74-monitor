from __future__ import annotations

from datetime import time

from route74.smoke.arrival_planning_smoke import main as arrival_planning_smoke
from route74.smoke.cli_arguments_smoke import main as cli_arguments_smoke
from route74.smoke.cli_formatting_smoke import main as cli_formatting_smoke
from route74.smoke.decision_validation_smoke import assert_commute_payload_guardrails
from route74.domain.commute import CommuteProfile
from route74.domain.traffic import RouteTrafficSnapshot
from route74.smoke.forecast_readiness_validation_smoke import main as forecast_readiness_validation_smoke
from route74.models import NOVOSIBIRSK_TZ
from route74.presenters.calculation import format_calculation_explanation
from route74.smoke.presentation_smoke import main as presentation_smoke
from route74.smoke.prediction_engine_smoke import main as prediction_engine_smoke
from route74.smoke.prediction_selection_smoke import main as prediction_selection_smoke


def main() -> None:
    _assert_profile_guardrails()
    assert_commute_payload_guardrails()
    _assert_traffic_payload_guardrails()
    cli_arguments_smoke()
    cli_formatting_smoke()
    presentation_smoke()
    arrival_planning_smoke()
    prediction_engine_smoke()
    prediction_selection_smoke()
    forecast_readiness_validation_smoke()
    print("OK | commute architecture smoke passed")


def _assert_profile_guardrails() -> None:
    _assert_rejects(lambda: _profile(key=""), "key")
    _assert_rejects(lambda: _profile(window_start=time(22, 0), window_end=time(6, 0)), "window")
    _assert_rejects(lambda: _profile(default_walk_minutes=True), "walk")
    _assert_rejects(lambda: _profile(default_walk_minutes=61), "walk")
    _assert_rejects(lambda: format_calculation_explanation(True, 17), "morning walk")
    _assert_rejects(lambda: format_calculation_explanation(12, 61), "evening walk")


def _assert_traffic_payload_guardrails() -> None:
    RouteTrafficSnapshot("fake", "ok", delay_seconds=0)
    RouteTrafficSnapshot(
        "fake",
        "ok",
        raw={"reason": "ok", "items": [{"delay": 1, "ratio": 1.5, "empty": None}]},
    )
    _assert_rejects(lambda: RouteTrafficSnapshot(" fake", "ok"), "plain key")
    _assert_rejects(lambda: RouteTrafficSnapshot("fake", " ok "), "plain key")
    _assert_rejects(lambda: RouteTrafficSnapshot("fake route", "ok"), "plain key")
    _assert_rejects(lambda: RouteTrafficSnapshot("fake", "not\nok"), "plain key")
    _assert_rejects(lambda: RouteTrafficSnapshot("fake", "ok", raw=[]), "JSON object")
    _assert_rejects(lambda: RouteTrafficSnapshot("fake", "ok", raw={1: "bad"}), "string keys")
    _assert_rejects(lambda: RouteTrafficSnapshot("fake", "ok", raw={"bad": object()}), "JSON values")
    _assert_rejects(lambda: RouteTrafficSnapshot("fake", "ok", raw={"bad": float("nan")}), "non-finite")
    _assert_rejects(lambda: RouteTrafficSnapshot("fake", "ok", route_duration_seconds=0), "route duration")
    _assert_rejects(lambda: RouteTrafficSnapshot("fake", "ok", route_duration_in_traffic_seconds=0), "traffic route")
    _assert_rejects(lambda: RouteTrafficSnapshot("fake", "ok", distance_meters=0), "traffic distance")


def _profile(**overrides: object) -> CommuteProfile:
    values = {
        "key": "test",
        "title": "Test profile",
        "live_stop_id": "stop",
        "destination": "Destination",
        "window_start": time(6, 0),
        "window_end": time(10, 0),
        "default_walk_minutes": 12,
    } | overrides
    return CommuteProfile(**values)  # type: ignore[arg-type]


def _assert_rejects(factory, expected: str) -> None:
    try:
        factory()
    except ValueError as error:
        if expected not in str(error):
            raise AssertionError(f"expected {expected!r} in {str(error)!r}") from error
    else:
        raise AssertionError(f"expected validation error: {expected}")


if __name__ == "__main__":
    main()
