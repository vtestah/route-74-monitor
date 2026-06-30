from __future__ import annotations

from datetime import datetime
from typing import Protocol

from route74.diagnostics import sanitize_command_text, sanitize_diagnostic_text
from route74.domain.commute_change import DepartureChange
from route74.presenters.commute_change import format_departure_change_line


MAX_VISIBLE_ITEMS = 5


class SupportSnapshotItemView(Protocol):
    severity: str
    key: str
    message: str
    action: str


class SupportSnapshotView(Protocol):
    profile_key: str
    window_key: str
    hours: int
    current_time: datetime
    status: str
    primary_action: str
    primary_issue: SupportSnapshotItemView | None
    latest_reply_change: DepartureChange | None
    snapshot_command: str
    report_command: str
    items: tuple[SupportSnapshotItemView, ...]


def format_support_snapshot(snapshot: SupportSnapshotView) -> str:
    visible_items = _visible_items(snapshot.items, primary_issue=snapshot.primary_issue)
    lines = [
        "🧰 Разбор 74",
        f"🕒 Сейчас: {snapshot.current_time:%H:%M}",
        _direction_line(snapshot.profile_key),
        f"📌 Статус: {_status_text(snapshot.status)}",
        f"🎯 Следующий шаг: {_command(snapshot.primary_action)}",
    ]
    reason = _primary_issue_line(snapshot.primary_issue)
    if reason:
        lines.append(reason)
    change_line = format_departure_change_line(snapshot.latest_reply_change)
    if change_line:
        lines.append(change_line)
    if visible_items:
        lines.extend(["", "Сигналы:"])
        for item in visible_items:
            lines.append(_item_line(item))
            action = _item_action_line(
                item,
                primary_action=snapshot.primary_action,
                report_command=snapshot.report_command,
            )
            if action:
                lines.append(action)
        hidden = len(snapshot.items) - len(visible_items)
        if hidden > 0:
            lines.append(f"• ещё {hidden} сигналов в полном отчёте")
    else:
        lines.extend(["", "✅ Критичных проблем не вижу."])
    lines.extend(
        [
            "",
            f"Быстрый снимок: {_command(snapshot.snapshot_command)}",
            f"Полный отчёт: {_command(snapshot.report_command)}",
        ]
    )
    return "\n".join(lines)


def _visible_items(
    items: tuple[SupportSnapshotItemView, ...],
    *,
    primary_issue: SupportSnapshotItemView | None,
) -> tuple[SupportSnapshotItemView, ...]:
    actionable = tuple(item for item in items if item.severity in {"critical", "warning"})
    visible = actionable or items
    if primary_issue is None:
        return visible[:MAX_VISIBLE_ITEMS]
    return _primary_first(visible, primary_issue)[:MAX_VISIBLE_ITEMS]


def _primary_first(
    items: tuple[SupportSnapshotItemView, ...],
    primary_issue: SupportSnapshotItemView,
) -> tuple[SupportSnapshotItemView, ...]:
    primary = next((item for item in items if _same_item(item, primary_issue)), None)
    if primary is None:
        return items
    rest = tuple(item for item in items if not _same_item(item, primary_issue))
    return (primary, *rest)


def _same_item(left: SupportSnapshotItemView, right: SupportSnapshotItemView) -> bool:
    return (left.severity, left.key, left.message, left.action) == (
        right.severity,
        right.key,
        right.message,
        right.action,
    )


def _item_line(item: SupportSnapshotItemView) -> str:
    key = sanitize_diagnostic_text(item.key, fallback="unknown", limit=40)
    message = sanitize_diagnostic_text(item.message, fallback="-", limit=140)
    return f"• {_severity_text(item.severity)} {key}: {message}"


def _item_action_line(item: SupportSnapshotItemView, *, primary_action: str, report_command: str) -> str:
    action = _command(item.action)
    if action in {_command(primary_action), _command(report_command)}:
        return ""
    return f"  ↳ {action}"


def _primary_issue_line(item: SupportSnapshotItemView | None) -> str:
    if item is None:
        return ""
    key = sanitize_diagnostic_text(item.key, fallback="unknown", limit=40)
    message = sanitize_diagnostic_text(item.message, fallback="-", limit=120)
    return f"🔎 Почему: {key} - {message}"


def _direction_line(profile_key: str) -> str:
    if profile_key == "morning":
        return "🏠 Дом -> Академ"
    if profile_key == "evening":
        return "🏢 Академ -> дом"
    profile = sanitize_diagnostic_text(profile_key, fallback="profile", limit=40)
    return f"🧭 Профиль: {profile}"


def _status_text(status: str) -> str:
    if status == "critical":
        return "критично"
    if status == "warning":
        return "нужно проверить"
    if status == "ok":
        return "ok"
    return sanitize_diagnostic_text(status, fallback="unknown", limit=40)


def _severity_text(severity: str) -> str:
    if severity == "critical":
        return "critical"
    if severity == "warning":
        return "warning"
    if severity == "info":
        return "info"
    return sanitize_diagnostic_text(severity, fallback="signal", limit=20)


def _command(value: str) -> str:
    return sanitize_command_text(value, fallback="route74 monitor-tick --fail-on critical", limit=160)
