from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from route74.domain.commute import DepartureSource
from route74.domain.eta import EtaConfidence
from route74.domain.profiles import MORNING
from route74.models import NOVOSIBIRSK_TZ
from route74.services.commute import CommuteService
from route74.services.departure import choose_profile_for_time
from route74.services.prediction_engine import PredictionEngine
from route74.sources.yandex.models import YandexLiveForecast, YandexSourceMethod, YandexSourceStatus


def main() -> None:
    service = _service_with_clock(lambda: datetime(2026, 6, 4, 7, 0, tzinfo=NOVOSIBIRSK_TZ))
    _assert_rejects(lambda: service.build_snapshot(object(), 12), "CommuteProfile")
    _assert_rejects(lambda: service.build_snapshot(MORNING, True), "walk")
    _assert_rejects(lambda: service.build_decision(MORNING, 61), "walk")
    _assert_rejects(
        lambda: _service_with_clock(lambda: datetime(2026, 6, 4, 7, 0)).build_snapshot(
            MORNING,
            12,
        ),
        "timezone-aware",
    )
    _assert_rejects(
        lambda: _service_with_clock(lambda: datetime(2026, 6, 4, 7, 0, tzinfo=timezone.utc)).build_snapshot(
            MORNING, 12
        ),
        "Asia/Novosibirsk",
    )
    _assert_rejects(
        lambda: _service_with_clock(
            lambda: datetime(2026, 6, 4, 7, 0, tzinfo=timezone(timedelta(hours=7)))
        ).build_snapshot(MORNING, 12),
        "Asia/Novosibirsk",
    )
    _assert_auto_profile_window_seconds()
    _assert_history_storage_failure_keeps_live_eta()
    print("OK | commute service smoke passed")


def _service_with_clock(clock: Callable[[], datetime]) -> CommuteService:
    return CommuteService(
        yandex_source=_FailingYandexSource(),
        history_predictor=_FailingHistoryPredictor(),
        prediction_engine=_FailingPredictionEngine(),
        clock=clock,
    )


class _FailingYandexSource:
    def get_forecast(self, *_args: object) -> object:
        raise AssertionError("Yandex source should not be called for invalid request")


class _FailingHistoryPredictor:
    def predict_at(self, *_args: object) -> object:
        raise AssertionError("history predictor should not be called for invalid request")


class _FailingPredictionEngine:
    def predict(self, **_kwargs: object) -> object:
        raise AssertionError("prediction engine should not be called for invalid request")


def _assert_history_storage_failure_keeps_live_eta() -> None:
    current_time = datetime(2026, 6, 4, 7, 0, tzinfo=NOVOSIBIRSK_TZ)
    with TemporaryDirectory() as temp_dir:
        service = CommuteService(
            yandex_source=_TrustedYandexSource(),
            history_predictor=_StorageFailingHistoryPredictor(),
            prediction_engine=PredictionEngine(db_path=Path(temp_dir) / "route74.sqlite"),
            clock=lambda: current_time,
        )
        decision = service.build_decision(MORNING, 12)

    _assert_equal(decision.source, DepartureSource.YANDEX)
    _assert_equal(decision.arrival_in_minutes, 18)
    _assert_equal(decision.yandex_history.available, False)
    _assert_equal(decision.yandex_history.fallback_reason, "history_error:OperationalError")


class _TrustedYandexSource:
    def get_forecast(self, *_args: object) -> YandexLiveForecast:
        return YandexLiveForecast(
            enabled=True,
            available=True,
            source_method=YandexSourceMethod.VEHICLE_PREDICTION,
            status=YandexSourceStatus.OK,
            arrival_minutes=(18,),
            newest_age_seconds=0,
            confidence=EtaConfidence.HIGH,
        )


class _StorageFailingHistoryPredictor:
    def predict_at(self, *_args: object) -> object:
        raise sqlite3.OperationalError("history db is temporarily unavailable")


def _assert_auto_profile_window_seconds() -> None:
    _assert_rejects(lambda: choose_profile_for_time(datetime(2026, 6, 4, 7, 0)), "timezone-aware")
    _assert_rejects(
        lambda: choose_profile_for_time(datetime(2026, 6, 4, 7, 0, tzinfo=timezone.utc)),
        "Asia/Novosibirsk",
    )
    _assert_equal(_auto_profile_key(10, 59, 59), "morning")
    _assert_equal(_auto_profile_key(22, 59, 59), "evening")
    _assert_equal(_auto_profile_key(11, 0, 1), None)


def _auto_profile_key(hour: int, minute: int, second: int) -> str | None:
    current_time = datetime(2026, 6, 4, hour, minute, second, tzinfo=NOVOSIBIRSK_TZ)
    profile = choose_profile_for_time(current_time)
    return profile.key if profile is not None else None


def _assert_rejects(factory: Callable[[], object], expected: str) -> None:
    try:
        factory()
    except ValueError as error:
        if expected not in str(error):
            raise AssertionError(f"expected {expected!r} in {str(error)!r}") from error
    else:
        raise AssertionError(f"expected validation error: {expected}")


def _assert_equal(actual: object, expected: object) -> None:
    if actual != expected:
        raise AssertionError(f"expected {expected!r}, got {actual!r}")


if __name__ == "__main__":
    main()
