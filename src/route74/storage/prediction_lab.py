from __future__ import annotations

from collections.abc import Iterable
import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from math import ceil

from route74.domain.eta import EtaConfidence
from route74.domain.eta_policy import source_risk_buffer_floor_minutes
from route74.domain.prediction_buckets import (
    PREDICTION_ETA_BUCKETS,
    prediction_bucket_label,
)
from route74.domain.prediction_consensus import (
    early_conflict_minutes_for_event_source,
    prediction_selection_candidate_for_event_source,
)
from route74.domain.prediction_selection import (
    SELECTION_POLICY_NAME,
    PredictionSelectionCandidate,
    select_prediction_key,
)
from route74.domain.prediction_sources import (
    EVALUATED_EVENT_SOURCES,
    EVENT_SOURCE_PRIORITY,
    SOURCE_CORRECTED_LIVE,
    SOURCE_ENSEMBLE,
    SOURCE_HISTORY_HEADWAY,
    SOURCE_TARGET_STOP_LIVE,
    SOURCE_VEHICLE_PROGRESS,
)
from route74.domain.reporting import ReportWindow, matching_report_window
from route74.domain.runtime_sources import RUNTIME_SOURCE_WEB_APP
from route74.models import now_local
from route74.sources.yandex.constants import (
    ROUTE_TRAFFIC_POINTS_BY_PROFILE,
    STOP_ID_BY_PROFILE,
)
from route74.sources.yandex.freshness import (
    DEFAULT_FRESH_VEHICLE_MAX_AGE_SECONDS,
    vehicle_is_fresh,
)
from route74.sources.yandex.line import YandexLineTopology
from route74.sources.yandex.live_evidence import (
    LiveEtaEvidenceAdjustment,
    live_eta_evidence_adjustment,
)
from route74.sources.yandex.models import (
    YandexLiveForecast,
    YandexSourceMethod,
    YandexSourceStatus,
    YandexVehicle,
)
from route74.sources.yandex.trust import forecast_has_trusted_fresh_eta
from route74.storage.helpers import (
    arrival_minutes_from_json,
    count_table_rows,
    day_kind_weekdays,
)
from route74.storage.history import load_yandex_eta_history_for_profile_window
from route74.storage.models import RouteTrafficSnapshot
from route74.storage.route_geometry import (
    RouteGeometry,
    RouteProjection,
    VehiclePosition,
    haversine_meters,
    load_route_geometry,
    position_observed_at,
    previous_vehicle_positions,
    route_projection_for_point,
)

TARGET_STOP_RADIUS_METERS = 120
COORDINATE_ARRIVAL_MAX_ROUTE_SNAP_METERS = 120
COORDINATE_ARRIVAL_MAX_TARGET_ROUTE_DELTA_METERS = 160
COORDINATE_ARRIVAL_MAX_BACKTRACK_METERS = 30
ARRIVAL_DEDUPE_MINUTES = 2
PREDICTION_MATCH_MINUTES = 90
RESIDUAL_MIN_SAMPLES = 5
RESIDUAL_MAX_EARLY_CORRECTION_MINUTES = 6
PREDICTION_ERROR_MAX_AGE_DAYS = 14
SOURCE_RELIABILITY_MIN_SAMPLES = 10
RUNTIME_RELIABILITY_MIN_SAMPLES = 3
SOURCE_RELIABILITY_MAX_BUFFER_MINUTES = 4
SOURCE_RELIABILITY_MIN_MISS_RATE_PERCENT = 10
HISTORY_HEADWAY_DAYS = 14
HISTORY_HEADWAY_MIN_OBSERVATIONS = 20
HISTORY_HEADWAY_MIN_DAYS = 3
HISTORY_HEADWAY_BUCKETS = (30, 60)
HISTORY_HEADWAY_PERCENTILE = 80
HISTORY_HEADWAY_MAX_AGE_SECONDS = 180
FRESH_VEHICLE_MAX_AGE_SECONDS = DEFAULT_FRESH_VEHICLE_MAX_AGE_SECONDS
VEHICLE_PROGRESS_MAX_MINUTES = 60
VEHICLE_PROGRESS_LOOKBACK_MINUTES = 10
VEHICLE_PROGRESS_SPEED_SAMPLE_LIMIT = 5
VEHICLE_PROGRESS_MIN_SPEED_MPS = 1.0
VEHICLE_PROGRESS_MAX_SPEED_MPS = 25.0
VEHICLE_PROGRESS_MAX_ROUTE_SNAP_METERS = 180
VEHICLE_PROGRESS_MEDIUM_MIN_SPEED_SAMPLES = 2
VEHICLE_PROGRESS_MEDIUM_MAX_AGE_SECONDS = 60
VEHICLE_PROGRESS_MEDIUM_MAX_ROUTE_SNAP_METERS = 90
VEHICLE_PROGRESS_TRACK_MAX_AGE_SECONDS = 600
VEHICLE_PROGRESS_TRACK_ALPHA = 0.45
VEHICLE_PROGRESS_TRACK_BETA = 0.20
VEHICLE_PROGRESS_TRACK_MIN_SAMPLES = 2
VEHICLE_PROGRESS_TRACK_MIN_SPEED_MPS = 0.5
VEHICLE_PROGRESS_TELEPORT_SPEED_MPS = 30.0
VEHICLE_PROGRESS_TRACK_MAX_BACKTRACK_METERS = 80
VEHICLE_PROGRESS_STALLED_DELTA_METERS = 20
VEHICLE_PROGRESS_STALLED_AFTER_SECONDS = 120
VEHICLE_PROGRESS_STALLED_BUFFER_MINUTES = 2
EVALUATED_SOURCES = EVALUATED_EVENT_SOURCES
ENSEMBLE_SOURCE_PRIORITY = EVENT_SOURCE_PRIORITY


@dataclass(frozen=True)
class ArrivalEvent:
    id: int
    profile_key: str
    vehicle_id: str
    thread_id: str
    stop_id: str
    arrived_at: datetime
    source: str
    confidence: str


@dataclass(frozen=True)
class PredictionEvent:
    id: int
    profile_key: str
    sampled_at: datetime
    report_window_key: str
    source: str
    predicted_minutes: int
    vehicle_id: str
    thread_id: str


@dataclass(frozen=True)
class ResidualCorrection:
    bucket: str
    sample_count: int
    p10_error_minutes: int
    correction_minutes: int
    scope: str = "bucket"

    @property
    def applied(self) -> bool:
        return self.correction_minutes < 0


@dataclass(frozen=True)
class SourceReliability:
    source: str
    bucket: str
    sample_count: int
    miss_cases: int
    miss_rate_percent: int
    p10_error_minutes: int
    safety_buffer_minutes: int
    scope: str = "bucket"

    @property
    def applied(self) -> bool:
        return self.safety_buffer_minutes > 0


@dataclass(frozen=True)
class PredictionErrorSample:
    error_minutes: int
    arrival_confidence: EtaConfidence


@dataclass(frozen=True)
class PredictionLabSourceSummary:
    source: str
    evaluated_predictions: int
    miss_cases: int
    miss_minutes: int
    extra_wait_minutes: int
    mean_absolute_error: float

    @property
    def miss_rate_percent(self) -> int:
        return round(self.miss_cases * 100 / self.evaluated_predictions) if self.evaluated_predictions else 0


@dataclass(frozen=True)
class PredictionLabSummary:
    window_key: str
    profile_key: str
    arrival_events: int
    prediction_events: int
    evaluated_predictions: int
    latest_arrival_at: datetime | None
    latest_prediction_at: datetime | None
    sources: tuple[PredictionLabSourceSummary, ...]


@dataclass(frozen=True)
class PredictionLabCalibrationBucket:
    source: str
    bucket: str
    evaluated_predictions: int
    miss_cases: int
    miss_rate_percent: int
    p10_error_minutes: int
    reliability: SourceReliability
    runtime_reliability: SourceReliability
    residual_correction: ResidualCorrection | None

    @property
    def effective_reliability(self) -> SourceReliability:
        return effective_source_reliability(self.reliability, self.runtime_reliability)

    @property
    def effective_reliability_reason(self) -> str:
        return effective_source_reliability_reason(self.reliability, self.runtime_reliability)


@dataclass(frozen=True)
class PredictionLabCalibrationSummary:
    window_key: str
    profile_key: str
    current_time: datetime
    buckets: tuple[PredictionLabCalibrationBucket, ...]


@dataclass(frozen=True)
class PredictionLabBackfillResult:
    snapshots_scanned: int
    snapshots_replayed: int
    snapshots_skipped_existing: int
    prediction_events_created: int
    arrival_events_created: int
    evaluations_created: int


@dataclass(frozen=True)
class _VehicleProgressTrack:
    progress_meters: float
    velocity_mps: float
    sample_count: int
    stalled_seconds: int
    confidence: str


@dataclass(frozen=True)
class _CoordinateArrivalRouteMatch:
    evidence: str
    snap_distance_meters: float
    target_delta_meters: float
    progress_meters: float | None


def count_arrival_events(connection: sqlite3.Connection) -> int:
    return count_table_rows(connection, "arrival_events")


def count_prediction_events(connection: sqlite3.Connection) -> int:
    return count_table_rows(connection, "prediction_events")


def count_prediction_evaluations(connection: sqlite3.Connection) -> int:
    return count_table_rows(connection, "prediction_evaluations")


def prediction_bucket(minutes: int) -> str:
    return prediction_bucket_label(minutes)


def process_yandex_snapshot_for_prediction_lab(
    connection: sqlite3.Connection,
    *,
    yandex_snapshot_id: int,
    profile_key: str,
    forecast: YandexLiveForecast,
    sampled_at: datetime,
    traffic: RouteTrafficSnapshot | None,
) -> None:
    report_window = matching_report_window(sampled_at, profile_key)
    insert_prediction_events_for_snapshot(
        connection,
        yandex_snapshot_id=yandex_snapshot_id,
        profile_key=profile_key,
        forecast=forecast,
        sampled_at=sampled_at,
        report_window=report_window,
        traffic=traffic,
    )
    arrivals = infer_arrival_events_for_snapshot(
        connection,
        yandex_snapshot_id=yandex_snapshot_id,
        profile_key=profile_key,
        forecast=forecast,
        sampled_at=sampled_at,
    )
    for arrival in arrivals:
        evaluate_predictions_for_arrival(connection, arrival)


