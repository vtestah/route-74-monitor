from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

from route74.domain.eta import (
    EtaConfidence,
    EtaConsensus,
    EtaExplanationAction,
    EtaExplanationCode,
    EtaFactor,
    EtaFactorKind,
    EtaSource,
)
from route74.domain.eta_policy import (
    DEFAULT_ETA_POLICY,
    is_high_source_risk,
    is_very_high_source_risk,
    source_risk_buffer_floor_minutes,
    target_wait_minutes_for_confidence,
    vehicle_progress_target_extra_minutes_for_confidence,
)
from route74.domain.prediction_consensus import (
    PredictionCandidate,
    build_prediction_consensus,
    valid_prediction_candidates,
)
from route74.domain.prediction_sources import (
    EVENT_SOURCE_BY_ETA_SOURCE,
    EVALUATED_EVENT_SOURCES,
    PREDICTION_SOURCE_SPECS,
    SOURCE_CORRECTED_LIVE,
    SOURCE_ENSEMBLE,
    SOURCE_HISTORY_HEADWAY,
    SOURCE_TARGET_STOP_LIVE,
    SOURCE_VEHICLE_PROGRESS,
    PredictionSourceSpec,
    validate_evaluated_event_sources,
    validate_prediction_source_specs,
)
from route74.domain.prediction_selection import (
    EARLY_CONFLICT_MINUTES,
    PredictionSelectionCandidate,
    select_prediction_key,
)


