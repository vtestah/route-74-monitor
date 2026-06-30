from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from route74.domain.commute import CommuteProfile
from route74.domain.eta import EtaConfidence, EtaConsensus, EtaFactor, EtaFactorKind, EtaSource
from route74.domain.prediction_consensus import (
    PredictionCandidate,
    build_prediction_consensus,
    prediction_selection_candidate_for_eta_source,
    valid_prediction_candidates,
)
from route74.domain.prediction_selection import select_prediction_key
from route74.domain.prediction_sources import EVENT_SOURCE_BY_ETA_SOURCE
from route74.domain.reporting import matching_report_window
from route74.domain.runtime_sources import RUNTIME_SOURCE_WEB_APP
from route74.domain.yandex_history import YandexHistoryPrediction
from route74.sources.yandex.live_evidence import LiveEtaEvidenceAdjustment, live_eta_evidence_adjustment
from route74.sources.yandex.models import YandexLiveForecast, YandexVehicle
from route74.sources.yandex.freshness import forecast_is_fresh
from route74.sources.yandex.trust import (
    forecast_has_trusted_fresh_eta,
    is_trusted_eta_observation,
    trusted_arrivals_for_forecast,
)
from route74.storage import DEFAULT_DB, connect, init_db
from route74.storage.prediction_lab import (
    RESIDUAL_MIN_SAMPLES,
    RUNTIME_RELIABILITY_MIN_SAMPLES,
    SOURCE_RELIABILITY_MIN_SAMPLES,
    SourceReliability,
    effective_source_reliability,
    estimate_vehicle_progress_candidates,
    load_residual_correction,
    load_source_reliability,
    vehicle_progress_confidence,
)


PREDICTION_STORAGE_ERRORS = (OSError, sqlite3.Error, ValueError)
PREDICTION_STORAGE_GUARDRAIL_SCOPE = "prediction_storage_unavailable"


@dataclass(frozen=True)
class PredictionEngineResult:
    candidates: tuple[PredictionCandidate, ...]
    selected: PredictionCandidate | None
    consensus: EtaConsensus


