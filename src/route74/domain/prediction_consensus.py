from __future__ import annotations

from dataclasses import dataclass

from route74.domain.eta import (
    EtaConfidence,
    EtaConsensus,
    EtaEstimate,
    EtaExplanation,
    EtaExplanationAction,
    EtaExplanationCode,
    EtaFactor,
    EtaFactorKind,
    EtaSource,
    eta_scope_text,
)
from route74.domain.eta_policy import (
    HIGH_SPREAD_MINUTES,
    HISTORY_TARGET_WAIT_MINUTES,
    MEDIUM_SPREAD_MINUTES,
    is_high_source_risk,
    is_very_high_source_risk,
    target_wait_minutes_for_confidence,
    vehicle_progress_target_extra_minutes_for_confidence,
)
from route74.domain.prediction_selection import (
    EARLY_CONFLICT_MINUTES,
    LOW_CONFIDENCE_EARLY_CONFLICT_MINUTES,
    MEDIUM_CONFIDENCE_EARLY_CONFLICT_MINUTES,
    PredictionSelectionCandidate,
    select_prediction_key,
)
from route74.domain.prediction_sources import (
    EARLY_CONFLICT_EVENT_SOURCES,
    EARLY_CONFLICT_ETA_SOURCES,
    ETA_SOURCE_BY_EVENT_SOURCE,
    EVENT_SOURCE_PRIORITY,
    LIVE_ETA_SOURCES,
    SOURCE_PRIORITY_BY_ETA_SOURCE,
    SOURCE_VEHICLE_PROGRESS,
)


@dataclass(frozen=True)
class PredictionCandidate:
    source: EtaSource
    arrival_minutes: int
    confidence: EtaConfidence
    correction_minutes: int = 0
    correction_scope: str = ""
    sample_count: int = 0
    safety_wait_minutes: int = 0
    reliability_sample_count: int = 0
    miss_rate_percent: int = 0
    reliability_scope: str = ""
    diagnostic_factors: tuple[EtaFactor, ...] = ()
    history_percentile: int = 0

    def __post_init__(self) -> None:
        _validate_prediction_candidate(self)


def build_prediction_consensus(
    candidates: tuple[PredictionCandidate, ...],
) -> tuple[PredictionCandidate, EtaConsensus]:
    valid_candidates = valid_prediction_candidates(candidates)
    if not valid_candidates:
        raise ValueError("prediction consensus needs at least one valid candidate")
    selected = select_prediction_candidate(valid_candidates)
    return selected, consensus_from_candidates(valid_candidates, selected)


def select_prediction_candidate(candidates: tuple[PredictionCandidate, ...]) -> PredictionCandidate:
    keyed = {str(index): candidate for index, candidate in enumerate(candidates)}
    selected_key = select_prediction_key(tuple(_selection_candidate(key, candidate) for key, candidate in keyed.items()))
    return keyed[selected_key]


def consensus_from_candidates(
    candidates: tuple[PredictionCandidate, ...],
    selected: PredictionCandidate,
) -> EtaConsensus:
    estimate_candidates = _estimate_candidates(candidates, selected)
    estimates = tuple(EtaEstimate(candidate.source, candidate.arrival_minutes) for candidate in estimate_candidates)
    spread = _spread(estimate_candidates)
    confidence = _confidence(selected, spread)
    return EtaConsensus(
        selected_source=selected.source,
        arrival_minutes=selected.arrival_minutes,
        confidence=confidence,
        target_wait_minutes=_target_wait(selected, confidence),
        spread_minutes=spread,
        warning=_warning(confidence, spread, candidates, selected),
        estimates=estimates,
        factors=_factors(candidates, estimate_candidates, selected, confidence, spread),
        explanations=_explanations(candidates, selected, confidence),
    )


def _selection_candidate(key: str, candidate: PredictionCandidate) -> PredictionSelectionCandidate:
    return prediction_selection_candidate_for_eta_source(
        key=key,
        source=candidate.source,
        arrival_minutes=candidate.arrival_minutes,
        confidence=candidate.confidence,
        safety_wait_minutes=candidate.safety_wait_minutes,
    )


