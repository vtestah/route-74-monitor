from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from route74.domain.commute import CommuteSnapshot, DepartureSource, DepartureUrgency
from route74.domain.departure_safety import (
    missed_by_minutes,
    physical_catch_margin_minutes,
    safe_catch_margin_minutes,
    unsafe_arrival_without_safe_margin,
)
from route74.domain.eta import EtaConfidence, EtaConsensus, EtaEstimate, EtaSource
from route74.domain.profiles import MORNING
from route74.models import NOVOSIBIRSK_TZ
from route74.presenters.commute import format_action_message
from route74.services.arrival_planning import ArrivalPlan, plan_arrival
from route74.services.commute import CommuteService
from route74.services.departure import build_departure_decision, choose_profile_for_time
from route74.services.prediction_engine import PredictionEngine
from route74.sources.yandex.models import YandexLiveForecast, YandexSourceMethod, YandexSourceStatus, YandexVehicle


class FakeMissedFirstYandexSource:
    def get_forecast(self, *_args: object) -> YandexLiveForecast:
        return _forecast((5, 23), "route74-live")


class FakeTightFirstYandexSource:
    def get_forecast(self, *_args: object) -> YandexLiveForecast:
        return _forecast((12, 20), "route74-tight")


class FakeUnsafeShortYandexSource:
    def get_forecast(self, *_args: object) -> YandexLiveForecast:
        return YandexLiveForecast(
            enabled=True,
            available=True,
            source_method=YandexSourceMethod.STOP_INFO,
            status=YandexSourceStatus.OK,
            arrival_minutes=(8,),
            vehicles=(YandexVehicle("unsafe-short", arrival_minutes=8),),
            vehicle_count=1,
            newest_age_seconds=0,
            confidence=EtaConfidence.MEDIUM,
            fallback_reason="stop_estimated",
        )


def main() -> None:
    _assert_auto_profile_windows()
    _assert_departure_safety_rejects_invalid_minutes()
    _assert_arrival_plan_contract()
    _assert_next_live_context_skips_selected_live_eta()
    _assert_corrected_live_context_skips_raw_source_eta()
    _assert_vehicle_progress_context_skips_earlier_live_eta()
    _assert_promotion_replaces_stale_live_estimate()
    _assert_corrected_live_promotion_skips_raw_source_eta()

    promoted = _message(FakeMissedFirstYandexSource(), walk_minutes=12)
    _assert_contains(promoted, "✅ ПОКА ЖДИ ДОМА")
    _assert_contains(promoted, "🎯 Коротко: выйти 07:09 · 74-й 07:23 · ждать ~2 мин")
    _assert_contains(promoted, "• 07:09 - выйти (можно подождать 9 мин дома)")
    _assert_contains(promoted, "ближайший 74-й уже не успеть, планирую следующий")
    _assert_not_contains(promoted, "❌ НА ЭТОТ 74-Й УЖЕ НЕ УСПЕЕШЬ")

    safe_promoted = _message(FakeTightFirstYandexSource(), walk_minutes=12)
    _assert_contains(safe_promoted, "✅ ПОКА ЖДИ ДОМА")
    _assert_contains(safe_promoted, "🎯 Коротко: выйти 07:06 · 74-й 07:20 · ждать ~2 мин")
    _assert_contains(safe_promoted, "• 07:06 - выйти (можно подождать 6 мин дома)")
    _assert_contains(safe_promoted, "ближайший 74-й уже не успеть, планирую следующий")
    _assert_not_contains(safe_promoted, "⚠️ Выходи сейчас: впритык")

    unsafe_short = _message(FakeUnsafeShortYandexSource(), walk_minutes=6)
    _assert_contains(unsafe_short, "❌ НА ЭТОТ 74-Й УЖЕ НЕ УСПЕЕШЬ")
    _assert_contains(unsafe_short, "безопасный запас меньше на 6 мин")
    _assert_contains(unsafe_short, "короткий ETA без координаты машины")
    _assert_not_contains(unsafe_short, "🏃 ВЫХОДИ СЕЙЧАС")
    _assert_not_contains(unsafe_short, "✅ Выходи сейчас")

    unsafe_progress = _unsafe_vehicle_progress_message(walk_minutes=6)
    _assert_contains(unsafe_progress, "❌ НА ЭТОТ 74-Й УЖЕ НЕ УСПЕЕШЬ")
    _assert_contains(unsafe_progress, "безопасный запас меньше на 5 мин")
    _assert_contains(unsafe_progress, "координатный прогноз, держу запас 2 мин")
    _assert_contains(unsafe_progress, "🎯 Коротко: этот уйдёт 07:08 · ты у остановки 07:06")
    _assert_contains(unsafe_progress, "• 07:08 - этот 74-й уйдёт")
    _assert_not_contains(unsafe_progress, "🏃 ВЫХОДИ СЕЙЧАС")
    _assert_not_contains(unsafe_progress, "⚠️ Выходи сейчас: впритык")

    unsafe_progress_with_next_live = _unsafe_vehicle_progress_with_next_live_message(walk_minutes=6)
    _assert_contains(unsafe_progress_with_next_live, "❌ НА ЭТОТ 74-Й УЖЕ НЕ УСПЕЕШЬ")
    _assert_contains(
        unsafe_progress_with_next_live,
        "🎯 Коротко: этот уйдёт 07:08 · ты у остановки 07:06 · следующая 07:22",
    )
    _assert_contains(unsafe_progress_with_next_live, "🎯 Следующая цель: 07:22 · выходить около 07:09")
    _assert_not_contains(unsafe_progress_with_next_live, "🎯 Следующая цель: пока не вижу")

    unsafe_history = _unsafe_history_message(walk_minutes=12)
    _assert_contains(unsafe_history, "❌ НА ЭТОТ 74-Й УЖЕ НЕ УСПЕЕШЬ")
    _assert_contains(unsafe_history, "безопасный запас меньше на 3 мин")
    _assert_contains(unsafe_history, "🎯 Коротко: этот уйдёт 07:15 · ты у остановки 07:12")
    _assert_contains(unsafe_history, "• 07:15 - этот 74-й уйдёт")
    _assert_not_contains(unsafe_history, "✅ Выходи сейчас")
    print("OK | arrival planning smoke passed")


