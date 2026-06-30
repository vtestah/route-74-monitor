from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from route74.cli.prediction_lab import format_prediction_lab_calibration
from route74.domain.eta import EtaConfidence, EtaFactorKind, EtaSource
from route74.domain.prediction_buckets import (
    PredictionEtaBucket,
    prediction_bucket_tolerance,
    validate_prediction_eta_buckets,
)
from route74.domain.prediction_consensus import (
    PredictionCandidate,
    build_prediction_consensus,
    early_conflict_minutes_for_event_source,
    prediction_selection_candidate_for_event_source,
    select_prediction_candidate,
)
from route74.domain.prediction_sources import (
    EVENT_SOURCE_BY_ETA_SOURCE,
    EVENT_SOURCE_PRIORITY,
    EVALUATED_EVENT_SOURCES,
)
from route74.domain.profiles import MORNING
from route74.domain.runtime_sources import RUNTIME_SOURCE_WEB_APP
from route74.models import NOVOSIBIRSK_TZ
from route74.services.prediction_engine import PredictionEngine
from route74.sources.yandex.cache import CachedYandexForecastSource
from route74.sources.yandex.models import (
    YandexLiveForecast,
    YandexSourceMethod,
    YandexSourceStatus,
    YandexVehicle,
)
from route74.storage import connect, init_db, insert_yandex_snapshot
from route74.storage.eta_quality import sanitize_untrusted_eta
from route74.storage.prediction_lab import (
    ENSEMBLE_SOURCE_PRIORITY,
    EVALUATED_SOURCES,
    SOURCE_ENSEMBLE,
    SOURCE_HISTORY_HEADWAY,
    SOURCE_TARGET_STOP_LIVE,
    SOURCE_VEHICLE_PROGRESS,
    _insert_ensemble_prediction_event,
    backfill_prediction_lab,
    count_arrival_events,
    count_prediction_evaluations,
    count_prediction_events,
    evaluate_pending_predictions,
    load_arrival_events,
    load_residual_correction,
    load_source_reliability,
    prediction_bucket,
    summarize_prediction_lab_calibration,
    summarize_prediction_lab_window,
)
from route74.domain.yandex_history import YandexHistoryPrediction


def main() -> None:
    current_time = datetime(2026, 6, 4, 9, 0, tzinfo=NOVOSIBIRSK_TZ)
    _assert_prediction_policy_prefers_materially_earlier_progress()
    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "prediction-lab.sqlite"
        _assert_trusted_eta_arrival(db_path, current_time)
        _assert_coordinate_arrival_without_trusted_prediction(db_path, current_time + timedelta(minutes=20))
        _assert_coordinate_arrival_dedupes_trusted_eta(db_path, current_time + timedelta(minutes=25))
        _assert_coordinate_arrival_rejects_wrong_route_thread(db_path, current_time + timedelta(minutes=26))
        _assert_coordinate_arrival_requires_route_evidence(db_path, current_time + timedelta(minutes=27))
        _assert_coordinate_arrival_recovers_missing_thread(db_path, current_time + timedelta(minutes=28))
        _assert_coordinate_arrival_rejects_bad_route_snap(db_path, current_time + timedelta(minutes=29))
        _assert_browser_eta_is_not_trusted(db_path, current_time + timedelta(minutes=30))
        _assert_trusted_live_not_overridden_by_early_history(db_path, current_time + timedelta(minutes=40))
        _assert_low_confidence_live_keeps_more_wait_buffer(db_path, current_time + timedelta(minutes=44))
        _assert_thread_fallback_eta_is_tentative(db_path, current_time + timedelta(minutes=44, seconds=30))
        _assert_short_stop_info_eta_without_coordinates_gets_buffer(
            db_path,
            current_time + timedelta(minutes=44, seconds=45),
        )
        _assert_stale_live_eta_falls_back_to_history(db_path, current_time + timedelta(minutes=45))
        _assert_stale_eta_does_not_create_prediction_or_arrival(db_path, current_time + timedelta(minutes=46))
        _assert_stale_coordinate_does_not_create_arrival(db_path, current_time + timedelta(minutes=47))
        _assert_ensemble_policy_prefers_materially_earlier_progress(db_path, current_time + timedelta(minutes=50))
        _assert_ensemble_policy_prefers_close_progress_when_live_confidence_is_low(
            db_path,
            current_time + timedelta(minutes=55),
        )
        _assert_ensemble_policy_prefers_progress_quality(db_path, current_time + timedelta(minutes=57))
        _assert_vehicle_progress_requires_route_thread(db_path, current_time + timedelta(minutes=60))
        _assert_vehicle_progress_prefers_quality_unless_materially_earlier(
            db_path,
            current_time + timedelta(minutes=65),
        )
        _assert_cached_coordinates_feed_vehicle_progress(db_path, current_time + timedelta(minutes=70))
        _assert_vehicle_progress_rejects_stale_route_geometry(db_path, current_time + timedelta(minutes=80))
        _assert_invalid_route_stop_coordinates_do_not_crash_progress(
            db_path,
            current_time + timedelta(minutes=85),
        )
        _assert_nonfinite_route_polyline_does_not_crash_progress(
            db_path,
            current_time + timedelta(minutes=86),
        )
        _assert_vehicle_progress_tracker_marks_stalled(db_path, current_time + timedelta(minutes=90))

    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "ensemble-safety.sqlite"
        _assert_ensemble_policy_uses_safety_adjusted_selection(db_path, current_time)

    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "prediction-lab-backfill.sqlite"
        _assert_prediction_lab_backfill(db_path, current_time)

    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "prediction-match.sqlite"
        _assert_prediction_evaluation_matches_vehicle_and_thread(db_path, current_time)

    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "malformed-latest-times.sqlite"
        _assert_prediction_lab_summary_ignores_malformed_latest_times(db_path, current_time)

    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "eta-quality.sqlite"
        _assert_sanitize_drops_unsafe_coordinate_arrivals(db_path, current_time)

    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "history-headway.sqlite"
        _assert_history_headway_uses_prior_samples(db_path, current_time + timedelta(hours=1))

    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "residual.sqlite"
        _seed_negative_residuals(db_path, current_time)
        with connect(db_path) as connection:
            init_db(connection)
            correction = load_residual_correction(
                connection,
                profile_key=MORNING.key,
                report_window_key="weekday_morning_09_12",
                predicted_minutes=10,
                min_samples=5,
                current_time=current_time,
            )
        _assert_equal(correction.correction_minutes, -3)

        buffered = PredictionEngine(db_path=db_path, residual_min_samples=999, reliability_min_samples=5).predict(
            profile=MORNING,
            current_time=current_time,
            yandex_forecast=_trusted_forecast(10),
            yandex_history=YandexHistoryPrediction.unavailable(),
        )
        _assert_equal(buffered.consensus.selected_source, EtaSource.YANDEX)
        _assert_equal(buffered.selected.safety_wait_minutes if buffered.selected else None, 3)
        _assert_equal(buffered.consensus.target_wait_minutes, 8)
        _assert_contains(buffered.consensus.warning, "добавил запас 3 мин")

        result = PredictionEngine(db_path=db_path, residual_min_samples=5).predict(
            profile=MORNING,
            current_time=current_time,
            yandex_forecast=_trusted_forecast(10),
            yandex_history=YandexHistoryPrediction.unavailable(),
        )
        _assert_equal(result.consensus.selected_source, EtaSource.YANDEX_CORRECTED)
        _assert_equal(result.consensus.arrival_minutes, 7)
        _assert_contains(result.consensus.warning, "ETA сдвинут на 3 мин раньше")

    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "residual-cap.sqlite"
        _assert_residual_correction_is_capped(db_path, current_time)

    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "stale-errors.sqlite"
        _assert_stale_prediction_errors_are_ignored(db_path, current_time)

    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "future-errors.sqlite"
        _assert_future_prediction_errors_are_ignored(db_path, current_time)

    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "reliability-fallback.sqlite"
        _assert_source_reliability_uses_source_scope_when_bucket_is_sparse(db_path, current_time)

    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "miss-rate-floor.sqlite"
        _assert_source_reliability_uses_miss_rate_floor(db_path, current_time)

    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "calibration.sqlite"
        _assert_prediction_lab_calibration_surfaces_runtime_guardrails(db_path, current_time)

    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "bot-runtime-miss-rate.sqlite"
        _assert_bot_runtime_reliability_surfaces_worse_miss_rate_without_buffer(db_path, current_time)

    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "bot-runtime-reliability.sqlite"
        _assert_bot_runtime_reliability_can_raise_buffer(db_path, current_time)

    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "residual-fallback.sqlite"
        _assert_residual_correction_uses_source_scope_when_bucket_is_sparse(db_path, current_time)

    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "medium-arrival-residual.sqlite"
        _assert_residual_correction_ignores_medium_arrival_facts(db_path, current_time)

    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "high-arrival-reliability.sqlite"
        _assert_source_reliability_prefers_high_confidence_arrivals(db_path, current_time)

    print("OK | prediction lab smoke passed")