def backfill_prediction_lab(
    connection: sqlite3.Connection,
    *,
    profile_key: str | None = None,
    report_window_key: str | None = None,
    reset_existing: bool = False,
) -> PredictionLabBackfillResult:
    rows = _prediction_lab_backfill_rows(connection, profile_key=profile_key, report_window_key=report_window_key)
    snapshot_ids = tuple(int(row["id"]) for row in rows)
    if reset_existing and snapshot_ids:
        _delete_prediction_lab_events_for_snapshots(connection, snapshot_ids)
    base_predictions = count_prediction_events(connection)
    base_arrivals = count_arrival_events(connection)
    base_evaluations = count_prediction_evaluations(connection)
    replayed = 0
    skipped_existing = 0
    for row in rows:
        if not reset_existing and (int(row["has_prediction_events"]) or int(row["has_arrival_events"])):
            skipped_existing += 1
            continue
        sampled_at = datetime.fromisoformat(str(row["sampled_at"]))
        process_yandex_snapshot_for_prediction_lab(
            connection,
            yandex_snapshot_id=int(row["id"]),
            profile_key=str(row["profile_key"]),
            forecast=_forecast_from_snapshot_row(connection, row),
            sampled_at=sampled_at,
            traffic=_traffic_from_snapshot_row(row),
        )
        replayed += 1
    evaluate_pending_predictions(connection, profile_key=profile_key, report_window_key=report_window_key)
    connection.commit()
    return PredictionLabBackfillResult(
        snapshots_scanned=len(rows),
        snapshots_replayed=replayed,
        snapshots_skipped_existing=skipped_existing,
        prediction_events_created=count_prediction_events(connection) - base_predictions,
        arrival_events_created=count_arrival_events(connection) - base_arrivals,
        evaluations_created=count_prediction_evaluations(connection) - base_evaluations,
    )


def insert_prediction_events_for_snapshot(
    connection: sqlite3.Connection,
    *,
    yandex_snapshot_id: int,
    profile_key: str,
    forecast: YandexLiveForecast,
    sampled_at: datetime,
    report_window: ReportWindow | None,
    traffic: RouteTrafficSnapshot | None,
) -> tuple[int, ...]:
    report_window_key = report_window.key if report_window is not None else ""
    inserted: list[int] = []
    if _trusted_forecast_has_eta(forecast):
        for vehicle, minutes in _forecast_predictions(forecast):
            evidence = live_eta_evidence_adjustment(forecast, arrival_minutes=minutes)
            raw = _prediction_raw(forecast, vehicle, correction=None, evidence=evidence)
            inserted.append(
                _insert_prediction_event(
                    connection,
                    yandex_snapshot_id=yandex_snapshot_id,
                    profile_key=profile_key,
                    sampled_at=sampled_at,
                    report_window_key=report_window_key,
                    source=SOURCE_TARGET_STOP_LIVE,
                    source_method=forecast.source_method.value,
                    predicted_minutes=minutes,
                    confidence=forecast.confidence.value,
                    vehicle=vehicle,
                    traffic=traffic,
                    raw=raw,
                )
            )
            correction = load_residual_correction(
                connection,
                profile_key=profile_key,
                report_window_key=report_window_key,
                predicted_minutes=minutes,
                min_samples=RESIDUAL_MIN_SAMPLES,
                current_time=sampled_at,
            )
            if correction.applied:
                corrected_minutes = max(0, minutes + correction.correction_minutes)
                raw = _prediction_raw(forecast, vehicle, correction=correction, evidence=evidence)
                inserted.append(
                    _insert_prediction_event(
                        connection,
                        yandex_snapshot_id=yandex_snapshot_id,
                        profile_key=profile_key,
                        sampled_at=sampled_at,
                        report_window_key=report_window_key,
                        source=SOURCE_CORRECTED_LIVE,
                        source_method=forecast.source_method.value,
                        predicted_minutes=corrected_minutes,
                        confidence=EtaConfidence.MEDIUM.value,
                        vehicle=vehicle,
                        traffic=traffic,
                        raw=raw,
                    )
                )

    for vehicle, minutes, raw in estimate_vehicle_progress_candidates(
        connection,
        profile_key=profile_key,
        forecast=forecast,
        sampled_at=sampled_at,
    ):
        inserted.append(
            _insert_prediction_event(
                connection,
                yandex_snapshot_id=yandex_snapshot_id,
                profile_key=profile_key,
                sampled_at=sampled_at,
                report_window_key=report_window_key,
                source=SOURCE_VEHICLE_PROGRESS,
                source_method=forecast.source_method.value,
                predicted_minutes=minutes,
                confidence=vehicle_progress_confidence(raw).value,
                vehicle=vehicle,
                traffic=traffic,
                raw=raw,
            )
        )
    history_prediction = estimate_history_headway_prediction(
        connection,
        profile_key=profile_key,
        sampled_at=sampled_at,
        report_window=report_window,
    )
    if history_prediction is not None:
        minutes, raw = history_prediction
        inserted.append(
            _insert_prediction_event(
                connection,
                yandex_snapshot_id=yandex_snapshot_id,
                profile_key=profile_key,
                sampled_at=sampled_at,
                report_window_key=report_window_key,
                source=SOURCE_HISTORY_HEADWAY,
                source_method="history",
                predicted_minutes=minutes,
                confidence=EtaConfidence.LOW.value,
                vehicle=None,
                traffic=traffic,
                raw=raw,
            )
        )
    ensemble_id = _insert_ensemble_prediction_event(connection, inserted)
    if ensemble_id is not None:
        inserted.append(ensemble_id)
    return tuple(inserted)


def infer_arrival_events_for_snapshot(
    connection: sqlite3.Connection,
    *,
    yandex_snapshot_id: int,
    profile_key: str,
    forecast: YandexLiveForecast,
    sampled_at: datetime,
) -> tuple[ArrivalEvent, ...]:
    arrivals: list[ArrivalEvent] = []
    arrived_vehicle_ids: set[str] = set()
    if _trusted_forecast_has_eta(forecast):
        for vehicle, minutes in _forecast_predictions(forecast):
            if minutes <= 1:
                vehicle_id = vehicle.vehicle_id if vehicle is not None else ""
                if minutes == 1 and not _has_prior_prediction(connection, profile_key, vehicle_id, sampled_at):
                    continue
                event = _insert_arrival_event(
                    connection,
                    yandex_snapshot_id=yandex_snapshot_id,
                    profile_key=profile_key,
                    vehicle=vehicle,
                    stop_id=_target_stop_id(profile_key),
                    arrived_at=sampled_at,
                    source="trusted_eta",
                    confidence=EtaConfidence.HIGH.value if minutes == 0 else EtaConfidence.MEDIUM.value,
                    raw={
                        "arrival_minutes": minutes,
                        "source_method": forecast.source_method.value,
                    },
                )
                if event is not None:
                    arrivals.append(event)
                    if event.vehicle_id:
                        arrived_vehicle_ids.add(event.vehicle_id)
    coordinate_geometry = load_route_geometry(connection, profile_key, sampled_at=sampled_at)
    for vehicle in forecast.vehicles:
        if vehicle.vehicle_id in arrived_vehicle_ids:
            continue
        if not vehicle_is_fresh(vehicle):
            continue
        if vehicle.lat is None or vehicle.lng is None:
            continue
        route_match = _coordinate_arrival_route_match(
            connection,
            profile_key,
            vehicle,
            sampled_at,
            coordinate_geometry,
        )
        if route_match is None:
            continue
        distance = _distance_to_target_stop(profile_key, vehicle.lat, vehicle.lng)
        if distance is None or distance > TARGET_STOP_RADIUS_METERS:
            continue
        event = _insert_arrival_event(
            connection,
            yandex_snapshot_id=yandex_snapshot_id,
            profile_key=profile_key,
            vehicle=vehicle,
            stop_id=_target_stop_id(profile_key),
            arrived_at=sampled_at,
            source="coordinate_stop",
            confidence=EtaConfidence.MEDIUM.value,
            raw={
                "distance_meters": round(distance),
                "source_method": forecast.source_method.value,
                "route_thread_id": coordinate_geometry.thread_id if coordinate_geometry is not None else "",
                "vehicle_thread_id": vehicle.thread_id,
                "route_evidence": route_match.evidence,
                "route_snap_distance_meters": round(route_match.snap_distance_meters),
                "target_route_delta_meters": round(route_match.target_delta_meters),
                "route_progress_meters": round(route_match.progress_meters)
                if route_match.progress_meters is not None
                else None,
            },
        )
        if event is not None:
            arrivals.append(event)
    return tuple(arrivals)


def evaluate_pending_predictions(
    connection: sqlite3.Connection,
    *,
    profile_key: str | None = None,
    report_window_key: str | None = None,
) -> int:
    filters = []
    params: list[object] = []
    if profile_key is not None:
        filters.append("profile_key = ?")
        params.append(profile_key)
    rows = connection.execute(
        f"""
        SELECT *
        FROM arrival_events
        {"WHERE " + " AND ".join(filters) if filters else ""}
        ORDER BY arrived_at
        """,
        tuple(params),
    ).fetchall()
    total = 0
    for row in rows:
        arrival = _arrival_from_row(row)
        total += evaluate_predictions_for_arrival(connection, arrival, report_window_key=report_window_key)
    connection.commit()
    return total


