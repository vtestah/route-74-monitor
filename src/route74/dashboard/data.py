from __future__ import annotations

import json
import math
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from route74.build_info import load_build_info
from route74.dashboard.preview import (
    dashboard_preview_cache_dir,
    load_dashboard_preview,
)
from route74.diagnostics import sanitize_diagnostic_text
from route74.domain.commute_change import DepartureChange
from route74.domain.profiles import PROFILES_BY_KEY
from route74.domain.reporting import REPORT_WINDOWS, report_window_for_profile
from route74.domain.runtime_sources import (
    BOT_EVENT_USER_REPLY,
    BOT_EVENT_WATCH_EARLY,
    BOT_EVENT_WATCH_FINAL,
)
from route74.domain.yandex_history import DEFAULT_HISTORY_PERCENTILE
from route74.models import now_local
from route74.presenters.bot_errors import bot_error_category_text
from route74.presenters.commute_change import format_departure_change_details
from route74.presenters.eta_factors import format_eta_factor_payload_texts
from route74.presenters.no_eta_reason import no_eta_reason_text
from route74.presenters.support_snapshot import format_support_snapshot
from route74.services.commute_change import build_runtime_prediction_change_map
from route74.services.yandex_history import (
    DEFAULT_FALLBACK_BUCKET_MINUTES,
    DEFAULT_HISTORY_DAYS,
    DEFAULT_HISTORY_MAX_AGE_SECONDS,
    DEFAULT_MIN_HISTORY_DAYS,
    DEFAULT_MIN_OBSERVATIONS,
    DEFAULT_PRIMARY_BUCKET_MINUTES,
)
from route74.storage import (
    BotLatencySummary,
    BotRuntimeCalibration,
    BotRuntimeCalibrationGroup,
    BotRuntimePrediction,
    BotRuntimePredictionQuality,
    BotRuntimePredictionQualityGroup,
    ForecastReadinessSummary,
    connect_readonly,
    load_recent_bot_runtime_predictions,
    summarize_bot_latency,
    summarize_bot_runtime_calibration,
    summarize_report_windows,
    summarize_yandex_forecast_readiness,
    summarize_yandex_telemetry,
)
from route74.storage.forecast_backtest import (
    ForecastBacktestResult,
    ForecastBacktestSummary,
    best_forecast_backtest_result,
    selected_forecast_backtest_result,
)
from route74.storage.helpers import (
    WEEKDAYS,
    arrival_minutes_from_json,
    optional_int_value,
)
from route74.storage.monitoring import (
    DEFAULT_HISTORY_BACKTEST_MIN_EVALUATED,
    DEFAULT_HISTORY_BACKTEST_WARN_MISS_RATE_PERCENT,
    MonitorSummary,
    summarize_monitor,
)
from route74.support_actions import (
    bot_latency_command,
    bot_runtime_command,
    forecast_backtest_command_for_profile,
    forecast_coverage_command_for_window,
    forecast_readiness_command_for_profile,
    prediction_calibration_command_for_window,
    prediction_evaluate_command_for_window,
    support_report_command_for_profile,
    support_report_command_for_window,
    support_snapshot_command_for_profile,
    watch_state_command_for_path,
)
from route74.support_triage import (
    TRIAGE_STATUS_ORDER,
    SupportTriage,
    SupportTriageItem,
    build_support_triage,
    operator_primary_action,
    operator_primary_triage_item,
)
from route74.watch_state import (
    DEFAULT_WATCH_STATE_PATH,
    WatchStateSummary,
    summarize_watch_state,
)

WINDOWS_BY_KEY = {window.key: window for window in REPORT_WINDOWS}
BOT_LATENCY_MIN_EVENTS = 3
MIN_SERIES_SAMPLES_FOR_STATS = 3


@dataclass(frozen=True)
class _DashboardSupportSnapshotItem:
    severity: str
    key: str
    message: str
    action: str


@dataclass(frozen=True)
class _DashboardSupportSnapshotView:
    profile_key: str
    window_key: str
    hours: int
    current_time: datetime
    status: str
    primary_action: str
    primary_issue: _DashboardSupportSnapshotItem | None
    latest_reply_change: DepartureChange | None
    snapshot_command: str
    report_command: str
    items: tuple[_DashboardSupportSnapshotItem, ...]


