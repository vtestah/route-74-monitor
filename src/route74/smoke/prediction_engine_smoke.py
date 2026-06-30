from __future__ import annotations

from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from route74.domain.commute import CommuteProfile, DepartureSource, DepartureUrgency
from route74.domain.eta import EtaConfidence, EtaFactorKind, EtaSource
from route74.domain.profiles import MORNING
from route74.domain.yandex_history import YandexHistoryPrediction
from route74.models import NOVOSIBIRSK_TZ
from route74.services.commute import CommuteService
from route74.services.prediction_engine import PredictionEngine
from route74.sources.yandex.models import YandexLiveForecast, YandexSourceMethod, YandexSourceStatus


class UnavailableHistoryPredictor:
    def predict_at(self, _profile: CommuteProfile, _current_time: datetime) -> YandexHistoryPrediction:
        return YandexHistoryPrediction.unavailable(reason="local_history_unavailable")


def main() -> None:
    _assert_live_eta_skips_bool_arrivals()
    _assert_unknown_live_confidence_falls_back_to_history()
    _assert_stale_live_eta_explains_history_fallback()
    _assert_storage_failure_keeps_live_eta()
    _assert_storage_failure_keeps_history_eta()
    with TemporaryDirectory() as temp_dir:
        service = CommuteService(
            history_predictor=UnavailableHistoryPredictor(),
            prediction_engine=PredictionEngine(db_path=Path(temp_dir) / "route74.sqlite"),
            clock=lambda: datetime(2026, 6, 4, 7, 0, tzinfo=NOVOSIBIRSK_TZ),
        )
        decision = service.build_decision(MORNING, walk_minutes=12)

    _assert_equal(decision.source, DepartureSource.NONE)
    _assert_equal(decision.urgency, DepartureUrgency.NO_DATA)
    _assert_equal(decision.eta_consensus.selected_source, None)
    _assert_equal(decision.eta_consensus.arrival_minutes, None)
    _assert_equal(decision.eta_consensus.confidence, EtaConfidence.UNKNOWN)
    print("OK | prediction engine smoke passed")


def _assert_storage_failure_keeps_live_eta() -> None:
    current_time = datetime(2026, 6, 4, 7, 0, tzinfo=NOVOSIBIRSK_TZ)
    with TemporaryDirectory() as temp_dir:
        blocked_db_path = Path(temp_dir) / "route74.sqlite"
        blocked_db_path.mkdir()
        result = PredictionEngine(db_path=blocked_db_path).predict(
            profile=MORNING,
            current_time=current_time,
            yandex_forecast=_trusted_forecast(12),
            yandex_history=YandexHistoryPrediction.unavailable(reason="history_disabled"),
        )
    _assert_equal(result.selected.source if result.selected else None, EtaSource.YANDEX)
    _assert_equal(result.consensus.selected_source, EtaSource.YANDEX)
    _assert_equal(result.consensus.arrival_minutes, 12)
    _assert_equal(
        tuple(factor.kind for factor in result.consensus.factors),
        (EtaFactorKind.GUARDRAIL_UNAVAILABLE,),
    )


def _assert_storage_failure_keeps_history_eta() -> None:
    current_time = datetime(2026, 6, 4, 7, 0, tzinfo=NOVOSIBIRSK_TZ)
    with TemporaryDirectory() as temp_dir:
        blocked_db_path = Path(temp_dir) / "route74.sqlite"
        blocked_db_path.mkdir()
        result = PredictionEngine(db_path=blocked_db_path).predict(
            profile=MORNING,
            current_time=current_time,
            yandex_forecast=YandexLiveForecast.unavailable(
                status=YandexSourceStatus.UNAVAILABLE,
                reason="source_unavailable",
            ),
            yandex_history=YandexHistoryPrediction(
                available=True,
                arrival_minutes=18,
                sample_count=20,
                bucket_minutes=30,
                window_days=14,
                percentile=80,
                fallback_reason="",
            ),
        )
    _assert_equal(result.selected.source if result.selected else None, EtaSource.YANDEX_HISTORY)
    _assert_equal(result.consensus.selected_source, EtaSource.YANDEX_HISTORY)
    _assert_equal(result.consensus.arrival_minutes, 18)
    _assert_equal(
        tuple(factor.kind for factor in result.consensus.factors),
        (EtaFactorKind.GUARDRAIL_UNAVAILABLE, EtaFactorKind.HISTORY_SAMPLE),
    )
    _assert_equal(result.consensus.factors[1].percent, 80)
    _assert_equal(result.consensus.factors[0].percent, 0)
    _assert_equal(result.consensus.factors[1].percent, 80)


