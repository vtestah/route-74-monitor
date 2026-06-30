from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Protocol

from route74.domain.commute import (
    CommuteProfile,
    CommuteSnapshot,
    DepartureDecision,
)
from route74.domain.walk_buffer import is_valid_walk_minutes
from route74.domain.yandex_history import YandexHistoryPrediction
from route74.models import now_local, require_local_datetime
from route74.services.departure import build_departure_decision
from route74.services.prediction_engine import PredictionEngine
from route74.services.yandex_forecast import (
    YandexSource,
    build_yandex_forecast,
)
from route74.storage import STORAGE_READ_ERRORS

HISTORY_PREDICTOR_ERRORS = STORAGE_READ_ERRORS
MAX_HISTORY_EXCEPTION_TYPE_LENGTH = 80


class HistoryPredictor(Protocol):
    def predict_at(self, profile: CommuteProfile, current_time: datetime) -> YandexHistoryPrediction: ...


Clock = Callable[[], datetime]


class CommuteService:
    def __init__(
        self,
        yandex_source: YandexSource | None = None,
        history_predictor: HistoryPredictor | None = None,
        prediction_engine: PredictionEngine | None = None,
        clock: Clock = now_local,
    ) -> None:
        self._yandex_source = yandex_source
        self._history_predictor = history_predictor
        self._prediction_engine = prediction_engine or PredictionEngine()
        self._clock = clock

    @property
    def clock(self) -> Clock:
        return self._clock

    def build_snapshot(self, profile: CommuteProfile, walk_minutes: int) -> CommuteSnapshot:
        _validate_request(profile, walk_minutes)
        current_time = require_local_datetime(self._clock(), name="commute service clock")
        yandex_forecast = build_yandex_forecast(self._yandex_source, profile, current_time)
        yandex_history = _build_yandex_history(self._history_predictor, profile, current_time)
        eta_consensus = self._prediction_engine.predict(
            profile=profile,
            current_time=current_time,
            yandex_forecast=yandex_forecast,
            yandex_history=yandex_history,
        ).consensus

        return CommuteSnapshot(
            profile=profile,
            current_time=current_time,
            walk_minutes=walk_minutes,
            eta_consensus=eta_consensus,
            yandex_forecast=yandex_forecast,
            yandex_history=yandex_history,
        )

    def build_decision(self, profile: CommuteProfile, walk_minutes: int) -> DepartureDecision:
        return build_departure_decision(self.build_snapshot(profile, walk_minutes))


def _validate_request(profile: object, walk_minutes: object) -> None:
    if not isinstance(profile, CommuteProfile):
        raise ValueError("commute request profile needs CommuteProfile")
    if not is_valid_walk_minutes(walk_minutes):
        raise ValueError("commute request walk minutes is out of range")


def _build_yandex_history(
    predictor: HistoryPredictor | None,
    profile: CommuteProfile,
    current_time: datetime,
) -> YandexHistoryPrediction:
    if predictor is None:
        return YandexHistoryPrediction.unavailable(reason="history_disabled")
    try:
        history = predictor.predict_at(profile, current_time)
    except HISTORY_PREDICTOR_ERRORS as error:
        return YandexHistoryPrediction.unavailable(reason=_history_error_reason(error))
    if not isinstance(history, YandexHistoryPrediction):
        raise ValueError("history predictor must return YandexHistoryPrediction")
    return history


def _history_error_reason(error: Exception) -> str:
    type_name = "".join(
        character
        for character in type(error).__name__
        if character.isascii() and (character.isalnum() or character == "_")
    )
    type_name = type_name[:MAX_HISTORY_EXCEPTION_TYPE_LENGTH] or "unknown"
    return f"history_error:{type_name}"
