from __future__ import annotations

from collections.abc import Callable

from route74.domain.eta_policy import (
    HIGH_TARGET_WAIT_MINUTES,
    HISTORY_TARGET_WAIT_MINUTES,
    LOW_TARGET_WAIT_MINUTES,
    MEDIUM_TARGET_WAIT_MINUTES,
)
from route74.domain.wait_policy import TARGET_STOP_WAIT_MINUTES, validate_wait_policy


def main() -> None:
    _assert_equal(TARGET_STOP_WAIT_MINUTES, 2)
    _assert_equal(HIGH_TARGET_WAIT_MINUTES, 2)
    _assert_equal(MEDIUM_TARGET_WAIT_MINUTES, 3)
    _assert_equal(LOW_TARGET_WAIT_MINUTES, 5)
    _assert_equal(HISTORY_TARGET_WAIT_MINUTES, 6)
    _policy()
    _assert_rejects(lambda: _policy(target_stop_wait_minutes=0), "target stop wait")
    _assert_rejects(lambda: _policy(target_stop_wait_minutes=True), "target stop wait")
    _assert_rejects(
        lambda: _policy(high_confidence_target_wait_minutes=True),
        "high confidence target wait",
    )
    _assert_rejects(
        lambda: _policy(target_stop_wait_minutes=3),
        "must match high confidence",
    )
    _assert_rejects(
        lambda: _policy(
            target_stop_wait_minutes=3,
            high_confidence_target_wait_minutes=3,
            medium_confidence_target_wait_minutes=3,
        ),
        "below medium confidence",
    )
    _assert_rejects(
        lambda: _policy(low_confidence_target_wait_minutes=2),
        "medium confidence target wait must stay below low confidence target wait",
    )
    _assert_rejects(
        lambda: _policy(low_confidence_target_wait_minutes=MEDIUM_TARGET_WAIT_MINUTES),
        "medium confidence target wait must stay below low confidence target wait",
    )
    _assert_rejects(
        lambda: _policy(history_target_wait_minutes=4),
        "low confidence target wait must stay below history target wait",
    )
    _assert_rejects(
        lambda: _policy(history_target_wait_minutes=LOW_TARGET_WAIT_MINUTES),
        "low confidence target wait must stay below history target wait",
    )
    print("OK | wait policy smoke passed")


def _policy(**overrides: object) -> None:
    values = {
        "target_stop_wait_minutes": TARGET_STOP_WAIT_MINUTES,
        "high_confidence_target_wait_minutes": HIGH_TARGET_WAIT_MINUTES,
        "medium_confidence_target_wait_minutes": MEDIUM_TARGET_WAIT_MINUTES,
        "low_confidence_target_wait_minutes": LOW_TARGET_WAIT_MINUTES,
        "history_target_wait_minutes": HISTORY_TARGET_WAIT_MINUTES,
    }
    values.update(overrides)
    validate_wait_policy(**values)


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