class PredictionEngine:
    def __init__(
        self,
        *,
        db_path: Path = DEFAULT_DB,
        residual_min_samples: int = RESIDUAL_MIN_SAMPLES,
        reliability_min_samples: int = SOURCE_RELIABILITY_MIN_SAMPLES,
        runtime_reliability_min_samples: int = RUNTIME_RELIABILITY_MIN_SAMPLES,
    ) -> None:
        self._db_path = db_path
        self._residual_min_samples = residual_min_samples
        self._reliability_min_samples = reliability_min_samples
        self._runtime_reliability_min_samples = runtime_reliability_min_samples

    def predict(
        self,
        *,
        profile: CommuteProfile,
        current_time: datetime,
        yandex_forecast: YandexLiveForecast,
        yandex_history: YandexHistoryPrediction,
    ) -> PredictionEngineResult:
        candidates = self._candidates(profile, current_time, yandex_forecast, yandex_history)
        valid_candidates = valid_prediction_candidates(candidates)
        if not valid_candidates:
            return PredictionEngineResult((), None, EtaConsensus.disabled())
        selected, consensus = build_prediction_consensus(valid_candidates)
        return PredictionEngineResult(valid_candidates, selected, consensus)

    def _candidates(
        self,
        profile: CommuteProfile,
        current_time: datetime,
        yandex_forecast: YandexLiveForecast,
        yandex_history: YandexHistoryPrediction,
    ) -> tuple[PredictionCandidate, ...]:
        candidates: list[PredictionCandidate] = []
        window = matching_report_window(current_time, profile.key)
        window_key = window.key if window is not None else None
        live_minutes = _trusted_live_minutes(yandex_forecast)
        ignored_live_factors = _ignored_live_eta_factors(yandex_forecast, live_minutes=live_minutes)

        try:
            with connect(self._db_path) as connection:
                init_db(connection)
                if live_minutes is not None:
                    evidence = live_eta_evidence_adjustment(yandex_forecast, arrival_minutes=live_minutes)
                    candidates.append(
                        self._candidate(
                            connection,
                            profile=profile,
                            window_key=window_key,
                            source=EtaSource.YANDEX,
                            minutes=live_minutes,
                            confidence=yandex_forecast.confidence,
                            current_time=current_time,
                            live_evidence=evidence,
                        )
                    )
                    correction = load_residual_correction(
                        connection,
                        profile_key=profile.key,
                        report_window_key=window_key,
                        predicted_minutes=live_minutes,
                        min_samples=self._residual_min_samples,
                        current_time=current_time,
                    )
                    if correction.correction_minutes < 0:
                        corrected_minutes = max(0, live_minutes + correction.correction_minutes)
                        candidates.append(
                            self._candidate(
                                connection,
                                profile=profile,
                                window_key=window_key,
                                source=EtaSource.YANDEX_CORRECTED,
                                minutes=corrected_minutes,
                                confidence=EtaConfidence.MEDIUM,
                                correction_minutes=correction.correction_minutes,
                                correction_scope=correction.scope,
                                sample_count=correction.sample_count,
                                current_time=current_time,
                                live_evidence=evidence,
                            )
                        )
                vehicle_progress = self._vehicle_progress(
                    connection,
                    profile,
                    current_time,
                    yandex_forecast,
                    diagnostic_factors=ignored_live_factors,
                )
                if vehicle_progress is not None:
                    candidates.append(vehicle_progress)
                if (
                    yandex_history.available
                    and yandex_history.arrival_minutes is not None
                    and yandex_history.arrival_minutes >= 0
                ):
                    candidates.append(
                        self._candidate(
                            connection,
                            profile=profile,
                            window_key=window_key,
                            source=EtaSource.YANDEX_HISTORY,
                            minutes=yandex_history.arrival_minutes,
                            confidence=EtaConfidence.LOW,
                            sample_count=yandex_history.sample_count,
                            current_time=current_time,
                            history_percentile=yandex_history.percentile,
                            diagnostic_factors=ignored_live_factors,
                        )
                    )
        except PREDICTION_STORAGE_ERRORS:
            return _stateless_candidates(
                yandex_forecast,
                yandex_history,
                live_minutes=live_minutes,
                storage_guardrail_unavailable=True,
            )
        return tuple(candidates)

    def _candidate(
        self,
        connection,
        *,
        profile: CommuteProfile,
        window_key: str | None,
        source: EtaSource,
        minutes: int,
        confidence: EtaConfidence,
        current_time: datetime,
        correction_minutes: int = 0,
        correction_scope: str = "",
        sample_count: int = 0,
        live_evidence: LiveEtaEvidenceAdjustment = LiveEtaEvidenceAdjustment(),
        diagnostic_factors: tuple[EtaFactor, ...] = (),
        history_percentile: int = 0,
    ) -> PredictionCandidate:
        reliability = self._source_reliability(
            connection,
            profile=profile,
            window_key=window_key,
            source=source,
            minutes=minutes,
            current_time=current_time,
        )
        safety_wait_minutes = max(reliability.safety_buffer_minutes, live_evidence.safety_wait_minutes)
        safety_scope = reliability.scope
        if _live_evidence_scope_is_more_relevant(live_evidence, reliability):
            safety_scope = live_evidence.scope
        return PredictionCandidate(
            source,
            minutes,
            confidence,
            correction_minutes=correction_minutes,
            correction_scope=correction_scope,
            sample_count=sample_count,
            safety_wait_minutes=safety_wait_minutes,
            reliability_sample_count=reliability.sample_count,
            miss_rate_percent=reliability.miss_rate_percent,
            reliability_scope=safety_scope,
            diagnostic_factors=diagnostic_factors,
            history_percentile=history_percentile,
        )

    def _source_reliability(
        self,
        connection: sqlite3.Connection,
        *,
        profile: CommuteProfile,
        window_key: str | None,
        source: EtaSource,
        minutes: int,
        current_time: datetime,
    ) -> SourceReliability:
        event_source = EVENT_SOURCE_BY_ETA_SOURCE[source]
        baseline = load_source_reliability(
            connection,
            profile_key=profile.key,
            report_window_key=window_key,
            source=event_source,
            predicted_minutes=minutes,
            min_samples=self._reliability_min_samples,
            current_time=current_time,
        )
        runtime = load_source_reliability(
            connection,
            profile_key=profile.key,
            report_window_key=window_key,
            source=event_source,
            predicted_minutes=minutes,
            min_samples=self._runtime_reliability_min_samples,
            current_time=current_time,
            runtime_source=RUNTIME_SOURCE_WEB_APP,
        )
        return effective_source_reliability(baseline, runtime)

    def _vehicle_progress(
        self,
        connection,
        profile: CommuteProfile,
        current_time: datetime,
        yandex_forecast: YandexLiveForecast,
        *,
        diagnostic_factors: tuple[EtaFactor, ...] = (),
    ) -> PredictionCandidate | None:
        progress = estimate_vehicle_progress_candidates(
            connection,
            profile_key=profile.key,
            forecast=yandex_forecast,
            sampled_at=current_time,
        )
        if not progress:
            return None
        _vehicle, minutes, raw = self._select_vehicle_progress(progress)
        window = matching_report_window(current_time, profile.key)
        return self._candidate(
            connection,
            profile=profile,
            window_key=window.key if window is not None else None,
            source=EtaSource.VEHICLE_PROGRESS,
            minutes=minutes,
            confidence=vehicle_progress_confidence(raw),
            sample_count=int(raw.get("speed_sample_count") or 0),
            current_time=current_time,
            diagnostic_factors=diagnostic_factors,
        )

    def _select_vehicle_progress(
        self,
        progress: tuple[tuple[YandexVehicle, int, dict[str, object]], ...],
    ) -> tuple[YandexVehicle, int, dict[str, object]]:
        keyed = {str(index): candidate for index, candidate in enumerate(progress)}
        selected_key = select_prediction_key(
            tuple(
                prediction_selection_candidate_for_eta_source(
                    key=key,
                    source=EtaSource.VEHICLE_PROGRESS,
                    arrival_minutes=minutes,
                    confidence=vehicle_progress_confidence(raw),
                )
                for key, (_vehicle, minutes, raw) in keyed.items()
            )
        )
        return keyed[selected_key]