def build_dashboard_summary(
    db_path: Path,
    *,
    watch_state_path: Path = DEFAULT_WATCH_STATE_PATH,
    preview_cache_path: Path | None = None,
) -> dict[str, object]:
    current_time = now_local()
    preview_cache_dir = preview_cache_path or dashboard_preview_cache_dir(db_path)
    with connect_readonly(db_path) as connection:
        monitor = summarize_monitor(
            connection,
            db_path=db_path,
            latency_hours=24,
            runtime_hours=24,
            current_time=current_time,
        )
        db_health = monitor.db
        forecast = monitor.forecast
        bot_latency = monitor.latency
        bot_prediction_quality = monitor.runtime
        if bot_prediction_quality is None:
            raise ValueError("dashboard monitor summary needs bot runtime quality")
        watch_state = summarize_watch_state(watch_state_path, current_time)
        telemetry = summarize_yandex_telemetry(connection, hours=24, current_time=current_time)
        report = summarize_report_windows(connection, days=30, current_time=current_time)
        bot_predictions = load_recent_bot_runtime_predictions(
            connection,
            current_time=current_time,
            hours=24,
            limit=8,
        )
        bot_prediction_history = load_recent_bot_runtime_predictions(
            connection,
            current_time=current_time,
            hours=24,
            limit=32,
            event_kind=BOT_EVENT_USER_REPLY,
        )
        bot_prediction_changes = build_runtime_prediction_change_map(
            bot_predictions,
            history_predictions=bot_prediction_history,
        )
        latest_reply_change_by_profile = _latest_reply_change_by_profile(bot_prediction_history)
        bot_prediction_calibration = monitor.calibration
        if bot_prediction_calibration is None:
            bot_prediction_calibration = summarize_bot_runtime_calibration(
                connection,
                current_time=current_time,
                hours=24,
            )
        latency_by_profile = _profile_latency_summaries(connection, current_time=current_time)
        monitor_by_profile = _profile_monitor_summaries(
            connection,
            db_path=db_path,
            current_time=current_time,
        )
        forecast_readiness_by_profile = _profile_forecast_readiness_summaries(
            connection,
            current_time=current_time,
        )
        support_triage = _support_report_payload(
            monitor,
            bot_prediction_quality,
            bot_prediction_calibration,
            watch_state,
            hours=24,
            monitor_by_profile=monitor_by_profile,
        )
        check_policy = _dashboard_check_policy(
            current_time=current_time,
            db_healthy=db_health.healthy,
            collector=forecast.collector,
            forecast_ready=forecast.ready,
            forecast_windows=forecast.windows,
            report_latest_sampled_at=report.latest_sampled_at,
            telemetry_latest_sampled_at=telemetry.latest_sampled_at,
            forecast_readiness_by_profile=forecast_readiness_by_profile,
            support_default_window_key=support_triage["default_window_key"],
            watch_status=watch_state.status,
        )
        operator_profiles = _operator_profiles_payload(
            triage_by_window=support_triage["triage_by_window"],
            forecast_windows=forecast.windows,
            monitor_by_profile=monitor_by_profile,
            runtime_quality=bot_prediction_quality,
            runtime_calibration=bot_prediction_calibration,
            latency_by_profile=latency_by_profile,
            forecast_readiness_by_profile=forecast_readiness_by_profile,
            watch_state=watch_state,
            current_time=current_time,
            hours=24,
            latest_reply_change_by_profile=latest_reply_change_by_profile,
            preview_cache_dir=preview_cache_dir,
        )
    return {
        "generated_at": _dt(current_time),
        "status": "ready" if db_health.healthy and forecast.ready else "not_ready",
        "build": load_build_info().to_jsonable(),
        "db": {
            "healthy": db_health.healthy,
            "size_bytes": db_health.db_size_bytes,
            "wal_size_bytes": db_health.wal_size_bytes,
            "journal_mode": db_health.journal_mode,
            "busy_timeout_ms": db_health.busy_timeout_ms,
            "foreign_keys": db_health.foreign_keys,
            "integrity_check": db_health.integrity_check,
            "quick_check": db_health.quick_check,
            "table_counts": _counts(db_health.table_counts),
            "latest": [{"key": item.key, "value": item.value} for item in db_health.latest_timestamps],
        },
        "collector": {
            "status": forecast.collector.status,
            "healthy": forecast.collector.healthy,
            "age_seconds": forecast.collector.age_seconds,
            "max_age_seconds": forecast.collector.max_age_seconds,
            "updated_at": _dt(forecast.collector.updated_at),
            "message": forecast.collector.message,
            "runs_24h": {
                "total": telemetry.collector_runs.total_runs,
                "eta": telemetry.collector_runs.eta_runs,
                "traffic_ok": telemetry.collector_runs.traffic_ok_runs,
                "skipped": telemetry.collector_runs.skipped_runs,
                "statuses": _counts(telemetry.collector_runs.statuses),
            },
        },
        "canary": {
            "status": forecast.canary.status,
            "healthy": forecast.canary.healthy,
            "latest_checked_at": _dt(forecast.canary.latest_checked_at),
            "risk_reason": forecast.canary.risk_reason,
            "risky_runs": forecast.canary.risky_runs,
        },
        "telemetry": {
            "snapshots_24h": telemetry.total_snapshots,
            "eta_coverage_percent": telemetry.eta_coverage_percent,
            "vehicle_coverage_percent": telemetry.vehicle_coverage_percent,
            "latest_sampled_at": _dt(telemetry.latest_sampled_at),
            "statuses": _counts(telemetry.statuses),
            "methods": _counts(telemetry.methods),
        },
        "report": {
            "samples_30d": report.total_samples,
            "eta_coverage_percent": report.eta_coverage_percent,
            "traffic_coverage_percent": report.traffic_coverage_percent,
            "latest_sampled_at": _dt(report.latest_sampled_at),
            "statuses": _counts(report.statuses),
        },
        "bot_latency": {
            "hours": bot_latency.hours,
            "profile_key": bot_latency.profile_key,
            "event_kind": bot_latency.event_kind,
            "latest_received_at": _dt(bot_latency.latest_received_at),
            "events": bot_latency.total_events,
            "errors": bot_latency.error_events,
            "error_rate_percent": bot_latency.error_rate_percent,
            "no_eta": bot_latency.no_eta_events,
            "no_eta_rate_percent": bot_latency.no_eta_rate_percent,
            "no_eta_reasons": _counts(bot_latency.no_eta_reasons),
            "top_no_eta_reason": _top_no_eta_reason(bot_latency.no_eta_reasons),
            "p50_total_ms": bot_latency.p50_total_ms,
            "p95_total_ms": bot_latency.p95_total_ms,
            "p95_forecast_ms": bot_latency.p95_forecast_ms,
            "p95_send_ms": bot_latency.p95_send_ms,
            "p95_followup_ms": bot_latency.p95_render_ms,
            "statuses": _counts(bot_latency.statuses),
            "updates": _counts(bot_latency.update_types),
            "event_kinds": _counts(bot_latency.event_kinds),
            "reply_sources": _counts(bot_latency.reply_sources),
            "methods": _counts(bot_latency.source_methods),
            "error_categories": _error_category_counts(bot_latency.error_categories),
            "top_error_category": _top_error_category(bot_latency.error_categories),
            "error_reasons": _counts(bot_latency.error_reasons),
        },
        "watch_state": {
            "path": str(watch_state.path),
            "command": watch_state_command_for_path(watch_state.path),
            "status": watch_state.status,
            "file_status": watch_state.file_status,
            "active_count": watch_state.active_count,
            "due_count": watch_state.due_count,
            "overdue_count": watch_state.overdue_count,
            "expired_records": watch_state.expired_records,
            "invalid_records": watch_state.invalid_records,
            "total_records": watch_state.total_records,
            "early_sent_count": watch_state.early_sent_count,
            "oldest_age_minutes": watch_state.oldest_age_minutes,
            "next_poll_at": _dt(watch_state.next_poll_at),
            "expires_at": _dt(watch_state.expires_at),
            "expires_in_minutes": watch_state.expires_in_minutes,
            "max_overdue_seconds": watch_state.max_overdue_seconds,
            "runtime_error_count": watch_state.runtime_error_count,
            "runtime_error_records": watch_state.runtime_error_records,
            "latest_error_at": _dt(watch_state.latest_error_at),
            "runtime_error_types": list(watch_state.runtime_error_types),
            "profiles": [
                {
                    "profile_key": profile.profile_key,
                    "active_count": profile.active_count,
                    "due_count": profile.due_count,
                    "early_sent_count": profile.early_sent_count,
                    "oldest_age_minutes": profile.oldest_age_minutes,
                    "next_poll_at": _dt(profile.next_poll_at),
                    "expires_at": _dt(profile.expires_at),
                    "expires_in_minutes": profile.expires_in_minutes,
                    "runtime_error_count": profile.runtime_error_count,
                    "runtime_error_records": profile.runtime_error_records,
                    "latest_error_at": _dt(profile.latest_error_at),
                    "runtime_error_types": list(profile.runtime_error_types),
                }
                for profile in watch_state.profiles
            ],
        },
        "bot_predictions": {
            "hours": 24,
            "quality": _bot_prediction_quality(bot_prediction_quality, event_kind=BOT_EVENT_USER_REPLY),
            "calibration": _bot_prediction_calibration(bot_prediction_calibration),
            "commands": {
                "all": bot_runtime_command(hours=24, limit=8),
                BOT_EVENT_USER_REPLY: bot_runtime_command(hours=24, limit=8, event_kind=BOT_EVENT_USER_REPLY),
                BOT_EVENT_WATCH_EARLY: bot_runtime_command(hours=24, limit=8, event_kind=BOT_EVENT_WATCH_EARLY),
                BOT_EVENT_WATCH_FINAL: bot_runtime_command(hours=24, limit=8, event_kind=BOT_EVENT_WATCH_FINAL),
            },
            "items": [_bot_prediction(item, change=bot_prediction_changes.get(item.id)) for item in bot_predictions],
        },
        "forecast": {
            "ready": forecast.ready,
            "ready_windows": forecast.ready_windows,
            "total_windows": forecast.total_windows,
            "windows": [_window_health(window) for window in forecast.windows],
        },
        "support_report": {
            "default_window_key": support_triage["default_window_key"],
            "commands": {window.key: support_report_command_for_window(window.key) for window in REPORT_WINDOWS},
            "triage": support_triage["triage"],
            "triage_by_window": support_triage["triage_by_window"],
        },
        "check_policy": check_policy,
        "operator_profiles": operator_profiles,
        "evidence": _dashboard_evidence_payload(
            bot_prediction_history,
            current_time=current_time,
        ),
        "prediction_evaluate": {
            "commands": {window.key: prediction_evaluate_command_for_window(window.key) for window in REPORT_WINDOWS},
        },
    }


def _dashboard_evidence_payload(
    predictions: tuple[BotRuntimePrediction, ...] | list[BotRuntimePrediction],
    *,
    current_time: datetime,
) -> dict[str, object]:
    latest = next(
        (prediction for prediction in predictions if prediction.event_kind == BOT_EVENT_USER_REPLY),
        None,
    )
    if latest is None:
        return _dashboard_evidence_missing_payload()
    profile = PROFILES_BY_KEY.get(latest.profile_key)
    window = WINDOWS_BY_KEY.get(latest.report_window_key)
    freshness_seconds = max(0, round((current_time - latest.sampled_at).total_seconds()))
    profile_title = profile.title if profile is not None else latest.profile_key
    window_title = window.title if window is not None else latest.report_window_key
    return _dashboard_evidence_available_payload(
        latest,
        profile_title=profile_title,
        window_title=window_title,
        freshness_seconds=freshness_seconds,
    )


def _dashboard_evidence_missing_payload() -> dict[str, object]:
    return {
        "status": "missing",
        "route": "74",
        "summary": "нет подтверждения",
        "profile_key": "",
        "profile_title": "",
        "window_key": "",
        "window_title": "",
        "sampled_at": None,
        "freshness_seconds": None,
        "confidence": "",
        "source": "",
        "source_method": "",
        "selected_departure_source": "",
        "predicted_minutes": None,
        "predicted_arrival_at": None,
        "yandex_status": "",
    }


def _dashboard_evidence_available_payload(
    latest: BotRuntimePrediction,
    *,
    profile_title: str,
    window_title: str,
    freshness_seconds: int,
) -> dict[str, object]:
    return {
        "status": "available",
        "route": "74",
        "summary": f"Маршрут 74 · {profile_title} · {window_title}",
        "profile_key": latest.profile_key,
        "profile_title": profile_title,
        "window_key": latest.report_window_key,
        "window_title": window_title,
        "sampled_at": _dt(latest.sampled_at),
        "freshness_seconds": freshness_seconds,
        "confidence": latest.confidence,
        "source": latest.source,
        "source_method": latest.source_method,
        "selected_departure_source": latest.selected_departure_source,
        "predicted_minutes": latest.predicted_minutes,
        "predicted_arrival_at": _dt(latest.predicted_arrival_at),
        "yandex_status": latest.yandex_status,
    }


def build_dashboard_support_snapshot(
    db_path: Path,
    profile_key: str,
    *,
    watch_state_path: Path = DEFAULT_WATCH_STATE_PATH,
) -> dict[str, object]:
    if profile_key not in PROFILES_BY_KEY:
        raise KeyError(profile_key)
    current_time = now_local()
    window = report_window_for_profile(profile_key)
    watch_state = summarize_watch_state(watch_state_path, current_time)
    with connect_readonly(db_path) as connection:
        monitor = summarize_monitor(
            connection,
            db_path=db_path,
            latency_hours=24,
            runtime_hours=24,
            profile_key=profile_key,
            current_time=current_time,
        )
        latest_reply_change = _latest_reply_change_for_profile(
            connection,
            profile_key=profile_key,
            current_time=current_time,
            hours=24,
        )
    if monitor.runtime is None or monitor.calibration is None:
        raise ValueError("dashboard support snapshot needs bot runtime diagnostics")
    triage = _support_triage_json(
        build_support_triage(
            window_key=window.key,
            profile_key=profile_key,
            hours=24,
            monitor=monitor,
            forecast=monitor.forecast,
            runtime_quality=monitor.runtime,
            runtime_calibration=monitor.calibration,
            watch_state=watch_state,
        )
    )
    return _profile_support_snapshot(
        window,
        triage,
        current_time=current_time,
        hours=24,
        latest_reply_change=latest_reply_change,
    )


