from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from route74.domain.eta import (
    MAX_ETA_WARNING_LENGTH,
    EtaConfidence,
    EtaConsensus,
    EtaEstimate,
    EtaFactor,
    EtaFactorKind,
    EtaSource,
)
from route74.domain.profiles import MORNING
from route74.domain.yandex_history import YandexHistoryPrediction
from route74.models import NOVOSIBIRSK_TZ
from route74.presenters.calculation import format_calculation_explanation
from route74.presenters.commute import format_action_message
from route74.presenters.commute_lines import format_duration_minutes
from route74.presenters.eta_factors import eta_factor_texts
from route74.services.commute import CommuteService
from route74.services.prediction_engine import PredictionEngine
from route74.sources.yandex.models import YandexLiveForecast, YandexSourceMethod, YandexSourceStatus, YandexVehicle


class FakeYandexSource:
    def get_forecast(self, *_args: object) -> YandexLiveForecast:
        return YandexLiveForecast(
            enabled=True,
            available=True,
            source_method=YandexSourceMethod.VEHICLE_PREDICTION,
            status=YandexSourceStatus.OK,
            arrival_minutes=(8, 15),
            vehicles=(
                YandexVehicle(vehicle_id="route74-live-1", lat=54.85, lng=83.10, arrival_minutes=8, age_seconds=20),
                YandexVehicle(vehicle_id="route74-live-2", lat=54.86, lng=83.11, arrival_minutes=15, age_seconds=20),
            ),
            vehicle_count=2,
            newest_age_seconds=20,
            confidence=EtaConfidence.HIGH,
        )


class FakeUntrustedThreadSource:
    def get_forecast(self, *_args: object) -> YandexLiveForecast:
        return YandexLiveForecast(
            enabled=True,
            available=False,
            source_method=YandexSourceMethod.VEHICLE_PREDICTION,
            status=YandexSourceStatus.NO_TARGET,
            confidence=EtaConfidence.LOW,
            fallback_reason="vehicle_prediction_thread_fallback:not_found:2161326768",
            raw_status="vehicle_prediction_thread_fallback",
        )


class FakeInvalidEtaSource:
    def get_forecast(self, *_args: object) -> YandexLiveForecast:
        return YandexLiveForecast(
            enabled=True,
            available=True,
            source_method=YandexSourceMethod.VEHICLE_PREDICTION,
            status=YandexSourceStatus.OK,
            arrival_minutes=(-1,),
            vehicles=(YandexVehicle("invalid-eta", lat=54.85, lng=83.10, arrival_minutes=-1),),
            vehicle_count=1,
            newest_age_seconds=0,
            confidence=EtaConfidence.HIGH,
        )


class FakeInvalidAgeSource:
    def get_forecast(self, *_args: object) -> YandexLiveForecast:
        return YandexLiveForecast(
            enabled=True,
            available=True,
            source_method=YandexSourceMethod.VEHICLE_PREDICTION,
            status=YandexSourceStatus.OK,
            arrival_minutes=(8,),
            vehicles=(YandexVehicle("bad-age", lat=54.85, lng=83.10, arrival_minutes=8),),
            vehicle_count=1,
            newest_age_seconds=-5,
            confidence=EtaConfidence.HIGH,
        )


class FakeStaleYandexSource:
    def get_forecast(self, *_args: object) -> YandexLiveForecast:
        return YandexLiveForecast(
            enabled=True,
            available=True,
            source_method=YandexSourceMethod.VEHICLE_PREDICTION,
            status=YandexSourceStatus.OK,
            arrival_minutes=(6,),
            vehicles=(YandexVehicle("stale-live", lat=54.85, lng=83.10, arrival_minutes=6, age_seconds=600),),
            vehicle_count=1,
            newest_age_seconds=600,
            confidence=EtaConfidence.HIGH,
        )


class EarlyHistoryPredictor:
    def predict_at(self, *_args: object) -> YandexHistoryPrediction:
        return YandexHistoryPrediction(
            available=True,
            arrival_minutes=1,
            sample_count=120,
            bucket_minutes=15,
            window_days=30,
            percentile=80,
            fallback_reason="",
        )


class FallbackHistoryPredictor:
    def predict_at(self, *_args: object) -> YandexHistoryPrediction:
        return YandexHistoryPrediction(
            available=True,
            arrival_minutes=18,
            sample_count=120,
            bucket_minutes=30,
            window_days=14,
            percentile=80,
            fallback_reason="",
        )


class SparseHistoryPredictor:
    def predict_at(self, *_args: object) -> YandexHistoryPrediction:
        return YandexHistoryPrediction.unavailable(
            sample_count=12,
            bucket_minutes=30,
            window_days=14,
            reason="insufficient_history:12/20;days:1/3",
        )


