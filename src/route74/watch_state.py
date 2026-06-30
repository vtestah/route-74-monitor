from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from math import ceil
from pathlib import Path
from typing import Any

from route74.diagnostics import sanitize_diagnostic_text
from route74.domain.commute import CommuteProfile
from route74.domain.profiles import profile_by_key
from route74.domain.walk_buffer import is_valid_walk_minutes
from route74.domain.watch_policy import WATCH_DURATION_MINUTES, WATCH_POLL_INTERVAL_SECONDS


DEFAULT_WATCH_STATE_PATH = Path("data/web_watches.json")
WATCH_STATE_TTL = timedelta(minutes=WATCH_DURATION_MINUTES)
WATCH_STATE_OVERDUE_AFTER = timedelta(seconds=WATCH_POLL_INTERVAL_SECONDS * 2)


@dataclass(frozen=True)
class WatchState:
    watch_key: str
    profile: CommuteProfile
    walk_minutes: int
    started_at: datetime
    next_poll_at: datetime
    early_sent: bool
    last_error_type: str = ""
    last_error_at: datetime | None = None
    error_count: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.watch_key, str) or not self.watch_key or self.watch_key != self.watch_key.strip():
            raise ValueError("watch key must be non-empty text without leading or trailing whitespace")
        if not isinstance(self.last_error_type, str):
            raise ValueError("watch error type must be text")
        if isinstance(self.error_count, bool) or not isinstance(self.error_count, int) or self.error_count < 0:
            raise ValueError("watch error count must be a non-negative integer")
        if self.last_error_at is not None:
            if not isinstance(self.last_error_at, datetime) or self.last_error_at.tzinfo is None:
                raise ValueError("watch error datetime must be timezone-aware")
        object.__setattr__(
            self,
            "last_error_type",
            sanitize_diagnostic_text(self.last_error_type, fallback="", limit=80),
        )


@dataclass(frozen=True)
class WatchStateReadResult:
    path: Path
    current_time: datetime
    states: tuple[WatchState, ...]
    total_records: int
    expired_records: int
    invalid_records: int
    file_status: str
    error_type: str = ""

    @property
    def active_count(self) -> int:
        return len(self.states)


@dataclass(frozen=True)
class WatchStateProfileSummary:
    profile_key: str
    active_count: int
    due_count: int
    early_sent_count: int
    oldest_age_minutes: int | None
    next_poll_at: datetime | None
    runtime_error_count: int = 0
    runtime_error_records: int = 0
    latest_error_at: datetime | None = None
    runtime_error_types: tuple[str, ...] = ()
    expires_at: datetime | None = None
    expires_in_minutes: int | None = None


@dataclass(frozen=True)
class WatchStateSummary:
    path: Path
    current_time: datetime
    status: str
    active_count: int
    due_count: int
    overdue_count: int
    expired_records: int
    invalid_records: int
    total_records: int
    early_sent_count: int
    oldest_age_minutes: int | None
    next_poll_at: datetime | None
    max_overdue_seconds: int | None
    file_status: str
    error_type: str
    profiles: tuple[WatchStateProfileSummary, ...]
    runtime_error_count: int = 0
    runtime_error_records: int = 0
    latest_error_at: datetime | None = None
    runtime_error_types: tuple[str, ...] = ()
    expires_at: datetime | None = None
    expires_in_minutes: int | None = None


def load_watch_states(path: Path, current_time: datetime) -> WatchStateReadResult:
    raw_result = _read_raw_states(path)
    if raw_result.file_status != "ok":
        return WatchStateReadResult(
            path=path,
            current_time=current_time,
            states=(),
            total_records=0,
            expired_records=0,
            invalid_records=0,
            file_status=raw_result.file_status,
            error_type=raw_result.error_type,
        )

    states: list[WatchState] = []
    expired_records = 0
    invalid_records = 0
    for watch_key, value in raw_result.data.items():
        parse_result = _parse_state(watch_key, value, current_time)
        if parse_result.status == "active" and parse_result.state is not None:
            states.append(parse_result.state)
        elif parse_result.status == "expired":
            expired_records += 1
        else:
            invalid_records += 1
    return WatchStateReadResult(
        path=path,
        current_time=current_time,
        states=tuple(states),
        total_records=len(raw_result.data),
        expired_records=expired_records,
        invalid_records=invalid_records,
        file_status="ok",
    )


