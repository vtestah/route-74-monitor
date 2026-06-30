from __future__ import annotations

import sqlite3
import warnings
from contextlib import redirect_stderr
from datetime import datetime, timedelta
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

warnings.filterwarnings("ignore", message="Using `httpx` with `starlette.testclient` is deprecated.*")

from fastapi.testclient import TestClient

import route74.dashboard.app as dashboard_app
import route74.dashboard.data as dashboard_data
from route74.dashboard import create_app
from route74.dashboard.config import parse_dashboard_config
from route74.dashboard.data import _dashboard_evidence_payload, _profile_forecast_backtest
from route74.domain.prediction_sources import SOURCE_HISTORY_HEADWAY, SOURCE_TARGET_STOP_LIVE
from route74.domain.profiles import EVENING, MORNING
from route74.domain.runtime_sources import (
    BOT_EVENT_USER_REPLY,
    BOT_EVENT_WATCH_EARLY,
    BOT_EVENT_WATCH_FINAL,
    RUNTIME_SOURCE_WEB_APP,
)
from route74.domain.yandex_history import DEFAULT_HISTORY_PERCENTILE
from route74.models import NOVOSIBIRSK_TZ, now_local
from route74.reporting_smoke_fixtures import FakeYandexSource, fake_traffic_source
from route74.support_actions import (
    bot_latency_command,
    bot_runtime_command,
    forecast_backtest_command_for_profile,
    forecast_readiness_command_for_profile,
    prediction_calibration_command_for_profile,
    support_report_command_for_profile,
    support_snapshot_command_for_profile,
    watch_state_command_for_path,
)
from route74.sources.yandex.models import YandexLiveForecast, YandexSourceMethod, YandexSourceStatus
from route74.storage import (
    BotInteractionEvent,
    connect,
    connect_readonly,
    count_yandex_snapshots,
    init_db,
    insert_bot_interaction_event,
    insert_collector_run,
    insert_yandex_snapshot,
    update_collector_heartbeat,
)
from route74.storage.bot_latency import BotLatencySummary
from route74.storage.forecast_backtest import ForecastBacktestResult, ForecastBacktestSummary
from route74.storage.models import CountByKey