def evaluate_predictions_for_arrival(
    connection: sqlite3.Connection,
    arrival: ArrivalEvent,
    *,
    report_window_key: str | None = None,
) -> int:
    since = arrival.arrived_at - timedelta(minutes=PREDICTION_MATCH_MINUTES)
    filters = [
        "prediction_events.profile_key = ?",
        "prediction_events.sampled_at >= ?",
        "prediction_events.sampled_at < ?",
        "prediction_events.source IN ({})".format(",".join("?" for _ in EVALUATED_SOURCES)),
        "prediction_evaluations.id IS NULL",
    ]
    params: list[object] = [
        arrival.profile_key,
        since.isoformat(),
        arrival.arrived_at.isoformat(),
        *EVALUATED_SOURCES,
    ]
    if report_window_key is not None:
        filters.append("prediction_events.report_window_key = ?")
        params.append(report_window_key)
    if arrival.vehicle_id:
        filters.append("(prediction_events.vehicle_id = '' OR prediction_events.vehicle_id = ?)")
        params.append(arrival.vehicle_id)
        if arrival.thread_id:
            filters.append("(prediction_events.thread_id = '' OR prediction_events.thread_id = ?)")
            params.append(arrival.thread_id)
    else:
        filters.append("prediction_events.vehicle_id = ''")
    rows = connection.execute(
        f"""
        SELECT prediction_events.*
        FROM prediction_events
        LEFT JOIN prediction_evaluations
          ON prediction_evaluations.prediction_event_id = prediction_events.id
        WHERE {" AND ".join(filters)}
        ORDER BY prediction_events.sampled_at
        """,
        tuple(params),
    ).fetchall()
    inserted = 0
    for row in rows:
        prediction = _prediction_from_row(row)
        actual_minutes = max(0, round((arrival.arrived_at - prediction.sampled_at).total_seconds() / 60))
        error_minutes = actual_minutes - prediction.predicted_minutes
        cursor = connection.execute(
            """
            INSERT OR IGNORE INTO prediction_evaluations(
                prediction_event_id, arrival_event_id, profile_key, evaluated_at,
                actual_minutes, predicted_minutes, error_minutes, bucket, source, raw_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                prediction.id,
                arrival.id,
                arrival.profile_key,
                now_local().isoformat(),
                actual_minutes,
                prediction.predicted_minutes,
                error_minutes,
                prediction_bucket(prediction.predicted_minutes),
                prediction.source,
                json.dumps(
                    {
                        "arrival_source": arrival.source,
                        "arrival_confidence": arrival.confidence,
                        "vehicle_id": arrival.vehicle_id,
                    },
                    ensure_ascii=False,
                ),
            ),
        )
        inserted += cursor.rowcount
    return inserted


def estimate_vehicle_progress_candidates(
    connection: sqlite3.Connection,
    *,
    profile_key: str,
    forecast: YandexLiveForecast,
    sampled_at: datetime,
) -> tuple[tuple[YandexVehicle, int, dict[str, object]], ...]:
    geometry = load_route_geometry(connection, profile_key, sampled_at=sampled_at)
    if geometry is None:
        return ()
    candidates: list[tuple[YandexVehicle, int, dict[str, object]]] = []
    for vehicle in forecast.vehicles:
        if not vehicle.vehicle_id or vehicle.lat is None or vehicle.lng is None:
            continue
        if not vehicle_is_fresh(vehicle):
            continue
        if _vehicle_has_wrong_route_thread(vehicle, geometry):
            continue
        previous_positions = _previous_vehicle_positions(
            connection,
            profile_key,
            vehicle.vehicle_id,
            sampled_at,
            route_thread_id=geometry.thread_id,
        )
        if not previous_positions:
            continue
        current_observed_at = position_observed_at(sampled_at, vehicle.age_seconds)
        current_projection = route_projection_for_point(geometry.points, geometry.measures, vehicle.lat, vehicle.lng)
        if current_projection is None:
            continue
        if current_projection.distance_meters > VEHICLE_PROGRESS_MAX_ROUTE_SNAP_METERS:
            continue
        current_measure = current_projection.measure
        if current_measure >= geometry.target_measure:
            continue

        speeds: list[float] = []
        previous_samples: list[dict[str, object]] = []
        for previous in previous_positions:
            previous_projection = route_projection_for_point(
                geometry.points, geometry.measures, previous.lat, previous.lng
            )
            if previous_projection is None:
                continue
            if previous_projection.distance_meters > VEHICLE_PROGRESS_MAX_ROUTE_SNAP_METERS:
                continue
            elapsed_seconds = (current_observed_at - previous.observed_at).total_seconds()
            if elapsed_seconds <= 0:
                continue
            speed_mps = (current_measure - previous_projection.measure) / elapsed_seconds
            if speed_mps < VEHICLE_PROGRESS_MIN_SPEED_MPS or speed_mps > VEHICLE_PROGRESS_MAX_SPEED_MPS:
                continue
            speeds.append(speed_mps)
            previous_samples.append(
                {
                    "observed_at": previous.observed_at.isoformat(),
                    "age_seconds": previous.age_seconds,
                    "route_snap_distance_meters": round(previous_projection.distance_meters),
                    "speed_mps": round(speed_mps, 2),
                }
            )
        if not speeds:
            continue
        observed_speed_mps = _median_float(tuple(speeds))
        remaining_meters = geometry.target_measure - current_measure
        track = _update_vehicle_progress_track(
            connection,
            profile_key=profile_key,
            vehicle=vehicle,
            route_thread_id=geometry.thread_id,
            observed_at=current_observed_at,
            progress_meters=current_measure,
            observed_speed_mps=observed_speed_mps,
        )
        if track is None:
            continue
        speed_mps = _vehicle_progress_effective_speed(observed_speed_mps, track)
        stalled_buffer = _vehicle_progress_stalled_buffer(track)
        minutes = ceil(remaining_meters / speed_mps / 60) + stalled_buffer
        if minutes <= 0 or minutes > VEHICLE_PROGRESS_MAX_MINUTES:
            continue
        candidates.append(
            (
                vehicle,
                minutes,
                {
                    "route_thread_id": geometry.thread_id,
                    "vehicle_thread_id": vehicle.thread_id,
                    "thread_match": "direct" if vehicle.thread_id else "recovered_from_previous",
                    "current_observed_at": current_observed_at.isoformat(),
                    "current_age_seconds": vehicle.age_seconds,
                    "remaining_meters": round(remaining_meters),
                    "route_snap_distance_meters": round(current_projection.distance_meters),
                    "speed_mps": round(speed_mps, 2),
                    "observed_speed_mps": round(observed_speed_mps, 2),
                    "speed_sample_count": len(speeds),
                    "speed_samples": previous_samples,
                    "previous_observed_at": str(previous_samples[0]["observed_at"]),
                    "tracker": "alpha_beta_v2",
                    "tracker_sample_count": track.sample_count,
                    "tracker_velocity_mps": round(track.velocity_mps, 2),
                    "stalled_seconds": track.stalled_seconds,
                    "stalled_buffer_minutes": stalled_buffer,
                },
            )
        )
    return tuple(candidates)


def _update_vehicle_progress_track(
    connection: sqlite3.Connection,
    *,
    profile_key: str,
    vehicle: YandexVehicle,
    route_thread_id: str,
    observed_at: datetime,
    progress_meters: float,
    observed_speed_mps: float,
) -> _VehicleProgressTrack | None:
    row = connection.execute(
        """
        SELECT *
        FROM vehicle_progress_tracks
        WHERE profile_key = ? AND vehicle_id = ?
        """,
        (profile_key, vehicle.vehicle_id),
    ).fetchone()
    if row is None or str(row["thread_id"]) != route_thread_id:
        track = _initial_vehicle_progress_track(observed_speed_mps)
    else:
        previous_at = datetime.fromisoformat(str(row["updated_at"]))
        elapsed_seconds = (observed_at - previous_at).total_seconds()
        if elapsed_seconds <= 0:
            return _vehicle_progress_track_from_row(row)
        if elapsed_seconds > VEHICLE_PROGRESS_TRACK_MAX_AGE_SECONDS:
            track = _initial_vehicle_progress_track(observed_speed_mps)
        else:
            measured_delta = progress_meters - float(row["progress_meters"])
            measured_speed = measured_delta / elapsed_seconds
            if measured_delta < -VEHICLE_PROGRESS_TRACK_MAX_BACKTRACK_METERS:
                return None
            if abs(measured_speed) > VEHICLE_PROGRESS_TELEPORT_SPEED_MPS:
                return None
            track = _smoothed_vehicle_progress_track(
                row,
                observed_progress_meters=progress_meters,
                observed_speed_mps=observed_speed_mps,
                elapsed_seconds=elapsed_seconds,
                measured_delta=measured_delta,
            )
    connection.execute(
        """
        INSERT INTO vehicle_progress_tracks(
            profile_key, vehicle_id, thread_id, progress_meters, velocity_mps,
            updated_at, confidence, stalled_seconds, sample_count, raw_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(profile_key, vehicle_id) DO UPDATE SET
            thread_id = excluded.thread_id,
            progress_meters = excluded.progress_meters,
            velocity_mps = excluded.velocity_mps,
            updated_at = excluded.updated_at,
            confidence = excluded.confidence,
            stalled_seconds = excluded.stalled_seconds,
            sample_count = excluded.sample_count,
            raw_json = excluded.raw_json
        """,
        (
            profile_key,
            vehicle.vehicle_id,
            route_thread_id,
            progress_meters,
            track.velocity_mps,
            observed_at.isoformat(),
            track.confidence,
            track.stalled_seconds,
            track.sample_count,
            json.dumps(
                {
                    "alpha": VEHICLE_PROGRESS_TRACK_ALPHA,
                    "beta": VEHICLE_PROGRESS_TRACK_BETA,
                    "observed_speed_mps": round(observed_speed_mps, 2),
                },
                ensure_ascii=False,
            ),
        ),
    )
    return track


def _initial_vehicle_progress_track(observed_speed_mps: float) -> _VehicleProgressTrack:
    return _VehicleProgressTrack(
        progress_meters=0.0,
        velocity_mps=observed_speed_mps,
        sample_count=1,
        stalled_seconds=0,
        confidence="warming_up",
    )


def _vehicle_progress_track_from_row(row: sqlite3.Row) -> _VehicleProgressTrack:
    return _VehicleProgressTrack(
        progress_meters=float(row["progress_meters"]),
        velocity_mps=float(row["velocity_mps"]),
        sample_count=int(row["sample_count"]),
        stalled_seconds=int(row["stalled_seconds"]),
        confidence=str(row["confidence"]),
    )


def _smoothed_vehicle_progress_track(
    row: sqlite3.Row,
    *,
    observed_progress_meters: float,
    observed_speed_mps: float,
    elapsed_seconds: float,
    measured_delta: float,
) -> _VehicleProgressTrack:
    predicted_progress = float(row["progress_meters"]) + float(row["velocity_mps"]) * elapsed_seconds
    residual = observed_progress_meters - predicted_progress
    velocity = float(row["velocity_mps"]) + VEHICLE_PROGRESS_TRACK_BETA * residual / elapsed_seconds
    velocity = _clamp_float(velocity, VEHICLE_PROGRESS_TRACK_MIN_SPEED_MPS, VEHICLE_PROGRESS_MAX_SPEED_MPS)
    if measured_delta < VEHICLE_PROGRESS_STALLED_DELTA_METERS:
        stalled_seconds = int(row["stalled_seconds"]) + round(elapsed_seconds)
        velocity = min(velocity, max(VEHICLE_PROGRESS_TRACK_MIN_SPEED_MPS, observed_speed_mps))
    else:
        stalled_seconds = 0
    return _VehicleProgressTrack(
        progress_meters=predicted_progress + VEHICLE_PROGRESS_TRACK_ALPHA * residual,
        velocity_mps=velocity,
        sample_count=int(row["sample_count"]) + 1,
        stalled_seconds=stalled_seconds,
        confidence="tracking",
    )


def _vehicle_progress_effective_speed(observed_speed_mps: float, track: _VehicleProgressTrack) -> float:
    if track.sample_count < VEHICLE_PROGRESS_TRACK_MIN_SAMPLES:
        return observed_speed_mps
    return _clamp_float(
        track.velocity_mps,
        VEHICLE_PROGRESS_TRACK_MIN_SPEED_MPS,
        VEHICLE_PROGRESS_MAX_SPEED_MPS,
    )


def _vehicle_progress_stalled_buffer(track: _VehicleProgressTrack) -> int:
    if track.stalled_seconds >= VEHICLE_PROGRESS_STALLED_AFTER_SECONDS:
        return VEHICLE_PROGRESS_STALLED_BUFFER_MINUTES
    return 0


def vehicle_progress_confidence(raw: dict[str, object]) -> EtaConfidence:
    speed_sample_count = _optional_int(raw.get("speed_sample_count")) or 0
    stalled_seconds = _optional_int(raw.get("stalled_seconds")) or 0
    age_seconds = _optional_int(raw.get("current_age_seconds"))
    snap_distance = _optional_float(raw.get("route_snap_distance_meters"))
    if age_seconds is None or snap_distance is None:
        return EtaConfidence.LOW
    if stalled_seconds >= VEHICLE_PROGRESS_STALLED_AFTER_SECONDS:
        return EtaConfidence.LOW
    if (
        speed_sample_count >= VEHICLE_PROGRESS_MEDIUM_MIN_SPEED_SAMPLES
        and age_seconds <= VEHICLE_PROGRESS_MEDIUM_MAX_AGE_SECONDS
        and snap_distance <= VEHICLE_PROGRESS_MEDIUM_MAX_ROUTE_SNAP_METERS
    ):
        return EtaConfidence.MEDIUM
    return EtaConfidence.LOW


def estimate_history_headway_prediction(
    connection: sqlite3.Connection,
    *,
    profile_key: str,
    sampled_at: datetime,
    report_window: ReportWindow | None,
) -> tuple[int, dict[str, object]] | None:
    if report_window is None:
        return None
    weekdays = day_kind_weekdays(sampled_at)
    for bucket_minutes in HISTORY_HEADWAY_BUCKETS:
        history = load_yandex_eta_history_for_profile_window(
            connection,
            profile_key=profile_key,
            current_time=sampled_at,
            days=HISTORY_HEADWAY_DAYS,
            bucket_minutes=bucket_minutes,
            weekdays=weekdays,
            max_age_seconds=HISTORY_HEADWAY_MAX_AGE_SECONDS,
            report_window_key=report_window.key,
            before=sampled_at,
        )
        if len(history.arrival_minutes) < HISTORY_HEADWAY_MIN_OBSERVATIONS:
            continue
        if history.distinct_service_days < HISTORY_HEADWAY_MIN_DAYS:
            continue
        return (
            _percentile(history.arrival_minutes, HISTORY_HEADWAY_PERCENTILE),
            {
                "bucket_minutes": bucket_minutes,
                "sample_count": len(history.arrival_minutes),
                "distinct_service_days": history.distinct_service_days,
                "percentile": HISTORY_HEADWAY_PERCENTILE,
            },
        )
    return None


def load_residual_correction(
    connection: sqlite3.Connection,
    *,
    profile_key: str,
    report_window_key: str | None,
    predicted_minutes: int,
    min_samples: int = RESIDUAL_MIN_SAMPLES,
    current_time: datetime | None = None,
) -> ResidualCorrection:
    current_time = current_time or now_local()
    bucket = prediction_bucket(predicted_minutes)
    values = _error_values(
        connection,
        profile_key,
        report_window_key,
        SOURCE_TARGET_STOP_LIVE,
        bucket,
        current_time=current_time,
        min_arrival_confidence=EtaConfidence.HIGH,
    )
    scope = "bucket"
    if len(values) < min_samples:
        source_values = _error_values(
            connection,
            profile_key,
            report_window_key,
            SOURCE_TARGET_STOP_LIVE,
            bucket=None,
            current_time=current_time,
            min_arrival_confidence=EtaConfidence.HIGH,
        )
        if len(source_values) < min_samples:
            return ResidualCorrection(bucket, len(values), 0, 0, scope)
        values = source_values
        scope = "source"
    p10 = _percentile(values, 10)
    correction = max(-RESIDUAL_MAX_EARLY_CORRECTION_MINUTES, min(0, p10))
    return ResidualCorrection(bucket, len(values), p10, correction, scope)


def load_source_reliability(
    connection: sqlite3.Connection,
    *,
    profile_key: str,
    report_window_key: str | None,
    source: str,
    predicted_minutes: int,
    min_samples: int = SOURCE_RELIABILITY_MIN_SAMPLES,
    current_time: datetime | None = None,
    runtime_source: str | None = None,
) -> SourceReliability:
    current_time = current_time or now_local()
    bucket = prediction_bucket(predicted_minutes)
    values = _error_values(
        connection,
        profile_key,
        report_window_key,
        source,
        bucket,
        current_time=current_time,
        min_arrival_confidence=EtaConfidence.HIGH,
        runtime_source=runtime_source,
    )
    scope = "bucket"
    if len(values) < min_samples:
        fallback_values = _error_values(
            connection,
            profile_key,
            report_window_key,
            source,
            bucket,
            current_time=current_time,
            min_arrival_confidence=EtaConfidence.MEDIUM,
            runtime_source=runtime_source,
        )
        if len(fallback_values) >= min_samples:
            values = fallback_values
    if len(values) < min_samples:
        source_values = _error_values(
            connection,
            profile_key,
            report_window_key,
            source,
            bucket=None,
            current_time=current_time,
            min_arrival_confidence=EtaConfidence.HIGH,
            runtime_source=runtime_source,
        )
        if len(source_values) < min_samples:
            source_values = _error_values(
                connection,
                profile_key,
                report_window_key,
                source,
                bucket=None,
                current_time=current_time,
                min_arrival_confidence=EtaConfidence.MEDIUM,
                runtime_source=runtime_source,
            )
        if len(source_values) >= min_samples:
            values = source_values
            scope = "source"
    misses = sum(1 for value in values if value < 0)
    miss_rate = round(misses * 100 / len(values)) if values else 0
    p10 = _percentile(values, 10) if values else 0
    buffer = _source_safety_buffer(values, min_samples=min_samples)
    return SourceReliability(
        source,
        bucket,
        len(values),
        misses,
        miss_rate,
        p10,
        buffer,
        _reliability_scope(scope, runtime_source),
    )


def effective_source_reliability(
    baseline: SourceReliability,
    runtime: SourceReliability,
) -> SourceReliability:
    reliability, _reason = _effective_source_reliability_choice(baseline, runtime)
    return reliability


def effective_source_reliability_reason(
    baseline: SourceReliability,
    runtime: SourceReliability,
) -> str:
    _reliability, reason = _effective_source_reliability_choice(baseline, runtime)
    return reason


def _effective_source_reliability_choice(
    baseline: SourceReliability,
    runtime: SourceReliability,
) -> tuple[SourceReliability, str]:
    if runtime.sample_count <= 0:
        return baseline, "baseline_no_runtime"
    if baseline.sample_count <= 0:
        return runtime, "runtime_no_baseline"
    if runtime.safety_buffer_minutes > baseline.safety_buffer_minutes:
        return runtime, "runtime_buffer"
    if runtime.safety_buffer_minutes < baseline.safety_buffer_minutes:
        return baseline, "baseline_buffer"
    if runtime.miss_rate_percent > baseline.miss_rate_percent:
        return runtime, "runtime_miss_rate"
    if runtime.miss_rate_percent < baseline.miss_rate_percent:
        return baseline, "baseline_miss_rate"
    return baseline, "baseline_tie"


def summarize_prediction_lab_window(
    connection: sqlite3.Connection,
    *,
    profile_key: str,
    report_window_key: str,
) -> PredictionLabSummary:
    arrivals = _count_arrivals(connection, profile_key, report_window_key)
    predictions = _count_predictions(connection, profile_key, report_window_key)
    evaluated = _count_evaluations(connection, profile_key, report_window_key)
    return PredictionLabSummary(
        window_key=report_window_key,
        profile_key=profile_key,
        arrival_events=arrivals,
        prediction_events=predictions,
        evaluated_predictions=evaluated,
        latest_arrival_at=_latest_arrival_at(connection, profile_key, report_window_key),
        latest_prediction_at=_latest_prediction_at(connection, profile_key, report_window_key),
        sources=_source_summaries(connection, profile_key, report_window_key),
    )


def summarize_prediction_lab_calibration(
    connection: sqlite3.Connection,
    *,
    profile_key: str,
    report_window_key: str,
    current_time: datetime | None = None,
    reliability_min_samples: int = SOURCE_RELIABILITY_MIN_SAMPLES,
    runtime_reliability_min_samples: int = RUNTIME_RELIABILITY_MIN_SAMPLES,
    residual_min_samples: int = RESIDUAL_MIN_SAMPLES,
) -> PredictionLabCalibrationSummary:
    current_time = current_time or now_local()
    return PredictionLabCalibrationSummary(
        window_key=report_window_key,
        profile_key=profile_key,
        current_time=current_time,
        buckets=_calibration_buckets(
            connection,
            profile_key,
            report_window_key,
            current_time=current_time,
            reliability_min_samples=reliability_min_samples,
            runtime_reliability_min_samples=runtime_reliability_min_samples,
            residual_min_samples=residual_min_samples,
        ),
    )


def load_arrival_events(
    connection: sqlite3.Connection,
    *,
    profile_key: str,
    report_window_key: str,
    limit: int,
) -> tuple[ArrivalEvent, ...]:
    rows = connection.execute(
        """
        SELECT arrival_events.*
        FROM arrival_events
        JOIN yandex_snapshots ON yandex_snapshots.id = arrival_events.yandex_snapshot_id
        JOIN yandex_forecast_samples ON yandex_forecast_samples.yandex_snapshot_id = yandex_snapshots.id
        WHERE arrival_events.profile_key = ?
          AND yandex_forecast_samples.report_window_key = ?
        ORDER BY arrival_events.arrived_at DESC
        LIMIT ?
        """,
        (profile_key, report_window_key, limit),
    ).fetchall()
    return tuple(arrival for row in rows if (arrival := _arrival_from_row(row)) is not None)


def upsert_route_geometry(
    connection: sqlite3.Connection,
    *,
    profile_key: str,
    target_stop_id: str,
    topology: YandexLineTopology,
    updated_at: datetime | None = None,
    preferred_thread_ids: tuple[str, ...] = (),
    candidate_stop_ids: tuple[str, ...] = (),
) -> None:
    updated_at = updated_at or now_local()
    stop_ids = _unique_strings((*candidate_stop_ids, target_stop_id))
    selected = topology.thread_for_stops(stop_ids, preferred_thread_ids=preferred_thread_ids)
    if selected is None:
        raise ValueError(f"target stops {', '.join(stop_ids)} are absent from getLine topology")
    thread, selected_stop_id = selected
    stops = [asdict(stop) for stop in thread.stops]
    points = [[point.lng, point.lat] for point in thread.points]
    raw = {
        "line_id": topology.line_id,
        "active_thread_id": topology.active_thread_id,
        "thread_id": thread.thread_id,
        "requested_target_stop_id": target_stop_id,
        "selected_stop_id": selected_stop_id,
        "candidate_stop_ids": list(stop_ids),
        "preferred_thread_ids": list(preferred_thread_ids),
        "segment_point_count": thread.segment_point_count,
    }
    connection.execute(
        """
        INSERT INTO route_geometry(
            profile_key, line_id, thread_id, target_stop_id,
            route_polyline_json, stops_json, updated_at, raw_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(profile_key) DO UPDATE SET
            line_id = excluded.line_id,
            thread_id = excluded.thread_id,
            target_stop_id = excluded.target_stop_id,
            route_polyline_json = excluded.route_polyline_json,
            stops_json = excluded.stops_json,
            updated_at = excluded.updated_at,
            raw_json = excluded.raw_json
        """,
        (
            profile_key,
            topology.line_id,
            thread.thread_id,
            selected_stop_id,
            json.dumps(points, ensure_ascii=False),
            json.dumps(stops, ensure_ascii=False),
            updated_at.isoformat(),
            json.dumps(raw, ensure_ascii=False),
        ),
    )
    connection.commit()


def _unique_strings(values: tuple[str, ...]) -> tuple[str, ...]:
    result: list[str] = []
    for value in values:
        if value and value not in result:
            result.append(value)
    return tuple(result)


def touch_route_geometry(
    connection: sqlite3.Connection,
    *,
    profile_key: str,
    updated_at: datetime | None = None,
) -> bool:
    updated_at = updated_at or now_local()
    cursor = connection.execute(
        "UPDATE route_geometry SET updated_at = ? WHERE profile_key = ?",
        (updated_at.isoformat(), profile_key),
    )
    connection.commit()
    return cursor.rowcount > 0


def _prediction_lab_backfill_rows(
    connection: sqlite3.Connection,
    *,
    profile_key: str | None,
    report_window_key: str | None,
) -> tuple[sqlite3.Row, ...]:
    filters: list[str] = []
    params: list[object] = []
    if profile_key is not None:
        filters.append("yandex_snapshots.profile_key = ?")
        params.append(profile_key)
    if report_window_key is not None:
        filters.append("COALESCE(yandex_forecast_samples.report_window_key, '') = ?")
        params.append(report_window_key)
    where = f"WHERE {' AND '.join(filters)}" if filters else ""
    rows = connection.execute(
        f"""
        SELECT
            yandex_snapshots.*,
            yandex_forecast_samples.report_window_key AS sample_report_window_key,
            yandex_forecast_samples.traffic_provider,
            yandex_forecast_samples.traffic_status,
            yandex_forecast_samples.traffic_delay_seconds,
            yandex_forecast_samples.traffic_jams_level,
            yandex_forecast_samples.route_duration_seconds,
            yandex_forecast_samples.route_duration_in_traffic_seconds,
            yandex_forecast_samples.traffic_distance_meters,
            yandex_forecast_samples.traffic_raw_json,
            EXISTS(
                SELECT 1
                FROM prediction_events
                WHERE prediction_events.yandex_snapshot_id = yandex_snapshots.id
            ) AS has_prediction_events,
            EXISTS(
                SELECT 1
                FROM arrival_events
                WHERE arrival_events.yandex_snapshot_id = yandex_snapshots.id
            ) AS has_arrival_events
        FROM yandex_snapshots
        LEFT JOIN yandex_forecast_samples
          ON yandex_forecast_samples.yandex_snapshot_id = yandex_snapshots.id
        {where}
        ORDER BY yandex_snapshots.sampled_at, yandex_snapshots.id
        """,
        tuple(params),
    ).fetchall()
    return tuple(rows)


def _delete_prediction_lab_events_for_snapshots(connection: sqlite3.Connection, snapshot_ids: tuple[int, ...]) -> None:
    if not snapshot_ids:
        return
    placeholders = ",".join("?" for _ in snapshot_ids)
    prediction_ids = [
        int(row["id"])
        for row in connection.execute(
            f"SELECT id FROM prediction_events WHERE yandex_snapshot_id IN ({placeholders})",
            snapshot_ids,
        ).fetchall()
    ]
    arrival_ids = [
        int(row["id"])
        for row in connection.execute(
            f"SELECT id FROM arrival_events WHERE yandex_snapshot_id IN ({placeholders})",
            snapshot_ids,
        ).fetchall()
    ]
    if prediction_ids:
        prediction_placeholders = ",".join("?" for _ in prediction_ids)
        connection.execute(
            f"DELETE FROM prediction_evaluations WHERE prediction_event_id IN ({prediction_placeholders})",
            prediction_ids,
        )
    if arrival_ids:
        arrival_placeholders = ",".join("?" for _ in arrival_ids)
        connection.execute(
            f"DELETE FROM prediction_evaluations WHERE arrival_event_id IN ({arrival_placeholders})",
            arrival_ids,
        )
    connection.execute(
        f"DELETE FROM prediction_events WHERE yandex_snapshot_id IN ({placeholders})",
        snapshot_ids,
    )
    connection.execute(
        f"DELETE FROM arrival_events WHERE yandex_snapshot_id IN ({placeholders})",
        snapshot_ids,
    )


def _forecast_from_snapshot_row(connection: sqlite3.Connection, row: sqlite3.Row) -> YandexLiveForecast:
    raw = _json_object(str(row["raw_json"]))
    return YandexLiveForecast(
        enabled=_bool_value(raw.get("enabled"), default=True),
        available=bool(row["available"]),
        source_method=_source_method(row["source_method"]),
        status=_source_status(row["source_status"]),
        arrival_minutes=arrival_minutes_from_json(str(row["arrival_minutes_json"])),
        vehicles=_vehicles_for_snapshot(connection, int(row["id"])),
        vehicle_count=int(row["vehicle_count"]),
        newest_age_seconds=_optional_int(raw.get("newest_age_seconds")),
        confidence=_eta_confidence(raw.get("confidence")),
        fallback_reason=str(row["fallback_reason"] or raw.get("fallback_reason") or ""),
        raw_status=str(raw.get("raw_status") or ""),
        diagnostics=_string_tuple(raw.get("diagnostics")),
    )


def _vehicles_for_snapshot(connection: sqlite3.Connection, snapshot_id: int) -> tuple[YandexVehicle, ...]:
    rows = connection.execute(
        """
        SELECT vehicle_id, thread_id, lat, lng, arrival_minutes, age_seconds
        FROM yandex_vehicle_observations
        WHERE snapshot_id = ?
        ORDER BY id
        """,
        (snapshot_id,),
    ).fetchall()
    return tuple(
        YandexVehicle(
            vehicle_id=str(row["vehicle_id"]),
            thread_id=str(row["thread_id"]),
            lat=_optional_float(row["lat"]),
            lng=_optional_float(row["lng"]),
            arrival_minutes=_optional_int(row["arrival_minutes"]),
            age_seconds=_optional_int(row["age_seconds"]),
        )
        for row in rows
    )


def _traffic_from_snapshot_row(row: sqlite3.Row) -> RouteTrafficSnapshot | None:
    provider = row["traffic_provider"]
    if provider is None:
        return None
    return RouteTrafficSnapshot(
        provider=str(provider),
        status=str(row["traffic_status"] or "not_collected"),
        jams_level=_optional_int(row["traffic_jams_level"]),
        route_duration_seconds=_optional_int(row["route_duration_seconds"]),
        route_duration_in_traffic_seconds=_optional_int(row["route_duration_in_traffic_seconds"]),
        delay_seconds=_optional_int(row["traffic_delay_seconds"]),
        distance_meters=_optional_int(row["traffic_distance_meters"]),
        raw=_json_object(str(row["traffic_raw_json"] or "{}")),
    )


def _source_method(value: object) -> YandexSourceMethod:
    try:
        return YandexSourceMethod(str(value))
    except ValueError:
        return YandexSourceMethod.NONE


def _source_status(value: object) -> YandexSourceStatus:
    try:
        return YandexSourceStatus(str(value))
    except ValueError:
        return YandexSourceStatus.UNAVAILABLE


def _eta_confidence(value: object) -> EtaConfidence:
    try:
        return EtaConfidence(str(value))
    except ValueError:
        return EtaConfidence.UNKNOWN


def _bool_value(value: object, *, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return bool(value)
    return default


def _string_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(str(item) for item in value)


def _json_object(raw_json: str) -> dict[str, object]:
    try:
        value = json.loads(raw_json)
    except json.JSONDecodeError:
        return {}
    if isinstance(value, dict):
        return value
    return {}


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _trusted_forecast_has_eta(forecast: YandexLiveForecast) -> bool:
    return forecast_has_trusted_fresh_eta(forecast)


def _forecast_predictions(
    forecast: YandexLiveForecast,
) -> tuple[tuple[YandexVehicle | None, int], ...]:
    vehicle_predictions = tuple(
        (vehicle, int(vehicle.arrival_minutes)) for vehicle in forecast.vehicles if vehicle.arrival_minutes is not None
    )
    if vehicle_predictions:
        return vehicle_predictions
    return tuple((None, int(minutes)) for minutes in forecast.arrival_minutes)


def _insert_prediction_event(
    connection: sqlite3.Connection,
    *,
    yandex_snapshot_id: int,
    profile_key: str,
    sampled_at: datetime,
    report_window_key: str,
    source: str,
    source_method: str,
    predicted_minutes: int,
    confidence: str,
    vehicle: YandexVehicle | None,
    traffic: RouteTrafficSnapshot | None,
    raw: dict[str, object],
) -> int:
    cursor = connection.execute(
        """
        INSERT INTO prediction_events(
            yandex_snapshot_id, profile_key, sampled_at, report_window_key,
            source, source_method, predicted_minutes, predicted_arrival_at,
            confidence, vehicle_id, thread_id, traffic_provider, traffic_status,
            traffic_delay_seconds, raw_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            yandex_snapshot_id,
            profile_key,
            sampled_at.isoformat(),
            report_window_key,
            source,
            source_method,
            predicted_minutes,
            (sampled_at + timedelta(minutes=predicted_minutes)).isoformat(),
            confidence,
            vehicle.vehicle_id if vehicle is not None else "",
            vehicle.thread_id if vehicle is not None else "",
            traffic.provider if traffic is not None else "none",
            traffic.status if traffic is not None else "not_collected",
            traffic.delay_seconds if traffic is not None else None,
            json.dumps(raw, ensure_ascii=False),
        ),
    )
    return int(cursor.lastrowid)


def _insert_ensemble_prediction_event(
    connection: sqlite3.Connection,
    prediction_event_ids: list[int],
) -> int | None:
    if not prediction_event_ids:
        return None
    placeholders = ",".join("?" for _ in prediction_event_ids)
    rows = connection.execute(
        f"""
        SELECT *
        FROM prediction_events
        WHERE id IN ({placeholders})
          AND source != ?
        """,
        (*prediction_event_ids, SOURCE_ENSEMBLE),
    ).fetchall()
    candidates = tuple(row for row in rows if str(row["source"]) in ENSEMBLE_SOURCE_PRIORITY)
    if not candidates:
        return None
    selected = _select_ensemble_candidate(connection, candidates)
    selected_safety_wait = _ensemble_candidate_safety_wait(connection, selected)
    raw = {
        "selected_prediction_event_id": int(selected["id"]),
        "selected_source": str(selected["source"]),
        "selection_policy": SELECTION_POLICY_NAME,
        "early_conflict_minutes": _ensemble_candidate_early_conflict_minutes(
            selected,
            safety_wait_minutes=selected_safety_wait,
        ),
        "candidates": [
            _ensemble_candidate_raw(connection, row)
            for row in sorted(candidates, key=_ensemble_candidate_display_sort_key)
        ],
    }
    cursor = connection.execute(
        """
        INSERT INTO prediction_events(
            yandex_snapshot_id, profile_key, sampled_at, report_window_key,
            source, source_method, predicted_minutes, predicted_arrival_at,
            confidence, vehicle_id, thread_id, traffic_provider, traffic_status,
            traffic_delay_seconds, raw_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            selected["yandex_snapshot_id"],
            selected["profile_key"],
            selected["sampled_at"],
            selected["report_window_key"],
            SOURCE_ENSEMBLE,
            selected["source_method"],
            selected["predicted_minutes"],
            selected["predicted_arrival_at"],
            selected["confidence"],
            selected["vehicle_id"],
            selected["thread_id"],
            selected["traffic_provider"],
            selected["traffic_status"],
            selected["traffic_delay_seconds"],
            json.dumps(raw, ensure_ascii=False),
        ),
    )
    return int(cursor.lastrowid)


def _select_ensemble_candidate(connection: sqlite3.Connection, candidates: tuple[sqlite3.Row, ...]) -> sqlite3.Row:
    rows_by_key = {str(row["id"]): row for row in candidates}
    selected_key = select_prediction_key(tuple(_ensemble_selection_candidate(connection, row) for row in candidates))
    return rows_by_key[selected_key]


def _ensemble_candidate_display_sort_key(row: sqlite3.Row) -> tuple[int, int, int]:
    return (
        ENSEMBLE_SOURCE_PRIORITY[str(row["source"])],
        int(row["predicted_minutes"]),
        int(row["id"]),
    )


def _ensemble_selection_candidate(connection: sqlite3.Connection, row: sqlite3.Row) -> PredictionSelectionCandidate:
    source = str(row["source"])
    safety_wait_minutes = _ensemble_candidate_safety_wait(connection, row)
    return prediction_selection_candidate_for_event_source(
        key=str(row["id"]),
        source=source,
        arrival_minutes=int(row["predicted_minutes"]),
        confidence=_eta_confidence(row["confidence"]),
        safety_wait_minutes=safety_wait_minutes,
    )


def _ensemble_candidate_raw(connection: sqlite3.Connection, row: sqlite3.Row) -> dict[str, object]:
    safety_wait_minutes = _ensemble_candidate_safety_wait(connection, row)
    selection_candidate = prediction_selection_candidate_for_event_source(
        key=str(row["id"]),
        source=str(row["source"]),
        arrival_minutes=int(row["predicted_minutes"]),
        confidence=_eta_confidence(row["confidence"]),
        safety_wait_minutes=safety_wait_minutes,
    )
    return {
        "prediction_event_id": int(row["id"]),
        "source": str(row["source"]),
        "predicted_minutes": int(row["predicted_minutes"]),
        "safety_wait_minutes": safety_wait_minutes,
        "early_conflict_minutes": selection_candidate.early_conflict_minutes,
        "quality_rank": selection_candidate.quality_rank,
        "confidence": str(row["confidence"]),
    }


def _ensemble_candidate_early_conflict_minutes(row: sqlite3.Row, *, safety_wait_minutes: int) -> int:
    return early_conflict_minutes_for_event_source(
        str(row["source"]),
        _eta_confidence(row["confidence"]),
        safety_wait_minutes=safety_wait_minutes,
    )


def _ensemble_candidate_safety_wait(connection: sqlite3.Connection, row: sqlite3.Row) -> int:
    source = str(row["source"])
    if source not in ENSEMBLE_SOURCE_PRIORITY:
        return 0
    reliability = load_source_reliability(
        connection,
        profile_key=str(row["profile_key"]),
        report_window_key=str(row["report_window_key"] or ""),
        source=source,
        predicted_minutes=int(row["predicted_minutes"]),
        current_time=datetime.fromisoformat(str(row["sampled_at"])),
    )
    return max(reliability.safety_buffer_minutes, _raw_live_evidence_safety_wait(row))


def _raw_live_evidence_safety_wait(row: sqlite3.Row) -> int:
    raw = _json_object(str(row["raw_json"] or "{}"))
    evidence = raw.get("live_evidence")
    if not isinstance(evidence, dict):
        return 0
    return _optional_int(evidence.get("safety_wait_minutes")) or 0


def _insert_arrival_event(
    connection: sqlite3.Connection,
    *,
    yandex_snapshot_id: int,
    profile_key: str,
    vehicle: YandexVehicle | None,
    stop_id: str,
    arrived_at: datetime,
    source: str,
    confidence: str,
    raw: dict[str, object],
) -> ArrivalEvent | None:
    vehicle_id = vehicle.vehicle_id if vehicle is not None else ""
    if _has_recent_arrival(connection, profile_key, vehicle_id, source, arrived_at):
        return None
    cursor = connection.execute(
        """
        INSERT INTO arrival_events(
            yandex_snapshot_id, profile_key, vehicle_id, thread_id, stop_id,
            arrived_at, source, confidence, lat, lng, raw_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            yandex_snapshot_id,
            profile_key,
            vehicle_id,
            vehicle.thread_id if vehicle is not None else "",
            stop_id,
            arrived_at.isoformat(),
            source,
            confidence,
            vehicle.lat if vehicle is not None else None,
            vehicle.lng if vehicle is not None else None,
            json.dumps(raw, ensure_ascii=False),
        ),
    )
    return ArrivalEvent(
        id=int(cursor.lastrowid),
        profile_key=profile_key,
        vehicle_id=vehicle_id,
        thread_id=vehicle.thread_id if vehicle is not None else "",
        stop_id=stop_id,
        arrived_at=arrived_at,
        source=source,
        confidence=confidence,
    )


def _has_recent_arrival(
    connection: sqlite3.Connection,
    profile_key: str,
    vehicle_id: str,
    source: str,
    arrived_at: datetime,
) -> bool:
    since = arrived_at - timedelta(minutes=ARRIVAL_DEDUPE_MINUTES)
    until = arrived_at + timedelta(minutes=ARRIVAL_DEDUPE_MINUTES)
    if vehicle_id:
        row = connection.execute(
            """
            SELECT 1
            FROM arrival_events
            WHERE profile_key = ?
              AND vehicle_id = ?
              AND arrived_at BETWEEN ? AND ?
            LIMIT 1
            """,
            (profile_key, vehicle_id, since.isoformat(), until.isoformat()),
        ).fetchone()
        return row is not None

    row = connection.execute(
        """
        SELECT 1
        FROM arrival_events
        WHERE profile_key = ?
          AND vehicle_id = ?
          AND source = ?
          AND arrived_at BETWEEN ? AND ?
        LIMIT 1
        """,
        (profile_key, vehicle_id, source, since.isoformat(), until.isoformat()),
    ).fetchone()
    return row is not None


def _has_prior_prediction(
    connection: sqlite3.Connection,
    profile_key: str,
    vehicle_id: str,
    arrived_at: datetime,
) -> bool:
    since = arrived_at - timedelta(minutes=PREDICTION_MATCH_MINUTES)
    filters = [
        "profile_key = ?",
        "sampled_at >= ?",
        "sampled_at < ?",
        "source = ?",
    ]
    params: list[object] = [
        profile_key,
        since.isoformat(),
        arrived_at.isoformat(),
        SOURCE_TARGET_STOP_LIVE,
    ]
    if vehicle_id:
        filters.append("(vehicle_id = '' OR vehicle_id = ?)")
        params.append(vehicle_id)
    else:
        filters.append("vehicle_id = ''")
    row = connection.execute(
        f"""
        SELECT 1
        FROM prediction_events
        WHERE {" AND ".join(filters)}
        LIMIT 1
        """,
        tuple(params),
    ).fetchone()
    return row is not None


def _prediction_raw(
    forecast: YandexLiveForecast,
    vehicle: YandexVehicle | None,
    correction: ResidualCorrection | None,
    evidence: LiveEtaEvidenceAdjustment,
) -> dict[str, object]:
    return {
        "forecast": {
            "arrival_minutes": list(forecast.arrival_minutes),
            "confidence": forecast.confidence.value,
            "fallback_reason": forecast.fallback_reason,
        },
        "vehicle": asdict(vehicle) if vehicle is not None else None,
        "correction": asdict(correction) if correction is not None else None,
        "live_evidence": asdict(evidence) if evidence.applied else None,
    }


def _arrival_from_row(row: sqlite3.Row) -> ArrivalEvent | None:
    arrived_at = _optional_datetime(row["arrived_at"])
    if arrived_at is None:
        return None
    return ArrivalEvent(
        id=int(row["id"]),
        profile_key=str(row["profile_key"]),
        vehicle_id=str(row["vehicle_id"]),
        thread_id=str(row["thread_id"]),
        stop_id=str(row["stop_id"]),
        arrived_at=arrived_at,
        source=str(row["source"]),
        confidence=str(row["confidence"]),
    )


def _prediction_from_row(row: sqlite3.Row) -> PredictionEvent:
    return PredictionEvent(
        id=int(row["id"]),
        profile_key=str(row["profile_key"]),
        sampled_at=datetime.fromisoformat(str(row["sampled_at"])),
        report_window_key=str(row["report_window_key"]),
        source=str(row["source"]),
        predicted_minutes=int(row["predicted_minutes"]),
        vehicle_id=str(row["vehicle_id"]),
        thread_id=str(row["thread_id"]),
    )


def _optional_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _target_stop_id(profile_key: str) -> str:
    return STOP_ID_BY_PROFILE.get(profile_key, STOP_ID_BY_PROFILE["morning"])


def _distance_to_target_stop(profile_key: str, lat: float, lng: float) -> float | None:
    points = ROUTE_TRAFFIC_POINTS_BY_PROFILE.get(profile_key)
    if not points:
        return None
    target = points[0]
    return haversine_meters(lat, lng, target.lat, target.lng)


def _vehicle_has_wrong_route_thread(vehicle: YandexVehicle, geometry: RouteGeometry) -> bool:
    if not geometry.thread_id:
        return False
    return bool(vehicle.thread_id and vehicle.thread_id != geometry.thread_id)


def _coordinate_arrival_route_evidence(
    connection: sqlite3.Connection,
    profile_key: str,
    vehicle: YandexVehicle,
    sampled_at: datetime,
    geometry: RouteGeometry | None,
) -> str:
    if geometry is None or not geometry.thread_id:
        return ""
    if vehicle.thread_id == geometry.thread_id:
        return "direct_thread"
    if vehicle.thread_id:
        return ""
    if not vehicle.vehicle_id:
        return ""
    if _previous_vehicle_positions(
        connection,
        profile_key,
        vehicle.vehicle_id,
        sampled_at,
        route_thread_id=geometry.thread_id,
    ):
        return "recovered_previous_thread"
    return ""


def _coordinate_arrival_route_match(
    connection: sqlite3.Connection,
    profile_key: str,
    vehicle: YandexVehicle,
    sampled_at: datetime,
    geometry: RouteGeometry | None,
) -> _CoordinateArrivalRouteMatch | None:
    if geometry is None:
        return None
    evidence = _coordinate_arrival_route_evidence(connection, profile_key, vehicle, sampled_at, geometry)
    if not evidence or vehicle.lat is None or vehicle.lng is None:
        return None
    projection = route_projection_for_point(geometry.points, geometry.measures, vehicle.lat, vehicle.lng)
    if projection is None or projection.distance_meters > COORDINATE_ARRIVAL_MAX_ROUTE_SNAP_METERS:
        return None
    target_delta = abs(projection.measure - geometry.target_measure)
    if target_delta > COORDINATE_ARRIVAL_MAX_TARGET_ROUTE_DELTA_METERS:
        return None
    progress = _coordinate_arrival_progress_meters(connection, profile_key, vehicle, sampled_at, geometry, projection)
    if progress is not None and progress < -COORDINATE_ARRIVAL_MAX_BACKTRACK_METERS:
        return None
    return _CoordinateArrivalRouteMatch(evidence, projection.distance_meters, target_delta, progress)


def _coordinate_arrival_progress_meters(
    connection: sqlite3.Connection,
    profile_key: str,
    vehicle: YandexVehicle,
    sampled_at: datetime,
    geometry: RouteGeometry,
    current_projection: RouteProjection,
) -> float | None:
    if not vehicle.vehicle_id:
        return None
    for previous in _previous_vehicle_positions(
        connection,
        profile_key,
        vehicle.vehicle_id,
        sampled_at,
        route_thread_id=geometry.thread_id,
    ):
        previous_projection = route_projection_for_point(geometry.points, geometry.measures, previous.lat, previous.lng)
        if previous_projection is None:
            continue
        if previous_projection.distance_meters > VEHICLE_PROGRESS_MAX_ROUTE_SNAP_METERS:
            continue
        return current_projection.measure - previous_projection.measure
    return None


def _previous_vehicle_positions(
    connection: sqlite3.Connection,
    profile_key: str,
    vehicle_id: str,
    sampled_at: datetime,
    *,
    route_thread_id: str,
) -> tuple[VehiclePosition, ...]:
    return previous_vehicle_positions(
        connection,
        profile_key,
        vehicle_id,
        sampled_at,
        route_thread_id=route_thread_id,
        lookback_minutes=VEHICLE_PROGRESS_LOOKBACK_MINUTES,
        max_age_seconds=FRESH_VEHICLE_MAX_AGE_SECONDS,
        limit=VEHICLE_PROGRESS_SPEED_SAMPLE_LIMIT,
    )


def _count_arrivals(connection: sqlite3.Connection, profile_key: str, report_window_key: str) -> int:
    row = connection.execute(
        """
        SELECT COUNT(*) AS count
        FROM arrival_events
        JOIN yandex_forecast_samples ON yandex_forecast_samples.yandex_snapshot_id = arrival_events.yandex_snapshot_id
        WHERE arrival_events.profile_key = ?
          AND yandex_forecast_samples.report_window_key = ?
        """,
        (profile_key, report_window_key),
    ).fetchone()
    return int(row["count"])


def _count_predictions(connection: sqlite3.Connection, profile_key: str, report_window_key: str) -> int:
    row = connection.execute(
        """
        SELECT COUNT(*) AS count
        FROM prediction_events
        WHERE profile_key = ? AND report_window_key = ?
        """,
        (profile_key, report_window_key),
    ).fetchone()
    return int(row["count"])


def _count_evaluations(connection: sqlite3.Connection, profile_key: str, report_window_key: str) -> int:
    row = connection.execute(
        """
        SELECT COUNT(*) AS count
        FROM prediction_evaluations
        JOIN prediction_events ON prediction_events.id = prediction_evaluations.prediction_event_id
        WHERE prediction_evaluations.profile_key = ?
          AND prediction_events.report_window_key = ?
        """,
        (profile_key, report_window_key),
    ).fetchone()
    return int(row["count"])


def _latest_arrival_at(connection: sqlite3.Connection, profile_key: str, report_window_key: str) -> datetime | None:
    rows = connection.execute(
        """
        SELECT arrival_events.arrived_at AS value
        FROM arrival_events
        JOIN yandex_forecast_samples ON yandex_forecast_samples.yandex_snapshot_id = arrival_events.yandex_snapshot_id
        WHERE arrival_events.profile_key = ?
          AND yandex_forecast_samples.report_window_key = ?
        ORDER BY arrival_events.arrived_at DESC
        """,
        (profile_key, report_window_key),
    ).fetchall()
    return _latest_datetime(rows)


def _latest_prediction_at(connection: sqlite3.Connection, profile_key: str, report_window_key: str) -> datetime | None:
    rows = connection.execute(
        """
        SELECT sampled_at AS value
        FROM prediction_events
        WHERE profile_key = ? AND report_window_key = ?
        ORDER BY sampled_at DESC
        """,
        (profile_key, report_window_key),
    ).fetchall()
    return _latest_datetime(rows)


def _latest_datetime(rows: Iterable[sqlite3.Row]) -> datetime | None:
    for row in rows:
        value = _optional_datetime(row["value"])
        if value is not None:
            return value
    return None


def _source_summaries(
    connection: sqlite3.Connection,
    profile_key: str,
    report_window_key: str,
) -> tuple[PredictionLabSourceSummary, ...]:
    rows = connection.execute(
        """
        SELECT
            prediction_evaluations.source,
            COUNT(*) AS count,
            SUM(CASE WHEN error_minutes < 0 THEN 1 ELSE 0 END) AS misses,
            SUM(CASE WHEN error_minutes < 0 THEN ABS(error_minutes) ELSE 0 END) AS miss_minutes,
            SUM(CASE WHEN error_minutes > 0 THEN error_minutes ELSE 0 END) AS extra_wait_minutes,
            AVG(ABS(error_minutes)) AS mae
        FROM prediction_evaluations
        JOIN prediction_events ON prediction_events.id = prediction_evaluations.prediction_event_id
        WHERE prediction_evaluations.profile_key = ?
          AND prediction_events.report_window_key = ?
        GROUP BY prediction_evaluations.source
        ORDER BY prediction_evaluations.source
        """,
        (profile_key, report_window_key),
    ).fetchall()
    return tuple(
        PredictionLabSourceSummary(
            source=str(row["source"]),
            evaluated_predictions=int(row["count"]),
            miss_cases=int(row["misses"] or 0),
            miss_minutes=int(row["miss_minutes"] or 0),
            extra_wait_minutes=int(row["extra_wait_minutes"] or 0),
            mean_absolute_error=float(row["mae"] or 0),
        )
        for row in rows
    )


def _calibration_buckets(
    connection: sqlite3.Connection,
    profile_key: str,
    report_window_key: str,
    *,
    current_time: datetime,
    reliability_min_samples: int,
    runtime_reliability_min_samples: int,
    residual_min_samples: int,
) -> tuple[PredictionLabCalibrationBucket, ...]:
    grouped_errors = _calibration_error_values(
        connection,
        profile_key,
        report_window_key,
        current_time=current_time,
    )
    buckets = []
    for (source, bucket), values in sorted(grouped_errors.items()):
        representative_minutes = _representative_minutes_for_bucket_label(bucket)
        if representative_minutes is None:
            continue
        reliability = load_source_reliability(
            connection,
            profile_key=profile_key,
            report_window_key=report_window_key,
            source=source,
            predicted_minutes=representative_minutes,
            min_samples=reliability_min_samples,
            current_time=current_time,
        )
        runtime_reliability = load_source_reliability(
            connection,
            profile_key=profile_key,
            report_window_key=report_window_key,
            source=source,
            predicted_minutes=representative_minutes,
            min_samples=runtime_reliability_min_samples,
            current_time=current_time,
            runtime_source=RUNTIME_SOURCE_WEB_APP,
        )
        residual_correction = None
        if source == SOURCE_TARGET_STOP_LIVE:
            residual_correction = load_residual_correction(
                connection,
                profile_key=profile_key,
                report_window_key=report_window_key,
                predicted_minutes=representative_minutes,
                min_samples=residual_min_samples,
                current_time=current_time,
            )
        buckets.append(
            PredictionLabCalibrationBucket(
                source=source,
                bucket=bucket,
                evaluated_predictions=len(values),
                miss_cases=sum(1 for value in values if value < 0),
                miss_rate_percent=round(sum(1 for value in values if value < 0) * 100 / len(values)),
                p10_error_minutes=_percentile(values, 10),
                reliability=reliability,
                runtime_reliability=runtime_reliability,
                residual_correction=residual_correction,
            )
        )
    return tuple(buckets)


def _calibration_error_values(
    connection: sqlite3.Connection,
    profile_key: str,
    report_window_key: str,
    *,
    current_time: datetime,
) -> dict[tuple[str, str], tuple[int, ...]]:
    since = current_time - timedelta(days=PREDICTION_ERROR_MAX_AGE_DAYS)
    arrival_confidences = _arrival_confidences_at_or_above(EtaConfidence.MEDIUM)
    rows = connection.execute(
        """
        SELECT
            prediction_evaluations.source,
            prediction_evaluations.bucket,
            prediction_evaluations.error_minutes
        FROM prediction_evaluations
        JOIN prediction_events ON prediction_events.id = prediction_evaluations.prediction_event_id
        JOIN arrival_events ON arrival_events.id = prediction_evaluations.arrival_event_id
        WHERE prediction_evaluations.profile_key = ?
          AND prediction_events.report_window_key = ?
          AND prediction_events.sampled_at >= ?
          AND prediction_events.sampled_at < ?
          AND arrival_events.confidence IN ({})
        ORDER BY prediction_evaluations.source, prediction_evaluations.bucket
        """.format(",".join("?" for _ in arrival_confidences)),
        (
            profile_key,
            report_window_key,
            since.isoformat(),
            current_time.isoformat(),
            *arrival_confidences,
        ),
    ).fetchall()
    grouped: dict[tuple[str, str], list[int]] = {}
    for row in rows:
        key = (str(row["source"]), str(row["bucket"]))
        grouped.setdefault(key, []).append(int(row["error_minutes"]))
    return {key: tuple(values) for key, values in grouped.items()}


def _representative_minutes_for_bucket_label(bucket_label: str) -> int | None:
    previous_max_minutes = -1
    for bucket in PREDICTION_ETA_BUCKETS:
        lower_bound_minutes = previous_max_minutes + 1
        if bucket.label == bucket_label:
            return lower_bound_minutes
        if bucket.max_minutes is None:
            break
        previous_max_minutes = bucket.max_minutes
    return None


def _percentile(values: tuple[int, ...], percentile: int) -> int:
    ordered = sorted(values)
    index = max(0, ceil(percentile / 100 * len(ordered)) - 1)
    return ordered[index]


def _median_float(values: tuple[float, ...]) -> float:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def _clamp_float(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


def _error_values(
    connection: sqlite3.Connection,
    profile_key: str,
    report_window_key: str | None,
    source: str,
    bucket: str | None,
    *,
    current_time: datetime,
    min_arrival_confidence: EtaConfidence,
    runtime_source: str | None = None,
) -> tuple[int, ...]:
    samples = _error_samples(
        connection,
        profile_key,
        report_window_key,
        source,
        bucket,
        current_time=current_time,
        min_arrival_confidence=min_arrival_confidence,
        runtime_source=runtime_source,
    )
    return tuple(sample.error_minutes for sample in samples)


def _error_samples(
    connection: sqlite3.Connection,
    profile_key: str,
    report_window_key: str | None,
    source: str,
    bucket: str | None,
    *,
    current_time: datetime,
    min_arrival_confidence: EtaConfidence,
    runtime_source: str | None = None,
) -> tuple[PredictionErrorSample, ...]:
    since = current_time - timedelta(days=PREDICTION_ERROR_MAX_AGE_DAYS)
    arrival_confidences = _arrival_confidences_at_or_above(min_arrival_confidence)
    filters = [
        "prediction_evaluations.profile_key = ?",
        "prediction_evaluations.source = ?",
        "prediction_events.sampled_at >= ?",
        "prediction_events.sampled_at < ?",
        "arrival_events.confidence IN ({})".format(",".join("?" for _ in arrival_confidences)),
    ]
    params: list[object] = [
        profile_key,
        source,
        since.isoformat(),
        current_time.isoformat(),
        *arrival_confidences,
    ]
    if bucket is not None:
        filters.append("prediction_evaluations.bucket = ?")
        params.append(bucket)
    if report_window_key:
        filters.append("prediction_events.report_window_key = ?")
        params.append(report_window_key)
    if runtime_source is not None:
        filters.append("prediction_events.runtime_source = ?")
        params.append(runtime_source)
    rows = connection.execute(
        f"""
        SELECT prediction_evaluations.error_minutes, arrival_events.confidence AS arrival_confidence
        FROM prediction_evaluations
        JOIN prediction_events ON prediction_events.id = prediction_evaluations.prediction_event_id
        JOIN arrival_events ON arrival_events.id = prediction_evaluations.arrival_event_id
        WHERE {" AND ".join(filters)}
        ORDER BY prediction_evaluations.evaluated_at DESC
        LIMIT 500
        """,
        tuple(params),
    ).fetchall()
    return tuple(
        PredictionErrorSample(
            error_minutes=int(row["error_minutes"]),
            arrival_confidence=_eta_confidence(row["arrival_confidence"]),
        )
        for row in rows
    )


def _arrival_confidences_at_or_above(confidence: EtaConfidence) -> tuple[str, ...]:
    if confidence == EtaConfidence.HIGH:
        return (EtaConfidence.HIGH.value,)
    if confidence == EtaConfidence.MEDIUM:
        return (EtaConfidence.HIGH.value, EtaConfidence.MEDIUM.value)
    if confidence == EtaConfidence.LOW:
        return (
            EtaConfidence.HIGH.value,
            EtaConfidence.MEDIUM.value,
            EtaConfidence.LOW.value,
        )
    return tuple(item.value for item in EtaConfidence)


def _source_safety_buffer(values: tuple[int, ...], *, min_samples: int) -> int:
    if len(values) < min_samples:
        return 0
    misses = sum(1 for value in values if value < 0)
    miss_rate = round(misses * 100 / len(values))
    p10 = _percentile(values, 10)
    if p10 >= 0 or miss_rate < SOURCE_RELIABILITY_MIN_MISS_RATE_PERCENT:
        return 0
    return min(
        SOURCE_RELIABILITY_MAX_BUFFER_MINUTES,
        max(abs(p10), _source_miss_rate_buffer_floor(miss_rate)),
    )


def _source_miss_rate_buffer_floor(miss_rate_percent: int) -> int:
    return source_risk_buffer_floor_minutes(miss_rate_percent)


def _reliability_scope(scope: str, runtime_source: str | None) -> str:
    if runtime_source == RUNTIME_SOURCE_WEB_APP:
        return f"bot_runtime_{scope}"
    if runtime_source:
        return f"runtime_{scope}"
    return scope
