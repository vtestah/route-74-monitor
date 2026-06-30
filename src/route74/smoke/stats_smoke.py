from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from route74.build_info import BuildInfo
from route74.cli.bot_latency import format_bot_latency_summary
from route74.cli.formatting import counts_text
from route74.diagnostics import sanitize_diagnostic_text
from route74.domain.commute import DepartureDecision, DepartureSource, DepartureUrgency
from route74.domain.eta import EtaConfidence, EtaConsensus, EtaEstimate, EtaFactor, EtaFactorKind, EtaSource
from route74.domain.prediction_sources import SOURCE_HISTORY_HEADWAY, SOURCE_TARGET_STOP_LIVE
from route74.domain.profiles import EVENING, MORNING
from route74.domain.runtime_sources import BOT_EVENT_USER_REPLY, RUNTIME_SOURCE_WEB_APP
from route74.domain.yandex_history import YandexHistoryPrediction, YandexHistoryScope
from route74.models import NOVOSIBIRSK_TZ
from route74.presenters.eta_factors import eta_factor_texts
from route74.presenters.stats import format_stats_message
from route74.services.stats import StatsService, StatsSnapshot, _telemetry_error
from route74.sources.yandex.models import YandexLiveForecast, YandexSourceMethod, YandexSourceStatus
from route74.storage.bot_latency import (
    BotInteractionEvent,
    BotLatencySummary,
    insert_bot_interaction_event,
    summarize_bot_latency,
)
from route74.storage.connection import connect, init_db
from route74.storage.forecast_backtest import ForecastBacktestResult, ForecastBacktestSummary
from route74.storage.forecast_health import ForecastCollectorHealth, ForecastHealthSummary, ForecastWindowHealth
from route74.storage.models import (
    CollectorHeartbeat,
    CollectorRunSummary,
    CountByKey,
    ForecastReadinessSummary,
    YandexTelemetrySummary,
)
from route74.storage.runtime_quality import (
    BotRuntimeCalibration,
    BotRuntimeCalibrationGroup,
    BotRuntimePredictionQuality,
    BotRuntimePredictionQualityGroup,
)
from route74.storage.yandex_canary import YandexCanaryHealth
from route74.support_triage import TRIAGE_WARNING, SupportTriage, SupportTriageItem, operator_primary_triage_item
from route74.watch_state import WatchStateProfileSummary, WatchStateSummary