def _dashboard_check_policy(
    *,
    current_time: datetime,
    db_healthy: bool,
    collector: object,
    forecast_ready: bool,
    forecast_windows: tuple[object, ...],
    report_latest_sampled_at: datetime | None,
    telemetry_latest_sampled_at: datetime | None,
    forecast_readiness_by_profile: dict[str, ForecastReadinessSummary],
    support_default_window_key: str,
    watch_status: str,
) -> dict[str, object]:
    default_window = WINDOWS_BY_KEY.get(support_default_window_key, REPORT_WINDOWS[0])
    return {
        "summary": (
            "Автопроверки обновляются вместе со сводкой; ручную диагностику "
            "запускай только по warning/critical сигналу."
        ),
        "mode_note": (
            "Это policy-слой dashboard: он классифицирует уже собранные сигналы, а тяжёлые команды не запускает сам."
        ),
        "auto_checks": [
            {
                "key": "startup_health",
                "label": "startup/health",
                "status": "ok" if db_healthy else "critical",
                "last_run_at": _dt(current_time),
                "reason": _startup_health_reason(db_healthy, watch_status),
            },
            {
                "key": "collector_ingest",
                "label": "collector/ingest",
                "status": _collector_check_status(collector),
                "last_run_at": _dt(getattr(collector, "updated_at", None)),
                "reason": _collector_check_reason(collector),
            },
            {
                "key": "forecast_refresh",
                "label": "forecast refresh",
                "status": "ok" if forecast_ready else "warning",
                "last_run_at": _dt(_latest_window_sampled_at(forecast_windows, report_latest_sampled_at)),
                "reason": _forecast_refresh_reason(forecast_ready, forecast_windows),
            },
            {
                "key": "history_backfill",
                "label": "history/backfill",
                "status": _history_backfill_status(forecast_readiness_by_profile),
                "last_run_at": _dt(_latest_forecast_readiness_at(forecast_readiness_by_profile)),
                "reason": _history_backfill_reason(forecast_readiness_by_profile),
            },
            {
                "key": "freshness_timer",
                "label": "freshness timer",
                "status": _freshness_timer_status(
                    telemetry_latest_sampled_at=telemetry_latest_sampled_at,
                    current_time=current_time,
                    collector=collector,
                ),
                "last_run_at": _dt(telemetry_latest_sampled_at),
                "reason": _freshness_timer_reason(
                    telemetry_latest_sampled_at=telemetry_latest_sampled_at,
                    current_time=current_time,
                ),
            },
        ],
        "manual_diagnostics": [
            {
                "key": "support_snapshot",
                "label": "Быстрый support snapshot",
                "command": support_snapshot_command_for_profile(default_window.profile_key),
                "when": "Когда нужен короткий профильный срез без полного drill-down.",
                "reason": f"Показывает быстрый runtime/support срез по профилю {default_window.profile_key}.",
            },
            {
                "key": "support_report",
                "label": "Полный support report",
                "command": support_report_command_for_window(default_window.key),
                "when": "Когда auto checks ушли в warning/critical и нужен полный разбор окна.",
                "reason": "Глубокий разбор окна, runtime, watch и history readiness.",
            },
            {
                "key": "prediction_evaluate",
                "label": "Проверка runtime-фактов",
                "command": prediction_evaluate_command_for_window(default_window.key),
                "when": "Когда есть pending/miss ответы или нужна перепроверка фактов.",
                "reason": "Тяжёлая ручная сверка prediction events против фактических приездов.",
            },
            {
                "key": "forecast_backtest",
                "label": "Проверка history percentile",
                "command": forecast_backtest_command_for_profile(default_window.profile_key),
                "when": "Когда history готова, но качество fallback всё ещё под вопросом.",
                "reason": "Дорогая проверка percentile, промахов и лишнего ожидания history fallback.",
            },
        ],
    }


def load_window_series(db_path: Path, window_key: str, *, days: int) -> dict[str, object]:
    _require_window(window_key)
    since = now_local() - timedelta(days=max(1, min(days, 120)))
    with connect_readonly(db_path) as connection:
        rows = connection.execute(
            """
            SELECT service_date, arrival_minutes_json, traffic_status,
                   traffic_delay_seconds, traffic_jams_level
            FROM report_window_snapshots
            WHERE report_window_key = ?
              AND sampled_at >= ?
            ORDER BY service_date, sampled_at
            """,
            (window_key, since.isoformat()),
        ).fetchall()
    grouped: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        grouped[str(row["service_date"])].append(row)
    return {
        "window_key": window_key,
        "days": days,
        "rows": [_series_row(day, day_rows) for day, day_rows in sorted(grouped.items())],
    }


def load_recent_samples(db_path: Path, *, window_key: str | None, limit: int) -> dict[str, object]:
    if window_key:
        _require_window(window_key)
    limit = max(1, min(limit, 200))
    filters = []
    params: list[object] = []
    if window_key:
        filters.append("report_window_key = ?")
        params.append(window_key)
    where = f"WHERE {' AND '.join(filters)}" if filters else ""
    params.append(limit)
    with connect_readonly(db_path) as connection:
        rows = connection.execute(
            f"""
            SELECT sampled_at, report_window_key, profile_key, source_method,
                   source_status, arrival_minutes_json, traffic_status,
                   traffic_delay_seconds, traffic_jams_level, raw_json
            FROM report_window_snapshots
            {where}
            ORDER BY sampled_at DESC
            LIMIT ?
            """,
            tuple(params),
        ).fetchall()
    return {"items": [_recent_row(row) for row in rows]}


def _bot_prediction(item: BotRuntimePrediction, *, change: DepartureChange | None = None) -> dict[str, object]:
    return {
        "id": item.id,
        "sampled_at": _dt(item.sampled_at),
        "profile_key": item.profile_key,
        "window_key": item.report_window_key,
        "source": item.source,
        "source_method": item.source_method,
        "predicted_minutes": item.predicted_minutes,
        "predicted_arrival_at": _dt(item.predicted_arrival_at),
        "confidence": item.confidence,
        "urgency": item.urgency,
        "selected_departure_source": item.selected_departure_source,
        "leave_in_minutes": item.leave_in_minutes,
        "target_wait_minutes": item.target_wait_minutes,
        "history_scope": item.history_scope,
        "history_report_window_key": item.history_report_window_key,
        "history_sample_count": item.history_sample_count,
        "history_bucket_minutes": item.history_bucket_minutes,
        "history_percentile": item.history_percentile,
        "yandex_status": item.yandex_status,
        "eta_factors": list(item.eta_factors),
        "eta_explanation": "; ".join(format_eta_factor_payload_texts(item.eta_factors)),
        "warning": item.warning,
        "actual_minutes": item.actual_minutes,
        "error_minutes": item.error_minutes,
        "evaluated_at": _dt(item.evaluated_at),
        "event_kind": item.event_kind,
        "change": format_departure_change_details(change),
    }


def _latest_reply_change_for_profile(
    connection: sqlite3.Connection,
    *,
    profile_key: str,
    current_time: datetime,
    hours: int,
) -> DepartureChange | None:
    predictions = load_recent_bot_runtime_predictions(
        connection,
        current_time=current_time,
        hours=hours,
        limit=8,
        profile_key=profile_key,
        event_kind=BOT_EVENT_USER_REPLY,
    )
    return _latest_reply_change_by_profile(predictions).get(profile_key)


def _latest_reply_change_by_profile(
    predictions: list[BotRuntimePrediction] | tuple[BotRuntimePrediction, ...],
) -> dict[str, DepartureChange]:
    latest_by_profile: dict[str, BotRuntimePrediction] = {}
    for prediction in predictions:
        if prediction.event_kind != BOT_EVENT_USER_REPLY:
            continue
        current = latest_by_profile.get(prediction.profile_key)
        if current is None or prediction.sampled_at > current.sampled_at:
            latest_by_profile[prediction.profile_key] = prediction
    changes = build_runtime_prediction_change_map(
        latest_by_profile.values(),
        history_predictions=predictions,
    )
    return {
        prediction.profile_key: change
        for prediction in latest_by_profile.values()
        if (change := changes.get(prediction.id)) is not None
    }


