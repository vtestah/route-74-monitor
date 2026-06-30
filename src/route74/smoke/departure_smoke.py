from __future__ import annotations

from collections.abc import Callable

from route74.domain.commute import DepartureUrgency
from route74.services.departure import (
    GET_READY_THRESHOLD_MINUTES,
    GO_NOW_THRESHOLD_MINUTES,
    _urgency_for_leave_in,
    validate_departure_thresholds,
)


def main() -> None:
    _assert_equal(GO_NOW_THRESHOLD_MINUTES, 0)
    _assert_equal(GET_READY_THRESHOLD_MINUTES, 5)
    _thresholds()
    _assert_equal(_urgency_for_leave_in(-1), DepartureUrgency.GO_NOW)
    _assert_equal(_urgency_for_leave_in(0), DepartureUrgency.GO_NOW)
    _assert_equal(_urgency_for_leave_in(1), DepartureUrgency.GET_READY)
    _assert_equal(_urgency_for_leave_in(5), DepartureUrgency.GET_READY)
    _assert_equal(_urgency_for_leave_in(6), DepartureUrgency.RELAX)
    _assert_rejects(lambda: _urgency_for_leave_in(True), "leave-in minutes")
    _assert_rejects(lambda: _thresholds(go_now_threshold_minutes=True), "go-now threshold")
    _assert_rejects(lambda: _thresholds(get_ready_threshold_minutes=True), "get-ready threshold")
    _assert_rejects(lambda: _thresholds(go_now_threshold_minutes=-1), "must be zero")
    _assert_rejects(
        lambda: _thresholds(get_ready_threshold_minutes=0),
        "greater than go-now",
    )
    print("OK | departure smoke passed")


def _thresholds(**overrides: object) -> None:
    values = {
        "go_now_threshold_minutes": GO_NOW_THRESHOLD_MINUTES,
        "get_ready_threshold_minutes": GET_READY_THRESHOLD_MINUTES,
    }
    values.update(overrides)
    validate_departure_thresholds(**values)


def _assert_rejects(action: Callable[[], object], expected: str) -> None:
    try:
        action()
    except ValueError as error:
        if expected not in str(error):
            raise AssertionError(f"expected {expected!r} in {error!s}") from error
    else:
        raise AssertionError(f"expected validation error containing {expected!r}")


def _assert_equal(actual: object, expected: object) -> None:
    if actual != expected:
        raise AssertionError(f"expected {expected!r}, got {actual!r}")


if __name__ == "__main__":
    main()