def main() -> None:
    _assert_duration_format_guards()
    _assert_calculation_explanation_contract()
    _assert_eta_warning_guards()

    message = _message_for(FakeYandexSource(), walk_minutes=5)
    _assert_equal(
        [line for line in message.splitlines() if line][:4],
        [
            "🧥 СОБИРАЙСЯ",
            "🎯 Коротко: выйти 07:01 · 74-й 07:08 · ждать ~2 мин",
            "🕒 Сейчас: 07:00",
            "🏠 Дом -> Академ",
        ],
    )

    _assert_contains(message, "🧥 СОБИРАЙСЯ")
    _assert_contains(message, "🕒 Сейчас: 07:00")
    _assert_contains(message, "🏠 Дом -> Академ")
    _assert_contains(message, "📌 Надёжность: высокое · Яндекс")
    _assert_contains(message, "🟡 Яндекс 74: данные есть · машина на карте · свежесть 20 сек назад · основной источник")
    _assert_contains(message, "🧭 План выхода:")
    _assert_contains(message, "• 07:01 - выйти (можно подождать 1 мин дома)")
    _assert_contains(message, "• 07:08 - 74-й")
    _assert_contains(message, "✅ Итог: ждать у остановки ~2 мин")
    _assert_contains(message, "🚶 Твой путь до остановки: 5 мин")
    _assert_not_contains(message, "🕒 Выходить:")
    _assert_not_contains(message, "🟡 Яндекс 74: 07:08 (через 8 мин)")
    _assert_not_contains(message, "Плановое расписание")

    untrusted = _message_for(FakeUntrustedThreadSource(), walk_minutes=5)
    _assert_equal(
        [line for line in untrusted.splitlines() if line][:4],
        [
            "⚠️ ТОЧНОГО СИГНАЛА НЕТ",
            "🎯 Коротко: точного ETA нет · обнови прогноз или открой карту 74",
            "🕒 Сейчас: 07:00",
            "🏠 Дом -> Академ",
        ],
    )
    _assert_contains(untrusted, "⚠️ ТОЧНОГО СИГНАЛА НЕТ")
    _assert_contains(untrusted, "📡 74-й не виден, точного ETA нет")
    _assert_contains(untrusted, "📌 Действие: обнови прогноз через минуту или открой карту 74")
    _assert_contains(
        untrusted,
        "⚠️ Яндекс: нет нашей остановки в прогнозе (нужное направление не найдено) · беру нет точного ETA",
    )
    _assert_contains(
        untrusted,
        "📈 История Яндекса: недоступна · похожее время профиля · история не подключена в этом режиме",
    )
    _assert_not_contains(untrusted, "🟡 Яндекс 74: машина на карте")

    sparse_history = _message_for(
        FakeUntrustedThreadSource(),
        walk_minutes=5,
        history_predictor=SparseHistoryPredictor(),
    )
    _assert_contains(
        sparse_history,
        "📈 История Яндекса: данных мало · 12/20 замеров, 1/3 дней · похожее время профиля",
    )
    _assert_not_contains(sparse_history, "insufficient_history")

    invalid_eta = _message_for(FakeInvalidEtaSource(), walk_minutes=5)
    _assert_contains(invalid_eta, "⚠️ ТОЧНОГО СИГНАЛА НЕТ")
    _assert_contains(
        invalid_eta,
        "⚠️ Яндекс: дал только координаты (ETA некорректный) · беру нет точного ETA",
    )
    _assert_not_contains(invalid_eta, "🟡 Яндекс 74: машина на карте")

    invalid_age = _message_for(FakeInvalidAgeSource(), walk_minutes=5)
    _assert_contains(invalid_age, "🟡 Яндекс 74: данные есть · машина на карте")
    _assert_not_contains(invalid_age, "свежесть -5 сек назад")
    _assert_not_contains(invalid_age, "свежесть")

    live_with_history_conflict = _message_for(
        FakeYandexSource(),
        walk_minutes=5,
        history_predictor=EarlyHistoryPredictor(),
    )
    _assert_contains(live_with_history_conflict, "📌 Надёжность: высокое · Яндекс")
    _assert_contains(
        live_with_history_conflict,
        "🧪 Почему: история на 7 мин раньше не выбрана, 120 замеров",
    )

    stale_live_with_history = _message_for(
        FakeStaleYandexSource(),
        walk_minutes=5,
        history_predictor=FallbackHistoryPredictor(),
    )
    _assert_contains(stale_live_with_history, "🎯 Коротко: выйти 07:07 · 74-й 07:18 · ждать ~6 мин")
    _assert_contains(
        stale_live_with_history,
        "🧪 Почему: live ETA 6 мин не выбрал: данные устарели; история p80: 120 замеров",
    )
    _assert_contains(
        stale_live_with_history,
        "🟡 Яндекс 74: данные есть · машина на карте · свежесть 10 мин назад",
    )
    _assert_contains(
        stale_live_with_history,
        "📈 История Яндекса: 07:18 (через 18 мин) · 120 замеров · окно ±30 мин",
    )
    print("OK | commute presentation smoke passed")