def _assert_auto_profile_windows() -> None:
    _assert_equal(_auto_profile_key(5, 59), None)
    _assert_equal(_auto_profile_key(6, 0), "morning")
    _assert_equal(_auto_profile_key(10, 59), "morning")
    _assert_equal(_auto_profile_key(11, 0), None)
    _assert_equal(_auto_profile_key(16, 59), None)
    _assert_equal(_auto_profile_key(17, 0), "evening")
    _assert_equal(_auto_profile_key(22, 59), "evening")
    _assert_equal(_auto_profile_key(23, 0), None)


def _auto_profile_key(hour: int, minute: int) -> str | None:
    current_time = datetime(2026, 6, 4, hour, minute, tzinfo=NOVOSIBIRSK_TZ)
    profile = choose_profile_for_time(current_time)
    return profile.key if profile is not None else None


def _assert_departure_safety_rejects_invalid_minutes() -> None:
    decision = SimpleNamespace(
        arrival_in_minutes=True,
        walk_minutes=12,
        source=DepartureSource.YANDEX,
        eta_consensus=EtaConsensus(
            selected_source=EtaSource.YANDEX,
            arrival_minutes=8,
            confidence=EtaConfidence.LOW,
            target_wait_minutes=5,
            spread_minutes=None,
            warning="",
        ),
    )
    _assert_equal(physical_catch_margin_minutes(decision), None)
    _assert_equal(safe_catch_margin_minutes(decision), None)
    _assert_equal(missed_by_minutes(decision), None)
    _assert_equal(unsafe_arrival_without_safe_margin(decision), False)

    no_consensus_decision = SimpleNamespace(
        arrival_in_minutes=8,
        walk_minutes=6,
        source=DepartureSource.YANDEX,
        eta_consensus=None,
    )
    _assert_equal(physical_catch_margin_minutes(no_consensus_decision), 2)
    _assert_equal(safe_catch_margin_minutes(no_consensus_decision), None)
    _assert_equal(missed_by_minutes(no_consensus_decision), None)
    _assert_equal(unsafe_arrival_without_safe_margin(no_consensus_decision), False)

    malformed_consensus_decision = SimpleNamespace(
        arrival_in_minutes=8,
        walk_minutes=6,
        source=DepartureSource.YANDEX,
        eta_consensus=SimpleNamespace(confidence=EtaConfidence.LOW, target_wait_minutes=True),
    )
    _assert_equal(safe_catch_margin_minutes(malformed_consensus_decision), None)
    _assert_equal(missed_by_minutes(malformed_consensus_decision), None)
    _assert_equal(unsafe_arrival_without_safe_margin(malformed_consensus_decision), False)