def main() -> None:
    _assert_forecast_backtest_profile_payload_warns()
    _assert_dashboard_latency_small_sample_is_warning()
    _assert_dashboard_evidence_payload_fallback()
    _assert_dashboard_surfaces_runtime_prediction_change()
    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "route74.sqlite"
        watch_state_path = Path(temp_dir) / "bot-watches.json"
        watch_now = now_local()
        watch_state_path.write_text(
            json.dumps(
                {
                    "101": {
                        "profile_key": MORNING.key,
                        "walk_minutes": 12,
                        "started_at": (watch_now - timedelta(minutes=1)).isoformat(),
                        "next_poll_at": (watch_now + timedelta(minutes=10)).isoformat(),
                        "early_sent": False,
                    }
                }
            ),
            encoding="utf-8",
        )
        _seed(db_path)
        config = parse_dashboard_config(["--db", str(db_path), "--host", "127.0.0.1", "--port", "8075"])
        _assert_equal(config.db_path, db_path)
        _assert_equal(config.port, 8075)
        _assert_public_bind_guard(db_path)
        _assert_dashboard_port_guard(db_path)
        before = _snapshot_count(db_path)
        client = TestClient(create_app(db_path, watch_state_path=watch_state_path))

        health = client.get("/healthz")
        _assert_equal(health.status_code, 200)
        _assert_equal(health.json()["status"], "ok")
        _assert_contains(str(health.json()["version"]), "package_version")

        page = client.get("/")
        _assert_equal(page.status_code, 200)
        _assert_contains(page.text, "Дашборд 74")
        _assert_contains(page.text, "Главный вывод")
        _assert_contains(page.text, "Профили сценариев")
        _assert_contains(page.text, "Риск Яндекса")
        _assert_contains(page.text, "Prediction events")
        _assert_contains(page.text, "Последние prediction events")
        _assert_contains(page.text, "Окно отчёта")
        _assert_contains(page.text, "data-chart-date")
        _assert_contains(page.text, "handleChartPick")
        _assert_contains(page.text, "handleChartPreview")
        _assert_contains(page.text, "Подтверждение")
        _assert_contains(page.text, "evidencePanel")
        _assert_contains(page.text, "evidenceSummary")
        _assert_contains(page.text, "evidenceStatus")
        _assert_contains(page.text, "evidenceFactsHtml")
        _assert_contains(page.text, "renderEvidence")
        _assert_contains(page.text, "trustHeadlineText")
        _assert_contains(page.text, "dashboardHasNoData")
        _assert_contains(page.text, "summaryTrustChips")
        _assert_contains(page.text, "System health:")
        _assert_contains(page.text, "Yandex source quality:")
        _assert_contains(page.text, "History readiness:")
        _assert_contains(page.text, "Runtime quality:")
        _assert_contains(page.text, "canary ещё не запускался")
        _assert_contains(page.text, "botLatencySmallSample")
        _assert_contains(page.text, "мало данных")
        _assert_contains(page.text, "история учится")
        _assert_contains(page.text, "Данных пока нет")
        _assert_contains(page.text, "нет подтверждения")
        _assert_contains(page.text, "operatorProfiles")
        _assert_contains(page.text, "renderOperatorProfiles")
        _assert_contains(page.text, "data-recent-filter")
        _assert_contains(page.text, "data-recent-action")
        _assert_contains(page.text, "filterRecentItems")
        _assert_contains(page.text, 'data-bot-filter="user_reply"')
        _assert_contains(page.text, 'data-bot-filter="watch"')
        _assert_contains(page.text, "botResponseIsUserReply")
        _assert_contains(page.text, "botResponseIsWatch")
        _assert_contains(page.text, "DASHBOARD_PREFS_KEY")
        _assert_contains(page.text, "savePreferences")
        _assert_contains(page.text, "errorRetryBtn")
        _assert_contains(page.text, "Не удалось обновить данные")
        _assert_contains(page.text, "lastErrorContext")
        _assert_contains(page.text, "lastErrorRetryTarget")
        _assert_contains(page.text, "retryLastError")
        _assert_contains(page.text, "dashboardErrorMessage")
        _assert_contains(page.text, "Локальный сервер не ответил при обновлении")
        _assert_contains(page.text, "Повторить график")
        _assert_contains(page.text, "Повторить сводку")
        _assert_contains(page.text, "Сбой при обновлении")
        _assert_contains(page.text, "· сбой")
        _assert_contains(page.text, "resetViewBtn")
        _assert_contains(page.text, "removePreferences")
        _assert_contains(page.text, "setupSectionNav")
        _assert_contains(page.text, "aria-current")
        _assert_contains(page.text, "is-loading")
        _assert_contains(page.text, "aria-busy")
        _assert_contains(page.text, "data-refresh-tone")
        _assert_contains(page.text, "refreshStateText")
        _assert_contains(page.text, "setRefreshState")
        _assert_contains(page.text, "lastErrorAt")
        _assert_contains(page.text, "lastErrorRetryTarget")
        _assert_contains(page.text, "retryLastError")
        _assert_contains(page.text, "showError(error, 'сводки', 'summary')")
        _assert_contains(page.text, "showError(error, 'графика и замеров', 'window')")
        _assert_contains(page.text, "dashboardErrorDetail")
        _assert_contains(page.text, "Повторить график")
        _assert_not_contains(page.text, "$('errorRetryBtn').addEventListener('click', loadAll)")
        _assert_contains(page.text, "grid-template-columns: repeat(3, 36px) minmax(0, 1fr)")
        _assert_contains(page.text, "text-overflow: ellipsis")
        _assert_contains(page.text, ".operator-actions {")
        _assert_contains(page.text, "min-height: 36px")
        _assert_contains(page.text, ".brand { gap: 0; }")
        _assert_contains(page.text, ".brand p { display: none; }")
        _assert_contains(page.text, "width: 30px")
        _assert_contains(page.text, "grid-template-columns: repeat(2, minmax(0, 1fr))")
        _assert_contains(page.text, ".health-node:last-child")
        _assert_contains(page.text, "height: 34px")
        _assert_contains(page.text, ".chart-tools .legend")
        _assert_contains(page.text, "height: 36px")
        _assert_contains(page.text, "свежие")
        _assert_contains(page.text, "shortDuration")
        _assert_contains(page.text, "window-selected-badge")
        _assert_contains(page.text, ".window-metric:first-child")
        _assert_contains(page.text, "aria-pressed")
        _assert_contains(page.text, "revealChartPanel")
        _assert_contains(page.text, "moveChartSelection")
        _assert_contains(page.text, "focusChartPoint")
        _assert_contains(page.text, 'focusable="true"')
        _assert_contains(page.text, "30-минутные слоты готовности")
        _assert_contains(page.text, "30-мин слоты")
        _assert_contains(page.text, "Координаты без ETA")
        _assert_contains(page.text, "recentOverviewHtml")
        _assert_contains(page.text, "recentEmptyFilterHtml")
        _assert_contains(page.text, "handleRecentAction")
        _assert_contains(page.text, "Сводка последних замеров")
        _assert_contains(page.text, "data-copy-state")
        _assert_contains(page.text, "setCopyButtonState")
        _assert_contains(page.text, "resetCopyButton")
        _assert_contains(page.text, "filter-count")
        _assert_contains(page.text, "updateRecentFilterCounts")
        _assert_contains(page.text, "recentFilterCounts")
        _assert_contains(page.text, "chartPrevDayBtn")
        _assert_contains(page.text, "shiftChartSelection")
        _assert_contains(page.text, "syncChartStepButtons")
        _assert_contains(page.text, "background: rgba(255, 255, 255, .55)")
        _assert_contains(page.text, "grid-template-columns: repeat(2, 42px)")
        _assert_contains(page.text, "chartSelectionBadge")
        _assert_contains(page.text, "setChartSelectionBadge")
        _assert_contains(page.text, "quick-nav-count")
        _assert_contains(page.text, "navWindowsBadge")
        _assert_contains(page.text, "navProfilesBadge")
        _assert_contains(page.text, "navBotBadge")
        _assert_contains(page.text, "setQuickNavBadge")
        _assert_contains(page.text, "Профили сценариев")
        _assert_contains(page.text, "operatorProfiles")
        _assert_contains(page.text, "renderOperatorProfiles")
        _assert_contains(page.text, "operatorProfileCard")
        _assert_contains(page.text, "profileGuardrailValue")
        _assert_contains(page.text, "profileSupportSnapshotHtml")
        _assert_contains(page.text, "profileSupportSnapshotItemHtml")
        _assert_contains(page.text, "data-profile-support-snapshot")
        _assert_contains(page.text, "profile-overview")
        _assert_contains(page.text, "profile-details")
        _assert_contains(page.text, "profile-details-body")
        _assert_contains(page.text, "Диагностика и метрики")
        _assert_contains(page.text, "Support snapshot")
        _assert_contains(page.text, "Preview маршрута")
        _assert_contains(page.text, "data-profile-preview")
        _assert_contains(page.text, "profile-snapshot-meta")
        _assert_contains(page.text, "profile-snapshot-reason")
        _assert_contains(page.text, "profile-snapshot-change")
        _assert_contains(page.text, "profile-snapshot-count")
        _assert_contains(page.text, "Скопировать сводку")
        _assert_contains(page.text, "profileSupportDiagnosticCommands")
        _assert_contains(page.text, "profileLatencyDetail")
        _assert_contains(page.text, "profileLatencyTimingText")
        _assert_contains(page.text, "topNoEtaReasonText")
        _assert_contains(page.text, "topErrorCategoryText")
        _assert_contains(page.text, "profileGuardrailDetail")
        _assert_contains(page.text, "ETA-защита")
        _assert_contains(page.text, "profileForecastMissingBuckets")
        _assert_contains(page.text, "coverage_command")
        _assert_contains(page.text, "bot_runtime_guardrail_unavailable")
        _assert_contains(page.text, "botLatencyFreshness")
        _assert_contains(page.text, "data-profile-card")
        _assert_contains(page.text, "botResponses")
        _assert_contains(page.text, "renderBotResponses")
        _assert_contains(page.text, "bot-response-list")
        _assert_contains(page.text, "bot-response-item")
        _assert_contains(page.text, "bot-response-rail")
        _assert_contains(page.text, "bot-response-card")
        _assert_contains(page.text, "bot-response-outcome")
        _assert_contains(page.text, "bot-response-marker")
        _assert_contains(page.text, "bot-response-${state}")
        _assert_contains(page.text, "botResponseStateText")
        _assert_contains(page.text, "botResponseDiagnosticText")
        _assert_contains(page.text, "botResponseChipsHtml")
        _assert_contains(page.text, "botChangeFactHtml")
        _assert_contains(page.text, "<span>С прошлого ответа</span>")
        _assert_contains(page.text, "Ждёт факт уже")
        _assert_contains(page.text, "Промах: 74 пришёл раньше")
        _assert_contains(page.text, "bot-quality-facts")
        _assert_contains(page.text, "botQualityBreakdown")
        _assert_contains(page.text, 'data-bot-filter="misses"')
        _assert_contains(page.text, 'data-bot-filter="watch_early"')
        _assert_contains(page.text, 'data-bot-filter="watch_final"')
        _assert_contains(page.text, "BOT_RESPONSE_FILTER_OPTIONS")
        _assert_contains(page.text, "activateBotResponseFilter")
        _assert_contains(page.text, "updateBotResponseFilterCounts")
        _assert_contains(page.text, "filterBotResponseItems")
        _assert_contains(page.text, "botResponseCommandText")
        _assert_contains(page.text, "data-bot-runtime-command")
        _assert_contains(page.text, "botResponseEmptyFilterHtml")
        _assert_contains(page.text, "Показать все события")
        _assert_contains(page.text, "Служебные детали")
        _assert_contains(page.text, "botPendingAgeText")
        _assert_contains(page.text, "botResponseIsWatchEarly")
        _assert_contains(page.text, "botResponseIsWatchFinal")
        _assert_contains(page.text, "Не проверено")
        _assert_contains(page.text, "operatorActions")
        _assert_contains(page.text, "actionDock")
        _assert_contains(page.text, "renderActionDock")
        _assert_contains(page.text, "dockCommand")
        _assert_contains(page.text, "dockDiagnostics")
        _assert_contains(page.text, "dockActions")
        _assert_contains(page.text, "--action-dock-top")
        _assert_contains(page.text, "action-dock")
        _assert_contains(page.text, "position: sticky")
        _assert_contains(page.text, "data-operator-action")
        _assert_contains(page.text, "operatorActionsHtml")
        _assert_contains(page.text, "handleOperatorAction")
        _assert_contains(page.text, "operatorCommand")
        _assert_contains(page.text, "operatorDiagnostics")
        _assert_contains(page.text, "copyOperatorCommand")
        _assert_contains(page.text, "$('operatorDiagnostics').addEventListener('click', handleOperatorAction)")
        _assert_contains(page.text, 'id="actionDock"')
        _assert_contains(page.text, "$('actionDock').addEventListener('click', handleOperatorAction)")
        _assert_contains(page.text, "renderActionDock(primaryTone")
        _assert_contains(page.text, "applyDockStatus")
        _assert_contains(page.text, "dockStatus")
        _assert_contains(page.text, "dockConclusion")
        _assert_contains(page.text, 'data-operator-action="copy-command"')
        _assert_contains(page.text, "supportReportCommand")
        _assert_contains(page.text, "supportReportWindowKey")
        _assert_contains(page.text, "supportTriage")
        _assert_contains(page.text, "triagePrimaryItem")
        _assert_contains(page.text, "triageActionKind")
        _assert_contains(page.text, "history_backtest")
        _assert_contains(page.text, "history_backtest', 'integrity_gap'")
        _assert_contains(page.text, "Расхождение витрин")
        _assert_contains(page.text, "Открыть выбранное окно и сверить витрины прогноза и отчётного окна.")
        _assert_contains(page.text, "isWatchStateTriageItem")
        _assert_contains(page.text, "watchIssue.command")
        _assert_contains(page.text, "mainConclusionText")
        _assert_contains(page.text, "Диагностика:")
        _assert_contains(page.text, "Следующий шаг")
        _assert_contains(page.text, "route74 support-report --window")
        _assert_contains(page.text, "predictionEvaluateCommand")
        _assert_contains(page.text, "route74 prediction-evaluate --window")
        _assert_contains(page.text, "kind: 'pending'")
        _assert_contains(page.text, "window_key: profileWindows[profileKey]")
        _assert_contains(page.text, "botRuntimeIssue")
        _assert_contains(page.text, "topCalibrationGroup")
        _assert_contains(page.text, "botCalibrationProfileKey")
        _assert_contains(page.text, "botCalibrationGroupsText")
        _assert_contains(page.text, "profileLatencyDetail")
        _assert_contains(page.text, "profileLatencyCommand")
        _assert_contains(page.text, "profile.forecast_readiness")
        _assert_contains(page.text, "profile.forecast_backtest")
        _assert_contains(page.text, "profileForecastReadinessDetail")
        _assert_contains(page.text, "profileForecastHealthDetail")
        _assert_contains(page.text, "profileForecastReadinessCommandHtml")
        _assert_contains(page.text, "profileForecastBacktestDetail")
        _assert_contains(page.text, "profileForecastBacktestCommandHtml")
        _assert_contains(page.text, "return status === 'warning'")
        _assert_contains(page.text, "profileCalibrationDetail")
        _assert_contains(page.text, "profile.source_calibration")
        _assert_contains(page.text, "profileSourceCalibrationDetail")
        _assert_contains(page.text, "profileSourceCalibrationCommandHtml")
        _assert_contains(page.text, "profileSourceCalibrationSource")
        _assert_contains(page.text, "profile.support_snapshot")
        _assert_contains(page.text, "profileSupportSnapshotHtml")
        _assert_contains(page.text, "renderCheckPolicy")
        _assert_contains(page.text, "autoCheckHtml")
        _assert_contains(page.text, "manualDiagnosticHtml")
        _assert_contains(page.text, "check-policy-grid")
        _assert_contains(page.text, "Safe auto checks")
        _assert_contains(page.text, "Ручная диагностика")
        _assert_contains(page.text, "manualDiagnosticsPanel")
        _assert_contains(page.text, "dashboard-details")
        _assert_contains(page.text, "Проверки dashboard")
        _assert_contains(page.text, "data-profile-support-snapshot")
        _assert_contains(page.text, 'data-operator-action="copy-text"')
        _assert_contains(page.text, "copyOperatorText")
        _assert_contains(page.text, "Support snapshot")
        _assert_contains(page.text, "data-profile-source-calibration-command")
        _assert_contains(page.text, "Калибровка source")
        _assert_contains(page.text, "Источник риска")
        _assert_contains(page.text, "profile.latency")
        _assert_contains(page.text, "profile-command-group")
        _assert_contains(page.text, "profile-command-label")
        _assert_contains(page.text, "data-profile-readiness-command")
        _assert_contains(page.text, "data-profile-backtest-command")
        _assert_contains(page.text, "Готовность истории")
        _assert_contains(page.text, "Качество истории")
        _assert_contains(page.text, "Открыть forecast-backtest по профилю")
        _assert_contains(page.text, "data-profile-latency-command")
        _assert_contains(page.text, "Задержки runtime")
        _assert_contains(page.text, "Направления")
        _assert_contains(page.text, "Источник риска")
        _assert_contains(page.text, "Prediction events требуют проверки")
        _assert_contains(page.text, "Watch state")
        _assert_contains(page.text, "watchStateIssue")
        _assert_contains(page.text, "operatorButton('bot'")
        _assert_contains(page.text, "scrollToSection('bot-panel')")
        _assert_contains(page.text, "scrollToSection")
        _assert_contains(page.text, "icon-button")
        _assert_contains(page.text, 'aria-label="Обновить данные"')
        _assert_contains(page.text, "setIconButton")
        _assert_contains(page.text, "scroll-snap-type: x proximity")
        _assert_contains(page.text, "scroll-snap-align: start")
        _assert_contains(page.text, "scroll-padding-inline: 14px")
        _assert_contains(page.text, ".quick-nav::-webkit-scrollbar")
        _assert_contains(page.text, "sample-status-row")
        _assert_contains(page.text, "sampleVerdict")
        _assert_contains(page.text, "ETA получен")
        _assert_contains(page.text, "ETA не получен")
        _assert_contains(page.text, "без ETA")
        _assert_contains(page.text, "coordinates_only")
        _assert_contains(page.text, "координаты есть, ETA нет")
        _assert_contains(page.text, "sampleReasonFactHtml")
        _assert_contains(page.text, "sourceReasonText")
        _assert_contains(page.text, "isSevereTrafficStatus")
        _assert_contains(page.text, "fallback не нужен")
        _assert_contains(page.text, "сигнал есть, но выборка мала")
        _assert_contains(page.text, "чаще всего")
        _assert_contains(page.text, "botExplanationText")
        _assert_contains(page.text, "<span>Почему</span>")
        _assert_contains(page.text, "Статус пробок")
        _assert_contains(page.text, ".recent-overview-item:first-child")

        favicon = client.get("/favicon.ico")
        _assert_equal(favicon.status_code, 200)
        _assert_contains(favicon.headers["content-type"], "image/svg+xml")

        summary = client.get("/api/summary")
        _assert_equal(summary.status_code, 200)
        payload = summary.json()
        _assert_contains(str(payload["build"]), "package_version")
        _assert_equal(payload["db"]["healthy"], True)
        evidence = payload["evidence"]
        _assert_equal(evidence["status"], "available")
        _assert_equal(evidence["route"], "74")
        _assert_equal(evidence["profile_key"], MORNING.key)
        _assert_equal(evidence["profile_title"], MORNING.title)
        _assert_equal(evidence["window_key"], "weekday_morning_09_12")
        _assert_equal(evidence["source_method"], "vehicle_prediction")
        _assert_equal(evidence["selected_departure_source"], "yandex")
        _assert_equal(evidence["confidence"], "medium")
        _assert_contains(evidence["summary"], "Маршрут 74")
        _assert_contains(evidence["summary"], MORNING.title)
        _assert_contains(evidence["summary"], "Будни утром 09-12")
        _assert_equal(evidence["sampled_at"] is not None, True)
        _assert_equal(evidence["freshness_seconds"] is not None, True)
        support_report = payload["support_report"]
        _assert_equal(support_report["default_window_key"], "weekday_morning_09_12")
        _assert_equal(support_report["triage"]["status"], "critical")
        _assert_equal(
            support_report["triage"]["primary_action"],
            "route74 runtime-latency --hours 24 --profile morning --event-kind user_reply",
        )
        _assert_equal(
            support_report["triage_by_window"]["weekday_evening_19_22"]["items"][-1]["key"],
            "bot_runtime_profile",
        )
        _assert_contains(str(support_report["triage"]), "top_error=followup_send_error")
        _assert_not_contains(str(support_report["triage"]), "123456:")
        _assert_equal(
            support_report["commands"]["weekday_evening_19_22"],
            "route74 support-report --window weekday_evening_19_22",
        )
        _assert_equal(
            support_report["triage_by_window"]["weekday_morning_09_12"]["primary_action"],
            "route74 runtime-latency --hours 24 --profile morning --event-kind user_reply",
        )
        _assert_equal(
            payload["prediction_evaluate"]["commands"]["weekday_morning_09_12"],
            "route74 prediction-evaluate --window weekday_morning_09_12",
        )
        check_policy = payload["check_policy"]
        _assert_contains(check_policy["summary"], "Автопроверки")
        _assert_contains(check_policy["mode_note"], "policy-слой dashboard")
        _assert_equal(len(check_policy["auto_checks"]), 5)
        _assert_equal(check_policy["auto_checks"][0]["label"], "startup/health")
        _assert_equal(check_policy["auto_checks"][0]["status"], "ok")
        _assert_equal(check_policy["manual_diagnostics"][0]["label"], "Быстрый support snapshot")
        _assert_equal(
            check_policy["manual_diagnostics"][1]["command"],
            "route74 support-report --window weekday_morning_09_12",
        )
        bot_runtime_commands = payload["bot_predictions"]["commands"]
        _assert_equal(bot_runtime_commands["all"], bot_runtime_command(hours=24, limit=8))
        _assert_equal(
            bot_runtime_commands[BOT_EVENT_USER_REPLY],
            bot_runtime_command(hours=24, limit=8, event_kind=BOT_EVENT_USER_REPLY),
        )
        _assert_equal(
            bot_runtime_commands[BOT_EVENT_WATCH_EARLY],
            bot_runtime_command(hours=24, limit=8, event_kind=BOT_EVENT_WATCH_EARLY),
        )
        _assert_equal(
            bot_runtime_commands[BOT_EVENT_WATCH_FINAL],
            bot_runtime_command(hours=24, limit=8, event_kind=BOT_EVENT_WATCH_FINAL),
        )
        _assert_equal(len(payload["forecast"]["windows"]), 2)
        _assert_equal(payload["forecast"]["windows"][0]["api_risk_samples"], 0)
        _assert_equal(payload["forecast"]["windows"][0]["api_risk_reasons"], [])
        _assert_equal(payload["forecast"]["windows"][0]["forecast_without_report_samples"], 0)
        _assert_equal(payload["forecast"]["windows"][0]["report_without_forecast_samples"], 0)
        _assert_equal(payload["forecast"]["windows"][0]["integrity_gap_samples"], 0)
        operator_profiles = payload["operator_profiles"]
        _assert_equal(len(operator_profiles), 2)
        morning_profile = _profile_by_key(operator_profiles, MORNING.key)
        evening_profile = _profile_by_key(operator_profiles, EVENING.key)
        _assert_equal(morning_profile["profile_title"], MORNING.title)
        _assert_equal(morning_profile["window_key"], "weekday_morning_09_12")
        _assert_equal(
            morning_profile["status"],
            support_report["triage_by_window"]["weekday_morning_09_12"]["status"],
        )
        _assert_equal(
            morning_profile["primary_action"],
            support_report["triage_by_window"]["weekday_morning_09_12"]["primary_action"],
        )
        _assert_equal(morning_profile["primary_issue"]["action"], morning_profile["primary_action"])
        morning_snapshot = morning_profile["support_snapshot"]
        _assert_equal(morning_snapshot["profile_key"], MORNING.key)
        _assert_equal(morning_snapshot["window_key"], "weekday_morning_09_12")
        _assert_equal(morning_snapshot["hours"], 24)
        _assert_equal(morning_snapshot["status"], morning_profile["status"])
        _assert_equal(morning_snapshot["primary_action"], morning_profile["primary_action"])
        _assert_equal(morning_snapshot["snapshot_command"], support_snapshot_command_for_profile(MORNING.key))
        _assert_equal(morning_snapshot["report_command"], support_report_command_for_profile(MORNING.key))
        _assert_equal(morning_snapshot["diagnostic_commands"][0]["label"], "Следующая диагностика")
        _assert_equal(
            morning_snapshot["diagnostic_commands"][0]["command"],
            morning_profile["primary_action"],
        )
        _assert_equal(morning_snapshot["diagnostic_commands"][1]["label"], "Быстрый support snapshot")
        _assert_equal(morning_snapshot["diagnostic_commands"][2]["label"], "Полный support report")
        _assert_equal(morning_snapshot["primary_issue"]["action"], morning_profile["primary_action"])
        _assert_equal(morning_snapshot["actionable_count"], morning_profile["issue_count"])
        _assert_equal(morning_snapshot["items"][0]["action"], morning_profile["primary_action"])
        _assert_equal(
            morning_snapshot["hidden_item_count"],
            max(0, morning_snapshot["item_count"] - len(morning_snapshot["items"])),
        )
        _assert_contains(morning_snapshot["text"], "🧰 Разбор 74")
        _assert_contains(
            morning_snapshot["text"],
            "🎯 Следующий шаг: route74 runtime-latency --hours 24 --profile morning --event-kind user_reply",
        )
        _assert_contains(morning_snapshot["text"], "Быстрый снимок: route74 support-snapshot --profile morning")
        _assert_contains(morning_snapshot["text"], "Полный отчёт: route74 support-report --profile morning")
        _assert_equal(morning_profile["forecast"]["status"], payload["forecast"]["windows"][0]["status"])
        _assert_equal(
            morning_profile["forecast"]["forecast_without_report_samples"],
            payload["forecast"]["windows"][0]["forecast_without_report_samples"],
        )
        _assert_equal(
            morning_profile["forecast"]["report_without_forecast_samples"],
            payload["forecast"]["windows"][0]["report_without_forecast_samples"],
        )
        _assert_equal(
            morning_profile["forecast"]["integrity_gap_samples"],
            payload["forecast"]["windows"][0]["integrity_gap_samples"],
        )
        _assert_equal(morning_profile["forecast_readiness"]["status"], "not_ready")
        _assert_equal(
            morning_profile["forecast_readiness"]["command"],
            forecast_readiness_command_for_profile(MORNING.key),
        )
        _assert_equal(morning_profile["forecast_readiness"]["selected_bucket_minutes"], 30)
        _assert_equal(morning_profile["forecast_readiness"]["selected_sample_count"], 2)
        _assert_equal(morning_profile["forecast_readiness"]["selected_distinct_days"], 1)
        _assert_equal(morning_profile["forecast_readiness"]["min_samples"], 20)
        _assert_equal(morning_profile["forecast_readiness"]["min_distinct_days"], 3)
        _assert_equal(morning_profile["forecast_backtest"]["status"], "insufficient")
        _assert_equal(
            morning_profile["forecast_backtest"]["command"],
            forecast_backtest_command_for_profile(MORNING.key),
        )
        _assert_equal(morning_profile["forecast_backtest"]["percentile"], DEFAULT_HISTORY_PERCENTILE)
        _assert_equal(morning_profile["runtime"]["status"], "warning")
        _assert_equal(morning_profile["runtime"]["total"], 2)
        _assert_equal(morning_profile["runtime"]["pending"], 1)
        _assert_equal(morning_profile["runtime"]["miss_rate_percent"], 100)
        _assert_equal(morning_profile["calibration"]["status"], "insufficient")
        _assert_equal(morning_profile["latency"]["profile_key"], MORNING.key)
        _assert_equal(morning_profile["latency"]["status"], "critical")
        _assert_equal(
            morning_profile["latency"]["command"],
            bot_latency_command(profile_key=MORNING.key, event_kind=BOT_EVENT_USER_REPLY),
        )
        _assert_equal(morning_profile["latency"]["events"], 3)
        _assert_equal(morning_profile["latency"]["errors"], 2)
        _assert_equal(morning_profile["latency"]["no_eta"], 0)
        _assert_equal(morning_profile["latency"]["p95_total_ms"], 402)
        _assert_equal(morning_profile["latency"]["p95_send_ms"], 100)
        _assert_equal(morning_profile["latency"]["p95_followup_ms"], 2)
        if morning_profile["latency"]["latest_received_at"] is None:
            raise AssertionError("expected morning latency freshness timestamp")
        _assert_contains(str(morning_profile["latency"]["error_reasons"]), "<redacted>")
        _assert_contains(str(morning_profile["latency"]["error_reasons"]), "local followup failure")
        _assert_contains(str(morning_profile["latency"]["error_reasons"]), "send failed")
        _assert_equal(morning_profile["latency"]["error_reasons"][0]["count"], 1)
        _assert_equal(morning_profile["latency"]["error_categories"][0]["key"], "followup_send_error")
        _assert_equal(morning_profile["latency"]["error_categories"][0]["label"], "quick-start подсказка не ушла")
        _assert_equal(morning_profile["latency"]["top_error_category"]["label"], "quick-start подсказка не ушла")
        _assert_equal(evening_profile["window_key"], "weekday_evening_19_22")
        _assert_equal(evening_profile["runtime"]["status"], "missing")
        _assert_equal(evening_profile["calibration"]["status"], "missing")
        _assert_equal(evening_profile["latency"]["profile_key"], EVENING.key)
        _assert_equal(evening_profile["latency"]["status"], "warning")
        _assert_equal(
            evening_profile["latency"]["command"],
            bot_latency_command(profile_key=EVENING.key, event_kind=BOT_EVENT_USER_REPLY),
        )
        _assert_equal(evening_profile["latency"]["events"], 1)
        _assert_equal(evening_profile["latency"]["errors"], 0)
        _assert_equal(evening_profile["latency"]["no_eta"], 1)
        _assert_equal(evening_profile["latency"]["no_eta_reasons"][0]["key"], "yandex_no_target+history_unavailable")
        _assert_equal(evening_profile["latency"]["top_no_eta_reason"]["count"], 1)
        _assert_equal(
            evening_profile["latency"]["top_no_eta_reason"]["label"],
            "Яндекс: нет нашей остановки; история недоступна",
        )
        _assert_equal(evening_profile["latency"]["p95_total_ms"], 171)
        _assert_equal(evening_profile["latency"]["p95_send_ms"], 50)
        _assert_equal(evening_profile["latency"]["p95_followup_ms"], 1)
        if evening_profile["latency"]["latest_received_at"] is None:
            raise AssertionError("expected evening latency freshness timestamp")
        _assert_not_contains(str(operator_profiles), "123456:")
        _assert_equal(payload["bot_latency"]["events"], 4)
        _assert_equal(payload["bot_latency"]["profile_key"], None)
        _assert_equal(payload["bot_latency"]["errors"], 2)
        _assert_equal(payload["bot_latency"]["no_eta"], 1)
        _assert_equal(payload["bot_latency"]["no_eta_rate_percent"], 25)
        _assert_equal(payload["bot_latency"]["p95_send_ms"], 100)
        _assert_equal(payload["bot_latency"]["p95_followup_ms"], 2)
        _assert_equal(payload["bot_latency"]["top_no_eta_reason"]["count"], 1)
        _assert_equal(
            payload["bot_latency"]["top_no_eta_reason"]["label"],
            "Яндекс: нет нашей остановки; история недоступна",
        )
        if payload["bot_latency"]["latest_received_at"] is None:
            raise AssertionError("expected bot latency freshness timestamp")
        _assert_contains(str(payload["bot_latency"]["error_reasons"]), "<redacted>")
        _assert_equal(payload["bot_latency"]["top_error_category"]["label"], "quick-start подсказка не ушла")
        watch_state = payload["watch_state"]
        _assert_equal(watch_state["path"], str(watch_state_path))
        _assert_equal(watch_state["command"], watch_state_command_for_path(watch_state_path))
        _assert_equal(watch_state["status"], "ok")
        _assert_equal(watch_state["active_count"], 1)
        _assert_watch_expiry_minutes(watch_state["expires_in_minutes"])
        if watch_state["expires_at"] is None:
            raise AssertionError("expected watch expiry timestamp")
        _assert_equal(watch_state["profiles"][0]["profile_key"], MORNING.key)
        _assert_watch_expiry_minutes(watch_state["profiles"][0]["expires_in_minutes"])
        if watch_state["profiles"][0]["expires_at"] is None:
            raise AssertionError("expected profile watch expiry timestamp")
        _assert_equal(morning_profile["watch"]["active_count"], 1)
        _assert_watch_expiry_minutes(morning_profile["watch"]["expires_in_minutes"])
        if morning_profile["watch"]["expires_at"] is None:
            raise AssertionError("expected operator profile watch expiry timestamp")
        _assert_equal(evening_profile["watch"]["active_count"], 0)
        _assert_equal(evening_profile["watch"]["expires_at"], None)
        _assert_equal(evening_profile["watch"]["expires_in_minutes"], None)
        _assert_watch_state_hides_chat_ids(watch_state)
        snapshot_response = client.get("/api/support-snapshot/morning")
        _assert_equal(snapshot_response.status_code, 200)
        api_snapshot = snapshot_response.json()
        _assert_equal(_stable_snapshot_payload(api_snapshot), _stable_snapshot_payload(morning_snapshot))
        _assert_contains(api_snapshot["text"], "🧰 Разбор 74")
        _assert_contains(api_snapshot["text"], "🔎 Почему: bot_latency_errors")
        with patch.object(dashboard_data, "build_dashboard_summary", side_effect=AssertionError("full summary")):
            direct_snapshot = dashboard_data.build_dashboard_support_snapshot(
                db_path,
                MORNING.key,
                watch_state_path=watch_state_path,
            )
        _assert_equal(_stable_snapshot_payload(direct_snapshot), _stable_snapshot_payload(morning_snapshot))
        missing_snapshot_response = client.get("/api/support-snapshot/night")
        _assert_equal(missing_snapshot_response.status_code, 404)
        _assert_contains(missing_snapshot_response.json()["detail"], "Неизвестный профиль")

        missing_watch_state_path = Path(temp_dir) / "missing-bot-watches.json"
        missing_client = TestClient(create_app(db_path, watch_state_path=missing_watch_state_path))
        missing_summary = missing_client.get("/api/summary")
        _assert_equal(missing_summary.status_code, 200)
        missing_watch_state = missing_summary.json()["watch_state"]
        _assert_equal(missing_watch_state["path"], str(missing_watch_state_path))
        _assert_equal(missing_watch_state["command"], watch_state_command_for_path(missing_watch_state_path))
        _assert_equal(missing_watch_state["status"], "ok")
        _assert_equal(missing_watch_state["file_status"], "missing")
        _assert_equal(missing_watch_state["active_count"], 0)
        _assert_equal(missing_watch_state["due_count"], 0)
        _assert_equal(missing_watch_state["overdue_count"], 0)
        _assert_equal(missing_watch_state["expires_in_minutes"], None)
        _assert_equal(missing_watch_state["expires_at"], None)
        _assert_equal(missing_watch_state["profiles"], [])

        broken_watch_state_path = Path(temp_dir) / "broken-bot-watches.json"
        broken_watch_state_path.write_text("{not-json", encoding="utf-8")
        broken_client = TestClient(create_app(db_path, watch_state_path=broken_watch_state_path))
        broken_summary = broken_client.get("/api/summary")
        _assert_equal(broken_summary.status_code, 200)
        broken_payload = broken_summary.json()
        broken_watch_state = broken_payload["watch_state"]
        _assert_equal(broken_watch_state["path"], str(broken_watch_state_path))
        _assert_equal(broken_watch_state["command"], watch_state_command_for_path(broken_watch_state_path))
        _assert_equal(broken_watch_state["status"], "critical")
        _assert_equal(broken_watch_state["file_status"], "unreadable")
        _assert_equal(broken_payload["support_report"]["triage"]["status"], "critical")
        _assert_equal(
            broken_payload["support_report"]["triage"]["primary_action"],
            watch_state_command_for_path(broken_watch_state_path),
        )
        broken_watch_item = _item_by_key(
            broken_payload["support_report"]["triage"]["items"],
            "watch_state_file",
        )
        _assert_equal(broken_watch_item["action"], watch_state_command_for_path(broken_watch_state_path))

        errored_watch_state_path = Path(temp_dir) / "errored-bot-watches.json"
        errored_watch_state_path.write_text(
            json.dumps(
                {
                    "101": {
                        "profile_key": MORNING.key,
                        "walk_minutes": 12,
                        "started_at": (watch_now - timedelta(minutes=1)).isoformat(),
                        "next_poll_at": (watch_now + timedelta(minutes=10)).isoformat(),
                        "early_sent": False,
                        "error_count": 2,
                        "last_error_type": "RuntimeError",
                        "last_error_at": watch_now.isoformat(),
                    }
                }
            ),
            encoding="utf-8",
        )
        errored_client = TestClient(create_app(db_path, watch_state_path=errored_watch_state_path))
        errored_summary = errored_client.get("/api/summary")
        _assert_equal(errored_summary.status_code, 200)
        errored_payload = errored_summary.json()
        errored_watch_state = errored_payload["watch_state"]
        _assert_equal(errored_watch_state["path"], str(errored_watch_state_path))
        _assert_equal(errored_watch_state["command"], watch_state_command_for_path(errored_watch_state_path))
        _assert_equal(errored_watch_state["status"], "warning")
        _assert_equal(errored_watch_state["runtime_error_count"], 2)
        _assert_equal(errored_watch_state["runtime_error_records"], 1)
        _assert_equal(errored_watch_state["latest_error_at"], watch_now.isoformat())
        _assert_equal(errored_watch_state["runtime_error_types"], ["RuntimeError"])
        _assert_equal(errored_watch_state["profiles"][0]["runtime_error_count"], 2)
        _assert_watch_state_hides_chat_ids(errored_watch_state)
        errored_watch_item = _item_by_key(
            errored_payload["support_report"]["triage"]["items"],
            "watch_state_runtime_error",
        )
        _assert_equal(errored_watch_item["action"], watch_state_command_for_path(errored_watch_state_path))
        _assert_contains(str(errored_watch_item["message"]), "errors=2")

        _assert_equal(len(payload["bot_predictions"]["items"]), 3)
        _assert_equal(payload["bot_predictions"]["items"][0]["source"], SOURCE_HISTORY_HEADWAY)
        _assert_equal(payload["bot_predictions"]["items"][0]["event_kind"], BOT_EVENT_WATCH_EARLY)
        _assert_equal(payload["bot_predictions"]["items"][0]["change"], "")
        _assert_equal(payload["bot_predictions"]["items"][0]["history_scope"], "profile_time")
        _assert_equal(payload["bot_predictions"]["items"][0]["history_percentile"], 80)
        _assert_contains(payload["bot_predictions"]["items"][0]["eta_explanation"], "история p80: 24 замера")
        _assert_equal(payload["bot_predictions"]["items"][0]["eta_factors"][0]["kind"], "history_sample")
        _assert_equal(payload["bot_predictions"]["items"][1]["source"], SOURCE_TARGET_STOP_LIVE)
        _assert_equal(payload["bot_predictions"]["items"][1]["event_kind"], BOT_EVENT_USER_REPLY)
        _assert_contains(
            payload["bot_predictions"]["items"][1]["change"],
            "74-й позже на 8 мин · источник история Яндекса -> Яндекс live",
        )
        _assert_equal(payload["bot_predictions"]["items"][1]["error_minutes"], -2)
        _assert_equal(payload["bot_predictions"]["items"][1]["warning"], "координатный прогноз, держу запас 2 мин")
        _assert_contains(
            payload["bot_predictions"]["items"][1]["eta_explanation"],
            "слабая координата на 1 мин раньше не выбрана",
        )
        _assert_contains(
            payload["bot_predictions"]["items"][1]["eta_explanation"],
            "история на 8 мин раньше не выбрана",
        )
        _assert_contains(
            payload["bot_predictions"]["items"][1]["eta_explanation"],
            "прошлые поправки недоступны",
        )
        _assert_equal(payload["bot_predictions"]["items"][2]["source"], SOURCE_HISTORY_HEADWAY)
        _assert_equal(payload["bot_predictions"]["items"][2]["event_kind"], BOT_EVENT_USER_REPLY)
        _assert_equal(payload["bot_predictions"]["items"][2]["change"], "")
        quality = payload["bot_predictions"]["quality"]
        _assert_equal(quality["scope_event_kind"], BOT_EVENT_USER_REPLY)
        _assert_equal(quality["total"], 2)
        _assert_equal(quality["evaluated"], 1)
        _assert_equal(quality["pending"], 1)
        _assert_equal(quality["evaluated_percent"], 50)
        _assert_equal(quality["pending_percent"], 50)
        _assert_equal(quality["misses"], 1)
        _assert_equal(quality["miss_rate_percent"], 100)
        _assert_equal(quality["guardrail_unavailable"], 1)
        _assert_equal(quality["guardrail_unavailable_percent"], 50)
        _assert_equal(quality["average_error_minutes"], -2)
        _assert_equal(quality["p50_abs_error_minutes"], 2)
        _assert_equal(quality["by_profile"][0]["pending"], 1)
        _assert_equal(quality["by_profile"][0]["key"], MORNING.key)
        _assert_equal(morning_profile["runtime"]["total"], quality["by_profile"][0]["total"])
        _assert_equal(morning_profile["runtime"]["guardrail_unavailable"], 1)
        _assert_equal(morning_profile["runtime"]["guardrail_unavailable_percent"], 50)
        _assert_equal(quality["by_source"][0]["key"], SOURCE_HISTORY_HEADWAY)
        _assert_equal(quality["by_source"][1]["key"], SOURCE_TARGET_STOP_LIVE)
        _assert_equal(quality["by_profile_source"][0]["key"], f"{MORNING.key}/{SOURCE_HISTORY_HEADWAY}")
        _assert_equal(tuple(group["key"] for group in quality["by_event_kind"]), (BOT_EVENT_USER_REPLY,))
        calibration = payload["bot_predictions"]["calibration"]
        _assert_equal(calibration["status"], "insufficient")
        _assert_equal(calibration["by_profile"][0]["key"], MORNING.key)
        _assert_equal(morning_profile["calibration"]["status"], calibration["by_profile"][0]["status"])
        _assert_equal(calibration["by_source"][0]["key"], SOURCE_TARGET_STOP_LIVE)
        _assert_equal(calibration["by_profile_source"][0]["key"], f"{MORNING.key}/{SOURCE_TARGET_STOP_LIVE}")
        _assert_equal(morning_profile["source_calibration"]["key"], f"{MORNING.key}/{SOURCE_TARGET_STOP_LIVE}")
        _assert_equal(morning_profile["source_calibration"]["source_key"], SOURCE_TARGET_STOP_LIVE)
        _assert_equal(morning_profile["source_calibration"]["status"], calibration["by_profile_source"][0]["status"])
        _assert_dashboard_surfaces_runtime_calibration_late_risk(db_path, client)

        series = client.get("/api/windows/weekday_morning_09_12/series?days=30")
        _assert_equal(series.status_code, 200)
        _assert_equal(series.json()["rows"][0]["samples"], 2)

        recent = client.get("/api/recent-samples?window=weekday_evening_19_22&limit=5")
        _assert_equal(recent.status_code, 200)
        _assert_equal(len(recent.json()["items"]), 2)
        _assert_recent_samples_expose_source_reason()

        missing = client.get("/api/windows/nope/series")
        _assert_equal(missing.status_code, 404)
        _assert_api_error_details_are_sanitized(client)
        _assert_equal(_snapshot_count(db_path), before)
        _assert_readonly(db_path)

    print("OK | dashboard smoke passed")


