from __future__ import annotations

import re
from datetime import datetime, time, timedelta, timezone
from math import isfinite
from typing import Any

from route74.domain.commute import CommuteProfile
from route74.domain.eta import EtaConfidence
from route74.sources.yandex.constants import YANDEX_LINE_ID
from route74.sources.yandex.models import (
    YandexLiveForecast,
    YandexSourceMethod,
    YandexSourceStatus,
    YandexVehicle,
)


MAX_REASONABLE_STOP_ETA_MINUTES = 180


def parse_stop_info_payload(
    payload: dict[str, Any],
    *,
    profile: CommuteProfile,
    current_time: datetime,
) -> YandexLiveForecast:
    transports = _transports(payload)
    if transports is None:
        return YandexLiveForecast.unavailable(
            status=YandexSourceStatus.PARSE_ERROR,
            source_method=YandexSourceMethod.STOP_INFO,
            reason="stop_transports_not_found",
        )

    transport = _route74_transport(transports)
    if transport is None:
        return YandexLiveForecast.unavailable(
            status=YandexSourceStatus.EMPTY,
            source_method=YandexSourceMethod.STOP_INFO,
            reason="stop_line_74_not_found",
        )

    thread = _select_thread(transport, profile.destination)
    if thread is None:
        return YandexLiveForecast.unavailable(
            status=YandexSourceStatus.PARSE_ERROR,
            source_method=YandexSourceMethod.STOP_INFO,
            reason="stop_direction_not_found",
        )

    schedule = thread.get("BriefSchedule")
    if not isinstance(schedule, dict):
        return YandexLiveForecast.unavailable(
            status=YandexSourceStatus.PARSE_ERROR,
            source_method=YandexSourceMethod.STOP_INFO,
            reason="stop_schedule_not_found",
        )

    estimated = _estimated_events(schedule, current_time)
    if estimated:
        arrivals = tuple(sorted({event.minutes for event in estimated}))
        vehicles = tuple(
            YandexVehicle(
                vehicle_id=event.vehicle_id or f"stop-info-{index}",
                arrival_minutes=event.minutes,
            )
            for index, event in enumerate(estimated)
        )
        return YandexLiveForecast(
            enabled=True,
            available=True,
            source_method=YandexSourceMethod.STOP_INFO,
            status=YandexSourceStatus.OK,
            arrival_minutes=arrivals,
            vehicles=vehicles,
            vehicle_count=len(vehicles),
            newest_age_seconds=0,
            confidence=EtaConfidence.MEDIUM,
            fallback_reason="stop_estimated",
            raw_status="stop_estimated",
        )

    scheduled = _scheduled_events(schedule, current_time)
    if scheduled:
        return YandexLiveForecast.unavailable(
            status=YandexSourceStatus.SCHEDULE_ONLY,
            source_method=YandexSourceMethod.STOP_INFO,
            reason=f"план Яндекса: {_times_reason(scheduled)}",
        )

    frequency = _frequency_reason(schedule)
    if frequency:
        return YandexLiveForecast.unavailable(
            status=YandexSourceStatus.FREQUENCY_ONLY,
            source_method=YandexSourceMethod.STOP_INFO,
            reason=frequency,
        )

    return YandexLiveForecast.unavailable(
        status=YandexSourceStatus.EMPTY,
        source_method=YandexSourceMethod.STOP_INFO,
        reason="stop_events_empty",
    )


class _StopEvent:
    def __init__(self, minutes: int, text: str, vehicle_id: str = "") -> None:
        self.minutes = minutes
        self.text = text
        self.vehicle_id = vehicle_id


def _transports(payload: dict[str, Any]) -> list[Any] | None:
    data = payload.get("data")
    if isinstance(data, dict) and isinstance(data.get("transports"), list):
        return data["transports"]
    if isinstance(payload.get("transports"), list):
        return payload["transports"]
    return None


def _route74_transport(transports: list[Any]) -> dict[str, Any] | None:
    for item in transports:
        if not isinstance(item, dict):
            continue
        if item.get("lineId") == YANDEX_LINE_ID:
            return item
        if str(item.get("name", "")).casefold() == "74" and str(item.get("type", "")) == "minibus":
            return item
    return None


def _select_thread(transport: dict[str, Any], destination: str) -> dict[str, Any] | None:
    threads = [item for item in transport.get("threads", []) if isinstance(item, dict)]
    if not threads:
        return None
    normalized_destination = _normalize_stop_name(destination)
    for thread in threads:
        terminal = _thread_terminal(thread)
        if normalized_destination and normalized_destination in _normalize_stop_name(terminal):
            return thread
    return threads[0] if len(threads) == 1 else None


