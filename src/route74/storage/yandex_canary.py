from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta

from route74.domain.commute import CommuteProfile
from route74.domain.profiles import PROFILE_KEYS
from route74.models import NOVOSIBIRSK_TZ, now_local, require_local_datetime
from route74.sources.yandex.models import YandexLiveForecast, YandexSourceStatus


CANARY_RISK_STATUSES = {
    YandexSourceStatus.PARSE_ERROR.value,
    YandexSourceStatus.NEEDS_SIGNATURE.value,
    YandexSourceStatus.BLOCKED.value,
    YandexSourceStatus.NO_TARGET.value,
}
CANARY_RUN_STATUSES = {"ok", "warning"}
CANARY_HEALTH_STATUSES = {"ok", "warning", "missing"}
CANARY_REASON_LIMIT = 160
CANARY_SCHEMA_HASH_LENGTH = 16
_ANSI_ESCAPE_PATTERN = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_HEX_DIGITS = frozenset("0123456789abcdef")


@dataclass(frozen=True)
class YandexCanaryRun:
    id: int
    checked_at: datetime
    status: str
    source_method: str
    profile_key: str
    schema_hash: str
    risk_reason: str
    changed_keys: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _positive_int("canary run id", self.id))
        object.__setattr__(
            self,
            "checked_at",
            _require_local_offset_datetime("canary run checked_at", self.checked_at),
        )
        object.__setattr__(
            self,
            "status",
            _enum_text("canary run status", self.status, CANARY_RUN_STATUSES),
        )
        object.__setattr__(self, "source_method", _plain_key("canary source method", self.source_method))
        object.__setattr__(self, "profile_key", _profile_key("canary profile key", self.profile_key))
        object.__setattr__(self, "schema_hash", _schema_hash_text(self.schema_hash))
        object.__setattr__(self, "risk_reason", _required_reason("canary risk reason", self.risk_reason))
        object.__setattr__(self, "changed_keys", _changed_keys_tuple(self.changed_keys))


@dataclass(frozen=True)
class YandexCanaryHealth:
    status: str
    latest_checked_at: datetime | None
    risk_reason: str
    risky_runs: int

    @property
    def healthy(self) -> bool:
        return self.status == "ok"

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "status",
            _enum_text("canary health status", self.status, CANARY_HEALTH_STATUSES),
        )
        if self.latest_checked_at is not None:
            object.__setattr__(
                self,
                "latest_checked_at",
                _require_local_offset_datetime("canary health latest_checked_at", self.latest_checked_at),
            )
        object.__setattr__(self, "risk_reason", _required_reason("canary health risk reason", self.risk_reason))
        object.__setattr__(self, "risky_runs", _non_negative_int("canary health risky_runs", self.risky_runs))
        if self.status == "ok" and self.risky_runs:
            raise ValueError("canary health risky_runs must be zero when status is ok")