def _assert_forecast_backtest_profile_payload_warns() -> None:
    payload = _profile_forecast_backtest(
        ForecastBacktestSummary(
            profile_key=MORNING.key,
            report_window_key="weekday_morning_09_12",
            history_days=14,
            bucket_minutes=30,
            min_samples=20,
            min_distinct_days=3,
            percentiles=(DEFAULT_HISTORY_PERCENTILE,),
            target_cases=8,
            results=(
                ForecastBacktestResult(
                    percentile=DEFAULT_HISTORY_PERCENTILE,
                    evaluated_cases=6,
                    skipped_cases=2,
                    miss_cases=3,
                    bucket_accurate_cases=4,
                    miss_minutes=12,
                    extra_wait_minutes=5,
                    mean_absolute_error=2.5,
                ),
            ),
        ),
        MORNING.key,
    )
    _assert_equal(payload["status"], "warning")
    _assert_equal(payload["ready"], False)
    _assert_equal(payload["percentile"], DEFAULT_HISTORY_PERCENTILE)
    _assert_equal(payload["evaluated_cases"], 6)
    _assert_equal(payload["target_cases"], 8)
    _assert_equal(payload["miss_cases"], 3)
    _assert_equal(payload["miss_rate_percent"], 50)
    _assert_equal(payload["bucket_accuracy_percent"], 67)
    _assert_equal(payload["command"], forecast_backtest_command_for_profile(MORNING.key))


