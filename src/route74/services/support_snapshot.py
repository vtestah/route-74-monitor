from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from route74.diagnostics import sanitize_command_text, sanitize_diagnostic_text
from route74.domain.commute import CommuteProfile
from route74.domain.commute_change import DepartureChange
from route74.domain.reporting import report_window_for_profile
from route74.domain.runtime_sources import BOT_EVENT_USER_REPLY
from route74.models import now_local
from route74.services.commute_change import build_runtime_prediction_change_map
from route74.storage import (
    DEFAULT_DB,
    STORAGE_READ_ERRORS,
    connect,
    init_db,
    load_recent_bot_runtime_predictions,
)
from route74.storage.monitoring import summarize_monitor
from route74.support_actions import (
    support_report_command_for_profile,
    support_snapshot_command_for_profile,
    watch_state_command_for_path,
)
from route74.support_triage import (
    DEFAULT_TRIAGE_ACTION,
    TRIAGE_CRITICAL,
    TRIAGE_INFO,
    TRIAGE_OK,
    TRIAGE_STATUS_ORDER,
    TRIAGE_WARNING,
    SupportTriageItem,
    build_support_triage,
    operator_triage_item_for_items,
)
from route74.watch_state import DEFAULT_WATCH_STATE_PATH, WatchStateSummary, summarize_watch_state


SUPPORT_SNAPSHOT_ERRORS = STORAGE_READ_ERRORS
SUPPORT_SNAPSHOT_HOURS = 24


@dataclass(frozen=True)
class SupportSnapshotItem:
    severity: str
    key: str
    message: str
    action: str

    def __post_init__(self) -> None:
        if self.severity not in {TRIAGE_OK, TRIAGE_WARNING, TRIAGE_CRITICAL, TRIAGE_INFO}:
            raise ValueError("support snapshot item severity is unknown")
        object.__setattr__(self, "key", sanitize_diagnostic_text(self.key, fallback="unknown", limit=80))
        object.__setattr__(self, "message", sanitize_diagnostic_text(self.message, fallback="-", limit=220))
        object.__setattr__(self, "action", sanitize_command_text(self.action, fallback=DEFAULT_TRIAGE_ACTION, limit=160))


@dataclass(frozen=True)
class SupportSnapshot:
    profile_key: str
    window_key: str
    hours: int
    current_time: datetime
    status: str
    primary_action: str
    primary_issue: SupportSnapshotItem | None
    latest_reply_change: DepartureChange | None
    snapshot_command: str
    report_command: str
    items: tuple[SupportSnapshotItem, ...]

    def __post_init__(self) -> None:
        if self.status not in {TRIAGE_OK, TRIAGE_WARNING, TRIAGE_CRITICAL}:
            raise ValueError("support snapshot status is unknown")
        if not isinstance(self.current_time, datetime):
            raise ValueError("support snapshot current_time needs datetime")
        if self.primary_issue is not None and not isinstance(self.primary_issue, SupportSnapshotItem):
            raise ValueError("support snapshot primary_issue needs SupportSnapshotItem or None")
        if self.latest_reply_change is not None and not isinstance(self.latest_reply_change, DepartureChange):
            raise ValueError("support snapshot latest_reply_change needs DepartureChange or None")
        if not isinstance(self.items, tuple) or any(not isinstance(item, SupportSnapshotItem) for item in self.items):
            raise ValueError("support snapshot items need tuple of SupportSnapshotItem")
        object.__setattr__(self, "profile_key", sanitize_diagnostic_text(self.profile_key, fallback="unknown", limit=40))
        object.__setattr__(self, "window_key", sanitize_diagnostic_text(self.window_key, fallback="unknown", limit=80))
        object.__setattr__(
            self,
            "primary_action",
            sanitize_command_text(self.primary_action, fallback=DEFAULT_TRIAGE_ACTION, limit=160),
        )
        object.__setattr__(
            self,
            "snapshot_command",
            sanitize_command_text(self.snapshot_command, fallback=DEFAULT_TRIAGE_ACTION, limit=160),
        )
        object.__setattr__(
            self,
            "report_command",
            sanitize_command_text(self.report_command, fallback=DEFAULT_TRIAGE_ACTION, limit=160),
        )