def _assert_live_eta_skips_bool_arrivals() -> None:
    current_time = datetime(2026, 6, 4, 7, 0, tzinfo=NOVOSIBIRSK_TZ)
    with TemporaryDirectory() as temp_dir:
        result = PredictionEngine(db_path=Path(temp_dir) / "route74.sqlite").predict(
            profile=MORNING,
            current_time=current_time,
            yandex_forecast=YandexLiveForecast(
                enabled=True,
                available=True,
                source_method=YandexSourceMethod.VEHICLE_PREDICTION,
                status=YandexSourceStatus.OK,
                arrival_minutes=(True, 8),  # type: ignore[list-item]
                newest_age_seconds=0,
                confidence=EtaConfidence.HIGH,
            ),
            yandex_history=YandexHistoryPrediction.unavailable(reason="history_disabled"),
        )
    _assert_equal(result.selected.arrival_minutes if result.selected else None, 8)
    _assert_equal(result.consensus.selected_source, EtaSource.YANDEX)


def _assert_unknown_live_confidence_falls_back_to_history() -> None:
    current_time = datetime(2026, 6, 4, 7, 0, tzinfo=NOVOSIBIRSK_TZ)
    with TemporaryDirectory() as temp_dir:
        result = PredictionEngine(db_path=Path(temp_dir) / "route74.sqlite").predict(
            profile=MORNING,
            current_time=current_time,
            yandex_forecast=_trusted_forecast(9, confidence=EtaConfidence.UNKNOWN),
            yandex_history=YandexHistoryPrediction(
                available=True,
                arrival_minutes=18,
                sample_count=20,
                bucket_minutes=30,
                window_days=14,
                percentile=80,
                fallback_reason="",
            ),
        )
    _assert_equal(tuple(candidate.source for candidate in result.candidates), (EtaSource.YANDEX_HISTORY,))
    _assert_equal(result.selected.source if result.selected else None, EtaSource.YANDEX_HISTORY)
    _assert_equal(result.consensus.selected_source, EtaSource.YANDEX_HISTORY)
    _assert_equal(result.consensus.arrival_minutes, 18)


def _assert_stale_live_eta_explains_history_fallback() -> None:
    current_time = datetime(2026, 6, 4, 7, 0, tzinfo=NOVOSIBIRSK_TZ)
    with TemporaryDirectory() as temp_dir:
        result = PredictionEngine(db_path=Path(temp_dir) / "route74.sqlite").predict(
            profile=MORNING,
            current_time=current_time,
            yandex_forecast=_trusted_forecast(6, newest_age_seconds=600),
            yandex_history=YandexHistoryPrediction(
                available=True,
                arrival_minutes=18,
                sample_count=20,
                bucket_minutes=30,
                window_days=14,
                percentile=80,
                fallback_reason="",
            ),
        )
    _assert_equal(result.selected.source if result.selected else None, EtaSource.YANDEX_HISTORY)
    _assert_equal(result.consensus.selected_source, EtaSource.YANDEX_HISTORY)
    _assert_equal(
        tuple(
            (factor.kind, factor.minutes, factor.sample_count, factor.percent, factor.scope)
            for factor in result.consensus.factors
        ),
        (
            (EtaFactorKind.IGNORED_LIVE_ETA, 6, 0, 0, "stale"),
            (EtaFactorKind.HISTORY_SAMPLE, 0, 20, 80, ""),
        ),
    )
    _assert_equal(result.consensus.factors[1].percent, 80)


def _trusted_forecast(
    arrival_minutes: int,
    *,
    confidence: EtaConfidence = EtaConfidence.HIGH,
    newest_age_seconds: int = 0,
) -> YandexLiveForecast:
    return YandexLiveForecast(
        enabled=True,
        available=True,
        source_method=YandexSourceMethod.VEHICLE_PREDICTION,
        status=YandexSourceStatus.OK,
        arrival_minutes=(arrival_minutes,),
        newest_age_seconds=newest_age_seconds,
        confidence=confidence,
    )


def _assert_equal(actual: object, expected: object) -> None:
    if actual != expected:
        raise AssertionError(f"expected {expected!r}, got {actual!r}")


if __name__ == "__main__":
    main()
