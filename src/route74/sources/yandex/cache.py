from __future__ import annotations

import sqlite3
from datetime import datetime
from math import isfinite
from pathlib import Path

from route74.domain.commute import CommuteProfile
from route74.domain.eta import EtaConfidence
from route74.sources.yandex.freshness import (
    DEFAULT_FRESH_VEHICLE_MAX_AGE_SECONDS,
    effective_vehicle_age_seconds,
    is_fresh_age,
)
from route74.sources.yandex.models import (
    YandexLiveForecast,
    YandexSourceMethod,
    YandexSourceStatus,
    YandexVehicle,
)
from route74.sources.yandex.trust import is_trusted_eta_observation
from route74.storage import connect_readonly
from route74.storage.helpers import arrival_minutes_from_json, optional_int_value


class CachedYandexForecastSource:
    def __init__(
        self,
        db_path: Path,
        *,
        max_age_seconds: int = 90,
        max_vehicle_age_seconds: int | None = DEFAULT_FRESH_VEHICLE_MAX_AGE_SECONDS,
    ) -> None:
        self._db_path = db_path
        self._max_age_seconds = max_age_seconds
        self._max_vehicle_age_seconds = max_vehicle_age_seconds

    def get_forecast(self, profile: CommuteProfile, current_time: datetime) -> YandexLiveForecast:
        try:
            with connect_readonly(self._db_path) as connection:
                row, future_rows = _latest_row(connection, profile.key, current_time)
                if row is None:
                    if future_rows:
                        return YandexLiveForecast.unavailable(
                            status=YandexSourceStatus.STALE,
                            reason="cache_future_only",
                        )
                    return _unavailable("cache_empty")

                sampled_at = datetime.fromisoformat(row["sampled_at"])
                age_seconds = round((current_time - sampled_at).total_seconds())
                if age_seconds < 0 or age_seconds > self._max_age_seconds:
                    return YandexLiveForecast.unavailable(
                        status=YandexSourceStatus.STALE,
                        reason=f"cache_stale:{age_seconds}s",
                    )
                vehicles = _vehicles_for_row(connection, row, age_seconds)
            arrivals = _arrival_minutes(row, age_seconds)
            status = _status(row["source_status"])
            method = _method(row["source_method"])
            vehicle_count = len(vehicles) or max(0, _optional_int(row["vehicle_count"]) or 0)
            newest_age_seconds = effective_vehicle_age_seconds(
                _optional_int(row["newest_age_seconds"]),
                snapshot_age_seconds=age_seconds,
            )
            if arrivals and not is_fresh_age(newest_age_seconds, max_age_seconds=self._max_vehicle_age_seconds):
                return YandexLiveForecast(
                    enabled=True,
                    available=False,
                    source_method=method,
                    status=YandexSourceStatus.STALE,
                    vehicles=vehicles,
                    vehicle_count=vehicle_count,
                    newest_age_seconds=newest_age_seconds,
                    confidence=EtaConfidence.LOW,
                    fallback_reason=f"cache_vehicle_stale:{newest_age_seconds}s",
                    raw_status=f"cached_snapshot:{age_seconds}s",
                )
            available = bool(row["available"]) and bool(arrivals)
            if bool(row["available"]) and not arrivals:
                status = YandexSourceStatus.STALE
            return YandexLiveForecast(
                enabled=True,
                available=available,
                source_method=method,
                status=status,
                arrival_minutes=arrivals if available else (),
                vehicles=vehicles,
                vehicle_count=vehicle_count,
                newest_age_seconds=newest_age_seconds,
                confidence=_confidence(row["confidence"], newest_age_seconds),
                fallback_reason=_cached_fallback_reason(row, arrivals),
                raw_status=f"cached_snapshot:{age_seconds}s",
            )
        except FileNotFoundError:
            return _unavailable("cache_db_missing")
        except sqlite3.Error as exc:
            return _unavailable(f"cache_db_error:{type(exc).__name__}")
        except (TypeError, ValueError) as exc:
            return _unavailable(f"cache_bad_row:{type(exc).__name__}")


