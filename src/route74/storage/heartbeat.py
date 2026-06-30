from __future__ import annotations

import sqlite3
from datetime import datetime

from route74.models import NOVOSIBIRSK_TZ, now_local, require_local_datetime
from route74.storage.helpers import optional_int_value
from route74.storage.models import CollectorHeartbeat


def update_collector_heartbeat(
    connection: sqlite3.Connection,
    *,
    name: str,
    pid: int,
    profile_filter: str,
    last_status: str,
    last_message: str,
    updated_at: datetime | None = None,
) -> None:
    name = _normalize_name(name, "collector heartbeat")
    pid = _positive_int("collector heartbeat pid", pid)
    profile_filter = _normalize_text(profile_filter, "collector heartbeat profile filter")
    last_status = _normalize_text(last_status, "collector heartbeat status")
    last_message = _normalize_text(last_message, "collector heartbeat message")
    updated_at = updated_at or now_local()
    updated_at = _normalize_updated_at(updated_at, "collector heartbeat updated_at")
    connection.execute(
        """
        INSERT INTO collector_heartbeat(name, updated_at, pid, profile_filter, last_status, last_message)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(name) DO UPDATE SET
            updated_at = excluded.updated_at,
            pid = excluded.pid,
            profile_filter = excluded.profile_filter,
            last_status = excluded.last_status,
            last_message = excluded.last_message
        """,
        (name, updated_at.isoformat(), pid, profile_filter, last_status, last_message),
    )
    connection.commit()


def load_collector_heartbeat(connection: sqlite3.Connection, name: str) -> CollectorHeartbeat | None:
    name = _normalize_name(name, "collector heartbeat")
    row = connection.execute(
        """
        SELECT name, updated_at, pid, profile_filter, last_status, last_message
        FROM collector_heartbeat
        WHERE name = ?
        """,
        (name,),
    ).fetchone()
    if row is None:
        return None
    updated_at = _heartbeat_updated_at(row["updated_at"])
    pid = optional_int_value(row["pid"])
    if updated_at is None or pid is None or pid <= 0:
        return None
    return CollectorHeartbeat(
        name=row["name"],
        updated_at=updated_at,
        pid=pid,
        profile_filter=row["profile_filter"],
        last_status=row["last_status"],
        last_message=row["last_message"],
    )


def _heartbeat_updated_at(value: object) -> datetime | None:
    try:
        updated_at = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if not _has_local_offset(updated_at):
        return None
    return updated_at


def load_bot_update_offset(connection: sqlite3.Connection, name: str) -> int:
    name = _normalize_name(name, "bot update offset")
    row = connection.execute(
        """
        SELECT update_offset
        FROM bot_update_offsets
        WHERE name = ?
        """,
        (name,),
    ).fetchone()
    if row is None:
        return 0
    update_offset = optional_int_value(row["update_offset"])
    if update_offset is None:
        return 0
    return max(0, update_offset)


def save_bot_update_offset(
    connection: sqlite3.Connection,
    *,
    name: str,
    update_offset: int,
    updated_at: datetime | None = None,
) -> None:
    name = _normalize_name(name, "bot update offset")
    updated_at = updated_at or now_local()
    updated_at = _normalize_updated_at(updated_at, "bot update offset updated_at")
    update_offset = max(load_bot_update_offset(connection, name), _normalize_update_offset(update_offset))
    connection.execute(
        """
        INSERT INTO bot_update_offsets(name, update_offset, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(name) DO UPDATE SET
            update_offset = excluded.update_offset,
            updated_at = excluded.updated_at
        """,
        (name, update_offset, updated_at.isoformat()),
    )
    connection.commit()


def _normalize_update_offset(update_offset: int) -> int:
    if isinstance(update_offset, bool) or not isinstance(update_offset, int):
        raise ValueError("bot update offset needs non-negative integer")
    return max(0, update_offset)


def _positive_int(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} needs positive integer")
    return value


def _normalize_text(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} is required")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{label} is required")
    return normalized


def _normalize_updated_at(value: object, label: str) -> datetime:
    return require_local_datetime(value, name=label)


def _has_local_offset(value: object) -> bool:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        return False
    expected_offset = NOVOSIBIRSK_TZ.utcoffset(value.replace(tzinfo=None))
    return value.utcoffset() == expected_offset


def _normalize_name(name: object, label: str) -> str:
    if not isinstance(name, str):
        raise ValueError(f"{label} name is required")
    normalized = name.strip()
    if not normalized:
        raise ValueError(f"{label} name is required")
    return normalized