def _bot_prediction_quality(
    summary: BotRuntimePredictionQuality,
    *,
    event_kind: str | None = None,
) -> dict[str, object]:
    return {
        "hours": summary.hours,
        "scope_event_kind": event_kind,
        "total": summary.total,
        "evaluated": summary.evaluated,
        "pending": summary.pending,
        "misses": summary.misses,
        "guardrail_unavailable": summary.guardrail_unavailable,
        "evaluated_percent": summary.evaluated_percent,
        "pending_percent": summary.pending_percent,
        "miss_rate_percent": summary.miss_rate_percent,
        "guardrail_unavailable_percent": summary.guardrail_unavailable_percent,
        "average_error_minutes": summary.average_error_minutes,
        "p50_abs_error_minutes": summary.p50_abs_error_minutes,
        "latest_sampled_at": _dt(summary.latest_sampled_at),
        "latest_evaluated_at": _dt(summary.latest_evaluated_at),
        "oldest_pending_sampled_at": _dt(summary.oldest_pending_sampled_at),
        "by_profile": [_bot_prediction_quality_group(group) for group in summary.by_profile],
        "by_source": [_bot_prediction_quality_group(group) for group in summary.by_source],
        "by_profile_source": [_bot_prediction_quality_group(group) for group in summary.by_profile_source],
        "by_event_kind": [_bot_prediction_quality_group(group) for group in summary.by_event_kind],
    }


def _bot_prediction_quality_group(
    group: BotRuntimePredictionQualityGroup,
) -> dict[str, object]:
    return {
        "key": group.key,
        "total": group.total,
        "evaluated": group.evaluated,
        "pending": group.pending,
        "misses": group.misses,
        "guardrail_unavailable": group.guardrail_unavailable,
        "evaluated_percent": group.evaluated_percent,
        "pending_percent": group.pending_percent,
        "miss_rate_percent": group.miss_rate_percent,
        "guardrail_unavailable_percent": group.guardrail_unavailable_percent,
        "average_error_minutes": group.average_error_minutes,
        "p50_abs_error_minutes": group.p50_abs_error_minutes,
        "latest_sampled_at": _dt(group.latest_sampled_at),
        "latest_evaluated_at": _dt(group.latest_evaluated_at),
        "oldest_pending_sampled_at": _dt(group.oldest_pending_sampled_at),
    }


def _bot_prediction_calibration(summary: BotRuntimeCalibration) -> dict[str, object]:
    return {
        "hours": summary.hours,
        "total": summary.total,
        "evaluated": summary.evaluated,
        "misses": summary.misses,
        "evaluated_percent": summary.evaluated_percent,
        "miss_rate_percent": summary.miss_rate_percent,
        "p80_early_minutes": summary.p80_early_minutes,
        "p50_extra_wait_minutes": summary.p50_extra_wait_minutes,
        "suggested_buffer_minutes": summary.suggested_buffer_minutes,
        "status": summary.status,
        "action": summary.action,
        "by_profile": [_bot_prediction_calibration_group(group) for group in summary.by_profile],
        "by_source": [_bot_prediction_calibration_group(group) for group in summary.by_source],
        "by_profile_source": [_bot_prediction_calibration_group(group) for group in summary.by_profile_source],
    }


def _bot_prediction_calibration_group(
    group: BotRuntimeCalibrationGroup,
) -> dict[str, object]:
    return {
        "key": group.key,
        "total": group.total,
        "evaluated": group.evaluated,
        "misses": group.misses,
        "evaluated_percent": group.evaluated_percent,
        "miss_rate_percent": group.miss_rate_percent,
        "p80_early_minutes": group.p80_early_minutes,
        "p50_extra_wait_minutes": group.p50_extra_wait_minutes,
        "suggested_buffer_minutes": group.suggested_buffer_minutes,
        "status": group.status,
        "action": group.action,
    }


def _support_report_payload(
    monitor: MonitorSummary,
    runtime_quality: BotRuntimePredictionQuality,
    runtime_calibration: BotRuntimeCalibration,
    watch_state: WatchStateSummary,
    *,
    hours: int,
    monitor_by_profile: dict[str, MonitorSummary] | None = None,
) -> dict[str, object]:
    profile_monitors = monitor_by_profile or {}
    triage_by_window = {
        window.key: _support_triage_json(
            _support_triage_for_window(
                window.key,
                window.profile_key,
                monitor=profile_monitors.get(window.profile_key, monitor),
                fallback_runtime_quality=runtime_quality,
                fallback_runtime_calibration=runtime_calibration,
                watch_state=watch_state,
                hours=hours,
            )
        )
        for window in REPORT_WINDOWS
    }
    default_window_key = _default_support_window_key(triage_by_window)
    return {
        "default_window_key": default_window_key,
        "triage": triage_by_window[default_window_key],
        "triage_by_window": triage_by_window,
    }


def _support_triage_for_window(
    window_key: str,
    profile_key: str,
    *,
    monitor: MonitorSummary,
    fallback_runtime_quality: BotRuntimePredictionQuality,
    fallback_runtime_calibration: BotRuntimeCalibration,
    watch_state: WatchStateSummary,
    hours: int,
) -> SupportTriage:
    return build_support_triage(
        window_key=window_key,
        profile_key=profile_key,
        hours=hours,
        monitor=monitor,
        forecast=monitor.forecast,
        runtime_quality=monitor.runtime or fallback_runtime_quality,
        runtime_calibration=monitor.calibration or fallback_runtime_calibration,
        watch_state=watch_state,
    )


def _default_support_window_key(triage_by_window: dict[str, dict[str, object]]) -> str:
    return max(
        (window.key for window in REPORT_WINDOWS),
        key=lambda key: _triage_priority(triage_by_window[key]),
    )


def _triage_priority(triage: dict[str, object]) -> tuple[int, int]:
    status = str(triage.get("status", "ok"))
    items = triage.get("items")
    item_count = len(items) if isinstance(items, list) else 0
    return TRIAGE_STATUS_ORDER.get(status, 0), item_count


def _support_triage_json(triage: SupportTriage) -> dict[str, object]:
    return {
        "status": triage.status,
        "primary_action": operator_primary_action(triage),
        "primary_issue": _triage_item_json(operator_primary_triage_item(triage)),
        "items": [_triage_item_json(item) for item in triage.items],
    }


def _triage_item_json(item: SupportTriageItem | None) -> dict[str, object] | None:
    if item is None:
        return None
    return {
        "severity": item.severity,
        "key": item.key,
        "message": item.message,
        "action": item.action,
    }


def _operator_profiles_payload(
    *,
    triage_by_window: dict[str, dict[str, object]],
    forecast_windows: tuple[object, ...],
    monitor_by_profile: dict[str, MonitorSummary],
    runtime_quality: BotRuntimePredictionQuality,
    runtime_calibration: BotRuntimeCalibration,
    latency_by_profile: dict[str, BotLatencySummary],
    forecast_readiness_by_profile: dict[str, ForecastReadinessSummary],
    watch_state: WatchStateSummary,
    current_time: datetime,
    hours: int,
    latest_reply_change_by_profile: dict[str, DepartureChange],
    preview_cache_dir: Path,
) -> list[dict[str, object]]:
    forecast_by_window = {str(getattr(window, "window_key", "")): window for window in forecast_windows}
    quality_by_profile = {group.key: group for group in runtime_quality.by_profile}
    calibration_by_profile = {group.key: group for group in runtime_calibration.by_profile}
    source_calibration_by_profile = _source_calibration_by_profile(runtime_calibration.by_profile_source)
    watch_by_profile = {profile.profile_key: profile for profile in watch_state.profiles}
    profiles: list[dict[str, object]] = []
    for window in REPORT_WINDOWS:
        triage = triage_by_window.get(window.key, {"status": "ok", "primary_action": "", "items": []})
        profile_monitor = monitor_by_profile.get(window.profile_key)
        profiles.append(
            {
                "profile_key": window.profile_key,
                "profile_title": PROFILES_BY_KEY[window.profile_key].title,
                "window_key": window.key,
                "window_title": window.title,
                "status": str(triage.get("status", "ok")),
                "primary_action": str(triage.get("primary_action", "")),
                "primary_issue": _profile_primary_issue(triage),
                "issue_count": _profile_issue_count(triage),
                "support_snapshot": _profile_support_snapshot(
                    window,
                    triage,
                    current_time=current_time,
                    hours=hours,
                    latest_reply_change=latest_reply_change_by_profile.get(window.profile_key),
                ),
                "forecast": _profile_forecast(forecast_by_window.get(window.key)),
                "forecast_readiness": _profile_forecast_readiness(
                    forecast_readiness_by_profile.get(window.profile_key),
                    window.profile_key,
                ),
                "forecast_backtest": _profile_forecast_backtest(
                    profile_monitor.backtest if profile_monitor is not None else None,
                    window.profile_key,
                ),
                "runtime": _profile_runtime_quality(quality_by_profile.get(window.profile_key)),
                "calibration": _profile_runtime_calibration(calibration_by_profile.get(window.profile_key)),
                "source_calibration": _profile_runtime_source_calibration(
                    source_calibration_by_profile.get(window.profile_key),
                    window.key,
                ),
                "latency": _profile_bot_latency(latency_by_profile.get(window.profile_key)),
                "watch": _profile_watch_state(watch_by_profile.get(window.profile_key), watch_state),
                "preview": load_dashboard_preview(
                    preview_cache_dir,
                    window.profile_key,
                    current_time=current_time,
                ),
            }
        )
    return profiles


