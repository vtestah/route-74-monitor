from __future__ import annotations

from dataclasses import dataclass, replace

from route74.domain.commute import CommuteSnapshot
from route74.domain.eta import (
    EtaConsensus,
    EtaEstimate,
    EtaExplanation,
    EtaExplanationAction,
    EtaExplanationCode,
    EtaSource,
)
from route74.sources.yandex.trust import (
    forecast_has_trusted_fresh_eta,
    trusted_arrivals_for_forecast,
)

SKIP_MISSED_LIVE_WARNING = "ближайший 74-й уже не успеть, планирую следующий"
LIVE_CONTEXT_SOURCES = {
    EtaSource.YANDEX,
    EtaSource.YANDEX_CORRECTED,
    EtaSource.VEHICLE_PROGRESS,
}


@dataclass(frozen=True)
class ArrivalPlan:
    source: EtaSource
    arrival_minutes: int
    next_live_minutes: tuple[int, ...]
    eta_consensus: EtaConsensus

    def __post_init__(self) -> None:
        if not isinstance(self.source, EtaSource):
            raise ValueError("arrival plan source needs EtaSource")
        if _invalid_non_negative_int(self.arrival_minutes):
            raise ValueError("arrival plan needs non-negative arrival minutes")
        if not isinstance(self.next_live_minutes, tuple) or any(
            _invalid_non_negative_int(minutes) for minutes in self.next_live_minutes
        ):
            raise ValueError("arrival plan next live minutes need tuple of non-negative integers")
        if any(previous >= current for previous, current in zip(self.next_live_minutes, self.next_live_minutes[1:])):
            raise ValueError("arrival plan next live minutes must be strictly increasing")
        if any(minutes <= self.arrival_minutes for minutes in self.next_live_minutes):
            raise ValueError("arrival plan next live minutes must be after selected arrival")
        if not isinstance(self.eta_consensus, EtaConsensus):
            raise ValueError("arrival plan ETA consensus needs EtaConsensus")
        if self.eta_consensus.selected_source != self.source:
            raise ValueError("arrival plan source must match ETA consensus")
        if self.eta_consensus.arrival_minutes != self.arrival_minutes:
            raise ValueError("arrival plan arrival must match ETA consensus")


def plan_arrival(snapshot: CommuteSnapshot, consensus: EtaConsensus) -> ArrivalPlan:
    if consensus.selected_source is None or consensus.arrival_minutes is None:
        raise ValueError("arrival planning needs selected ETA consensus")
    promoted = _promote_next_catchable_live(snapshot, consensus)
    if promoted is not None:
        return promoted
    return ArrivalPlan(
        source=consensus.selected_source,
        arrival_minutes=consensus.arrival_minutes,
        next_live_minutes=_next_live_minutes(snapshot, consensus),
        eta_consensus=consensus,
    )


def _promote_next_catchable_live(snapshot: CommuteSnapshot, consensus: EtaConsensus) -> ArrivalPlan | None:
    if consensus.selected_source not in {EtaSource.YANDEX, EtaSource.YANDEX_CORRECTED}:
        return None
    if consensus.arrival_minutes is None:
        return None
    catchable_after = snapshot.walk_minutes + consensus.target_wait_minutes
    if consensus.arrival_minutes >= catchable_after:
        return None
    live_minutes = _promotion_live_minutes(snapshot, consensus)
    for index, minutes in enumerate(live_minutes):
        if minutes < catchable_after:
            continue
        return ArrivalPlan(
            source=EtaSource.YANDEX,
            arrival_minutes=minutes,
            next_live_minutes=live_minutes[index + 1 : index + 4],
            eta_consensus=_promoted_consensus(consensus, minutes),
        )
    return None


def _next_live_minutes(snapshot: CommuteSnapshot, consensus: EtaConsensus) -> tuple[int, ...]:
    source = consensus.selected_source
    if source in LIVE_CONTEXT_SOURCES:
        live_minutes = _trusted_live_minutes(snapshot)
        if source == EtaSource.VEHICLE_PROGRESS:
            return _live_minutes_after(live_minutes, consensus.arrival_minutes)
        if source == EtaSource.YANDEX_CORRECTED:
            return live_minutes[1:4]
        return _live_minutes_after(live_minutes, consensus.arrival_minutes)
    return ()


def _promotion_live_minutes(snapshot: CommuteSnapshot, consensus: EtaConsensus) -> tuple[int, ...]:
    live_minutes = _trusted_live_minutes(snapshot)
    if consensus.selected_source == EtaSource.YANDEX_CORRECTED:
        return live_minutes[1:]
    return live_minutes


def _live_minutes_after(
    live_minutes: tuple[int, ...],
    arrival_minutes: int | None,
) -> tuple[int, ...]:
    if arrival_minutes is None:
        return live_minutes[:3]
    return tuple(minutes for minutes in live_minutes if minutes > arrival_minutes)[:3]


def _trusted_live_minutes(snapshot: CommuteSnapshot) -> tuple[int, ...]:
    if not forecast_has_trusted_fresh_eta(snapshot.yandex_forecast):
        return ()
    return trusted_arrivals_for_forecast(snapshot.yandex_forecast)


def _next_live_warning(consensus: EtaConsensus) -> str:
    if not consensus.warning:
        return SKIP_MISSED_LIVE_WARNING
    return f"{SKIP_MISSED_LIVE_WARNING}; {consensus.warning}"


def _promoted_consensus(consensus: EtaConsensus, minutes: int) -> EtaConsensus:
    return replace(
        consensus,
        selected_source=EtaSource.YANDEX,
        arrival_minutes=minutes,
        spread_minutes=None,
        warning=_next_live_warning(consensus),
        estimates=_with_promoted_estimate(consensus.estimates, minutes),
        explanations=_with_promoted_explanation(consensus.explanations),
    )


def _with_promoted_estimate(estimates: tuple[EtaEstimate, ...], minutes: int) -> tuple[EtaEstimate, ...]:
    promoted = EtaEstimate(EtaSource.YANDEX, minutes)
    retained = tuple(estimate for estimate in estimates if estimate.source != EtaSource.YANDEX)
    return (promoted, *retained)


def _with_promoted_explanation(
    explanations: tuple[EtaExplanation, ...],
) -> tuple[EtaExplanation, ...]:
    promoted = EtaExplanation(
        EtaExplanationCode.LIVE_ETA,
        EtaExplanationAction.TRUST_ETA,
        detail="next_catchable_live",
    )
    retained = tuple(
        explanation
        for explanation in explanations
        if explanation.code
        not in {
            EtaExplanationCode.LIVE_ETA,
            EtaExplanationCode.CORRECTED_LIVE,
            EtaExplanationCode.VEHICLE_PROGRESS,
            EtaExplanationCode.HISTORY_FALLBACK,
        }
    )
    return (promoted, *retained)


def _invalid_non_negative_int(value: object) -> bool:
    return isinstance(value, bool) or not isinstance(value, int) or value < 0