def valid_prediction_candidates(candidates: tuple[PredictionCandidate, ...]) -> tuple[PredictionCandidate, ...]:
    if not isinstance(candidates, tuple):
        raise ValueError("prediction candidates need tuple")
    return tuple(
        candidate
        for candidate in candidates
        if isinstance(candidate, PredictionCandidate) and _valid_prediction_candidate(candidate)
    )


def _validate_prediction_candidate(candidate: PredictionCandidate) -> None:
    if not isinstance(candidate.source, EtaSource):
        raise ValueError("prediction candidate source needs EtaSource")
    if not _valid_non_negative_int(candidate.arrival_minutes):
        raise ValueError("prediction candidate arrival minutes need non-negative integer")
    if not isinstance(candidate.confidence, EtaConfidence):
        raise ValueError("prediction candidate confidence needs EtaConfidence")
    if candidate.confidence == EtaConfidence.UNKNOWN:
        raise ValueError("prediction candidate confidence needs known confidence")
    if not _valid_int(candidate.correction_minutes):
        raise ValueError("prediction candidate correction minutes need integer")
    if not _valid_scope_text(candidate.correction_scope):
        raise ValueError("prediction candidate correction scope needs compact single-line text")
    if not _valid_non_negative_int(candidate.sample_count):
        raise ValueError("prediction candidate sample count needs non-negative integer")
    if not _valid_non_negative_int(candidate.safety_wait_minutes):
        raise ValueError("prediction candidate safety wait needs non-negative integer")
    if not _valid_non_negative_int(candidate.reliability_sample_count):
        raise ValueError("prediction candidate reliability sample count needs non-negative integer")
    if not _valid_percent(candidate.miss_rate_percent):
        raise ValueError("prediction candidate miss rate needs percent")
    if not _valid_scope_text(candidate.reliability_scope):
        raise ValueError("prediction candidate reliability scope needs compact single-line text")
    if not isinstance(candidate.diagnostic_factors, tuple) or any(
        not isinstance(factor, EtaFactor) for factor in candidate.diagnostic_factors
    ):
        raise ValueError("prediction candidate diagnostic factors need tuple of EtaFactor")
    if not _valid_percent(candidate.history_percentile):
        raise ValueError("prediction candidate history percentile needs percent")


def _valid_prediction_candidate(candidate: PredictionCandidate) -> bool:
    return (
        isinstance(candidate.source, EtaSource)
        and isinstance(candidate.confidence, EtaConfidence)
        and candidate.confidence != EtaConfidence.UNKNOWN
        and _valid_non_negative_int(candidate.arrival_minutes)
        and _valid_non_negative_int(candidate.safety_wait_minutes)
        and _valid_non_negative_int(candidate.sample_count)
        and _valid_non_negative_int(candidate.reliability_sample_count)
        and _valid_percent(candidate.miss_rate_percent)
        and _valid_int(candidate.correction_minutes)
        and _valid_scope_text(candidate.correction_scope)
        and _valid_scope_text(candidate.reliability_scope)
        and isinstance(candidate.diagnostic_factors, tuple)
        and all(isinstance(factor, EtaFactor) for factor in candidate.diagnostic_factors)
        and _valid_percent(candidate.history_percentile)
    )


def prediction_selection_candidate_for_event_source(
    *,
    key: str,
    source: str,
    arrival_minutes: int,
    confidence: EtaConfidence,
    safety_wait_minutes: int = 0,
) -> PredictionSelectionCandidate:
    return PredictionSelectionCandidate(
        key=key,
        priority=EVENT_SOURCE_PRIORITY[source],
        arrival_minutes=arrival_minutes,
        early_conflict_eligible=source in EARLY_CONFLICT_EVENT_SOURCES,
        safety_wait_minutes=safety_wait_minutes,
        early_conflict_minutes=early_conflict_minutes_for_event_source(
            source,
            confidence,
            safety_wait_minutes=safety_wait_minutes,
        ),
        quality_rank=selection_quality_rank_for_event_source(source, confidence),
    )


