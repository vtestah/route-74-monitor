from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from route74.domain.commute import DepartureDecision, DepartureSource
from route74.domain.eta import EtaSource
from route74.domain.prediction_sources import (
    SOURCE_CORRECTED_LIVE,
    SOURCE_HISTORY_HEADWAY,
    SOURCE_TARGET_STOP_LIVE,
    SOURCE_VEHICLE_PROGRESS,
)
from route74.domain.reporting import matching_report_window
from route74.domain.runtime_sources import (
    BOT_EVENT_KINDS,
    BOT_EVENT_USER_REPLY,
)
from route74.sources.yandex.models import YandexSourceMethod, YandexVehicle
from route74.storage.connection import connect, init_db
from route74.storage.runtime_quality import BOT_RUNTIME_SOURCE


BOT_DECISION_DEDUPE_SECONDS = 20
LATEST_SNAPSHOT_MAX_AGE_SECONDS = 180
MAX_PREDICTED_ARRIVAL_SKEW_SECONDS = 60
SOURCE_BY_DEPARTURE_SOURCE = {
    DepartureSource.YANDEX: SOURCE_TARGET_STOP_LIVE,
    DepartureSource.YANDEX_CORRECTED: SOURCE_CORRECTED_LIVE,
    DepartureSource.VEHICLE_PROGRESS: SOURCE_VEHICLE_PROGRESS,
    DepartureSource.YANDEX_HISTORY: SOURCE_HISTORY_HEADWAY,
}


@dataclass(frozen=True)
class BotDecisionPredictionResult:
    prediction_event_id: int | None
    created: bool
    reason: str


class BotDecisionRecorder:
    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path

    def record(self, decision: DepartureDecision) -> BotDecisionPredictionResult:
        return self.record_user_reply(decision)

    def record_user_reply(self, decision: DepartureDecision) -> BotDecisionPredictionResult:
        return self._record(decision, event_kind=BOT_EVENT_USER_REPLY)

    def record_watch_alert(self, decision: DepartureDecision, event_kind: str) -> BotDecisionPredictionResult:
        return self._record(decision, event_kind=event_kind)

    def _record(self, decision: DepartureDecision, *, event_kind: str) -> BotDecisionPredictionResult:
        with connect(self._db_path) as connection:
            init_db(connection)
            return insert_bot_decision_prediction_event(connection, decision, event_kind=event_kind)