def _assert_prediction_policy_prefers_materially_earlier_progress() -> None:
    _assert_equal(prediction_bucket(3), "0-3")
    _assert_equal(prediction_bucket(4), "3-6")
    _assert_equal(prediction_bucket(11), "10-15")
    _assert_equal(prediction_bucket(16), "15+")
    _assert_equal(prediction_bucket_tolerance(3), 1)
    _assert_equal(prediction_bucket_tolerance(16), 5)
    _assert_raises_value_error(lambda: prediction_bucket(-1), "non-negative minutes")
    _assert_raises_value_error(lambda: prediction_bucket_tolerance(-1), "non-negative minutes")
    _assert_raises_value_error(lambda: prediction_bucket(True), "integer")
    _assert_raises_value_error(lambda: prediction_bucket_tolerance(3.5), "integer")
    _assert_raises_value_error(
        lambda: PredictionEtaBucket(" ", max_minutes=3, accuracy_tolerance_minutes=1),
        "label",
    )
    _assert_raises_value_error(
        lambda: PredictionEtaBucket(123, max_minutes=3, accuracy_tolerance_minutes=1),  # type: ignore[arg-type]
        "label",
    )
    _assert_raises_value_error(lambda: validate_prediction_eta_buckets(()), "non-empty")
    _assert_raises_value_error(
        lambda: validate_prediction_eta_buckets(
            (
                PredictionEtaBucket("15+", max_minutes=None, accuracy_tolerance_minutes=5),
                PredictionEtaBucket("20", max_minutes=20, accuracy_tolerance_minutes=6),
            )
        ),
        "open-ended",
    )
    _assert_raises_value_error(
        lambda: validate_prediction_eta_buckets(
            (
                PredictionEtaBucket("0-3", max_minutes=3, accuracy_tolerance_minutes=1),
                PredictionEtaBucket("0-3", max_minutes=6, accuracy_tolerance_minutes=2),
                PredictionEtaBucket("15+", max_minutes=None, accuracy_tolerance_minutes=3),
            )
        ),
        "duplicate",
    )
    _assert_raises_value_error(
        lambda: validate_prediction_eta_buckets(
            (
                PredictionEtaBucket("0-3", max_minutes=3, accuracy_tolerance_minutes=1),
                PredictionEtaBucket("duplicate", max_minutes=3, accuracy_tolerance_minutes=2),
                PredictionEtaBucket("15+", max_minutes=None, accuracy_tolerance_minutes=3),
            )
        ),
        "increase",
    )
    _assert_raises_value_error(
        lambda: validate_prediction_eta_buckets(
            (
                PredictionEtaBucket("0-3", max_minutes=3, accuracy_tolerance_minutes=2),
                PredictionEtaBucket("3-6", max_minutes=6, accuracy_tolerance_minutes=1),
                PredictionEtaBucket("15+", max_minutes=None, accuracy_tolerance_minutes=3),
            )
        ),
        "tolerance",
    )
    _assert_raises_value_error(
        lambda: validate_prediction_eta_buckets(
            (PredictionEtaBucket("0-3", max_minutes=3, accuracy_tolerance_minutes=1),)
        ),
        "open-ended",
    )

    _assert_equal(EVENT_SOURCE_BY_ETA_SOURCE[EtaSource.YANDEX], SOURCE_TARGET_STOP_LIVE)
    _assert_equal(EVALUATED_SOURCES, EVALUATED_EVENT_SOURCES)
    _assert_equal(ENSEMBLE_SOURCE_PRIORITY, EVENT_SOURCE_PRIORITY)
    _assert_equal(
        early_conflict_minutes_for_event_source(
            SOURCE_TARGET_STOP_LIVE,
            EtaConfidence.LOW,
            safety_wait_minutes=0,
        ),
        1,
    )
    _assert_equal(
        early_conflict_minutes_for_event_source(
            SOURCE_VEHICLE_PROGRESS,
            EtaConfidence.LOW,
            safety_wait_minutes=0,
        ),
        3,
    )
    selection_candidate = prediction_selection_candidate_for_event_source(
        key="live",
        source=SOURCE_TARGET_STOP_LIVE,
        arrival_minutes=8,
        confidence=EtaConfidence.LOW,
    )
    _assert_equal(selection_candidate.priority, EVENT_SOURCE_PRIORITY[SOURCE_TARGET_STOP_LIVE])
    _assert_equal(selection_candidate.early_conflict_minutes, 1)
    progress_selection_candidate = prediction_selection_candidate_for_event_source(
        key="progress",
        source=SOURCE_VEHICLE_PROGRESS,
        arrival_minutes=7,
        confidence=EtaConfidence.LOW,
    )
    _assert_equal(progress_selection_candidate.quality_rank, 1)

    selected, consensus = build_prediction_consensus(
        (
            PredictionCandidate(
                EtaSource.YANDEX,
                10,
                EtaConfidence.HIGH,
                safety_wait_minutes=3,
                reliability_scope="source",
            ),
        )
    )
    _assert_equal(selected.source, EtaSource.YANDEX)
    _assert_equal(consensus.confidence, EtaConfidence.LOW)
    _assert_equal(consensus.target_wait_minutes, 8)
    _assert_contains(consensus.warning, "по общей статистике источника Яндекс live добавил запас 3 мин")

    selected, consensus = build_prediction_consensus(
        (PredictionCandidate(EtaSource.VEHICLE_PROGRESS, 10, EtaConfidence.LOW),)
    )
    _assert_equal(selected.source, EtaSource.VEHICLE_PROGRESS)
    _assert_equal(consensus.target_wait_minutes, 7)
    _assert_contains(consensus.warning, "координатный прогноз, держу запас 2 мин")

    _selected, medium_progress = build_prediction_consensus(
        (PredictionCandidate(EtaSource.VEHICLE_PROGRESS, 10, EtaConfidence.MEDIUM),)
    )
    _assert_equal(medium_progress.target_wait_minutes, 6)
    _assert_contains(medium_progress.warning, "координатный прогноз, держу запас 1 мин")

    selected, high_live_with_weak_progress = build_prediction_consensus(
        (
            PredictionCandidate(EtaSource.YANDEX, 8, EtaConfidence.HIGH),
            PredictionCandidate(EtaSource.VEHICLE_PROGRESS, 20, EtaConfidence.LOW),
        )
    )
    _assert_equal(selected.source, EtaSource.YANDEX)
    _assert_equal(high_live_with_weak_progress.confidence, EtaConfidence.HIGH)
    _assert_equal(high_live_with_weak_progress.target_wait_minutes, 2)
    _assert_equal(high_live_with_weak_progress.spread_minutes, None)

    selected = select_prediction_candidate(
        (
            PredictionCandidate(EtaSource.YANDEX, 12, EtaConfidence.HIGH),
            PredictionCandidate(EtaSource.VEHICLE_PROGRESS, 4, EtaConfidence.LOW),
        )
    )
    _assert_equal(selected.source, EtaSource.VEHICLE_PROGRESS)

    close_conflict = select_prediction_candidate(
        (
            PredictionCandidate(EtaSource.YANDEX, 8, EtaConfidence.HIGH),
            PredictionCandidate(EtaSource.VEHICLE_PROGRESS, 6, EtaConfidence.LOW),
        )
    )
    _assert_equal(close_conflict.source, EtaSource.YANDEX)

    low_confidence_close_progress = select_prediction_candidate(
        (
            PredictionCandidate(EtaSource.YANDEX, 8, EtaConfidence.LOW),
            PredictionCandidate(EtaSource.VEHICLE_PROGRESS, 7, EtaConfidence.LOW),
        )
    )
    _assert_equal(low_confidence_close_progress.source, EtaSource.VEHICLE_PROGRESS)

    medium_confidence_close_progress = select_prediction_candidate(
        (
            PredictionCandidate(EtaSource.YANDEX, 8, EtaConfidence.MEDIUM),
            PredictionCandidate(EtaSource.VEHICLE_PROGRESS, 7, EtaConfidence.LOW),
        )
    )
    _assert_equal(medium_confidence_close_progress.source, EtaSource.YANDEX)

    _selected_medium_live, medium_live_with_weak_progress = build_prediction_consensus(
        (
            PredictionCandidate(EtaSource.YANDEX, 8, EtaConfidence.MEDIUM),
            PredictionCandidate(EtaSource.VEHICLE_PROGRESS, 7, EtaConfidence.LOW),
        )
    )
    _assert_equal(medium_live_with_weak_progress.spread_minutes, None)
    _assert_equal(
        tuple(factor.kind for factor in medium_live_with_weak_progress.factors),
        (EtaFactorKind.IGNORED_WEAK_PROGRESS,),
    )
    _assert_equal(medium_live_with_weak_progress.factors[0].minutes, 1)

    history_conflict = select_prediction_candidate(
        (
            PredictionCandidate(EtaSource.YANDEX, 12, EtaConfidence.HIGH),
            PredictionCandidate(EtaSource.YANDEX_HISTORY, 4, EtaConfidence.LOW),
        )
    )
    _assert_equal(history_conflict.source, EtaSource.YANDEX)

    buffered_live_conflict = select_prediction_candidate(
        (
            PredictionCandidate(EtaSource.YANDEX, 10, EtaConfidence.MEDIUM, safety_wait_minutes=3),
            PredictionCandidate(EtaSource.VEHICLE_PROGRESS, 7, EtaConfidence.LOW),
        )
    )
    _assert_equal(buffered_live_conflict.source, EtaSource.VEHICLE_PROGRESS)

    materially_earlier_progress = select_prediction_candidate(
        (
            PredictionCandidate(EtaSource.YANDEX, 10, EtaConfidence.MEDIUM, safety_wait_minutes=3),
            PredictionCandidate(EtaSource.VEHICLE_PROGRESS, 4, EtaConfidence.LOW),
        )
    )
    _assert_equal(materially_earlier_progress.source, EtaSource.VEHICLE_PROGRESS)

    higher_quality_progress = select_prediction_candidate(
        (
            PredictionCandidate(EtaSource.VEHICLE_PROGRESS, 4, EtaConfidence.LOW),
            PredictionCandidate(EtaSource.VEHICLE_PROGRESS, 5, EtaConfidence.MEDIUM),
        )
    )
    _assert_equal(higher_quality_progress.arrival_minutes, 5)

    materially_earlier_low_quality_progress = select_prediction_candidate(
        (
            PredictionCandidate(EtaSource.VEHICLE_PROGRESS, 1, EtaConfidence.LOW),
            PredictionCandidate(EtaSource.VEHICLE_PROGRESS, 5, EtaConfidence.MEDIUM),
        )
    )
    _assert_equal(materially_earlier_low_quality_progress.arrival_minutes, 1)


def _assert_trusted_eta_arrival(db_path: Path, base: datetime) -> None:
    with connect(db_path) as connection:
        init_db(connection)
        insert_yandex_snapshot(connection, MORNING.key, _trusted_forecast(1), base)
        insert_yandex_snapshot(connection, MORNING.key, _trusted_forecast(0), base + timedelta(minutes=1))
        _assert_equal(count_arrival_events(connection), 1)
        _assert_equal(count_prediction_events(connection), 4)
        _assert_equal(count_prediction_evaluations(connection), 2)
        rows = connection.execute(
            """
            SELECT source, actual_minutes, predicted_minutes, error_minutes
            FROM prediction_evaluations
            ORDER BY source
            """
        ).fetchall()
    _assert_equal(
        tuple(tuple(row) for row in rows),
        (
            (SOURCE_ENSEMBLE, 1, 1, 0),
            (SOURCE_TARGET_STOP_LIVE, 1, 1, 0),
        ),
    )


def _assert_coordinate_arrival_without_trusted_prediction(db_path: Path, sampled_at: datetime) -> None:
    with connect(db_path) as connection:
        init_db(connection)
        _seed_coordinate_arrival_geometry(connection, sampled_at)
        before_predictions = count_prediction_events(connection)
        insert_yandex_snapshot(connection, MORNING.key, _coordinate_forecast_near_stop(), sampled_at)
        _assert_equal(count_arrival_events(connection), 2)
        _assert_equal(count_prediction_events(connection), before_predictions)


def _assert_coordinate_arrival_dedupes_trusted_eta(db_path: Path, sampled_at: datetime) -> None:
    vehicle_id = "dedupe-arrival"
    with connect(db_path) as connection:
        init_db(connection)
        before_arrivals = count_arrival_events(connection)
        insert_yandex_snapshot(connection, MORNING.key, _trusted_forecast(0, vehicle_id=vehicle_id), sampled_at)
        after_trusted_eta = count_arrival_events(connection)
        _assert_equal(after_trusted_eta, before_arrivals + 1)

        before_predictions = count_prediction_events(connection)
        insert_yandex_snapshot(
            connection,
            MORNING.key,
            _coordinate_forecast_near_stop(vehicle_id=vehicle_id),
            sampled_at + timedelta(seconds=45),
        )
        _assert_equal(count_arrival_events(connection), after_trusted_eta)
        _assert_equal(count_prediction_events(connection), before_predictions)


def _assert_coordinate_arrival_rejects_wrong_route_thread(db_path: Path, sampled_at: datetime) -> None:
    with connect(db_path) as connection:
        init_db(connection)
        _seed_coordinate_arrival_geometry(connection, sampled_at)
        before_arrivals = count_arrival_events(connection)
        before_predictions = count_prediction_events(connection)
        insert_yandex_snapshot(
            connection,
            MORNING.key,
            _coordinate_forecast_near_stop(vehicle_id="wrong-thread-near-stop", thread_id="2161326764"),
            sampled_at,
        )
        _assert_equal(count_arrival_events(connection), before_arrivals)
        _assert_equal(count_prediction_events(connection), before_predictions)


def _assert_coordinate_arrival_requires_route_evidence(db_path: Path, sampled_at: datetime) -> None:
    with connect(db_path) as connection:
        init_db(connection)
        _seed_coordinate_arrival_geometry(connection, sampled_at)
        before_arrivals = count_arrival_events(connection)
        before_predictions = count_prediction_events(connection)
        insert_yandex_snapshot(
            connection,
            MORNING.key,
            _coordinate_forecast_near_stop(vehicle_id="missing-thread-near-stop", thread_id=""),
            sampled_at,
        )
        _assert_equal(count_arrival_events(connection), before_arrivals)
        _assert_equal(count_prediction_events(connection), before_predictions)


def _assert_coordinate_arrival_recovers_missing_thread(db_path: Path, sampled_at: datetime) -> None:
    vehicle_id = "recovered-thread-near-stop"
    with connect(db_path) as connection:
        init_db(connection)
        _seed_coordinate_arrival_geometry(connection, sampled_at)
        insert_yandex_snapshot(
            connection,
            MORNING.key,
            _progress_forecast(vehicle_id, "2161326768", 54.934, 83.099067176),
            sampled_at - timedelta(minutes=5),
        )
        before_arrivals = count_arrival_events(connection)
        insert_yandex_snapshot(
            connection,
            MORNING.key,
            _coordinate_forecast_near_stop(vehicle_id=vehicle_id, thread_id=""),
            sampled_at,
        )
        _assert_equal(count_arrival_events(connection), before_arrivals + 1)


def _assert_coordinate_arrival_rejects_bad_route_snap(db_path: Path, sampled_at: datetime) -> None:
    with connect(db_path) as connection:
        init_db(connection)
        _seed_bad_coordinate_arrival_geometry(connection, sampled_at)
        before_arrivals = count_arrival_events(connection)
        before_predictions = count_prediction_events(connection)
        insert_yandex_snapshot(
            connection,
            MORNING.key,
            _coordinate_forecast_near_stop(vehicle_id="bad-route-snap"),
            sampled_at,
        )
        _assert_equal(count_arrival_events(connection), before_arrivals)
        _assert_equal(count_prediction_events(connection), before_predictions)


def _assert_browser_eta_is_not_trusted(db_path: Path, sampled_at: datetime) -> None:
    with connect(db_path) as connection:
        init_db(connection)
        before_predictions = count_prediction_events(connection)
        insert_yandex_snapshot(connection, MORNING.key, _browser_route_level_eta(), sampled_at)
        _assert_equal(count_prediction_events(connection), before_predictions)


def _assert_trusted_live_not_overridden_by_early_history(db_path: Path, sampled_at: datetime) -> None:
    result = PredictionEngine(db_path=db_path).predict(
        profile=MORNING,
        current_time=sampled_at,
        yandex_forecast=_trusted_forecast(12, vehicle_id="live-wins"),
        yandex_history=YandexHistoryPrediction(
            available=True,
            arrival_minutes=4,
            sample_count=120,
            bucket_minutes=15,
            window_days=30,
            percentile=80,
            fallback_reason="",
        ),
    )
    _assert_equal(result.consensus.selected_source, EtaSource.YANDEX)
    _assert_equal(result.consensus.arrival_minutes, 12)
    _assert_equal(result.consensus.confidence, EtaConfidence.HIGH)
    _assert_equal(result.consensus.target_wait_minutes, 2)
    _assert_equal(result.consensus.spread_minutes, None)
    _assert_equal(tuple(factor.kind for factor in result.consensus.factors), (EtaFactorKind.HISTORY_DISAGREEMENT,))
    _assert_equal(result.consensus.factors[0].minutes, 8)
    _assert_equal(result.consensus.factors[0].sample_count, 120)