def _assert_arrival_plan_contract() -> None:
    valid = ArrivalPlan(
        source=EtaSource.YANDEX,
        arrival_minutes=10,
        next_live_minutes=(18, 25),
        eta_consensus=_consensus(EtaSource.YANDEX, 10),
    )
    _assert_equal(valid.next_live_minutes, (18, 25))

    _assert_rejects(
        lambda: ArrivalPlan(
            source="yandex",  # type: ignore[arg-type]
            arrival_minutes=10,
            next_live_minutes=(),
            eta_consensus=_consensus(EtaSource.YANDEX, 10),
        ),
        "source needs EtaSource",
    )
    _assert_rejects(
        lambda: ArrivalPlan(
            source=EtaSource.YANDEX,
            arrival_minutes=True,  # type: ignore[arg-type]
            next_live_minutes=(),
            eta_consensus=_consensus(EtaSource.YANDEX, 10),
        ),
        "arrival minutes",
    )
    _assert_rejects(
        lambda: ArrivalPlan(
            source=EtaSource.YANDEX,
            arrival_minutes=10,
            next_live_minutes=[18],  # type: ignore[arg-type]
            eta_consensus=_consensus(EtaSource.YANDEX, 10),
        ),
        "next live minutes",
    )
    _assert_rejects(
        lambda: ArrivalPlan(
            source=EtaSource.YANDEX,
            arrival_minutes=10,
            next_live_minutes=(18, 17),
            eta_consensus=_consensus(EtaSource.YANDEX, 10),
        ),
        "strictly increasing",
    )
    _assert_rejects(
        lambda: ArrivalPlan(
            source=EtaSource.YANDEX,
            arrival_minutes=10,
            next_live_minutes=(10, 18),
            eta_consensus=_consensus(EtaSource.YANDEX, 10),
        ),
        "after selected arrival",
    )
    _assert_rejects(
        lambda: ArrivalPlan(
            source=EtaSource.YANDEX,
            arrival_minutes=10,
            next_live_minutes=(),
            eta_consensus=object(),  # type: ignore[arg-type]
        ),
        "ETA consensus",
    )
    _assert_rejects(
        lambda: ArrivalPlan(
            source=EtaSource.YANDEX,
            arrival_minutes=10,
            next_live_minutes=(),
            eta_consensus=_consensus(EtaSource.YANDEX_HISTORY, 10),
        ),
        "source must match ETA consensus",
    )
    _assert_rejects(
        lambda: ArrivalPlan(
            source=EtaSource.YANDEX,
            arrival_minutes=10,
            next_live_minutes=(),
            eta_consensus=_consensus(EtaSource.YANDEX, 11),
        ),
        "arrival must match ETA consensus",
    )


def _consensus(source: EtaSource, arrival_minutes: int) -> EtaConsensus:
    return EtaConsensus(
        selected_source=source,
        arrival_minutes=arrival_minutes,
        confidence=EtaConfidence.HIGH,
        target_wait_minutes=2,
        spread_minutes=None,
        warning="",
    )


def _assert_next_live_context_skips_selected_live_eta() -> None:
    current_time = datetime(2026, 6, 4, 7, 0, tzinfo=NOVOSIBIRSK_TZ)
    consensus = EtaConsensus(
        selected_source=EtaSource.YANDEX,
        arrival_minutes=15,
        confidence=EtaConfidence.HIGH,
        target_wait_minutes=2,
        spread_minutes=None,
        warning="",
    )
    plan = plan_arrival(
        CommuteSnapshot(
            profile=MORNING,
            current_time=current_time,
            walk_minutes=5,
            eta_consensus=consensus,
            yandex_forecast=_forecast((8, 15, 28), "route74-live-context"),
        ),
        consensus,
    )

    _assert_equal(plan.arrival_minutes, 15)
    _assert_equal(plan.next_live_minutes, (28,))