def _profile_support_snapshot(
    window: object,
    triage: dict[str, object],
    *,
    current_time: datetime,
    hours: int,
    latest_reply_change: DepartureChange | None = None,
) -> dict[str, object]:
    all_items = _profile_support_snapshot_all_items(triage)
    items = _profile_support_snapshot_visible_items(all_items, triage)
    profile_key = str(getattr(window, "profile_key", ""))
    window_key = str(getattr(window, "key", ""))
    status = str(triage.get("status", "ok"))
    primary_action = str(triage.get("primary_action", ""))
    primary_issue = _profile_primary_issue(triage)
    snapshot_command = support_snapshot_command_for_profile(profile_key)
    report_command = support_report_command_for_profile(profile_key)
    return {
        "profile_key": profile_key,
        "window_key": window_key,
        "hours": hours,
        "generated_at": _dt(current_time),
        "status": status,
        "primary_action": primary_action,
        "primary_issue": primary_issue,
        "snapshot_command": snapshot_command,
        "report_command": report_command,
        "latest_reply_change": format_departure_change_details(latest_reply_change),
        "items": items,
        "diagnostic_commands": _profile_support_snapshot_commands(
            primary_action=primary_action,
            snapshot_command=snapshot_command,
            report_command=report_command,
        ),
        "item_count": len(all_items),
        "actionable_count": _profile_support_snapshot_actionable_count(all_items),
        "hidden_item_count": max(0, len(all_items) - len(items)),
        "text": _format_profile_support_snapshot(
            profile_key=profile_key,
            window_key=window_key,
            hours=hours,
            current_time=current_time,
            status=status,
            primary_action=primary_action,
            primary_issue=primary_issue,
            snapshot_command=snapshot_command,
            report_command=report_command,
            latest_reply_change=latest_reply_change,
            items=all_items,
        ),
    }


def _profile_support_snapshot_all_items(
    triage: dict[str, object],
) -> list[dict[str, object]]:
    items = triage.get("items")
    if not isinstance(items, list):
        return []
    return [_profile_issue_payload(item) for item in items if isinstance(item, dict)]


def _profile_support_snapshot_visible_items(
    item_payloads: list[dict[str, object]],
    triage: dict[str, object],
    *,
    limit: int = 5,
) -> list[dict[str, object]]:
    actionable = [item for item in item_payloads if item["severity"] in {"warning", "critical"}]
    visible = actionable or item_payloads
    primary_issue = _profile_primary_issue(triage)
    primary_identity = _issue_identity(primary_issue) if primary_issue is not None else None
    primary = next((item for item in visible if _issue_identity(item) == primary_identity), None)
    if primary is not None:
        visible = [
            primary,
            *(item for item in visible if _issue_identity(item) != primary_identity),
        ]
    return visible[:limit]


def _profile_support_snapshot_actionable_count(items: list[dict[str, object]]) -> int:
    return sum(1 for item in items if item["severity"] in {"warning", "critical"})


def _profile_support_snapshot_commands(
    *,
    primary_action: str,
    snapshot_command: str,
    report_command: str,
) -> list[dict[str, str]]:
    commands: list[dict[str, str]] = []
    seen: set[str] = set()
    for label, command in (
        ("Следующая диагностика", primary_action),
        ("Быстрый support snapshot", snapshot_command),
        ("Полный support report", report_command),
    ):
        if not command or command in seen:
            continue
        seen.add(command)
        commands.append({"label": label, "command": command})
    return commands


def _startup_health_reason(db_healthy: bool, watch_status: str) -> str:
    db_text = "SQLite ok" if db_healthy else "SQLite health failed"
    watch_text = f"watch {watch_status}" if watch_status else "watch unknown"
    return f"{db_text} · {watch_text}"


def _collector_check_status(collector: object) -> str:
    if bool(getattr(collector, "healthy", False)):
        return "ok"
    if getattr(collector, "updated_at", None) is None:
        return "critical"
    return "warning"


def _collector_check_reason(collector: object) -> str:
    message = str(getattr(collector, "message", "") or "").strip()
    if message:
        return message
    updated_at = getattr(collector, "updated_at", None)
    if updated_at is None:
        return "collector heartbeat пока не появился"
    age_seconds = getattr(collector, "age_seconds", None)
    if isinstance(age_seconds, (int, float)):
        return f"последний heartbeat {_duration_text(int(age_seconds))} назад"
    return "collector heartbeat доступен"


def _forecast_refresh_reason(
    forecast_ready: bool,
    forecast_windows: tuple[object, ...],
) -> str:
    ready_windows = sum(1 for window in forecast_windows if bool(getattr(window, "ready", False)))
    total_windows = len(forecast_windows)
    if forecast_ready:
        return f"готово {ready_windows}/{total_windows} окон"
    blocked = next(
        (window for window in forecast_windows if not bool(getattr(window, "ready", False))),
        None,
    )
    if blocked is None:
        return f"готово {ready_windows}/{total_windows} окон"
    return (
        f"{getattr(blocked, 'title', getattr(blocked, 'window_key', 'window'))}: "
        f"{getattr(blocked, 'reason', 'нужна проверка')}"
    )


def _history_backfill_status(
    forecast_readiness_by_profile: dict[str, ForecastReadinessSummary],
) -> str:
    if not forecast_readiness_by_profile:
        return "critical"
    statuses = []
    for window in REPORT_WINDOWS:
        summary = forecast_readiness_by_profile.get(window.profile_key)
        if summary is None:
            statuses.append("missing")
        elif summary.ready:
            statuses.append("ready")
        else:
            statuses.append("not_ready")
    if all(status == "ready" for status in statuses):
        return "ok"
    if "missing" in statuses:
        return "critical"
    return "warning"


def _history_backfill_reason(
    forecast_readiness_by_profile: dict[str, ForecastReadinessSummary],
) -> str:
    if not forecast_readiness_by_profile:
        return "витрина history readiness пока недоступна"
    ready_profiles = sum(
        1
        for window in REPORT_WINDOWS
        if forecast_readiness_by_profile.get(window.profile_key, None)
        and forecast_readiness_by_profile[window.profile_key].ready
    )
    blocked_window = next(
        (
            window
            for window in REPORT_WINDOWS
            if not forecast_readiness_by_profile.get(window.profile_key)
            or not forecast_readiness_by_profile[window.profile_key].ready
        ),
        None,
    )
    if blocked_window is None:
        return f"история готова {ready_profiles}/{len(REPORT_WINDOWS)} профилей"
    summary = forecast_readiness_by_profile.get(blocked_window.profile_key)
    if summary is None:
        return f"{PROFILES_BY_KEY[blocked_window.profile_key].title}: readiness-данные ещё не собраны"
    return (
        f"{PROFILES_BY_KEY[blocked_window.profile_key].title}: "
        f"samples {summary.selected_sample_count}/{summary.min_samples} · "
        f"days {summary.selected_distinct_days}/{summary.min_distinct_days}"
    )


def _freshness_timer_status(
    *,
    telemetry_latest_sampled_at: datetime | None,
    current_time: datetime,
    collector: object,
) -> str:
    if telemetry_latest_sampled_at is None:
        return "critical"
    age_seconds = max(0, int((current_time - telemetry_latest_sampled_at).total_seconds()))
    max_age_seconds = int(getattr(collector, "max_age_seconds", 0) or 0)
    if max_age_seconds and age_seconds > max_age_seconds:
        return "warning"
    return "ok"


def _freshness_timer_reason(
    *,
    telemetry_latest_sampled_at: datetime | None,
    current_time: datetime,
) -> str:
    if telemetry_latest_sampled_at is None:
        return "свежих ETA-замеров за окно мониторинга пока нет"
    age_seconds = max(0, int((current_time - telemetry_latest_sampled_at).total_seconds()))
    return f"последний ETA-замер {_duration_text(age_seconds)} назад"


def _latest_window_sampled_at(
    forecast_windows: tuple[object, ...],
    fallback: datetime | None,
) -> datetime | None:
    timestamps = [
        getattr(window, "latest_sampled_at", None)
        for window in forecast_windows
        if getattr(window, "latest_sampled_at", None) is not None
    ]
    if fallback is not None:
        timestamps.append(fallback)
    return max(timestamps, default=None)


def _latest_forecast_readiness_at(
    forecast_readiness_by_profile: dict[str, ForecastReadinessSummary],
) -> datetime | None:
    timestamps = [
        summary.latest_sampled_at
        for summary in forecast_readiness_by_profile.values()
        if summary.latest_sampled_at is not None
    ]
    return max(timestamps, default=None)