def prediction_selection_candidate_for_eta_source(
    *,
    key: str,
    source: EtaSource,
    arrival_minutes: int,
    confidence: EtaConfidence,
    safety_wait_minutes: int = 0,
) -> PredictionSelectionCandidate:
    return PredictionSelectionCandidate(
        key=key,
        priority=SOURCE_PRIORITY_BY_ETA_SOURCE[source],
        arrival_minutes=arrival_minutes,
        early_conflict_eligible=source in EARLY_CONFLICT_ETA_SOURCES,
        safety_wait_minutes=safety_wait_minutes,
        early_conflict_minutes=early_conflict_minutes_for_eta_source(
            source,
            confidence,
            safety_wait_minutes=safety_wait_minutes,
        ),
        quality_rank=selection_quality_rank_for_eta_source(source, confidence),
    )


def early_conflict_minutes_for_event_source(
    source: str,
    confidence: EtaConfidence,
    *,
    safety_wait_minutes: int,
) -> int:
    eta_source = ETA_SOURCE_BY_EVENT_SOURCE.get(source)
    if eta_source is None:
        return EARLY_CONFLICT_MINUTES
    return early_conflict_minutes_for_eta_source(
        eta_source,
        confidence,
        safety_wait_minutes=safety_wait_minutes,
    )


def selection_quality_rank_for_event_source(source: str, confidence: EtaConfidence) -> int:
    if source != SOURCE_VEHICLE_PROGRESS:
        return 0
    return _vehicle_progress_quality_rank(confidence)


def selection_quality_rank_for_eta_source(source: EtaSource, confidence: EtaConfidence) -> int:
    if source != EtaSource.VEHICLE_PROGRESS:
        return 0
    return _vehicle_progress_quality_rank(confidence)


def _vehicle_progress_quality_rank(confidence: EtaConfidence) -> int:
    if confidence in {EtaConfidence.HIGH, EtaConfidence.MEDIUM}:
        return 0
    return 1


def early_conflict_minutes_for_eta_source(
    source: EtaSource,
    confidence: EtaConfidence,
    *,
    safety_wait_minutes: int,
) -> int:
    if source not in LIVE_ETA_SOURCES or confidence == EtaConfidence.UNKNOWN:
        return EARLY_CONFLICT_MINUTES
    if confidence == EtaConfidence.LOW or safety_wait_minutes >= 3:
        return LOW_CONFIDENCE_EARLY_CONFLICT_MINUTES
    if confidence == EtaConfidence.MEDIUM or safety_wait_minutes > 0:
        return MEDIUM_CONFIDENCE_EARLY_CONFLICT_MINUTES
    return EARLY_CONFLICT_MINUTES


def _confidence(candidate: PredictionCandidate, spread: int | None) -> EtaConfidence:
    if candidate.source == EtaSource.YANDEX_HISTORY:
        return EtaConfidence.LOW
    if is_very_high_source_risk(candidate.miss_rate_percent):
        return EtaConfidence.LOW
    if candidate.safety_wait_minutes >= 3:
        return EtaConfidence.LOW
    if is_high_source_risk(candidate.miss_rate_percent):
        return EtaConfidence.MEDIUM if candidate.confidence == EtaConfidence.HIGH else EtaConfidence.LOW
    if candidate.safety_wait_minutes > 0 and candidate.confidence == EtaConfidence.HIGH:
        return EtaConfidence.MEDIUM
    if spread is not None and spread > MEDIUM_SPREAD_MINUTES:
        return EtaConfidence.LOW
    if spread is not None and spread > HIGH_SPREAD_MINUTES:
        return EtaConfidence.MEDIUM
    return candidate.confidence


def _target_wait(candidate: PredictionCandidate, confidence: EtaConfidence) -> int:
    source = candidate.source
    if source == EtaSource.YANDEX_HISTORY:
        return HISTORY_TARGET_WAIT_MINUTES + candidate.safety_wait_minutes
    if source == EtaSource.VEHICLE_PROGRESS:
        return (
            target_wait_minutes_for_confidence(EtaConfidence.LOW)
            + candidate.safety_wait_minutes
            + _vehicle_progress_target_extra_minutes(candidate, confidence)
        )
    return target_wait_minutes_for_confidence(confidence) + candidate.safety_wait_minutes