def summarize_watch_state(path: Path, current_time: datetime) -> WatchStateSummary:
    result = load_watch_states(path, current_time)
    states = result.states
    due_states = tuple(state for state in states if state.next_poll_at <= current_time)
    overdue_states = tuple(
        state for state in states if current_time - state.next_poll_at >= WATCH_STATE_OVERDUE_AFTER
    )
    runtime_errors = _runtime_error_summary(states)
    next_poll_at = min((state.next_poll_at for state in states), default=None)
    oldest_started_at = min((state.started_at for state in states), default=None)
    expires_at = _next_expiration_at(states)
    max_overdue_seconds = (
        max(int((current_time - state.next_poll_at).total_seconds()) for state in overdue_states)
        if overdue_states
        else None
    )
    return WatchStateSummary(
        path=path,
        current_time=current_time,
        status=_summary_status(
            result,
            overdue_count=len(overdue_states),
            runtime_error_count=runtime_errors.error_count,
        ),
        active_count=len(states),
        due_count=len(due_states),
        overdue_count=len(overdue_states),
        expired_records=result.expired_records,
        invalid_records=result.invalid_records,
        total_records=result.total_records,
        early_sent_count=sum(1 for state in states if state.early_sent),
        oldest_age_minutes=_age_minutes(current_time, oldest_started_at),
        next_poll_at=next_poll_at,
        max_overdue_seconds=max_overdue_seconds,
        file_status=result.file_status,
        error_type=result.error_type,
        profiles=_profile_summaries(states, current_time),
        runtime_error_count=runtime_errors.error_count,
        runtime_error_records=runtime_errors.error_records,
        latest_error_at=runtime_errors.latest_error_at,
        runtime_error_types=runtime_errors.runtime_error_types,
        expires_at=expires_at,
        expires_in_minutes=_remaining_minutes(current_time, expires_at),
    )


def format_watch_state_summary(summary: WatchStateSummary, path_label: str | None = None) -> str:
    label = path_label or str(summary.path)
    runtime_error_text = ""
    if summary.runtime_error_count:
        runtime_error_text = (
            f" runtime_errors={summary.runtime_error_count} "
            f"runtime_watches={summary.runtime_error_records} "
            f"latest_error={_format_datetime(summary.latest_error_at)} "
            f"error_types={_error_types_text(summary.runtime_error_types)}"
        )
    lines = [
        (
            f"watch-state status={summary.status} path={label} file={summary.file_status} "
            f"active={summary.active_count} due={summary.due_count} overdue={summary.overdue_count} "
            f"expired={summary.expired_records} invalid={summary.invalid_records} total={summary.total_records} "
            f"early_sent={summary.early_sent_count} oldest_age={_minutes(summary.oldest_age_minutes)} "
            f"next_poll={_format_datetime(summary.next_poll_at)} "
            f"expires_in={_minutes(summary.expires_in_minutes)} "
            f"expires_at={_format_datetime(summary.expires_at)} "
            f"max_overdue={_seconds(summary.max_overdue_seconds)}{runtime_error_text}"
        )
    ]
    if summary.file_status == "missing":
        lines.append("- info watch_state_file: file not created yet")
    if summary.error_type:
        lines.append(f"- {summary.status} watch_state_file: unreadable type={summary.error_type}")
    if summary.overdue_count:
        lines.append(
            f"- warning watch_state_overdue: {summary.overdue_count} active watch polls are overdue"
        )
    if summary.runtime_error_count:
        lines.append(
            (
                f"- warning watch_state_runtime_error: errors={summary.runtime_error_count} "
                f"watches={summary.runtime_error_records} latest={_format_datetime(summary.latest_error_at)} "
                f"types={_error_types_text(summary.runtime_error_types)}"
            )
        )
    if summary.expired_records:
        lines.append(f"- info watch_state_expired: {summary.expired_records} stale records ignored")
    if summary.invalid_records:
        lines.append(f"- warning watch_state_invalid: {summary.invalid_records} malformed records ignored")
    if not summary.profiles:
        if summary.file_status in {"ok", "missing"}:
            lines.append(f"- {summary.status} watch_state_empty: no active watches")
        return "\n".join(lines)
    for profile in summary.profiles:
        runtime_error_text = ""
        if profile.runtime_error_count:
            runtime_error_text = (
                f" errors={profile.runtime_error_count}"
                f" latest_error={_format_datetime(profile.latest_error_at)}"
                f" types={_error_types_text(profile.runtime_error_types)}"
            )
        lines.append(
            (
                f"- profile={profile.profile_key} active={profile.active_count} due={profile.due_count} "
                f"early_sent={profile.early_sent_count} "
                f"oldest_age={_minutes(profile.oldest_age_minutes)} "
                f"next_poll={_format_datetime(profile.next_poll_at)} "
                f"expires_in={_minutes(profile.expires_in_minutes)} "
                f"expires_at={_format_datetime(profile.expires_at)}{runtime_error_text}"
            )
        )
    return "\n".join(lines)


