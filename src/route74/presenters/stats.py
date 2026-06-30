from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol

from route74.build_info import format_build_status
from route74.diagnostics import sanitize_diagnostic_text
from route74.domain.commute import DepartureDecision
from route74.domain.eta import EtaConfidence, EtaSource
from route74.domain.yandex_history import DEFAULT_HISTORY_PERCENTILE
from route74.presenters.commute_lines import (
    current_time_line,
    direction_line,
    format_duration_minutes,
)
from route74.presenters.bot_errors import bot_error_top_category_text
from route74.presenters.eta_factors import eta_factors_line
from route74.presenters.eta_explanations import eta_explanation_line
from route74.presenters.history_status import history_scope_text, unavailable_history_status_text
from route74.presenters.no_eta_reason import no_eta_top_reason_text
from route74.presenters.runtime import format_runtime_source_calibration_line, profile_source_group
from route74.presenters.yandex_status import (
    yandex_issue_text,
    yandex_method_text,
    yandex_status_text,
    yandex_status_summary,
)
from route74.sources.yandex.freshness import effective_forecast_age_seconds
from route74.support_actions import prediction_calibration_command_for_profile


STALE_PENDING_MINUTES = 120
NO_ETA_MIN_EVENTS = 3
NO_ETA_WARN_PERCENT = 50
BOT_ERROR_WARN_PERCENT = 10
BOT_P95_WARN_MS = 5_000
BOT_LATENCY_MIN_EVENTS = 3


class StatsSnapshotView(Protocol):
    decision: DepartureDecision
    telemetry: Any | None
    forecast_health: Any | None
    forecast_readiness: Any | None
    forecast_backtest: Any | None
    bot_latency: Any | None
    runtime_quality: Any | None
    runtime_calibration: Any | None
    watch_state: Any | None
    build_info: Any | None
    telemetry_error: str
    forecast_health_error: str
    forecast_readiness_error: str
    forecast_backtest_error: str
    bot_latency_error: str
    runtime_error: str
    watch_state_error: str
    forecast_readiness_command: str
    forecast_coverage_command: str
    forecast_backtest_command: str
    bot_latency_command: str
    support_report_command: str
    prediction_calibration_command: str
    prediction_evaluate_command: str
    watch_state_command: str
    triage_action_command: str
    triage_action_key: str
    triage_action_message: str


def format_stats_message(snapshot: StatsSnapshotView) -> str:
    decision = snapshot.decision
    lines = [
        "📊 Статистика 74",
        _version_line(snapshot),
        current_time_line(decision),
        direction_line(decision),
        _consensus_line(snapshot),
        eta_explanation_line(decision.eta_consensus.explanations),
        _signals_line(snapshot),
        eta_factors_line(decision.eta_consensus.factors),
        _yandex_line(snapshot),
        _history_line(snapshot),
        _forecast_health_line(snapshot),
        _forecast_readiness_line(snapshot),
        _forecast_backtest_line(snapshot),
        _telemetry_line(snapshot),
        _heartbeat_line(snapshot),
        _bot_latency_line(snapshot),
        _runtime_quality_line(snapshot),
        _runtime_pending_line(snapshot),
        _runtime_calibration_line(snapshot),
        _runtime_source_calibration_line(snapshot),
        _runtime_source_calibration_action_line(snapshot),
        _watch_state_line(snapshot),
        _support_report_line(snapshot),
    ]
    warning = decision.eta_consensus.warning
    if warning:
        lines.append(f"⚠️ {warning}")
    return "\n".join(line for line in lines if line)


def _consensus_line(snapshot: StatsSnapshotView) -> str:
    consensus = snapshot.decision.eta_consensus
    if consensus.arrival_minutes is None:
        return "🧭 Оценка: нет точного ETA"
    source = _selected_source_text(consensus.selected_source)
    return (
        f"🧭 Оценка: через {format_duration_minutes(consensus.arrival_minutes)} · "
        f"источник {source} · "
        f"доверие {_confidence(consensus.confidence)} · "
        f"цель ждать {format_duration_minutes(consensus.target_wait_minutes)}"
    )


def _signals_line(snapshot: StatsSnapshotView) -> str:
    consensus = snapshot.decision.eta_consensus
    if not consensus.estimates:
        return ""
    selected_source = consensus.selected_source
    signals = ", ".join(
        f"{'✓ ' if estimate.source == selected_source else ''}{_eta_source_text(estimate.source)} "
        f"{format_duration_minutes(estimate.arrival_minutes)}"
        for estimate in consensus.estimates
    )
    if consensus.spread_minutes is not None:
        return f"🧪 Сигналы: {signals} · разброс {format_duration_minutes(consensus.spread_minutes)}"
    return f"🧪 Сигналы: {signals}"


def _version_line(snapshot: StatsSnapshotView) -> str:
    info = snapshot.build_info
    if info is None:
        return ""
    return f"🧩 Версия: {info.label} · {format_build_status(info)}"