def insert_yandex_canary_run(
    connection: sqlite3.Connection,
    *,
    profile: CommuteProfile,
    forecast: YandexLiveForecast,
    checked_at: datetime | None = None,
) -> YandexCanaryRun:
    if checked_at is None:
        checked_at = now_local()
    checked_at = require_local_datetime(checked_at, name="yandex canary checked_at")
    summary = _forecast_summary(forecast)
    schema_hash = _schema_hash(summary)
    previous_hash, previous_summary = _previous_ok_schema(
        connection,
        profile_key=profile.key,
        source_method=forecast.source_method.value,
        checked_at=checked_at,
    )
    status, reason = _status_reason(forecast, schema_hash=schema_hash, previous_hash=previous_hash)
    changed = _changed_keys(
        summary=summary,
        previous_summary=previous_summary,
        schema_hash=schema_hash,
        previous_hash=previous_hash,
    )
    cursor = connection.execute(
        """
        INSERT INTO yandex_canary_runs(
            checked_at, status, source_method, profile_key, schema_hash,
            changed_keys_json, risk_reason, raw_summary_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            checked_at.isoformat(),
            status,
            forecast.source_method.value,
            profile.key,
            schema_hash,
            json.dumps(changed, ensure_ascii=False),
            reason,
            json.dumps(summary, ensure_ascii=False),
        ),
    )
    return YandexCanaryRun(
        id=int(cursor.lastrowid),
        checked_at=checked_at,
        status=status,
        source_method=forecast.source_method.value,
        profile_key=profile.key,
        schema_hash=schema_hash,
        risk_reason=reason,
        changed_keys=_changed_key_names(changed),
    )


def summarize_yandex_canary_health(
    connection: sqlite3.Connection,
    *,
    current_time: datetime | None = None,
    hours: int = 24,
    required_profile_keys: tuple[str, ...] = (),
) -> YandexCanaryHealth:
    if current_time is None:
        current_time = now_local()
    current_time = require_local_datetime(current_time, name="yandex canary current_time")
    window_hours = _positive_int("hours", hours)
    required_profiles = _required_profile_keys(required_profile_keys)
    since = current_time - timedelta(hours=window_hours)
    try:
        rows = tuple(
            connection.execute(
                """
                SELECT
                    checked_at, status, profile_key, schema_hash,
                    risk_reason, changed_keys_json, raw_summary_json
                FROM yandex_canary_runs
                WHERE checked_at >= ?
                  AND checked_at <= ?
                ORDER BY checked_at DESC
                """,
                (since.isoformat(), current_time.isoformat()),
            ).fetchall()
        )
    except sqlite3.OperationalError:
        return YandexCanaryHealth("missing", None, "canary table is absent", 0)
    if not rows:
        return YandexCanaryHealth("missing", None, "no canary runs", 0)
    rows = _valid_canary_rows(rows, since=since, until=current_time)
    if not rows:
        return YandexCanaryHealth("missing", None, "no valid canary runs", 0)
    risky = tuple(row for row in rows if str(row["status"]) != "ok")
    latest = _dt(rows[0]["checked_at"])
    if risky:
        return YandexCanaryHealth("warning", latest, _risk_reason(risky[0]), len(risky))
    missing_profiles = _missing_profile_keys(rows, required_profiles)
    if missing_profiles:
        return YandexCanaryHealth(
            "missing",
            latest,
            f"missing canary profiles: {','.join(missing_profiles)}",
            0,
        )
    return YandexCanaryHealth("ok", latest, "latest canary runs are ok", 0)


def _forecast_summary(forecast: YandexLiveForecast) -> dict[str, object]:
    vehicle_fields = sorted(
        {
            field
            for vehicle in forecast.vehicles
            for field, value in {
                "vehicle_id": vehicle.vehicle_id,
                "thread_id": vehicle.thread_id,
                "lat": vehicle.lat,
                "lng": vehicle.lng,
                "arrival_minutes": vehicle.arrival_minutes,
                "age_seconds": vehicle.age_seconds,
            }.items()
            if value not in (None, "")
        }
    )
    return {
        "available": forecast.available,
        "status": forecast.status.value,
        "source_method": forecast.source_method.value,
        "arrival_count": len(forecast.arrival_minutes),
        "vehicle_count": forecast.vehicle_count,
        "vehicle_fields": vehicle_fields,
        "has_diagnostics": bool(forecast.diagnostics),
        "fallback_reason_prefix": forecast.fallback_reason.split(":", 1)[0],
    }


def _schema_hash(summary: dict[str, object]) -> str:
    payload = json.dumps(_schema_keys(summary), ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _schema_keys(summary: dict[str, object]) -> dict[str, object]:
    return {
        "status": summary["status"],
        "source_method": summary["source_method"],
        "has_arrivals": bool(summary["arrival_count"]),
        "vehicle_fields": summary["vehicle_fields"],
    }


def _previous_ok_schema(
    connection: sqlite3.Connection,
    *,
    profile_key: str,
    source_method: str,
    checked_at: datetime,
) -> tuple[str, dict[str, object]]:
    rows = connection.execute(
        """
        SELECT checked_at, schema_hash, raw_summary_json
        FROM yandex_canary_runs
        WHERE profile_key = ? AND source_method = ? AND status = 'ok'
        ORDER BY checked_at DESC
        """,
        (profile_key, source_method),
    ).fetchall()
    for row in rows:
        previous_checked_at = _dt(row["checked_at"])
        if previous_checked_at is None or not _datetime_at_or_before(previous_checked_at, checked_at):
            continue
        schema_hash = str(row["schema_hash"]).strip()
        summary = _json_object_or_none(row["raw_summary_json"])
        if schema_hash and summary:
            return schema_hash, summary
    return "", {}


def _status_reason(forecast: YandexLiveForecast, *, schema_hash: str, previous_hash: str) -> tuple[str, str]:
    if forecast.status.value in CANARY_RISK_STATUSES:
        return "warning", forecast.status.value
    if not forecast.available or not forecast.arrival_minutes:
        return "warning", forecast.fallback_reason or forecast.status.value
    if previous_hash and previous_hash != schema_hash:
        return "warning", "schema_changed"
    return "ok", "ok"


def _changed_keys(
    *,
    summary: dict[str, object],
    previous_summary: dict[str, object],
    schema_hash: str,
    previous_hash: str,
) -> dict[str, object]:
    if not previous_hash or previous_hash == schema_hash:
        return {}
    previous_keys = _schema_keys(previous_summary) if previous_summary else {}
    current_keys = _schema_keys(summary)
    changed = {
        key: {
            "previous": previous_keys.get(key),
            "current": current_keys.get(key),
        }
        for key in sorted(current_keys)
        if previous_keys.get(key) != current_keys.get(key)
    }
    return {
        "previous_schema_hash": previous_hash,
        "current_schema_hash": schema_hash,
        "changed": changed,
    }


def _risk_reason(row: sqlite3.Row) -> str:
    reason = _clean_reason(row["risk_reason"]) or "warning"
    changed = _changed_key_names(_json_object(row["changed_keys_json"]))
    if not changed:
        return reason
    return f"{reason}: changed={','.join(changed)}"


def _missing_profile_keys(rows: tuple[sqlite3.Row, ...], required_profile_keys: tuple[str, ...]) -> tuple[str, ...]:
    seen = {str(row["profile_key"]) for row in rows}
    return tuple(profile for profile in required_profile_keys if profile not in seen)


def _required_profile_keys(value: object) -> tuple[str, ...]:
    if not isinstance(value, tuple):
        raise ValueError("required canary profile keys need tuple")
    seen: set[str] = set()
    for profile_key in value:
        if not isinstance(profile_key, str) or not profile_key or profile_key != profile_key.strip():
            raise ValueError("required canary profile key must be a known profile key")
        if profile_key not in PROFILE_KEYS:
            expected = ", ".join(PROFILE_KEYS)
            raise ValueError(f"required canary profile key must be one of {expected}")
        if profile_key in seen:
            raise ValueError(f"duplicate required canary profile key: {profile_key}")
        seen.add(profile_key)
    return value


def _valid_canary_rows(
    rows: tuple[sqlite3.Row, ...],
    *,
    since: datetime,
    until: datetime,
) -> tuple[sqlite3.Row, ...]:
    return tuple(
        row
        for row in rows
        if _valid_canary_row(row, since=since, until=until)
    )


def _changed_key_names(payload: dict[str, object]) -> tuple[str, ...]:
    changed = payload.get("changed")
    if not isinstance(changed, dict):
        return ()
    names = (_plain_key_or_empty(key) for key in changed)
    return tuple(sorted(name for name in names if name))


def _json_object(value: object) -> dict[str, object]:
    payload = _json_object_or_none(value)
    return payload or {}


def _json_object_or_none(value: object) -> dict[str, object] | None:
    try:
        payload = json.loads(str(value))
    except (TypeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _positive_int(name: str, value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _non_negative_int(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _enum_text(label: str, value: object, choices: set[str]) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} is required")
    normalized = value.strip()
    if normalized not in choices:
        expected = ", ".join(sorted(choices))
        raise ValueError(f"{label} must be one of {expected}")
    return normalized


def _plain_key(label: str, value: object) -> str:
    normalized = _plain_key_or_empty(value)
    if not normalized:
        raise ValueError(f"{label} must be a plain key")
    return normalized


def _plain_key_or_empty(value: object) -> str:
    if not isinstance(value, str):
        return ""
    normalized = value.strip()
    if (
        not normalized
        or normalized != value
        or not normalized.isascii()
        or any(not (character.isalnum() or character == "_") for character in normalized)
    ):
        return ""
    return normalized


def _profile_key(label: str, value: object) -> str:
    profile_key = _plain_key(label, value)
    if profile_key not in PROFILE_KEYS:
        expected = ", ".join(PROFILE_KEYS)
        raise ValueError(f"{label} must be one of {expected}")
    return profile_key


def _schema_hash_text(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("canary schema hash is required")
    normalized = value.strip()
    if (
        len(normalized) != CANARY_SCHEMA_HASH_LENGTH
        or normalized != normalized.lower()
        or any(character not in _HEX_DIGITS for character in normalized)
    ):
        raise ValueError("canary schema hash must be a short lowercase hex digest")
    return normalized


def _required_reason(label: str, value: object) -> str:
    reason = _clean_reason(value)
    if not reason:
        raise ValueError(f"{label} is required")
    return reason


def _clean_reason(value: object) -> str:
    if not isinstance(value, str):
        return ""
    without_ansi = _ANSI_ESCAPE_PATTERN.sub("", value)
    printable = "".join(character if character.isprintable() else " " for character in without_ansi)
    return " ".join(printable.split())[:CANARY_REASON_LIMIT]


def _changed_keys_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, tuple):
        raise ValueError("canary changed keys need tuple")
    return tuple(_plain_key("canary changed key", key) for key in value)


def _require_local_offset_datetime(label: str, value: object) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")
    expected_offset = NOVOSIBIRSK_TZ.utcoffset(value.replace(tzinfo=None))
    if value.utcoffset() != expected_offset:
        raise ValueError(f"{label} must use Asia/Novosibirsk timezone")
    return value


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


def _dt(value: object) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _valid_canary_row(row: sqlite3.Row, *, since: datetime, until: datetime) -> bool:
    checked_at = _dt(row["checked_at"])
    if (
        checked_at is None
        or not _datetime_at_or_after(checked_at, since)
        or not _datetime_at_or_before(checked_at, until)
    ):
        return False
    status = str(row["status"])
    profile_key = str(row["profile_key"]).strip()
    schema_hash = str(row["schema_hash"]).strip()
    risk_reason = _clean_reason(row["risk_reason"])
    changed = _json_object_or_none(row["changed_keys_json"])
    summary = _json_object_or_none(row["raw_summary_json"])
    if status not in CANARY_RUN_STATUSES or not profile_key or not schema_hash or not risk_reason:
        return False
    if changed is None or summary is None or not summary:
        return False
    if status == "ok" and (risk_reason != "ok" or _changed_key_names(changed)):
        return False
    return True