def _assert_low_confidence_live_keeps_more_wait_buffer(db_path: Path, sampled_at: datetime) -> None:
    result = PredictionEngine(db_path=db_path).predict(
        profile=MORNING,
        current_time=sampled_at,
        yandex_forecast=_trusted_forecast(12, vehicle_id="low-confidence-live", confidence=EtaConfidence.LOW),
        yandex_history=YandexHistoryPrediction.unavailable(),
    )
    _assert_equal(result.consensus.selected_source, EtaSource.YANDEX)
    _assert_equal(result.consensus.confidence, EtaConfidence.LOW)
    _assert_equal(result.consensus.target_wait_minutes, 5)
    _assert_contains(result.consensus.warning, "слабый ETA")


def _assert_thread_fallback_eta_is_tentative(db_path: Path, sampled_at: datetime) -> None:
    forecast = _trusted_forecast(
        5,
        vehicle_id="thread-fallback",
        confidence=EtaConfidence.LOW,
        fallback_reason="vehicle_prediction_thread_fallback:not_found:2161326768",
        raw_status="vehicle_prediction_thread_fallback",
    )
    result = PredictionEngine(db_path=db_path).predict(
        profile=MORNING,
        current_time=sampled_at,
        yandex_forecast=forecast,
        yandex_history=YandexHistoryPrediction.unavailable(),
    )
    _assert_equal(result.selected, None)

    with connect(db_path) as connection:
        init_db(connection)
        before_predictions = count_prediction_events(connection)
        before_arrivals = count_arrival_events(connection)
        snapshot_id = insert_yandex_snapshot(connection, MORNING.key, forecast, sampled_at)
        _assert_equal(count_prediction_events(connection), before_predictions)
        _assert_equal(count_arrival_events(connection), before_arrivals)
        snapshot = connection.execute(
            "SELECT arrival_minutes_json FROM yandex_snapshots WHERE id = ?",
            (snapshot_id,),
        ).fetchone()
        sample = connection.execute(
            "SELECT arrival_minutes, next_arrival_minutes_json FROM yandex_forecast_samples WHERE yandex_snapshot_id = ?",
            (snapshot_id,),
        ).fetchone()
        report = connection.execute(
            "SELECT arrival_minutes_json FROM report_window_snapshots WHERE yandex_snapshot_id = ?",
            (snapshot_id,),
        ).fetchone()
        observation = connection.execute(
            "SELECT arrival_minutes FROM yandex_vehicle_observations WHERE snapshot_id = ?",
            (snapshot_id,),
        ).fetchone()
        dirty_id = _manual_prediction_event(
            connection,
            snapshot_id=snapshot_id,
            sampled_at=sampled_at,
            source=SOURCE_TARGET_STOP_LIVE,
            predicted_minutes=5,
        )
        ensemble_id = _manual_prediction_event(
            connection,
            snapshot_id=snapshot_id,
            sampled_at=sampled_at,
            source=SOURCE_ENSEMBLE,
            predicted_minutes=5,
        )
        connection.execute(
            "UPDATE prediction_events SET raw_json = ? WHERE id = ?",
            (
                json.dumps(
                    {"forecast": {"fallback_reason": "vehicle_prediction_thread_fallback:not_found:2161326768"}},
                    ensure_ascii=False,
                ),
                dirty_id,
            ),
        )
        _manual_prediction_evaluation(
            connection,
            prediction_id=dirty_id,
            source=SOURCE_TARGET_STOP_LIVE,
            predicted_minutes=5,
            error_minutes=-1,
        )
        _manual_prediction_evaluation(
            connection,
            prediction_id=ensemble_id,
            source=SOURCE_ENSEMBLE,
            predicted_minutes=5,
            error_minutes=-1,
        )
        _assert_equal(sanitize_untrusted_eta(connection) > 0, True)
        remaining_predictions = connection.execute(
            "SELECT COUNT(*) AS count FROM prediction_events WHERE id IN (?, ?)",
            (dirty_id, ensemble_id),
        ).fetchone()
        remaining_evaluations = connection.execute(
            "SELECT COUNT(*) AS count FROM prediction_evaluations WHERE prediction_event_id IN (?, ?)",
            (dirty_id, ensemble_id),
        ).fetchone()
    _assert_equal(snapshot["arrival_minutes_json"], "[]")
    _assert_equal(sample["arrival_minutes"], None)
    _assert_equal(sample["next_arrival_minutes_json"], "[]")
    _assert_equal(report["arrival_minutes_json"], "[]")
    _assert_equal(observation["arrival_minutes"], None)
    _assert_equal(remaining_predictions["count"], 0)
    _assert_equal(remaining_evaluations["count"], 0)


def _assert_short_stop_info_eta_without_coordinates_gets_buffer(db_path: Path, sampled_at: datetime) -> None:
    forecast = YandexLiveForecast(
        enabled=True,
        available=True,
        source_method=YandexSourceMethod.STOP_INFO,
        status=YandexSourceStatus.OK,
        arrival_minutes=(4,),
        vehicles=(YandexVehicle(vehicle_id="stop-info-vehicle", arrival_minutes=4),),
        vehicle_count=1,
        newest_age_seconds=0,
        confidence=EtaConfidence.MEDIUM,
        fallback_reason="stop_estimated",
        raw_status="stop_estimated",
    )
    result = PredictionEngine(db_path=db_path).predict(
        profile=MORNING,
        current_time=sampled_at,
        yandex_forecast=forecast,
        yandex_history=YandexHistoryPrediction.unavailable(),
    )
    _assert_equal(result.consensus.selected_source, EtaSource.YANDEX)
    _assert_equal(result.consensus.confidence, EtaConfidence.LOW)
    _assert_equal(result.selected.safety_wait_minutes if result.selected else None, 3)
    _assert_equal(result.consensus.target_wait_minutes, 8)
    _assert_contains(result.consensus.warning, "без координаты машины")

    with connect(db_path) as connection:
        init_db(connection)
        before_predictions = count_prediction_events(connection)
        insert_yandex_snapshot(connection, MORNING.key, forecast, sampled_at)
        live_event = connection.execute(
            """
            SELECT raw_json
            FROM prediction_events
            WHERE source = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (SOURCE_TARGET_STOP_LIVE,),
        ).fetchone()
        ensemble_event = connection.execute(
            """
            SELECT raw_json
            FROM prediction_events
            WHERE source = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (SOURCE_ENSEMBLE,),
        ).fetchone()
        after_predictions = count_prediction_events(connection)
    _assert_equal(after_predictions > before_predictions, True)
    live_raw = json.loads(live_event["raw_json"])
    ensemble_raw = json.loads(ensemble_event["raw_json"])
    _assert_equal(live_raw["live_evidence"]["safety_wait_minutes"], 3)
    _assert_equal(ensemble_raw["candidates"][0]["safety_wait_minutes"], 3)


def _assert_sanitize_drops_unsafe_coordinate_arrivals(db_path: Path, sampled_at: datetime) -> None:
    with connect(db_path) as connection:
        init_db(connection)
        unsafe_prediction_id = _manual_prediction_event(
            connection,
            snapshot_id=None,
            sampled_at=sampled_at,
            source=SOURCE_VEHICLE_PROGRESS,
            predicted_minutes=4,
        )
        safe_prediction_id = _manual_prediction_event(
            connection,
            snapshot_id=None,
            sampled_at=sampled_at,
            source=SOURCE_VEHICLE_PROGRESS,
            predicted_minutes=5,
        )
        unsafe_arrival_id = _manual_arrival_event(
            connection,
            sampled_at=sampled_at,
            vehicle_id="unsafe-coordinate-arrival",
            source="coordinate_stop",
            raw={"distance_meters": 12},
        )
        safe_arrival_id = _manual_arrival_event(
            connection,
            sampled_at=sampled_at,
            vehicle_id="safe-coordinate-arrival",
            source="coordinate_stop",
            raw={"distance_meters": 12, "route_evidence": "direct_thread"},
        )
        _manual_prediction_evaluation_for_arrival(
            connection,
            prediction_id=unsafe_prediction_id,
            arrival_id=unsafe_arrival_id,
            source=SOURCE_VEHICLE_PROGRESS,
            predicted_minutes=4,
            error_minutes=-1,
            sampled_at=sampled_at,
        )
        _manual_prediction_evaluation_for_arrival(
            connection,
            prediction_id=safe_prediction_id,
            arrival_id=safe_arrival_id,
            source=SOURCE_VEHICLE_PROGRESS,
            predicted_minutes=5,
            error_minutes=1,
            sampled_at=sampled_at,
        )
        _assert_equal(sanitize_untrusted_eta(connection) > 0, True)
        unsafe_arrivals = connection.execute(
            "SELECT COUNT(*) AS count FROM arrival_events WHERE id = ?",
            (unsafe_arrival_id,),
        ).fetchone()
        safe_arrivals = connection.execute(
            "SELECT COUNT(*) AS count FROM arrival_events WHERE id = ?",
            (safe_arrival_id,),
        ).fetchone()
        unsafe_evaluations = connection.execute(
            "SELECT COUNT(*) AS count FROM prediction_evaluations WHERE arrival_event_id = ?",
            (unsafe_arrival_id,),
        ).fetchone()
        safe_evaluations = connection.execute(
            "SELECT COUNT(*) AS count FROM prediction_evaluations WHERE arrival_event_id = ?",
            (safe_arrival_id,),
        ).fetchone()
    _assert_equal(unsafe_arrivals["count"], 0)
    _assert_equal(safe_arrivals["count"], 1)
    _assert_equal(unsafe_evaluations["count"], 0)
    _assert_equal(safe_evaluations["count"], 1)


def _assert_stale_live_eta_falls_back_to_history(db_path: Path, sampled_at: datetime) -> None:
    result = PredictionEngine(db_path=db_path).predict(
        profile=MORNING,
        current_time=sampled_at,
        yandex_forecast=_trusted_forecast(6, vehicle_id="stale-live", age_seconds=600),
        yandex_history=YandexHistoryPrediction(
            available=True,
            arrival_minutes=14,
            sample_count=120,
            bucket_minutes=15,
            window_days=30,
            percentile=80,
            fallback_reason="",
        ),
    )
    _assert_equal(result.consensus.selected_source, EtaSource.YANDEX_HISTORY)
    _assert_equal(result.consensus.arrival_minutes, 14)


def _assert_stale_eta_does_not_create_prediction_or_arrival(db_path: Path, sampled_at: datetime) -> None:
    with connect(db_path) as connection:
        init_db(connection)
        before_predictions = count_prediction_events(connection)
        before_arrivals = count_arrival_events(connection)
        insert_yandex_snapshot(
            connection, MORNING.key, _trusted_forecast(0, vehicle_id="stale-arrival", age_seconds=600), sampled_at
        )
        _assert_equal(count_prediction_events(connection), before_predictions)
        _assert_equal(count_arrival_events(connection), before_arrivals)


def _assert_stale_coordinate_does_not_create_arrival(db_path: Path, sampled_at: datetime) -> None:
    with connect(db_path) as connection:
        init_db(connection)
        before_arrivals = count_arrival_events(connection)
        insert_yandex_snapshot(connection, MORNING.key, _coordinate_forecast_near_stop(age_seconds=600), sampled_at)
        _assert_equal(count_arrival_events(connection), before_arrivals)


def _assert_ensemble_policy_prefers_materially_earlier_progress(db_path: Path, sampled_at: datetime) -> None:
    with connect(db_path) as connection:
        init_db(connection)
        snapshot_id = insert_yandex_snapshot(connection, MORNING.key, _unavailable_forecast(), sampled_at)
        live_id = _manual_prediction_event(
            connection,
            snapshot_id=snapshot_id,
            sampled_at=sampled_at,
            source=SOURCE_TARGET_STOP_LIVE,
            predicted_minutes=12,
        )
        progress_id = _manual_prediction_event(
            connection,
            snapshot_id=snapshot_id,
            sampled_at=sampled_at,
            source=SOURCE_VEHICLE_PROGRESS,
            predicted_minutes=4,
        )
        ensemble_id = _insert_ensemble_prediction_event(connection, [live_id, progress_id])
        row = connection.execute(
            "SELECT predicted_minutes, raw_json FROM prediction_events WHERE id = ?",
            (ensemble_id,),
        ).fetchone()
    raw = json.loads(row["raw_json"])
    _assert_equal(int(row["predicted_minutes"]), 4)
    _assert_equal(raw["selected_source"], SOURCE_VEHICLE_PROGRESS)