def _yandex_line(snapshot: StatsSnapshotView) -> str:
    forecast = snapshot.decision.yandex_forecast
    if not forecast.enabled:
        return "🟡 Яндекс: выключен"
    if forecast.available:
        if not forecast.arrival_minutes:
            detail = yandex_issue_text(forecast.status, forecast.fallback_reason, fallback="ETA отсутствует")
            return f"🟡 Яндекс: {yandex_status_text(forecast.status)} · {detail}"
        eta = format_duration_minutes(forecast.arrival_minutes[0])
        age = _age(effective_forecast_age_seconds(forecast))
        status = yandex_status_summary(forecast.status, forecast.fallback_reason)
        return (
            f"🟡 Яндекс: через {eta} · {status} · {yandex_method_text(forecast.source_method)} · "
            f"машин {forecast.vehicle_count} · свежесть {age}"
        )
    detail = yandex_issue_text(forecast.status, forecast.fallback_reason, fallback="данных для ETA нет")
    return f"🟡 Яндекс: {yandex_status_text(forecast.status)} · {detail}"


def _history_line(snapshot: StatsSnapshotView) -> str:
    history = snapshot.decision.yandex_history
    scope = history_scope_text(history)
    if history.available and history.arrival_minutes is not None:
        eta = format_duration_minutes(history.arrival_minutes)
        bucket = format_duration_minutes(history.bucket_minutes)
        return (
            f"📈 История Яндекса: p{history.percentile} через {eta} · "
            f"n={history.sample_count} · окно ±{bucket} · {scope}"
        )
    return f"📈 История Яндекса: {unavailable_history_status_text(history)}"


def _forecast_health_line(snapshot: StatsSnapshotView) -> str:
    summary = snapshot.forecast_health
    if summary is None:
        if snapshot.forecast_health_error:
            reason = _diagnostic_text(snapshot.forecast_health_error, fallback="ошибка без деталей")
            return f"🩺 Прогноз: недоступен · {reason}"
        return ""
    parts = [f"🩺 Прогноз: {_forecast_ready_text(summary)}"]
    windows = getattr(summary, "windows", ())
    window = _forecast_window_for_profile(windows, snapshot.decision.profile.key)
    if window is None:
        window = _forecast_first_window(windows)
    window_text = _forecast_window_text(window)
    if window_text:
        parts.append(window_text)
    collector_text = _forecast_collector_text(getattr(summary, "collector", None))
    if collector_text:
        parts.append(collector_text)
    canary_text = _forecast_canary_text(getattr(summary, "canary", None))
    if canary_text:
        parts.append(canary_text)
    return " · ".join(parts)


def _forecast_readiness_line(snapshot: StatsSnapshotView) -> str:
    readiness = snapshot.forecast_readiness
    if readiness is None:
        if snapshot.forecast_readiness_error:
            reason = _diagnostic_text(snapshot.forecast_readiness_error, fallback="ошибка без деталей")
            return f"📚 История: недоступна · {reason}"
        return ""
    state = "готова" if readiness.ready else _forecast_readiness_not_ready_text(readiness)
    window = _forecast_window_for_profile(
        getattr(snapshot.forecast_health, "windows", ()), snapshot.decision.profile.key
    )
    line = (
        f"📚 История: {state} · ±{readiness.selected_bucket_minutes}м · "
        f"samples {readiness.selected_sample_count}/{readiness.min_samples} · "
        f"days {readiness.selected_distinct_days}/{readiness.min_distinct_days}"
    )
    if not readiness.ready:
        missing = _forecast_missing_buckets(window)
        if missing:
            line += f" · не хватает {missing}"
        command, _ = _forecast_readiness_action(snapshot, window)
        if command:
            line += f" · {command}"
    return line


def _forecast_backtest_line(snapshot: StatsSnapshotView) -> str:
    backtest = snapshot.forecast_backtest
    command = _diagnostic_text(snapshot.forecast_backtest_command, fallback="", limit=160)
    suffix = f" · {command}" if command else ""
    if backtest is None:
        if snapshot.forecast_backtest_error:
            reason = _diagnostic_text(snapshot.forecast_backtest_error, fallback="ошибка без деталей")
            return f"🧪 Качество истории: недоступно · {reason}{suffix}"
        return ""
    result = _forecast_backtest_result(backtest)
    best = _forecast_backtest_best_result(backtest)
    target_cases = getattr(backtest, "target_cases", 0)
    if result is None or getattr(result, "evaluated_cases", 0) <= 0:
        return f"🧪 Качество истории: данных мало · кейсы {target_cases}{suffix}"
    evaluated = getattr(result, "evaluated_cases", 0)
    miss_cases = getattr(result, "miss_cases", 0)
    miss_rate = getattr(result, "miss_rate_percent", 0)
    accuracy = getattr(result, "bucket_accuracy_percent", 0)
    mae = getattr(result, "mean_absolute_error", 0.0)
    best_suffix = _forecast_backtest_best_suffix(result, best)
    return (
        f"🧪 Качество истории: p{getattr(result, 'percentile', DEFAULT_HISTORY_PERCENTILE)} · "
        f"проверено {evaluated}/{target_cases} · "
        f"промахов {miss_cases} ({miss_rate}%) · "
        f"точность {accuracy}% · ср. ошибка {mae:.1f}м"
        f"{best_suffix}"
        f"{suffix}"
    )