def main() -> None:
    _assert_eta_domain_guardrails()
    _assert_counts_text_is_single_line()
    _assert_diagnostic_sanitizer_redacts_secrets()
    _assert_bot_latency_event_guardrails()
    _assert_bot_latency_storage_sanitizes_command_and_error()
    _assert_bot_latency_summarizes_stable_error_categories()
    _assert_bot_latency_ignores_invalid_durations()
    _assert_stats_service_summary_hours_guardrails()

    current_time = datetime(2026, 6, 4, 7, 0, tzinfo=NOVOSIBIRSK_TZ)
    decision = DepartureDecision(
        profile=MORNING,
        current_time=current_time,
        walk_minutes=12,
        source=DepartureSource.YANDEX,
        urgency=DepartureUrgency.RELAX,
        arrival_in_minutes=20,
        arrival_at=current_time + timedelta(minutes=20),
        leave_in_minutes=5,
        leave_at=current_time + timedelta(minutes=5),
        next_live_minutes=(),
        eta_consensus=EtaConsensus(
            selected_source=EtaSource.YANDEX,
            arrival_minutes=20,
            confidence=EtaConfidence.MEDIUM,
            target_wait_minutes=3,
            spread_minutes=None,
            warning="",
            estimates=(EtaEstimate(EtaSource.YANDEX, 20),),
            factors=(EtaFactor(EtaFactorKind.SAFETY_BUFFER, minutes=2, sample_count=7, percent=14, scope="source"),),
        ),
        yandex_forecast=YandexLiveForecast(
            enabled=True,
            available=True,
            source_method=YandexSourceMethod.VEHICLE_PREDICTION,
            status=YandexSourceStatus.OK,
            arrival_minutes=(20,),
            vehicle_count=2,
            newest_age_seconds=42,
            confidence=EtaConfidence.MEDIUM,
        ),
    )
    _assert_stats_triage_reports_history_readiness(decision)
    _assert_stats_triage_reports_history_backtest(decision)
    _assert_stats_triage_reports_integrity_gap(decision)
    _assert_stats_forecast_health_reports_integrity_gap(decision)
    _assert_stats_forecast_health_reports_missing_buckets(decision)
    _assert_stats_snapshot_guardrails(decision)
    message = format_stats_message(
        StatsSnapshot(decision, build_info=BuildInfo("0.1.0", short_commit="abc1234", dirty=False))
    )
    _assert_contains(message, "📊 Статистика 74")
    _assert_contains(message, "🧩 Версия: abc1234 · clean")
    _assert_contains(message, "🧭 Оценка: через 20 мин · источник Яндекс · доверие среднее · цель ждать 3 мин")
    _assert_contains(message, "🧪 Сигналы: ✓ Яндекс 20 мин")
    _assert_contains(message, "🧮 Почему: запас +2 мин, промахи 14%, 7 замеров, по источнику")
    _assert_equal(
        eta_factor_texts(
            (
                EtaFactor(
                    EtaFactorKind.SAFETY_BUFFER,
                    minutes=3,
                    sample_count=3,
                    percent=100,
                    scope="bot_runtime_bucket",
                ),
            )
        ),
        ("запас +3 мин, промахи 100%, 3 замера, по похожим ответам бота",),
    )
    _assert_contains(
        message, "🟡 Яндекс: через 20 мин · данные есть · машина на карте · машин 2 · свежесть 42 сек назад"
    )
    _assert_contains(message, "📈 История Яндекса: данных пока нет")
    backtest_message = format_stats_message(
        StatsSnapshot(
            decision,
            forecast_backtest=_forecast_backtest_summary(),
            forecast_backtest_command="route74 forecast-backtest --window weekday_morning_09_12",
        )
    )
    _assert_contains(
        backtest_message,
        "🧪 Качество истории: p80 · проверено 6/8 · промахов 3 (50%) · точность 50% · ср. ошибка 2.5м · route74 forecast-backtest --window weekday_morning_09_12",
    )
    support_message = format_stats_message(
        StatsSnapshot(
            decision,
            support_report_command="route74 support-report --profile morning",
            prediction_evaluate_command="route74 prediction-evaluate --window weekday_morning_09_12",
        )
    )
    _assert_contains(support_message, "🧰 Разбор: route74 support-report --profile morning")
    latency_message = format_stats_message(
        StatsSnapshot(
            decision,
            bot_latency=_bot_latency_summary(
                total_events=4,
                no_eta_events=3,
                p95_total_ms=6200,
                latest_received_at=datetime(2026, 6, 4, 6, 43, tzinfo=NOVOSIBIRSK_TZ),
            ),
            bot_latency_command="route74 runtime-latency --hours 24 --profile morning --event-kind user_reply",
            support_report_command="route74 support-report --profile morning",
        )
    )
    _assert_contains(
        latency_message,
        "🔔 Runtime 24ч: ответов 4 · ошибки 0 (0%) · без ETA 3 (75%) · p95 6200мс",
    )
    _assert_contains(latency_message, "свежесть 17 мин назад")
    _assert_contains(
        latency_message,
        "🧰 Разбор: route74 runtime-latency --hours 24 --profile morning --event-kind user_reply · много ответов без ETA",
    )
    _assert_not_contains(latency_message, "🧰 Разбор: route74 forecast-health")
    empty_latency_message = format_stats_message(
        StatsSnapshot(
            decision,
            bot_latency=_bot_latency_summary(
                latest_received_at=datetime(2026, 6, 4, 6, 43, tzinfo=NOVOSIBIRSK_TZ),
            ),
            bot_latency_command="route74 runtime-latency --hours 24 --profile morning --event-kind user_reply",
            support_report_command="route74 support-report --profile morning",
        )
    )
    _assert_contains(empty_latency_message, "🔔 Runtime 24ч: ответов пока нет · свежесть 17 мин назад")
    small_latency_message = format_stats_message(
        StatsSnapshot(
            decision,
            bot_latency=_bot_latency_summary(
                total_events=1,
                no_eta_events=1,
                p95_total_ms=19_965,
                latest_received_at=datetime(2026, 6, 4, 6, 52, tzinfo=NOVOSIBIRSK_TZ),
            ),
            bot_latency_command="route74 runtime-latency --hours 24 --profile morning --event-kind user_reply",
            support_report_command="route74 support-report --profile morning",
        )
    )
    _assert_contains(
        small_latency_message,
        "🔔 Runtime 24ч: ответов 1 · ошибки 0 (0%) · без ETA 1 (100%) · p95 19965мс · мало данных для p95 (1/3)",
    )
    _assert_not_contains(small_latency_message, "медленные runtime-ответы")
    latency_error_message = format_stats_message(
        StatsSnapshot(
            decision,
            bot_latency=_bot_latency_summary(
                total_events=4,
                error_events=1,
                p95_total_ms=6200,
                error_reasons=(CountByKey("send_error", 1),),
                latest_received_at=datetime(2026, 6, 4, 6, 43, tzinfo=NOVOSIBIRSK_TZ),
            ),
            bot_latency_command="route74 runtime-latency --hours 24 --profile morning --event-kind user_reply",
            support_report_command="route74 support-report --profile morning",
            triage_action_command="route74 runtime-latency --hours 24 --profile morning --event-kind user_reply",
            triage_action_key="bot_latency_errors",
            triage_action_message="errors=1/4(25%) top_error=send_error:1",
        )
    )
    _assert_contains(
        latency_error_message,
        "🔔 Runtime 24ч: ответов 4 · ошибки 1 (25%) · причина send_error:1 · p95 6200мс",
    )
    _assert_contains(
        latency_error_message,
        "🧰 Разбор: route74 runtime-latency --hours 24 --profile morning --event-kind user_reply · ошибки runtime-ответов",
    )
    followup_error_message = format_stats_message(
        StatsSnapshot(
            decision,
            bot_latency=_bot_latency_summary(
                total_events=2,
                error_events=1,
                p95_total_ms=180,
                error_categories=(CountByKey("followup_send_error", 1),),
                error_reasons=(CountByKey("followup_send_error: RuntimeError: local followup failure", 1),),
            ),
            bot_latency_command="route74 runtime-latency --hours 24 --profile morning --event-kind user_reply",
            support_report_command="route74 support-report --profile morning",
        )
    )
    _assert_contains(
        followup_error_message,
        "🔔 Runtime 24ч: ответов 2 · ошибки 1 (50%) · причина quick-start подсказка не ушла:1 · p95 180мс",
    )
    triage_message = format_stats_message(
        StatsSnapshot(
            decision,
            bot_latency=_bot_latency_summary(
                total_events=4,
                no_eta_events=3,
                p95_total_ms=6200,
                latest_received_at=datetime(2026, 6, 4, 6, 43, tzinfo=NOVOSIBIRSK_TZ),
            ),
            bot_latency_command="route74 runtime-latency --hours 24 --profile morning --event-kind user_reply",
            support_report_command="route74 support-report --profile morning",
            triage_action_command="route74 forecast-health",
            triage_action_key="collector",
            triage_action_message="yandex-collect status=missing",
        )
    )
    _assert_contains(
        triage_message,
        "🧰 Разбор: route74 runtime-latency --hours 24 --profile morning --event-kind user_reply · много ответов без ETA",
    )
    _assert_not_contains(triage_message, "🧰 Разбор: route74 forecast-health")
    source_risk_message = format_stats_message(
        StatsSnapshot(
            decision,
            support_report_command="route74 support-report --profile morning",
            triage_action_command="route74 prediction-calibration --window weekday_morning_09_12",
            triage_action_key="bot_runtime_source_late_risk",
            triage_action_message=(
                "profile=morning source=target_stop_live eval=3/3 miss=1(33%) p80_early=1m suggested=+1m"
            ),
        )
    )
    _assert_contains(
        source_risk_message,
        "🧰 Разбор: route74 prediction-calibration --window weekday_morning_09_12 · проверить источник",
    )
    sanitized_support_message = format_stats_message(
        StatsSnapshot(
            decision,
            support_report_command="route74 support-report\n--profile morning",
            prediction_evaluate_command="route74 prediction-evaluate\n--window weekday_morning_09_12",
        )
    )
    _assert_contains(sanitized_support_message, "🧰 Разбор: route74 support-report --profile morning")
    runtime_message = format_stats_message(
        StatsSnapshot(
            decision,
            runtime_quality=_runtime_quality(
                BotRuntimePredictionQualityGroup(
                    "morning",
                    4,
                    3,
                    1,
                    1,
                    2,
                    -1,
                    2,
                    current_time,
                    current_time,
                    current_time,
                ),
            ),
            runtime_calibration=_runtime_calibration(
                BotRuntimeCalibrationGroup("morning", 4, 3, 1, 2, None, 2, "late_risk", "review +2m"),
                by_profile_source=(
                    BotRuntimeCalibrationGroup(
                        f"{MORNING.key}/{SOURCE_HISTORY_HEADWAY}",
                        3,
                        3,
                        2,
                        3,
                        None,
                        3,
                        "late_risk",
                        "review +3m",
                    ),
                    BotRuntimeCalibrationGroup(
                        f"{MORNING.key}/{SOURCE_TARGET_STOP_LIVE}",
                        4,
                        4,
                        0,
                        None,
                        1,
                        0,
                        "balanced",
                        "keep current buffers",
                    ),
                ),
            ),
            support_report_command="route74 support-report --profile morning",
            prediction_calibration_command="route74 prediction-calibration --window weekday_morning_09_12",
            prediction_evaluate_command="route74 prediction-evaluate --window weekday_morning_09_12",
        )
    )
    _assert_contains(
        runtime_message,
        "🌐 Runtime 24ч: прогнозов 4 · проверено 3 (75%) · ждёт 1 (25%) · промахи 1 (33%) · p50 ошибка 2 мин",
    )
    _assert_contains(runtime_message, "ETA-защита недоступна 2")
    _assert_contains(
        runtime_message,
        "🧪 Проверка фактов: ждёт факта 1/4, старое 0 мин назад · route74 prediction-evaluate --window weekday_morning_09_12",
    )
    _assert_contains(runtime_message, "🛠️ Запас: проверь +2 мин для этого направления")
    _assert_contains(
        runtime_message,
        "🔎 Источник риска: история Яндекса · промахи 2/3 (67%) · p80 раннего прихода 3 мин",
    )
    _assert_contains(
        runtime_message,
        "🧪 Калибровка source: route74 prediction-calibration --window weekday_morning_09_12",
    )
    _assert_contains(runtime_message, "🧰 Разбор: route74 support-report --profile morning · проверить ETA-защиту")
    stale_pending_message = format_stats_message(
        StatsSnapshot(
            decision,
            runtime_quality=_runtime_quality(
                BotRuntimePredictionQualityGroup(
                    "morning",
                    4,
                    1,
                    3,
                    0,
                    0,
                    0,
                    1,
                    current_time,
                    current_time,
                    current_time - timedelta(hours=3),
                ),
            ),
            runtime_calibration=_runtime_calibration(
                BotRuntimeCalibrationGroup("morning", 4, 1, 0, None, 1, 0, "insufficient", "collect more")
            ),
            support_report_command="route74 support-report --profile morning",
            prediction_evaluate_command="route74 prediction-evaluate --window weekday_morning_09_12",
        )
    )
    _assert_contains(
        stale_pending_message,
        "🧰 Разбор: route74 prediction-evaluate --window weekday_morning_09_12 · проверить факты прибытия",
    )
    late_risk_message = format_stats_message(
        StatsSnapshot(
            decision,
            runtime_quality=_runtime_quality(
                BotRuntimePredictionQualityGroup(
                    "morning",
                    3,
                    3,
                    0,
                    2,
                    0,
                    -2,
                    2,
                    current_time,
                    current_time,
                    None,
                ),
            ),
            runtime_calibration=_runtime_calibration(
                BotRuntimeCalibrationGroup("morning", 3, 3, 2, 4, None, 4, "late_risk", "review +4m")
            ),
            support_report_command="route74 support-report --profile morning",
            prediction_evaluate_command="route74 prediction-evaluate --window weekday_morning_09_12",
        )
    )
    _assert_contains(late_risk_message, "🧰 Разбор: route74 support-report --profile morning · разобрать промахи")
    buffer_risk_message = format_stats_message(
        StatsSnapshot(
            decision,
            runtime_quality=_runtime_quality(
                BotRuntimePredictionQualityGroup(
                    "morning",
                    3,
                    3,
                    0,
                    1,
                    0,
                    0,
                    2,
                    current_time,
                    current_time,
                    None,
                ),
            ),
            runtime_calibration=_runtime_calibration(
                BotRuntimeCalibrationGroup("morning", 3, 3, 1, 4, None, 4, "late_risk", "review +4m"),
                by_profile_source=(
                    BotRuntimeCalibrationGroup(
                        f"{MORNING.key}/{SOURCE_TARGET_STOP_LIVE}",
                        3,
                        3,
                        1,
                        4,
                        None,
                        4,
                        "late_risk",
                        "review +4m for source",
                    ),
                ),
            ),
            support_report_command="route74 support-report --profile morning",
            prediction_calibration_command="route74 prediction-calibration --window weekday_morning_09_12",
            prediction_evaluate_command="route74 prediction-evaluate --window weekday_morning_09_12",
        )
    )
    _assert_contains(
        buffer_risk_message,
        "🧪 Калибровка source: route74 prediction-calibration --window weekday_morning_09_12",
    )
    _assert_contains(
        buffer_risk_message,
        "🧰 Разбор: route74 prediction-calibration --window weekday_morning_09_12 · проверить источник",
    )
    p50_issue_message = format_stats_message(
        StatsSnapshot(
            decision,
            runtime_quality=_runtime_quality(
                BotRuntimePredictionQualityGroup(
                    "morning",
                    3,
                    3,
                    0,
                    0,
                    0,
                    0,
                    4,
                    current_time,
                    current_time,
                    None,
                ),
            ),
            runtime_calibration=_runtime_calibration(
                BotRuntimeCalibrationGroup("morning", 3, 3, 0, None, 1, 0, "balanced", "keep current buffers")
            ),
            support_report_command="route74 support-report --profile morning",
            prediction_evaluate_command="route74 prediction-evaluate --window weekday_morning_09_12",
        )
    )
    _assert_contains(p50_issue_message, "🧰 Разбор: route74 support-report --profile morning · разобрать ошибку ETA")
    no_profile_runtime = format_stats_message(
        StatsSnapshot(
            decision,
            runtime_quality=_runtime_quality(),
            runtime_calibration=_runtime_calibration(),
        )
    )
    _assert_contains(no_profile_runtime, "🌐 Runtime 24ч: по этому направлению фактов пока нет")
    _assert_contains(no_profile_runtime, "🛠️ Запас: жду проверенные ответы по этому направлению")
    _assert_watch_state_stats_lines(decision)

    mixed_signals = format_stats_message(
        StatsSnapshot(
            replace(
                decision,
                eta_consensus=EtaConsensus(
                    selected_source=EtaSource.YANDEX,
                    arrival_minutes=20,
                    confidence=EtaConfidence.MEDIUM,
                    target_wait_minutes=3,
                    spread_minutes=2,
                    warning="источники немного расходятся",
                    estimates=(
                        EtaEstimate(EtaSource.YANDEX, 20),
                        EtaEstimate(EtaSource.YANDEX_HISTORY, 22),
                    ),
                ),
            )
        )
    )
    _assert_contains(
        mixed_signals,
        "🧪 Сигналы: ✓ Яндекс 20 мин, история Яндекса 22 мин · разброс 2 мин",
    )

    scoped_history = format_stats_message(
        StatsSnapshot(
            replace(
                decision,
                yandex_history=YandexHistoryPrediction(
                    available=True,
                    arrival_minutes=18,
                    sample_count=24,
                    bucket_minutes=30,
                    window_days=14,
                    percentile=80,
                    fallback_reason="",
                    scope=YandexHistoryScope.REPORT_WINDOW,
                    report_window_key="weekday_morning_09_12",
                ),
            )
        )
    )
    _assert_contains(
        scoped_history,
        "📈 История Яндекса: p80 через 18 мин · n=24 · окно ±30 мин · отчётное окно weekday_morning_09_12",
    )

    profile_time_history = format_stats_message(
        StatsSnapshot(
            replace(
                decision,
                yandex_history=YandexHistoryPrediction(
                    available=True,
                    arrival_minutes=21,
                    sample_count=20,
                    bucket_minutes=60,
                    window_days=14,
                    percentile=80,
                    fallback_reason="",
                    scope=YandexHistoryScope.PROFILE_TIME,
                ),
            )
        )
    )
    _assert_contains(
        profile_time_history,
        "📈 История Яндекса: p80 через 21 мин · n=20 · окно ±1ч · похожее время профиля",
    )
    forecast_health_message = format_stats_message(
        StatsSnapshot(
            decision,
            forecast_health=_forecast_health_summary(
                current_time,
                morning_status="no_collector_runs",
                morning_reason="collector has no recorded runs in this report window",
                morning_ready_buckets=2,
                morning_total_buckets=6,
                morning_fresh_eta_samples=2,
                morning_total_samples=12,
                collector_status="missing",
                collector_message="collector heartbeat is absent",
                canary_status="warning",
                canary_reason="missing canary profiles: morning",
            ),
        )
    )
    _assert_contains(
        forecast_health_message,
        "🩺 Прогноз: 1/2 окон готовы · weekday_morning_09_12 no_collector_runs · collector missing · canary warning",
    )
    forecast_health_error = format_stats_message(
        StatsSnapshot(decision, forecast_health_error="OperationalError: forecast\nlocked")
    )
    _assert_contains(
        forecast_health_error,
        "🩺 Прогноз: недоступен · OperationalError: forecast locked",
    )
    forecast_readiness_message = format_stats_message(
        StatsSnapshot(
            decision,
            forecast_readiness=_forecast_readiness_summary(current_time),
            forecast_readiness_command="route74 forecast-readiness --window weekday_morning_09_12",
            support_report_command="route74 support-report --profile morning",
        )
    )
    _assert_contains(
        forecast_readiness_message,
        "📚 История: данных мало · ±30м · samples 12/20 · days 2/3 · route74 forecast-readiness --window weekday_morning_09_12",
    )
    _assert_contains(
        forecast_readiness_message,
        "🧰 Разбор: route74 forecast-readiness --window weekday_morning_09_12 · проверить историю Яндекса",
    )
    forecast_coverage_message = format_stats_message(
        StatsSnapshot(
            decision,
            forecast_health=_forecast_health_summary(
                current_time,
                morning_status="insufficient_bucket_coverage",
                morning_reason="not enough ready buckets",
                morning_ready_buckets=2,
                morning_total_buckets=6,
                morning_missing_bucket_labels=("09:00", "09:30", "10:00", "10:30", "11:00"),
            ),
            forecast_readiness=_forecast_readiness_summary(current_time),
            forecast_readiness_command="route74 forecast-readiness --window weekday_morning_09_12",
            forecast_coverage_command="route74 forecast-coverage --window weekday_morning_09_12",
            support_report_command="route74 support-report --profile morning",
        )
    )
    _assert_contains(
        forecast_coverage_message,
        "📚 История: данных мало · ±30м · samples 12/20 · days 2/3 · "
        "не хватает 09:00, 09:30, 10:00, 10:30,+1 · "
        "route74 forecast-coverage --window weekday_morning_09_12",
    )
    _assert_contains(
        forecast_coverage_message,
        "🧰 Разбор: route74 forecast-coverage --window weekday_morning_09_12 · проверить покрытие окна",
    )
    _assert_not_contains(forecast_coverage_message, "route74 forecast-readiness --window weekday_morning_09_12")
    forecast_readiness_error = format_stats_message(
        StatsSnapshot(decision, forecast_readiness_error="OperationalError: readiness\nlocked")
    )
    _assert_contains(
        forecast_readiness_error,
        "📚 История: недоступна · OperationalError: readiness locked",
    )
    history_error = format_stats_message(
        StatsSnapshot(
            replace(
                decision,
                yandex_history=YandexHistoryPrediction.unavailable(reason="history_error:OperationalError"),
            )
        )
    )
    _assert_contains(
        history_error,
        "📈 История Яндекса: недоступна · похожее время профиля · ошибка чтения истории: OperationalError",
    )
    sparse_history = format_stats_message(
        StatsSnapshot(
            replace(
                decision,
                yandex_history=YandexHistoryPrediction.unavailable(
                    sample_count=12,
                    bucket_minutes=30,
                    window_days=14,
                    reason="insufficient_history:12/20;days:1/3",
                ),
            )
        )
    )
    _assert_contains(
        sparse_history,
        "📈 История Яндекса: данных мало · 12/20 замеров, 1/3 дней · похожее время профиля",
    )
    _assert_not_contains(sparse_history, "insufficient_history")

    invalid_age = format_stats_message(
        StatsSnapshot(
            replace(
                decision,
                yandex_forecast=replace(decision.yandex_forecast, newest_age_seconds=-5),
            )
        )
    )
    _assert_contains(
        invalid_age, "🟡 Яндекс: через 20 мин · данные есть · машина на карте · машин 2 · свежесть нет данных"
    )
    _assert_not_contains(invalid_age, "свежесть -5 сек назад")

    malformed = format_stats_message(
        StatsSnapshot(
            replace(
                decision,
                yandex_forecast=YandexLiveForecast(
                    enabled=True,
                    available=True,
                    source_method=YandexSourceMethod.VEHICLE_PREDICTION,
                    status=YandexSourceStatus.OK,
                    arrival_minutes=(),
                    confidence=EtaConfidence.HIGH,
                    fallback_reason="available_without_eta\nsecond line",
                ),
            )
        )
    )
    _assert_contains(malformed, "🟡 Яндекс: данные есть · ETA сейчас не отдал")
    _assert_not_contains(malformed, "available_without_eta")
    _assert_not_contains(malformed, "available_without_eta\nsecond line")

    long_reason = "x" * 140
    unavailable = format_stats_message(
        StatsSnapshot(
            replace(
                decision,
                yandex_forecast=YandexLiveForecast.unavailable(
                    status=YandexSourceStatus.PARSE_ERROR,
                    source_method=YandexSourceMethod.HTTP,
                    reason=f"{long_reason}\nsecond line",
                ),
            )
        )
    )
    _assert_contains(unavailable, f"🟡 Яндекс: непонятный ответ · {'x' * 120}")
    _assert_not_contains(unavailable, "second line")

    noisy_unavailable = format_stats_message(
        StatsSnapshot(
            replace(
                decision,
                yandex_forecast=YandexLiveForecast.unavailable(
                    status=YandexSourceStatus.PARSE_ERROR,
                    source_method=YandexSourceMethod.HTTP,
                    reason="\x1b[31mblocked\nby upstream\x00\x1b[0m",
                ),
            )
        )
    )
    _assert_contains(noisy_unavailable, "🟡 Яндекс: непонятный ответ · Яндекс заблокировал запрос")
    _assert_not_contains(noisy_unavailable, "\x1b")
    _assert_not_contains(noisy_unavailable, "\x00")
    _assert_not_contains(noisy_unavailable, "[31m")
    _assert_not_contains(noisy_unavailable, "[0m")
    _assert_not_contains(noisy_unavailable, "\nby upstream")

    compound_unavailable = format_stats_message(
        StatsSnapshot(
            replace(
                decision,
                yandex_forecast=YandexLiveForecast.unavailable(
                    status=YandexSourceStatus.UNAVAILABLE,
                    source_method=YandexSourceMethod.BROWSER,
                    reason=(
                        "http:needs_signature:bad_request_maybe_s; "
                        "vehicle_prediction:empty:browser_no_prediction_response; "
                        "browser:no_target:direction_thread_not_found"
                    ),
                ),
            )
        )
    )
    _assert_contains(
        compound_unavailable,
        "🟡 Яндекс: недоступен · нужен browser-capture; нужное направление не найдено; ETA сейчас не отдал",
    )
    _assert_not_contains(compound_unavailable, "bad_request_maybe_s")
    _assert_not_contains(compound_unavailable, "browser_no_prediction_response")
    _assert_not_contains(compound_unavailable, "direction_thread_not_found")

    sensitive_unavailable = format_stats_message(
        StatsSnapshot(
            replace(
                decision,
                yandex_forecast=YandexLiveForecast.unavailable(
                    status=YandexSourceStatus.UNAVAILABLE,
                    source_method=YandexSourceMethod.HTTP,
                    reason=(
                        'csrfToken=query-secret&sessionId=session-secret {"csrfToken":"json-secret"} '
                        "/home/vladimir/work-projects/74/data/yandex.json"
                    ),
                ),
            )
        )
    )
    _assert_contains(sensitive_unavailable, "csrfToken=<redacted>")
    _assert_contains(sensitive_unavailable, "sessionId=<redacted>")
    _assert_contains(sensitive_unavailable, "<path>")
    _assert_not_contains(sensitive_unavailable, "query-secret")
    _assert_not_contains(sensitive_unavailable, "session-secret")
    _assert_not_contains(sensitive_unavailable, "json-secret")
    _assert_not_contains(sensitive_unavailable, "/home/vladimir")

    future_heartbeat = format_stats_message(
        StatsSnapshot(
            decision,
            telemetry=_telemetry_summary(
                CollectorHeartbeat(
                    name="yandex-collect",
                    updated_at=current_time + timedelta(minutes=2),
                    pid=1234,
                    profile_filter="morning",
                    last_status="ok",
                    last_message="ok",
                )
            ),
        )
    )
    _assert_contains(future_heartbeat, "💓 Collector: часы впереди на 2 мин · ok")
    _assert_not_contains(future_heartbeat, "💓 Collector: 0 мин назад · ok")

    noisy_status = "\x1b[31mstale\n" + "x" * 80 + "\x1b[0m"
    sanitized_heartbeat = format_stats_message(
        StatsSnapshot(
            decision,
            telemetry=_telemetry_summary(
                CollectorHeartbeat(
                    name="yandex-collect",
                    updated_at=current_time,
                    pid=1234,
                    profile_filter="morning",
                    last_status=noisy_status,
                    last_message="ok",
                )
            ),
        )
    )
    _assert_contains(sanitized_heartbeat, f"💓 Collector: 0 мин назад · {'stale ' + 'x' * 34}")
    _assert_not_contains(sanitized_heartbeat, noisy_status)
    _assert_not_contains(sanitized_heartbeat, "[31m")
    _assert_not_contains(sanitized_heartbeat, "[0m")
    _assert_stats_telemetry_failure_degrades(decision)
    _assert_stats_partial_summary_failure_degrades(decision)
    _assert_stats_service_reads_forecast_health_summary(decision)
    _assert_stats_service_reads_forecast_backtest_summary(decision)
    _assert_stats_service_reads_bot_latency_summary(decision)
    _assert_stats_message_uses_bot_error_category(decision)
    _assert_stats_triage_prefers_pending_over_forecast_window()
    _assert_stats_triage_prefers_runtime_over_forecast_window()
    _assert_stats_service_runtime_summary_is_profile_scoped(decision)
    _assert_stats_service_reads_watch_state_without_chat_id(decision)
    print("OK | stats smoke passed")


