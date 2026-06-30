from __future__ import annotations

import hashlib
import re
import sqlite3
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from route74.diagnostics import sanitize_diagnostic_text
from route74.domain.profiles import PROFILE_KEYS
from route74.domain.runtime_sources import BOT_EVENT_KINDS, BOT_EVENT_USER_REPLY
from route74.models import now_local
from route74.storage.connection import connect, init_db
from route74.storage.helpers import count_rows, optional_int_value
from route74.storage.models import CountByKey, percent

NO_ETA_REPLY_SOURCE = "no_eta"
ERROR_CATEGORY_PREFIXES = (
    "followup_send_error",
    "decision_record_error",
    "watch_start_error",
    "send_error",
    "reply_error",
)


@dataclass(frozen=True)
class BotInteractionEvent:
    received_at: datetime
    chat_id: int
    update_type: str
    command: str
    event_kind: str
    reply_source: str
    yandex_source_method: str
    forecast_ms: int
    render_ms: int
    send_ms: int
    total_ms: int
    status: str
    error: str = ""
    profile_key: str = ""
    no_eta_reason: str = ""

    def __post_init__(self) -> None:
        _ensure_aware_datetime("received_at", self.received_at)
        if isinstance(self.chat_id, bool) or not isinstance(self.chat_id, int):
            raise ValueError("bot interaction chat_id needs integer")
        for field_name in (
            "update_type",
            "event_kind",
            "reply_source",
            "yandex_source_method",
            "status",
        ):
            _ensure_plain_key(field_name, getattr(self, field_name))
        if self.event_kind not in BOT_EVENT_KINDS:
            allowed = ", ".join(sorted(BOT_EVENT_KINDS))
            raise ValueError(f"bot interaction event_kind must be one of {allowed}")
        if self.profile_key:
            _ensure_profile_key("profile_key", self.profile_key)
        if self.no_eta_reason:
            _ensure_plain_key("no_eta_reason", self.no_eta_reason)
        _ensure_text("command", self.command)
        _ensure_text("error", self.error)
        for field_name in _DURATION_COLUMNS:
            _ensure_non_negative_int(field_name, getattr(self, field_name))


@dataclass(frozen=True)
class BotLatencySummary:
    hours: int
    latest_received_at: datetime | None
    total_events: int
    invalid_duration_events: int
    error_events: int
    no_eta_events: int
    p50_total_ms: int | None
    p95_total_ms: int | None
    p95_forecast_ms: int | None
    p95_send_ms: int | None
    statuses: tuple[CountByKey, ...]
    source_methods: tuple[CountByKey, ...]
    update_types: tuple[CountByKey, ...]
    event_kinds: tuple[CountByKey, ...]
    reply_sources: tuple[CountByKey, ...]
    error_reasons: tuple[CountByKey, ...]
    error_categories: tuple[CountByKey, ...] = ()
    no_eta_reasons: tuple[CountByKey, ...] = ()
    profile_key: str | None = None
    event_kind: str | None = None
    p95_render_ms: int | None = None

    @property
    def error_rate_percent(self) -> int:
        return percent(self.error_events, self.total_events)

    @property
    def no_eta_rate_percent(self) -> int:
        return percent(self.no_eta_events, self.total_events)

    @property
    def top_no_eta_reason(self) -> CountByKey | None:
        if not self.no_eta_reasons:
            return None
        return self.no_eta_reasons[0]


class BotLatencyRecorder:
    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path

    def record(self, event: BotInteractionEvent) -> int:
        with connect(self._db_path) as connection:
            init_db(connection)
            return insert_bot_interaction_event(connection, event)


def insert_bot_interaction_event(connection: sqlite3.Connection, event: BotInteractionEvent) -> int:
    columns = [
        "received_at",
        "chat_id_hash",
        "update_type",
        "command",
        "event_kind",
        "profile_key",
        "reply_source",
        "yandex_source_method",
        "forecast_ms",
        "render_ms",
        "send_ms",
        "total_ms",
        "status",
        "error",
    ]
    values: list[object] = [
        event.received_at.isoformat(),
        _chat_hash(event.chat_id),
        event.update_type,
        _stored_command(event.command),
        event.event_kind,
        event.profile_key,
        event.reply_source,
        event.yandex_source_method,
        max(0, event.forecast_ms),
        max(0, event.render_ms),
        max(0, event.send_ms),
        max(0, event.total_ms),
        event.status,
        _stored_error(event.error),
    ]
    if _has_bot_interaction_column(connection, "no_eta_reason"):
        columns.append("no_eta_reason")
        values.append(event.no_eta_reason)
    placeholders = ", ".join("?" for _column in columns)
    cursor = connection.execute(
        f"INSERT INTO bot_interaction_events({', '.join(columns)}) VALUES ({placeholders})",
        tuple(values),
    )
    return int(cursor.lastrowid)


