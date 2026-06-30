from __future__ import annotations

from typing import Literal

from route74.domain.commute import DepartureDecision
from route74.domain.commute_change import DepartureChange
from route74.presenters.commute_change import format_departure_change_line
from route74.presenters.commute_lines import (
    arrival_line,
    current_time_line,
    direction_line,
    forecast_line,
    follow_up_line,
    headline,
    leave_line,
    missed_arrival,
    next_catchable_line,
    post_forecast_lines,
    summary_line,
    timing_lines,
    upcoming_line,
    walk_line,
)
from route74.presenters.timeline import format_timeline_block


WatchAlertKind = Literal["early", "final"]


def format_action_message(
    decision: DepartureDecision,
    *,
    include_follow_up: bool = False,
    change: DepartureChange | None = None,
) -> str:
    lines = [
        headline(decision),
        summary_line(decision),
        current_time_line(decision),
        direction_line(decision),
    ]
    change_line = format_departure_change_line(change)
    if change_line:
        lines.append(change_line)
    timeline = format_timeline_block(decision)
    if timeline:
        lines.extend(["", *_compact_timing_lines(decision)])
        lines.extend(["", timeline])
    else:
        lines.extend(["", *timing_lines(decision)])
    lines.extend(["", forecast_line(decision)])
    if include_follow_up:
        follow_up = follow_up_line(decision)
        if follow_up:
            lines.append(follow_up)
    lines.extend(post_forecast_lines(decision))
    return "\n".join(lines)


def _compact_timing_lines(decision: DepartureDecision) -> list[str]:
    next_target = next_catchable_line(decision)
    lines = ([next_target] if next_target else []) + [walk_line(decision)]
    upcoming = upcoming_line(decision)
    if upcoming:
        lines.append(upcoming)
    return lines


def format_watch_alert(
    decision: DepartureDecision,
    alert_kind: WatchAlertKind,
) -> str:
    if alert_kind == "final":
        prefix = _final_alert_prefix(decision)
    else:
        prefix = "🧥 СОБИРАЙСЯ"
    forecast = forecast_line(decision)
    lines = [
        prefix,
        summary_line(decision),
        current_time_line(decision),
        direction_line(decision),
    ]
    timeline = format_timeline_block(decision)
    if timeline:
        lines.extend(["", timeline])
    else:
        fallback_lines = []
        leave = leave_line(decision)
        if leave:
            fallback_lines.append(leave)
        fallback_lines.extend([arrival_line(decision), walk_line(decision)])
        lines.extend(["", *fallback_lines])
    lines.extend(["", forecast])
    lines.extend(post_forecast_lines(decision))
    return "\n".join(lines)


def _final_alert_prefix(decision: DepartureDecision) -> str:
    return "❌ НА ЭТОТ 74-Й УЖЕ НЕ УСПЕЕШЬ" if missed_arrival(decision) else "🏃 ВЫХОДИ СЕЙЧАС"