class FakeStatsCommuteService:
    def __init__(self, decision: DepartureDecision) -> None:
        self._decision = decision

    def build_decision(self, _profile: object, _walk_minutes: object) -> DepartureDecision:
        return self._decision


def _assert_watch_state_stats_lines(decision: DepartureDecision) -> None:
    active_message = format_stats_message(
        StatsSnapshot(
            decision,
            watch_state=_watch_summary(
                status="ok",
                active_count=1,
                due_count=1,
                early_sent_count=1,
                next_poll_at=decision.current_time + timedelta(minutes=2),
                profiles=(
                    WatchStateProfileSummary(
                        profile_key=MORNING.key,
                        active_count=1,
                        due_count=1,
                        early_sent_count=1,
                        oldest_age_minutes=8,
                        next_poll_at=decision.current_time + timedelta(minutes=2),
                        expires_in_minutes=22,
                    ),
                ),
            ),
        )
    )
    _assert_contains(
        active_message,
        "🔔 Watch: активен 1 · ждёт проверку 1 · ранний сигнал уже был 1 · следующий через 2 мин · до конца 22 мин",
    )

    runtime_error_message = format_stats_message(
        StatsSnapshot(
            decision,
            watch_state=_watch_summary(
                status="warning",
                active_count=1,
                runtime_error_count=2,
                runtime_error_records=1,
                latest_error_at=decision.current_time - timedelta(minutes=4),
                runtime_error_types=("RuntimeError",),
            ),
            watch_state_command="route74 watch-state --path data/custom-watches.json",
        )
    )
    _assert_contains(
        runtime_error_message,
        "🔔 Watch: ошибки проверки 2 · watch 1 · последняя 4 мин назад · RuntimeError · "
        "route74 watch-state --path data/custom-watches.json",
    )
    _assert_contains(
        runtime_error_message,
        "🧰 Разбор: route74 watch-state --path data/custom-watches.json · ошибки watch-проверок",
    )

    overdue_message = format_stats_message(
        StatsSnapshot(
            decision,
            watch_state=_watch_summary(
                status="degraded",
                active_count=2,
                due_count=1,
                overdue_count=1,
                max_overdue_seconds=180,
            ),
            watch_state_command="route74 watch-state --path data/custom-watches.json",
        )
    )
    _assert_contains(
        overdue_message,
        "🔔 Watch: проверка просрочена 1/2 · максимум 3 мин · route74 watch-state --path data/custom-watches.json",
    )
    _assert_contains(
        overdue_message,
        "🧰 Разбор: route74 watch-state --path data/custom-watches.json · проверка watch просрочена",
    )

    unreadable_message = format_stats_message(
        StatsSnapshot(
            decision,
            watch_state=_watch_summary(
                status="critical",
                file_status="unreadable",
                error_type="JSONDecodeError",
            ),
            watch_state_command="route74 watch-state\n--path data/web_watches.json",
        )
    )
    _assert_contains(
        unreadable_message,
        "🔔 Watch: файл недоступен · unreadable/JSONDecodeError · route74 watch-state --path data/web_watches.json",
    )
    _assert_contains(
        unreadable_message,
        "🧰 Разбор: route74 watch-state --path data/web_watches.json · проверить watch-state",
    )
    _assert_not_contains(unreadable_message, "route74 watch-state\n--path")

    invalid_message = format_stats_message(
        StatsSnapshot(
            decision,
            watch_state=_watch_summary(status="degraded", invalid_records=2, total_records=2),
        )
    )
    _assert_contains(invalid_message, "🔔 Watch: повреждённых записей 2 · route74 watch-state")

    missing_message = format_stats_message(
        StatsSnapshot(
            decision,
            watch_state=_watch_summary(status="ok", file_status="missing"),
        )
    )
    _assert_contains(missing_message, "🔔 Watch: файл ещё не создан · route74 watch-state")

    idle_message = format_stats_message(
        StatsSnapshot(
            decision,
            watch_state=_watch_summary(status="ok"),
        )
    )
    _assert_contains(idle_message, "🔔 Watch: активных проверок нет · route74 watch-state")

    watch_error_message = format_stats_message(StatsSnapshot(decision, watch_state_error="open\nfailed"))
    _assert_contains(watch_error_message, "🔔 Watch: недоступен · open failed · route74 watch-state")
    _assert_not_contains(watch_error_message, "open\nfailed")


def _assert_stats_service_reads_watch_state_without_chat_id(decision: DepartureDecision) -> None:
    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "route74.sqlite"
        watch_state_path = Path(temp_dir) / "web_watches.json"
        watch_state_path.write_text(
            (
                "{"
                '"101": {'
                '"profile_key": "morning", '
                '"walk_minutes": 12, '
                f'"started_at": "{(decision.current_time - timedelta(minutes=8)).isoformat()}", '
                f'"next_poll_at": "{(decision.current_time + timedelta(minutes=2)).isoformat()}", '
                '"early_sent": false'
                "}"
                "}"
            ),
            encoding="utf-8",
        )
        snapshot = StatsService(
            FakeStatsCommuteService(decision),  # type: ignore[arg-type]
            db_path=db_path,
            watch_state_path=watch_state_path,
        ).build(MORNING, 12)
    if snapshot.watch_state is None:
        raise AssertionError("expected stats snapshot watch_state")
    _assert_equal(snapshot.watch_state.active_count, 1)
    message = format_stats_message(snapshot)
    _assert_contains(message, "🔔 Watch: активен 1 · следующий через 2 мин")
    _assert_not_contains(message, "101")