def insert_bot_decision_prediction_event(
    connection: sqlite3.Connection,
    decision: DepartureDecision,
    *,
    event_kind: str = BOT_EVENT_USER_REPLY,
) -> BotDecisionPredictionResult:
    event_kind = _event_kind_value(event_kind)
    predicted_minutes = decision.arrival_in_minutes
    source = SOURCE_BY_DEPARTURE_SOURCE.get(decision.source)
    if predicted_minutes is None:
        return BotDecisionPredictionResult(None, False, "no_eta")
    if predicted_minutes < 0:
        return BotDecisionPredictionResult(None, False, "invalid_eta")
    if source is None:
        return BotDecisionPredictionResult(None, False, "unsupported_source")

    sampled_at = decision.current_time
    predicted_arrival_at = _predicted_arrival_at(sampled_at, predicted_minutes, decision.arrival_at)
    if predicted_arrival_at is None:
        return BotDecisionPredictionResult(None, False, "inconsistent_eta")
    if event_kind == BOT_EVENT_USER_REPLY and _has_recent_duplicate(
        connection,
        profile_key=decision.profile.key,
        sampled_at=sampled_at,
        source=source,
        predicted_minutes=predicted_minutes,
    ):
        return BotDecisionPredictionResult(None, False, "duplicate")

    report_window = matching_report_window(sampled_at, decision.profile.key)
    snapshot_id = _latest_snapshot_id(connection, decision.profile.key, sampled_at)
    vehicle = _matching_vehicle(decision, predicted_minutes)
    cursor = connection.execute(
        """
        INSERT INTO prediction_events(
            yandex_snapshot_id, profile_key, sampled_at, report_window_key,
            source, source_method, predicted_minutes, predicted_arrival_at,
            confidence, vehicle_id, thread_id, traffic_provider, traffic_status,
            traffic_delay_seconds, runtime_source, raw_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            snapshot_id,
            decision.profile.key,
            sampled_at.isoformat(),
            report_window.key if report_window is not None else "",
            source,
            _source_method(decision),
            predicted_minutes,
            predicted_arrival_at.isoformat(),
            decision.eta_consensus.confidence.value,
            vehicle.vehicle_id if vehicle is not None else "",
            vehicle.thread_id if vehicle is not None else "",
            "none",
            "not_collected",
            None,
            BOT_RUNTIME_SOURCE,
            json.dumps(_raw_payload(decision, vehicle, snapshot_id, event_kind), ensure_ascii=False),
        ),
    )
    return BotDecisionPredictionResult(int(cursor.lastrowid), True, "created")


def _has_recent_duplicate(
    connection: sqlite3.Connection,
    *,
    profile_key: str,
    sampled_at: datetime,
    source: str,
    predicted_minutes: int,
) -> bool:
    since = sampled_at - timedelta(seconds=BOT_DECISION_DEDUPE_SECONDS)
    row = connection.execute(
        """
        SELECT id
        FROM prediction_events
        WHERE profile_key = ?
          AND sampled_at >= ?
          AND sampled_at <= ?
          AND source = ?
          AND predicted_minutes = ?
          AND runtime_source = ?
        ORDER BY sampled_at DESC
        LIMIT 1
        """,
        (
            profile_key,
            since.isoformat(),
            sampled_at.isoformat(),
            source,
            predicted_minutes,
            BOT_RUNTIME_SOURCE,
        ),
    ).fetchone()
    return row is not None


def _predicted_arrival_at(
    sampled_at: datetime,
    predicted_minutes: int,
    arrival_at: datetime | None,
) -> datetime | None:
    expected = sampled_at + timedelta(minutes=predicted_minutes)
    if arrival_at is None:
        return expected
    try:
        skew_seconds = abs((arrival_at - expected).total_seconds())
    except TypeError:
        return None
    if skew_seconds > MAX_PREDICTED_ARRIVAL_SKEW_SECONDS:
        return None
    return arrival_at


def _latest_snapshot_id(connection: sqlite3.Connection, profile_key: str, sampled_at: datetime) -> int | None:
    row = connection.execute(
        """
        SELECT id, sampled_at
        FROM yandex_snapshots
        WHERE profile_key = ?
          AND sampled_at <= ?
        ORDER BY sampled_at DESC
        LIMIT 1
        """,
        (profile_key, sampled_at.isoformat()),
    ).fetchone()
    if row is None:
        return None
    try:
        snapshot_sampled_at = datetime.fromisoformat(str(row["sampled_at"]))
    except ValueError:
        return None
    try:
        snapshot_age = sampled_at - snapshot_sampled_at
    except TypeError:
        return None
    if snapshot_age < timedelta(0):
        return None
    if snapshot_age > timedelta(seconds=LATEST_SNAPSHOT_MAX_AGE_SECONDS):
        return None
    return int(row["id"])


def _matching_vehicle(decision: DepartureDecision, predicted_minutes: int) -> YandexVehicle | None:
    if decision.source == DepartureSource.YANDEX_HISTORY:
        return None
    if decision.source == DepartureSource.YANDEX_CORRECTED:
        raw_live_minutes = _eta_estimate_minutes(decision, EtaSource.YANDEX)
        if raw_live_minutes is not None:
            vehicle = _vehicle_for_arrival_minutes(decision, raw_live_minutes)
            if vehicle is not None:
                return vehicle
    return _vehicle_for_arrival_minutes(decision, predicted_minutes)


def _eta_estimate_minutes(decision: DepartureDecision, source: EtaSource) -> int | None:
    for estimate in decision.eta_consensus.estimates:
        if estimate.source == source:
            return estimate.arrival_minutes
    return None


def _vehicle_for_arrival_minutes(decision: DepartureDecision, arrival_minutes: int) -> YandexVehicle | None:
    candidates = tuple(
        vehicle
        for vehicle in decision.yandex_forecast.vehicles
        if vehicle.arrival_minutes == arrival_minutes
    )
    if not candidates:
        return None
    return min(candidates, key=lambda vehicle: (vehicle.age_seconds is None, vehicle.age_seconds or 0))


def _source_method(decision: DepartureDecision) -> str:
    if decision.source == DepartureSource.YANDEX_HISTORY:
        return "history"
    method = decision.yandex_forecast.source_method
    if method == YandexSourceMethod.NONE:
        return "runtime"
    return method.value


def _raw_payload(
    decision: DepartureDecision,
    vehicle: YandexVehicle | None,
    snapshot_id: int | None,
    event_kind: str,
) -> dict[str, object]:
    selected_eta_source = decision.eta_consensus.selected_source
    return {
        "runtime_source": BOT_RUNTIME_SOURCE,
        "event_kind": event_kind,
        "selected_departure_source": decision.source.value,
        "selected_eta_source": selected_eta_source.value if selected_eta_source is not None else "",
        "walk_minutes": decision.walk_minutes,
        "leave_in_minutes": decision.leave_in_minutes,
        "urgency": decision.urgency.value,
        "target_wait_minutes": decision.eta_consensus.target_wait_minutes,
        "spread_minutes": decision.eta_consensus.spread_minutes,
        "warning": decision.eta_consensus.warning,
        "eta_factors": _eta_factor_payloads(decision),
        "next_live_minutes": list(decision.next_live_minutes),
        "history_available": decision.yandex_history.available,
        "history_arrival_minutes": decision.yandex_history.arrival_minutes,
        "history_scope": decision.yandex_history.scope.value,
        "history_report_window_key": decision.yandex_history.report_window_key,
        "history_sample_count": decision.yandex_history.sample_count,
        "history_bucket_minutes": decision.yandex_history.bucket_minutes,
        "history_window_days": decision.yandex_history.window_days,
        "history_percentile": decision.yandex_history.percentile,
        "history_fallback_reason": decision.yandex_history.fallback_reason,
        "yandex_status": decision.yandex_forecast.status.value,
        "yandex_source_method": decision.yandex_forecast.source_method.value,
        "yandex_fallback_reason": decision.yandex_forecast.fallback_reason,
        "yandex_newest_age_seconds": decision.yandex_forecast.newest_age_seconds,
        "matched_vehicle_id": vehicle.vehicle_id if vehicle is not None else "",
        "matched_thread_id": vehicle.thread_id if vehicle is not None else "",
        "matched_snapshot_id": snapshot_id,
    }


def _event_kind_value(value: object) -> str:
    if not isinstance(value, str) or value not in BOT_EVENT_KINDS:
        raise ValueError("bot prediction event_kind is unknown")
    return value


def _eta_factor_payloads(decision: DepartureDecision) -> list[dict[str, object]]:
    return [
        {
            "kind": factor.kind.value,
            "minutes": factor.minutes,
            "sample_count": factor.sample_count,
            "percent": factor.percent,
            "scope": factor.scope,
        }
        for factor in decision.eta_consensus.factors
    ]