def summarize_bot_latency(
    connection: sqlite3.Connection,
    *,
    hours: int,
    current_time: datetime | None = None,
    profile_key: str | None = None,
    event_kind: str | None = None,
) -> BotLatencySummary:
    current_time = current_time or now_local()
    window_hours = _positive_int("hours", hours)
    profile_key = _optional_profile_key(profile_key)
    event_kind = _optional_event_kind(event_kind)
    since = current_time - timedelta(hours=window_hours)
    where = "received_at >= ? AND received_at <= ?"
    params: list[object] = [since.isoformat(), current_time.isoformat()]
    if profile_key is not None:
        where += " AND profile_key = ?"
        params.append(profile_key)
    has_event_kind_column = _has_bot_interaction_column(connection, "event_kind")
    if event_kind is not None and has_event_kind_column:
        where += " AND event_kind = ?"
        params.append(event_kind)
    no_eta_reason_column = (
        "no_eta_reason" if _has_bot_interaction_column(connection, "no_eta_reason") else "'' AS no_eta_reason"
    )
    event_kind_column = "event_kind" if has_event_kind_column else f"'{BOT_EVENT_USER_REPLY}' AS event_kind"
    raw_rows = connection.execute(
        f"""
        SELECT received_at, total_ms, forecast_ms, render_ms, send_ms, status, update_type,
               {event_kind_column}, reply_source, yandex_source_method, error, {no_eta_reason_column}
        FROM bot_interaction_events
        WHERE {where}
        ORDER BY received_at DESC
        """,
        tuple(params),
    ).fetchall()
    rows = tuple(
        row
        for row, _received_at in _valid_received_rows(raw_rows, since=since, until=current_time)
        if event_kind is None or str(row["event_kind"]) == event_kind
    )
    statuses = Counter(str(row["status"]) for row in rows)
    methods = Counter(str(row["yandex_source_method"]) for row in rows)
    update_types = Counter(str(row["update_type"]) for row in rows)
    event_kinds = Counter(str(row["event_kind"]) for row in rows)
    reply_sources = Counter(str(row["reply_source"]) for row in rows)
    error_reasons = Counter(_safe_error_key(row["error"]) for row in rows if str(row["status"]) != "ok")
    error_categories = Counter(_safe_error_category(row["error"]) for row in rows if str(row["status"]) != "ok")
    no_eta_reasons = Counter(
        _safe_no_eta_reason_key(row["no_eta_reason"]) for row in rows if str(row["reply_source"]) == NO_ETA_REPLY_SOURCE
    )
    return BotLatencySummary(
        hours=window_hours,
        latest_received_at=_latest_valid_received_at(
            connection,
            current_time=current_time,
            profile_key=profile_key,
            event_kind=event_kind,
            has_event_kind_column=has_event_kind_column,
        ),
        total_events=len(rows),
        invalid_duration_events=sum(1 for row in rows if _has_invalid_duration(row)),
        error_events=sum(count for status, count in statuses.items() if status != "ok"),
        no_eta_events=reply_sources[NO_ETA_REPLY_SOURCE],
        p50_total_ms=_percentile(_duration_values(rows, "total_ms"), 50),
        p95_total_ms=_percentile(_duration_values(rows, "total_ms"), 95),
        p95_forecast_ms=_percentile(_duration_values(rows, "forecast_ms"), 95),
        p95_send_ms=_percentile(_duration_values(rows, "send_ms"), 95),
        statuses=count_rows(statuses),
        source_methods=count_rows(methods),
        update_types=count_rows(update_types),
        event_kinds=count_rows(event_kinds),
        reply_sources=count_rows(reply_sources),
        error_reasons=count_rows(error_reasons),
        error_categories=count_rows(error_categories),
        no_eta_reasons=count_rows(no_eta_reasons),
        profile_key=profile_key,
        event_kind=event_kind,
        p95_render_ms=_percentile(_duration_values(rows, "render_ms"), 95),
    )


def _chat_hash(chat_id: int) -> str:
    return hashlib.sha256(str(chat_id).encode("utf-8")).hexdigest()[:16]


def _percentile(values: tuple[int, ...], percentile: int) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    index = round((len(ordered) - 1) * percentile / 100)
    return ordered[index]


def _duration_values(rows: Iterable[sqlite3.Row], column: str) -> tuple[int, ...]:
    return tuple(duration for row in rows if (duration := _nonnegative_int(row[column])) is not None)


_DURATION_COLUMNS = ("forecast_ms", "render_ms", "send_ms", "total_ms")


def _has_invalid_duration(row: sqlite3.Row) -> bool:
    return any(_nonnegative_int(row[column]) is None for column in _DURATION_COLUMNS)


def _nonnegative_int(value: object) -> int | None:
    parsed = optional_int_value(value)
    if parsed is None or parsed < 0:
        return None
    return parsed