def _watch_summary(
    *,
    status: str,
    active_count: int = 0,
    due_count: int = 0,
    overdue_count: int = 0,
    expired_records: int = 0,
    invalid_records: int = 0,
    total_records: int | None = None,
    early_sent_count: int = 0,
    next_poll_at: datetime | None = None,
    expires_at: datetime | None = None,
    expires_in_minutes: int | None = None,
    max_overdue_seconds: int | None = None,
    file_status: str = "ok",
    error_type: str = "",
    profiles: tuple[WatchStateProfileSummary, ...] = (),
    runtime_error_count: int = 0,
    runtime_error_records: int = 0,
    latest_error_at: datetime | None = None,
    runtime_error_types: tuple[str, ...] = (),
) -> WatchStateSummary:
    return WatchStateSummary(
        path=Path("data/web_watches.json"),
        current_time=datetime(2026, 6, 4, 7, 0, tzinfo=NOVOSIBIRSK_TZ),
        status=status,
        active_count=active_count,
        due_count=due_count,
        overdue_count=overdue_count,
        expired_records=expired_records,
        invalid_records=invalid_records,
        total_records=total_records if total_records is not None else active_count + invalid_records + expired_records,
        early_sent_count=early_sent_count,
        oldest_age_minutes=8 if active_count else None,
        next_poll_at=next_poll_at,
        expires_at=expires_at,
        expires_in_minutes=expires_in_minutes,
        max_overdue_seconds=max_overdue_seconds,
        file_status=file_status,
        error_type=error_type,
        profiles=profiles,
        runtime_error_count=runtime_error_count,
        runtime_error_records=runtime_error_records,
        latest_error_at=latest_error_at,
        runtime_error_types=runtime_error_types,
    )


def _assert_eta_domain_guardrails() -> None:
    _assert_rejects(lambda: EtaEstimate("yandex", 8), "EtaSource")
    _assert_rejects(lambda: EtaEstimate(EtaSource.YANDEX, -1), "non-negative")
    _assert_rejects(lambda: EtaEstimate(EtaSource.YANDEX, True), "non-negative")
    _assert_rejects(lambda: EtaEstimate(EtaSource.YANDEX, 8.5), "non-negative")
    _assert_rejects(lambda: _consensus(selected_source="yandex"), "EtaSource")
    _assert_rejects(lambda: _consensus(confidence="high"), "EtaConfidence")
    _assert_rejects(lambda: _consensus(arrival_minutes=-1), "non-negative")
    _assert_rejects(lambda: _consensus(arrival_minutes=True), "non-negative")
    _assert_rejects(lambda: _consensus(arrival_minutes=8.5), "non-negative")
    _assert_rejects(lambda: _consensus(target_wait_minutes=-1), "target wait")
    _assert_rejects(lambda: _consensus(target_wait_minutes=True), "target wait")
    _assert_rejects(lambda: _consensus(target_wait_minutes=2.5), "target wait")
    _assert_rejects(lambda: _consensus(spread_minutes=-1), "spread")
    _assert_rejects(lambda: _consensus(spread_minutes=True), "spread")
    _assert_rejects(lambda: _consensus(spread_minutes=1.5), "spread")
    _assert_rejects(lambda: _consensus(spread_minutes=1), "two estimates")
    _assert_rejects(
        lambda: _consensus(
            spread_minutes=1,
            estimates=(
                EtaEstimate(EtaSource.YANDEX, 8),
                EtaEstimate(EtaSource.YANDEX_HISTORY, 10),
            ),
        ),
        "spread must match estimates",
    )
    _assert_rejects(lambda: _consensus(warning=None), "text")
    _assert_rejects(lambda: _consensus(estimates=[EtaEstimate(EtaSource.YANDEX, 8)]), "tuple")
    _assert_rejects(lambda: _consensus(estimates=(object(),)), "EtaEstimate")
    _assert_rejects(lambda: _consensus(factors=[EtaFactor(EtaFactorKind.SPREAD)]), "tuple")
    _assert_rejects(lambda: _consensus(factors=(object(),)), "EtaFactor")
    _assert_rejects(lambda: EtaFactor("spread"), "EtaFactorKind")
    _assert_rejects(lambda: EtaFactor(EtaFactorKind.SPREAD, minutes=True), "minutes")
    _assert_rejects(lambda: EtaFactor(EtaFactorKind.SPREAD, sample_count=-1), "sample count")
    _assert_rejects(lambda: EtaFactor(EtaFactorKind.SOURCE_RISK, percent=101), "percent")
    _assert_rejects(lambda: EtaFactor(EtaFactorKind.SAFETY_BUFFER, scope="source\nbad"), "single-line")
    _assert_rejects(lambda: _consensus(estimates=(EtaEstimate(EtaSource.YANDEX, 9),)), "selected arrival")
    _assert_rejects(lambda: _consensus(selected_source=None, arrival_minutes=8), "selected source")
    _assert_rejects(lambda: _consensus(selected_source=EtaSource.YANDEX, arrival_minutes=None), "arrival minutes")
    _assert_rejects(lambda: _consensus(selected_source=None, arrival_minutes=None), "unknown confidence")
    _assert_rejects(lambda: _consensus(confidence=EtaConfidence.UNKNOWN), "known confidence")
    _consensus(
        spread_minutes=2,
        estimates=(
            EtaEstimate(EtaSource.YANDEX, 8),
            EtaEstimate(EtaSource.YANDEX_HISTORY, 10),
        ),
    )


def _assert_counts_text_is_single_line() -> None:
    text = counts_text((CountByKey("  parse_error\nblocked  ", 2), CountByKey("", 1)))
    _assert_equal(text, "parse_error blocked:2, -:1")
    if "\n" in text:
        raise AssertionError(f"expected single-line counts text, got {text!r}")


def _assert_diagnostic_sanitizer_redacts_secrets() -> None:
    fake_bot_token = "123456:" + "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    text = sanitize_diagnostic_text(
        (
            'access_token=token-secret apiKey=api-secret {"password":"json-secret"} '
            "Authorization: Bearer bearer-secret Cookie: session=browser-secret "
            f"https://user:pass@example.test/path {fake_bot_token}"
        ),
        limit=400,
    )
    _assert_contains(text, "access_token=<redacted>")
    _assert_contains(text, "apiKey=<redacted>")
    _assert_contains(text, '"password":"<redacted>"')
    _assert_contains(text, "Authorization: <redacted>")
    _assert_contains(text, "Cookie: <redacted>")
    _assert_contains(text, "<path>")
    _assert_not_contains(text, "token-secret")
    _assert_not_contains(text, "api-secret")
    _assert_not_contains(text, "json-secret")
    _assert_not_contains(text, "bearer-secret")
    _assert_not_contains(text, "browser-secret")
    _assert_not_contains(text, "user:pass")
    _assert_not_contains(text, "ABCDEFGHIJKLMNOPQRSTUVWXYZ")


def _assert_bot_latency_event_guardrails() -> None:
    current_time = datetime(2026, 6, 4, 7, 0, tzinfo=NOVOSIBIRSK_TZ)
    event = _latency_event(received_at=current_time)
    _assert_equal(event.received_at, current_time)
    _assert_equal(event.profile_key, "")
    _assert_equal(_latency_event(profile_key="morning").profile_key, "morning")

    _assert_rejects(
        lambda: _latency_event(received_at=current_time.replace(tzinfo=None)),
        "timezone-aware",
    )
    _assert_rejects(lambda: _latency_event(chat_id=True), "chat_id")
    _assert_rejects(lambda: _latency_event(update_type=" "), "update_type")
    _assert_rejects(lambda: _latency_event(update_type=" message "), "plain key")
    _assert_rejects(lambda: _latency_event(reply_source="yandex live"), "plain key")
    _assert_rejects(lambda: _latency_event(profile_key="night"), "profile_key")
    _assert_rejects(lambda: _latency_event(profile_key="morning bad"), "plain key")
    _assert_rejects(lambda: _latency_event(yandex_source_method="vehicle\nprediction"), "plain key")
    _assert_rejects(lambda: _latency_event(status=" ok"), "plain key")
    _assert_rejects(lambda: _latency_event(command=object()), "command")
    _assert_rejects(lambda: _latency_event(forecast_ms=-1), "forecast_ms")
    _assert_rejects(lambda: _latency_event(send_ms=True), "send_ms")


def _assert_bot_latency_storage_sanitizes_command_and_error() -> None:
    current_time = datetime(2026, 6, 4, 7, 0, tzinfo=NOVOSIBIRSK_TZ)
    raw_token = "123456:" + "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdef"
    with TemporaryDirectory() as temp_dir:
        with connect(Path(temp_dir) / "latency-storage.sqlite") as connection:
            init_db(connection)
            insert_bot_interaction_event(
                connection,
                _latency_event(
                    received_at=current_time,
                    command=f"/walk 14 /home/vladimir/work-projects/74 token={raw_token}",
                    status="error",
                    error=f"failed /home/vladimir/work-projects/74/.env token={raw_token}",
                ),
            )
            row = connection.execute("SELECT command, error FROM bot_interaction_events").fetchone()
            summary = summarize_bot_latency(connection, hours=1, current_time=current_time)
    if row is None:
        raise AssertionError("expected bot latency row")
    stored_command = str(row["command"])
    stored_error = str(row["error"])
    _assert_equal(stored_command, "/walk")
    _assert_not_contains(stored_command, raw_token)
    _assert_not_contains(stored_command, "/home/vladimir")
    _assert_contains(stored_error, "token=<redacted>")
    _assert_contains(stored_error, "<path>")
    _assert_not_contains(stored_error, raw_token)
    _assert_not_contains(stored_error, "/home/vladimir")
    _assert_equal(summary.error_reasons[0].key, "failed <path> token=<redacted>")


def _assert_bot_latency_summarizes_stable_error_categories() -> None:
    current_time = datetime(2026, 6, 4, 7, 0, tzinfo=NOVOSIBIRSK_TZ)
    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "latency-categories.sqlite"
        with connect(db_path) as connection:
            init_db(connection)
            for index, error in enumerate(
                (
                    "followup_send_error: TimeoutError: quick-start failed",
                    "followup_send_error: RuntimeError: notifier unavailable",
                    "send_error: RuntimeError: push blocked",
                )
            ):
                insert_bot_interaction_event(
                    connection,
                    _latency_event(
                        received_at=current_time - timedelta(minutes=index),
                        chat_id=300 + index,
                        status="error",
                        reply_source="none",
                        yandex_source_method="none",
                        error=error,
                    ),
                )
            summary = summarize_bot_latency(connection, hours=1, current_time=current_time)
    _assert_equal(summary.error_events, 3)
    _assert_equal(summary.error_categories[0], CountByKey("followup_send_error", 2))
    _assert_equal(summary.error_categories[1], CountByKey("send_error", 1))
    _assert_equal(len(summary.error_reasons), 3)
    formatted = format_bot_latency_summary(summary, db_path)
    _assert_contains(formatted, "error_categories=followup_send_error:2, send_error:1")
    _assert_contains(formatted, "error_reasons=followup_send_error:")


def _latency_event(**overrides: object) -> BotInteractionEvent:
    params = {
        "received_at": datetime(2026, 6, 4, 7, 0, tzinfo=NOVOSIBIRSK_TZ),
        "chat_id": 101,
        "update_type": "message",
        "command": "🎯 Поймать 74",
        "event_kind": BOT_EVENT_USER_REPLY,
        "reply_source": "yandex",
        "yandex_source_method": "vehicle_prediction",
        "forecast_ms": 100,
        "render_ms": 1,
        "send_ms": 50,
        "total_ms": 151,
        "status": "ok",
        "error": "",
    }
    params.update(overrides)
    return BotInteractionEvent(**params)  # type: ignore[arg-type]