def _first_non_negative_arrival(arrivals: tuple[int, ...]) -> int | None:
    return next((minutes for minutes in arrivals if _valid_arrival_minutes(minutes)), None)


def _trusted_live_minutes(yandex_forecast: YandexLiveForecast) -> int | None:
    if yandex_forecast.confidence == EtaConfidence.UNKNOWN:
        return None
    if not forecast_has_trusted_fresh_eta(yandex_forecast):
        return None
    return _first_non_negative_arrival(yandex_forecast.arrival_minutes)


def _stateless_candidates(
    yandex_forecast: YandexLiveForecast,
    yandex_history: YandexHistoryPrediction,
    *,
    live_minutes: int | None,
    storage_guardrail_unavailable: bool = False,
) -> tuple[PredictionCandidate, ...]:
    candidates: list[PredictionCandidate] = []
    diagnostic_factors = _storage_guardrail_factors(storage_guardrail_unavailable)
    ignored_live_factors = _ignored_live_eta_factors(yandex_forecast, live_minutes=live_minutes)
    if live_minutes is not None:
        evidence = live_eta_evidence_adjustment(yandex_forecast, arrival_minutes=live_minutes)
        candidates.append(
            PredictionCandidate(
                EtaSource.YANDEX,
                live_minutes,
                yandex_forecast.confidence,
                safety_wait_minutes=evidence.safety_wait_minutes,
                reliability_scope=evidence.scope,
                diagnostic_factors=diagnostic_factors,
            )
        )
    if (
        yandex_history.available
        and yandex_history.arrival_minutes is not None
        and yandex_history.arrival_minutes >= 0
    ):
        candidates.append(
            PredictionCandidate(
                EtaSource.YANDEX_HISTORY,
                yandex_history.arrival_minutes,
                EtaConfidence.LOW,
                sample_count=yandex_history.sample_count,
                diagnostic_factors=(*diagnostic_factors, *ignored_live_factors),
                history_percentile=yandex_history.percentile,
            )
        )
    return tuple(candidates)


def _storage_guardrail_factors(enabled: bool) -> tuple[EtaFactor, ...]:
    if not enabled:
        return ()
    return (
        EtaFactor(
            EtaFactorKind.GUARDRAIL_UNAVAILABLE,
            scope=PREDICTION_STORAGE_GUARDRAIL_SCOPE,
        ),
    )


def _ignored_live_eta_factors(
    yandex_forecast: YandexLiveForecast,
    *,
    live_minutes: int | None,
) -> tuple[EtaFactor, ...]:
    if live_minutes is not None or not yandex_forecast.available:
        return ()
    arrivals = trusted_arrivals_for_forecast(yandex_forecast)
    arrival_minutes = arrivals[0] if arrivals else _first_non_negative_arrival(yandex_forecast.arrival_minutes)
    if arrival_minutes is None:
        return ()
    scope = _ignored_live_eta_scope(yandex_forecast)
    if not scope:
        return ()
    return (EtaFactor(EtaFactorKind.IGNORED_LIVE_ETA, minutes=arrival_minutes, scope=scope),)


def _ignored_live_eta_scope(yandex_forecast: YandexLiveForecast) -> str:
    if not is_trusted_eta_observation(
        yandex_forecast.source_method.value,
        fallback_reason=yandex_forecast.fallback_reason,
        raw_status=yandex_forecast.raw_status,
    ):
        return _untrusted_live_eta_scope(yandex_forecast)
    if not forecast_is_fresh(yandex_forecast):
        return "stale"
    if yandex_forecast.confidence == EtaConfidence.UNKNOWN:
        return "unknown_confidence"
    return ""


def _untrusted_live_eta_scope(yandex_forecast: YandexLiveForecast) -> str:
    diagnostic = f"{yandex_forecast.raw_status} {yandex_forecast.fallback_reason}"
    if "thread_fallback" in diagnostic or "direction_thread" in diagnostic:
        return "untrusted_direction"
    return "untrusted_live"


def _live_evidence_scope_is_more_relevant(
    live_evidence: LiveEtaEvidenceAdjustment,
    reliability: SourceReliability,
) -> bool:
    if not live_evidence.applied:
        return False
    if live_evidence.safety_wait_minutes > reliability.safety_buffer_minutes:
        return True
    if live_evidence.safety_wait_minutes < reliability.safety_buffer_minutes:
        return False
    return not reliability.scope.startswith("bot_runtime_")


def _valid_arrival_minutes(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0