def _positive_int(name: str, value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _optional_profile_key(value: str | None) -> str | None:
    if value is None:
        return None
    _ensure_profile_key("profile_key", value)
    return value


def _optional_event_kind(value: str | None) -> str | None:
    if value is None:
        return None
    if value not in BOT_EVENT_KINDS:
        allowed = ", ".join(sorted(BOT_EVENT_KINDS))
        raise ValueError(f"event kind must be one of {allowed}")
    return value


def _ensure_profile_key(name: str, value: object) -> None:
    _ensure_plain_key(name, value)
    if value not in PROFILE_KEYS:
        expected = ", ".join(PROFILE_KEYS)
        raise ValueError(f"{name} must be one of {expected}")


def _ensure_aware_datetime(name: str, value: object) -> None:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError(f"bot interaction {name} needs timezone-aware datetime")


def _ensure_non_negative_int(name: str, value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"bot interaction {name} needs non-negative integer")


def _ensure_text(name: str, value: object) -> None:
    if not isinstance(value, str):
        raise ValueError(f"bot interaction {name} needs text")


def _ensure_plain_key(name: str, value: object) -> None:
    _ensure_text(name, value)
    if not value.strip() or value != value.strip() or any(char.isspace() for char in value):
        raise ValueError(f"bot interaction {name} needs plain key")


def _datetime_value(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _valid_received_rows(
    rows: Iterable[sqlite3.Row],
    *,
    since: datetime,
    until: datetime,
) -> tuple[tuple[sqlite3.Row, datetime], ...]:
    valid: list[tuple[sqlite3.Row, datetime]] = []
    for row in rows:
        received_at = _datetime_value(row["received_at"])
        if (
            received_at is not None
            and _datetime_at_or_after(received_at, since)
            and _datetime_at_or_before(received_at, until)
        ):
            valid.append((row, received_at))
    return tuple(valid)


def _latest_valid_received_at(
    connection: sqlite3.Connection,
    *,
    current_time: datetime,
    profile_key: str | None,
    event_kind: str | None,
    has_event_kind_column: bool,
) -> datetime | None:
    where = ""
    params: list[object] = []
    if profile_key is not None:
        where = "WHERE profile_key = ?"
        params.append(profile_key)
    if event_kind is not None and has_event_kind_column:
        where = f"{where} AND event_kind = ?" if where else "WHERE event_kind = ?"
        params.append(event_kind)
    rows = connection.execute(
        f"""
        SELECT received_at,
               {"event_kind" if has_event_kind_column else f"'{BOT_EVENT_USER_REPLY}' AS event_kind"}
        FROM bot_interaction_events
        {where}
        ORDER BY received_at DESC
        """,
        tuple(params),
    ).fetchall()
    for row in rows:
        if event_kind is not None and str(row["event_kind"]) != event_kind:
            continue
        received_at = _datetime_value(row["received_at"])
        if received_at is not None and _datetime_at_or_before(received_at, current_time):
            return received_at
    return None


def _datetime_at_or_after(value: datetime, boundary: datetime) -> bool:
    try:
        return value >= boundary
    except TypeError:
        return False


def _datetime_at_or_before(value: datetime, boundary: datetime) -> bool:
    try:
        return value <= boundary
    except TypeError:
        return False


def _safe_error_key(error: object) -> str:
    return sanitize_diagnostic_text(error, fallback="unknown_error", limit=120)


def _safe_error_category(error: object) -> str:
    key = _safe_error_key(error)
    if key == "unknown_error":
        return key
    for prefix in ERROR_CATEGORY_PREFIXES:
        if key == prefix or key.startswith(f"{prefix}:"):
            return prefix
    for prefix in ERROR_CATEGORY_PREFIXES:
        if f"; {prefix}:" in key:
            return prefix
    return key


def _safe_no_eta_reason_key(reason: object) -> str:
    return sanitize_diagnostic_text(reason, fallback="unknown_no_eta", limit=120)


def _has_bot_interaction_column(connection: sqlite3.Connection, column_name: str) -> bool:
    return column_name in {
        str(row["name"]) for row in connection.execute("PRAGMA table_info(bot_interaction_events)").fetchall()
    }


_SLASH_COMMAND_RE = re.compile(r"^/[A-Za-z0-9_]{1,64}(?:@[A-Za-z0-9_]{1,64})?$")


def _stored_command(command: object) -> str:
    if not isinstance(command, str):
        return ""
    text = " ".join(command.strip().split())
    if not text:
        return ""
    token = text.split(maxsplit=1)[0]
    if _SLASH_COMMAND_RE.fullmatch(token):
        return token.split("@", 1)[0].casefold()
    return sanitize_diagnostic_text(text, fallback="", limit=80)


def _stored_error(error: object) -> str:
    return sanitize_diagnostic_text(error, fallback="", limit=240)