def _warning(
    confidence: EtaConfidence,
    spread: int | None,
    candidates: tuple[PredictionCandidate, ...],
    selected: PredictionCandidate,
) -> str:
    if len(candidates) < 2 or spread is None:
        if selected.safety_wait_minutes:
            return f"{_safety_scope_prefix(selected)}добавил запас {selected.safety_wait_minutes} мин"
        if _vehicle_progress_target_extra_minutes(selected, confidence):
            return _vehicle_progress_warning(selected, confidence)
        if selected.correction_minutes < 0:
            return _correction_warning(selected)
        if confidence == EtaConfidence.LOW and selected.source in LIVE_ETA_SOURCES:
            return "Яндекс дал слабый ETA, держу запас"
        return ""
    if selected.safety_wait_minutes:
        return f"источники спорят, {_safety_scope_prefix(selected)}добавил запас {selected.safety_wait_minutes} мин"
    if _vehicle_progress_target_extra_minutes(selected, confidence):
        return f"источники спорят, {_vehicle_progress_warning(selected, confidence)}"
    if selected.correction_minutes < 0:
        return _correction_warning(selected)
    if confidence == EtaConfidence.LOW and selected.source in LIVE_ETA_SOURCES:
        return "источники спорят, доверяю прямому ETA и держу запас"
    if confidence == EtaConfidence.LOW:
        return "источники спорят, проверь карту"
    if confidence == EtaConfidence.MEDIUM:
        return "источники немного расходятся"
    return ""


def _vehicle_progress_target_extra_minutes(
    candidate: PredictionCandidate,
    confidence: EtaConfidence | None = None,
) -> int:
    if candidate.source != EtaSource.VEHICLE_PROGRESS:
        return 0
    return vehicle_progress_target_extra_minutes_for_confidence(confidence or candidate.confidence)


def _vehicle_progress_warning(candidate: PredictionCandidate, confidence: EtaConfidence) -> str:
    extra = _vehicle_progress_target_extra_minutes(candidate, confidence)
    return f"координатный прогноз, держу запас {extra} мин"


def _factors(
    candidates: tuple[PredictionCandidate, ...],
    estimate_candidates: tuple[PredictionCandidate, ...],
    selected: PredictionCandidate,
    confidence: EtaConfidence,
    spread: int | None,
) -> tuple[EtaFactor, ...]:
    factors: list[EtaFactor] = []
    if selected.correction_minutes < 0:
        factors.append(
            EtaFactor(
                EtaFactorKind.RESIDUAL_CORRECTION,
                minutes=abs(selected.correction_minutes),
                sample_count=selected.sample_count,
                scope=_candidate_scope(selected.correction_scope, selected.source),
            )
        )
    if selected.safety_wait_minutes:
        factors.append(
            EtaFactor(
                EtaFactorKind.SAFETY_BUFFER,
                minutes=selected.safety_wait_minutes,
                sample_count=selected.reliability_sample_count,
                percent=selected.miss_rate_percent,
                scope=_candidate_scope(selected.reliability_scope, selected.source),
            )
        )
    factors.extend(selected.diagnostic_factors)
    if is_high_source_risk(selected.miss_rate_percent):
        factors.append(
            EtaFactor(
                EtaFactorKind.SOURCE_RISK,
                sample_count=selected.reliability_sample_count,
                percent=selected.miss_rate_percent,
                scope=_candidate_scope(selected.reliability_scope, selected.source),
            )
        )
    extra = _vehicle_progress_target_extra_minutes(selected, confidence)
    if extra:
        factors.append(
            EtaFactor(
                EtaFactorKind.VEHICLE_PROGRESS_BUFFER,
                minutes=extra,
                sample_count=selected.sample_count,
            )
        )
    if spread is not None:
        factors.append(
            EtaFactor(
                EtaFactorKind.SPREAD,
                minutes=spread,
                sample_count=len(estimate_candidates),
            )
        )
    if selected.source == EtaSource.YANDEX_HISTORY and selected.sample_count:
        factors.append(
            EtaFactor(
                EtaFactorKind.HISTORY_SAMPLE,
                sample_count=selected.sample_count,
                percent=selected.history_percentile,
            )
        )
    history_disagreement = _history_disagreement_factor(candidates, selected)
    if history_disagreement is not None:
        factors.append(history_disagreement)
    ignored_progress = _ignored_weak_progress_candidate(candidates, selected)
    if ignored_progress is not None:
        factors.append(
            EtaFactor(
                EtaFactorKind.IGNORED_WEAK_PROGRESS,
                minutes=selected.arrival_minutes - ignored_progress.arrival_minutes,
                sample_count=ignored_progress.sample_count,
                scope=ignored_progress.source.value,
            )
        )
    return tuple(factors)