def _assert_corrected_live_context_skips_raw_source_eta() -> None:
    current_time = datetime(2026, 6, 4, 7, 0, tzinfo=NOVOSIBIRSK_TZ)
    consensus = EtaConsensus(
        selected_source=EtaSource.YANDEX_CORRECTED,
        arrival_minutes=6,
        confidence=EtaConfidence.MEDIUM,
        target_wait_minutes=2,
        spread_minutes=None,
        warning="ETA сдвинут на 2 мин раньше",
    )
    plan = plan_arrival(
        CommuteSnapshot(
            profile=MORNING,
            current_time=current_time,
            walk_minutes=3,
            eta_consensus=consensus,
            yandex_forecast=_forecast((8, 15, 28), "route74-corrected-context"),
        ),
        consensus,
    )

    _assert_equal(plan.arrival_minutes, 6)
    _assert_equal(plan.next_live_minutes, (15, 28))


def _assert_vehicle_progress_context_skips_earlier_live_eta() -> None:
    current_time = datetime(2026, 6, 4, 7, 0, tzinfo=NOVOSIBIRSK_TZ)
    consensus = EtaConsensus(
        selected_source=EtaSource.VEHICLE_PROGRESS,
        arrival_minutes=10,
        confidence=EtaConfidence.LOW,
        target_wait_minutes=7,
        spread_minutes=None,
        warning="координатный прогноз, держу запас 2 мин",
    )
    plan = plan_arrival(
        CommuteSnapshot(
            profile=MORNING,
            current_time=current_time,
            walk_minutes=6,
            eta_consensus=consensus,
            yandex_forecast=_forecast((6, 8, 14, 18), "route74-progress-context"),
        ),
        consensus,
    )

    _assert_equal(plan.arrival_minutes, 10)
    _assert_equal(plan.next_live_minutes, (14, 18))


def _assert_promotion_replaces_stale_live_estimate() -> None:
    current_time = datetime(2026, 6, 4, 7, 0, tzinfo=NOVOSIBIRSK_TZ)
    consensus = EtaConsensus(
        selected_source=EtaSource.YANDEX,
        arrival_minutes=5,
        confidence=EtaConfidence.MEDIUM,
        target_wait_minutes=2,
        spread_minutes=4,
        warning="источники немного расходятся",
        estimates=(
            EtaEstimate(EtaSource.YANDEX, 5),
            EtaEstimate(EtaSource.VEHICLE_PROGRESS, 9),
        ),
    )
    plan = plan_arrival(
        CommuteSnapshot(
            profile=MORNING,
            current_time=current_time,
            walk_minutes=12,
            eta_consensus=consensus,
            yandex_forecast=_forecast((5, 23), "route74-promoted-consensus"),
        ),
        consensus,
    )

    _assert_equal(plan.source, EtaSource.YANDEX)
    _assert_equal(plan.arrival_minutes, 23)
    _assert_equal(plan.eta_consensus.arrival_minutes, 23)
    _assert_equal(plan.eta_consensus.spread_minutes, None)
    _assert_equal(
        plan.eta_consensus.estimates,
        (
            EtaEstimate(EtaSource.YANDEX, 23),
            EtaEstimate(EtaSource.VEHICLE_PROGRESS, 9),
        ),
    )
    _assert_contains(plan.eta_consensus.warning, "ближайший 74-й уже не успеть")
    _assert_contains(plan.eta_consensus.warning, "источники немного расходятся")


def _assert_corrected_live_promotion_skips_raw_source_eta() -> None:
    current_time = datetime(2026, 6, 4, 7, 0, tzinfo=NOVOSIBIRSK_TZ)
    consensus = EtaConsensus(
        selected_source=EtaSource.YANDEX_CORRECTED,
        arrival_minutes=6,
        confidence=EtaConfidence.MEDIUM,
        target_wait_minutes=2,
        spread_minutes=None,
        warning="ETA сдвинут на 2 мин раньше",
    )

    no_next_plan = plan_arrival(
        CommuteSnapshot(
            profile=MORNING,
            current_time=current_time,
            walk_minutes=5,
            eta_consensus=consensus,
            yandex_forecast=_forecast((8,), "route74-corrected-promotion-single"),
        ),
        consensus,
    )
    _assert_equal(no_next_plan.arrival_minutes, 6)
    _assert_equal(no_next_plan.next_live_minutes, ())

    next_plan = plan_arrival(
        CommuteSnapshot(
            profile=MORNING,
            current_time=current_time,
            walk_minutes=5,
            eta_consensus=consensus,
            yandex_forecast=_forecast((8, 16, 24), "route74-corrected-promotion-next"),
        ),
        consensus,
    )
    _assert_equal(next_plan.source, EtaSource.YANDEX)
    _assert_equal(next_plan.arrival_minutes, 16)
    _assert_equal(next_plan.next_live_minutes, (24,))