def _forecast_readiness_not_ready_text(readiness: object) -> str:
    selected_samples = _non_negative_count(getattr(readiness, "selected_sample_count", 0))
    min_samples = _non_negative_count(getattr(readiness, "min_samples", 0))
    selected_days = _non_negative_count(getattr(readiness, "selected_distinct_days", 0))
    min_days = _non_negative_count(getattr(readiness, "min_distinct_days", 0))
    if selected_samples < min_samples or selected_days < min_days:
        return "данных мало"
    return "не готова"


def _forecast_backtest_result(backtest: object) -> Any | None:
    selected = getattr(backtest, "selected_result", None)
    if selected is not None:
        return selected
    try:
        results = tuple(getattr(backtest, "results", ()))
    except TypeError:
        return None
    if not results:
        return None
    selected = next(
        (result for result in results if getattr(result, "percentile", None) == DEFAULT_HISTORY_PERCENTILE),
        None,
    )
    return selected or results[0]


def _forecast_backtest_best_result(backtest: object) -> Any | None:
    best = getattr(backtest, "best_result", None)
    if best is not None:
        return best
    try:
        results = tuple(getattr(backtest, "results", ()))
    except TypeError:
        return None
    if not results:
        return None
    best = min(
        results,
        key=lambda result: (
            getattr(result, "miss_rate_percent", 0),
            getattr(result, "miss_minutes", 0),
            getattr(result, "mean_absolute_error", 0.0),
            getattr(result, "extra_wait_minutes", 0),
            0 if getattr(result, "percentile", 0) == DEFAULT_HISTORY_PERCENTILE else 1,
            getattr(result, "percentile", 0),
        ),
    )
    return best


def _forecast_backtest_best_suffix(selected: object, best: object | None) -> str:
    if best is None:
        return ""
    selected_percentile = getattr(selected, "percentile", None)
    best_percentile = getattr(best, "percentile", None)
    if best_percentile is None or best_percentile == selected_percentile:
        return ""
    best_miss_rate = getattr(best, "miss_rate_percent", 0)
    best_miss_cases = getattr(best, "miss_cases", 0)
    best_evaluated = getattr(best, "evaluated_cases", 0)
    best_mae = getattr(best, "mean_absolute_error", 0.0)
    return (
        f" · лучший p{best_percentile} · промахов {best_miss_cases}/{best_evaluated}"
        f"({best_miss_rate}%) · ср. ошибка {best_mae:.1f}м"
    )


def _forecast_window_for_profile(windows: object, profile_key: str) -> Any | None:
    try:
        return next((window for window in windows if getattr(window, "profile_key", "") == profile_key), None)
    except TypeError:
        return None


def _forecast_first_window(windows: object) -> Any | None:
    try:
        return next(iter(windows))
    except (StopIteration, TypeError):
        return None


def _forecast_ready_text(summary: object) -> str:
    ready_windows = getattr(summary, "ready_windows", 0)
    total_windows = getattr(summary, "total_windows", 0)
    return f"{ready_windows}/{total_windows} окон готовы"


def _forecast_window_text(window: object | None) -> str:
    if window is None:
        return ""
    window_key = _diagnostic_text(getattr(window, "window_key", ""), fallback="", limit=80)
    status = _diagnostic_text(getattr(window, "status", ""), fallback="", limit=40)
    base = " ".join(part for part in (window_key, status) if part)
    detail = _forecast_window_detail(window, status)
    if base and detail:
        return f"{base} · {detail}"
    if detail:
        return detail
    return base


def _forecast_window_detail(window: object, status: str) -> str:
    if status == "integrity_gap":
        forecast_only = _non_negative_count(getattr(window, "forecast_without_report_samples", 0))
        report_only = _non_negative_count(getattr(window, "report_without_forecast_samples", 0))
        return f"forecast_only={forecast_only} report_only={report_only}"
    if status == "insufficient_bucket_coverage":
        missing = _forecast_missing_buckets(window)
        if missing:
            return f"не хватает {missing}"
    return ""


def _non_negative_count(value: object) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return max(0, value)
    return 0


def _forecast_missing_buckets(window: object | None) -> str:
    if window is None:
        return ""
    labels = getattr(window, "missing_bucket_labels", ())
    if not labels:
        return ""
    try:
        items = tuple(_diagnostic_text(label, fallback="", limit=16) for label in labels)
    except TypeError:
        return ""
    visible = tuple(item for item in items if item)
    if not visible:
        return ""
    if len(visible) > 4:
        return ", ".join(visible[:4]) + f",+{len(visible) - 4}"
    return ", ".join(visible)


def _forecast_collector_text(collector: object | None) -> str:
    if collector is None:
        return ""
    status = _diagnostic_text(getattr(collector, "status", ""), fallback="", limit=40)
    if not status:
        return ""
    if getattr(collector, "healthy", False):
        return "collector ok"
    return f"collector {status}"


