from __future__ import annotations

from datetime import datetime, time, timedelta

from route74.domain.eta import EtaConsensus, EtaSource
from route74.domain.walk_buffer import is_valid_walk_minutes
from route74.domain.yandex_history import YandexHistoryPrediction
from route74.models import require_local_datetime
from route74.sources.yandex.models import YandexLiveForecast


def validate_profile_window_time(label: str, value: object) -> None:
    if not isinstance(value, time):
        raise ValueError("commute profile window needs start and end times")
    if value.tzinfo is not None:
        raise ValueError(f"commute profile window {label} must be timezone-naive")
    if value.second or value.microsecond:
        raise ValueError(f"commute profile window {label} must use minute precision")


MAX_PROFILE_TEXT_LENGTH = 160
MAX_PROFILE_WALK_NOTE_LENGTH = 120


def validate_profile_text(name: str, value: object, *, plain_key: bool = False) -> None:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"commute profile {name} is required")
    _validate_compact_profile_text(name, value, max_length=MAX_PROFILE_TEXT_LENGTH)
    if plain_key and not _is_plain_key(value):
        raise ValueError(f"commute profile {name} must be a plain key")


def validate_profile_walk_note(value: object) -> None:
    if not isinstance(value, str):
        raise ValueError("commute profile walk note needs text")
    if not value:
        return
    _validate_compact_profile_text(
        "walk note",
        value,
        max_length=MAX_PROFILE_WALK_NOTE_LENGTH,
    )


def _validate_compact_profile_text(name: str, value: str, *, max_length: int) -> None:
    if len(value) > max_length or value != " ".join(value.split()):
        raise ValueError(f"commute profile {name} must be compact single-line text")


def _is_plain_key(value: str) -> bool:
    return value.isascii() and all(char.isalnum() or char == "_" for char in value)


def validate_snapshot(snapshot: object) -> None:
    from route74.domain.commute import CommuteProfile

    if not isinstance(snapshot.profile, CommuteProfile):
        raise ValueError("commute snapshot profile needs CommuteProfile")
    _validate_datetime("commute snapshot current_time", snapshot.current_time)
    _validate_walk_minutes("commute snapshot walk_minutes", snapshot.walk_minutes)
    _validate_eta_consensus("commute snapshot eta_consensus", snapshot.eta_consensus)
    _validate_yandex_forecast("commute snapshot yandex_forecast", snapshot.yandex_forecast)
    _validate_yandex_history("commute snapshot yandex_history", snapshot.yandex_history)


def validate_decision(decision: object) -> None:
    from route74.domain.commute import CommuteProfile, DepartureSource, DepartureUrgency

    if not isinstance(decision.profile, CommuteProfile):
        raise ValueError("departure decision profile needs CommuteProfile")
    _validate_datetime("departure decision current_time", decision.current_time)
    _validate_walk_minutes("departure decision walk_minutes", decision.walk_minutes)
    if not isinstance(decision.source, DepartureSource):
        raise ValueError("departure decision source needs DepartureSource")
    if not isinstance(decision.urgency, DepartureUrgency):
        raise ValueError("departure decision urgency needs DepartureUrgency")
    _validate_optional_non_negative_minutes(
        "departure decision arrival_in_minutes",
        decision.arrival_in_minutes,
    )
    _validate_optional_minutes("departure decision leave_in_minutes", decision.leave_in_minutes)
    _validate_optional_datetime("departure decision arrival_at", decision.arrival_at)
    _validate_optional_datetime("departure decision leave_at", decision.leave_at)
    _validate_next_live_minutes(decision.next_live_minutes)
    _validate_eta_consensus("departure decision eta_consensus", decision.eta_consensus)
    _validate_yandex_forecast("departure decision yandex_forecast", decision.yandex_forecast)
    _validate_yandex_history("departure decision yandex_history", decision.yandex_history)
    _validate_decision_shape(decision)


