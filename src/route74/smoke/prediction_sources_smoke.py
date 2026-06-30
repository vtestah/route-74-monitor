from __future__ import annotations

from collections.abc import Callable

from route74.domain.prediction_sources import (
    EVALUATED_EVENT_SOURCES,
    PREDICTION_SOURCE_SPECS,
    SOURCE_ENSEMBLE,
    PredictionSourceSpec,
    validate_evaluated_event_sources,
    validate_prediction_source_specs,
)


def main() -> None:
    _assert_source_priorities_are_contiguous()
    _assert_evaluated_sources_follow_priority()
    print("OK | prediction sources smoke passed")


def _assert_source_priorities_are_contiguous() -> None:
    _assert_equal(
        tuple(spec.priority for spec in PREDICTION_SOURCE_SPECS),
        tuple(range(len(PREDICTION_SOURCE_SPECS))),
    )
    gap = tuple(
        PredictionSourceSpec(
            spec.eta_source,
            spec.event_source,
            priority=spec.priority + (1 if spec.priority >= 2 else 0),
            is_live=spec.is_live,
            early_conflict_eligible=spec.early_conflict_eligible,
        )
        for spec in PREDICTION_SOURCE_SPECS
    )
    _assert_rejects(
        lambda: validate_prediction_source_specs(gap),
        "priorities must be contiguous",
    )


def _assert_evaluated_sources_follow_priority() -> None:
    ordered_sources = tuple(
        spec.event_source for spec in sorted(PREDICTION_SOURCE_SPECS, key=lambda item: item.priority)
    )
    _assert_equal(EVALUATED_EVENT_SOURCES, (*ordered_sources, SOURCE_ENSEMBLE))
    wrong_order = (
        PREDICTION_SOURCE_SPECS[1].event_source,
        PREDICTION_SOURCE_SPECS[0].event_source,
        *(spec.event_source for spec in PREDICTION_SOURCE_SPECS[2:]),
        SOURCE_ENSEMBLE,
    )
    _assert_rejects(
        lambda: validate_evaluated_event_sources(wrong_order, PREDICTION_SOURCE_SPECS),
        "prediction priority",
    )


def _assert_rejects(action: Callable[[], object], expected: str) -> None:
    try:
        action()
    except ValueError as error:
        _assert_contains(str(error), expected)
    else:
        raise AssertionError(f"expected validation error containing {expected!r}")


def _assert_contains(actual: str, expected: str) -> None:
    if expected not in actual:
        raise AssertionError(f"expected {expected!r} in {actual!r}")


def _assert_equal(actual: object, expected: object) -> None:
    if actual != expected:
        raise AssertionError(f"expected {expected!r}, got {actual!r}")


if __name__ == "__main__":
    main()