def _assert_dashboard_latency_small_sample_is_warning() -> None:
    latency = BotLatencySummary(
        hours=24,
        latest_received_at=datetime(2026, 6, 4, 19, 0, tzinfo=NOVOSIBIRSK_TZ),
        total_events=1,
        invalid_duration_events=0,
        error_events=0,
        no_eta_events=1,
        p50_total_ms=19_965,
        p95_total_ms=19_965,
        p95_forecast_ms=19_965,
        p95_send_ms=0,
        statuses=(CountByKey("ok", 1),),
        source_methods=(CountByKey("browser", 1),),
        update_types=(CountByKey("http_request", 1),),
        event_kinds=(CountByKey(BOT_EVENT_USER_REPLY, 1),),
        reply_sources=(CountByKey("no_eta", 1),),
        error_reasons=(),
        no_eta_reasons=(CountByKey("history_insufficient", 1),),
        profile_key=EVENING.key,
        event_kind=BOT_EVENT_USER_REPLY,
        p95_render_ms=0,
    )
    payload = dashboard_data._profile_bot_latency(latency)
    _assert_equal(payload["status"], "warning")
    _assert_equal(payload["events"], 1)
    _assert_equal(payload["p95_total_ms"], 19_965)


def _assert_dashboard_evidence_payload_fallback() -> None:
    payload = _dashboard_evidence_payload((), current_time=now_local())
    _assert_equal(payload["status"], "missing")
    _assert_equal(payload["summary"], "нет подтверждения")


