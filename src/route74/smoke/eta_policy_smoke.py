from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

from route74.domain.eta import EtaConfidence
from route74.domain.eta_policy import (
    DEFAULT_ETA_POLICY,
    target_wait_minutes_for_confidence,
)


def main() -> None:
    _assert_strict_target_wait_contract()
    _assert_source_risk_contract()
    _assert_default_target_waits()
    print("OK | ETA policy smoke passed")


def _assert_strict_target_wait_contract() -> None:
    _assert_rejects(
        lambda: replace(
            DEFAULT_ETA_POLICY,
            medium_target_wait_minutes=DEFAULT_ETA_POLICY.high_target_wait_minutes,
        ),
        "strictly grow",
    )
    _assert_rejects(
        lambda: replace(
            DEFAULT_ETA_POLICY,
            low_target_wait_minutes=DEFAULT_ETA_POLICY.medium_target_wait_minutes,
        ),
        "strictly grow",
    )
    _assert_rejects(
        lambda: replace(
            DEFAULT_ETA_POLICY,
            history_target_wait_minutes=DEFAULT_ETA_POLICY.low_target_wait_minutes,
        ),
        "strictly grow",
    )


def _assert_source_risk_contract() -> None:
    _assert_rejects(
        lambda: replace(
            DEFAULT_ETA_POLICY,
            source_risk_high_miss_rate_percent=0,
        ),
        "above zero",
    )
    _assert_rejects(
        lambda: replace(
            DEFAULT_ETA_POLICY,
            source_risk_high_min_buffer_minutes=0,
        ),
        "positive integer",
    )


def _assert_default_target_waits() -> None:
    _assert_equal(target_wait_minutes_for_confidence(EtaConfidence.HIGH), 2)
    _assert_equal(target_wait_minutes_for_confidence(EtaConfidence.MEDIUM), 3)
    _assert_equal(target_wait_minutes_for_confidence(EtaConfidence.LOW), 5)
    _assert_equal(target_wait_minutes_for_confidence(EtaConfidence.UNKNOWN), 5)


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