def main() -> None:
    selected = select_prediction_key(
        (
            PredictionSelectionCandidate("live", priority=0, arrival_minutes=10, early_conflict_eligible=True),
            PredictionSelectionCandidate("progress", priority=2, arrival_minutes=6, early_conflict_eligible=True),
        )
    )
    _assert_equal(selected, "progress")

    buffered = select_prediction_key(
        (
            PredictionSelectionCandidate(
                "live-buffered",
                priority=0,
                arrival_minutes=10,
                early_conflict_eligible=True,
                safety_wait_minutes=3,
            ),
            PredictionSelectionCandidate("real-earlier", priority=1, arrival_minutes=7, early_conflict_eligible=True),
        )
    )
    _assert_equal(buffered, "real-earlier")

    self_buffered_tie = select_prediction_key(
        (
            PredictionSelectionCandidate(
                "live-buffered",
                priority=0,
                arrival_minutes=10,
                early_conflict_eligible=True,
                safety_wait_minutes=3,
            ),
            PredictionSelectionCandidate(
                "progress-buffered",
                priority=1,
                arrival_minutes=9,
                early_conflict_eligible=True,
                safety_wait_minutes=2,
            ),
        )
    )
    _assert_equal(self_buffered_tie, "live-buffered")

    invalid_eta_ignored = select_prediction_key(
        (
            PredictionSelectionCandidate("invalid-live", priority=0, arrival_minutes=-2, early_conflict_eligible=True),
            PredictionSelectionCandidate(
                "valid-history", priority=3, arrival_minutes=12, early_conflict_eligible=False
            ),
        )
    )
    _assert_equal(invalid_eta_ignored, "valid-history")

    try:
        select_prediction_key(
            (PredictionSelectionCandidate("bad-live", priority=0, arrival_minutes=-2, early_conflict_eligible=True),)
        )
    except ValueError as error:
        _assert_contains(str(error), "non-negative ETA")
    else:
        raise AssertionError("expected invalid ETA candidates to be rejected")

    _assert_candidate_rejects("safety wait", "negative-safety", safety_wait_minutes=-1)
    _assert_candidate_rejects("early conflict", "negative-conflict", early_conflict_minutes=-1)
    _assert_candidate_rejects("quality rank", "negative-rank", quality_rank=-1)
    _assert_candidate_rejects("priority", "bool-priority", priority=True)
    _assert_candidate_rejects("ETA", "float-eta", arrival_minutes=8.5)
    _assert_candidate_rejects("key", "", priority=0)
    _assert_candidate_rejects("key", 123, priority=0)
    _assert_candidate_rejects("eligible", "string-eligible", early_conflict_eligible="yes")
    _assert_rejects(
        lambda: select_prediction_key(
            [PredictionSelectionCandidate("list-item", 0, 8, True)]  # type: ignore[arg-type]
        ),
        "tuple",
    )
    _assert_rejects(
        lambda: select_prediction_key(
            (
                PredictionSelectionCandidate("same", 0, 10, True),
                PredictionSelectionCandidate("same", 1, 6, True),
            )
        ),
        "duplicate prediction selection candidate key",
    )

    _assert_prediction_candidate_rejects("arrival", arrival_minutes=-2)
    _assert_prediction_candidate_rejects("arrival", arrival_minutes=True)
    _assert_prediction_candidate_rejects("source", source="yandex")
    _assert_prediction_candidate_rejects("confidence", confidence="high")
    _assert_prediction_candidate_rejects("known confidence", confidence=EtaConfidence.UNKNOWN)
    _assert_prediction_candidate_rejects("correction", correction_minutes=True)
    _assert_prediction_candidate_rejects("correction scope", correction_scope=None)
    _assert_prediction_candidate_rejects("correction scope", correction_scope="source\nbad")
    _assert_prediction_candidate_rejects("sample count", sample_count=-1)
    _assert_prediction_candidate_rejects("safety wait", safety_wait_minutes=True)
    _assert_prediction_candidate_rejects("reliability sample count", reliability_sample_count=-1)
    _assert_prediction_candidate_rejects("miss rate", miss_rate_percent=101)
    _assert_prediction_candidate_rejects("reliability scope", reliability_scope=" source")
    _assert_prediction_candidate_rejects("diagnostic factors", diagnostic_factors=[])
    _assert_prediction_candidate_rejects("history percentile", history_percentile=101)

    selected_consensus, consensus = build_prediction_consensus(
        (PredictionCandidate(EtaSource.YANDEX, 8, EtaConfidence.HIGH),)
    )
    _assert_equal(selected_consensus.arrival_minutes, 8)
    _assert_equal(consensus.confidence, EtaConfidence.HIGH)
    _assert_equal(consensus.spread_minutes, None)
    _assert_equal(tuple(estimate.arrival_minutes for estimate in consensus.estimates), (8,))
    _assert_equal(consensus.factors, ())
    _assert_equal(consensus.explanations[0].code, EtaExplanationCode.LIVE_ETA)
    _assert_equal(consensus.explanations[0].action, EtaExplanationAction.TRUST_ETA)

    safety_selected, safety_consensus = build_prediction_consensus(
        (
            PredictionCandidate(
                EtaSource.YANDEX,
                8,
                EtaConfidence.HIGH,
                safety_wait_minutes=2,
                reliability_sample_count=12,
                miss_rate_percent=35,
                reliability_scope="source",
            ),
        )
    )
    _assert_equal(safety_selected.arrival_minutes, 8)
    _assert_equal(
        tuple(factor.kind for factor in safety_consensus.factors),
        (EtaFactorKind.SAFETY_BUFFER, EtaFactorKind.SOURCE_RISK),
    )
    _assert_equal(safety_consensus.factors[0].minutes, 2)
    _assert_equal(safety_consensus.factors[0].percent, 35)
    _assert_equal(
        tuple(explanation.code for explanation in safety_consensus.explanations),
        (EtaExplanationCode.LIVE_ETA, EtaExplanationCode.RISK_BUFFER),
    )

    selected_mixed, mixed_consensus = build_prediction_consensus(
        (
            PredictionCandidate(EtaSource.YANDEX, 8, EtaConfidence.HIGH),
            PredictionCandidate(EtaSource.YANDEX_CORRECTED, 10, EtaConfidence.MEDIUM),
            PredictionCandidate(EtaSource.YANDEX_HISTORY, 40, EtaConfidence.LOW),
        )
    )
    _assert_equal(selected_mixed.source, EtaSource.YANDEX)
    _assert_equal(mixed_consensus.spread_minutes, 2)
    _assert_equal(mixed_consensus.factors[0].kind, EtaFactorKind.SPREAD)
    _assert_equal(mixed_consensus.factors[0].minutes, 2)
    _assert_equal(
        tuple((estimate.source, estimate.arrival_minutes) for estimate in mixed_consensus.estimates),
        ((EtaSource.YANDEX, 8), (EtaSource.YANDEX_CORRECTED, 10)),
    )

    _selected_stable, stable_live = build_prediction_consensus(
        (
            PredictionCandidate(EtaSource.YANDEX, 8, EtaConfidence.MEDIUM),
            PredictionCandidate(EtaSource.VEHICLE_PROGRESS, 7, EtaConfidence.LOW, sample_count=2),
        )
    )
    _assert_equal(stable_live.spread_minutes, None)
    _assert_equal(tuple(factor.kind for factor in stable_live.factors), (EtaFactorKind.IGNORED_WEAK_PROGRESS,))
    _assert_equal(stable_live.factors[0].minutes, 1)
    _assert_equal(stable_live.factors[0].sample_count, 2)
    _assert_equal(
        tuple(explanation.code for explanation in stable_live.explanations),
        (EtaExplanationCode.LIVE_ETA, EtaExplanationCode.WEAK_LIVE_IGNORED),
    )

    candidates = valid_prediction_candidates(
        (
            object(),  # type: ignore[arg-type]
            PredictionCandidate(EtaSource.YANDEX_HISTORY, 11, EtaConfidence.LOW),
        )
    )
    _assert_equal(tuple(candidate.arrival_minutes for candidate in candidates), (11,))

    _history_selected, history_consensus = build_prediction_consensus(
        (
            PredictionCandidate(
                EtaSource.YANDEX_HISTORY,
                11,
                EtaConfidence.LOW,
                sample_count=24,
                history_percentile=80,
            ),
        )
    )
    _assert_equal(tuple(factor.kind for factor in history_consensus.factors), (EtaFactorKind.HISTORY_SAMPLE,))
    _assert_equal(history_consensus.factors[0].percent, 80)
    _assert_equal(history_consensus.factors[0].sample_count, 24)
    _assert_equal(history_consensus.explanations[0].code, EtaExplanationCode.HISTORY_FALLBACK)

    _corrected_selected, corrected_consensus = build_prediction_consensus(
        (
            PredictionCandidate(
                EtaSource.YANDEX_CORRECTED,
                9,
                EtaConfidence.MEDIUM,
                correction_minutes=-2,
                correction_scope="bucket",
                sample_count=12,
            ),
        )
    )
    _assert_equal(corrected_consensus.explanations[0].code, EtaExplanationCode.CORRECTED_LIVE)
    _assert_equal(corrected_consensus.explanations[0].action, EtaExplanationAction.KEEP_BUFFER)

    _vehicle_selected, vehicle_consensus = build_prediction_consensus(
        (PredictionCandidate(EtaSource.VEHICLE_PROGRESS, 9, EtaConfidence.MEDIUM, sample_count=5),)
    )
    _assert_equal(vehicle_consensus.explanations[0].code, EtaExplanationCode.VEHICLE_PROGRESS)

    _guarded_selected, guarded_consensus = build_prediction_consensus(
        (
            PredictionCandidate(
                EtaSource.YANDEX_HISTORY,
                14,
                EtaConfidence.LOW,
                diagnostic_factors=(EtaFactor(EtaFactorKind.GUARDRAIL_UNAVAILABLE),),
            ),
        )
    )
    _assert_equal(
        tuple(explanation.code for explanation in guarded_consensus.explanations),
        (EtaExplanationCode.HISTORY_FALLBACK, EtaExplanationCode.STORAGE_GUARDRAIL),
    )

    _ignored_live_selected, ignored_live_consensus = build_prediction_consensus(
        (
            PredictionCandidate(
                EtaSource.YANDEX_HISTORY,
                14,
                EtaConfidence.LOW,
                diagnostic_factors=(EtaFactor(EtaFactorKind.IGNORED_LIVE_ETA, minutes=10, scope="stale"),),
            ),
        )
    )
    _assert_equal(
        tuple(explanation.code for explanation in ignored_live_consensus.explanations),
        (EtaExplanationCode.HISTORY_FALLBACK, EtaExplanationCode.WEAK_LIVE_IGNORED),
    )
    _assert_equal(ignored_live_consensus.explanations[1].detail, "stale")
    _assert_equal(EtaConsensus.disabled().explanations[0].code, EtaExplanationCode.NO_ETA)

    _assert_rejects(lambda: valid_prediction_candidates([candidates[0]]), "tuple")  # type: ignore[arg-type]
    _assert_rejects(
        lambda: build_prediction_consensus((object(),)),  # type: ignore[arg-type]
        "valid candidate",
    )

    _assert_prediction_source_specs()
    _assert_eta_policy_contract()
    print("OK | prediction selection smoke passed")