def _forecast_canary_text(canary: object | None) -> str:
    if canary is None:
        return ""
    status = _diagnostic_text(getattr(canary, "status", ""), fallback="", limit=40)
    if not status:
        return ""
    if getattr(canary, "healthy", False):
        return "canary ok"
    return f"canary {status}"


def _telemetry_line(snapshot: StatsSnapshotView) -> str:
    telemetry = snapshot.telemetry
    if telemetry is None:
        if snapshot.telemetry_error:
            reason = _diagnostic_text(snapshot.telemetry_error, fallback="ошибка без деталей")
            return f"🗄️ Сбор: недоступен · {reason}"
        return ""
    return (
        f"🗄️ Сбор {telemetry.hours}ч: snapshots {telemetry.total_snapshots} · "
        f"ETA {telemetry.eta_coverage_percent}% · машины {telemetry.vehicle_coverage_percent}% · "
        f"наблюдений {telemetry.total_observations}"
    )


def _heartbeat_line(snapshot: StatsSnapshotView) -> str:
    telemetry = snapshot.telemetry
    if telemetry is None or telemetry.heartbeat is None:
        if snapshot.telemetry_error:
            return ""
        return "💓 Collector: heartbeat пока нет"
    age_label = _heartbeat_age(snapshot.decision.current_time, telemetry.heartbeat.updated_at)
    status = _diagnostic_text(telemetry.heartbeat.last_status, fallback="status unknown", limit=40)
    return f"💓 Collector: {age_label} · {status}"


def _bot_latency_line(snapshot: StatsSnapshotView) -> str:
    latency = snapshot.bot_latency
    if latency is None:
        if snapshot.bot_latency_error:
            reason = _diagnostic_text(snapshot.bot_latency_error, fallback="ошибка без деталей")
            return f"🔔 Уведомления: недоступны · {reason}"
        return ""
    if not getattr(latency, "total_events", 0):
        freshness = _bot_latency_freshness(
            snapshot.decision.current_time,
            getattr(latency, "latest_received_at", None),
        )
        suffix = f" · свежесть {freshness}" if freshness else ""
        return f"🔔 Runtime {latency.hours}ч: ответов пока нет{suffix}"
    top_error = _bot_latency_top_error_reason(latency)
    no_eta = ""
    if getattr(latency, "no_eta_events", 0):
        reason = no_eta_top_reason_text(getattr(latency, "no_eta_reasons", ()))
        reason_suffix = f" · чаще всего: {reason}" if reason else ""
        no_eta = f" · без ETA {latency.no_eta_events} ({latency.no_eta_rate_percent}%){reason_suffix}"
    invalid = ""
    if getattr(latency, "invalid_duration_events", 0):
        invalid = f" · плохих замеров {latency.invalid_duration_events}"
    freshness = _bot_latency_freshness(
        snapshot.decision.current_time,
        getattr(latency, "latest_received_at", None),
    )
    freshness_text = f" · свежесть {freshness}" if freshness else ""
    timing_split = _bot_latency_timing_split(latency)
    sample_note = _bot_latency_sample_note(latency)
    return (
        f"🔔 Runtime {latency.hours}ч: ответов {latency.total_events} · "
        f"ошибки {latency.error_events} ({latency.error_rate_percent}%)"
        f"{top_error}"
        f"{no_eta} · p95 {_milliseconds(latency.p95_total_ms)}{timing_split}"
        f"{invalid}{sample_note}{freshness_text}"
    )


def _bot_latency_sample_note(latency: object) -> str:
    total_events = _non_negative_count(getattr(latency, "total_events", 0))
    if 0 < total_events < BOT_LATENCY_MIN_EVENTS:
        return f" · мало данных для p95 ({total_events}/{BOT_LATENCY_MIN_EVENTS})"
    return ""


def _bot_latency_timing_split(latency: object) -> str:
    parts: list[str] = []
    send_ms = getattr(latency, "p95_send_ms", None)
    if isinstance(send_ms, int) and not isinstance(send_ms, bool):
        parts.append(f"отправка {_milliseconds(send_ms)}")
    followup_ms = getattr(latency, "p95_render_ms", None)
    if isinstance(followup_ms, int) and not isinstance(followup_ms, bool) and followup_ms > 0:
        parts.append(f"доп. сообщения {_milliseconds(followup_ms)}")
    return f" · {' · '.join(parts)}" if parts else ""


def _runtime_quality_line(snapshot: StatsSnapshotView) -> str:
    quality = snapshot.runtime_quality
    if quality is None:
        if snapshot.runtime_error:
            reason = _diagnostic_text(snapshot.runtime_error, fallback="ошибка без деталей")
            return f"🌐 Runtime: недоступен · {reason}"
        return ""
    group = _profile_group(quality.by_profile, snapshot.decision.profile.key)
    if group is None:
        return f"🌐 Runtime {quality.hours}ч: по этому направлению фактов пока нет"
    guardrail = ""
    if group.guardrail_unavailable:
        guardrail = f" · ETA-защита недоступна {group.guardrail_unavailable}"
    return (
        f"🌐 Runtime {quality.hours}ч: прогнозов {group.total} · "
        f"проверено {group.evaluated} ({group.evaluated_percent}%) · "
        f"ждёт {group.pending} ({group.pending_percent}%) · "
        f"промахи {group.misses} ({group.miss_rate_percent}%) · "
        f"p50 ошибка {_runtime_minutes(group.p50_abs_error_minutes)}"
        f"{guardrail}"
    )