def _assert_ensemble_policy_prefers_close_progress_when_live_confidence_is_low(
    db_path: Path,
    sampled_at: datetime,
) -> None:
    with connect(db_path) as connection:
        init_db(connection)
        snapshot_id = insert_yandex_snapshot(connection, MORNING.key, _unavailable_forecast(), sampled_at)
        high_live_id = _manual_prediction_event(
            connection,
            snapshot_id=snapshot_id,
            sampled_at=sampled_at,
            source=SOURCE_TARGET_STOP_LIVE,
            predicted_minutes=8,
            confidence=EtaConfidence.HIGH,
        )
        high_progress_id = _manual_prediction_event(
            connection,
            snapshot_id=snapshot_id,
            sampled_at=sampled_at,
            source=SOURCE_VEHICLE_PROGRESS,
            predicted_minutes=7,
        )
        high_ensemble_id = _insert_ensemble_prediction_event(connection, [high_live_id, high_progress_id])
        low_live_id = _manual_prediction_event(
            connection,
            snapshot_id=snapshot_id,
            sampled_at=sampled_at + timedelta(minutes=1),
            source=SOURCE_TARGET_STOP_LIVE,
            predicted_minutes=8,
            confidence=EtaConfidence.LOW,
        )
        low_progress_id = _manual_prediction_event(
            connection,
            snapshot_id=snapshot_id,
            sampled_at=sampled_at + timedelta(minutes=1),
            source=SOURCE_VEHICLE_PROGRESS,
            predicted_minutes=7,
        )
        low_ensemble_id = _insert_ensemble_prediction_event(connection, [low_live_id, low_progress_id])
        rows = connection.execute(
            """
            SELECT predicted_minutes, raw_json
            FROM prediction_events
            WHERE id IN (?, ?)
            ORDER BY id
            """,
            (high_ensemble_id, low_ensemble_id),
        ).fetchall()
    high_raw = json.loads(rows[0]["raw_json"])
    low_raw = json.loads(rows[1]["raw_json"])
    low_live_candidate = next(item for item in low_raw["candidates"] if item["source"] == SOURCE_TARGET_STOP_LIVE)
    _assert_equal(int(rows[0]["predicted_minutes"]), 8)
    _assert_equal(high_raw["selected_source"], SOURCE_TARGET_STOP_LIVE)
    _assert_equal(int(rows[1]["predicted_minutes"]), 7)
    _assert_equal(low_raw["selected_source"], SOURCE_VEHICLE_PROGRESS)
    _assert_equal(low_live_candidate["early_conflict_minutes"], 1)


def _assert_ensemble_policy_prefers_progress_quality(db_path: Path, sampled_at: datetime) -> None:
    with connect(db_path) as connection:
        init_db(connection)
        snapshot_id = insert_yandex_snapshot(connection, MORNING.key, _unavailable_forecast(), sampled_at)
        close_low_progress_id = _manual_prediction_event(
            connection,
            snapshot_id=snapshot_id,
            sampled_at=sampled_at,
            source=SOURCE_VEHICLE_PROGRESS,
            predicted_minutes=4,
            confidence=EtaConfidence.LOW,
        )
        close_medium_progress_id = _manual_prediction_event(
            connection,
            snapshot_id=snapshot_id,
            sampled_at=sampled_at,
            source=SOURCE_VEHICLE_PROGRESS,
            predicted_minutes=5,
            confidence=EtaConfidence.MEDIUM,
        )
        close_ensemble_id = _insert_ensemble_prediction_event(
            connection,
            [close_low_progress_id, close_medium_progress_id],
        )
        early_low_progress_id = _manual_prediction_event(
            connection,
            snapshot_id=snapshot_id,
            sampled_at=sampled_at + timedelta(minutes=1),
            source=SOURCE_VEHICLE_PROGRESS,
            predicted_minutes=1,
            confidence=EtaConfidence.LOW,
        )
        early_medium_progress_id = _manual_prediction_event(
            connection,
            snapshot_id=snapshot_id,
            sampled_at=sampled_at + timedelta(minutes=1),
            source=SOURCE_VEHICLE_PROGRESS,
            predicted_minutes=5,
            confidence=EtaConfidence.MEDIUM,
        )
        early_ensemble_id = _insert_ensemble_prediction_event(
            connection,
            [early_low_progress_id, early_medium_progress_id],
        )
        rows = connection.execute(
            """
            SELECT predicted_minutes, raw_json
            FROM prediction_events
            WHERE id IN (?, ?)
            ORDER BY id
            """,
            (close_ensemble_id, early_ensemble_id),
        ).fetchall()
    close_raw = json.loads(rows[0]["raw_json"])
    early_raw = json.loads(rows[1]["raw_json"])
    close_low_candidate = next(
        item
        for item in close_raw["candidates"]
        if item["predicted_minutes"] == 4 and item["source"] == SOURCE_VEHICLE_PROGRESS
    )
    close_medium_candidate = next(
        item
        for item in close_raw["candidates"]
        if item["predicted_minutes"] == 5 and item["source"] == SOURCE_VEHICLE_PROGRESS
    )
    _assert_equal(int(rows[0]["predicted_minutes"]), 5)
    _assert_equal(close_raw["selected_source"], SOURCE_VEHICLE_PROGRESS)
    _assert_equal(close_low_candidate["quality_rank"], 1)
    _assert_equal(close_medium_candidate["quality_rank"], 0)
    _assert_equal(int(rows[1]["predicted_minutes"]), 1)
    _assert_equal(early_raw["selected_source"], SOURCE_VEHICLE_PROGRESS)


def _assert_ensemble_policy_uses_safety_adjusted_selection(db_path: Path, sampled_at: datetime) -> None:
    with connect(db_path) as connection:
        init_db(connection)
        for index in range(10):
            prediction_id = _manual_prediction_event(
                connection,
                snapshot_id=None,
                sampled_at=sampled_at - timedelta(minutes=30 - index),
                source=SOURCE_TARGET_STOP_LIVE,
                predicted_minutes=10,
            )
            _manual_prediction_evaluation(
                connection,
                prediction_id=prediction_id,
                source=SOURCE_TARGET_STOP_LIVE,
                predicted_minutes=10,
                error_minutes=-3,
            )
        snapshot_id = insert_yandex_snapshot(connection, MORNING.key, _unavailable_forecast(), sampled_at)
        live_id = _manual_prediction_event(
            connection,
            snapshot_id=snapshot_id,
            sampled_at=sampled_at,
            source=SOURCE_TARGET_STOP_LIVE,
            predicted_minutes=10,
        )
        progress_id = _manual_prediction_event(
            connection,
            snapshot_id=snapshot_id,
            sampled_at=sampled_at,
            source=SOURCE_VEHICLE_PROGRESS,
            predicted_minutes=7,
        )
        ensemble_id = _insert_ensemble_prediction_event(connection, [live_id, progress_id])
        row = connection.execute(
            "SELECT predicted_minutes, raw_json FROM prediction_events WHERE id = ?",
            (ensemble_id,),
        ).fetchone()
    raw = json.loads(row["raw_json"])
    live_candidate = next(item for item in raw["candidates"] if item["source"] == SOURCE_TARGET_STOP_LIVE)
    _assert_equal(int(row["predicted_minutes"]), 7)
    _assert_equal(raw["selected_source"], SOURCE_VEHICLE_PROGRESS)
    _assert_equal(live_candidate["safety_wait_minutes"], 3)


def _assert_vehicle_progress_requires_route_thread(db_path: Path, base: datetime) -> None:
    with connect(db_path) as connection:
        init_db(connection)
        _seed_progress_geometry(connection, base)
        insert_yandex_snapshot(
            connection,
            MORNING.key,
            _progress_forecast("progress-ok", "2161326768", 55.0, 83.0),
            base,
        )
        insert_yandex_snapshot(
            connection,
            MORNING.key,
            _progress_forecast("progress-wrong", "2161326764", 55.0, 83.0),
            base,
        )
        insert_yandex_snapshot(
            connection,
            MORNING.key,
            _progress_forecast("progress-missing", "", 55.0, 83.0),
            base,
        )
        insert_yandex_snapshot(
            connection,
            MORNING.key,
            _progress_forecast("progress-lost-thread", "2161326768", 55.0, 83.0),
            base,
        )
        insert_yandex_snapshot(
            connection,
            MORNING.key,
            _progress_forecast("progress-aged", "2161326768", 55.0, 83.0, age_seconds=120),
            base,
        )
        insert_yandex_snapshot(
            connection,
            MORNING.key,
            _progress_forecast("progress-offroute", "2161326768", 55.0, 83.01),
            base,
        )
        insert_yandex_snapshot(
            connection,
            MORNING.key,
            _progress_forecast("progress-stall", "2161326768", 55.0, 83.0),
            base,
        )
        insert_yandex_snapshot(
            connection,
            MORNING.key,
            _progress_forecast("progress-stall", "2161326768", 55.0099, 83.0),
            base + timedelta(minutes=4),
        )
        insert_yandex_snapshot(
            connection,
            MORNING.key,
            _progress_forecast("progress-stale", "2161326768", 55.0, 83.0),
            base,
        )
        insert_yandex_snapshot(
            connection,
            MORNING.key,
            _progress_forecast("progress-medium", "2161326768", 55.0, 83.0),
            base,
        )
        insert_yandex_snapshot(
            connection,
            MORNING.key,
            _progress_forecast("progress-medium", "2161326768", 55.004, 83.0),
            base + timedelta(minutes=2),
        )
        insert_yandex_snapshot(
            connection,
            MORNING.key,
            _progress_forecast("progress-medium", "2161326768", 55.01, 83.0),
            base + timedelta(minutes=5),
        )
        medium_event = connection.execute(
            """
            SELECT confidence
            FROM prediction_events
            WHERE source = ? AND vehicle_id = ?
            ORDER BY sampled_at DESC
            LIMIT 1
            """,
            (SOURCE_VEHICLE_PROGRESS, "progress-medium"),
        ).fetchone()
        _assert_equal(medium_event["confidence"], EtaConfidence.MEDIUM.value)

    ok = PredictionEngine(db_path=db_path).predict(
        profile=MORNING,
        current_time=base + timedelta(minutes=5),
        yandex_forecast=_progress_forecast("progress-ok", "2161326768", 55.01, 83.0),
        yandex_history=YandexHistoryPrediction.unavailable(),
    )
    _assert_equal(ok.consensus.selected_source, EtaSource.VEHICLE_PROGRESS)
    _assert_equal(ok.consensus.arrival_minutes, 6)

    wrong = PredictionEngine(db_path=db_path).predict(
        profile=MORNING,
        current_time=base + timedelta(minutes=5),
        yandex_forecast=_progress_forecast("progress-wrong", "2161326764", 55.01, 83.0),
        yandex_history=YandexHistoryPrediction.unavailable(),
    )
    _assert_equal(wrong.selected, None)

    missing = PredictionEngine(db_path=db_path).predict(
        profile=MORNING,
        current_time=base + timedelta(minutes=5),
        yandex_forecast=_progress_forecast("progress-missing", "", 55.01, 83.0),
        yandex_history=YandexHistoryPrediction.unavailable(),
    )
    _assert_equal(missing.selected, None)

    recovered_missing_thread = PredictionEngine(db_path=db_path).predict(
        profile=MORNING,
        current_time=base + timedelta(minutes=5),
        yandex_forecast=_progress_forecast("progress-lost-thread", "", 55.01, 83.0),
        yandex_history=YandexHistoryPrediction.unavailable(),
    )
    _assert_equal(recovered_missing_thread.consensus.selected_source, EtaSource.VEHICLE_PROGRESS)
    _assert_equal(recovered_missing_thread.consensus.arrival_minutes, 6)

    age_adjusted = PredictionEngine(db_path=db_path).predict(
        profile=MORNING,
        current_time=base + timedelta(minutes=5),
        yandex_forecast=_progress_forecast("progress-aged", "2161326768", 55.01, 83.0, age_seconds=0),
        yandex_history=YandexHistoryPrediction.unavailable(),
    )
    _assert_equal(age_adjusted.consensus.selected_source, EtaSource.VEHICLE_PROGRESS)
    _assert_equal(age_adjusted.consensus.arrival_minutes, 8)

    offroute = PredictionEngine(db_path=db_path).predict(
        profile=MORNING,
        current_time=base + timedelta(minutes=5),
        yandex_forecast=_progress_forecast("progress-offroute", "2161326768", 55.01, 83.01),
        yandex_history=YandexHistoryPrediction.unavailable(),
    )
    _assert_equal(offroute.selected, None)

    stalled_latest_point = PredictionEngine(db_path=db_path).predict(
        profile=MORNING,
        current_time=base + timedelta(minutes=5),
        yandex_forecast=_progress_forecast("progress-stall", "2161326768", 55.01, 83.0),
        yandex_history=YandexHistoryPrediction.unavailable(),
    )
    _assert_equal(stalled_latest_point.consensus.selected_source, EtaSource.VEHICLE_PROGRESS)
    _assert_equal(stalled_latest_point.consensus.arrival_minutes, 6)

    stale_current = PredictionEngine(db_path=db_path).predict(
        profile=MORNING,
        current_time=base + timedelta(minutes=5),
        yandex_forecast=_progress_forecast("progress-stale", "2161326768", 55.01, 83.0, age_seconds=600),
        yandex_history=YandexHistoryPrediction.unavailable(),
    )
    _assert_equal(stale_current.selected, None)

    medium = PredictionEngine(db_path=db_path).predict(
        profile=MORNING,
        current_time=base + timedelta(minutes=5),
        yandex_forecast=_progress_forecast("progress-medium", "2161326768", 55.01, 83.0),
        yandex_history=YandexHistoryPrediction.unavailable(),
    )
    _assert_equal(medium.consensus.selected_source, EtaSource.VEHICLE_PROGRESS)
    _assert_equal(medium.consensus.confidence, EtaConfidence.MEDIUM)
    _assert_equal(medium.selected.sample_count if medium.selected else None, 2)
    _assert_equal(medium.consensus.target_wait_minutes, 6)
    _assert_contains(medium.consensus.warning, "координатный прогноз, держу запас 1 мин")


