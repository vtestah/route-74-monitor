from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta
from pathlib import Path

from route74.domain.commute import DepartureDecision
from route74.domain.commute_change import DepartureChange
from route74.domain.runtime_sources import BOT_EVENT_USER_REPLY
from route74.storage.connection import DEFAULT_DB, connect_readonly
from route74.storage.errors import STORAGE_READ_ERRORS
from route74.storage.runtime_quality import (
    BotRuntimePrediction,
    load_recent_bot_runtime_predictions,
)

DEFAULT_CHANGE_LOOKBACK_HOURS = 3
DEFAULT_CHANGE_MAX_AGE_MINUTES = 90


class BotDecisionChangeService:
    def __init__(
        self,
        db_path: Path = DEFAULT_DB,
        *,
        lookback_hours: int = DEFAULT_CHANGE_LOOKBACK_HOURS,
        max_age_minutes: int = DEFAULT_CHANGE_MAX_AGE_MINUTES,
    ) -> None:
        if lookback_hours <= 0:
            raise ValueError("decision change lookback_hours must be positive")
        if max_age_minutes <= 0:
            raise ValueError("decision change max_age_minutes must be positive")
        self._db_path = db_path
        self._lookback_hours = lookback_hours
        self._max_age = timedelta(minutes=max_age_minutes)

    def build(self, decision: DepartureDecision) -> DepartureChange | None:
        previous = self._latest_previous(decision)
        if previous is None:
            return None
        previous_arrival_at = _prediction_arrival_at(previous)
        current_arrival_at = _current_arrival_at(decision)
        return DepartureChange(
            previous_sampled_at=previous.sampled_at,
            current_sampled_at=decision.current_time,
            previous_arrival_at=previous_arrival_at,
            current_arrival_at=current_arrival_at,
            arrival_shift_minutes=_arrival_shift_minutes(current_arrival_at, previous_arrival_at),
            previous_source=_prediction_source(previous),
            current_source=decision.source.value,
        )

    def _latest_previous(self, decision: DepartureDecision) -> BotRuntimePrediction | None:
        try:
            with connect_readonly(self._db_path) as connection:
                predictions = load_recent_bot_runtime_predictions(
                    connection,
                    current_time=decision.current_time,
                    hours=self._lookback_hours,
                    limit=8,
                    profile_key=decision.profile.key,
                    event_kind=BOT_EVENT_USER_REPLY,
                )
        except STORAGE_READ_ERRORS:
            return None
        for prediction in predictions:
            age = _safe_delta(decision.current_time, prediction.sampled_at)
            if age <= timedelta(0):
                continue
            if age <= self._max_age:
                return prediction
        return None


def build_runtime_prediction_change(
    current: BotRuntimePrediction,
    previous: BotRuntimePrediction,
) -> DepartureChange | None:
    if current.profile_key != previous.profile_key:
        return None
    if previous.sampled_at >= current.sampled_at:
        return None
    previous_arrival_at = _prediction_arrival_at(previous)
    current_arrival_at = _prediction_arrival_at(current)
    return DepartureChange(
        previous_sampled_at=previous.sampled_at,
        current_sampled_at=current.sampled_at,
        previous_arrival_at=previous_arrival_at,
        current_arrival_at=current_arrival_at,
        arrival_shift_minutes=_arrival_shift_minutes(current_arrival_at, previous_arrival_at),
        previous_source=_prediction_source(previous),
        current_source=_prediction_source(current),
    )


def build_runtime_prediction_change_map(
    current_predictions: Iterable[BotRuntimePrediction],
    *,
    history_predictions: Iterable[BotRuntimePrediction] = (),
    event_kind: str = BOT_EVENT_USER_REPLY,
    max_age_minutes: int = DEFAULT_CHANGE_MAX_AGE_MINUTES,
) -> dict[int, DepartureChange]:
    if max_age_minutes <= 0:
        raise ValueError("runtime prediction change max_age_minutes must be positive")
    max_age = timedelta(minutes=max_age_minutes)
    current_by_id = {
        prediction.id: prediction for prediction in current_predictions if prediction.event_kind == event_kind
    }
    if not current_by_id:
        return {}
    candidates_by_profile: dict[str, dict[int, BotRuntimePrediction]] = {}
    for prediction in (*tuple(current_by_id.values()), *tuple(history_predictions)):
        if prediction.event_kind != event_kind:
            continue
        candidates_by_profile.setdefault(prediction.profile_key, {})[prediction.id] = prediction
    changes: dict[int, DepartureChange] = {}
    for current in current_by_id.values():
        previous = _nearest_previous_prediction(
            current,
            candidates_by_profile.get(current.profile_key, {}).values(),
            max_age=max_age,
        )
        if previous is None:
            continue
        change = build_runtime_prediction_change(current, previous)
        if change is not None:
            changes[current.id] = change
    return changes


def _nearest_previous_prediction(
    current: BotRuntimePrediction,
    candidates: Iterable[BotRuntimePrediction],
    *,
    max_age: timedelta,
) -> BotRuntimePrediction | None:
    previous_candidates = tuple(
        prediction
        for prediction in candidates
        if prediction.id != current.id
        and prediction.sampled_at < current.sampled_at
        and _safe_delta(current.sampled_at, prediction.sampled_at) <= max_age
    )
    if not previous_candidates:
        return None
    return max(
        previous_candidates,
        key=lambda prediction: (prediction.sampled_at, prediction.id),
    )


def _prediction_arrival_at(prediction: BotRuntimePrediction) -> datetime | None:
    if prediction.predicted_arrival_at is not None:
        return prediction.predicted_arrival_at
    return prediction.sampled_at + timedelta(minutes=prediction.predicted_minutes)


def _current_arrival_at(decision: DepartureDecision) -> datetime | None:
    if decision.arrival_at is not None:
        return decision.arrival_at
    if decision.arrival_in_minutes is None:
        return None
    return decision.current_time + timedelta(minutes=decision.arrival_in_minutes)


def _arrival_shift_minutes(current_arrival_at: datetime | None, previous_arrival_at: datetime | None) -> int | None:
    if current_arrival_at is None or previous_arrival_at is None:
        return None
    return round(_safe_delta(current_arrival_at, previous_arrival_at).total_seconds() / 60)


def _prediction_source(prediction: BotRuntimePrediction) -> str:
    return prediction.selected_departure_source or prediction.source


def _safe_delta(later: datetime, earlier: datetime) -> timedelta:
    try:
        return later - earlier
    except TypeError:
        return later.replace(tzinfo=None) - earlier.replace(tzinfo=None)