def _runtime_pending_line(snapshot: StatsSnapshotView) -> str:
    quality = snapshot.runtime_quality
    if quality is None:
        return ""
    group = _profile_group(quality.by_profile, snapshot.decision.profile.key)
    if group is None or group.pending <= 0:
        return ""
    command = _diagnostic_text(snapshot.prediction_evaluate_command, fallback="")
    age = _pending_age(snapshot.decision.current_time, group.oldest_pending_sampled_at)
    summary = f"ждёт факта {group.pending}/{group.total}"
    if age:
        summary += f", старое {age}"
    if command:
        summary += f" · {command}"
    return f"🧪 Проверка фактов: {summary}"


def _runtime_calibration_line(snapshot: StatsSnapshotView) -> str:
    calibration = snapshot.runtime_calibration
    if calibration is None:
        return ""
    group = _profile_group(calibration.by_profile, snapshot.decision.profile.key)
    if group is None:
        return "🛠️ Запас: жду проверенные ответы по этому направлению"
    if group.status == "late_risk":
        return f"🛠️ Запас: проверь +{group.suggested_buffer_minutes} мин для этого направления"
    if group.status == "extra_wait":
        return "🛠️ Запас: часто приходишь рано, пока только наблюдаю"
    if group.status == "balanced":
        return "🛠️ Запас: текущий запас выглядит ровно"
    return f"🛠️ Запас: нужно больше проверенных ответов ({group.evaluated}/{group.total})"


def _runtime_source_calibration_line(snapshot: StatsSnapshotView) -> str:
    return format_runtime_source_calibration_line(
        snapshot.runtime_calibration,
        snapshot.decision.profile.key,
    )


def _runtime_source_calibration_action_line(snapshot: StatsSnapshotView) -> str:
    calibration = snapshot.runtime_calibration
    if calibration is None:
        return ""
    group = profile_source_group(calibration.by_profile_source, snapshot.decision.profile.key)
    if group is None or getattr(group, "status", "") not in {"late_risk", "extra_wait"}:
        return ""
    command = _diagnostic_text(getattr(snapshot, "prediction_calibration_command", ""), fallback="", limit=160)
    if not command:
        return ""
    return f"🧪 Калибровка source: {command}"


def _support_report_line(snapshot: StatsSnapshotView) -> str:
    command, reason = _support_action(snapshot)
    command = _diagnostic_text(command, fallback="", limit=160)
    if not command:
        return ""
    reason = _diagnostic_text(reason, fallback="", limit=80)
    suffix = f" · {reason}" if reason else ""
    return f"🧰 Разбор: {command}{suffix}"


def _support_action(snapshot: StatsSnapshotView) -> tuple[str, str]:
    if snapshot.watch_state_error:
        return snapshot.watch_state_command, "проверить watch-state"
    watch_action = _watch_support_action(snapshot)
    if watch_action[0]:
        return watch_action
    triage_action = _triage_support_action(snapshot)
    if triage_action[0] and triage_action[0] == snapshot.bot_latency_command:
        return triage_action
    latency_action = _bot_latency_support_action(snapshot)
    if latency_action[0]:
        return latency_action
    runtime_action = _runtime_support_action(snapshot)
    if runtime_action[0]:
        return runtime_action
    forecast_readiness_action = _forecast_readiness_support_action(snapshot)
    if forecast_readiness_action[0]:
        return forecast_readiness_action
    if triage_action[0]:
        return triage_action
    if snapshot.telemetry_error:
        return snapshot.support_report_command, "сводка сбора недоступна"
    if snapshot.bot_latency_error:
        return snapshot.bot_latency_command, "runtime-метрики недоступны"
    if snapshot.runtime_error:
        return snapshot.support_report_command, "runtime недоступен"
    return snapshot.support_report_command, ""


def _triage_support_action(snapshot: StatsSnapshotView) -> tuple[str, str]:
    command = getattr(snapshot, "triage_action_command", "")
    if not command:
        return "", ""
    key = getattr(snapshot, "triage_action_key", "")
    message = getattr(snapshot, "triage_action_message", "")
    return command, _triage_reason(key, message)


