from __future__ import annotations

from datetime import timedelta

from route74.domain.commute import DepartureDecision, DepartureSource, DepartureUrgency
from route74.domain.departure_safety import (
    missed_by_minutes,
    physical_catch_margin_minutes,
    unsafe_arrival_without_safe_margin,
)
from route74.domain.eta import EtaConfidence
from route74.domain.wait_policy import TARGET_STOP_WAIT_MINUTES
from route74.domain.watch_policy import FINAL_ALERT_LEAVE_IN, WATCH_DURATION_MINUTES
from route74.domain.yandex_history import YandexHistoryPrediction
from route74.presenters.eta_explanations import eta_explanation_line as eta_decision_explanation_line
from route74.presenters.history_status import history_basis_text, unavailable_history_status_text
from route74.presenters.eta_factors import format_eta_factor_texts
from route74.presenters.yandex_status import (
    yandex_method_text,
    yandex_status_summary,
)
from route74.sources.yandex.freshness import effective_forecast_age_seconds


def headline(decision: DepartureDecision) -> str:
    if missed_arrival(decision):
        return "❌ НА ЭТОТ 74-Й УЖЕ НЕ УСПЕЕШЬ"
    if decision.urgency == DepartureUrgency.GO_NOW:
        return "🏃 ВЫХОДИ СЕЙЧАС"
    if decision.urgency == DepartureUrgency.GET_READY:
        return "🧥 СОБИРАЙСЯ"
    if decision.urgency == DepartureUrgency.RELAX:
        return f"✅ ПОКА ЖДИ {wait_place_upper(decision)}"
    return "⚠️ ТОЧНОГО СИГНАЛА НЕТ"


def direction_line(decision: DepartureDecision) -> str:
    if decision.profile.key == "morning":
        return "🏠 Дом -> Академ"
    return "🏢 Академ -> дом"


def current_time_line(decision: DepartureDecision) -> str:
    return f"🕒 Сейчас: {decision.current_time:%H:%M}"


def summary_line(decision: DepartureDecision) -> str:
    if missed_arrival(decision):
        return _missed_summary_line(decision)
    if decision.arrival_at is None or decision.arrival_in_minutes is None:
        return "🎯 Коротко: точного ETA нет · обнови прогноз или открой карту 74"
    return "🎯 Коротко: " + " · ".join(
        (
            _summary_leave_text(decision),
            f"74-й {decision.arrival_at:%H:%M}",
            _summary_wait_text(decision),
        )
    )


def forecast_line(decision: DepartureDecision) -> str:
    if missed_arrival(decision):
        if unsafe_arrival_without_safe_margin(decision) and decision.eta_consensus.warning:
            return (
                f"📌 Решение: безопасного запаса нет ({decision.eta_consensus.warning}), подожди {wait_place(decision)}"
            )
        return f"📌 Решение: на этот 74-й уже не успеешь, подожди {wait_place(decision)}"
    if decision.source == DepartureSource.YANDEX_HISTORY:
        return f"📌 Надёжность: низкая · {history_basis_text(decision.yandex_history)} · лучше сверить карту"
    if decision.eta_consensus.arrival_minutes is not None:
        return _consensus_forecast_line(decision)
    if decision.urgency == DepartureUrgency.GO_NOW:
        wait = expected_stop_wait_minutes(decision)
        if wait is not None and wait < TARGET_STOP_WAIT_MINUTES:
            return "📌 Надёжность: шанс есть, ожидание маленькое"
        return f"📌 Надёжность: поймаешь с ожиданием ~{format_duration_minutes(wait or 0)}"
    if decision.urgency == DepartureUrgency.GET_READY:
        return "📌 Надёжность: скоро окно 2-3 мин у остановки"
    if decision.urgency == DepartureUrgency.RELAX:
        return f"📌 Надёжность: можно подождать {wait_place(decision)}"
    return "📌 Действие: обнови прогноз через минуту или открой карту 74"


def follow_up_line(decision: DepartureDecision) -> str:
    if missed_arrival(decision) or _initial_reply_is_final(decision):
        return ""
    if decision.arrival_at is None or decision.arrival_in_minutes is None:
        return f"👀 Дальше: слежу {WATCH_DURATION_MINUTES} мин; если Яндекс увидит 74-й, пришлю сигнал."
    return f"👀 Дальше: слежу {WATCH_DURATION_MINUTES} мин и пришлю отдельный сигнал, когда пора двигаться."