def _assert_equal(actual: object, expected: object) -> None:
    if actual != expected:
        raise AssertionError(f"expected {expected!r}, got {actual!r}")


def _assert_contains(actual: str, expected: str) -> None:
    if expected not in actual:
        raise AssertionError(f"expected {expected!r} in {actual!r}")


def _assert_candidate_rejects(
    expected: str,
    key: object,
    *,
    priority: object = 0,
    arrival_minutes: object = 10,
    early_conflict_eligible: object = True,
    safety_wait_minutes: object = 0,
    early_conflict_minutes: object = EARLY_CONFLICT_MINUTES,
    quality_rank: object = 0,
) -> None:
    try:
        PredictionSelectionCandidate(
            key,
            priority=priority,  # type: ignore[arg-type]
            arrival_minutes=arrival_minutes,  # type: ignore[arg-type]
            early_conflict_eligible=early_conflict_eligible,  # type: ignore[arg-type]
            safety_wait_minutes=safety_wait_minutes,  # type: ignore[arg-type]
            early_conflict_minutes=early_conflict_minutes,  # type: ignore[arg-type]
            quality_rank=quality_rank,  # type: ignore[arg-type]
        )
    except ValueError as error:
        _assert_contains(str(error), expected)
    else:
        raise AssertionError(f"expected candidate to reject {expected}")


