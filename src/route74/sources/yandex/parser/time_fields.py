from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from route74.models import require_local_datetime
from route74.sources.yandex.parser.common import as_float, number_at


MAX_REASONABLE_ETA_MINUTES = 180
MAX_FUTURE_TIMESTAMP_SKEW_SECONDS = 60
NON_ETA_NUMERIC_TOKENS = ("azimuth", "bearing", "coordinate", "distance", "length", "meters", "metre", "speed")
NON_ETA_NUMERIC_SEGMENTS = {"lat", "lng", "lon", "meter"}


def arrival_minutes(item: dict[str, Any], current_time: datetime) -> int | None:
    current_time = require_local_datetime(current_time, name="Yandex parser current_time")
    candidates: list[int] = []
    _collect_arrival_candidates(item, (), current_time, candidates)
    return min(candidates) if candidates else None


def age_seconds(item: dict[str, Any], current_time: datetime) -> int | None:
    current_time = require_local_datetime(current_time, name="Yandex parser current_time")
    explicit = number_at(item, ("age", "ageSeconds", "age_seconds"))
    if explicit is not None and explicit >= 0:
        return round(explicit)
    for key in ("timestamp", "updatedAt", "updated_at", "time", "timeNav", "time_nav"):
        age = _age_from_time(item.get(key), current_time)
        if age is not None:
            return age
    return None


def _collect_arrival_candidates(
    value: Any,
    path: tuple[str, ...],
    current_time: datetime,
    candidates: list[int],
) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            _collect_arrival_candidates(item, (*path, key), current_time, candidates)
        return
    if isinstance(value, list):
        for item in value:
            _collect_arrival_candidates(item, path, current_time, candidates)
        return
    minutes = _minutes_from_value(path, value, current_time)
    if minutes is not None:
        candidates.append(minutes)


def _minutes_from_value(path: tuple[str, ...], value: Any, current_time: datetime) -> int | None:
    number = as_float(value)
    if number is None:
        return None
    key = ".".join(path).casefold()
    if any(token in key for token in ("timestamp", "updated", "created", "nav")):
        return None
    if _looks_like_non_eta_number(path):
        return None
    if "seconds" in key and any(token in key for token in ("arrival", "eta", "wait", "left")):
        minutes = round(number / 60)
    elif any(token in key for token in ("arrival", "eta", "wait", "left", "minutes")):
        minutes = round(number)
    else:
        return None
    if 0 <= minutes <= MAX_REASONABLE_ETA_MINUTES:
        return int(minutes)
    return None


def _looks_like_non_eta_number(path: tuple[str, ...]) -> bool:
    segments = tuple(segment.casefold() for segment in path)
    key = ".".join(segments)
    return any(token in key for token in NON_ETA_NUMERIC_TOKENS) or any(
        segment in NON_ETA_NUMERIC_SEGMENTS for segment in segments
    )


def _age_from_time(value: Any, current_time: datetime) -> int | None:
    number = as_float(value)
    if number is not None:
        timestamp = number / 1000 if number > 10_000_000_000 else number
        try:
            observed = datetime.fromtimestamp(timestamp, tz=timezone.utc).astimezone(current_time.tzinfo)
        except (OSError, OverflowError):
            return None
        return _clamped_age_seconds(observed, current_time)
    if isinstance(value, str):
        try:
            observed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=current_time.tzinfo)
        return _clamped_age_seconds(observed.astimezone(current_time.tzinfo), current_time)
    return None


def _clamped_age_seconds(observed: datetime, current_time: datetime) -> int | None:
    age = round((current_time - observed).total_seconds())
    return None if age < -MAX_FUTURE_TIMESTAMP_SKEW_SECONDS else max(0, age)