def _assert_api_error_details_are_sanitized(client: TestClient) -> None:
    raw_token = "123456:" + "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdef"
    raw_message = f"failed reading /home/vladimir/work-projects/74/.env token={raw_token} \x1b[31mboom\nnext"
    with patch.object(dashboard_app, "build_dashboard_summary", side_effect=RuntimeError(raw_message)):
        response = client.get("/api/summary")

    _assert_equal(response.status_code, 503)
    detail = response.json()["detail"]
    _assert_contains(detail, "<path>")
    _assert_contains(detail, "token=<redacted>")
    _assert_not_contains(detail, raw_token)
    _assert_not_contains(detail, "/home/vladimir")
    _assert_not_contains(detail, "\x1b")
    _assert_not_contains(detail, "\n")


def _assert_dashboard_surfaces_runtime_calibration_late_risk(db_path: Path, client: TestClient) -> None:
    runtime_base = now_local() - timedelta(minutes=4)
    with connect(db_path) as connection:
        _insert_runtime_prediction(
            connection,
            sampled_at=runtime_base,
            source=SOURCE_TARGET_STOP_LIVE,
            predicted_minutes=17,
            urgency="go_now",
            history_scope="report_window",
            history_report_window_key="weekday_morning_09_12",
            event_kind=BOT_EVENT_USER_REPLY,
            error_minutes=1,
        )
        _insert_runtime_prediction(
            connection,
            sampled_at=runtime_base + timedelta(minutes=1),
            source=SOURCE_TARGET_STOP_LIVE,
            predicted_minutes=17,
            urgency="go_now",
            history_scope="report_window",
            history_report_window_key="weekday_morning_09_12",
            event_kind=BOT_EVENT_USER_REPLY,
            error_minutes=1,
        )
        connection.commit()

    response = client.get("/api/summary")
    _assert_equal(response.status_code, 200)
    payload = response.json()
    calibration = payload["bot_predictions"]["calibration"]
    _assert_equal(calibration["status"], "late_risk")
    _assert_equal(calibration["suggested_buffer_minutes"], 2)
    morning_profile = _profile_by_key(payload["operator_profiles"], MORNING.key)
    source_calibration = morning_profile["source_calibration"]
    _assert_equal(source_calibration["key"], f"{MORNING.key}/{SOURCE_TARGET_STOP_LIVE}")
    _assert_equal(source_calibration["status"], "late_risk")
    _assert_equal(source_calibration["source_key"], SOURCE_TARGET_STOP_LIVE)
    _assert_equal(source_calibration["suggested_buffer_minutes"], 2)
    _assert_equal(source_calibration["command"], prediction_calibration_command_for_profile(MORNING.key))
    triage_items = payload["support_report"]["triage_by_window"]["weekday_morning_09_12"]["items"]
    late_risk = _item_by_key(triage_items, "bot_runtime_late_risk")
    _assert_contains(late_risk["message"], "profile=morning")
    _assert_contains(late_risk["message"], "suggested=+2m")
    _assert_equal(late_risk["action"], prediction_calibration_command_for_profile(MORNING.key))