def _assert_prediction_candidate_rejects(
    expected: str,
    *,
    source: object = EtaSource.YANDEX,
    arrival_minutes: object = 8,
    confidence: object = EtaConfidence.HIGH,
    correction_minutes: object = 0,
    correction_scope: object = "",
    sample_count: object = 0,
    safety_wait_minutes: object = 0,
    reliability_sample_count: object = 0,
    miss_rate_percent: object = 0,
    reliability_scope: object = "",
    diagnostic_factors: object = (),
    history_percentile: object = 0,
) -> None:
    _assert_rejects(
        lambda: PredictionCandidate(
            source,  # type: ignore[arg-type]
            arrival_minutes,  # type: ignore[arg-type]
            confidence,  # type: ignore[arg-type]
            correction_minutes=correction_minutes,  # type: ignore[arg-type]
            correction_scope=correction_scope,  # type: ignore[arg-type]
            sample_count=sample_count,  # type: ignore[arg-type]
            safety_wait_minutes=safety_wait_minutes,  # type: ignore[arg-type]
            reliability_sample_count=reliability_sample_count,  # type: ignore[arg-type]
            miss_rate_percent=miss_rate_percent,  # type: ignore[arg-type]
            reliability_scope=reliability_scope,  # type: ignore[arg-type]
            diagnostic_factors=diagnostic_factors,  # type: ignore[arg-type]
            history_percentile=history_percentile,  # type: ignore[arg-type]
        ),
        expected,
    )