def _assert_vehicle_progress_prefers_quality_unless_materially_earlier(db_path: Path, base: datetime) -> None:
    with connect(db_path) as connection:
        init_db(connection)
        _seed_progress_geometry(connection, base)
        insert_yandex_snapshot(
            connection,
            MORNING.key,
            _progress_forecast_many(
                YandexVehicle("progress-low-quality", thread_id="2161326768", lat=55.0, lng=83.0, age_seconds=15),
                YandexVehicle("progress-medium-quality", thread_id="2161326768", lat=55.0, lng=83.0, age_seconds=15),
            ),
            base,
        )
        insert_yandex_snapshot(
            connection,
            MORNING.key,
            _progress_forecast_many(
                YandexVehicle(
                    "progress-medium-quality",
                    thread_id="2161326768",
                    lat=55.004,
                    lng=83.0,
                    age_seconds=15,
                ),
            ),
            base + timedelta(minutes=2),
        )

    close_low_quality = PredictionEngine(db_path=db_path).predict(
        profile=MORNING,
        current_time=base + timedelta(minutes=5),
        yandex_forecast=_progress_forecast_many(
            YandexVehicle(
                "progress-low-quality",
                thread_id="2161326768",
                lat=55.010,
                lng=83.0,
                age_seconds=120,
            ),
            YandexVehicle(
                "progress-medium-quality",
                thread_id="2161326768",
                lat=55.010,
                lng=83.0,
                age_seconds=15,
            ),
        ),
        yandex_history=YandexHistoryPrediction.unavailable(),
    )
    _assert_equal(close_low_quality.consensus.selected_source, EtaSource.VEHICLE_PROGRESS)
    _assert_equal(close_low_quality.consensus.arrival_minutes, 6)
    _assert_equal(close_low_quality.consensus.confidence, EtaConfidence.MEDIUM)
    _assert_equal(close_low_quality.selected.sample_count if close_low_quality.selected else None, 2)

    materially_earlier_low_quality = PredictionEngine(db_path=db_path).predict(
        profile=MORNING,
        current_time=base + timedelta(minutes=5),
        yandex_forecast=_progress_forecast_many(
            YandexVehicle(
                "progress-low-quality",
                thread_id="2161326768",
                lat=55.016,
                lng=83.0,
                age_seconds=120,
            ),
            YandexVehicle(
                "progress-medium-quality",
                thread_id="2161326768",
                lat=55.010,
                lng=83.0,
                age_seconds=15,
            ),
        ),
        yandex_history=YandexHistoryPrediction.unavailable(),
    )
    _assert_equal(materially_earlier_low_quality.consensus.selected_source, EtaSource.VEHICLE_PROGRESS)
    _assert_equal(materially_earlier_low_quality.consensus.arrival_minutes, 1)
    _assert_equal(materially_earlier_low_quality.consensus.confidence, EtaConfidence.LOW)


def _assert_cached_coordinates_feed_vehicle_progress(db_path: Path, base: datetime) -> None:
    with connect(db_path) as connection:
        init_db(connection)
        _seed_progress_geometry(connection, base)
        insert_yandex_snapshot(
            connection,
            MORNING.key,
            _progress_forecast("cached-progress", "2161326768", 55.0, 83.0, available=False),
            base,
        )
        insert_yandex_snapshot(
            connection,
            MORNING.key,
            _progress_forecast("cached-progress", "2161326768", 55.01, 83.0, available=False),
            base + timedelta(minutes=5),
        )

    cached = CachedYandexForecastSource(db_path, max_age_seconds=600).get_forecast(MORNING, base + timedelta(minutes=5))
    _assert_equal(cached.available, False)
    _assert_equal(len(cached.vehicles), 1)
    _assert_equal(cached.vehicles[0].vehicle_id, "cached-progress")
    result = PredictionEngine(db_path=db_path).predict(
        profile=MORNING,
        current_time=base + timedelta(minutes=5),
        yandex_forecast=cached,
        yandex_history=YandexHistoryPrediction.unavailable(),
    )
    _assert_equal(result.consensus.selected_source, EtaSource.VEHICLE_PROGRESS)
    _assert_equal(result.consensus.arrival_minutes, 6)


def _assert_vehicle_progress_rejects_stale_route_geometry(db_path: Path, base: datetime) -> None:
    with connect(db_path) as connection:
        init_db(connection)
        _seed_progress_geometry(connection, base - timedelta(days=30))
        insert_yandex_snapshot(
            connection,
            MORNING.key,
            _progress_forecast("stale-geometry", "2161326768", 55.0, 83.0),
            base,
        )

    result = PredictionEngine(db_path=db_path).predict(
        profile=MORNING,
        current_time=base + timedelta(minutes=5),
        yandex_forecast=_progress_forecast("stale-geometry", "2161326768", 55.01, 83.0),
        yandex_history=YandexHistoryPrediction.unavailable(),
    )
    _assert_equal(result.selected, None)


def _assert_invalid_route_stop_coordinates_do_not_crash_progress(db_path: Path, base: datetime) -> None:
    vehicle_id = "invalid-stop-geometry"
    with connect(db_path) as connection:
        init_db(connection)
        _seed_progress_geometry(connection, base)
        connection.execute(
            "UPDATE route_geometry SET stops_json = ? WHERE profile_key = ?",
            (
                json.dumps(
                    [{"stop_id": "stop__9982194", "name": "target", "lat": "not-a-number", "lng": 83.0}],
                    ensure_ascii=False,
                ),
                MORNING.key,
            ),
        )
        insert_yandex_snapshot(
            connection,
            MORNING.key,
            _progress_forecast(vehicle_id, "2161326768", 55.0, 83.0),
            base,
        )

    result = PredictionEngine(db_path=db_path).predict(
        profile=MORNING,
        current_time=base + timedelta(minutes=5),
        yandex_forecast=_progress_forecast(vehicle_id, "2161326768", 55.01, 83.0),
        yandex_history=YandexHistoryPrediction.unavailable(),
    )
    _assert_equal(result.selected, None)


def _assert_nonfinite_route_polyline_does_not_crash_progress(db_path: Path, base: datetime) -> None:
    vehicle_id = "nonfinite-polyline"
    with connect(db_path) as connection:
        init_db(connection)
        _seed_progress_geometry(connection, base)
        connection.execute(
            "UPDATE route_geometry SET route_polyline_json = ? WHERE profile_key = ?",
            (
                json.dumps([[83.0, 55.0], [83.0, "nan"]], ensure_ascii=False),
                MORNING.key,
            ),
        )
        insert_yandex_snapshot(
            connection,
            MORNING.key,
            _progress_forecast(vehicle_id, "2161326768", 55.0, 83.0),
            base,
        )

    result = PredictionEngine(db_path=db_path).predict(
        profile=MORNING,
        current_time=base + timedelta(minutes=5),
        yandex_forecast=_progress_forecast(vehicle_id, "2161326768", 55.01, 83.0),
        yandex_history=YandexHistoryPrediction.unavailable(),
    )
    _assert_equal(result.selected, None)


def _assert_vehicle_progress_tracker_marks_stalled(db_path: Path, base: datetime) -> None:
    with connect(db_path) as connection:
        init_db(connection)
        _seed_progress_geometry(connection, base)
        insert_yandex_snapshot(
            connection,
            MORNING.key,
            _progress_forecast("progress-creep", "2161326768", 55.0, 83.0),
            base,
        )
        insert_yandex_snapshot(
            connection,
            MORNING.key,
            _progress_forecast("progress-creep", "2161326768", 55.004, 83.0),
            base + timedelta(minutes=2),
        )
        insert_yandex_snapshot(
            connection,
            MORNING.key,
            _progress_forecast("progress-creep", "2161326768", 55.0041, 83.0),
            base + timedelta(minutes=3),
        )
        insert_yandex_snapshot(
            connection,
            MORNING.key,
            _progress_forecast("progress-creep", "2161326768", 55.0042, 83.0),
            base + timedelta(minutes=5),
        )
        row = connection.execute(
            """
            SELECT raw_json
            FROM prediction_events
            WHERE source = ? AND vehicle_id = ?
            ORDER BY sampled_at DESC
            LIMIT 1
            """,
            (SOURCE_VEHICLE_PROGRESS, "progress-creep"),
        ).fetchone()
    if row is None:
        raise AssertionError("expected stalled vehicle progress prediction")
    raw = json.loads(row["raw_json"])
    _assert_equal(raw["tracker"], "alpha_beta_v2")
    _assert_equal(raw["stalled_buffer_minutes"], 2)


def _assert_prediction_lab_backfill(db_path: Path, base: datetime) -> None:
    with connect(db_path) as connection:
        init_db(connection)
        insert_yandex_snapshot(connection, MORNING.key, _trusted_forecast(1), base)
        insert_yandex_snapshot(connection, MORNING.key, _trusted_forecast(0), base + timedelta(minutes=1))
        connection.execute("DELETE FROM prediction_evaluations")
        connection.execute("DELETE FROM prediction_events")
        connection.execute("DELETE FROM arrival_events")
        result = backfill_prediction_lab(
            connection,
            profile_key=MORNING.key,
            report_window_key="weekday_morning_09_12",
        )
        _assert_equal(result.prediction_events_created, 4)
        _assert_equal(result.arrival_events_created, 1)
        _assert_equal(result.evaluations_created, 2)
        _assert_equal(count_arrival_events(connection), 1)
        _assert_equal(count_prediction_events(connection), 4)
        _assert_equal(count_prediction_evaluations(connection), 2)


def _assert_prediction_evaluation_matches_vehicle_and_thread(db_path: Path, base: datetime) -> None:
    with connect(db_path) as connection:
        init_db(connection)
        vehicle_prediction_id = _manual_prediction_event(
            connection,
            snapshot_id=None,
            sampled_at=base - timedelta(minutes=5),
            source=SOURCE_TARGET_STOP_LIVE,
            predicted_minutes=5,
            vehicle_id="vehicle-specific",
            thread_id="2161326768",
        )
        anonymous_prediction_id = _manual_prediction_event(
            connection,
            snapshot_id=None,
            sampled_at=base - timedelta(minutes=5),
            source=SOURCE_TARGET_STOP_LIVE,
            predicted_minutes=5,
            vehicle_id="",
            thread_id="",
        )
        _manual_arrival_event(
            connection,
            sampled_at=base,
            vehicle_id="",
            source="trusted_eta",
            raw={"arrival_minutes": 0},
        )
        inserted = evaluate_pending_predictions(
            connection,
            profile_key=MORNING.key,
            report_window_key="weekday_morning_09_12",
        )
        _assert_equal(inserted, 1)
        _assert_equal(_prediction_evaluation_count_for_prediction(connection, anonymous_prediction_id), 1)
        _assert_equal(_prediction_evaluation_count_for_prediction(connection, vehicle_prediction_id), 0)

    with connect(db_path) as connection:
        same_vehicle_wrong_thread_id = _manual_prediction_event(
            connection,
            snapshot_id=None,
            sampled_at=base + timedelta(minutes=5),
            source=SOURCE_TARGET_STOP_LIVE,
            predicted_minutes=5,
            vehicle_id="vehicle-thread",
            thread_id="wrong-thread",
        )
        same_vehicle_right_thread_id = _manual_prediction_event(
            connection,
            snapshot_id=None,
            sampled_at=base + timedelta(minutes=5),
            source=SOURCE_TARGET_STOP_LIVE,
            predicted_minutes=5,
            vehicle_id="vehicle-thread",
            thread_id="2161326768",
        )
        _manual_arrival_event(
            connection,
            sampled_at=base + timedelta(minutes=10),
            vehicle_id="vehicle-thread",
            source="trusted_eta",
            raw={"arrival_minutes": 0},
        )
        inserted = evaluate_pending_predictions(
            connection,
            profile_key=MORNING.key,
            report_window_key="weekday_morning_09_12",
        )
        _assert_equal(inserted, 1)
        _assert_equal(_prediction_evaluation_count_for_prediction(connection, same_vehicle_wrong_thread_id), 0)
        _assert_equal(_prediction_evaluation_count_for_prediction(connection, same_vehicle_right_thread_id), 1)


