from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta

from route74.domain.commute import (
    CommuteProfile,
    CommuteSnapshot,
    DepartureDecision,
    DepartureSource,
    DepartureUrgency,
)
from route74.domain.departure_policy import (
    GET_READY_THRESHOLD_MINUTES,
    GO_NOW_THRESHOLD_MINUTES,
    validate_departure_thresholds,
)
from route74.domain.departure_safety import unsafe_arrival_without_safe_margin
from route74.domain.eta import EtaConsensus, EtaSource
from route74.domain.profile_registry import profile_for_time
from route74.domain.profiles import PROFILES
from route74.models import require_local_datetime
from route74.services.arrival_planning import plan_arrival


def choose_profile_for_time(current_time: datetime) -> CommuteProfile | None:
    current_time = require_local_datetime(current_time, name="auto profile time")
    current = current_time.timetz().replace(tzinfo=None, second=0, microsecond=0)
    return profile_for_time(PROFILES, current)


def build_departure_decision(snapshot: CommuteSnapshot) -> DepartureDecision:
    consensus = snapshot.eta_consensus
    if consensus.arrival_minutes is not None and consensus.selected_source is not None:
        plan = plan_arrival(snapshot, consensus)
        return _decision_from_arrival(
            snapshot=snapshot,
            source=_departure_source(plan.source),
            arrival_in_minutes=plan.arrival_minutes,
            next_live_minutes=plan.next_live_minutes,
            eta_consensus=plan.eta_consensus,
            target_wait_minutes=plan.eta_consensus.target_wait_minutes,
        )

    return DepartureDecision(
        profile=snapshot.profile,
        current_time=snapshot.current_time,
        walk_minutes=snapshot.walk_minutes,
        source=DepartureSource.NONE,
        urgency=DepartureUrgency.NO_DATA,
        arrival_in_minutes=None,
        arrival_at=None,
        leave_in_minutes=None,
        leave_at=None,
        next_live_minutes=(),
        eta_consensus=snapshot.eta_consensus,
        yandex_forecast=snapshot.yandex_forecast,
        yandex_history=snapshot.yandex_history,
    )


def _decision_from_arrival(
    *,
    snapshot: CommuteSnapshot,
    source: DepartureSource,
    arrival_in_minutes: int,
    next_live_minutes: tuple[int, ...],
    eta_consensus: EtaConsensus,
    target_wait_minutes: int,
) -> DepartureDecision:
    arrival_at = snapshot.current_time + timedelta(minutes=arrival_in_minutes)
    leave_in_minutes = arrival_in_minutes - snapshot.walk_minutes - target_wait_minutes
    leave_at = snapshot.current_time + timedelta(minutes=leave_in_minutes)
    decision = DepartureDecision(
        profile=snapshot.profile,
        current_time=snapshot.current_time,
        walk_minutes=snapshot.walk_minutes,
        source=source,
        urgency=_urgency_for_leave_in(leave_in_minutes),
        arrival_in_minutes=arrival_in_minutes,
        arrival_at=arrival_at,
        leave_in_minutes=leave_in_minutes,
        leave_at=leave_at,
        next_live_minutes=next_live_minutes,
        eta_consensus=eta_consensus,
        yandex_forecast=snapshot.yandex_forecast,
        yandex_history=snapshot.yandex_history,
    )
    if unsafe_arrival_without_safe_margin(decision):
        return replace(decision, urgency=DepartureUrgency.RELAX)
    return decision


def _urgency_for_leave_in(leave_in_minutes: int) -> DepartureUrgency:
    _ensure_int("leave-in minutes", leave_in_minutes)
    if leave_in_minutes <= GO_NOW_THRESHOLD_MINUTES:
        return DepartureUrgency.GO_NOW
    if leave_in_minutes <= GET_READY_THRESHOLD_MINUTES:
        return DepartureUrgency.GET_READY
    return DepartureUrgency.RELAX


def _departure_source(source: EtaSource) -> DepartureSource:
    return {
        EtaSource.YANDEX: DepartureSource.YANDEX,
        EtaSource.YANDEX_CORRECTED: DepartureSource.YANDEX_CORRECTED,
        EtaSource.VEHICLE_PROGRESS: DepartureSource.VEHICLE_PROGRESS,
        EtaSource.YANDEX_HISTORY: DepartureSource.YANDEX_HISTORY,
    }[source]


def _ensure_int(label: str, value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} must be an integer")


validate_departure_thresholds(
    go_now_threshold_minutes=GO_NOW_THRESHOLD_MINUTES,
    get_ready_threshold_minutes=GET_READY_THRESHOLD_MINUTES,
)
