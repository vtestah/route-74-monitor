from __future__ import annotations

from typing import Literal, TypedDict

from route74.domain.commute import DepartureDecision, DepartureSource, DepartureUrgency
from route74.presenters.commute_lines import (
    expected_stop_wait_minutes,
    headline,
    missed_arrival,
    source_label,
)
from route74.presenters.eta_explanations import (
    eta_explanation_payloads,
    primary_eta_explanation_payload,
)
from route74.sources.yandex.freshness import forecast_is_fresh
from route74.sources.yandex.models import YandexSourceStatus


DecisionUiStatus = Literal["catch", "wait", "missed", "no_eta"]
DecisionUiEtaState = Literal["live", "stale", "history", "no_eta"]


class DecisionUiPayload(TypedDict):
    status: DecisionUiStatus
    headline: str
    eta_state: DecisionUiEtaState
    eta_state_label: str
    profile_key: str
    profile_label: str
    current_time: str
    leave_at: str | None
    leave_in_minutes: int | None
    arrival_at: str | None
    arrival_in_minutes: int | None
    wait_minutes: int | None
    source_label: str
    eta_reason_code: str
    eta_action_code: str
    eta_explanation_label: str
    eta_action_label: str
    eta_explanations: tuple[dict[str, str], ...]


def decision_ui_payload(decision: DepartureDecision) -> DecisionUiPayload:
    eta_state, eta_state_label = _eta_state(decision)
    primary_explanation = primary_eta_explanation_payload(decision.eta_consensus.explanations)
    return {
        "status": _status(decision),
        "headline": headline(decision),
        "eta_state": eta_state,
        "eta_state_label": eta_state_label,
        "profile_key": decision.profile.key,
        "profile_label": "Утро" if decision.profile.key == "morning" else "Вечер",
        "current_time": decision.current_time.isoformat(),
        "leave_at": decision.leave_at.isoformat() if decision.leave_at is not None else None,
        "leave_in_minutes": decision.leave_in_minutes,
        "arrival_at": decision.arrival_at.isoformat() if decision.arrival_at is not None else None,
        "arrival_in_minutes": decision.arrival_in_minutes,
        "wait_minutes": expected_stop_wait_minutes(decision),
        "source_label": "Нет ETA" if decision.arrival_at is None else source_label(decision.source),
        "eta_reason_code": primary_explanation["code"],
        "eta_action_code": primary_explanation["action"],
        "eta_explanation_label": primary_explanation["label"],
        "eta_action_label": primary_explanation["action_label"],
        "eta_explanations": eta_explanation_payloads(decision.eta_consensus.explanations),
    }


def _status(decision: DepartureDecision) -> DecisionUiStatus:
    if decision.arrival_at is None or decision.arrival_in_minutes is None:
        return "no_eta"
    if missed_arrival(decision):
        return "missed"
    if decision.urgency == DepartureUrgency.RELAX:
        return "wait"
    return "catch"


def _eta_state(decision: DepartureDecision) -> tuple[DecisionUiEtaState, str]:
    if decision.arrival_at is None or decision.arrival_in_minutes is None:
        return "no_eta", "Нет ETA"
    if decision.source == DepartureSource.YANDEX_HISTORY:
        return "history", "ETA по истории"
    forecast = decision.yandex_forecast
    if forecast.enabled and forecast.available and (
        forecast.status == YandexSourceStatus.STALE or not forecast_is_fresh(forecast)
    ):
        return "stale", "ETA устарел"
    return "live", "ETA live"