def _assert_prediction_lab_summary_ignores_malformed_latest_times(db_path: Path, base: datetime) -> None:
    valid_latest = base + timedelta(minutes=1)
    with connect(db_path) as connection:
        init_db(connection)
        insert_yandex_snapshot(connection, MORNING.key, _trusted_forecast(1, vehicle_id="summary-latest"), base)
        snapshot_id = insert_yandex_snapshot(
            connection,
            MORNING.key,
            _trusted_forecast(0, vehicle_id="summary-latest"),
            valid_latest,
        )
        bad_prediction_id = _manual_prediction_event(
            connection,
            snapshot_id=snapshot_id,
            sampled_at=base + timedelta(minutes=2),
            source=SOURCE_TARGET_STOP_LIVE,
            predicted_minutes=4,
            vehicle_id="malformed-latest",
        )
        connection.execute(
            "UPDATE prediction_events SET sampled_at = ? WHERE id = ?",
            ("zzzz-not-a-time", bad_prediction_id),
        )
        connection.execute(
            """
            INSERT INTO arrival_events(
                yandex_snapshot_id, profile_key, vehicle_id, thread_id, stop_id,
                arrived_at, source, confidence, lat, lng, raw_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot_id,
                MORNING.key,
                "malformed-latest",
                "2161326768",
                "stop__9982194",
                "zzzz-not-a-time",
                "smoke",
                EtaConfidence.HIGH.value,
                None,
                None,
                "{}",
            ),
        )
        connection.commit()
        summary = summarize_prediction_lab_window(
            connection,
            profile_key=MORNING.key,
            report_window_key="weekday_morning_09_12",
        )
        arrivals = load_arrival_events(
            connection,
            profile_key=MORNING.key,
            report_window_key="weekday_morning_09_12",
            limit=10,
        )
    _assert_equal(summary.latest_arrival_at, valid_latest)
    _assert_equal(summary.latest_prediction_at, valid_latest)
    _assert_equal(arrivals[0].arrived_at, valid_latest)


def _assert_history_headway_uses_prior_samples(db_path: Path, sampled_at: datetime) -> None:
    with connect(db_path) as connection:
        init_db(connection)
        for index, historical_at in enumerate(_history_sample_times(sampled_at, 19)):
            insert_yandex_snapshot(
                connection, MORNING.key, _trusted_forecast(16, vehicle_id=f"history-{index}"), historical_at
            )
        insert_yandex_snapshot(connection, MORNING.key, _trusted_forecast(18, vehicle_id="current-live"), sampled_at)
        _assert_equal(_prediction_source_count(connection, SOURCE_HISTORY_HEADWAY), 0)
        insert_yandex_snapshot(connection, MORNING.key, _unavailable_forecast(), sampled_at + timedelta(minutes=5))
        _assert_equal(_prediction_source_count(connection, SOURCE_HISTORY_HEADWAY), 1)


def _seed_negative_residuals(db_path: Path, base: datetime) -> None:
    with connect(db_path) as connection:
        init_db(connection)
        for index, day_offset in enumerate((7, 6, 3, 2, 1)):
            sampled_at = base - timedelta(days=day_offset)
            insert_yandex_snapshot(
                connection, MORNING.key, _trusted_forecast(10, vehicle_id=f"vehicle-{index}"), sampled_at
            )
            insert_yandex_snapshot(
                connection,
                MORNING.key,
                _trusted_forecast(0, vehicle_id=f"vehicle-{index}"),
                sampled_at + timedelta(minutes=7),
            )
        _assert_equal(_prediction_evaluation_source_count(connection, SOURCE_TARGET_STOP_LIVE), 5)
        _assert_equal(_prediction_evaluation_source_count(connection, SOURCE_ENSEMBLE), 5)


def _assert_residual_correction_is_capped(db_path: Path, base: datetime) -> None:
    with connect(db_path) as connection:
        init_db(connection)
        for index in range(5):
            prediction_id = _manual_prediction_event(
                connection,
                snapshot_id=None,
                sampled_at=base - timedelta(minutes=30 - index),
                source=SOURCE_TARGET_STOP_LIVE,
                predicted_minutes=20,
            )
            _manual_prediction_evaluation(
                connection,
                prediction_id=prediction_id,
                source=SOURCE_TARGET_STOP_LIVE,
                predicted_minutes=20,
                error_minutes=-12,
            )
        correction = load_residual_correction(
            connection,
            profile_key=MORNING.key,
            report_window_key="weekday_morning_09_12",
            predicted_minutes=20,
            min_samples=5,
            current_time=base,
        )
    _assert_equal(correction.p10_error_minutes, -12)
    _assert_equal(correction.correction_minutes, -6)

    result = PredictionEngine(db_path=db_path, residual_min_samples=5, reliability_min_samples=999).predict(
        profile=MORNING,
        current_time=base,
        yandex_forecast=_trusted_forecast(20),
        yandex_history=YandexHistoryPrediction.unavailable(),
    )
    _assert_equal(result.consensus.selected_source, EtaSource.YANDEX_CORRECTED)
    _assert_equal(result.consensus.arrival_minutes, 14)
    _assert_contains(result.consensus.warning, "ETA сдвинут на 6 мин раньше")


def _assert_stale_prediction_errors_are_ignored(db_path: Path, base: datetime) -> None:
    stale_base = base - timedelta(days=30)
    with connect(db_path) as connection:
        init_db(connection)
        for index in range(5):
            prediction_id = _manual_prediction_event(
                connection,
                snapshot_id=None,
                sampled_at=stale_base + timedelta(minutes=index),
                source=SOURCE_TARGET_STOP_LIVE,
                predicted_minutes=10,
            )
            _manual_prediction_evaluation(
                connection,
                prediction_id=prediction_id,
                source=SOURCE_TARGET_STOP_LIVE,
                predicted_minutes=10,
                error_minutes=-4,
            )
        correction = load_residual_correction(
            connection,
            profile_key=MORNING.key,
            report_window_key="weekday_morning_09_12",
            predicted_minutes=10,
            min_samples=5,
            current_time=base,
        )
        reliability = load_source_reliability(
            connection,
            profile_key=MORNING.key,
            report_window_key="weekday_morning_09_12",
            source=SOURCE_TARGET_STOP_LIVE,
            predicted_minutes=10,
            min_samples=5,
            current_time=base,
        )
    _assert_equal(correction.sample_count, 0)
    _assert_equal(correction.correction_minutes, 0)
    _assert_equal(reliability.sample_count, 0)
    _assert_equal(reliability.safety_buffer_minutes, 0)

    result = PredictionEngine(db_path=db_path, residual_min_samples=5, reliability_min_samples=5).predict(
        profile=MORNING,
        current_time=base,
        yandex_forecast=_trusted_forecast(10),
        yandex_history=YandexHistoryPrediction.unavailable(),
    )
    _assert_equal(result.consensus.selected_source, EtaSource.YANDEX)
    _assert_equal(result.consensus.arrival_minutes, 10)
    _assert_equal(result.selected.safety_wait_minutes if result.selected else None, 0)


def _assert_future_prediction_errors_are_ignored(db_path: Path, base: datetime) -> None:
    with connect(db_path) as connection:
        init_db(connection)
        for index in range(5):
            prediction_id = _manual_prediction_event(
                connection,
                snapshot_id=None,
                sampled_at=base + timedelta(minutes=index + 1),
                source=SOURCE_TARGET_STOP_LIVE,
                predicted_minutes=10,
            )
            _manual_prediction_evaluation(
                connection,
                prediction_id=prediction_id,
                source=SOURCE_TARGET_STOP_LIVE,
                predicted_minutes=10,
                error_minutes=-4,
            )
        correction = load_residual_correction(
            connection,
            profile_key=MORNING.key,
            report_window_key="weekday_morning_09_12",
            predicted_minutes=10,
            min_samples=5,
            current_time=base,
        )
        reliability = load_source_reliability(
            connection,
            profile_key=MORNING.key,
            report_window_key="weekday_morning_09_12",
            source=SOURCE_TARGET_STOP_LIVE,
            predicted_minutes=10,
            min_samples=5,
            current_time=base,
        )
    _assert_equal(correction.sample_count, 0)
    _assert_equal(correction.correction_minutes, 0)
    _assert_equal(reliability.sample_count, 0)
    _assert_equal(reliability.safety_buffer_minutes, 0)


def _assert_source_reliability_uses_source_scope_when_bucket_is_sparse(db_path: Path, base: datetime) -> None:
    with connect(db_path) as connection:
        init_db(connection)
        _seed_sparse_source_errors(connection, base)
        reliability = load_source_reliability(
            connection,
            profile_key=MORNING.key,
            report_window_key="weekday_morning_09_12",
            source=SOURCE_TARGET_STOP_LIVE,
            predicted_minutes=10,
            min_samples=5,
            current_time=base,
        )
    _assert_equal(reliability.scope, "source")
    _assert_equal(reliability.sample_count, 5)
    _assert_equal(reliability.safety_buffer_minutes, 3)

    buffered = PredictionEngine(db_path=db_path, residual_min_samples=999, reliability_min_samples=5).predict(
        profile=MORNING,
        current_time=base,
        yandex_forecast=_trusted_forecast(10),
        yandex_history=YandexHistoryPrediction.unavailable(),
    )
    _assert_equal(buffered.selected.safety_wait_minutes if buffered.selected else None, 3)
    _assert_equal(buffered.consensus.target_wait_minutes, 8)
    _assert_equal(buffered.selected.reliability_scope if buffered.selected else None, "source")
    _assert_contains(buffered.consensus.warning, "по общей статистике источника")


def _assert_source_reliability_uses_miss_rate_floor(db_path: Path, base: datetime) -> None:
    with connect(db_path) as connection:
        init_db(connection)
        for index, error_minutes in enumerate((-1, -1, -1, -1, -1, -1, 2, 2, 2, 2)):
            prediction_id = _manual_prediction_event(
                connection,
                snapshot_id=None,
                sampled_at=base - timedelta(minutes=90 - index),
                source=SOURCE_TARGET_STOP_LIVE,
                predicted_minutes=10,
            )
            _manual_prediction_evaluation(
                connection,
                prediction_id=prediction_id,
                source=SOURCE_TARGET_STOP_LIVE,
                predicted_minutes=10,
                error_minutes=error_minutes,
            )
        reliability = load_source_reliability(
            connection,
            profile_key=MORNING.key,
            report_window_key="weekday_morning_09_12",
            source=SOURCE_TARGET_STOP_LIVE,
            predicted_minutes=10,
            min_samples=10,
            current_time=base,
        )
    _assert_equal(reliability.miss_rate_percent, 60)
    _assert_equal(reliability.p10_error_minutes, -1)
    _assert_equal(reliability.safety_buffer_minutes, 3)

    buffered = PredictionEngine(db_path=db_path, residual_min_samples=999, reliability_min_samples=10).predict(
        profile=MORNING,
        current_time=base,
        yandex_forecast=_trusted_forecast(10),
        yandex_history=YandexHistoryPrediction.unavailable(),
    )
    _assert_equal(buffered.selected.safety_wait_minutes if buffered.selected else None, 3)
    _assert_equal(buffered.selected.miss_rate_percent if buffered.selected else None, 60)
    _assert_equal(buffered.consensus.confidence, EtaConfidence.LOW)
    _assert_equal(buffered.consensus.target_wait_minutes, 8)


def _assert_prediction_lab_calibration_surfaces_runtime_guardrails(db_path: Path, base: datetime) -> None:
    with connect(db_path) as connection:
        init_db(connection)
        for index, error_minutes in enumerate((-1, -1, -1, -1, -1, -1, 2, 2, 2, 2)):
            prediction_id = _manual_prediction_event(
                connection,
                snapshot_id=None,
                sampled_at=base - timedelta(minutes=90 - index),
                source=SOURCE_TARGET_STOP_LIVE,
                predicted_minutes=10,
            )
            _manual_prediction_evaluation(
                connection,
                prediction_id=prediction_id,
                source=SOURCE_TARGET_STOP_LIVE,
                predicted_minutes=10,
                error_minutes=error_minutes,
            )
        stale_prediction_id = _manual_prediction_event(
            connection,
            snapshot_id=None,
            sampled_at=base - timedelta(days=30),
            source=SOURCE_TARGET_STOP_LIVE,
            predicted_minutes=10,
        )
        _manual_prediction_evaluation(
            connection,
            prediction_id=stale_prediction_id,
            source=SOURCE_TARGET_STOP_LIVE,
            predicted_minutes=10,
            error_minutes=-9,
        )
        future_prediction_id = _manual_prediction_event(
            connection,
            snapshot_id=None,
            sampled_at=base + timedelta(minutes=1),
            source=SOURCE_TARGET_STOP_LIVE,
            predicted_minutes=10,
        )
        _manual_prediction_evaluation(
            connection,
            prediction_id=future_prediction_id,
            source=SOURCE_TARGET_STOP_LIVE,
            predicted_minutes=10,
            error_minutes=-9,
        )
        low_confidence_id = _manual_prediction_event(
            connection,
            snapshot_id=None,
            sampled_at=base - timedelta(minutes=5),
            source=SOURCE_TARGET_STOP_LIVE,
            predicted_minutes=10,
        )
        _manual_prediction_evaluation(
            connection,
            prediction_id=low_confidence_id,
            source=SOURCE_TARGET_STOP_LIVE,
            predicted_minutes=10,
            error_minutes=-9,
            arrival_confidence=EtaConfidence.LOW,
        )
        summary = summarize_prediction_lab_calibration(
            connection,
            profile_key=MORNING.key,
            report_window_key="weekday_morning_09_12",
            current_time=base,
        )

    _assert_equal(summary.window_key, "weekday_morning_09_12")
    _assert_equal(summary.profile_key, MORNING.key)
    _assert_equal(len(summary.buckets), 1)
    bucket = summary.buckets[0]
    _assert_equal(bucket.source, SOURCE_TARGET_STOP_LIVE)
    _assert_equal(bucket.bucket, "6-10")
    _assert_equal(bucket.evaluated_predictions, 10)
    _assert_equal(bucket.miss_cases, 6)
    _assert_equal(bucket.miss_rate_percent, 60)
    _assert_equal(bucket.p10_error_minutes, -1)
    _assert_equal(bucket.reliability.sample_count, 10)
    _assert_equal(bucket.reliability.scope, "bucket")
    _assert_equal(bucket.reliability.safety_buffer_minutes, 3)
    _assert_equal(bucket.runtime_reliability.sample_count, 0)
    _assert_equal(bucket.effective_reliability.scope, "bucket")
    _assert_equal(bucket.effective_reliability_reason, "baseline_no_runtime")
    _assert_equal(bucket.residual_correction.correction_minutes if bucket.residual_correction else None, -1)

    formatted = format_prediction_lab_calibration(summary, Path("data/calibration.sqlite"))
    _assert_contains(formatted, "prediction calibration window=weekday_morning_09_12")
    _assert_contains(formatted, "source=target_stop_live bucket=6-10 samples=10")
    _assert_contains(formatted, "buffer=3m correction=-1m")
    _assert_contains(formatted, "reliability_reason=baseline_no_runtime")
    _assert_contains(formatted, "runtime_samples=0 runtime_scope=bot_runtime_bucket")
    _assert_contains(formatted, "action=apply_buffer")


def _assert_bot_runtime_reliability_surfaces_worse_miss_rate_without_buffer(db_path: Path, base: datetime) -> None:
    with connect(db_path) as connection:
        init_db(connection)
        for index in range(30):
            prediction_id = _manual_prediction_event(
                connection,
                snapshot_id=None,
                sampled_at=base - timedelta(minutes=220 - index),
                source=SOURCE_TARGET_STOP_LIVE,
                predicted_minutes=10,
            )
            _manual_prediction_evaluation(
                connection,
                prediction_id=prediction_id,
                source=SOURCE_TARGET_STOP_LIVE,
                predicted_minutes=10,
                error_minutes=2,
            )
        for index, error_minutes in enumerate((-1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)):
            prediction_id = _manual_prediction_event(
                connection,
                snapshot_id=None,
                sampled_at=base - timedelta(minutes=60 - index),
                source=SOURCE_TARGET_STOP_LIVE,
                predicted_minutes=10,
                runtime_source=RUNTIME_SOURCE_WEB_APP,
            )
            _manual_prediction_evaluation(
                connection,
                prediction_id=prediction_id,
                source=SOURCE_TARGET_STOP_LIVE,
                predicted_minutes=10,
                error_minutes=error_minutes,
            )
        baseline = load_source_reliability(
            connection,
            profile_key=MORNING.key,
            report_window_key="weekday_morning_09_12",
            source=SOURCE_TARGET_STOP_LIVE,
            predicted_minutes=10,
            min_samples=10,
            current_time=base,
        )
        runtime = load_source_reliability(
            connection,
            profile_key=MORNING.key,
            report_window_key="weekday_morning_09_12",
            source=SOURCE_TARGET_STOP_LIVE,
            predicted_minutes=10,
            min_samples=3,
            current_time=base,
            runtime_source=RUNTIME_SOURCE_WEB_APP,
        )
        summary = summarize_prediction_lab_calibration(
            connection,
            profile_key=MORNING.key,
            report_window_key="weekday_morning_09_12",
            current_time=base,
            reliability_min_samples=10,
            runtime_reliability_min_samples=3,
        )

    _assert_equal(baseline.sample_count, 41)
    _assert_equal(baseline.miss_rate_percent, 2)
    _assert_equal(baseline.safety_buffer_minutes, 0)
    _assert_equal(runtime.scope, "bot_runtime_bucket")
    _assert_equal(runtime.sample_count, 11)
    _assert_equal(runtime.miss_cases, 1)
    _assert_equal(runtime.miss_rate_percent, 9)
    _assert_equal(runtime.safety_buffer_minutes, 0)
    _assert_equal(len(summary.buckets), 1)
    bucket = summary.buckets[0]
    _assert_equal(bucket.effective_reliability.scope, "bot_runtime_bucket")
    _assert_equal(bucket.effective_reliability.sample_count, 11)
    _assert_equal(bucket.effective_reliability.miss_rate_percent, 9)
    _assert_equal(bucket.effective_reliability_reason, "runtime_miss_rate")
    formatted = format_prediction_lab_calibration(summary, Path("data/runtime-miss-rate.sqlite"))
    _assert_contains(formatted, "reliability_scope=bot_runtime_bucket reliability_reason=runtime_miss_rate buffer=0m")
    _assert_contains(formatted, "baseline_samples=41 baseline_scope=bucket baseline_buffer=0m baseline_miss=1(2%)")
    _assert_contains(
        formatted,
        "runtime_samples=11 runtime_scope=bot_runtime_bucket runtime_miss=1(9%) runtime_buffer=0m",
    )
    _assert_contains(formatted, "action=review_runtime_miss_rate")

    buffered = PredictionEngine(
        db_path=db_path,
        residual_min_samples=999,
        reliability_min_samples=10,
        runtime_reliability_min_samples=3,
    ).predict(
        profile=MORNING,
        current_time=base,
        yandex_forecast=_trusted_forecast(10),
        yandex_history=YandexHistoryPrediction.unavailable(),
    )
    _assert_equal(buffered.selected.safety_wait_minutes if buffered.selected else None, 0)
    _assert_equal(buffered.selected.reliability_scope if buffered.selected else None, "bot_runtime_bucket")
    _assert_equal(buffered.selected.reliability_sample_count if buffered.selected else None, 11)
    _assert_equal(buffered.selected.miss_rate_percent if buffered.selected else None, 9)


def _assert_bot_runtime_reliability_can_raise_buffer(db_path: Path, base: datetime) -> None:
    with connect(db_path) as connection:
        init_db(connection)
        for index in range(30):
            prediction_id = _manual_prediction_event(
                connection,
                snapshot_id=None,
                sampled_at=base - timedelta(minutes=180 - index),
                source=SOURCE_TARGET_STOP_LIVE,
                predicted_minutes=10,
            )
            _manual_prediction_evaluation(
                connection,
                prediction_id=prediction_id,
                source=SOURCE_TARGET_STOP_LIVE,
                predicted_minutes=10,
                error_minutes=2,
            )
        for index in range(3):
            prediction_id = _manual_prediction_event(
                connection,
                snapshot_id=None,
                sampled_at=base - timedelta(minutes=30 - index),
                source=SOURCE_TARGET_STOP_LIVE,
                predicted_minutes=10,
                runtime_source=RUNTIME_SOURCE_WEB_APP,
            )
            _manual_prediction_evaluation(
                connection,
                prediction_id=prediction_id,
                source=SOURCE_TARGET_STOP_LIVE,
                predicted_minutes=10,
                error_minutes=-2,
            )
        baseline = load_source_reliability(
            connection,
            profile_key=MORNING.key,
            report_window_key="weekday_morning_09_12",
            source=SOURCE_TARGET_STOP_LIVE,
            predicted_minutes=10,
            min_samples=10,
            current_time=base,
        )
        runtime = load_source_reliability(
            connection,
            profile_key=MORNING.key,
            report_window_key="weekday_morning_09_12",
            source=SOURCE_TARGET_STOP_LIVE,
            predicted_minutes=10,
            min_samples=3,
            current_time=base,
            runtime_source=RUNTIME_SOURCE_WEB_APP,
        )
        summary = summarize_prediction_lab_calibration(
            connection,
            profile_key=MORNING.key,
            report_window_key="weekday_morning_09_12",
            current_time=base,
            reliability_min_samples=10,
            runtime_reliability_min_samples=3,
        )
    _assert_equal(baseline.sample_count, 33)
    _assert_equal(baseline.safety_buffer_minutes, 0)
    _assert_equal(runtime.scope, "bot_runtime_bucket")
    _assert_equal(runtime.sample_count, 3)
    _assert_equal(runtime.miss_rate_percent, 100)
    _assert_equal(runtime.safety_buffer_minutes, 3)
    _assert_equal(len(summary.buckets), 1)
    bucket = summary.buckets[0]
    _assert_equal(bucket.reliability.safety_buffer_minutes, 0)
    _assert_equal(bucket.runtime_reliability.safety_buffer_minutes, 3)
    _assert_equal(bucket.effective_reliability.scope, "bot_runtime_bucket")
    _assert_equal(bucket.effective_reliability_reason, "runtime_buffer")
    formatted = format_prediction_lab_calibration(summary, Path("data/runtime-calibration.sqlite"))
    _assert_contains(formatted, "reliability_scope=bot_runtime_bucket reliability_reason=runtime_buffer buffer=3m")
    _assert_contains(formatted, "baseline_samples=33 baseline_scope=bucket baseline_buffer=0m")
    _assert_contains(
        formatted, "runtime_samples=3 runtime_scope=bot_runtime_bucket runtime_miss=3(100%) runtime_buffer=3m"
    )
    _assert_contains(formatted, "action=apply_runtime_buffer")

    buffered = PredictionEngine(
        db_path=db_path,
        residual_min_samples=999,
        reliability_min_samples=10,
        runtime_reliability_min_samples=3,
    ).predict(
        profile=MORNING,
        current_time=base,
        yandex_forecast=_trusted_forecast(10),
        yandex_history=YandexHistoryPrediction.unavailable(),
    )
    _assert_equal(buffered.selected.safety_wait_minutes if buffered.selected else None, 3)
    _assert_equal(buffered.selected.reliability_scope if buffered.selected else None, "bot_runtime_bucket")
    _assert_equal(buffered.selected.reliability_sample_count if buffered.selected else None, 3)
    _assert_equal(buffered.selected.miss_rate_percent if buffered.selected else None, 100)
    _assert_contains(
        buffered.consensus.warning,
        "по похожим ответам бота для источника Яндекс live добавил запас 3 мин",
    )


def _assert_residual_correction_uses_source_scope_when_bucket_is_sparse(db_path: Path, base: datetime) -> None:
    with connect(db_path) as connection:
        init_db(connection)
        _seed_sparse_source_errors(connection, base)
        correction = load_residual_correction(
            connection,
            profile_key=MORNING.key,
            report_window_key="weekday_morning_09_12",
            predicted_minutes=10,
            min_samples=5,
            current_time=base,
        )
    _assert_equal(correction.scope, "source")
    _assert_equal(correction.sample_count, 5)
    _assert_equal(correction.correction_minutes, -3)

    result = PredictionEngine(db_path=db_path, residual_min_samples=5, reliability_min_samples=999).predict(
        profile=MORNING,
        current_time=base,
        yandex_forecast=_trusted_forecast(10),
        yandex_history=YandexHistoryPrediction.unavailable(),
    )
    _assert_equal(result.consensus.selected_source, EtaSource.YANDEX_CORRECTED)
    _assert_equal(result.consensus.arrival_minutes, 7)
    _assert_equal(result.selected.correction_scope if result.selected else None, "source")
    _assert_contains(result.consensus.warning, "по общей статистике источника")


def _assert_residual_correction_ignores_medium_arrival_facts(db_path: Path, base: datetime) -> None:
    with connect(db_path) as connection:
        init_db(connection)
        for index in range(5):
            prediction_id = _manual_prediction_event(
                connection,
                snapshot_id=None,
                sampled_at=base - timedelta(minutes=30 - index),
                source=SOURCE_TARGET_STOP_LIVE,
                predicted_minutes=10,
            )
            _manual_prediction_evaluation(
                connection,
                prediction_id=prediction_id,
                source=SOURCE_TARGET_STOP_LIVE,
                predicted_minutes=10,
                error_minutes=-4,
                arrival_confidence=EtaConfidence.MEDIUM,
            )
        correction = load_residual_correction(
            connection,
            profile_key=MORNING.key,
            report_window_key="weekday_morning_09_12",
            predicted_minutes=10,
            min_samples=5,
            current_time=base,
        )
        reliability = load_source_reliability(
            connection,
            profile_key=MORNING.key,
            report_window_key="weekday_morning_09_12",
            source=SOURCE_TARGET_STOP_LIVE,
            predicted_minutes=10,
            min_samples=5,
            current_time=base,
        )
    _assert_equal(correction.sample_count, 0)
    _assert_equal(correction.correction_minutes, 0)
    _assert_equal(reliability.sample_count, 5)
    _assert_equal(reliability.safety_buffer_minutes, 4)


def _assert_source_reliability_prefers_high_confidence_arrivals(db_path: Path, base: datetime) -> None:
    with connect(db_path) as connection:
        init_db(connection)
        for index in range(5):
            prediction_id = _manual_prediction_event(
                connection,
                snapshot_id=None,
                sampled_at=base - timedelta(minutes=90 - index),
                source=SOURCE_TARGET_STOP_LIVE,
                predicted_minutes=10,
            )
            _manual_prediction_evaluation(
                connection,
                prediction_id=prediction_id,
                source=SOURCE_TARGET_STOP_LIVE,
                predicted_minutes=10,
                error_minutes=1,
                arrival_confidence=EtaConfidence.HIGH,
            )
        for index in range(5):
            prediction_id = _manual_prediction_event(
                connection,
                snapshot_id=None,
                sampled_at=base - timedelta(minutes=30 - index),
                source=SOURCE_TARGET_STOP_LIVE,
                predicted_minutes=10,
            )
            _manual_prediction_evaluation(
                connection,
                prediction_id=prediction_id,
                source=SOURCE_TARGET_STOP_LIVE,
                predicted_minutes=10,
                error_minutes=-4,
                arrival_confidence=EtaConfidence.MEDIUM,
            )
        reliability = load_source_reliability(
            connection,
            profile_key=MORNING.key,
            report_window_key="weekday_morning_09_12",
            source=SOURCE_TARGET_STOP_LIVE,
            predicted_minutes=10,
            min_samples=5,
            current_time=base,
        )
    _assert_equal(reliability.sample_count, 5)
    _assert_equal(reliability.miss_cases, 0)
    _assert_equal(reliability.safety_buffer_minutes, 0)


def _seed_sparse_source_errors(connection, base: datetime) -> None:
    for index, (predicted_minutes, error_minutes) in enumerate(((4, -2), (5, -2), (7, -2), (12, -1), (16, -3))):
        sampled_at = base - timedelta(minutes=60 - index * 10)
        prediction_id = _manual_prediction_event(
            connection,
            snapshot_id=None,
            sampled_at=sampled_at,
            source=SOURCE_TARGET_STOP_LIVE,
            predicted_minutes=predicted_minutes,
        )
        _manual_prediction_evaluation(
            connection,
            prediction_id=prediction_id,
            source=SOURCE_TARGET_STOP_LIVE,
            predicted_minutes=predicted_minutes,
            error_minutes=error_minutes,
        )


def _history_sample_times(base: datetime, count: int) -> tuple[datetime, ...]:
    day_offsets = (1, 2, 3)
    minute_offsets = (-30, -25, -20, -15, -10, -5, 0)
    values = []
    for day_offset in day_offsets:
        for minute_offset in minute_offsets:
            values.append(base - timedelta(days=day_offset) + timedelta(minutes=minute_offset))
            if len(values) == count:
                return tuple(values)
    return tuple(values)


def _trusted_forecast(
    minutes: int,
    *,
    vehicle_id: str = "vehicle-1",
    age_seconds: int = 15,
    confidence: EtaConfidence = EtaConfidence.HIGH,
    fallback_reason: str = "",
    raw_status: str = "",
) -> YandexLiveForecast:
    lat, lng = (54.937428366, 83.099067176) if minutes == 0 else (54.93, 83.12)
    return YandexLiveForecast(
        enabled=True,
        available=True,
        source_method=YandexSourceMethod.VEHICLE_PREDICTION,
        status=YandexSourceStatus.OK,
        arrival_minutes=(minutes,),
        vehicles=(
            YandexVehicle(
                vehicle_id=vehicle_id,
                thread_id="2161326768",
                lat=lat,
                lng=lng,
                arrival_minutes=minutes,
                age_seconds=age_seconds,
            ),
        ),
        vehicle_count=1,
        newest_age_seconds=age_seconds,
        confidence=confidence,
        fallback_reason=fallback_reason,
        raw_status=raw_status,
    )


def _unavailable_forecast() -> YandexLiveForecast:
    return YandexLiveForecast(
        enabled=True,
        available=False,
        source_method=YandexSourceMethod.NONE,
        status=YandexSourceStatus.EMPTY,
        fallback_reason="empty",
    )


def _coordinate_forecast_near_stop(
    *,
    vehicle_id: str = "coord-only",
    thread_id: str = "2161326768",
    age_seconds: int = 10,
) -> YandexLiveForecast:
    return YandexLiveForecast(
        enabled=True,
        available=True,
        source_method=YandexSourceMethod.BROWSER,
        status=YandexSourceStatus.COORDINATES_ONLY,
        vehicles=(
            YandexVehicle(
                vehicle_id=vehicle_id,
                thread_id=thread_id,
                lat=54.937428366,
                lng=83.099067176,
                age_seconds=age_seconds,
            ),
        ),
        vehicle_count=1,
        newest_age_seconds=age_seconds,
        confidence=EtaConfidence.LOW,
    )


def _browser_route_level_eta() -> YandexLiveForecast:
    return YandexLiveForecast(
        enabled=True,
        available=True,
        source_method=YandexSourceMethod.BROWSER,
        status=YandexSourceStatus.OK,
        arrival_minutes=(4,),
        vehicles=(YandexVehicle(vehicle_id="browser-eta", thread_id="2161326768", lat=54.93, lng=83.12),),
        vehicle_count=1,
        confidence=EtaConfidence.LOW,
    )


def _progress_forecast(
    vehicle_id: str,
    thread_id: str,
    lat: float,
    lng: float,
    *,
    age_seconds: int = 15,
    available: bool = True,
) -> YandexLiveForecast:
    return YandexLiveForecast(
        enabled=True,
        available=available,
        source_method=YandexSourceMethod.VEHICLE_PREDICTION,
        status=YandexSourceStatus.COORDINATES_ONLY,
        vehicles=(
            YandexVehicle(
                vehicle_id=vehicle_id,
                thread_id=thread_id,
                lat=lat,
                lng=lng,
                age_seconds=age_seconds,
            ),
        ),
        vehicle_count=1,
        newest_age_seconds=age_seconds,
        confidence=EtaConfidence.LOW,
    )


def _progress_forecast_many(*vehicles: YandexVehicle, available: bool = True) -> YandexLiveForecast:
    newest_age_seconds = min(
        (vehicle.age_seconds for vehicle in vehicles if vehicle.age_seconds is not None),
        default=None,
    )
    return YandexLiveForecast(
        enabled=True,
        available=available,
        source_method=YandexSourceMethod.VEHICLE_PREDICTION,
        status=YandexSourceStatus.COORDINATES_ONLY,
        vehicles=vehicles,
        vehicle_count=len(vehicles),
        newest_age_seconds=newest_age_seconds,
        confidence=EtaConfidence.LOW,
    )


def _seed_progress_geometry(connection, sampled_at: datetime) -> None:
    _seed_route_geometry(
        connection,
        sampled_at=sampled_at,
        points=((83.0, 55.0), (83.0, 55.02)),
        target_lat=55.02,
        target_lng=83.0,
    )


def _seed_coordinate_arrival_geometry(connection, sampled_at: datetime) -> None:
    target_lat = 54.937428366
    target_lng = 83.099067176
    _seed_route_geometry(
        connection,
        sampled_at=sampled_at,
        points=((target_lng, target_lat - 0.01), (target_lng, target_lat + 0.01)),
        target_lat=target_lat,
        target_lng=target_lng,
    )


def _seed_bad_coordinate_arrival_geometry(connection, sampled_at: datetime) -> None:
    target_lat = 54.937428366
    target_lng = 83.099067176
    _seed_route_geometry(
        connection,
        sampled_at=sampled_at,
        points=((target_lng + 0.02, target_lat - 0.01), (target_lng + 0.02, target_lat + 0.01)),
        target_lat=target_lat,
        target_lng=target_lng,
    )


def _seed_route_geometry(
    connection,
    *,
    sampled_at: datetime,
    points: tuple[tuple[float, float], ...],
    target_lat: float,
    target_lng: float,
) -> None:
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
            MORNING.key,
            "line-74",
            "2161326768",
            "stop__9982194",
            json.dumps([[lng, lat] for lng, lat in points], ensure_ascii=False),
            json.dumps(
                [{"stop_id": "stop__9982194", "name": "target", "lat": target_lat, "lng": target_lng}],
                ensure_ascii=False,
            ),
            sampled_at.isoformat(),
            "{}",
        ),
    )


def _prediction_source_count(connection, source: str) -> int:
    row = connection.execute("SELECT COUNT(*) AS count FROM prediction_events WHERE source = ?", (source,)).fetchone()
    return int(row["count"])


def _prediction_evaluation_source_count(connection, source: str) -> int:
    row = connection.execute(
        "SELECT COUNT(*) AS count FROM prediction_evaluations WHERE source = ?", (source,)
    ).fetchone()
    return int(row["count"])


def _prediction_evaluation_count_for_prediction(connection, prediction_id: int) -> int:
    row = connection.execute(
        "SELECT COUNT(*) AS count FROM prediction_evaluations WHERE prediction_event_id = ?",
        (prediction_id,),
    ).fetchone()
    return int(row["count"])


def _manual_prediction_event(
    connection,
    *,
    snapshot_id: int | None,
    sampled_at: datetime,
    source: str,
    predicted_minutes: int,
    confidence: EtaConfidence = EtaConfidence.LOW,
    vehicle_id: str = "vehicle-policy",
    thread_id: str = "2161326768",
    runtime_source: str = "",
) -> int:
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
            MORNING.key,
            sampled_at.isoformat(),
            "weekday_morning_09_12",
            source,
            "smoke",
            predicted_minutes,
            (sampled_at + timedelta(minutes=predicted_minutes)).isoformat(),
            confidence.value,
            vehicle_id,
            thread_id,
            "none",
            "not_collected",
            None,
            runtime_source,
            "{}",
        ),
    )
    return int(cursor.lastrowid)


def _manual_prediction_evaluation(
    connection,
    *,
    prediction_id: int,
    source: str,
    predicted_minutes: int,
    error_minutes: int,
    arrival_confidence: EtaConfidence = EtaConfidence.HIGH,
) -> None:
    arrival_cursor = connection.execute(
        """
        INSERT INTO arrival_events(
            yandex_snapshot_id, profile_key, vehicle_id, thread_id, stop_id,
            arrived_at, source, confidence, lat, lng, raw_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            None,
            MORNING.key,
            "vehicle-policy",
            "2161326768",
            "stop__9982194",
            datetime(2026, 6, 4, 9, 0, tzinfo=NOVOSIBIRSK_TZ).isoformat(),
            "smoke",
            arrival_confidence.value,
            None,
            None,
            "{}",
        ),
    )
    actual_minutes = max(0, predicted_minutes + error_minutes)
    connection.execute(
        """
        INSERT INTO prediction_evaluations(
            prediction_event_id, arrival_event_id, profile_key, evaluated_at,
            actual_minutes, predicted_minutes, error_minutes, bucket, source, raw_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            prediction_id,
            int(arrival_cursor.lastrowid),
            MORNING.key,
            datetime(2026, 6, 4, 9, 0, tzinfo=NOVOSIBIRSK_TZ).isoformat(),
            actual_minutes,
            predicted_minutes,
            error_minutes,
            prediction_bucket(predicted_minutes),
            source,
            "{}",
        ),
    )


def _manual_arrival_event(
    connection,
    *,
    sampled_at: datetime,
    vehicle_id: str,
    source: str,
    raw: dict[str, object],
) -> int:
    cursor = connection.execute(
        """
        INSERT INTO arrival_events(
            yandex_snapshot_id, profile_key, vehicle_id, thread_id, stop_id,
            arrived_at, source, confidence, lat, lng, raw_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            None,
            MORNING.key,
            vehicle_id,
            "2161326768",
            "stop__9982194",
            sampled_at.isoformat(),
            source,
            EtaConfidence.MEDIUM.value,
            54.937428366,
            83.099067176,
            json.dumps(raw, ensure_ascii=False),
        ),
    )
    return int(cursor.lastrowid)


