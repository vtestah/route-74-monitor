from __future__ import annotations

from route74.domain.commute import DepartureDecision, DepartureSource
from route74.domain.eta import EtaConfidence


LIVE_DEPARTURE_SOURCES = frozenset({
    DepartureSource.YANDEX,
    DepartureSource.YANDEX_CORRECTED,
    DepartureSource.VEHICLE_PROGRESS,
})
LOW_TRUST_DEPARTURE_SOURCES = frozenset({
    *LIVE_DEPARTURE_SOURCES,
    DepartureSource.YANDEX_HISTORY,
})
LOW_TRUST_SAFE_MARGIN_MINUTES = 5


def validate_departure_safety_policy(
    *,
    live_sources: object,
    low_trust_sources: object,
    safe_margin_minutes: object,
) -> None:
    live = _ensure_source_set("live departure sources", live_sources)
    low_trust = _ensure_source_set("low-trust departure sources", low_trust_sources)
    if not live:
        raise ValueError("live departure sources must not be empty")
    if DepartureSource.YANDEX_HISTORY in live:
        raise ValueError("history source must not be a live departure source")
    if DepartureSource.NONE in live:
        raise ValueError("no-data source must not be a live departure source")
    if not live.issubset(low_trust):
        raise ValueError("low-trust departure sources must include live sources")
    if DepartureSource.YANDEX_HISTORY not in low_trust:
        raise ValueError("low-trust departure sources must include history")
    if DepartureSource.NONE in low_trust:
        raise ValueError("no-data source must not require safe margin")
    if isinstance(safe_margin_minutes, bool) or not isinstance(safe_margin_minutes, int):
        raise ValueError("low-trust safe margin must be an integer")
    if safe_margin_minutes <= 0:
        raise ValueError("low-trust safe margin must be positive")


def unsafe_arrival_without_safe_margin(decision: DepartureDecision) -> bool:
    if not _needs_safe_margin(decision):
        return False
    physical_margin = physical_catch_margin_minutes(decision)
    safe_margin = safe_catch_margin_minutes(decision)
    return physical_margin is not None and physical_margin >= 0 and safe_margin is not None and safe_margin < 0


def physical_catch_margin_minutes(decision: DepartureDecision) -> int | None:
    arrival_minutes = _valid_minutes(decision.arrival_in_minutes)
    walk_minutes = _valid_minutes(decision.walk_minutes)
    if arrival_minutes is None or walk_minutes is None:
        return None
    return arrival_minutes - walk_minutes


def safe_catch_margin_minutes(decision: DepartureDecision) -> int | None:
    arrival_minutes = _valid_minutes(decision.arrival_in_minutes)
    walk_minutes = _valid_minutes(decision.walk_minutes)
    target_wait_minutes = _target_wait_minutes(decision)
    if arrival_minutes is None or walk_minutes is None or target_wait_minutes is None:
        return None
    return arrival_minutes - walk_minutes - target_wait_minutes


def missed_by_minutes(decision: DepartureDecision) -> int | None:
    physical_margin = physical_catch_margin_minutes(decision)
    if physical_margin is None:
        return None
    if physical_margin < 0:
        return abs(physical_margin)
    if unsafe_arrival_without_safe_margin(decision):
        safe_margin = safe_catch_margin_minutes(decision)
        return abs(safe_margin or 0)
    return None


def _needs_safe_margin(decision: DepartureDecision) -> bool:
    source = getattr(decision, "source", None)
    if source not in LOW_TRUST_DEPARTURE_SOURCES:
        return False
    eta_consensus = getattr(decision, "eta_consensus", None)
    confidence = getattr(eta_consensus, "confidence", None)
    target_wait_minutes = _target_wait_minutes(decision)
    has_low_confidence = confidence == EtaConfidence.LOW
    has_large_wait = (
        target_wait_minutes is not None
        and target_wait_minutes >= LOW_TRUST_SAFE_MARGIN_MINUTES
    )
    return has_low_confidence or has_large_wait


def _target_wait_minutes(decision: DepartureDecision) -> int | None:
    eta_consensus = getattr(decision, "eta_consensus", None)
    return _valid_minutes(getattr(eta_consensus, "target_wait_minutes", None))


def _valid_minutes(value: int | None) -> int | None:
    if value is None or isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _ensure_source_set(label: str, value: object) -> frozenset[DepartureSource]:
    if not isinstance(value, frozenset):
        raise ValueError(f"{label} must be a frozenset")
    if any(not isinstance(item, DepartureSource) for item in value):
        raise ValueError(f"{label} must contain DepartureSource values")
    return value


validate_departure_safety_policy(
    live_sources=LIVE_DEPARTURE_SOURCES,
    low_trust_sources=LOW_TRUST_DEPARTURE_SOURCES,
    safe_margin_minutes=LOW_TRUST_SAFE_MARGIN_MINUTES,
)