def _triage_reason(key: object, message: object) -> str:
    key_text = str(key)
    reasons = {
        "db_integrity": "проверить SQLite",
        "collector": "проверить сборщик",
        "yandex_canary": "проверить Яндекс",
        "yandex_api_risk": "проверить контракт Яндекса",
        "integrity_gap": "проверить расхождение витрин",
        "history_readiness": "проверить готовность истории",
        "history_backtest": "проверить качество истории",
        "forecast_window": "проверить историю Яндекса",
        "forecast_window_missing": "проверить окно прогноза",
        "truth_window": "проверить факты прогноза",
        "watch_state_file": "проверить watch-state",
        "watch_state_overdue": "проверка watch просрочена",
        "watch_state_runtime_error": "ошибки watch-проверок",
        "watch_state_invalid": "повреждён watch-state",
        "bot_no_eta_replies": "много ответов без ETA",
        "bot_latency_errors": "ошибки runtime-ответов",
        "bot_latency_malformed": "плохие latency-замеры",
        "bot_latency_p95": "медленные runtime-ответы",
        "bot_latency_stale": "нет свежих runtime-ответов",
        "bot_runtime_guardrail_unavailable": "проверить ETA-защиту",
        "bot_runtime_pending": "проверить факты прибытия",
        "bot_runtime_misses": "разобрать промахи",
        "bot_runtime_late_risk": "проверить запас",
        "bot_runtime_source_late_risk": "проверить источник",
        "bot_runtime_p50_error": "разобрать ошибку ETA",
    }
    if key_text in reasons:
        reason = reasons[key_text]
        if key_text in {"bot_latency_errors", "bot_no_eta_replies", "integrity_gap"}:
            detail = _diagnostic_text(message, fallback="", limit=80)
            return f"{reason} · {detail}" if detail else reason
        return reason
    return _diagnostic_text(message, fallback="", limit=80)


def _watch_support_action(snapshot: StatsSnapshotView) -> tuple[str, str]:
    command = snapshot.watch_state_command
    if snapshot.watch_state_error:
        return command, "проверить watch-state"
    summary = snapshot.watch_state
    if summary is None:
        return "", ""
    if getattr(summary, "status", "") == "critical":
        return command, "проверить watch-state"
    if getattr(summary, "overdue_count", 0):
        return command, "проверка watch просрочена"
    profile = _watch_profile(getattr(summary, "profiles", ()), snapshot.decision.profile.key)
    target = profile if profile is not None and getattr(profile, "runtime_error_count", 0) else summary
    if getattr(target, "runtime_error_count", 0):
        return command, "ошибки watch-проверок"
    if getattr(summary, "invalid_records", 0):
        return command, "повреждён watch-state"
    return "", ""


def _bot_latency_support_action(snapshot: StatsSnapshotView) -> tuple[str, str]:
    latency = snapshot.bot_latency
    command = snapshot.bot_latency_command
    if latency is None:
        return ("", "") if not snapshot.bot_latency_error else (command, "runtime-метрики недоступны")
    if getattr(latency, "error_rate_percent", 0) >= BOT_ERROR_WARN_PERCENT:
        return command, "ошибки runtime-ответов"
    if (
        getattr(latency, "no_eta_events", 0) >= NO_ETA_MIN_EVENTS
        and getattr(latency, "no_eta_rate_percent", 0) >= NO_ETA_WARN_PERCENT
    ):
        return command, "много ответов без ETA"
    p95 = getattr(latency, "p95_total_ms", None)
    total_events = _non_negative_count(getattr(latency, "total_events", 0))
    if (
        total_events >= BOT_LATENCY_MIN_EVENTS
        and isinstance(p95, int)
        and not isinstance(p95, bool)
        and p95 >= BOT_P95_WARN_MS
    ):
        return command, "медленные runtime-ответы"
    if getattr(latency, "invalid_duration_events", 0):
        return command, "плохие latency-замеры"
    return "", ""


def _runtime_support_action(snapshot: StatsSnapshotView) -> tuple[str, str]:
    quality = snapshot.runtime_quality
    calibration = snapshot.runtime_calibration
    command = snapshot.support_report_command
    if quality is None:
        return ("", "") if not snapshot.runtime_error else (command, "runtime недоступен")
    group = _profile_group(quality.by_profile, snapshot.decision.profile.key)
    if group is None:
        return "", ""
    if getattr(group, "guardrail_unavailable", 0):
        return command, "проверить ETA-защиту"
    pending_minutes = _pending_age_minutes(
        snapshot.decision.current_time,
        getattr(group, "oldest_pending_sampled_at", None),
    )
    if getattr(group, "pending", 0) > 0 and pending_minutes is not None and pending_minutes >= STALE_PENDING_MINUTES:
        return snapshot.prediction_evaluate_command, "проверить факты прибытия"
    if _runtime_miss_issue(group):
        return command, "разобрать промахи"
    if calibration is not None:
        source_action = _runtime_source_support_action(snapshot)
        if source_action[0]:
            return source_action
        calibration_group = _profile_group(calibration.by_profile, snapshot.decision.profile.key)
        if calibration_group is not None and getattr(calibration_group, "status", "") == "late_risk":
            return command, "проверить запас"
    if _runtime_p50_issue(group):
        return command, "разобрать ошибку ETA"
    return "", ""