def _assert_recent_samples_expose_source_reason() -> None:
    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "route74.sqlite"
        watch_state_path = Path(temp_dir) / "bot-watches.json"
        watch_state_path.write_text("{}", encoding="utf-8")
        with connect(db_path) as connection:
            init_db(connection)
            sampled_at = datetime(2026, 6, 4, 9, 10, tzinfo=NOVOSIBIRSK_TZ)
            forecast = YandexLiveForecast.unavailable(
                status=YandexSourceStatus.UNAVAILABLE,
                source_method=YandexSourceMethod.VEHICLE_PREDICTION,
                reason="browser_no_prediction_response",
            )
            insert_yandex_snapshot(connection, MORNING.key, forecast, sampled_at, fake_traffic_source())
        client = TestClient(create_app(db_path, watch_state_path=watch_state_path))
        recent = client.get("/api/recent-samples?window=weekday_morning_09_12&limit=1")
        _assert_equal(recent.status_code, 200)
        items = recent.json()["items"]
        _assert_equal(len(items), 1)
        _assert_equal(items[0]["source_status"], "unavailable")
        _assert_equal(items[0]["source_reason"], "browser_no_prediction_response")


def _assert_dashboard_surfaces_runtime_prediction_change() -> None:
    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "route74.sqlite"
        _seed(db_path)
        sampled_at = now_local() - timedelta(minutes=1)
        with connect(db_path) as connection:
            _insert_runtime_prediction(
                connection,
                sampled_at=sampled_at - timedelta(minutes=6),
                source=SOURCE_HISTORY_HEADWAY,
                predicted_minutes=16,
                urgency="relax",
                history_scope="profile_time",
                history_report_window_key="",
                event_kind=BOT_EVENT_USER_REPLY,
            )
            _insert_runtime_prediction(
                connection,
                sampled_at=sampled_at,
                source=SOURCE_TARGET_STOP_LIVE,
                predicted_minutes=14,
                urgency="go_now",
                history_scope="report_window",
                history_report_window_key="weekday_morning_09_12",
                event_kind=BOT_EVENT_USER_REPLY,
            )
            connection.commit()

        client = TestClient(create_app(db_path, watch_state_path=Path(temp_dir) / "bot-watches.json"))
        response = client.get("/api/summary")
        _assert_equal(response.status_code, 200)
        payload = response.json()
        items = payload["bot_predictions"]["items"]
        current = _runtime_prediction_by_eta(items, SOURCE_TARGET_STOP_LIVE, 14)
        morning_snapshot = _profile_by_key(payload["operator_profiles"], MORNING.key)["support_snapshot"]

    expected_change = "74-й позже на 4 мин · источник история Яндекса -> Яндекс live"
    _assert_equal(current["change"], expected_change)
    _assert_equal(morning_snapshot["latest_reply_change"], expected_change)
    _assert_contains(morning_snapshot["text"], f"🔁 С прошлого ответа: {expected_change}")


