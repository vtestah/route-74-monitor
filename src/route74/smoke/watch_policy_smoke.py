from __future__ import annotations

from collections.abc import Callable

from route74.domain.watch_policy import (
    EARLY_ALERT_LEAVE_IN,
    FINAL_ALERT_LEAVE_IN,
    WATCH_DURATION_MINUTES,
    WATCH_POLL_INTERVAL_SECONDS,
    validate_watch_policy,
)


def main() -> None:
    assert_watch_policy_contract()
    print("OK | watch policy smoke passed")


def assert_watch_policy_contract() -> None:
    _assert_equal(WATCH_DURATION_MINUTES, 30)
    _assert_equal(WATCH_POLL_INTERVAL_SECONDS, 10)
    _assert_equal(EARLY_ALERT_LEAVE_IN, 7)
    _assert_equal(FINAL_ALERT_LEAVE_IN, 0)
    _policy()
    _assert_rejects(lambda: _policy(duration_minutes=0), "watch duration")
    _assert_rejects(lambda: _policy(poll_interval_seconds=True), "watch poll interval")
    _assert_rejects(
        lambda: _policy(final_alert_leave_in=1),
        "final alert leave-in must be zero",
    )
    _assert_rejects(lambda: _policy(early_alert_leave_in=0), "early alert leave-in")
    _assert_rejects(
        lambda: _policy(duration_minutes=7, early_alert_leave_in=7),
        "watch duration",
    )
    _assert_rejects(
        lambda: _policy(
            duration_minutes=3,
            poll_interval_seconds=181,
            early_alert_leave_in=1,
        ),
        "poll interval",
    )


def _policy(**overrides: object) -> None:
    values = {
        "duration_minutes": WATCH_DURATION_MINUTES,
        "poll_interval_seconds": WATCH_POLL_INTERVAL_SECONDS,
        "early_alert_leave_in": EARLY_ALERT_LEAVE_IN,
        "final_alert_leave_in": FINAL_ALERT_LEAVE_IN,
    }
    values.update(overrides)
    validate_watch_policy(**values)


def _assert_equal(actual: object, expected: object) -> None:
    if actual != expected:
        raise AssertionError(f"expected {expected!r}, got {actual!r}")


def _assert_rejects(action: Callable[[], object], expected: str) -> None:
    try:
        action()
    except ValueError as error:
        if expected not in str(error):
            raise AssertionError(f"expected {expected!r} in {error!s}") from error
    else:
        raise AssertionError(f"expected validation error containing {expected!r}")


if __name__ == "__main__":
    main()