def _manual_prediction_evaluation_for_arrival(
    connection,
    *,
    prediction_id: int,
    arrival_id: int,
    source: str,
    predicted_minutes: int,
    error_minutes: int,
    sampled_at: datetime,
) -> None:
    connection.execute(
        """
        INSERT INTO prediction_evaluations(
            prediction_event_id, arrival_event_id, profile_key, evaluated_at,
            actual_minutes, predicted_minutes, error_minutes, bucket, source, raw_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            prediction_id,
            arrival_id,
            MORNING.key,
            sampled_at.isoformat(),
            max(0, predicted_minutes + error_minutes),
            predicted_minutes,
            error_minutes,
            prediction_bucket(predicted_minutes),
            source,
            "{}",
        ),
    )


def _assert_equal(actual: object, expected: object) -> None:
    if actual != expected:
        raise AssertionError(f"expected {expected!r}, got {actual!r}")


def _assert_contains(text: str, expected: str) -> None:
    if expected not in text:
        raise AssertionError(f"expected {expected!r} in {text!r}")


def _assert_raises_value_error(callback: Callable[[], object], expected: str) -> None:
    try:
        callback()
    except ValueError as error:
        _assert_contains(str(error), expected)
        return
    raise AssertionError("expected ValueError")


if __name__ == "__main__":
    main()