def watch_state_json(state: WatchState) -> dict[str, object]:
    data = {
        "profile_key": state.profile.key,
        "walk_minutes": state.walk_minutes,
        "started_at": state.started_at.isoformat(),
        "next_poll_at": state.next_poll_at.isoformat(),
        "early_sent": state.early_sent,
    }
    if state.error_count:
        data["error_count"] = state.error_count
    if state.last_error_type:
        data["last_error_type"] = state.last_error_type
    if state.last_error_at is not None:
        data["last_error_at"] = state.last_error_at.isoformat()
    return data


@dataclass(frozen=True)
class _RawWatchStateRead:
    data: dict[str, Any]
    file_status: str
    error_type: str = ""


@dataclass(frozen=True)
class _ParseStateResult:
    status: str
    state: WatchState | None = None


def _read_raw_states(path: Path) -> _RawWatchStateRead:
    if not path.exists():
        return _RawWatchStateRead({}, "missing")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        return _RawWatchStateRead({}, "unreadable", type(exc).__name__)
    if not isinstance(data, dict):
        return _RawWatchStateRead({}, "invalid_root", type(data).__name__)
    return _RawWatchStateRead(data, "ok")


def _parse_state(chat_id: str, raw: object, current_time: datetime) -> _ParseStateResult:
    if not isinstance(raw, dict):
        return _ParseStateResult("invalid")
    try:
        state = WatchState(
            watch_key=_watch_key(chat_id),
            profile=profile_by_key(raw.get("profile_key")),
            walk_minutes=_walk_minutes(raw.get("walk_minutes")),
            started_at=_datetime(raw.get("started_at")),
            next_poll_at=_datetime(raw.get("next_poll_at")),
            early_sent=raw.get("early_sent") is True,
            last_error_type=_error_type(raw.get("last_error_type")),
            last_error_at=_optional_datetime(raw.get("last_error_at")),
            error_count=_error_count(raw.get("error_count")),
        )
    except (TypeError, ValueError):
        return _ParseStateResult("invalid")
    if current_time - state.started_at >= WATCH_STATE_TTL:
        return _ParseStateResult("expired")
    return _ParseStateResult("active", state)


def _summary_status(result: WatchStateReadResult, *, overdue_count: int, runtime_error_count: int) -> str:
    if result.file_status != "ok":
        if result.file_status == "missing":
            return "ok"
        if result.file_status in {"unreadable", "invalid_root"}:
            return "critical"
        return result.file_status
    if overdue_count:
        return "warning"
    if runtime_error_count:
        return "warning"
    if result.expired_records or result.invalid_records:
        return "degraded"
    return "ok"