def yandex_status_line(decision: DepartureDecision) -> str:
    forecast = decision.yandex_forecast
    if not forecast.enabled:
        return ""
    if forecast.available:
        status = yandex_status_summary(forecast.status, forecast.fallback_reason)
        method = yandex_method_text(forecast.source_method)
        age_seconds = effective_forecast_age_seconds(forecast)
        age = f" · свежесть {_age_label(age_seconds)}" if age_seconds is not None else ""
        suffix = (
            " · основной источник"
            if decision.source in {DepartureSource.YANDEX, DepartureSource.YANDEX_CORRECTED}
            else ""
        )
        return f"🟡 Яндекс 74: {status} · {method}{age}{suffix}"
    status = yandex_status_summary(forecast.status, forecast.fallback_reason)
    fallback = _fallback_source_text(decision.source)
    return f"⚠️ Яндекс: {status} · беру {fallback}"


def history_status_line(decision: DepartureDecision) -> str:
    history = decision.yandex_history
    if decision.source == DepartureSource.YANDEX_HISTORY and history.available and history.arrival_minutes is not None:
        detail = _available_history_detail(history)
        if decision.arrival_at is not None:
            eta = format_duration_minutes(history.arrival_minutes)
            return f"📈 История Яндекса: {decision.arrival_at:%H:%M} (через {eta}) · {detail}"
        return f"📈 История Яндекса: через {format_duration_minutes(history.arrival_minutes)} · {detail}"
    if decision.source != DepartureSource.NONE:
        return ""
    status = unavailable_history_status_text(history)
    if not status:
        return ""
    return f"📈 История Яндекса: {status}"


def post_forecast_lines(decision: DepartureDecision) -> list[str]:
    return [
        line
        for line in (
            eta_decision_explanation_line(decision.eta_consensus.explanations),
            eta_explanation_line(decision),
            yandex_status_line(decision),
            history_status_line(decision),
        )
        if line
    ]


def eta_explanation_line(decision: DepartureDecision) -> str:
    items = format_eta_factor_texts(decision.eta_consensus.factors)
    if not items:
        return ""
    return f"🧪 Почему: {'; '.join(items)}"


def _available_history_detail(history: YandexHistoryPrediction) -> str:
    return " · ".join(
        (
            _history_sample_count_text(history.sample_count),
            f"окно ±{format_duration_minutes(history.bucket_minutes)}",
        )
    )


def _history_sample_count_text(count: int) -> str:
    if count % 10 == 1 and count % 100 != 11:
        noun = "замер"
    elif count % 10 in {2, 3, 4} and count % 100 not in {12, 13, 14}:
        noun = "замера"
    else:
        noun = "замеров"
    return f"{count} {noun}"


def timing_lines(decision: DepartureDecision) -> list[str]:
    lines = []
    leave = leave_line(decision)
    if leave:
        lines.append(leave)
    target = next_catchable_line(decision)
    if target:
        lines.append(target)
    lines.append(arrival_line(decision))
    lines.append(walk_line(decision))
    return lines


def walk_line(decision: DepartureDecision) -> str:
    line = f"🚶 Твой путь до остановки: {format_duration_minutes(decision.walk_minutes)}"
    if decision.profile.walk_note:
        line += f" ({decision.profile.walk_note})"
    return line


def arrival_line(decision: DepartureDecision) -> str:
    if decision.source == DepartureSource.NONE:
        return "📡 74-й не виден, точного ETA нет"
    if decision.arrival_at is None or decision.arrival_in_minutes is None:
        return "📡 74-й не виден, точного ETA нет"

    line = (
        f"{arrival_label(decision)}: "
        f"{decision.arrival_at:%H:%M} "
        f"(через {format_duration_minutes(decision.arrival_in_minutes)})"
    )
    if decision.next_live_minutes:
        upcoming = ", ".join(f"+{format_duration_minutes(item)}" for item in decision.next_live_minutes)
        line += f" · ещё: {upcoming}"
    return line


def upcoming_line(decision: DepartureDecision) -> str:
    if missed_arrival(decision) or not decision.next_live_minutes:
        return ""
    upcoming = ", ".join(f"+{format_duration_minutes(item)}" for item in decision.next_live_minutes)
    return f"🚌 Следом: {upcoming}"