def _explanations(
    candidates: tuple[PredictionCandidate, ...],
    selected: PredictionCandidate,
    confidence: EtaConfidence,
) -> tuple[EtaExplanation, ...]:
    explanations = [_selected_source_explanation(selected, confidence)]
    if selected.safety_wait_minutes:
        explanations.append(
            EtaExplanation(
                EtaExplanationCode.RISK_BUFFER,
                EtaExplanationAction.KEEP_BUFFER,
                detail=_candidate_scope(selected.reliability_scope, selected.source),
            )
        )
    if any(factor.kind == EtaFactorKind.GUARDRAIL_UNAVAILABLE for factor in selected.diagnostic_factors):
        explanations.append(
            EtaExplanation(
                EtaExplanationCode.STORAGE_GUARDRAIL,
                EtaExplanationAction.CHECK_MAP,
                detail=PREDICTION_STORAGE_GUARDRAIL_DETAIL,
            )
        )
    ignored_live_eta = _first_factor(selected.diagnostic_factors, EtaFactorKind.IGNORED_LIVE_ETA)
    if ignored_live_eta is not None:
        explanations.append(
            EtaExplanation(
                EtaExplanationCode.WEAK_LIVE_IGNORED,
                EtaExplanationAction.CHECK_MAP,
                detail=ignored_live_eta.scope,
            )
        )
    if _ignored_weak_progress_candidate(candidates, selected) is not None:
        explanations.append(
            EtaExplanation(
                EtaExplanationCode.WEAK_LIVE_IGNORED,
                EtaExplanationAction.TRUST_ETA,
                detail=EtaSource.VEHICLE_PROGRESS.value,
            )
        )
    return tuple(_dedupe_explanations(explanations))


PREDICTION_STORAGE_GUARDRAIL_DETAIL = "prediction_storage_unavailable"


def _selected_source_explanation(
    selected: PredictionCandidate,
    confidence: EtaConfidence,
) -> EtaExplanation:
    if selected.source == EtaSource.YANDEX:
        action = EtaExplanationAction.CHECK_MAP if confidence == EtaConfidence.LOW else EtaExplanationAction.TRUST_ETA
        return EtaExplanation(EtaExplanationCode.LIVE_ETA, action, detail=selected.source.value)
    if selected.source == EtaSource.YANDEX_CORRECTED:
        return EtaExplanation(
            EtaExplanationCode.CORRECTED_LIVE,
            EtaExplanationAction.KEEP_BUFFER,
            detail=_candidate_scope(selected.correction_scope, selected.source),
        )
    if selected.source == EtaSource.VEHICLE_PROGRESS:
        return EtaExplanation(
            EtaExplanationCode.VEHICLE_PROGRESS,
            EtaExplanationAction.KEEP_BUFFER,
            detail=selected.source.value,
        )
    return EtaExplanation(
        EtaExplanationCode.HISTORY_FALLBACK,
        EtaExplanationAction.CHECK_MAP,
        detail=selected.source.value,
    )


def _dedupe_explanations(
    explanations: list[EtaExplanation],
) -> tuple[EtaExplanation, ...]:
    seen: set[tuple[EtaExplanationCode, EtaExplanationAction, str]] = set()
    result: list[EtaExplanation] = []
    for explanation in explanations:
        key = (explanation.code, explanation.action, explanation.detail)
        if key in seen:
            continue
        seen.add(key)
        result.append(explanation)
    return tuple(result)


def _first_factor(
    factors: tuple[EtaFactor, ...],
    kind: EtaFactorKind,
) -> EtaFactor | None:
    return next((factor for factor in factors if factor.kind == kind), None)


