from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time
from enum import StrEnum

from route74.domain import commute_validation as validation
from route74.domain.eta import EtaConsensus
from route74.domain.walk_buffer import is_valid_walk_minutes
from route74.domain.yandex_history import YandexHistoryPrediction
from route74.sources.yandex.models import YandexLiveForecast


class DepartureSource(StrEnum):
    YANDEX = "yandex"
    YANDEX_CORRECTED = "yandex_corrected"
    VEHICLE_PROGRESS = "vehicle_progress"
    YANDEX_HISTORY = "yandex_history"
    NONE = "none"


class DepartureUrgency(StrEnum):
    GO_NOW = "go_now"
    GET_READY = "get_ready"
    RELAX = "relax"
    NO_DATA = "no_data"


@dataclass(frozen=True)
class CommuteProfile:
    key: str
    title: str
    live_stop_id: str
    destination: str
    window_start: time
    window_end: time
    default_walk_minutes: int
    walk_note: str = ""

    def __post_init__(self) -> None:
        for name in ("key", "live_stop_id"):
            validation.validate_profile_text(name, getattr(self, name), plain_key=True)
        for name in ("title", "destination"):
            validation.validate_profile_text(name, getattr(self, name))
        validation.validate_profile_window_time("start", self.window_start)
        validation.validate_profile_window_time("end", self.window_end)
        if self.window_start > self.window_end:
            raise ValueError("commute profile window end must not be before start")
        if not is_valid_walk_minutes(self.default_walk_minutes):
            raise ValueError("commute profile default walk minutes is out of range")
        validation.validate_profile_walk_note(self.walk_note)


@dataclass(frozen=True)
class CommuteSnapshot:
    profile: CommuteProfile
    current_time: datetime
    walk_minutes: int
    eta_consensus: EtaConsensus = field(default_factory=EtaConsensus.disabled)
    yandex_forecast: YandexLiveForecast = field(default_factory=YandexLiveForecast.disabled)
    yandex_history: YandexHistoryPrediction = field(default_factory=YandexHistoryPrediction.unavailable)

    def __post_init__(self) -> None:
        validation.validate_snapshot(self)


@dataclass(frozen=True)
class DepartureDecision:
    profile: CommuteProfile
    current_time: datetime
    walk_minutes: int
    source: DepartureSource
    urgency: DepartureUrgency
    arrival_in_minutes: int | None
    arrival_at: datetime | None
    leave_in_minutes: int | None
    leave_at: datetime | None
    next_live_minutes: tuple[int, ...]
    eta_consensus: EtaConsensus = field(default_factory=EtaConsensus.disabled)
    yandex_forecast: YandexLiveForecast = field(default_factory=YandexLiveForecast.disabled)
    yandex_history: YandexHistoryPrediction = field(default_factory=YandexHistoryPrediction.unavailable)

    def __post_init__(self) -> None:
        validation.validate_decision(self)