def leave_line(decision: DepartureDecision) -> str:
    if decision.leave_at is None or decision.leave_in_minutes is None:
        return ""
    if missed_arrival(decision):
        missed_by = missed_by_minutes(decision) or 0
        if unsafe_arrival_without_safe_margin(decision):
            return f"❌ Если выйдешь сейчас: безопасный запас меньше на {format_duration_minutes(missed_by)}"
        return f"❌ Если выйдешь сейчас: опоздаешь на {format_duration_minutes(missed_by)}"
    wait = expected_stop_wait_minutes(decision) or 0
    if decision.leave_in_minutes <= 0:
        if wait >= TARGET_STOP_WAIT_MINUTES:
            return f"✅ Выходи сейчас: у остановки ждать ~{format_duration_minutes(wait)}"
        if wait == 1:
            return "✅ Выходи сейчас: успеваешь, ждать ~1 мин"
        return "⚠️ Выходи сейчас: впритык, ожидания почти нет"
    duration = format_duration_minutes(decision.leave_in_minutes)
    if decision.source == DepartureSource.YANDEX_HISTORY:
        return f"🕒 По истории выходить: через {duration} ({decision.leave_at:%H:%M}) · ждать ~{format_duration_minutes(wait)}"
    return f"🕒 Выходить: через {duration} ({decision.leave_at:%H:%M}) · ждать ~{format_duration_minutes(wait)}"


def missed_arrival(decision: DepartureDecision) -> bool:
    margin = catch_margin_minutes(decision)
    return (margin is not None and margin < 0) or unsafe_arrival_without_safe_margin(decision)


def catch_margin_minutes(decision: DepartureDecision) -> int | None:
    return physical_catch_margin_minutes(decision)


def expected_stop_wait_minutes(decision: DepartureDecision) -> int | None:
    margin = catch_margin_minutes(decision)
    if margin is None or margin < 0:
        return None
    if decision.leave_in_minutes is not None and decision.leave_in_minutes > 0:
        if decision.eta_consensus.arrival_minutes is not None:
            return decision.eta_consensus.target_wait_minutes
    return margin


def _initial_reply_is_final(decision: DepartureDecision) -> bool:
    return (
        decision.urgency == DepartureUrgency.GO_NOW
        and decision.leave_in_minutes is not None
        and decision.leave_in_minutes <= FINAL_ALERT_LEAVE_IN
    )


def next_catchable_line(decision: DepartureDecision) -> str:
    if not missed_arrival(decision):
        return ""
    target_wait = _target_wait_minutes(decision)
    candidate = _next_catchable_candidate(decision, target_wait)
    if candidate is None:
        return "🎯 Следующая цель: пока не вижу, обнови через минуту"
    arrival_at, arrival_in = candidate
    leave_in = max(0, arrival_in - decision.walk_minutes - target_wait)
    leave_at = decision.current_time + timedelta(minutes=leave_in)
    return f"🎯 Следующая цель: {arrival_at:%H:%M} · выходить около {leave_at:%H:%M}"


def _missed_summary_line(decision: DepartureDecision) -> str:
    if decision.arrival_at is None:
        return "🎯 Коротко: этот 74-й уже не цель · обнови прогноз"
    stop_at = decision.current_time + timedelta(minutes=decision.walk_minutes)
    parts = [
        f"этот уйдёт {decision.arrival_at:%H:%M}",
        f"ты у остановки {stop_at:%H:%M}",
    ]
    target_wait = _target_wait_minutes(decision)
    candidate = _next_catchable_candidate(decision, target_wait)
    if candidate is not None:
        next_arrival_at, _arrival_in = candidate
        parts.append(f"следующая {next_arrival_at:%H:%M}")
    return f"🎯 Коротко: {' · '.join(parts)}"


def _next_catchable_candidate(
    decision: DepartureDecision,
    target_wait: int,
) -> tuple[datetime, int] | None:
    candidates: list[tuple[datetime, int]] = []
    for minutes in decision.next_live_minutes:
        if minutes >= decision.walk_minutes + target_wait:
            candidates.append((decision.current_time + timedelta(minutes=minutes), minutes))
    return min(candidates, key=lambda item: item[0]) if candidates else None