def _validate_decision_shape(decision: object) -> None:
    from route74.domain.commute import DepartureSource, DepartureUrgency

    has_arrival = decision.arrival_in_minutes is not None or decision.arrival_at is not None
    has_leave = decision.leave_in_minutes is not None or decision.leave_at is not None
    if decision.source == DepartureSource.NONE:
        if decision.urgency != DepartureUrgency.NO_DATA:
            raise ValueError("no-data departure decision needs NO_DATA urgency")
        if has_arrival or has_leave:
            raise ValueError("no-data departure decision must not have arrival or leave time")
        if decision.next_live_minutes:
            raise ValueError("no-data departure decision must not have next live minutes")
        if decision.eta_consensus.selected_source is not None or decision.eta_consensus.arrival_minutes is not None:
            raise ValueError("no-data departure decision must not have ETA consensus")
        return
    if decision.urgency == DepartureUrgency.NO_DATA:
        raise ValueError("ETA departure decision must not use NO_DATA urgency")
    if (
        decision.arrival_in_minutes is None
        or decision.arrival_at is None
        or decision.leave_in_minutes is None
        or decision.leave_at is None
    ):
        raise ValueError("ETA departure decision must have arrival and leave time")
    _validate_decision_consensus(decision)
    _validate_decision_timeline(decision)


def _validate_decision_consensus(decision: object) -> None:
    expected_source = _eta_source_for_departure_source(decision.source)
    if decision.eta_consensus.selected_source != expected_source:
        raise ValueError("ETA departure decision source must match ETA consensus")
    if decision.eta_consensus.arrival_minutes != decision.arrival_in_minutes:
        raise ValueError("ETA departure decision arrival must match ETA consensus")
    if any(minutes <= decision.arrival_in_minutes for minutes in decision.next_live_minutes):
        raise ValueError("departure decision next_live_minutes must be after selected arrival")


def _eta_source_for_departure_source(source: object) -> EtaSource:
    from route74.domain.commute import DepartureSource

    return {
        DepartureSource.YANDEX: EtaSource.YANDEX,
        DepartureSource.YANDEX_CORRECTED: EtaSource.YANDEX_CORRECTED,
        DepartureSource.VEHICLE_PROGRESS: EtaSource.VEHICLE_PROGRESS,
        DepartureSource.YANDEX_HISTORY: EtaSource.YANDEX_HISTORY,
    }[source]


def _validate_decision_timeline(decision: object) -> None:
    _validate_offset_datetime_pair(
        "departure decision arrival",
        decision.current_time,
        decision.arrival_in_minutes,
        decision.arrival_at,
    )
    _validate_offset_datetime_pair(
        "departure decision leave",
        decision.current_time,
        decision.leave_in_minutes,
        decision.leave_at,
    )
    if decision.leave_at > decision.arrival_at:
        raise ValueError("departure decision leave time must not be after arrival time")


def _validate_offset_datetime_pair(
    label: str,
    current_time: datetime,
    offset_minutes: int,
    absolute_time: datetime,
) -> None:
    if absolute_time != current_time + timedelta(minutes=offset_minutes):
        raise ValueError(f"{label} time must match minutes offset")


def _validate_optional_non_negative_minutes(name: str, value: object) -> None:
    if value is not None and (_invalid_int(value) or value < 0):
        raise ValueError(f"{name} needs non-negative integer minutes")


def _validate_optional_minutes(name: str, value: object) -> None:
    if value is not None and _invalid_int(value):
        raise ValueError(f"{name} needs integer minutes")


def _validate_walk_minutes(name: str, value: object) -> None:
    if not is_valid_walk_minutes(value):
        raise ValueError(f"{name} is out of range")


def _validate_datetime(name: str, value: object) -> None:
    require_local_datetime(value, name=name)


def _validate_optional_datetime(name: str, value: object) -> None:
    if value is not None:
        _validate_datetime(name, value)


def _validate_next_live_minutes(value: object) -> None:
    if not isinstance(value, tuple) or any(_invalid_int(minutes) or minutes < 0 for minutes in value):
        raise ValueError("departure decision next_live_minutes needs tuple of non-negative integers")
    if any(previous >= current for previous, current in zip(value, value[1:])):
        raise ValueError("departure decision next_live_minutes must be strictly increasing")


def _validate_eta_consensus(name: str, value: object) -> None:
    if not isinstance(value, EtaConsensus):
        raise ValueError(f"{name} needs EtaConsensus")


def _validate_yandex_forecast(name: str, value: object) -> None:
    if not isinstance(value, YandexLiveForecast):
        raise ValueError(f"{name} needs YandexLiveForecast")


def _validate_yandex_history(name: str, value: object) -> None:
    if not isinstance(value, YandexHistoryPrediction):
        raise ValueError(f"{name} needs YandexHistoryPrediction")


def _invalid_int(value: object) -> bool:
    return isinstance(value, bool) or not isinstance(value, int)