def _history_disagreement_factor(
    candidates: tuple[PredictionCandidate, ...],
    selected: PredictionCandidate,
) -> EtaFactor | None:
    if selected.source == EtaSource.YANDEX_HISTORY:
        return None
    history_candidates = tuple(candidate for candidate in candidates if candidate.source == EtaSource.YANDEX_HISTORY)
    if not history_candidates:
        return None
    history = min(
        history_candidates,
        key=lambda candidate: abs(candidate.arrival_minutes - selected.arrival_minutes),
    )
    delta = selected.arrival_minutes - history.arrival_minutes
    if abs(delta) <= HIGH_SPREAD_MINUTES:
        return None
    scope = "history_earlier" if delta > 0 else "history_later"
    return EtaFactor(
        EtaFactorKind.HISTORY_DISAGREEMENT,
        minutes=abs(delta),
        sample_count=history.sample_count,
        scope=scope,
    )


def _ignored_weak_progress_candidate(
    candidates: tuple[PredictionCandidate, ...],
    selected: PredictionCandidate,
) -> PredictionCandidate | None:
    if selected.source == EtaSource.VEHICLE_PROGRESS:
        return None
    ignored = tuple(
        candidate
        for candidate in candidates
        if candidate.source == EtaSource.VEHICLE_PROGRESS
        and candidate.confidence == EtaConfidence.LOW
        and candidate.arrival_minutes < selected.arrival_minutes
        and not _candidate_counts_for_spread(candidate, selected)
    )
    if not ignored:
        return None
    return min(ignored, key=lambda candidate: candidate.arrival_minutes)


def _safety_scope_prefix(candidate: PredictionCandidate) -> str:
    scope = _candidate_scope(candidate.reliability_scope, candidate.source)
    if _scope_base(scope) == "live_eta_no_coordinates":
        return "короткий ETA без координаты машины: "
    scope_text = eta_scope_text(scope)
    return f"{scope_text} " if scope_text else "по прошлым ошибкам "


def _correction_warning(candidate: PredictionCandidate) -> str:
    scope_text = eta_scope_text(_candidate_scope(candidate.correction_scope, candidate.source)) or "по прошлым ошибкам"
    return f"ETA сдвинут на {abs(candidate.correction_minutes)} мин раньше {scope_text}"


def _candidate_scope(scope: str, source: EtaSource) -> str:
    if not scope or ":" in scope or scope == "live_eta_no_coordinates":
        return scope
    return f"{scope}:{source.value}"


def _scope_base(scope: str) -> str:
    return scope.split(":", 1)[0]


def _spread(candidates: tuple[PredictionCandidate, ...]) -> int | None:
    if len(candidates) < 2:
        return None
    values = [candidate.arrival_minutes for candidate in candidates]
    return max(values) - min(values)


def _spread_candidates(
    candidates: tuple[PredictionCandidate, ...],
    selected: PredictionCandidate,
) -> tuple[PredictionCandidate, ...]:
    if selected.source == EtaSource.YANDEX_HISTORY:
        return (selected,)
    return tuple(
        candidate
        for candidate in candidates
        if candidate.source != EtaSource.YANDEX_HISTORY
        and _candidate_counts_for_spread(candidate, selected)
    )


def _estimate_candidates(
    candidates: tuple[PredictionCandidate, ...],
    selected: PredictionCandidate,
) -> tuple[PredictionCandidate, ...]:
    spread_candidates = _spread_candidates(candidates, selected)
    if len(spread_candidates) >= 2:
        return spread_candidates
    return (selected,)


def _candidate_counts_for_spread(candidate: PredictionCandidate, selected: PredictionCandidate) -> bool:
    if candidate is selected:
        return True
    if candidate.source == EtaSource.VEHICLE_PROGRESS and candidate.confidence == EtaConfidence.LOW:
        return False
    return True


def _valid_non_negative_int(value: object) -> bool:
    return _valid_int(value) and value >= 0


def _valid_percent(value: object) -> bool:
    return _valid_non_negative_int(value) and value <= 100


def _valid_int(value: object) -> bool:
    return not isinstance(value, bool) and isinstance(value, int)


def _valid_scope_text(value: object) -> bool:
    return (
        isinstance(value, str)
        and value == value.strip()
        and "\n" not in value
        and "\r" not in value
    )