def _assert_duration_format_guards() -> None:
    _assert_equal(format_duration_minutes(0), "0 мин")
    _assert_equal(format_duration_minutes(61), "1ч 1 мин")
    _assert_value_error(lambda: format_duration_minutes(-1), "non-negative")
    _assert_value_error(lambda: format_duration_minutes(True), "non-negative")  # type: ignore[arg-type]
    _assert_value_error(lambda: format_duration_minutes(1.5), "non-negative")  # type: ignore[arg-type]


def _assert_calculation_explanation_contract() -> None:
    text = format_calculation_explanation(12, 17)
    _assert_contains(text, "Порядок источников: свежий Яндекс у остановки -> история Яндекса -> нет точного ETA.")
    _assert_contains(text, "история Яндекса: запасной ориентир")
    _assert_contains(text, "не подмешиваю расписание")
    _assert_contains(text, "🌅 утром: 12 мин (дом -> улица + пешком + запас).")
    _assert_contains(text, "🌆 вечером: 17 мин (2ГИС 8 + выйти из здания + запас).")
    _assert_not_contains(text, "live")


def _assert_eta_warning_guards() -> None:
    EtaConsensus(EtaSource.YANDEX, 8, EtaConfidence.MEDIUM, 3, None, "слабый ETA")
    _assert_value_error(
        lambda: EtaConsensus(EtaSource.YANDEX, 8, EtaConfidence.MEDIUM, 3, None, "слабый ETA\nspoofed"),
        "single-line",
    )
    _assert_value_error(
        lambda: EtaConsensus(EtaSource.YANDEX, 8, EtaConfidence.MEDIUM, 3, None, "  слабый ETA  "),
        "single-line",
    )
    _assert_value_error(
        lambda: EtaConsensus(
            EtaSource.YANDEX,
            8,
            EtaConfidence.MEDIUM,
            3,
            None,
            "x" * (MAX_ETA_WARNING_LENGTH + 1),
        ),
        "single-line",
    )
    _assert_value_error(
        lambda: EtaConsensus(
            EtaSource.YANDEX,
            8,
            EtaConfidence.MEDIUM,
            3,
            1,
            "",
            estimates=(
                EtaEstimate(EtaSource.YANDEX, 8),
                EtaEstimate(EtaSource.YANDEX, 9),
            ),
        ),
        "duplicate ETA consensus estimate source",
    )
    _assert_equal(
        eta_factor_texts(
            (
                EtaFactor(
                    EtaFactorKind.SAFETY_BUFFER,
                    minutes=2,
                    sample_count=3,
                    percent=100,
                    scope="bot_runtime_bucket",
                ),
            )
        ),
        ("запас +2 мин, промахи 100%, 3 замера, по похожим ответам бота",),
    )
    _assert_equal(
        eta_factor_texts(
            (
                EtaFactor(
                    EtaFactorKind.HISTORY_DISAGREEMENT,
                    minutes=8,
                    sample_count=120,
                    scope="history_earlier",
                ),
            )
        ),
        ("история на 8 мин раньше не выбрана, 120 замеров",),
    )
    _assert_equal(
        eta_factor_texts((EtaFactor(EtaFactorKind.GUARDRAIL_UNAVAILABLE),)),
        ("прошлые поправки недоступны",),
    )
    _assert_equal(
        eta_factor_texts(
            (
                EtaFactor(
                    EtaFactorKind.IGNORED_WEAK_PROGRESS,
                    minutes=1,
                    sample_count=2,
                    scope="vehicle_progress",
                ),
            )
        ),
        ("слабая координата на 1 мин раньше не выбрана, 2 замера",),
    )
    _assert_equal(
        eta_factor_texts(
            (
                EtaFactor(
                    EtaFactorKind.IGNORED_LIVE_ETA,
                    minutes=6,
                    scope="stale",
                ),
            )
        ),
        ("live ETA 6 мин не выбрал: данные устарели",),
    )


def _message_for(source: object, *, walk_minutes: int, history_predictor: object | None = None) -> str:
    with TemporaryDirectory() as temp_dir:
        service = CommuteService(
            yandex_source=source,
            history_predictor=history_predictor,
            prediction_engine=PredictionEngine(db_path=Path(temp_dir) / "route74.sqlite"),
            clock=lambda: datetime(2026, 6, 4, 7, 0, tzinfo=NOVOSIBIRSK_TZ),
        )
        return format_action_message(service.build_decision(MORNING, walk_minutes))


def _assert_contains(text: str, expected: str) -> None:
    if expected not in text:
        raise AssertionError(f"expected {expected!r} in {text!r}")


def _assert_not_contains(text: str, unexpected: str) -> None:
    if unexpected in text:
        raise AssertionError(f"did not expect {unexpected!r} in {text!r}")


def _assert_equal(actual: object, expected: object) -> None:
    if actual != expected:
        raise AssertionError(f"expected {expected!r}, got {actual!r}")


def _assert_value_error(action: Callable[[], object], expected: str) -> None:
    try:
        action()
    except ValueError as error:
        _assert_contains(str(error), expected)
    else:
        raise AssertionError(f"expected ValueError containing {expected!r}")


if __name__ == "__main__":
    main()