def _assert_prediction_source_specs() -> None:
    _assert_equal(set(EVENT_SOURCE_BY_ETA_SOURCE), set(EtaSource))
    _assert_equal(EVENT_SOURCE_BY_ETA_SOURCE[EtaSource.YANDEX], SOURCE_TARGET_STOP_LIVE)
    _assert_equal(EVENT_SOURCE_BY_ETA_SOURCE[EtaSource.YANDEX_CORRECTED], SOURCE_CORRECTED_LIVE)
    _assert_equal(
        EVALUATED_EVENT_SOURCES,
        (
            SOURCE_CORRECTED_LIVE,
            SOURCE_TARGET_STOP_LIVE,
            SOURCE_VEHICLE_PROGRESS,
            SOURCE_HISTORY_HEADWAY,
            SOURCE_ENSEMBLE,
        ),
    )
    _assert_rejects(
        lambda: PredictionSourceSpec(
            EtaSource.YANDEX,
            "   ",
            priority=0,
            is_live=True,
            early_conflict_eligible=True,
        ),
        "event source",
    )
    _assert_rejects(
        lambda: PredictionSourceSpec(
            EtaSource.YANDEX,
            "target-stop-live",
            priority=0,
            is_live=True,
            early_conflict_eligible=True,
        ),
        "plain key",
    )
    _assert_rejects(
        lambda: PredictionSourceSpec(
            EtaSource.YANDEX,
            "яндекс",
            priority=0,
            is_live=True,
            early_conflict_eligible=True,
        ),
        "plain key",
    )
    _assert_rejects(
        lambda: validate_prediction_source_specs(list(PREDICTION_SOURCE_SPECS)),  # type: ignore[arg-type]
        "tuple",
    )
    _assert_rejects(
        lambda: validate_prediction_source_specs((*PREDICTION_SOURCE_SPECS[:1], object())),  # type: ignore[arg-type]
        "PredictionSourceSpec",
    )
    duplicate = (
        *PREDICTION_SOURCE_SPECS[:1],
        PredictionSourceSpec(
            EtaSource.YANDEX,
            SOURCE_CORRECTED_LIVE,
            priority=0,
            is_live=True,
            early_conflict_eligible=True,
        ),
    )
    _assert_rejects(lambda: validate_prediction_source_specs(duplicate), "duplicate prediction event source")
    _assert_rejects(
        lambda: validate_evaluated_event_sources(
            list(EVALUATED_EVENT_SOURCES),  # type: ignore[arg-type]
            PREDICTION_SOURCE_SPECS,
        ),
        "tuple",
    )
    _assert_rejects(
        lambda: validate_evaluated_event_sources(
            (
                SOURCE_CORRECTED_LIVE,
                SOURCE_CORRECTED_LIVE,
                SOURCE_TARGET_STOP_LIVE,
                SOURCE_VEHICLE_PROGRESS,
                SOURCE_HISTORY_HEADWAY,
                SOURCE_ENSEMBLE,
            ),
            PREDICTION_SOURCE_SPECS,
        ),
        "duplicate evaluated event source",
    )
    _assert_rejects(
        lambda: validate_evaluated_event_sources(
            (
                SOURCE_CORRECTED_LIVE,
                SOURCE_TARGET_STOP_LIVE,
                SOURCE_VEHICLE_PROGRESS,
                SOURCE_ENSEMBLE,
            ),
            PREDICTION_SOURCE_SPECS,
        ),
        "missing evaluated event sources",
    )
    _assert_rejects(
        lambda: validate_evaluated_event_sources(
            (
                SOURCE_CORRECTED_LIVE,
                SOURCE_TARGET_STOP_LIVE,
                SOURCE_VEHICLE_PROGRESS,
                SOURCE_HISTORY_HEADWAY,
                "experimental",
                SOURCE_ENSEMBLE,
            ),
            PREDICTION_SOURCE_SPECS,
        ),
        "unknown evaluated event sources",
    )
    _assert_rejects(
        lambda: validate_evaluated_event_sources(
            (
                SOURCE_CORRECTED_LIVE,
                "source-with-dash",
                SOURCE_TARGET_STOP_LIVE,
                SOURCE_VEHICLE_PROGRESS,
                SOURCE_HISTORY_HEADWAY,
                SOURCE_ENSEMBLE,
            ),
            PREDICTION_SOURCE_SPECS,
        ),
        "plain key",
    )
    _assert_rejects(
        lambda: validate_evaluated_event_sources(
            (
                SOURCE_ENSEMBLE,
                SOURCE_CORRECTED_LIVE,
                SOURCE_TARGET_STOP_LIVE,
                SOURCE_VEHICLE_PROGRESS,
                SOURCE_HISTORY_HEADWAY,
            ),
            PREDICTION_SOURCE_SPECS,
        ),
        "ensemble source must be last",
    )
    _assert_rejects(
        lambda: validate_evaluated_event_sources(
            (
                SOURCE_TARGET_STOP_LIVE,
                SOURCE_CORRECTED_LIVE,
                SOURCE_VEHICLE_PROGRESS,
                SOURCE_HISTORY_HEADWAY,
                SOURCE_ENSEMBLE,
            ),
            PREDICTION_SOURCE_SPECS,
        ),
        "prediction priority",
    )