def _runtime_source_support_action(snapshot: StatsSnapshotView) -> tuple[str, str]:
    calibration = snapshot.runtime_calibration
    if calibration is None:
        return "", ""
    group = profile_source_group(calibration.by_profile_source, snapshot.decision.profile.key)
    if group is None or getattr(group, "status", "") != "late_risk":
        return "", ""
    command = _diagnostic_text(getattr(snapshot, "prediction_calibration_command", ""), fallback="", limit=160)
    if not command:
        command = prediction_calibration_command_for_profile(snapshot.decision.profile.key)
    return command, "проверить источник"


def _forecast_readiness_support_action(snapshot: StatsSnapshotView) -> tuple[str, str]:
    readiness = snapshot.forecast_readiness
    if readiness is None or getattr(readiness, "ready", True):
        return "", ""
    window = _forecast_window_for_profile(
        getattr(snapshot.forecast_health, "windows", ()), snapshot.decision.profile.key
    )
    command, reason = _forecast_readiness_action(snapshot, window)
    if not command:
        return "", ""
    return command, reason


def _forecast_readiness_action(snapshot: StatsSnapshotView, window: object | None) -> tuple[str, str]:
    coverage_command = _diagnostic_text(snapshot.forecast_coverage_command, fallback="", limit=160)
    if window is not None and getattr(window, "status", "") == "insufficient_bucket_coverage" and coverage_command:
        return coverage_command, "проверить покрытие окна"
    command = _diagnostic_text(snapshot.forecast_readiness_command, fallback="", limit=160)
    if command:
        return command, "проверить историю Яндекса"
    return "", ""


def _watch_state_line(snapshot: StatsSnapshotView) -> str:
    summary = snapshot.watch_state
    command = _diagnostic_text(snapshot.watch_state_command, fallback="route74 watch-state", limit=160)
    if summary is None:
        if snapshot.watch_state_error:
            reason = _diagnostic_text(snapshot.watch_state_error, fallback="ошибка без деталей")
            return f"🔔 Watch: недоступен · {reason} · {command}"
        return ""
    if getattr(summary, "status", "") == "critical":
        return f"🔔 Watch: файл недоступен · {_watch_file_detail(summary)} · {command}"
    if getattr(summary, "overdue_count", 0):
        return (
            f"🔔 Watch: проверка просрочена {summary.overdue_count}/{summary.active_count} · "
            f"максимум {_seconds(summary.max_overdue_seconds)} · {command}"
        )
    profile = _watch_profile(getattr(summary, "profiles", ()), snapshot.decision.profile.key)
    runtime_error_line = _watch_runtime_error_line(
        summary,
        profile,
        current_time=snapshot.decision.current_time,
        command=command,
    )
    if runtime_error_line:
        return runtime_error_line
    if profile is not None and getattr(profile, "active_count", 0):
        details = [f"активен {profile.active_count}"]
        if getattr(profile, "due_count", 0):
            details.append(f"ждёт проверку {profile.due_count}")
        if getattr(profile, "early_sent_count", 0):
            details.append(f"ранний сигнал уже был {profile.early_sent_count}")
        details.append(f"следующий {_relative_time(snapshot.decision.current_time, profile.next_poll_at)}")
        expires_in = getattr(profile, "expires_in_minutes", None)
        if expires_in is not None:
            details.append(f"до конца {format_duration_minutes(expires_in)}")
        return "🔔 Watch: " + " · ".join(details)
    if getattr(summary, "invalid_records", 0):
        return f"🔔 Watch: повреждённых записей {summary.invalid_records} · {command}"
    if getattr(summary, "file_status", "") == "missing":
        return f"🔔 Watch: файл ещё не создан · {command}"
    if not getattr(summary, "active_count", 0):
        return f"🔔 Watch: активных проверок нет · {command}"
    return ""


def _profile_group(groups: object, profile_key: str) -> Any | None:
    return next((group for group in groups if getattr(group, "key", "") == profile_key), None)


def _watch_profile(groups: object, profile_key: str) -> Any | None:
    return next((group for group in groups if getattr(group, "profile_key", "") == profile_key), None)


def _watch_runtime_error_line(
    summary: object,
    profile: object | None,
    *,
    current_time: datetime,
    command: str,
) -> str:
    target = profile if profile is not None and getattr(profile, "runtime_error_count", 0) else summary
    error_count = getattr(target, "runtime_error_count", 0)
    if not error_count:
        return ""
    details = [f"ошибки проверки {error_count}"]
    error_records = getattr(target, "runtime_error_records", 0)
    if error_records:
        details.append(f"watch {error_records}")
    latest = _relative_time(current_time, getattr(target, "latest_error_at", None))
    details.append(f"последняя {latest}")
    error_types = _watch_error_types(getattr(target, "runtime_error_types", ()))
    if error_types:
        details.append(error_types)
    details.append(command)
    return "🔔 Watch: " + " · ".join(details)


def _watch_error_types(values: object) -> str:
    if not isinstance(values, tuple):
        return ""
    return ", ".join(_diagnostic_text(value, fallback="", limit=40) for value in values if value)


