from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace

from route74.domain.commute import DepartureSource
from route74.domain.departure_safety import (
    LIVE_DEPARTURE_SOURCES,
    LOW_TRUST_DEPARTURE_SOURCES,
    LOW_TRUST_SAFE_MARGIN_MINUTES,
    missed_by_minutes,
    safe_catch_margin_minutes,
    unsafe_arrival_without_safe_margin,
    validate_departure_safety_policy,
)
from route74.domain.eta import EtaConfidence


def main() -> None:
    _assert_policy_contract()
    _assert_large_wait_requires_safe_margin()
    _assert_low_confidence_requires_safe_margin()
    _assert_high_confidence_keeps_physical_margin()
    _assert_no_data_source_never_requires_safe_margin()
    print("OK | departure safety smoke passed")


def _assert_policy_contract() -> None:
    _assert_equal(
        LIVE_DEPARTURE_SOURCES,
        frozenset(
            {
                DepartureSource.YANDEX,
                DepartureSource.YANDEX_CORRECTED,
                DepartureSource.VEHICLE_PROGRESS,
            }
        ),
    )
    _assert_equal(
        LOW_TRUST_DEPARTURE_SOURCES,
        frozenset({*LIVE_DEPARTURE_SOURCES, DepartureSource.YANDEX_HISTORY}),
    )
    _assert_equal(LOW_TRUST_SAFE_MARGIN_MINUTES, 5)

    validate_departure_safety_policy(
        live_sources=LIVE_DEPARTURE_SOURCES,
        low_trust_sources=LOW_TRUST_DEPARTURE_SOURCES,
        safe_margin_minutes=LOW_TRUST_SAFE_MARGIN_MINUTES,
    )
    _assert_rejects(
        lambda: validate_departure_safety_policy(
            live_sources={DepartureSource.YANDEX},
            low_trust_sources=LOW_TRUST_DEPARTURE_SOURCES,
            safe_margin_minutes=LOW_TRUST_SAFE_MARGIN_MINUTES,
        ),
        "frozenset",
    )
    _assert_rejects(
        lambda: validate_departure_safety_policy(
            live_sources=frozenset({DepartureSource.YANDEX, "history"}),
            low_trust_sources=LOW_TRUST_DEPARTURE_SOURCES,
            safe_margin_minutes=LOW_TRUST_SAFE_MARGIN_MINUTES,
        ),
        "DepartureSource",
    )
    _assert_rejects(
        lambda: validate_departure_safety_policy(
            live_sources=frozenset({DepartureSource.YANDEX_HISTORY}),
            low_trust_sources=LOW_TRUST_DEPARTURE_SOURCES,
            safe_margin_minutes=LOW_TRUST_SAFE_MARGIN_MINUTES,
        ),
        "history source",
    )
    _assert_rejects(
        lambda: validate_departure_safety_policy(
            live_sources=LIVE_DEPARTURE_SOURCES,
            low_trust_sources=frozenset({DepartureSource.YANDEX_HISTORY}),
            safe_margin_minutes=LOW_TRUST_SAFE_MARGIN_MINUTES,
        ),
        "include live",
    )
    _assert_rejects(
        lambda: validate_departure_safety_policy(
            live_sources=LIVE_DEPARTURE_SOURCES,
            low_trust_sources=frozenset({*LIVE_DEPARTURE_SOURCES, DepartureSource.NONE}),
            safe_margin_minutes=LOW_TRUST_SAFE_MARGIN_MINUTES,
        ),
        "include history",
    )
    _assert_rejects(
        lambda: validate_departure_safety_policy(
            live_sources=LIVE_DEPARTURE_SOURCES,
            low_trust_sources=frozenset({*LOW_TRUST_DEPARTURE_SOURCES, DepartureSource.NONE}),
            safe_margin_minutes=LOW_TRUST_SAFE_MARGIN_MINUTES,
        ),
        "no-data source",
    )
    _assert_rejects(
        lambda: validate_departure_safety_policy(
            live_sources=LIVE_DEPARTURE_SOURCES,
            low_trust_sources=LOW_TRUST_DEPARTURE_SOURCES,
            safe_margin_minutes=True,
        ),
        "integer",
    )
    _assert_rejects(
        lambda: validate_departure_safety_policy(
            live_sources=LIVE_DEPARTURE_SOURCES,
            low_trust_sources=LOW_TRUST_DEPARTURE_SOURCES,
            safe_margin_minutes=0,
        ),
        "positive",
    )


def _assert_large_wait_requires_safe_margin() -> None:
    decision = _decision(
        source=DepartureSource.YANDEX,
        arrival_in_minutes=8,
        walk_minutes=6,
        confidence=EtaConfidence.MEDIUM,
        target_wait_minutes=5,
    )
    _assert_equal(safe_catch_margin_minutes(decision), -3)
    _assert_equal(unsafe_arrival_without_safe_margin(decision), True)
    _assert_equal(missed_by_minutes(decision), 3)


def _assert_low_confidence_requires_safe_margin() -> None:
    decision = _decision(
        source=DepartureSource.YANDEX_HISTORY,
        arrival_in_minutes=14,
        walk_minutes=12,
        confidence=EtaConfidence.LOW,
        target_wait_minutes=3,
    )
    _assert_equal(safe_catch_margin_minutes(decision), -1)
    _assert_equal(unsafe_arrival_without_safe_margin(decision), True)
    _assert_equal(missed_by_minutes(decision), 1)


def _assert_high_confidence_keeps_physical_margin() -> None:
    decision = _decision(
        source=DepartureSource.YANDEX,
        arrival_in_minutes=8,
        walk_minutes=6,
        confidence=EtaConfidence.HIGH,
        target_wait_minutes=2,
    )
    _assert_equal(safe_catch_margin_minutes(decision), 0)
    _assert_equal(unsafe_arrival_without_safe_margin(decision), False)
    _assert_equal(missed_by_minutes(decision), None)


def _assert_no_data_source_never_requires_safe_margin() -> None:
    decision = _decision(
        source=DepartureSource.NONE,
        arrival_in_minutes=8,
        walk_minutes=6,
        confidence=EtaConfidence.LOW,
        target_wait_minutes=5,
    )
    _assert_equal(safe_catch_margin_minutes(decision), -3)
    _assert_equal(unsafe_arrival_without_safe_margin(decision), False)
    _assert_equal(missed_by_minutes(decision), None)


def _decision(
    *,
    source: DepartureSource,
    arrival_in_minutes: int,
    walk_minutes: int,
    confidence: EtaConfidence,
    target_wait_minutes: int,
) -> SimpleNamespace:
    return SimpleNamespace(
        source=source,
        arrival_in_minutes=arrival_in_minutes,
        walk_minutes=walk_minutes,
        eta_consensus=SimpleNamespace(
            confidence=confidence,
            target_wait_minutes=target_wait_minutes,
        ),
    )


def _assert_equal(actual: object, expected: object) -> None:
    if actual != expected:
        raise AssertionError(f"expected {expected!r}, got {actual!r}")


def _assert_rejects(action: Callable[[], object], expected: str) -> None:
    try:
        action()
    except ValueError as error:
        if expected not in str(error):
            raise AssertionError(f"expected {expected!r} in {str(error)!r}") from error
    else:
        raise AssertionError(f"expected ValueError containing {expected!r}")


if __name__ == "__main__":
    main()