def _assert_eta_policy_contract() -> None:
    _assert_rejects(lambda: replace(DEFAULT_ETA_POLICY, high_spread_minutes=True), "high spread")
    _assert_rejects(
        lambda: replace(
            DEFAULT_ETA_POLICY,
            medium_spread_minutes=DEFAULT_ETA_POLICY.high_spread_minutes,
        ),
        "medium spread",
    )
    _assert_rejects(
        lambda: replace(
            DEFAULT_ETA_POLICY,
            low_target_wait_minutes=DEFAULT_ETA_POLICY.high_target_wait_minutes - 1,
        ),
        "target wait",
    )
    _assert_rejects(
        lambda: replace(
            DEFAULT_ETA_POLICY,
            source_risk_very_high_miss_rate_percent=DEFAULT_ETA_POLICY.source_risk_high_miss_rate_percent,
        ),
        "very high source risk miss rate",
    )
    _assert_rejects(
        lambda: replace(
            DEFAULT_ETA_POLICY,
            vehicle_progress_low_target_extra_minutes=(
                DEFAULT_ETA_POLICY.vehicle_progress_medium_target_extra_minutes - 1
            ),
        ),
        "vehicle progress extra",
    )
    _assert_rejects(
        lambda: replace(
            DEFAULT_ETA_POLICY,
            source_risk_very_high_min_buffer_minutes=(DEFAULT_ETA_POLICY.source_risk_high_min_buffer_minutes - 1),
        ),
        "very high source risk buffer",
    )
    _assert_equal(
        target_wait_minutes_for_confidence(EtaConfidence.HIGH),
        DEFAULT_ETA_POLICY.high_target_wait_minutes,
    )
    _assert_equal(
        target_wait_minutes_for_confidence(EtaConfidence.MEDIUM),
        DEFAULT_ETA_POLICY.medium_target_wait_minutes,
    )
    _assert_equal(
        target_wait_minutes_for_confidence(EtaConfidence.LOW),
        DEFAULT_ETA_POLICY.low_target_wait_minutes,
    )
    _assert_equal(
        target_wait_minutes_for_confidence(EtaConfidence.UNKNOWN),
        DEFAULT_ETA_POLICY.low_target_wait_minutes,
    )
    _assert_equal(
        vehicle_progress_target_extra_minutes_for_confidence(EtaConfidence.HIGH),
        0,
    )
    _assert_equal(
        vehicle_progress_target_extra_minutes_for_confidence(EtaConfidence.MEDIUM),
        DEFAULT_ETA_POLICY.vehicle_progress_medium_target_extra_minutes,
    )
    _assert_equal(
        vehicle_progress_target_extra_minutes_for_confidence(EtaConfidence.LOW),
        DEFAULT_ETA_POLICY.vehicle_progress_low_target_extra_minutes,
    )
    high_risk = DEFAULT_ETA_POLICY.source_risk_high_miss_rate_percent
    very_high_risk = DEFAULT_ETA_POLICY.source_risk_very_high_miss_rate_percent
    _assert_equal(is_high_source_risk(high_risk - 1), False)
    _assert_equal(is_high_source_risk(high_risk), True)
    _assert_equal(is_very_high_source_risk(very_high_risk - 1), False)
    _assert_equal(is_very_high_source_risk(very_high_risk), True)
    _assert_equal(source_risk_buffer_floor_minutes(high_risk - 1), 0)
    _assert_equal(
        source_risk_buffer_floor_minutes(high_risk),
        DEFAULT_ETA_POLICY.source_risk_high_min_buffer_minutes,
    )
    _assert_equal(
        source_risk_buffer_floor_minutes(very_high_risk),
        DEFAULT_ETA_POLICY.source_risk_very_high_min_buffer_minutes,
    )
    _assert_rejects(
        lambda: target_wait_minutes_for_confidence("high"),  # type: ignore[arg-type]
        "ETA confidence",
    )
    _assert_rejects(
        lambda: target_wait_minutes_for_confidence(EtaConfidence.HIGH, policy=object()),  # type: ignore[arg-type]
        "ETA policy",
    )
    _assert_rejects(lambda: source_risk_buffer_floor_minutes(True), "source risk miss rate")


def _assert_rejects(action: Callable[[], object], expected: str) -> None:
    try:
        action()
    except ValueError as error:
        _assert_contains(str(error), expected)
    else:
        raise AssertionError(f"expected rejection containing {expected!r}")


if __name__ == "__main__":
    main()
