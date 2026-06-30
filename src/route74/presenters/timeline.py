from __future__ import annotations

from datetime import datetime, timedelta

from route74.domain.commute import DepartureDecision
from route74.domain.departure_safety import missed_by_minutes, unsafe_arrival_without_safe_margin
from route74.presenters.commute_lines import (
    expected_stop_wait_minutes,
    format_duration_minutes,
    missed_arrival,
    wait_place,
)


def format_timeline_block(decision: DepartureDecision) -> str:
    if decision.arrival_at is None or decision.arrival_in_minutes is None:
        return ""
    if missed_arrival(decision):
        return _missed_timeline(decision)
    return _catch_timeline(decision)


def _catch_timeline(decision: DepartureDecision) -> str:
    leave_at = _effective_leave_at(decision)
    stop_at = leave_at + timedelta(minutes=decision.walk_minutes)
    bus_at = decision.arrival_at
    wait = expected_stop_wait_minutes(decision) or 0
    title = "🧭 Если выйдешь сейчас:" if leave_at == decision.current_time else "🧭 План выхода:"
    rows = [title, f"• {decision.current_time:%H:%M} - сейчас"]
    if leave_at > decision.current_time:
        rows.append(f"• {leave_at:%H:%M} - выйти ({_wait_before_leave(decision, leave_at)})")
    if stop_at == bus_at:
        rows.extend(
            [
                f"• {stop_at:%H:%M} - ты у остановки и 74-й ({format_duration_minutes(decision.walk_minutes)} пути)",
                "⚠️ Итог: впритык, ожидания почти нет",
            ]
        )
        return "\n".join(rows)
    rows.extend(
        [
            f"• {stop_at:%H:%M} - ты у остановки ({format_duration_minutes(decision.walk_minutes)} пути)",
            f"• {bus_at:%H:%M} - 74-й",
            f"✅ Итог: ждать у остановки ~{format_duration_minutes(wait)}",
        ]
    )
    return "\n".join(rows)


def _missed_timeline(decision: DepartureDecision) -> str:
    stop_at = decision.current_time + timedelta(minutes=decision.walk_minutes)
    missed_by = missed_by_minutes(decision) or 0
    result = (
        f"❌ Итог: безопасный запас меньше на {format_duration_minutes(missed_by)}"
        if unsafe_arrival_without_safe_margin(decision)
        else f"❌ Итог: на этот 74-й уже не успеешь, придёшь на {format_duration_minutes(missed_by)} позже"
    )
    return "\n".join(
        [
            "🧭 Если выйдешь сейчас:",
            f"• {decision.current_time:%H:%M} - сейчас",
            f"• {decision.arrival_at:%H:%M} - этот 74-й уйдёт",
            f"• {stop_at:%H:%M} - ты у остановки",
            result,
        ]
    )


def _effective_leave_at(decision: DepartureDecision) -> datetime:
    if decision.leave_at is None or decision.leave_in_minutes is None:
        return decision.current_time
    if decision.leave_in_minutes <= 0:
        return decision.current_time
    return decision.leave_at


def _wait_before_leave(decision: DepartureDecision, leave_at: datetime) -> str:
    minutes = round((leave_at - decision.current_time).total_seconds() / 60)
    return f"можно подождать {format_duration_minutes(minutes)} {wait_place(decision)}"
