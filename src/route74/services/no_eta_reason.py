from __future__ import annotations

from route74.domain.commute import DepartureDecision, DepartureSource
from route74.domain.yandex_history import YandexHistoryPrediction
from route74.sources.yandex.models import YandexLiveForecast, YandexSourceStatus

NO_ETA_UNKNOWN_REASON = "unknown_no_eta"


def no_eta_reason_for_decision(decision: DepartureDecision | None) -> str:
    if decision is None or decision.source != DepartureSource.NONE:
        return ""
    live_reason = _yandex_no_eta_reason(decision.yandex_forecast)
    history_reason = _history_no_eta_reason(decision.yandex_history)
    return "+".join(part for part in (live_reason, history_reason) if part) or NO_ETA_UNKNOWN_REASON


def _yandex_no_eta_reason(forecast: YandexLiveForecast) -> str:
    if not forecast.enabled:
        return "yandex_disabled"
    if not forecast.available:
        return _yandex_status_reason(forecast.status)
    if not forecast.arrival_minutes:
        return _yandex_status_reason(forecast.status)
    return "yandex_untrusted_eta"


def _yandex_status_reason(status: YandexSourceStatus) -> str:
    if status == YandexSourceStatus.OK:
        return "yandex_no_eta"
    return f"yandex_{status.value}"


def _history_no_eta_reason(history: YandexHistoryPrediction) -> str:
    if history.available and history.arrival_minutes is not None:
        return ""
    reason = history.fallback_reason
    if reason == "history_disabled":
        return "history_disabled"
    if reason == "history_unavailable":
        return "history_unavailable"
    if reason == "local_history_unavailable":
        return "history_local_unavailable"
    if reason.startswith("history_error:"):
        return "history_error"
    if reason.startswith("insufficient_history:"):
        return "history_insufficient"
    return _reason_key(reason, fallback="history_unavailable")


def _reason_key(value: str, *, fallback: str) -> str:
    parts = []
    for character in value.casefold():
        if character.isascii() and (character.isalnum() or character == "_"):
            parts.append(character)
        elif parts and parts[-1] != "_":
            parts.append("_")
    key = "".join(parts).strip("_")
    if not key:
        return fallback
    if key[0].isdigit():
        return f"reason_{key}"[:80]
    return key[:80]