def _target_wait_minutes(decision: DepartureDecision) -> int:
    if decision.eta_consensus.arrival_minutes is not None:
        return decision.eta_consensus.target_wait_minutes
    return TARGET_STOP_WAIT_MINUTES


def _summary_leave_text(decision: DepartureDecision) -> str:
    if decision.leave_at is None or decision.leave_in_minutes is None:
        return "выход не считаю"
    if decision.leave_in_minutes <= 0:
        return "выйти сейчас"
    return f"выйти {decision.leave_at:%H:%M}"


def _summary_wait_text(decision: DepartureDecision) -> str:
    wait = expected_stop_wait_minutes(decision)
    if wait is None:
        return "ожидание неизвестно"
    if wait <= 0:
        return "почти без ожидания"
    return f"ждать ~{format_duration_minutes(wait)}"


def format_duration_minutes(minutes: int) -> str:
    if isinstance(minutes, bool) or not isinstance(minutes, int) or minutes < 0:
        raise ValueError("duration minutes must be a non-negative integer")
    if minutes < 60:
        return f"{minutes} мин"
    hours, rest = divmod(minutes, 60)
    if rest == 0:
        return f"{hours}ч"
    return f"{hours}ч {rest} мин"


def _age_label(age_seconds: int | None) -> str:
    if age_seconds is None:
        return "возраст неизвестен"
    if age_seconds < 60:
        return f"{age_seconds} сек назад"
    return f"{format_duration_minutes(round(age_seconds / 60))} назад"


def wait_place(decision: DepartureDecision) -> str:
    if decision.profile.key == "evening":
        return "на работе"
    return "дома"


def wait_place_upper(decision: DepartureDecision) -> str:
    return wait_place(decision).upper()


SOURCE_LABELS = {
    DepartureSource.YANDEX: "🟡 Яндекс 74",
    DepartureSource.YANDEX_CORRECTED: "🟠 Яндекс + поправка",
    DepartureSource.VEHICLE_PROGRESS: "🧭 По координате",
    DepartureSource.YANDEX_HISTORY: "📈 История Яндекса",
}


def source_label(source: DepartureSource) -> str:
    return SOURCE_LABELS.get(source, "🚌 Ближайшая")


def arrival_label(decision: DepartureDecision) -> str:
    if not missed_arrival(decision):
        return source_label(decision.source)
    return {
        DepartureSource.YANDEX: "🟡 Этот 74-й",
        DepartureSource.YANDEX_CORRECTED: "🟠 Этот 74-й",
        DepartureSource.VEHICLE_PROGRESS: "🧭 Этот 74-й",
        DepartureSource.YANDEX_HISTORY: "📈 По истории",
    }.get(decision.source, "🚌 Этот 74-й")


def _consensus_forecast_line(decision: DepartureDecision) -> str:
    consensus = decision.eta_consensus
    prefix = "⚠️" if consensus.warning else "📌"
    line = f"{prefix} Надёжность: {_eta_confidence_text(consensus.confidence)} · {_basis_text(decision.source)}"
    if consensus.confidence == EtaConfidence.LOW:
        line += " · лучше сверить карту"
    if consensus.warning:
        line += f" · {consensus.warning}"
    return line


def _eta_confidence_text(confidence: EtaConfidence) -> str:
    return {
        EtaConfidence.HIGH: "высокое",
        EtaConfidence.MEDIUM: "среднее",
        EtaConfidence.LOW: "низкое",
        EtaConfidence.UNKNOWN: "низкое",
    }[confidence]


def _basis_text(source: DepartureSource) -> str:
    return {
        DepartureSource.YANDEX: "Яндекс",
        DepartureSource.YANDEX_CORRECTED: "Яндекс + поправка",
        DepartureSource.VEHICLE_PROGRESS: "координата на линии",
        DepartureSource.YANDEX_HISTORY: "история Яндекса",
        DepartureSource.NONE: "нет данных",
    }[source]


def _fallback_source_text(source: DepartureSource) -> str:
    return {
        DepartureSource.YANDEX: "Яндекс",
        DepartureSource.YANDEX_CORRECTED: "Яндекс + поправку",
        DepartureSource.VEHICLE_PROGRESS: "координату на линии",
        DepartureSource.YANDEX_HISTORY: "историю Яндекса",
        DepartureSource.NONE: "нет точного ETA",
    }[source]