class SupportSnapshotService:
    def __init__(
        self,
        *,
        db_path: Path = DEFAULT_DB,
        watch_state_path: Path = DEFAULT_WATCH_STATE_PATH,
        hours: int = SUPPORT_SNAPSHOT_HOURS,
    ) -> None:
        self._db_path = db_path
        self._watch_state_path = watch_state_path
        self._hours = _positive_hours(hours)

    def build(self, profile: CommuteProfile, *, current_time: datetime | None = None) -> SupportSnapshot:
        current_time = current_time or now_local()
        window = report_window_for_profile(profile.key)
        snapshot_command = support_snapshot_command_for_profile(profile.key)
        report_command = support_report_command_for_profile(profile.key)
        watch_state, watch_items = _watch_state_for_snapshot(self._watch_state_path, current_time)
        try:
            with connect(self._db_path) as connection:
                init_db(connection)
                monitor = summarize_monitor(
                    connection,
                    db_path=self._db_path,
                    latency_hours=self._hours,
                    runtime_hours=self._hours,
                    profile_key=profile.key,
                    current_time=current_time,
                )
                if monitor.runtime is None or monitor.calibration is None:
                    raise ValueError("monitor runtime diagnostics are unavailable")
                latest_reply_change = _latest_reply_change(
                    connection,
                    profile_key=profile.key,
                    current_time=current_time,
                    hours=self._hours,
                )
                triage = build_support_triage(
                    window_key=window.key,
                    profile_key=profile.key,
                    hours=self._hours,
                    monitor=monitor,
                    forecast=monitor.forecast,
                    runtime_quality=monitor.runtime,
                    runtime_calibration=monitor.calibration,
                    watch_state=watch_state,
                )
        except SUPPORT_SNAPSHOT_ERRORS as exc:
            error_item = _support_snapshot_error_item(exc)
            return _snapshot_from_items(
                profile_key=profile.key,
                window_key=window.key,
                hours=self._hours,
                current_time=current_time,
                snapshot_command=snapshot_command,
                report_command=report_command,
                items=(error_item,),
            )

        items = (*watch_items, *triage.items)
        return _snapshot_from_items(
            profile_key=profile.key,
            window_key=window.key,
            hours=self._hours,
            current_time=current_time,
            snapshot_command=snapshot_command,
            report_command=report_command,
            items=items,
            latest_reply_change=latest_reply_change,
            fallback_action=triage.primary_action,
        )


def _watch_state_for_snapshot(path: Path, current_time: datetime) -> tuple[WatchStateSummary | None, tuple[SupportTriageItem, ...]]:
    try:
        return summarize_watch_state(path, current_time), ()
    except SUPPORT_SNAPSHOT_ERRORS as exc:
        error_type = sanitize_diagnostic_text(type(exc).__name__, fallback="Exception", limit=80)
        message = f"file=unreadable type={error_type}"
        return None, (SupportTriageItem(TRIAGE_CRITICAL, "watch_state_file", message, watch_state_command_for_path(path)),)


def _support_snapshot_error_item(error: Exception) -> SupportTriageItem:
    error_type = sanitize_diagnostic_text(type(error).__name__, fallback="Exception", limit=80)
    detail = sanitize_diagnostic_text(str(error), fallback="", limit=120)
    message = f"support snapshot failed type={error_type}"
    if detail:
        message = f"{message} message={detail}"
    return SupportTriageItem(TRIAGE_CRITICAL, "db_integrity", message, "route74 db-health")


def _latest_reply_change(
    connection,
    *,
    profile_key: str,
    current_time: datetime,
    hours: int,
) -> DepartureChange | None:
    try:
        predictions = load_recent_bot_runtime_predictions(
            connection,
            current_time=current_time,
            hours=hours,
            limit=8,
            profile_key=profile_key,
            event_kind=BOT_EVENT_USER_REPLY,
        )
        if not predictions:
            return None
        changes = build_runtime_prediction_change_map(
            (predictions[0],),
            history_predictions=predictions,
        )
    except (TypeError, ValueError):
        return None
    return changes.get(predictions[0].id)


def _snapshot_from_items(
    *,
    profile_key: str,
    window_key: str,
    hours: int,
    current_time: datetime,
    snapshot_command: str,
    report_command: str,
    items: tuple[SupportTriageItem, ...],
    latest_reply_change: DepartureChange | None = None,
    fallback_action: str = DEFAULT_TRIAGE_ACTION,
) -> SupportSnapshot:
    primary = operator_triage_item_for_items(items)
    primary_action = primary.action if primary is not None else fallback_action
    return SupportSnapshot(
        profile_key=profile_key,
        window_key=window_key,
        hours=hours,
        current_time=current_time,
        status=_status_from_items(items),
        primary_action=primary_action,
        primary_issue=_snapshot_item(primary) if primary is not None else None,
        latest_reply_change=latest_reply_change,
        snapshot_command=snapshot_command,
        report_command=report_command,
        items=tuple(_snapshot_item(item) for item in items),
    )


def _status_from_items(items: tuple[SupportTriageItem, ...]) -> str:
    actionable = tuple(item for item in items if item.severity in {TRIAGE_WARNING, TRIAGE_CRITICAL})
    if not actionable:
        return TRIAGE_OK
    return max(actionable, key=lambda item: TRIAGE_STATUS_ORDER[item.severity]).severity


def _snapshot_item(item: SupportTriageItem) -> SupportSnapshotItem:
    return SupportSnapshotItem(
        severity=item.severity,
        key=item.key,
        message=item.message,
        action=item.action,
    )


def _positive_hours(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("support snapshot hours must be a positive integer")
    return value