def _thread_terminal(thread: dict[str, Any]) -> str:
    stops = [item for item in thread.get("EssentialStops", []) if isinstance(item, dict)]
    for stop in stops:
        info = stop.get("info")
        if isinstance(info, dict) and info.get("lastStop") is True:
            return str(stop.get("name", ""))
    return str(stops[-1].get("name", "")) if stops else ""


def _estimated_events(schedule: dict[str, Any], current_time: datetime) -> list[_StopEvent]:
    events = []
    for event in _events(schedule):
        estimated = event.get("Estimated")
        stop_event = _stop_event(estimated, event, current_time)
        if stop_event is not None:
            events.append(stop_event)
    return events


def _scheduled_events(schedule: dict[str, Any], current_time: datetime) -> list[_StopEvent]:
    events = []
    for event in _events(schedule):
        if isinstance(event.get("Estimated"), dict):
            continue
        scheduled = event.get("Scheduled")
        stop_event = _stop_event(scheduled, event, current_time)
        if stop_event is not None:
            events.append(stop_event)
    return events


def _events(schedule: dict[str, Any]) -> list[dict[str, Any]]:
    raw_events = schedule.get("Events")
    if not isinstance(raw_events, list):
        return []
    return [item for item in raw_events if isinstance(item, dict)]


def _stop_event(raw_time: Any, event: dict[str, Any], current_time: datetime) -> _StopEvent | None:
    if not isinstance(raw_time, dict):
        return None
    minutes = _minutes_until(raw_time, current_time)
    if minutes is None:
        return None
    text = str(raw_time.get("text") or "")
    return _StopEvent(
        minutes=minutes,
        text=text,
        vehicle_id=str(event.get("vehicleId") or ""),
    )


def _minutes_until(raw_time: dict[str, Any], current_time: datetime) -> int | None:
    value = _as_float(raw_time.get("value"))
    if value is not None:
        timestamp = value / 1000 if value > 10_000_000_000 else value
        try:
            event_time = datetime.fromtimestamp(timestamp, tz=timezone.utc).astimezone(current_time.tzinfo)
        except (OSError, OverflowError, ValueError):
            pass
        else:
            minutes = _reasonable_minutes(event_time, current_time)
            if minutes is not None:
                return minutes

    text = raw_time.get("text")
    if isinstance(text, str):
        try:
            hour, minute = [int(part) for part in text.split(":", maxsplit=1)]
            event_time = datetime.combine(current_time.date(), time(hour, minute), tzinfo=current_time.tzinfo)
        except (TypeError, ValueError):
            return None
        return _reasonable_minutes(event_time, current_time, allow_next_day=True)
    return None


def _reasonable_minutes(
    event_time: datetime,
    current_time: datetime,
    *,
    allow_next_day: bool = False,
) -> int | None:
    minutes = round((event_time - current_time).total_seconds() / 60)
    if minutes < 0 and allow_next_day:
        minutes = round((event_time + timedelta(days=1) - current_time).total_seconds() / 60)
    if 0 <= minutes <= MAX_REASONABLE_STOP_ETA_MINUTES:
        return minutes
    return None


def _frequency_reason(schedule: dict[str, Any]) -> str:
    frequencies = schedule.get("Frequencies")
    if not isinstance(frequencies, list) or not frequencies:
        return ""
    first = next((item for item in frequencies if isinstance(item, dict)), None)
    if first is None:
        return ""
    interval = _clean_text(first.get("text"))
    if not interval:
        seconds = _as_float(first.get("value"))
        interval = f"{round(seconds / 60)} мин" if seconds else ""
    window = _frequency_window(first)
    if interval and window:
        return f"интервал {interval}, {window}"
    if interval:
        return f"интервал {interval}"
    return "есть только интервальное расписание"


def _frequency_window(frequency: dict[str, Any]) -> str:
    begin = frequency.get("begin")
    end = frequency.get("end")
    if not isinstance(begin, dict) or not isinstance(end, dict):
        return ""
    begin_text = _clean_text(begin.get("text"))
    end_text = _clean_text(end.get("text"))
    if not begin_text or not end_text:
        return ""
    return f"{begin_text}-{end_text}"


def _times_reason(events: list[_StopEvent]) -> str:
    labels = [event.text for event in events[:3] if event.text]
    if labels:
        return ", ".join(labels)
    return ", ".join(f"через {event.minutes} мин" for event in events[:3])


def _normalize_stop_name(value: str) -> str:
    normalized = value.casefold().replace("ё", "е")
    normalized = normalized.replace("улица", "ул")
    return re.sub(r"[^a-zа-я0-9]+", "", normalized)


def _clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if isfinite(number) else None