def _assert_bot_latency_ignores_invalid_durations() -> None:
    current_time = datetime(2026, 6, 4, 7, 0, tzinfo=NOVOSIBIRSK_TZ)
    with TemporaryDirectory() as temp_dir:
        with connect(Path(temp_dir) / "latency.sqlite") as connection:
            init_db(connection)
            _insert_bot_latency_row(
                connection,
                received_at=current_time.isoformat(),
                forecast_ms=0.5,
                send_ms="-10",
                total_ms=0.5,
            )
            _insert_bot_latency_row(
                connection,
                received_at=current_time.isoformat(),
                forecast_ms="100",
                send_ms="50",
                total_ms="200",
            )
            connection.commit()
            summary = summarize_bot_latency(connection, hours=1, current_time=current_time)

    _assert_equal(summary.total_events, 2)
    _assert_equal(summary.p50_total_ms, 200)
    _assert_equal(summary.p95_total_ms, 200)
    _assert_equal(summary.p95_forecast_ms, 100)
    _assert_equal(summary.p95_send_ms, 50)
    _assert_equal(summary.p95_render_ms, 1)


def _assert_stats_service_summary_hours_guardrails() -> None:
    StatsService(object(), summary_hours=1)  # type: ignore[arg-type]
    _assert_rejects(
        lambda: StatsService(object(), summary_hours=0),  # type: ignore[arg-type]
        "summary_hours",
    )
    _assert_rejects(
        lambda: StatsService(object(), summary_hours=True),  # type: ignore[arg-type]
        "summary_hours",
    )


def _assert_stats_snapshot_guardrails(decision: DepartureDecision) -> None:
    snapshot = StatsSnapshot(decision, telemetry_error="\x1b[31mdatabase\nlocked\x00\x1b[0m")
    _assert_equal(snapshot.telemetry_error, "database locked")
    secret_snapshot = StatsSnapshot(decision, telemetry_error="Authorization: Bearer db-secret")
    _assert_equal(secret_snapshot.telemetry_error, "Authorization: <redacted>")
    _assert_rejects(lambda: StatsSnapshot(object()), "DepartureDecision")  # type: ignore[arg-type]
    _assert_rejects(
        lambda: StatsSnapshot(decision, telemetry=object()),  # type: ignore[arg-type]
        "YandexTelemetrySummary",
    )
    _assert_rejects(
        lambda: StatsSnapshot(decision, forecast_health=object()),  # type: ignore[arg-type]
        "ForecastHealthSummary",
    )
    _assert_rejects(
        lambda: StatsSnapshot(decision, forecast_readiness=object()),  # type: ignore[arg-type]
        "ForecastReadinessSummary",
    )
    _assert_rejects(
        lambda: StatsSnapshot(decision, forecast_backtest=object()),  # type: ignore[arg-type]
        "ForecastBacktestSummary",
    )
    _assert_rejects(
        lambda: StatsSnapshot(decision, bot_latency=object()),  # type: ignore[arg-type]
        "BotLatencySummary",
    )
    _assert_rejects(
        lambda: StatsSnapshot(decision, runtime_quality=object()),  # type: ignore[arg-type]
        "BotRuntimePredictionQuality",
    )
    _assert_rejects(
        lambda: StatsSnapshot(decision, runtime_calibration=object()),  # type: ignore[arg-type]
        "BotRuntimeCalibration",
    )
    _assert_rejects(
        lambda: StatsSnapshot(decision, watch_state=object()),  # type: ignore[arg-type]
        "WatchStateSummary",
    )
    _assert_rejects(lambda: StatsSnapshot(decision, build_info=object()), "BuildInfo")  # type: ignore[arg-type]
    _assert_rejects(lambda: StatsSnapshot(decision, telemetry_error=object()), "text")  # type: ignore[arg-type]
    _assert_rejects(lambda: StatsSnapshot(decision, forecast_health_error=object()), "text")  # type: ignore[arg-type]
    _assert_rejects(lambda: StatsSnapshot(decision, forecast_readiness_error=object()), "text")  # type: ignore[arg-type]
    _assert_rejects(lambda: StatsSnapshot(decision, forecast_backtest_error=object()), "text")  # type: ignore[arg-type]
    _assert_rejects(lambda: StatsSnapshot(decision, bot_latency_error=object()), "text")  # type: ignore[arg-type]
    _assert_rejects(lambda: StatsSnapshot(decision, runtime_error=object()), "text")  # type: ignore[arg-type]
    _assert_rejects(lambda: StatsSnapshot(decision, watch_state_error=object()), "text")  # type: ignore[arg-type]
    _assert_rejects(lambda: StatsSnapshot(decision, bot_latency_command=object()), "text")  # type: ignore[arg-type]
    _assert_rejects(lambda: StatsSnapshot(decision, forecast_readiness_command=object()), "text")  # type: ignore[arg-type]
    _assert_rejects(lambda: StatsSnapshot(decision, forecast_coverage_command=object()), "text")  # type: ignore[arg-type]
    _assert_rejects(lambda: StatsSnapshot(decision, forecast_backtest_command=object()), "text")  # type: ignore[arg-type]
    _assert_rejects(lambda: StatsSnapshot(decision, support_report_command=object()), "text")  # type: ignore[arg-type]
    _assert_rejects(lambda: StatsSnapshot(decision, prediction_evaluate_command=object()), "text")  # type: ignore[arg-type]
    _assert_rejects(lambda: StatsSnapshot(decision, watch_state_command=object()), "text")  # type: ignore[arg-type]
    _assert_rejects(lambda: StatsSnapshot(decision, triage_action_command=object()), "text")  # type: ignore[arg-type]
    _assert_rejects(lambda: StatsSnapshot(decision, triage_action_key=object()), "text")  # type: ignore[arg-type]
    _assert_rejects(lambda: StatsSnapshot(decision, triage_action_message=object()), "text")  # type: ignore[arg-type]
    watch_error_snapshot = StatsSnapshot(decision, watch_state_error="\x1b[31mopen\nfailed\x00\x1b[0m")
    _assert_equal(watch_error_snapshot.watch_state_error, "open failed")
    watch_command_snapshot = StatsSnapshot(
        decision,
        watch_state_command="route74 watch-state\n--path /home/vladimir/work-projects/74/data/web_watches.json",
    )
    _assert_equal(watch_command_snapshot.watch_state_command, "route74 watch-state --path <path>")
    coverage_command_snapshot = StatsSnapshot(
        decision,
        forecast_coverage_command="route74 forecast-coverage\n--window weekday_morning_09_12",
    )
    _assert_equal(
        coverage_command_snapshot.forecast_coverage_command, "route74 forecast-coverage --window weekday_morning_09_12"
    )
    backtest_command_snapshot = StatsSnapshot(
        decision,
        forecast_backtest_command="route74 forecast-backtest\n--window weekday_morning_09_12",
    )
    _assert_equal(
        backtest_command_snapshot.forecast_backtest_command, "route74 forecast-backtest --window weekday_morning_09_12"
    )
    triage_snapshot = StatsSnapshot(
        decision,
        triage_action_command="route74 support-report\n--profile /home/vladimir/work-projects/74",
        triage_action_key="bot_runtime_pending\nbad",
        triage_action_message="pending\nold",
    )
    _assert_equal(triage_snapshot.triage_action_command, "route74 support-report --profile <path>")
    _assert_equal(triage_snapshot.triage_action_key, "bot_runtime_pending bad")
    _assert_equal(triage_snapshot.triage_action_message, "pending old")
    _assert_rejects(
        lambda: StatsSnapshot(decision, telemetry=_telemetry_summary(None), telemetry_error="database locked"),
        "telemetry_error",
    )
    _assert_rejects(
        lambda: StatsSnapshot(
            decision,
            forecast_health=_forecast_health_summary(decision.current_time),
            forecast_health_error="database locked",
        ),
        "forecast_health_error",
    )
    _assert_rejects(
        lambda: StatsSnapshot(
            decision,
            forecast_readiness=_forecast_readiness_summary(decision.current_time),
            forecast_readiness_error="database locked",
        ),
        "forecast_readiness_error",
    )
    _assert_rejects(
        lambda: StatsSnapshot(
            decision,
            forecast_backtest=_forecast_backtest_summary(),
            forecast_backtest_error="database locked",
        ),
        "forecast_backtest_error",
    )
    _assert_rejects(
        lambda: StatsSnapshot(decision, bot_latency=_bot_latency_summary(), bot_latency_error="database locked"),
        "bot_latency_error",
    )
    _assert_rejects(
        lambda: StatsSnapshot(decision, runtime_quality=_runtime_quality()),
        "runtime diagnostics",
    )
    _assert_rejects(
        lambda: StatsSnapshot(
            decision,
            runtime_quality=_runtime_quality(),
            runtime_calibration=_runtime_calibration(),
            runtime_error="database locked",
        ),
        "runtime_error",
    )
    _assert_rejects(
        lambda: StatsSnapshot(
            decision,
            watch_state=_watch_summary(status="ok"),
            watch_state_error="watch state locked",
        ),
        "watch_state_error",
    )


def _assert_stats_telemetry_failure_degrades(decision: DepartureDecision) -> None:
    formatted = format_stats_message(StatsSnapshot(decision, telemetry_error="database\nlocked"))
    _assert_contains(formatted, "🗄️ Сбор: недоступен · database locked")
    _assert_not_contains(formatted, "database\nlocked")
    _assert_not_contains(formatted, "💓 Collector")

    noisy_error = _telemetry_error(sqlite3.OperationalError("\x1b[31mdatabase\nlocked\x00\x1b[0m"))
    _assert_equal(noisy_error, "OperationalError: database locked")
    noisy_formatted = format_stats_message(StatsSnapshot(decision, telemetry_error=noisy_error))
    _assert_contains(noisy_formatted, "🗄️ Сбор: недоступен · OperationalError: database locked")
    _assert_not_contains(noisy_formatted, "\x1b")
    _assert_not_contains(noisy_formatted, "\x00")

    path_error = _telemetry_error(OSError("open failed: /home/vladimir/work-projects/74/data/route74.sqlite"))
    _assert_equal(path_error, "OSError: open failed: <path>")
    _assert_not_contains(path_error, "/home/vladimir")

    secret_error = _telemetry_error(OSError("access_token=token-secret apiKey=api-secret"))
    _assert_contains(secret_error, "access_token=<redacted>")
    _assert_contains(secret_error, "apiKey=<redacted>")
    _assert_not_contains(secret_error, "token-secret")
    _assert_not_contains(secret_error, "api-secret")

    with TemporaryDirectory() as temp_dir:
        bad_db_path = Path(temp_dir) / "route74.sqlite"
        bad_db_path.mkdir()
        snapshot = StatsService(
            FakeStatsCommuteService(decision),  # type: ignore[arg-type]
            db_path=bad_db_path,
        ).build(MORNING, 12)

    _assert_equal(snapshot.decision, decision)
    _assert_equal(snapshot.telemetry, None)
    _assert_equal(snapshot.runtime_quality, None)
    _assert_equal(snapshot.support_report_command, "route74 support-report --profile morning")
    _assert_equal(snapshot.prediction_evaluate_command, "route74 prediction-evaluate --window weekday_morning_09_12")
    _assert_contains(snapshot.telemetry_error, "OperationalError")
    _assert_contains(snapshot.forecast_health_error, "OperationalError")
    _assert_contains(snapshot.forecast_readiness_error, "OperationalError")
    _assert_contains(snapshot.forecast_backtest_error, "OperationalError")
    _assert_contains(snapshot.runtime_error, "OperationalError")
    _assert_contains(format_stats_message(snapshot), "🗄️ Сбор: недоступен · OperationalError")
    _assert_contains(format_stats_message(snapshot), "🩺 Прогноз: недоступен · OperationalError")
    _assert_contains(format_stats_message(snapshot), "📚 История: недоступна · OperationalError")
    _assert_contains(format_stats_message(snapshot), "🧪 Качество истории: недоступно · OperationalError")
    _assert_contains(format_stats_message(snapshot), "🌐 Runtime: недоступен · OperationalError")