def _message(source: object, *, walk_minutes: int) -> str:
    with TemporaryDirectory() as temp_dir:
        service = CommuteService(
            yandex_source=source,
            prediction_engine=PredictionEngine(db_path=Path(temp_dir) / "route74.sqlite"),
            clock=lambda: datetime(2026, 6, 4, 7, 0, tzinfo=NOVOSIBIRSK_TZ),
        )
        return format_action_message(service.build_decision(MORNING, walk_minutes))


def _unsafe_vehicle_progress_message(*, walk_minutes: int) -> str:
    current_time = datetime(2026, 6, 4, 7, 0, tzinfo=NOVOSIBIRSK_TZ)
    decision = build_departure_decision(
        CommuteSnapshot(
            profile=MORNING,
            current_time=current_time,
            walk_minutes=walk_minutes,
            eta_consensus=EtaConsensus(
                selected_source=EtaSource.VEHICLE_PROGRESS,
                arrival_minutes=8,
                confidence=EtaConfidence.LOW,
                target_wait_minutes=7,
                spread_minutes=None,
                warning="координатный прогноз, держу запас 2 мин",
            ),
        )
    )
    return format_action_message(decision)


def _unsafe_vehicle_progress_with_next_live_message(*, walk_minutes: int) -> str:
    current_time = datetime(2026, 6, 4, 7, 0, tzinfo=NOVOSIBIRSK_TZ)
    decision = build_departure_decision(
        CommuteSnapshot(
            profile=MORNING,
            current_time=current_time,
            walk_minutes=walk_minutes,
            eta_consensus=EtaConsensus(
                selected_source=EtaSource.VEHICLE_PROGRESS,
                arrival_minutes=8,
                confidence=EtaConfidence.LOW,
                target_wait_minutes=7,
                spread_minutes=None,
                warning="координатный прогноз, держу запас 2 мин",
            ),
            yandex_forecast=_forecast((9, 22), "route74-next-live"),
        )
    )
    return format_action_message(decision)


def _unsafe_history_message(*, walk_minutes: int) -> str:
    current_time = datetime(2026, 6, 4, 7, 0, tzinfo=NOVOSIBIRSK_TZ)
    decision = build_departure_decision(
        CommuteSnapshot(
            profile=MORNING,
            current_time=current_time,
            walk_minutes=walk_minutes,
            eta_consensus=EtaConsensus(
                selected_source=EtaSource.YANDEX_HISTORY,
                arrival_minutes=15,
                confidence=EtaConfidence.LOW,
                target_wait_minutes=6,
                spread_minutes=None,
                warning="",
            ),
        )
    )
    return format_action_message(decision)


def _forecast(minutes: tuple[int, ...], vehicle_prefix: str) -> YandexLiveForecast:
    return YandexLiveForecast(
        enabled=True,
        available=True,
        source_method=YandexSourceMethod.VEHICLE_PREDICTION,
        status=YandexSourceStatus.OK,
        arrival_minutes=minutes,
        vehicles=tuple(
            YandexVehicle(
                f"{vehicle_prefix}-{index}",
                lat=54.85 + index / 100,
                lng=83.10 + index / 100,
                arrival_minutes=arrival_minutes,
                age_seconds=20,
            )
            for index, arrival_minutes in enumerate(minutes, start=1)
        ),
        vehicle_count=len(minutes),
        newest_age_seconds=20,
        confidence=EtaConfidence.HIGH,
    )


def _assert_contains(text: str, expected: str) -> None:
    if expected not in text:
        raise AssertionError(f"expected {expected!r} in {text!r}")


def _assert_not_contains(text: str, unexpected: str) -> None:
    if unexpected in text:
        raise AssertionError(f"did not expect {unexpected!r} in {text!r}")


def _assert_equal(actual: object, expected: object) -> None:
    if actual != expected:
        raise AssertionError(f"expected {expected!r}, got {actual!r}")


def _assert_rejects(action: Callable[[], object], expected: str) -> None:
    try:
        action()
    except ValueError as error:
        if expected not in str(error):
            raise AssertionError(f"expected {expected!r} in {str(error)!r}") from error
    else:
        raise AssertionError(f"expected {expected!r} validation error")


if __name__ == "__main__":
    main()