def _seed(db_path: Path) -> None:
    forecast = FakeYandexSource().get_forecast()
    base = datetime(2026, 6, 4, 9, 10, tzinfo=NOVOSIBIRSK_TZ)
    with connect(db_path) as connection:
        init_db(connection)
        update_collector_heartbeat(
            connection,
            name="yandex-collect",
            pid=123,
            profile_filter="all",
            last_status="ok",
            last_message="ok",
            updated_at=base,
        )
        for offset in (0, 30):
            sampled_at = base + timedelta(minutes=offset)
            insert_yandex_snapshot(connection, MORNING.key, forecast, sampled_at, fake_traffic_source())
            insert_collector_run(
                connection,
                name="yandex-collect",
                started_at=sampled_at,
                completed_at=sampled_at + timedelta(seconds=2),
                profile_filter="all",
                report_windows_only=True,
                active_profiles=(MORNING.key,),
                status="ok",
                message="ok",
                result_count=1,
                eta_result_count=1,
                traffic_ok_count=1,
            )
        evening = base.replace(hour=19, minute=10)
        for offset in (0, 30):
            sampled_at = evening + timedelta(minutes=offset)
            insert_yandex_snapshot(connection, EVENING.key, forecast, sampled_at, fake_traffic_source())
            insert_collector_run(
                connection,
                name="yandex-collect",
                started_at=sampled_at,
                completed_at=sampled_at + timedelta(seconds=2),
                profile_filter="all",
                report_windows_only=True,
                active_profiles=(EVENING.key,),
                status="ok",
                message="ok",
                result_count=1,
                eta_result_count=1,
                traffic_ok_count=1,
            )
        latency_base = now_local() - timedelta(minutes=3)
        insert_bot_interaction_event(
            connection,
            BotInteractionEvent(
                received_at=latency_base,
                chat_id=101,
                update_type="message",
                command="🎯 Поймать 74",
                event_kind=BOT_EVENT_USER_REPLY,
                reply_source="yandex",
                yandex_source_method="vehicle_prediction",
                forecast_ms=300,
                render_ms=2,
                send_ms=100,
                total_ms=402,
                status="ok",
                profile_key=MORNING.key,
            ),
        )
        insert_bot_interaction_event(
            connection,
            BotInteractionEvent(
                received_at=latency_base + timedelta(minutes=1),
                chat_id=101,
                update_type="callback_query",
                command="🔄 Обновить",
                event_kind=BOT_EVENT_USER_REPLY,
                reply_source="none",
                yandex_source_method="none",
                forecast_ms=10,
                render_ms=0,
                send_ms=1,
                total_ms=11,
                status="error",
                error="send failed " + "123456:" + "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi",
                profile_key=MORNING.key,
            ),
        )
        insert_bot_interaction_event(
            connection,
            BotInteractionEvent(
                received_at=latency_base + timedelta(minutes=1, seconds=30),
                chat_id=101,
                update_type="message",
                command="/start",
                event_kind=BOT_EVENT_USER_REPLY,
                reply_source="none",
                yandex_source_method="none",
                forecast_ms=20,
                render_ms=0,
                send_ms=1,
                total_ms=21,
                status="degraded",
                error="followup_send_error: RuntimeError: local followup failure",
                profile_key=MORNING.key,
            ),
        )
        insert_bot_interaction_event(
            connection,
            BotInteractionEvent(
                received_at=latency_base + timedelta(minutes=2),
                chat_id=102,
                update_type="message",
                command="🎯 Поймать 74",
                event_kind=BOT_EVENT_USER_REPLY,
                reply_source="no_eta",
                yandex_source_method="none",
                forecast_ms=120,
                render_ms=1,
                send_ms=50,
                total_ms=171,
                status="ok",
                profile_key=EVENING.key,
                no_eta_reason="yandex_no_target+history_unavailable",
            ),
        )
        runtime_base = now_local() - timedelta(minutes=8)
        _insert_runtime_prediction(
            connection,
            sampled_at=runtime_base - timedelta(minutes=10),
            source=SOURCE_HISTORY_HEADWAY,
            predicted_minutes=19,
            urgency="relax",
            history_scope="profile_time",
            history_report_window_key="",
            event_kind=BOT_EVENT_USER_REPLY,
        )
        _insert_runtime_prediction(
            connection,
            sampled_at=runtime_base,
            source=SOURCE_TARGET_STOP_LIVE,
            predicted_minutes=17,
            urgency="get_ready",
            history_scope="report_window",
            history_report_window_key="weekday_morning_09_12",
            event_kind=BOT_EVENT_USER_REPLY,
            error_minutes=-2,
            warning="координатный прогноз, держу запас 2 мин",
            extra_eta_factors=(
                {
                    "kind": "guardrail_unavailable",
                    "minutes": 0,
                    "sample_count": 0,
                    "percent": 0,
                    "scope": "",
                },
            ),
        )
        _insert_runtime_prediction(
            connection,
            sampled_at=runtime_base + timedelta(minutes=3),
            source=SOURCE_HISTORY_HEADWAY,
            predicted_minutes=24,
            urgency="relax",
            history_scope="profile_time",
            history_report_window_key="",
            event_kind=BOT_EVENT_WATCH_EARLY,
        )
        connection.commit()