def _assert_stats_partial_summary_failure_degrades(decision: DepartureDecision) -> None:
    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "route74.sqlite"
        with patch(
            "route74.services.stats.summarize_yandex_telemetry",
            side_effect=sqlite3.OperationalError("telemetry\nlocked"),
        ):
            telemetry_failed = StatsService(
                FakeStatsCommuteService(decision),  # type: ignore[arg-type]
                db_path=db_path,
            ).build(MORNING, 12)

        with patch(
            "route74.services.stats.summarize_bot_runtime_predictions",
            side_effect=sqlite3.OperationalError("runtime\nlocked"),
        ):
            runtime_failed = StatsService(
                FakeStatsCommuteService(decision),  # type: ignore[arg-type]
                db_path=db_path,
            ).build(MORNING, 12)

        with patch(
            "route74.services.stats.summarize_bot_latency",
            side_effect=sqlite3.OperationalError("latency\nlocked"),
        ):
            latency_failed = StatsService(
                FakeStatsCommuteService(decision),  # type: ignore[arg-type]
                db_path=db_path,
            ).build(MORNING, 12)

        with patch(
            "route74.services.stats.summarize_yandex_forecast_backtest",
            side_effect=sqlite3.OperationalError("backtest\nlocked"),
        ):
            backtest_failed = StatsService(
                FakeStatsCommuteService(decision),  # type: ignore[arg-type]
                db_path=db_path,
            ).build(MORNING, 12)

    _assert_equal(telemetry_failed.telemetry, None)
    _assert_contains(telemetry_failed.telemetry_error, "OperationalError: telemetry locked")
    if telemetry_failed.bot_latency is None:
        raise AssertionError("expected bot latency summary when telemetry summary fails")
    _assert_equal(telemetry_failed.runtime_error, "")
    if telemetry_failed.runtime_quality is None or telemetry_failed.runtime_calibration is None:
        raise AssertionError("expected runtime diagnostics when telemetry summary fails")
    telemetry_failed_message = format_stats_message(telemetry_failed)
    _assert_contains(telemetry_failed_message, "🗄️ Сбор: недоступен · OperationalError: telemetry locked")
    _assert_contains(telemetry_failed_message, "🌐 Runtime 24ч: по этому направлению фактов пока нет")
    _assert_not_contains(telemetry_failed_message, "🌐 Runtime: недоступен")

    if runtime_failed.telemetry is None:
        raise AssertionError("expected telemetry summary when runtime summary fails")
    if runtime_failed.bot_latency is None:
        raise AssertionError("expected bot latency summary when runtime summary fails")
    _assert_equal(runtime_failed.telemetry_error, "")
    _assert_equal(runtime_failed.runtime_quality, None)
    _assert_equal(runtime_failed.runtime_calibration, None)
    _assert_contains(runtime_failed.runtime_error, "OperationalError: runtime locked")
    runtime_failed_message = format_stats_message(runtime_failed)
    _assert_contains(runtime_failed_message, "🗄️ Сбор 24ч: snapshots 0")
    _assert_contains(runtime_failed_message, "🌐 Runtime: недоступен · OperationalError: runtime locked")
    _assert_not_contains(runtime_failed_message, "🗄️ Сбор: недоступен")

    if latency_failed.telemetry is None:
        raise AssertionError("expected telemetry summary when latency summary fails")
    if latency_failed.runtime_quality is None or latency_failed.runtime_calibration is None:
        raise AssertionError("expected runtime diagnostics when latency summary fails")
    _assert_equal(latency_failed.bot_latency, None)
    _assert_contains(latency_failed.bot_latency_error, "OperationalError: latency locked")
    latency_failed_message = format_stats_message(latency_failed)
    _assert_contains(latency_failed_message, "🔔 Уведомления: недоступны · OperationalError: latency locked")
    _assert_contains(latency_failed_message, "🌐 Runtime 24ч: по этому направлению фактов пока нет")
    _assert_not_contains(latency_failed_message, "🗄️ Сбор: недоступен")

    _assert_equal(backtest_failed.forecast_backtest, None)
    _assert_contains(backtest_failed.forecast_backtest_error, "OperationalError: backtest locked")
    if backtest_failed.telemetry is None:
        raise AssertionError("expected telemetry summary when forecast backtest summary fails")
    backtest_failed_message = format_stats_message(backtest_failed)
    _assert_contains(
        backtest_failed_message,
        "🧪 Качество истории: недоступно · OperationalError: backtest locked · route74 forecast-backtest --window weekday_morning_09_12",
    )
    _assert_contains(backtest_failed_message, "🗄️ Сбор 24ч: snapshots 0")


def _assert_stats_service_reads_forecast_health_summary(decision: DepartureDecision) -> None:
    forecast_health = _forecast_health_summary(
        decision.current_time,
        morning_status="no_collector_runs",
        morning_reason="collector has no recorded runs in this report window",
        morning_ready_buckets=2,
        morning_total_buckets=6,
        morning_fresh_eta_samples=2,
        morning_total_samples=12,
        collector_status="missing",
        collector_message="collector heartbeat is absent",
        canary_status="warning",
        canary_reason="missing canary profiles: morning",
    )
    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "route74.sqlite"
        with (
            patch(
                "route74.services.stats.summarize_forecast_health",
                return_value=forecast_health,
            ),
            patch(
                "route74.services.stats.summarize_monitor",
                return_value=SimpleNamespace(runtime=None, calibration=None),
            ),
        ):
            snapshot = StatsService(
                FakeStatsCommuteService(decision),  # type: ignore[arg-type]
                db_path=db_path,
            ).build(MORNING, 12)

    _assert_equal(snapshot.forecast_health, forecast_health)
    _assert_equal(snapshot.forecast_health_error, "")
    _assert_contains(
        format_stats_message(snapshot),
        "🩺 Прогноз: 1/2 окон готовы · weekday_morning_09_12 no_collector_runs · collector missing · canary warning",
    )


def _assert_stats_service_reads_forecast_backtest_summary(decision: DepartureDecision) -> None:
    forecast_backtest = _forecast_backtest_summary()
    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "route74.sqlite"
        with (
            patch(
                "route74.services.stats.summarize_yandex_forecast_backtest",
                return_value=forecast_backtest,
            ),
            patch(
                "route74.services.stats.summarize_monitor",
                return_value=SimpleNamespace(runtime=None, calibration=None),
            ),
        ):
            snapshot = StatsService(
                FakeStatsCommuteService(decision),  # type: ignore[arg-type]
                db_path=db_path,
            ).build(MORNING, 12)

    _assert_equal(snapshot.forecast_backtest, forecast_backtest)
    _assert_equal(snapshot.forecast_backtest_error, "")
    _assert_equal(snapshot.forecast_backtest_command, "route74 forecast-backtest --window weekday_morning_09_12")
    _assert_contains(
        format_stats_message(snapshot),
        "🧪 Качество истории: p80 · проверено 6/8 · промахов 3 (50%) · точность 50% · ср. ошибка 2.5м · route74 forecast-backtest --window weekday_morning_09_12",
    )


def _assert_stats_service_reads_bot_latency_summary(decision: DepartureDecision) -> None:
    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "route74.sqlite"
        with connect(db_path) as connection:
            init_db(connection)
            for index, reply_source in enumerate(("no_eta", "no_eta", "no_eta", "yandex")):
                insert_bot_interaction_event(
                    connection,
                    _latency_event(
                        received_at=decision.current_time - timedelta(minutes=17 + index),
                        chat_id=200 + index,
                        profile_key=MORNING.key,
                        reply_source=reply_source,
                        yandex_source_method="none" if reply_source == "no_eta" else "vehicle_prediction",
                        no_eta_reason="yandex_no_target+history_insufficient" if reply_source == "no_eta" else "",
                        total_ms=110 + index,
                    ),
                )
            insert_bot_interaction_event(
                connection,
                _latency_event(
                    received_at=decision.current_time - timedelta(minutes=5),
                    chat_id=300,
                    profile_key=EVENING.key,
                    reply_source="no_eta",
                    yandex_source_method="none",
                    total_ms=120,
                ),
            )
            connection.commit()
        snapshot = StatsService(
            FakeStatsCommuteService(decision),  # type: ignore[arg-type]
            db_path=db_path,
        ).build(MORNING, 12)

    if snapshot.bot_latency is None:
        raise AssertionError("expected bot latency summary in stats snapshot")
    _assert_equal(snapshot.bot_latency.total_events, 4)
    _assert_equal(snapshot.bot_latency.no_eta_events, 3)
    _assert_equal(snapshot.bot_latency.no_eta_rate_percent, 75)
    _assert_equal(snapshot.bot_latency.profile_key, MORNING.key)
    _assert_equal(
        snapshot.bot_latency.latest_received_at,
        decision.current_time - timedelta(minutes=17),
    )
    _assert_equal(
        snapshot.bot_latency_command, "route74 runtime-latency --hours 24 --profile morning --event-kind user_reply"
    )
    _assert_equal(
        snapshot.triage_action_command, "route74 runtime-latency --hours 24 --profile morning --event-kind user_reply"
    )
    _assert_equal(snapshot.triage_action_key, "bot_no_eta_replies")
    message = format_stats_message(snapshot)
    _assert_contains(
        message,
        "🔔 Runtime 24ч: ответов 4 · ошибки 0 (0%) · без ETA 3 (75%) · "
        "чаще всего: Яндекс: нет нашей остановки; история: мало данных (3) · p95 113мс · "
        "отправка 50мс · доп. сообщения 1мс",
    )
    _assert_contains(message, "свежесть 17 мин назад")
    _assert_contains(
        message,
        "🧰 Разбор: route74 runtime-latency --hours 24 --profile morning --event-kind user_reply · много ответов без ETA",
    )
    _assert_stats_service_watch_state_beats_bot_latency(decision)


def _assert_stats_message_uses_bot_error_category(decision: DepartureDecision) -> None:
    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "route74.sqlite"
        with connect(db_path) as connection:
            init_db(connection)
            insert_bot_interaction_event(
                connection,
                _latency_event(
                    received_at=decision.current_time - timedelta(minutes=4),
                    profile_key=MORNING.key,
                    reply_source="none",
                    yandex_source_method="none",
                    status="error",
                    error="send_error: RuntimeError: notifier unavailable",
                    total_ms=250,
                ),
            )
            connection.commit()
        snapshot = StatsService(
            FakeStatsCommuteService(decision),  # type: ignore[arg-type]
            db_path=db_path,
        ).build(MORNING, 12)

    if snapshot.bot_latency is None:
        raise AssertionError("expected bot latency summary in stats snapshot")
    _assert_equal(snapshot.bot_latency.error_categories[0], CountByKey("send_error", 1))
    _assert_contains(
        format_stats_message(snapshot),
        "🔔 Runtime 24ч: ответов 1 · ошибки 1 (100%) · причина основной ответ не ушёл:1 · p95 250мс",
    )


def _assert_stats_triage_prefers_pending_over_forecast_window() -> None:
    triage = SupportTriage(
        status=TRIAGE_WARNING,
        primary_action="route74 forecast-health",
        items=(
            SupportTriageItem(
                TRIAGE_WARNING,
                "forecast_window",
                "window=weekday_morning_09_12 status=no_collector_runs reason=collector has not produced report-window snapshots yet",
                "route74 forecast-health",
            ),
            SupportTriageItem(
                TRIAGE_WARNING,
                "bot_runtime_pending",
                "profile=morning pending=3/3(100%) oldest_pending=180m",
                "route74 prediction-evaluate --window weekday_morning_09_12",
            ),
        ),
    )
    item = operator_primary_triage_item(triage)
    if item is None:
        raise AssertionError("expected stats triage item")
    _assert_equal(item.key, "bot_runtime_pending")
    _assert_equal(item.action, "route74 prediction-evaluate --window weekday_morning_09_12")