def _profile_summaries(
    states: tuple[WatchState, ...],
    current_time: datetime,
) -> tuple[WatchStateProfileSummary, ...]:
    summaries: list[WatchStateProfileSummary] = []
    for profile in _profile_keys(states):
        profile_states = tuple(state for state in states if state.profile.key == profile)
        runtime_errors = _runtime_error_summary(profile_states)
        expires_at = _next_expiration_at(profile_states)
        summaries.append(
            WatchStateProfileSummary(
                profile_key=profile,
                active_count=len(profile_states),
                due_count=sum(1 for state in profile_states if state.next_poll_at <= current_time),
                early_sent_count=sum(1 for state in profile_states if state.early_sent),
                oldest_age_minutes=_age_minutes(
                    current_time,
                    min((state.started_at for state in profile_states), default=None),
                ),
                next_poll_at=min((state.next_poll_at for state in profile_states), default=None),
                runtime_error_count=runtime_errors.error_count,
                runtime_error_records=runtime_errors.error_records,
                latest_error_at=runtime_errors.latest_error_at,
                runtime_error_types=runtime_errors.runtime_error_types,
                expires_at=expires_at,
                expires_in_minutes=_remaining_minutes(current_time, expires_at),
            )
        )
    return tuple(summaries)


def _profile_keys(states: tuple[WatchState, ...]) -> tuple[str, ...]:
    return tuple(sorted(dict.fromkeys(state.profile.key for state in states)))


def _age_minutes(current_time: datetime, started_at: datetime | None) -> int | None:
    if started_at is None:
        return None
    return max(0, int((current_time - started_at).total_seconds() // 60))


def _next_expiration_at(states: tuple[WatchState, ...]) -> datetime | None:
    return min((state.started_at + WATCH_STATE_TTL for state in states), default=None)


def _remaining_minutes(current_time: datetime, target_time: datetime | None) -> int | None:
    if target_time is None:
        return None
    return max(0, ceil((target_time - current_time).total_seconds() / 60))


def _minutes(value: int | None) -> str:
    return "n/a" if value is None else str(value)


def _seconds(value: int | None) -> str:
    return "n/a" if value is None else str(value)


def _format_datetime(value: datetime | None) -> str:
    return "n/a" if value is None else value.isoformat()


def _error_count(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value


def _optional_datetime(value: object) -> datetime | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed


def _error_type(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return sanitize_diagnostic_text(value, fallback="", limit=80)


@dataclass(frozen=True)
class _RuntimeErrorSummary:
    error_count: int
    error_records: int
    latest_error_at: datetime | None
    runtime_error_types: tuple[str, ...]


def _runtime_error_summary(states: tuple[WatchState, ...]) -> _RuntimeErrorSummary:
    error_states = tuple(state for state in states if state.error_count > 0)
    latest_error_at = max(
        (state.last_error_at for state in error_states if state.last_error_at is not None),
        default=None,
    )
    runtime_error_types = tuple(
        sorted(dict.fromkeys(state.last_error_type for state in error_states if state.last_error_type))
    )
    return _RuntimeErrorSummary(
        error_count=sum(state.error_count for state in error_states),
        error_records=len(error_states),
        latest_error_at=latest_error_at,
        runtime_error_types=runtime_error_types,
    )


def _error_types_text(values: tuple[str, ...]) -> str:
    return "n/a" if not values else ", ".join(values)


def _watch_key(value: object) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or any(character.isspace() for character in value):
        raise ValueError("watch key must be plain non-empty text")
    return value


def _walk_minutes(value: object) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not is_valid_walk_minutes(value)
    ):
        raise ValueError("watch walk minutes is out of range")
    return value


def _datetime(value: object) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError("watch datetime must be text")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("watch datetime must be timezone-aware")
    return parsed