def _insert_runtime_prediction(
    connection: sqlite3.Connection,
    *,
    sampled_at: datetime,
    source: str,
    predicted_minutes: int,
    urgency: str,
    history_scope: str,
    history_report_window_key: str,
    event_kind: str,
    error_minutes: int | None = None,
    warning: str = "",
    extra_eta_factors: tuple[dict[str, object], ...] = (),
) -> None:
    base_eta_factors = (
        [
            {
                "kind": "history_sample",
                "minutes": 0,
                "sample_count": 24,
                "percent": 0,
                "scope": "",
            }
        ]
        if source == SOURCE_HISTORY_HEADWAY
        else [
            {
                "kind": "safety_buffer",
                "minutes": 1,
                "sample_count": 24,
                "percent": 0,
                "scope": "",
            },
            {
                "kind": "ignored_weak_progress",
                "minutes": 1,
                "sample_count": 2,
                "percent": 0,
                "scope": "vehicle_progress",
            },
            {
                "kind": "history_disagreement",
                "minutes": 8,
                "sample_count": 120,
                "percent": 0,
                "scope": "history_earlier",
            },
        ]
    )
    eta_factors = tuple(base_eta_factors) + extra_eta_factors
    raw_json = json.dumps(
        {
            "runtime_source": RUNTIME_SOURCE_WEB_APP,
            "event_kind": event_kind,
            "selected_departure_source": "yandex_history" if source == SOURCE_HISTORY_HEADWAY else "yandex",
            "urgency": urgency,
            "leave_in_minutes": max(0, predicted_minutes - 15),
            "target_wait_minutes": 3 if source != SOURCE_HISTORY_HEADWAY else 6,
            "history_scope": history_scope,
            "history_report_window_key": history_report_window_key,
            "history_sample_count": 24,
            "history_bucket_minutes": 30,
            "history_percentile": 80 if source == SOURCE_HISTORY_HEADWAY else None,
            "eta_factors": eta_factors,
            "yandex_status": "ok",
            "warning": warning,
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
            MORNING.key,
            sampled_at.isoformat(),
            history_report_window_key,
            source,
            "history" if source == SOURCE_HISTORY_HEADWAY else "vehicle_prediction",
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
    if error_minutes is None:
        return
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
            MORNING.key,
            "",
            "",
            "stop",
            (sampled_at + timedelta(minutes=predicted_minutes + error_minutes)).isoformat(),
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
            int(cursor.lastrowid),
            int(arrival.lastrowid),
            MORNING.key,
            sampled_at.isoformat(),
            predicted_minutes + error_minutes,
            predicted_minutes,
            error_minutes,
            "15+",
            source,
            "{}",
        ),
    )


def _snapshot_count(db_path: Path) -> int:
    with connect_readonly(db_path) as connection:
        return count_yandex_snapshots(connection)


def _assert_readonly(db_path: Path) -> None:
    with connect_readonly(db_path) as connection:
        try:
            connection.execute("CREATE TABLE should_not_write(id INTEGER)")
        except sqlite3.OperationalError:
            return
    raise AssertionError("expected read-only dashboard DB connection")


def _assert_public_bind_guard(db_path: Path) -> None:
    try:
        parse_dashboard_config(["--db", str(db_path), "--host", "0.0.0.0"])
    except SystemExit:
        return
    raise AssertionError("expected public dashboard bind to require explicit opt-in")


def _assert_dashboard_port_guard(db_path: Path) -> None:
    with TemporaryDirectory() as temp_dir:
        env_path = Path(temp_dir) / ".env"
        env_path.write_text("ROUTE74_DASHBOARD_PORT=bad\n", encoding="utf-8")
        try:
            parse_dashboard_config(["--db", str(db_path), "--env-file", str(env_path)])
        except SystemExit as error:
            _assert_contains(str(error), "ROUTE74_DASHBOARD_PORT")
            _assert_contains(str(error), "1 to 65535")
        else:
            raise AssertionError("expected invalid dashboard env port to fail")

    error_output = StringIO()
    with redirect_stderr(error_output):
        try:
            parse_dashboard_config(["--db", str(db_path), "--host", "127.0.0.1", "--port", "0"])
        except SystemExit:
            _assert_contains(error_output.getvalue(), "--port")
            _assert_contains(error_output.getvalue(), "1 to 65535")
            return
    raise AssertionError("expected invalid dashboard CLI port to fail")


def _item_by_key(items: list[dict[str, object]], key: str) -> dict[str, object]:
    for item in items:
        if item.get("key") == key:
            return item
    raise AssertionError(f"expected item with key {key!r}")


def _profile_by_key(items: list[dict[str, object]], key: str) -> dict[str, object]:
    for item in items:
        if item.get("profile_key") == key:
            return item
    raise AssertionError(f"expected profile with key {key!r}")


def _runtime_prediction_by_eta(
    items: list[dict[str, object]], source: str, predicted_minutes: int
) -> dict[str, object]:
    for item in items:
        if item.get("source") == source and item.get("predicted_minutes") == predicted_minutes:
            return item
    raise AssertionError(f"expected runtime prediction source={source!r} eta={predicted_minutes!r}")


def _assert_watch_state_hides_chat_ids(watch_state: object) -> None:
    payload = json.dumps(watch_state, sort_keys=True)
    _assert_not_contains(payload, '"101":')
    _assert_not_contains(payload, "chat_id")


def _assert_watch_expiry_minutes(value: object) -> None:
    if not isinstance(value, int) or value < 20 or value > 30:
        raise AssertionError(f"expected watch expiry minutes in active range, got {value!r}")


def _stable_snapshot_payload(snapshot: dict[str, object]) -> dict[str, object]:
    payload = dict(snapshot)
    payload.pop("generated_at", None)
    payload.pop("text", None)
    return payload


def _assert_equal(actual: object, expected: object) -> None:
    if actual != expected:
        raise AssertionError(f"expected {expected!r}, got {actual!r}")


def _assert_contains(haystack: str, needle: str) -> None:
    if needle not in haystack:
        raise AssertionError(f"expected {needle!r} in output")


def _assert_not_contains(haystack: str, needle: str) -> None:
    if needle in haystack:
        raise AssertionError(f"did not expect {needle!r} in output")


if __name__ == "__main__":
    main()