def _assert_stats_triage_prefers_runtime_over_forecast_window() -> None:
    runtime_cases = (
        (
            SupportTriageItem(
                TRIAGE_WARNING,
                "bot_runtime_guardrail_unavailable",
                "guardrail_unavailable=2/2(100%)",
                "route74 support-report --profile morning",
            ),
            "route74 support-report --profile morning",
        ),
        (
            SupportTriageItem(
                TRIAGE_WARNING,
                "bot_runtime_misses",
                "misses=2/3(67%)",
                "route74 runtime-events --hours 24 --limit 8 --profile morning --event-kind user_reply",
            ),
            "route74 runtime-events --hours 24 --limit 8 --profile morning --event-kind user_reply",
        ),
        (
            SupportTriageItem(
                TRIAGE_WARNING,
                "bot_runtime_p50_error",
                "p50_abs_error=4m",
                "route74 runtime-events --hours 24 --limit 8 --profile morning --event-kind user_reply",
            ),
            "route74 runtime-events --hours 24 --limit 8 --profile morning --event-kind user_reply",
        ),
        (
            SupportTriageItem(
                TRIAGE_WARNING,
                "bot_runtime_late_risk",
                "profile=morning eval=3/3 miss=1(33%) suggested=+1m",
                "route74 runtime-events --hours 24 --limit 8 --profile morning --event-kind user_reply",
            ),
            "route74 runtime-events --hours 24 --limit 8 --profile morning --event-kind user_reply",
        ),
        (
            SupportTriageItem(
                TRIAGE_WARNING,
                "bot_runtime_source_late_risk",
                "profile=morning source=target_stop_live eval=3/3 miss=1(33%) suggested=+1m",
                "route74 prediction-calibration --window weekday_morning_09_12",
            ),
            "route74 prediction-calibration --window weekday_morning_09_12",
        ),
    )
    for runtime_item, expected_action in runtime_cases:
        triage = SupportTriage(
            status=TRIAGE_WARNING,
            primary_action="route74 forecast-health",
            items=(
                SupportTriageItem(
                    TRIAGE_WARNING,
                    "forecast_window",
                    "window=weekday_morning_09_12 status=no_collector_runs reason=collector has not produced report-window snapshots yet",
                    "route74 forecast-health",
                ),
                runtime_item,
            ),
        )
        item = operator_primary_triage_item(triage)
        if item is None:
            raise AssertionError("expected stats triage item")
        _assert_equal(item.key, runtime_item.key)
        _assert_equal(item.action, expected_action)


def _assert_stats_triage_reports_history_readiness(decision: DepartureDecision) -> None:
    snapshot = StatsSnapshot(
        decision,
        triage_action_command="route74 forecast-readiness --window weekday_morning_09_12",
        triage_action_key="history_readiness",
        triage_action_message="window=weekday_morning_09_12 bucket=+/-30m samples=1/20 days=1/3",
    )
    _assert_equal(snapshot.triage_action_command, "route74 forecast-readiness --window weekday_morning_09_12")
    _assert_equal(snapshot.triage_action_key, "history_readiness")
    message = format_stats_message(snapshot)
    _assert_contains(
        message,
        "🧰 Разбор: route74 forecast-readiness --window weekday_morning_09_12 · проверить готовность истории",
    )


def _assert_stats_triage_reports_history_backtest(decision: DepartureDecision) -> None:
    snapshot = StatsSnapshot(
        decision,
        triage_action_command="route74 forecast-backtest --window weekday_morning_09_12",
        triage_action_key="history_backtest",
        triage_action_message="window=weekday_morning_09_12 p80 miss=3/6(50%) bucket_accuracy=3/6(50%)",
    )
    _assert_equal(snapshot.triage_action_command, "route74 forecast-backtest --window weekday_morning_09_12")
    _assert_equal(snapshot.triage_action_key, "history_backtest")
    message = format_stats_message(snapshot)
    _assert_contains(
        message,
        "🧰 Разбор: route74 forecast-backtest --window weekday_morning_09_12 · проверить качество истории",
    )


def _assert_stats_triage_reports_integrity_gap(decision: DepartureDecision) -> None:
    snapshot = StatsSnapshot(
        decision,
        triage_action_command="route74 forecast-health",
        triage_action_key="integrity_gap",
        triage_action_message="forecast_only=3 report_only=1",
    )
    _assert_equal(snapshot.triage_action_command, "route74 forecast-health")
    _assert_equal(snapshot.triage_action_key, "integrity_gap")
    message = format_stats_message(snapshot)
    _assert_contains(
        message,
        "🧰 Разбор: route74 forecast-health · проверить расхождение витрин · forecast_only=3 report_only=1",
    )


def _assert_stats_forecast_health_reports_integrity_gap(decision: DepartureDecision) -> None:
    forecast_health = _forecast_health_summary(
        decision.current_time,
        morning_status="integrity_gap",
        morning_reason="forecast/report-window tables disagree",
        morning_forecast_without_report_samples=3,
        morning_report_without_forecast_samples=1,
    )
    message = format_stats_message(StatsSnapshot(decision, forecast_health=forecast_health))
    _assert_contains(
        message,
        "🩺 Прогноз: 1/2 окон готовы · weekday_morning_09_12 integrity_gap · forecast_only=3 report_only=1",
    )


def _assert_stats_forecast_health_reports_missing_buckets(decision: DepartureDecision) -> None:
    forecast_health = _forecast_health_summary(
        decision.current_time,
        morning_status="insufficient_bucket_coverage",
        morning_reason="missing ready forecast buckets",
        morning_ready_buckets=1,
        morning_total_buckets=6,
        morning_missing_bucket_labels=("09:00", "09:30", "10:00", "10:30", "11:00"),
    )
    message = format_stats_message(StatsSnapshot(decision, forecast_health=forecast_health))
    _assert_contains(
        message,
        "weekday_morning_09_12 insufficient_bucket_coverage · не хватает 09:00, 09:30, 10:00, 10:30,+1",
    )


def _assert_stats_service_runtime_summary_is_profile_scoped(decision: DepartureDecision) -> None:
    sampled_at = decision.current_time - timedelta(minutes=10)
    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "route74.sqlite"
        with connect(db_path) as connection:
            init_db(connection)
            morning_prediction_id = _insert_stats_runtime_prediction(
                connection,
                profile_key=MORNING.key,
                sampled_at=sampled_at,
                source=SOURCE_TARGET_STOP_LIVE,
                source_method="vehicle_prediction",
                predicted_minutes=14,
            )
            _insert_stats_runtime_evaluation(
                connection,
                prediction_id=morning_prediction_id,
                profile_key=MORNING.key,
                sampled_at=sampled_at,
                source=SOURCE_TARGET_STOP_LIVE,
                predicted_minutes=14,
                error_minutes=-2,
            )
            evening_prediction_id = _insert_stats_runtime_prediction(
                connection,
                profile_key=EVENING.key,
                sampled_at=sampled_at + timedelta(minutes=1),
                source=SOURCE_HISTORY_HEADWAY,
                source_method="history",
                predicted_minutes=26,
            )
            _insert_stats_runtime_evaluation(
                connection,
                prediction_id=evening_prediction_id,
                profile_key=EVENING.key,
                sampled_at=sampled_at + timedelta(minutes=1),
                source=SOURCE_HISTORY_HEADWAY,
                predicted_minutes=26,
                error_minutes=-4,
            )
            connection.commit()
        snapshot = StatsService(
            FakeStatsCommuteService(decision),  # type: ignore[arg-type]
            db_path=db_path,
        ).build(MORNING, 12)

    quality = snapshot.runtime_quality
    calibration = snapshot.runtime_calibration
    if quality is None or calibration is None:
        raise AssertionError("expected scoped runtime diagnostics in stats snapshot")
    _assert_equal(quality.total, 1)
    _assert_equal(tuple(group.key for group in quality.by_profile), (MORNING.key,))
    _assert_equal(tuple(group.key for group in quality.by_source), (SOURCE_TARGET_STOP_LIVE,))
    _assert_equal(calibration.total, 1)
    _assert_equal(tuple(group.key for group in calibration.by_profile), (MORNING.key,))
    _assert_equal(
        tuple(group.key for group in calibration.by_profile_source),
        (f"{MORNING.key}/{SOURCE_TARGET_STOP_LIVE}",),
    )
    message = format_stats_message(snapshot)
    _assert_contains(message, "🌐 Runtime 24ч: прогнозов 1")
    _assert_contains(message, "🔎 Источник фактов: Яндекс live")


def _assert_stats_service_watch_state_beats_bot_latency(decision: DepartureDecision) -> None:
    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "route74.sqlite"
        watch_state_path = Path(temp_dir) / "bot-watches.json"
        with connect(db_path) as connection:
            init_db(connection)
            for index in range(3):
                insert_bot_interaction_event(
                    connection,
                    _latency_event(
                        received_at=decision.current_time - timedelta(minutes=10 + index),
                        chat_id=200 + index,
                        profile_key=MORNING.key,
                        reply_source="no_eta",
                        yandex_source_method="none",
                        total_ms=120 + index,
                    ),
                )
            insert_bot_interaction_event(
                connection,
                _latency_event(
                    received_at=decision.current_time - timedelta(minutes=4),
                    chat_id=999,
                    profile_key=MORNING.key,
                    reply_source="yandex",
                    yandex_source_method="vehicle_prediction",
                    total_ms=160,
                ),
            )
            connection.commit()
        watch_state_path.write_text("{not-json", encoding="utf-8")
        snapshot = StatsService(
            FakeStatsCommuteService(decision),  # type: ignore[arg-type]
            db_path=db_path,
            watch_state_path=watch_state_path,
        ).build(MORNING, 12)

    if snapshot.watch_state is None:
        raise AssertionError("expected watch state summary in stats snapshot")
    _assert_equal(snapshot.watch_state.status, "critical")
    _assert_equal(snapshot.watch_state_command, "route74 watch-state --path <path>")
    _assert_equal(
        snapshot.bot_latency_command, "route74 runtime-latency --hours 24 --profile morning --event-kind user_reply"
    )
    _assert_contains(
        format_stats_message(snapshot),
        "🧰 Разбор: route74 watch-state --path <path> · проверить watch-state",
    )


def _bot_latency_summary(
    *,
    total_events: int = 0,
    error_events: int = 0,
    no_eta_events: int = 0,
    p95_total_ms: int | None = None,
    invalid_duration_events: int = 0,
    latest_received_at: datetime | None = None,
    error_reasons: tuple[CountByKey, ...] | None = None,
    error_categories: tuple[CountByKey, ...] | None = None,
) -> BotLatencySummary:
    ok_events = max(0, total_events - error_events)
    reply_sources: list[CountByKey] = []
    if no_eta_events:
        reply_sources.append(CountByKey("no_eta", no_eta_events))
    if total_events - no_eta_events:
        reply_sources.append(CountByKey("yandex", total_events - no_eta_events))
    return BotLatencySummary(
        hours=24,
        latest_received_at=latest_received_at,
        total_events=total_events,
        invalid_duration_events=invalid_duration_events,
        error_events=error_events,
        no_eta_events=no_eta_events,
        p50_total_ms=p95_total_ms,
        p95_total_ms=p95_total_ms,
        p95_forecast_ms=None,
        p95_send_ms=None,
        statuses=(CountByKey("ok", ok_events), CountByKey("error", error_events))
        if error_events
        else (CountByKey("ok", ok_events),),
        source_methods=(CountByKey("vehicle_prediction", total_events),) if total_events else (),
        update_types=(CountByKey("message", total_events),) if total_events else (),
        event_kinds=(CountByKey(BOT_EVENT_USER_REPLY, total_events),) if total_events else (),
        reply_sources=tuple(reply_sources),
        error_reasons=error_reasons
        if error_reasons is not None
        else ((CountByKey("local smoke failure", error_events),) if error_events else ()),
        error_categories=error_categories if error_categories is not None else (),
    )


def _telemetry_summary(heartbeat: CollectorHeartbeat | None) -> YandexTelemetrySummary:
    return YandexTelemetrySummary(
        profile_key=MORNING.key,
        hours=24,
        total_snapshots=0,
        eta_snapshots=0,
        vehicle_snapshots=0,
        total_observations=0,
        eta_observations=0,
        latest_sampled_at=None,
        heartbeat=heartbeat,
        collector_runs=CollectorRunSummary(
            name="yandex-collect",
            hours=24,
            total_runs=0,
            result_runs=0,
            eta_runs=0,
            traffic_ok_runs=0,
            skipped_runs=0,
            latest_started_at=None,
            statuses=(),
        ),
        statuses=(),
        methods=(),
    )