def _bot_latency_top_error_reason(latency: object) -> str:
    category = bot_error_top_category_text(getattr(latency, "error_categories", ()))
    if category:
        return f" · причина {category}"
    reasons = getattr(latency, "error_reasons", ())
    if not isinstance(reasons, tuple) or not reasons:
        return ""
    top_reason = reasons[0]
    key = _diagnostic_text(getattr(top_reason, "key", ""), fallback="", limit=48)
    if not key:
        return ""
    count = getattr(top_reason, "count", 0)
    if isinstance(count, int) and not isinstance(count, bool) and count > 0:
        return f" · причина {key}:{count}"
    return f" · причина {key}"


def _age(age_seconds: int | None) -> str:
    if age_seconds is None or isinstance(age_seconds, bool) or age_seconds < 0:
        return "нет данных"
    if age_seconds < 60:
        return f"{age_seconds} сек назад"
    return f"{format_duration_minutes(round(age_seconds / 60))} назад"


def _bot_latency_freshness(current_time: datetime, latest_received_at: datetime | None) -> str:
    if latest_received_at is None:
        return ""
    try:
        age_seconds = round((current_time - latest_received_at).total_seconds())
    except (TypeError, ValueError):
        return ""
    return _age(age_seconds)


def _runtime_minutes(value: int | None) -> str:
    return "нет" if value is None else format_duration_minutes(value)


def _pending_age(current_time: datetime, sampled_at: datetime | None) -> str:
    age_minutes = _pending_age_minutes(current_time, sampled_at)
    if age_minutes is None:
        return ""
    return f"{format_duration_minutes(age_minutes)} назад"


def _pending_age_minutes(current_time: datetime, sampled_at: datetime | None) -> int | None:
    if sampled_at is None:
        return None
    try:
        delta = current_time - sampled_at
    except (TypeError, ValueError):
        return None
    total_seconds = round(delta.total_seconds())
    if total_seconds < 0:
        return None
    return round(total_seconds / 60)


def _runtime_miss_issue(group: object) -> bool:
    evaluated = int(getattr(group, "evaluated", 0) or 0)
    misses = int(getattr(group, "misses", 0) or 0)
    miss_rate = int(getattr(group, "miss_rate_percent", 0) or 0)
    return evaluated >= 3 and misses > 0 and miss_rate >= 50


def _runtime_p50_issue(group: object) -> bool:
    evaluated = int(getattr(group, "evaluated", 0) or 0)
    p50_abs = getattr(group, "p50_abs_error_minutes", None)
    return evaluated >= 3 and isinstance(p50_abs, int) and not isinstance(p50_abs, bool) and p50_abs >= 4


def _milliseconds(value: int | None) -> str:
    return "нет" if value is None else f"{value}мс"


def _relative_time(current_time: datetime, value: datetime | None) -> str:
    if value is None:
        return "время неизвестно"
    try:
        delta = value - current_time
    except (TypeError, ValueError):
        return "время неизвестно"
    total_seconds = round(delta.total_seconds())
    minutes = round(abs(total_seconds) / 60)
    if total_seconds >= 0:
        return f"через {format_duration_minutes(minutes)}"
    return f"{format_duration_minutes(minutes)} назад"


def _seconds(value: int | None) -> str:
    if value is None or isinstance(value, bool) or value < 0:
        return "нет данных"
    if value < 60:
        return f"{value} сек"
    return format_duration_minutes(round(value / 60))


def _watch_file_detail(summary: object) -> str:
    file_status = _diagnostic_text(getattr(summary, "file_status", ""), fallback="file unknown", limit=40)
    error_type = _diagnostic_text(getattr(summary, "error_type", ""), fallback="", limit=40)
    return f"{file_status}/{error_type}" if error_type else file_status


def _diagnostic_text(value: object, *, fallback: str, limit: int = 120) -> str:
    return sanitize_diagnostic_text(value, fallback=fallback, limit=limit)


def _heartbeat_age(current_time: datetime, updated_at: datetime) -> str:
    try:
        delta = current_time - updated_at
    except (TypeError, ValueError):
        return "время неизвестно"
    total_seconds = round(delta.total_seconds())
    age_minutes = round(abs(total_seconds) / 60)
    if total_seconds < 0:
        return f"часы впереди на {format_duration_minutes(age_minutes)}"
    return f"{format_duration_minutes(age_minutes)} назад"


def _confidence(confidence: EtaConfidence) -> str:
    return {
        EtaConfidence.HIGH: "высокое",
        EtaConfidence.MEDIUM: "среднее",
        EtaConfidence.LOW: "низкое",
        EtaConfidence.UNKNOWN: "низкое",
    }[confidence]


def _eta_source_text(source: EtaSource) -> str:
    return {
        EtaSource.YANDEX: "Яндекс",
        EtaSource.YANDEX_CORRECTED: "Яндекс+поправка",
        EtaSource.VEHICLE_PROGRESS: "координата",
        EtaSource.YANDEX_HISTORY: "история Яндекса",
    }[source]


def _selected_source_text(source: EtaSource | None) -> str:
    if source is None:
        return "нет источника"
    return _eta_source_text(source)