def _duration_text(seconds: int) -> str:
    minutes, sec = divmod(max(0, seconds), 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    if days:
        return f"{days}д {hours}ч"
    if hours:
        return f"{hours}ч {minutes}м"
    if minutes:
        return f"{minutes}м"
    return f"{sec}с"


def _format_profile_support_snapshot(
    *,
    profile_key: str,
    window_key: str,
    hours: int,
    current_time: datetime,
    status: str,
    primary_action: str,
    primary_issue: dict[str, object] | None,
    snapshot_command: str,
    report_command: str,
    latest_reply_change: DepartureChange | None,
    items: list[dict[str, object]],
) -> str:
    view = _DashboardSupportSnapshotView(
        profile_key=profile_key,
        window_key=window_key,
        hours=hours,
        current_time=current_time,
        status=status,
        primary_action=primary_action,
        primary_issue=_dashboard_snapshot_item(primary_issue),
        latest_reply_change=latest_reply_change,
        snapshot_command=snapshot_command,
        report_command=report_command,
        items=tuple(item for item in (_dashboard_snapshot_item(item) for item in items) if item is not None),
    )
    return format_support_snapshot(view)


def _dashboard_snapshot_item(
    payload: dict[str, object] | None,
) -> _DashboardSupportSnapshotItem | None:
    if payload is None:
        return None
    return _DashboardSupportSnapshotItem(
        severity=str(payload.get("severity", "info")),
        key=str(payload.get("key", "")),
        message=str(payload.get("message", "")),
        action=str(payload.get("action", "")),
    )


def _profile_primary_issue(triage: dict[str, object]) -> dict[str, object] | None:
    primary_issue = triage.get("primary_issue")
    if isinstance(primary_issue, dict):
        return _profile_issue_payload(primary_issue)
    items = triage.get("items")
    if not isinstance(items, list):
        return None
    issue_items = [item for item in items if isinstance(item, dict)]
    if not issue_items:
        return None
    primary_action = str(triage.get("primary_action", ""))
    if not primary_action:
        return None
    primary = next((item for item in issue_items if item.get("action") == primary_action), None)
    if primary is None or str(primary.get("severity", "")) not in {
        "warning",
        "critical",
    }:
        return None
    return _profile_issue_payload(primary)


def _profile_issue_payload(primary: dict[str, object]) -> dict[str, object]:
    return {
        "severity": str(primary.get("severity", "info")),
        "key": str(primary.get("key", "")),
        "message": str(primary.get("message", "")),
        "action": str(primary.get("action", "")),
    }


def _issue_identity(item: dict[str, object]) -> tuple[str, str, str, str]:
    return (
        str(item.get("severity", "")),
        str(item.get("key", "")),
        str(item.get("message", "")),
        str(item.get("action", "")),
    )


def _profile_issue_count(triage: dict[str, object]) -> int:
    items = triage.get("items")
    if not isinstance(items, list):
        return 0
    return sum(
        1 for item in items if isinstance(item, dict) and str(item.get("severity", "")) in {"warning", "critical"}
    )


def _profile_forecast(window: object | None) -> dict[str, object]:
    if window is None:
        return {
            "status": "missing",
            "ready": False,
            "reason": "forecast window missing",
            "readiness_percent": 0,
            "ready_buckets": 0,
            "total_buckets": 0,
            "eta_coverage_percent": 0,
            "traffic_coverage_percent": 0,
            "forecast_without_report_samples": 0,
            "report_without_forecast_samples": 0,
            "integrity_gap_samples": 0,
            "missing_bucket_labels": [],
            "coverage_command": "",
            "latest_sampled_at": None,
        }
    return {
        "status": window.status,
        "ready": window.ready,
        "reason": window.reason,
        "readiness_percent": window.readiness_percent,
        "ready_buckets": window.ready_buckets,
        "total_buckets": window.total_buckets,
        "eta_coverage_percent": window.eta_coverage_percent,
        "traffic_coverage_percent": window.traffic_coverage_percent,
        "forecast_without_report_samples": window.forecast_without_report_samples,
        "report_without_forecast_samples": window.report_without_forecast_samples,
        "integrity_gap_samples": window.integrity_gap_samples,
        "missing_bucket_labels": list(window.missing_bucket_labels),
        "coverage_command": forecast_coverage_command_for_window(window.window_key),
        "latest_sampled_at": _dt(window.latest_sampled_at),
    }


def _profile_forecast_readiness(
    summary: ForecastReadinessSummary | None,
    profile_key: str,
) -> dict[str, object]:
    command = forecast_readiness_command_for_profile(profile_key)
    if summary is None:
        return {
            "status": "missing",
            "ready": False,
            "command": command,
            "selected_bucket_minutes": 0,
            "selected_sample_count": 0,
            "selected_distinct_days": 0,
            "min_samples": 0,
            "min_distinct_days": 0,
            "total_samples": 0,
            "eta_samples": 0,
            "fresh_eta_samples": 0,
            "traffic_samples": 0,
            "primary_samples": 0,
            "fallback_samples": 0,
            "primary_distinct_days": 0,
            "fallback_distinct_days": 0,
            "eta_coverage_percent": 0,
            "fresh_eta_coverage_percent": 0,
            "traffic_coverage_percent": 0,
            "latest_sampled_at": None,
        }
    return {
        "status": "ready" if summary.ready else "not_ready",
        "ready": summary.ready,
        "command": command,
        "selected_bucket_minutes": summary.selected_bucket_minutes,
        "selected_sample_count": summary.selected_sample_count,
        "selected_distinct_days": summary.selected_distinct_days,
        "min_samples": summary.min_samples,
        "min_distinct_days": summary.min_distinct_days,
        "total_samples": summary.total_samples,
        "eta_samples": summary.eta_samples,
        "fresh_eta_samples": summary.fresh_eta_samples,
        "traffic_samples": summary.traffic_samples,
        "primary_samples": summary.primary_samples,
        "fallback_samples": summary.fallback_samples,
        "primary_distinct_days": summary.primary_distinct_days,
        "fallback_distinct_days": summary.fallback_distinct_days,
        "eta_coverage_percent": summary.eta_coverage_percent,
        "fresh_eta_coverage_percent": summary.fresh_eta_coverage_percent,
        "traffic_coverage_percent": summary.traffic_coverage_percent,
        "latest_sampled_at": _dt(summary.latest_sampled_at),
    }


def _profile_forecast_backtest(
    summary: ForecastBacktestSummary | None,
    profile_key: str,
) -> dict[str, object]:
    command = _forecast_backtest_command(profile_key)
    if summary is None:
        return _empty_forecast_backtest_payload(command)
    result = selected_forecast_backtest_result(summary)
    base: dict[str, object] = {
        "status": "empty",
        "ready": False,
        "profile_key": summary.profile_key,
        "window_key": summary.report_window_key,
        "history_days": summary.history_days,
        "bucket_minutes": summary.bucket_minutes,
        "min_samples": summary.min_samples,
        "min_distinct_days": summary.min_distinct_days,
        "target_cases": summary.target_cases,
        "min_evaluated": DEFAULT_HISTORY_BACKTEST_MIN_EVALUATED,
        "warn_miss_rate_percent": DEFAULT_HISTORY_BACKTEST_WARN_MISS_RATE_PERCENT,
        "command": command,
    }
    best = best_forecast_backtest_result(summary)
    if result is None:
        return base
    status = _forecast_backtest_status(result)
    return {
        **base,
        "status": status,
        "ready": status == "ok",
        "percentile": result.percentile,
        "evaluated_cases": result.evaluated_cases,
        "skipped_cases": result.skipped_cases,
        "miss_cases": result.miss_cases,
        "miss_rate_percent": result.miss_rate_percent,
        "bucket_accuracy_percent": result.bucket_accuracy_percent,
        "miss_minutes": result.miss_minutes,
        "extra_wait_minutes": result.extra_wait_minutes,
        "mean_absolute_error": round(result.mean_absolute_error, 1),
        "best_percentile": None if best is None else best.percentile,
        "best_evaluated_cases": None if best is None else best.evaluated_cases,
        "best_miss_cases": None if best is None else best.miss_cases,
        "best_miss_rate_percent": None if best is None else best.miss_rate_percent,
        "best_bucket_accuracy_percent": None if best is None else best.bucket_accuracy_percent,
        "best_mean_absolute_error": None if best is None else round(best.mean_absolute_error, 1),
        "best_extra_wait_minutes": None if best is None else best.extra_wait_minutes,
    }


def _empty_forecast_backtest_payload(command: str) -> dict[str, object]:
    return {
        "status": "missing",
        "ready": False,
        "profile_key": "",
        "window_key": "",
        "history_days": 0,
        "bucket_minutes": 0,
        "min_samples": 0,
        "min_distinct_days": 0,
        "target_cases": 0,
        "min_evaluated": DEFAULT_HISTORY_BACKTEST_MIN_EVALUATED,
        "warn_miss_rate_percent": DEFAULT_HISTORY_BACKTEST_WARN_MISS_RATE_PERCENT,
        "command": command,
        "percentile": DEFAULT_HISTORY_PERCENTILE,
        "best_percentile": None,
        "best_evaluated_cases": None,
        "best_miss_cases": None,
        "best_miss_rate_percent": None,
        "best_bucket_accuracy_percent": None,
        "best_mean_absolute_error": None,
        "best_extra_wait_minutes": None,
        "evaluated_cases": 0,
        "skipped_cases": 0,
        "miss_cases": 0,
        "miss_rate_percent": 0,
        "bucket_accuracy_percent": 0,
        "miss_minutes": 0,
        "extra_wait_minutes": 0,
        "mean_absolute_error": 0.0,
    }


def _forecast_backtest_status(result: ForecastBacktestResult) -> str:
    if result.evaluated_cases < DEFAULT_HISTORY_BACKTEST_MIN_EVALUATED:
        return "insufficient"
    if result.miss_rate_percent >= DEFAULT_HISTORY_BACKTEST_WARN_MISS_RATE_PERCENT:
        return "warning"
    return "ok"


def _forecast_backtest_command(profile_key: str) -> str:
    try:
        return forecast_backtest_command_for_profile(profile_key)
    except ValueError:
        return ""


def _profile_runtime_quality(
    group: BotRuntimePredictionQualityGroup | None,
) -> dict[str, object]:
    if group is None:
        return {
            "status": "missing",
            "total": 0,
            "evaluated": 0,
            "pending": 0,
            "misses": 0,
            "guardrail_unavailable": 0,
            "evaluated_percent": 0,
            "pending_percent": 0,
            "miss_rate_percent": 0,
            "guardrail_unavailable_percent": 0,
            "p50_abs_error_minutes": None,
            "latest_sampled_at": None,
            "oldest_pending_sampled_at": None,
        }
    payload = _bot_prediction_quality_group(group)
    payload["status"] = _profile_runtime_quality_status(group)
    return payload


def _profile_runtime_quality_status(group: BotRuntimePredictionQualityGroup) -> str:
    if group.total == 0:
        return "missing"
    if group.evaluated >= 3 and (
        group.miss_rate_percent >= 80
        or (group.p50_abs_error_minutes is not None and group.p50_abs_error_minutes >= 8)
        or group.guardrail_unavailable >= 3
    ):
        return "critical"
    if group.pending or group.misses or group.guardrail_unavailable:
        return "warning"
    return "ok"


def _profile_runtime_calibration(
    group: BotRuntimeCalibrationGroup | None,
) -> dict[str, object]:
    if group is None:
        return {
            "status": "missing",
            "total": 0,
            "evaluated": 0,
            "misses": 0,
            "miss_rate_percent": 0,
            "p80_early_minutes": None,
            "p50_extra_wait_minutes": None,
            "suggested_buffer_minutes": 0,
            "action": "wait_for_more_evaluations",
        }
    return _bot_prediction_calibration_group(group)


def _profile_runtime_source_calibration(
    group: BotRuntimeCalibrationGroup | None,
    window_key: str,
) -> dict[str, object]:
    if group is None:
        payload = _profile_runtime_calibration(None)
        payload["key"] = ""
        payload["source_key"] = ""
        payload["command"] = ""
        return payload
    payload = _bot_prediction_calibration_group(group)
    _profile, source_key = _profile_source_key(group.key)
    payload["source_key"] = source_key
    payload["command"] = (
        prediction_calibration_command_for_window(window_key) if group.status in {"late_risk", "extra_wait"} else ""
    )
    return payload


def _source_calibration_by_profile(
    groups: tuple[BotRuntimeCalibrationGroup, ...],
) -> dict[str, BotRuntimeCalibrationGroup]:
    grouped: dict[str, list[BotRuntimeCalibrationGroup]] = defaultdict(list)
    for group in groups:
        profile_key, source_key = _profile_source_key(group.key)
        if not profile_key or not source_key:
            continue
        grouped[profile_key].append(group)
    return {
        profile_key: max(profile_groups, key=_source_calibration_priority)
        for profile_key, profile_groups in grouped.items()
    }


def _profile_source_key(value: object) -> tuple[str, str]:
    text = str(value)
    if "/" not in text:
        return text, ""
    profile, source = text.split("/", 1)
    return profile, source


def _source_calibration_priority(
    group: BotRuntimeCalibrationGroup,
) -> tuple[int, int, int, int]:
    status_rank = {
        "late_risk": 4,
        "extra_wait": 3,
        "insufficient": 2,
        "balanced": 1,
    }.get(group.status, 0)
    return (
        status_rank,
        group.suggested_buffer_minutes,
        group.miss_rate_percent,
        group.evaluated,
    )


def _profile_latency_summaries(
    connection: sqlite3.Connection,
    *,
    current_time: datetime,
) -> dict[str, BotLatencySummary]:
    summaries: dict[str, BotLatencySummary] = {}
    for window in REPORT_WINDOWS:
        try:
            summaries[window.profile_key] = summarize_bot_latency(
                connection,
                hours=24,
                current_time=current_time,
                profile_key=window.profile_key,
                event_kind=BOT_EVENT_USER_REPLY,
            )
        except sqlite3.OperationalError as exc:
            if _missing_profile_key_column(exc):
                return {}
            raise
    return summaries


def _profile_monitor_summaries(
    connection: sqlite3.Connection,
    *,
    db_path: Path,
    current_time: datetime,
) -> dict[str, MonitorSummary]:
    summaries: dict[str, MonitorSummary] = {}
    for window in REPORT_WINDOWS:
        try:
            summaries[window.profile_key] = summarize_monitor(
                connection,
                db_path=db_path,
                latency_hours=24,
                runtime_hours=24,
                profile_key=window.profile_key,
                current_time=current_time,
            )
        except sqlite3.OperationalError as exc:
            if _missing_profile_key_column(exc) or _missing_forecast_readiness_column(exc):
                return {}
            raise
    return summaries


def _profile_forecast_readiness_summaries(
    connection: sqlite3.Connection,
    *,
    current_time: datetime,
) -> dict[str, ForecastReadinessSummary]:
    summaries: dict[str, ForecastReadinessSummary] = {}
    for window in REPORT_WINDOWS:
        try:
            summaries[window.profile_key] = _profile_forecast_readiness_summary(
                connection,
                current_time=current_time,
                profile_key=window.profile_key,
                window_key=window.key,
            )
        except sqlite3.OperationalError as exc:
            if _missing_forecast_readiness_column(exc):
                return {}
            raise
    return summaries


def _profile_forecast_readiness_summary(
    connection: sqlite3.Connection,
    *,
    current_time: datetime,
    profile_key: str,
    window_key: str,
) -> ForecastReadinessSummary:
    _require_window(window_key)
    window = WINDOWS_BY_KEY[window_key]
    readiness_time = current_time.replace(
        hour=window.start.hour,
        minute=window.start.minute,
        second=0,
        microsecond=0,
    ) + timedelta(minutes=DEFAULT_PRIMARY_BUCKET_MINUTES)
    return summarize_yandex_forecast_readiness(
        connection,
        profile_key=profile_key,
        current_time=readiness_time,
        days=DEFAULT_HISTORY_DAYS,
        min_samples=DEFAULT_MIN_OBSERVATIONS,
        min_distinct_days=DEFAULT_MIN_HISTORY_DAYS,
        primary_bucket_minutes=DEFAULT_PRIMARY_BUCKET_MINUTES,
        fallback_bucket_minutes=DEFAULT_FALLBACK_BUCKET_MINUTES,
        max_age_seconds=DEFAULT_HISTORY_MAX_AGE_SECONDS,
        weekdays=WEEKDAYS,
        report_window_key=window.key,
    )


def _missing_profile_key_column(exc: sqlite3.OperationalError) -> bool:
    return "no such column: profile_key" in str(exc)


def _missing_forecast_readiness_column(exc: sqlite3.OperationalError) -> bool:
    text = str(exc)
    return "no such table: yandex_forecast_samples" in text or "no such column" in text


def _profile_bot_latency(summary: BotLatencySummary | None) -> dict[str, object]:
    if summary is None:
        return {
            "status": "missing",
            "command": "",
            "hours": 24,
            "profile_key": None,
            "event_kind": BOT_EVENT_USER_REPLY,
            "events": 0,
            "errors": 0,
            "error_rate_percent": 0,
            "no_eta": 0,
            "no_eta_rate_percent": 0,
            "invalid_duration_events": 0,
            "p50_total_ms": None,
            "p95_total_ms": None,
            "p95_forecast_ms": None,
            "p95_send_ms": None,
            "p95_followup_ms": None,
            "latest_received_at": None,
            "statuses": [],
            "event_kinds": [],
            "reply_sources": [],
            "error_categories": [],
            "top_error_category": None,
            "error_reasons": [],
            "no_eta_reasons": [],
            "top_no_eta_reason": None,
        }
    return {
        "status": _profile_bot_latency_status(summary),
        "command": bot_latency_command(
            hours=summary.hours,
            profile_key=summary.profile_key,
            event_kind=BOT_EVENT_USER_REPLY,
        ),
        "hours": summary.hours,
        "profile_key": summary.profile_key,
        "event_kind": summary.event_kind,
        "events": summary.total_events,
        "errors": summary.error_events,
        "error_rate_percent": summary.error_rate_percent,
        "no_eta": summary.no_eta_events,
        "no_eta_rate_percent": summary.no_eta_rate_percent,
        "no_eta_reasons": _counts(summary.no_eta_reasons),
        "top_no_eta_reason": _top_no_eta_reason(summary.no_eta_reasons),
        "invalid_duration_events": summary.invalid_duration_events,
        "p50_total_ms": summary.p50_total_ms,
        "p95_total_ms": summary.p95_total_ms,
        "p95_forecast_ms": summary.p95_forecast_ms,
        "p95_send_ms": summary.p95_send_ms,
        "p95_followup_ms": summary.p95_render_ms,
        "latest_received_at": _dt(summary.latest_received_at),
        "statuses": _counts(summary.statuses),
        "event_kinds": _counts(summary.event_kinds),
        "reply_sources": _counts(summary.reply_sources),
        "error_categories": _error_category_counts(summary.error_categories),
        "top_error_category": _top_error_category(summary.error_categories),
        "error_reasons": _counts(summary.error_reasons),
    }


def _profile_bot_latency_status(summary: BotLatencySummary) -> str:
    if summary.total_events == 0:
        return "missing"
    if summary.total_events < BOT_LATENCY_MIN_EVENTS:
        return "warning"
    if (
        summary.error_rate_percent >= 30
        or (summary.p95_total_ms is not None and summary.p95_total_ms >= 15_000)
        or (summary.no_eta_events >= 3 and summary.no_eta_rate_percent >= 80)
    ):
        return "critical"
    if (
        summary.error_events
        or (summary.p95_total_ms is not None and summary.p95_total_ms >= 5_000)
        or (summary.no_eta_events and summary.no_eta_rate_percent >= 50)
    ):
        return "warning"
    return "ok"


def _profile_watch_state(profile: object | None, summary: WatchStateSummary) -> dict[str, object]:
    if profile is None:
        return {
            "status": "critical" if summary.status == "critical" else "ok",
            "file_status": summary.file_status,
            "active_count": 0,
            "due_count": 0,
            "early_sent_count": 0,
            "oldest_age_minutes": None,
            "next_poll_at": None,
            "expires_at": None,
            "expires_in_minutes": None,
            "runtime_error_count": 0,
            "runtime_error_records": 0,
            "latest_error_at": None,
            "runtime_error_types": [],
        }
    runtime_error_count = int(getattr(profile, "runtime_error_count", 0))
    return {
        "status": "warning" if runtime_error_count else "ok",
        "file_status": summary.file_status,
        "active_count": getattr(profile, "active_count", 0),
        "due_count": getattr(profile, "due_count", 0),
        "early_sent_count": getattr(profile, "early_sent_count", 0),
        "oldest_age_minutes": getattr(profile, "oldest_age_minutes", None),
        "next_poll_at": _dt(getattr(profile, "next_poll_at", None)),
        "expires_at": _dt(getattr(profile, "expires_at", None)),
        "expires_in_minutes": getattr(profile, "expires_in_minutes", None),
        "runtime_error_count": runtime_error_count,
        "runtime_error_records": getattr(profile, "runtime_error_records", 0),
        "latest_error_at": _dt(getattr(profile, "latest_error_at", None)),
        "runtime_error_types": list(getattr(profile, "runtime_error_types", ())),
    }


def _window_health(window: object) -> dict[str, object]:
    return {
        "window_key": window.window_key,
        "profile_key": window.profile_key,
        "status": window.status,
        "ready": window.ready,
        "reason": window.reason,
        "samples": window.total_samples,
        "eta_samples": window.eta_samples,
        "fresh_eta_samples": window.fresh_eta_samples,
        "traffic_samples": window.traffic_samples,
        "eta_coverage_percent": window.eta_coverage_percent,
        "traffic_coverage_percent": window.traffic_coverage_percent,
        "readiness_percent": window.readiness_percent,
        "ready_buckets": window.ready_buckets,
        "total_buckets": window.total_buckets,
        "forecast_without_report_samples": window.forecast_without_report_samples,
        "report_without_forecast_samples": window.report_without_forecast_samples,
        "integrity_gap_samples": window.integrity_gap_samples,
        "missing_bucket_labels": list(window.missing_bucket_labels),
        "latest_sampled_at": _dt(window.latest_sampled_at),
        "latest_run_at": _dt(window.collector_latest_started_at),
        "collector_runs": window.collector_runs,
        "collector_eta_run_percent": window.collector_eta_run_percent,
        "collector_traffic_ok_run_percent": window.collector_traffic_ok_run_percent,
        "api_risk_samples": window.api_risk_samples,
        "api_risk_percent": window.api_risk_percent,
        "api_risk_reasons": _counts(window.api_risk_reasons),
        "coordinate_fallback_samples": window.coordinate_fallback_samples,
        "coordinate_fallback_percent": window.coordinate_fallback_percent,
        "coordinate_fallback_reasons": _counts(window.coordinate_fallback_reasons),
        "arrival_events": window.arrival_events,
        "prediction_events": window.prediction_events,
        "prediction_evaluations": window.prediction_evaluations,
        "prediction_miss_cases": window.prediction_miss_cases,
        "prediction_miss_rate_percent": window.prediction_miss_rate_percent,
        "bot_prediction_events": window.bot_prediction_events,
        "bot_prediction_evaluations": window.bot_prediction_evaluations,
        "bot_prediction_miss_cases": window.bot_prediction_miss_cases,
        "bot_prediction_miss_rate_percent": window.bot_prediction_miss_rate_percent,
        "truth_status": window.truth_status,
        "truth_reason": window.truth_reason,
        "latest_arrival_at": _dt(window.latest_arrival_at),
    }


def _series_row(day: str, rows: list[sqlite3.Row]) -> dict[str, object]:
    etas = [minutes[0] for row in rows if (minutes := arrival_minutes_from_json(row["arrival_minutes_json"]))]
    delays = [_non_negative_int(value) for row in rows if (value := row["traffic_delay_seconds"]) is not None]
    delays = [value for value in delays if value is not None]
    jams = [_non_negative_int(value) for row in rows if (value := row["traffic_jams_level"]) is not None]
    jams = [value for value in jams if value is not None]
    traffic_samples = sum(1 for row in rows if str(row["traffic_status"]) == "ok")
    return {
        "date": day,
        "samples": len(rows),
        "eta_samples": len(etas),
        "traffic_samples": traffic_samples,
        "eta_coverage_percent": _percent(len(etas), len(rows)),
        "traffic_coverage_percent": _percent(traffic_samples, len(rows)),
        "p80_eta_minutes": _p80(etas) if len(etas) >= MIN_SERIES_SAMPLES_FOR_STATS else None,
        "avg_eta_minutes": _avg(etas) if len(etas) >= MIN_SERIES_SAMPLES_FOR_STATS else None,
        "avg_traffic_delay_minutes": _avg_minutes_from_seconds(delays),
        "max_traffic_delay_minutes": round(max(delays) / 60) if delays else None,
        "avg_jams_level": _avg(jams),
    }


def _recent_row(row: sqlite3.Row) -> dict[str, object]:
    traffic_delay_seconds = _non_negative_int(row["traffic_delay_seconds"])
    raw = _json_object(str(row["raw_json"]))
    forecast = raw.get("forecast") if isinstance(raw, dict) else {}
    forecast_raw = forecast if isinstance(forecast, dict) else {}
    return {
        "sampled_at": row["sampled_at"],
        "window_key": row["report_window_key"],
        "profile_key": row["profile_key"],
        "source_method": row["source_method"],
        "source_status": row["source_status"],
        "source_reason": sanitize_diagnostic_text(
            str(forecast_raw.get("fallback_reason") or ""),
            fallback="",
            limit=200,
        ),
        "source_raw_status": sanitize_diagnostic_text(
            str(forecast_raw.get("raw_status") or ""),
            fallback="",
            limit=120,
        ),
        "arrival_minutes": list(arrival_minutes_from_json(row["arrival_minutes_json"])),
        "traffic_status": row["traffic_status"],
        "traffic_delay_minutes": round(traffic_delay_seconds / 60) if traffic_delay_seconds is not None else None,
        "traffic_jams_level": _non_negative_int(row["traffic_jams_level"]),
    }


def _json_object(raw_json: str) -> dict[str, object]:
    try:
        raw = json.loads(raw_json)
    except json.JSONDecodeError:
        return {}
    if not isinstance(raw, dict):
        return {}
    return raw


def _require_window(window_key: str) -> None:
    if window_key not in WINDOWS_BY_KEY:
        raise KeyError(window_key)


def _counts(items: object) -> list[dict[str, object]]:
    return [{"key": item.key, "count": item.count} for item in items]


def _error_category_counts(items: object) -> list[dict[str, object]]:
    return [
        {
            "key": item.key,
            "count": item.count,
            "label": bot_error_category_text(str(item.key)),
        }
        for item in items
    ]


def _top_error_category(items: object) -> dict[str, object] | None:
    if not isinstance(items, tuple) or not items:
        return None
    top = items[0]
    key = str(getattr(top, "key", "") or "")
    count = getattr(top, "count", 0)
    if not key or not isinstance(count, int) or isinstance(count, bool) or count < 0:
        return None
    return {"key": key, "count": count, "label": bot_error_category_text(key)}


def _top_no_eta_reason(items: object) -> dict[str, object] | None:
    if not isinstance(items, tuple) or not items:
        return None
    top = items[0]
    key = str(getattr(top, "key", "") or "")
    count = getattr(top, "count", 0)
    if not key or not isinstance(count, int) or isinstance(count, bool) or count < 0:
        return None
    return {"key": key, "count": count, "label": no_eta_reason_text(key)}


def _dt(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _percent(numerator: int, denominator: int) -> int:
    return 0 if denominator <= 0 else round(numerator * 100 / denominator)


def _avg(values: list[int]) -> int | None:
    return round(sum(values) / len(values)) if values else None


def _avg_minutes_from_seconds(seconds: list[int]) -> int | None:
    if not seconds:
        return None
    return round(sum(seconds) / len(seconds) / 60)


def _p80(values: list[int]) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(len(ordered) * 0.8) - 1)
    return ordered[index]


def _non_negative_int(value: object) -> int | None:
    parsed = optional_int_value(value)
    if parsed is None or parsed < 0:
        return None
    return parsed