def _forecast_health_summary(
    current_time: datetime,
    *,
    morning_status: str = "ready",
    morning_reason: str = "ready",
    morning_ready_buckets: int = 6,
    morning_total_buckets: int = 6,
    morning_fresh_eta_samples: int = 12,
    morning_total_samples: int = 12,
    morning_missing_bucket_labels: tuple[str, ...] = (),
    morning_forecast_without_report_samples: int = 0,
    morning_report_without_forecast_samples: int = 0,
    collector_status: str = "ok",
    collector_message: str = "ok",
    canary_status: str = "ok",
    canary_reason: str = "ok",
) -> ForecastHealthSummary:
    canary_risky_runs = 1 if canary_status == "warning" else 0
    return ForecastHealthSummary(
        days=30,
        min_samples=20,
        min_distinct_days=3,
        collector=ForecastCollectorHealth(
            name="yandex-collect",
            status=collector_status,
            message=collector_message,
            updated_at=current_time,
            age_seconds=0,
            max_age_seconds=120,
        ),
        canary=YandexCanaryHealth(canary_status, current_time, canary_reason, canary_risky_runs),
        windows=(
            _forecast_window(
                "weekday_morning_09_12",
                MORNING.key,
                status=morning_status,
                reason=morning_reason,
                ready_buckets=morning_ready_buckets,
                total_buckets=morning_total_buckets,
                fresh_eta_samples=morning_fresh_eta_samples,
                total_samples=morning_total_samples,
                missing_bucket_labels=morning_missing_bucket_labels,
                forecast_without_report_samples=morning_forecast_without_report_samples,
                report_without_forecast_samples=morning_report_without_forecast_samples,
                current_time=current_time,
            ),
            _forecast_window(
                "weekday_evening_19_22",
                EVENING.key,
                status="ready",
                reason="ready",
                ready_buckets=6,
                total_buckets=6,
                fresh_eta_samples=12,
                total_samples=12,
                current_time=current_time,
            ),
        ),
    )


def _forecast_window(
    window_key: str,
    profile_key: str,
    *,
    status: str,
    reason: str,
    ready_buckets: int,
    total_buckets: int,
    fresh_eta_samples: int,
    total_samples: int,
    missing_bucket_labels: tuple[str, ...] = (),
    forecast_without_report_samples: int = 0,
    report_without_forecast_samples: int = 0,
    current_time: datetime,
) -> ForecastWindowHealth:
    return ForecastWindowHealth(
        window_key=window_key,
        profile_key=profile_key,
        status=status,
        reason=reason,
        total_samples=total_samples,
        eta_samples=fresh_eta_samples,
        fresh_eta_samples=fresh_eta_samples,
        traffic_samples=0,
        ready_buckets=ready_buckets,
        total_buckets=total_buckets,
        forecast_without_report_samples=forecast_without_report_samples,
        report_without_forecast_samples=report_without_forecast_samples,
        collector_runs=0,
        collector_eta_runs=0,
        collector_traffic_ok_runs=0,
        collector_run_statuses=(),
        api_risk_samples=0,
        api_risk_reasons=(),
        coordinate_fallback_samples=0,
        coordinate_fallback_reasons=(),
        arrival_events=0,
        prediction_events=0,
        prediction_evaluations=0,
        prediction_miss_cases=0,
        bot_prediction_events=0,
        bot_prediction_evaluations=0,
        bot_prediction_miss_cases=0,
        truth_status="ready",
        truth_reason="ready",
        latest_arrival_at=None,
        collector_latest_started_at=None,
        missing_bucket_labels=missing_bucket_labels,
        bucket_gaps=(),
        latest_sampled_at=current_time,
    )


def _forecast_backtest_summary(
    *,
    evaluated_cases: int = 6,
    target_cases: int = 8,
    miss_cases: int = 3,
    bucket_accurate_cases: int = 3,
) -> ForecastBacktestSummary:
    return ForecastBacktestSummary(
        profile_key=MORNING.key,
        report_window_key="weekday_morning_09_12",
        history_days=14,
        bucket_minutes=30,
        min_samples=20,
        min_distinct_days=3,
        percentiles=(80,),
        target_cases=target_cases,
        results=(
            ForecastBacktestResult(
                percentile=80,
                evaluated_cases=evaluated_cases,
                skipped_cases=max(0, target_cases - evaluated_cases),
                miss_cases=miss_cases,
                bucket_accurate_cases=bucket_accurate_cases,
                miss_minutes=9,
                extra_wait_minutes=4,
                mean_absolute_error=2.5,
            ),
        ),
    )


def _forecast_readiness_summary(
    current_time: datetime,
    *,
    primary_samples: int = 12,
    fallback_samples: int = 10,
    primary_distinct_days: int = 2,
    fallback_distinct_days: int = 2,
) -> ForecastReadinessSummary:
    fresh_eta_samples = max(primary_samples, fallback_samples)
    return ForecastReadinessSummary(
        profile_key=MORNING.key,
        report_window_key="weekday_morning_09_12",
        current_time=current_time,
        days=30,
        min_samples=20,
        min_distinct_days=3,
        primary_bucket_minutes=30,
        fallback_bucket_minutes=60,
        max_age_seconds=86_400,
        total_samples=fresh_eta_samples,
        eta_samples=fresh_eta_samples,
        fresh_eta_samples=fresh_eta_samples,
        traffic_samples=0,
        primary_samples=primary_samples,
        fallback_samples=fallback_samples,
        primary_distinct_days=primary_distinct_days,
        fallback_distinct_days=fallback_distinct_days,
        latest_sampled_at=current_time - timedelta(minutes=5),
    )


def _runtime_quality(
    profile_group: BotRuntimePredictionQualityGroup | None = None,
    *,
    by_profile_source: tuple[BotRuntimePredictionQualityGroup, ...] = (),
) -> BotRuntimePredictionQuality:
    params: dict[str, object] = {
        "hours": 24,
        "total": profile_group.total if profile_group is not None else 0,
        "evaluated": profile_group.evaluated if profile_group is not None else 0,
        "pending": profile_group.pending if profile_group is not None else 0,
        "misses": profile_group.misses if profile_group is not None else 0,
        "guardrail_unavailable": profile_group.guardrail_unavailable if profile_group is not None else 0,
        "average_error_minutes": profile_group.average_error_minutes if profile_group is not None else None,
        "p50_abs_error_minutes": profile_group.p50_abs_error_minutes if profile_group is not None else None,
        "latest_sampled_at": profile_group.latest_sampled_at if profile_group is not None else None,
        "latest_evaluated_at": profile_group.latest_evaluated_at if profile_group is not None else None,
        "oldest_pending_sampled_at": profile_group.oldest_pending_sampled_at if profile_group is not None else None,
        "by_profile": (profile_group,) if profile_group is not None else (),
        "by_source": (),
        "by_event_kind": (),
    }
    if "by_profile_source" in BotRuntimePredictionQuality.__dataclass_fields__:
        params["by_profile_source"] = by_profile_source
    return BotRuntimePredictionQuality(**params)  # type: ignore[arg-type]


def _runtime_calibration(
    profile_group: BotRuntimeCalibrationGroup | None = None,
    *,
    by_profile_source: tuple[BotRuntimeCalibrationGroup, ...] = (),
) -> BotRuntimeCalibration:
    return BotRuntimeCalibration(
        hours=24,
        total=profile_group.total if profile_group is not None else 0,
        evaluated=profile_group.evaluated if profile_group is not None else 0,
        misses=profile_group.misses if profile_group is not None else 0,
        p80_early_minutes=profile_group.p80_early_minutes if profile_group is not None else None,
        p50_extra_wait_minutes=profile_group.p50_extra_wait_minutes if profile_group is not None else None,
        suggested_buffer_minutes=profile_group.suggested_buffer_minutes if profile_group is not None else 0,
        status=profile_group.status if profile_group is not None else "insufficient",
        action=profile_group.action if profile_group is not None else "collect more evaluated bot replies",
        by_profile=(profile_group,) if profile_group is not None else (),
        by_source=(),
        by_profile_source=by_profile_source,
    )


def _insert_stats_runtime_prediction(
    connection: sqlite3.Connection,
    *,
    profile_key: str,
    sampled_at: datetime,
    source: str,
    source_method: str,
    predicted_minutes: int,
) -> int:
    history_scope = "profile_time" if source == SOURCE_HISTORY_HEADWAY else ""
    raw_json = json.dumps(
        {
            "runtime_source": RUNTIME_SOURCE_WEB_APP,
            "event_kind": BOT_EVENT_USER_REPLY,
            "selected_departure_source": "yandex_history" if source == SOURCE_HISTORY_HEADWAY else "yandex",
            "urgency": "go_now",
            "leave_in_minutes": max(0, predicted_minutes - 12),
            "target_wait_minutes": 6 if source == SOURCE_HISTORY_HEADWAY else 3,
            "history_scope": history_scope,
            "history_report_window_key": _stats_runtime_report_window_key(profile_key) if history_scope else "",
            "history_sample_count": 24 if history_scope else None,
            "history_bucket_minutes": 30 if history_scope else None,
            "history_percentile": 80 if history_scope else None,
            "yandex_status": "ok",
            "eta_factors": [],
            "warning": "",
        },
        ensure_ascii=False,
    )
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
            None,
            profile_key,
            sampled_at.isoformat(),
            _stats_runtime_report_window_key(profile_key),
            source,
            source_method,
            predicted_minutes,
            (sampled_at + timedelta(minutes=predicted_minutes)).isoformat(),
            "low" if source == SOURCE_HISTORY_HEADWAY else "medium",
            "",
            "",
            "none",
            "not_collected",
            None,
            RUNTIME_SOURCE_WEB_APP,
            raw_json,
        ),
    )
    return int(cursor.lastrowid)


def _insert_stats_runtime_evaluation(
    connection: sqlite3.Connection,
    *,
    prediction_id: int,
    profile_key: str,
    sampled_at: datetime,
    source: str,
    predicted_minutes: int,
    error_minutes: int,
) -> None:
    actual_minutes = predicted_minutes + error_minutes
    arrival = connection.execute(
        """
        INSERT INTO arrival_events(
            yandex_snapshot_id, profile_key, vehicle_id, thread_id, stop_id,
            arrived_at, source, confidence, lat, lng, raw_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            None,
            profile_key,
            "",
            "",
            _stats_runtime_stop_id(profile_key),
            (sampled_at + timedelta(minutes=actual_minutes)).isoformat(),
            "smoke",
            "high",
            None,
            None,
            "{}",
        ),
    )
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
            int(arrival.lastrowid),
            profile_key,
            sampled_at.isoformat(),
            actual_minutes,
            predicted_minutes,
            error_minutes,
            "10_14",
            source,
            "{}",
        ),
    )


def _stats_runtime_report_window_key(profile_key: str) -> str:
    return "weekday_evening_19_22" if profile_key == EVENING.key else "weekday_morning_09_12"


def _stats_runtime_stop_id(profile_key: str) -> str:
    return EVENING.live_stop_id if profile_key == EVENING.key else MORNING.live_stop_id


def _insert_bot_latency_row(
    connection: sqlite3.Connection,
    *,
    received_at: str,
    forecast_ms: object,
    send_ms: object,
    total_ms: object,
) -> None:
    connection.execute(
        """
        INSERT INTO bot_interaction_events(
            received_at, chat_id_hash, update_type, command, reply_source,
            yandex_source_method, forecast_ms, render_ms, send_ms, total_ms,
            status, error
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            received_at,
            "local-smoke-chat",
            "message",
            "/stats",
            "yandex_live",
            "vehicle_prediction",
            forecast_ms,
            1,
            send_ms,
            total_ms,
            "ok",
            "",
        ),
    )


def _consensus(
    *,
    selected_source: object | None = EtaSource.YANDEX,
    arrival_minutes: object | None = 8,
    confidence: object = EtaConfidence.HIGH,
    target_wait_minutes: object = 2,
    spread_minutes: object | None = None,
    warning: object = "",
    estimates: object = (),
    factors: object = (),
) -> EtaConsensus:
    return EtaConsensus(
        selected_source=selected_source,
        arrival_minutes=arrival_minutes,
        confidence=confidence,
        target_wait_minutes=target_wait_minutes,
        spread_minutes=spread_minutes,
        warning=warning,
        estimates=estimates,
        factors=factors,
    )


def _assert_rejects(factory: Callable[[], object], expected: str) -> None:
    try:
        factory()
    except ValueError as error:
        _assert_contains(str(error), expected)
    else:
        raise AssertionError(f"expected {expected!r} validation error")


def _assert_contains(text: str, expected: str) -> None:
    if expected not in text:
        raise AssertionError(f"expected {expected!r} in {text!r}")


def _assert_not_contains(text: str, unexpected: str) -> None:
    if unexpected in text:
        raise AssertionError(f"did not expect {unexpected!r} in {text!r}")


def _assert_equal(actual: object, expected: object) -> None:
    if actual != expected:
        raise AssertionError(f"expected {expected!r}, got {actual!r}")


if __name__ == "__main__":
    main()
