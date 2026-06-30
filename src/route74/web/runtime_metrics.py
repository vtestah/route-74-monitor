from __future__ import annotations

from time import perf_counter

from route74.domain.commute import DepartureDecision
from route74.domain.runtime_sources import BOT_EVENT_USER_REPLY
from route74.notifications.base import NotificationSendResult
from route74.services.no_eta_reason import no_eta_reason_for_decision
from route74.sources.yandex.models import YandexSourceMethod
from route74.storage.bot_latency import BotInteractionEvent


def now_perf() -> float:
    return perf_counter()


def elapsed_ms(started_at: float) -> int:
    return max(0, int(round((perf_counter() - started_at) * 1000)))


def web_interaction_event(
    *,
    decision: DepartureDecision,
    command: str,
    event_kind: str = BOT_EVENT_USER_REPLY,
    forecast_ms: int,
    total_ms: int,
    render_ms: int = 0,
    send_ms: int = 0,
    status: str = "ok",
    error: str = "",
) -> BotInteractionEvent:
    return BotInteractionEvent(
        received_at=decision.current_time,
        chat_id=0,
        update_type="http_request",
        command=command,
        event_kind=event_kind,
        profile_key=decision.profile.key,
        reply_source=_reply_source(decision),
        yandex_source_method=_source_method(decision),
        forecast_ms=forecast_ms,
        render_ms=render_ms,
        send_ms=send_ms,
        total_ms=total_ms,
        status=status,
        error=error,
        no_eta_reason=no_eta_reason_for_decision(decision),
    )


def watch_notification_event(
    *,
    decision: DepartureDecision,
    event_kind: str,
    forecast_ms: int,
    total_ms: int,
    send_ms: int,
    notification: NotificationSendResult,
) -> BotInteractionEvent:
    return BotInteractionEvent(
        received_at=decision.current_time,
        chat_id=0,
        update_type="watch_check",
        command=event_kind,
        event_kind=event_kind,
        profile_key=decision.profile.key,
        reply_source=_reply_source(decision),
        yandex_source_method=_source_method(decision),
        forecast_ms=forecast_ms,
        render_ms=0,
        send_ms=send_ms,
        total_ms=total_ms,
        status="ok" if notification.delivered else "error",
        error="" if notification.delivered else f"notify_error:{notification.error_type or notification.detail}",
        no_eta_reason=no_eta_reason_for_decision(decision),
    )


def _reply_source(decision: DepartureDecision) -> str:
    if decision.source.value == "none":
        return "no_eta"
    return decision.source.value


def _source_method(decision: DepartureDecision) -> str:
    method = decision.yandex_forecast.source_method
    if method == YandexSourceMethod.NONE:
        return "none"
    return method.value