def _latest_row(
    connection: sqlite3.Connection,
    profile_key: str,
    current_time: datetime,
) -> tuple[sqlite3.Row | None, int]:
    row = connection.execute(
        """
        SELECT id, sampled_at, source_method, source_status, available,
               yandex_snapshot_id, arrival_minutes, next_arrival_minutes_json, vehicle_count,
               newest_age_seconds, confidence, fallback_reason
        FROM yandex_forecast_samples
        WHERE profile_key = ?
          AND sampled_at <= ?
        ORDER BY sampled_at DESC, id DESC
        LIMIT 1
        """,
        (profile_key, current_time.isoformat()),
    ).fetchone()
    if row is not None:
        return row, 0

    latest = connection.execute(
        """
        SELECT id, sampled_at, source_method, source_status, available,
               yandex_snapshot_id, arrival_minutes, next_arrival_minutes_json, vehicle_count,
               newest_age_seconds, confidence, fallback_reason
        FROM yandex_forecast_samples
        WHERE profile_key = ?
        ORDER BY sampled_at DESC, id DESC
        LIMIT 1
        """,
        (profile_key,),
    ).fetchone()
    if latest is None:
        return None, 0
    try:
        sampled_at = datetime.fromisoformat(latest["sampled_at"])
        is_future = sampled_at > current_time
    except (TypeError, ValueError):
        return latest, 0
    return None, int(is_future)


def _vehicles_for_row(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    snapshot_age_seconds: int,
) -> tuple[YandexVehicle, ...]:
    rows = connection.execute(
        """
        SELECT vehicle_id, thread_id, lat, lng, arrival_minutes, age_seconds
        FROM yandex_vehicle_observations
        WHERE snapshot_id = ?
        ORDER BY id
        """,
        (int(row["yandex_snapshot_id"]),),
    ).fetchall()
    trusted = is_trusted_eta_observation(row["source_method"], fallback_reason=row["fallback_reason"])
    vehicles: list[YandexVehicle] = []
    for item in rows:
        try:
            vehicles.append(_vehicle_from_row(item, snapshot_age_seconds=snapshot_age_seconds, trusted_eta=trusted))
        except (TypeError, ValueError, OverflowError):
            continue
    return tuple(vehicles)


def _vehicle_from_row(row: sqlite3.Row, *, snapshot_age_seconds: int, trusted_eta: bool) -> YandexVehicle:
    return YandexVehicle(
        vehicle_id=str(row["vehicle_id"]),
        thread_id=str(row["thread_id"]),
        lat=_optional_float(row["lat"]),
        lng=_optional_float(row["lng"]),
        arrival_minutes=_vehicle_arrival_minutes(row["arrival_minutes"], snapshot_age_seconds, trusted_eta),
        age_seconds=effective_vehicle_age_seconds(
            _optional_int(row["age_seconds"]),
            snapshot_age_seconds=snapshot_age_seconds,
        ),
    )


def _arrival_minutes(row: sqlite3.Row, age_seconds: int) -> tuple[int, ...]:
    if not is_trusted_eta_observation(row["source_method"], fallback_reason=row["fallback_reason"]):
        return ()
    first = _optional_int(row["arrival_minutes"])
    rest = arrival_minutes_from_json(row["next_arrival_minutes_json"])
    age_minutes = max(0, age_seconds // 60)
    items = rest if first is None or first < 0 else (first, *rest)
    return tuple(value for item in items if (value := item - age_minutes) >= 0)


def _cached_fallback_reason(row: sqlite3.Row, arrivals: tuple[int, ...]) -> str:
    original = str(row["fallback_reason"] or "")
    if arrivals:
        return original
    if not bool(row["available"]):
        return original or "cache_no_eta"
    if is_trusted_eta_observation(row["source_method"], fallback_reason=row["fallback_reason"]):
        return "cache_arrivals_expired"
    return original or "cache_untrusted_eta"


def _vehicle_arrival_minutes(value: object, snapshot_age_seconds: int, trusted_eta: bool) -> int | None:
    if value is None or not trusted_eta:
        return None
    minutes = _optional_int(value)
    if minutes is None or minutes < 0:
        raise ValueError("invalid vehicle arrival minutes")
    aged = minutes - max(0, snapshot_age_seconds // 60)
    return aged if aged >= 0 else None


def _status(value: object) -> YandexSourceStatus:
    try:
        return YandexSourceStatus(str(value))
    except ValueError:
        return YandexSourceStatus.UNAVAILABLE


def _method(value: object) -> YandexSourceMethod:
    try:
        return YandexSourceMethod(str(value))
    except ValueError:
        return YandexSourceMethod.NONE


def _confidence(value: object, age_seconds: int | None) -> EtaConfidence:
    try:
        confidence = EtaConfidence(str(value))
    except ValueError:
        confidence = EtaConfidence.UNKNOWN
    if age_seconds is None:
        return confidence
    if age_seconds > 180:
        return EtaConfidence.LOW
    if age_seconds > 90 and confidence == EtaConfidence.HIGH:
        return EtaConfidence.MEDIUM
    return confidence


def _optional_int(value: object) -> int | None:
    return optional_int_value(value)


def _optional_float(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if isfinite(parsed) else None


def _unavailable(reason: str) -> YandexLiveForecast:
    return YandexLiveForecast.unavailable(status=YandexSourceStatus.UNAVAILABLE, reason=reason)
