from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from route74.domain.profiles import PROFILE_KEYS
from route74.domain.reporting import report_window_for_profile
from route74.domain.runtime_sources import BOT_EVENT_USER_REPLY
from route74.models import now_local
from route74.services.yandex_history import (
    DEFAULT_FALLBACK_BUCKET_MINUTES,
    DEFAULT_HISTORY_DAYS,
    DEFAULT_HISTORY_MAX_AGE_SECONDS,
    DEFAULT_MIN_HISTORY_DAYS,
    DEFAULT_MIN_OBSERVATIONS,
    DEFAULT_PRIMARY_BUCKET_MINUTES,
)
from route74.storage.bot_latency import BotLatencySummary, summarize_bot_latency
from route74.storage.db_admin import DbHealthSummary, summarize_db_health_readonly
from route74.storage.forecast_backtest import (
    DEFAULT_FORECAST_BACKTEST_PERCENTILES,
    ForecastBacktestSummary,
    best_forecast_backtest_result,
    selected_forecast_backtest_result,
    summarize_yandex_forecast_backtest,
)
from route74.storage.forecast_health import (
    ForecastHealthSummary,
    summarize_forecast_health,
)
from route74.storage.forecast_readiness import summarize_yandex_forecast_readiness
from route74.storage.helpers import WEEKDAYS
from route74.storage.models import CountByKey, ForecastReadinessSummary
from route74.storage.runtime_quality import (
    BotRuntimeCalibration,
    BotRuntimePredictionQuality,
    BotRuntimePredictionQualityGroup,
    summarize_bot_runtime_calibration,
    summarize_bot_runtime_predictions,
)

MONITOR_OK = "ok"
MONITOR_WARNING = "warning"
MONITOR_CRITICAL = "critical"
MONITOR_SEVERITY_ORDER = {MONITOR_OK: 0, MONITOR_WARNING: 1, MONITOR_CRITICAL: 2}
MONITOR_SEVERITIES = frozenset(MONITOR_SEVERITY_ORDER)
CRITICAL_COLLECTOR_STATUSES = {"missing", "stale", "unexpected_skipped"}
DEFAULT_MIN_NO_ETA_EVENTS = 3
DEFAULT_MIN_LATENCY_EVENTS = 3
DEFAULT_HISTORY_BACKTEST_MIN_EVALUATED = 5
DEFAULT_HISTORY_BACKTEST_WARN_MISS_RATE_PERCENT = 25


@dataclass(frozen=True)
class MonitorIssue:
    severity: str
    key: str
    message: str
    profile_key: str = ""

    def __post_init__(self) -> None:
        if self.severity not in MONITOR_SEVERITIES:
            raise ValueError("monitor issue severity is unknown")
        _ensure_text("monitor issue key", self.key)
        _ensure_text("monitor issue message", self.message)
        _ensure_optional_plain_key("monitor issue profile key", self.profile_key)


@dataclass(frozen=True)
class MonitorSummary:
    db: DbHealthSummary
    forecast: ForecastHealthSummary
    latency: BotLatencySummary
    issues: tuple[MonitorIssue, ...]
    runtime: BotRuntimePredictionQuality | None = None
    calibration: BotRuntimeCalibration | None = None
    readiness: ForecastReadinessSummary | None = None
    backtest: ForecastBacktestSummary | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.issues, tuple) or any(not isinstance(issue, MonitorIssue) for issue in self.issues):
            raise ValueError("monitor summary issues need tuple of MonitorIssue")
        if self.readiness is not None and not isinstance(self.readiness, ForecastReadinessSummary):
            raise ValueError("monitor summary readiness needs ForecastReadinessSummary or None")
        if self.backtest is not None and not isinstance(self.backtest, ForecastBacktestSummary):
            raise ValueError("monitor summary backtest needs ForecastBacktestSummary or None")

    @property
    def status(self) -> str:
        if not self.issues:
            return MONITOR_OK
        return max(self.issues, key=lambda issue: MONITOR_SEVERITY_ORDER[issue.severity]).severity

    @property
    def warning_count(self) -> int:
        return sum(1 for issue in self.issues if issue.severity == MONITOR_WARNING)

    @property
    def critical_count(self) -> int:
        return sum(1 for issue in self.issues if issue.severity == MONITOR_CRITICAL)


def summarize_monitor(
    connection: sqlite3.Connection,
    *,
    db_path: Path,
    latency_hours: int = 24,
    warn_latency_ms: int = 5_000,
    critical_latency_ms: int = 15_000,
    warn_error_rate_percent: int = 10,
    critical_error_rate_percent: int = 30,
    min_no_eta_events: int = DEFAULT_MIN_NO_ETA_EVENTS,
    min_latency_events: int = DEFAULT_MIN_LATENCY_EVENTS,
    warn_no_eta_rate_percent: int = 50,
    critical_no_eta_rate_percent: int = 80,
    runtime_hours: int = 24,
    min_bot_runtime_evaluated: int = 3,
    warn_bot_miss_rate_percent: int = 50,
    critical_bot_miss_rate_percent: int = 80,
    warn_bot_p50_abs_error_minutes: int = 4,
    critical_bot_p50_abs_error_minutes: int = 8,
    warn_bot_pending_age_minutes: int = 120,
    critical_bot_pending_age_minutes: int = 360,
    warn_bot_guardrail_unavailable: int = 1,
    critical_bot_guardrail_unavailable: int = 3,
    warn_history_backtest_min_evaluated: int = DEFAULT_HISTORY_BACKTEST_MIN_EVALUATED,
    warn_history_backtest_miss_rate_percent: int = DEFAULT_HISTORY_BACKTEST_WARN_MISS_RATE_PERCENT,
    profile_key: str | None = None,
    current_time: datetime | None = None,
) -> MonitorSummary:
    profile_key = _optional_profile_key(profile_key)
    latency_hours = _positive_int("latency_hours", latency_hours)
    warn_latency_ms = _positive_int("warn_latency_ms", warn_latency_ms)
    critical_latency_ms = _positive_int("critical_latency_ms", critical_latency_ms)
    warn_error_rate_percent = _percent_int("warn_error_rate_percent", warn_error_rate_percent)
    critical_error_rate_percent = _percent_int("critical_error_rate_percent", critical_error_rate_percent)
    min_no_eta_events = _positive_int("min_no_eta_events", min_no_eta_events)
    min_latency_events = _positive_int("min_latency_events", min_latency_events)
    warn_no_eta_rate_percent = _percent_int("warn_no_eta_rate_percent", warn_no_eta_rate_percent)
    critical_no_eta_rate_percent = _percent_int("critical_no_eta_rate_percent", critical_no_eta_rate_percent)
    runtime_hours = _positive_int("runtime_hours", runtime_hours)
    min_bot_runtime_evaluated = _positive_int("min_bot_runtime_evaluated", min_bot_runtime_evaluated)
    warn_bot_miss_rate_percent = _percent_int("warn_bot_miss_rate_percent", warn_bot_miss_rate_percent)
    critical_bot_miss_rate_percent = _percent_int("critical_bot_miss_rate_percent", critical_bot_miss_rate_percent)
    warn_bot_p50_abs_error_minutes = _positive_int(
        "warn_bot_p50_abs_error_minutes",
        warn_bot_p50_abs_error_minutes,
    )
    critical_bot_p50_abs_error_minutes = _positive_int(
        "critical_bot_p50_abs_error_minutes",
        critical_bot_p50_abs_error_minutes,
    )
    warn_bot_pending_age_minutes = _positive_int(
        "warn_bot_pending_age_minutes",
        warn_bot_pending_age_minutes,
    )
    critical_bot_pending_age_minutes = _positive_int(
        "critical_bot_pending_age_minutes",
        critical_bot_pending_age_minutes,
    )
    warn_bot_guardrail_unavailable = _positive_int(
        "warn_bot_guardrail_unavailable",
        warn_bot_guardrail_unavailable,
    )
    critical_bot_guardrail_unavailable = _positive_int(
        "critical_bot_guardrail_unavailable",
        critical_bot_guardrail_unavailable,
    )
    warn_history_backtest_min_evaluated = _positive_int(
        "warn_history_backtest_min_evaluated",
        warn_history_backtest_min_evaluated,
    )
    warn_history_backtest_miss_rate_percent = _percent_int(
        "warn_history_backtest_miss_rate_percent",
        warn_history_backtest_miss_rate_percent,
    )
    _ensure_threshold_order("latency", warn_latency_ms, critical_latency_ms)
    _ensure_threshold_order("error rate", warn_error_rate_percent, critical_error_rate_percent)
    _ensure_threshold_order("no ETA rate", warn_no_eta_rate_percent, critical_no_eta_rate_percent)
    _ensure_threshold_order("bot miss rate", warn_bot_miss_rate_percent, critical_bot_miss_rate_percent)
    _ensure_threshold_order(
        "bot p50 absolute error",
        warn_bot_p50_abs_error_minutes,
        critical_bot_p50_abs_error_minutes,
    )
    _ensure_threshold_order(
        "bot pending age",
        warn_bot_pending_age_minutes,
        critical_bot_pending_age_minutes,
    )
    _ensure_threshold_order(
        "bot guardrail unavailable",
        warn_bot_guardrail_unavailable,
        critical_bot_guardrail_unavailable,
    )

    current_time = current_time or now_local()
    db = summarize_db_health_readonly(db_path)
    forecast = summarize_forecast_health(
        connection,
        current_date=current_time,
        days=DEFAULT_HISTORY_DAYS,
        min_samples=DEFAULT_MIN_OBSERVATIONS,
        min_distinct_days=DEFAULT_MIN_HISTORY_DAYS,
        primary_bucket_minutes=DEFAULT_PRIMARY_BUCKET_MINUTES,
        fallback_bucket_minutes=DEFAULT_FALLBACK_BUCKET_MINUTES,
        max_age_seconds=DEFAULT_HISTORY_MAX_AGE_SECONDS,
        step_minutes=30,
    )
    latency = summarize_bot_latency(
        connection,
        hours=latency_hours,
        current_time=current_time,
        profile_key=profile_key,
    )
    runtime = summarize_bot_runtime_predictions(
        connection,
        current_time=current_time,
        hours=runtime_hours,
        profile_key=profile_key,
        event_kind=BOT_EVENT_USER_REPLY,
    )
    calibration = summarize_bot_runtime_calibration(
        connection,
        current_time=current_time,
        hours=runtime_hours,
        min_evaluated=min_bot_runtime_evaluated,
        profile_key=profile_key,
        event_kind=BOT_EVENT_USER_REPLY,
    )
    readiness = _profile_readiness_summary(
        connection,
        profile_key=profile_key,
        current_time=current_time,
    )
    backtest = _profile_backtest_summary(connection, profile_key=profile_key)
    issues = (
        *_db_issues(db),
        *_forecast_issues(forecast),
        *_history_readiness_issues(readiness),
        *_history_backtest_issues(
            backtest,
            min_evaluated=warn_history_backtest_min_evaluated,
            warn_miss_rate_percent=warn_history_backtest_miss_rate_percent,
        ),
        *_latency_issues(
            latency,
            warn_latency_ms=warn_latency_ms,
            critical_latency_ms=critical_latency_ms,
            warn_error_rate_percent=warn_error_rate_percent,
            critical_error_rate_percent=critical_error_rate_percent,
            min_no_eta_events=min_no_eta_events,
            min_latency_events=min_latency_events,
            warn_no_eta_rate_percent=warn_no_eta_rate_percent,
            critical_no_eta_rate_percent=critical_no_eta_rate_percent,
        ),
        *_runtime_issues(
            runtime,
            min_evaluated=min_bot_runtime_evaluated,
            warn_miss_rate_percent=warn_bot_miss_rate_percent,
            critical_miss_rate_percent=critical_bot_miss_rate_percent,
            warn_p50_abs_error_minutes=warn_bot_p50_abs_error_minutes,
            critical_p50_abs_error_minutes=critical_bot_p50_abs_error_minutes,
            warn_pending_age_minutes=warn_bot_pending_age_minutes,
            critical_pending_age_minutes=critical_bot_pending_age_minutes,
            warn_guardrail_unavailable=warn_bot_guardrail_unavailable,
            critical_guardrail_unavailable=critical_bot_guardrail_unavailable,
            current_time=current_time,
        ),
        *_runtime_calibration_issues(calibration),
    )
    return MonitorSummary(
        db=db,
        forecast=forecast,
        latency=latency,
        issues=issues,
        runtime=runtime,
        calibration=calibration,
        readiness=readiness,
        backtest=backtest,
    )


def _db_issues(db: DbHealthSummary) -> tuple[MonitorIssue, ...]:
    if db.healthy:
        return ()
    return (
        MonitorIssue(
            MONITOR_CRITICAL,
            "db_integrity",
            f"integrity={db.integrity_check} quick={db.quick_check}",
        ),
    )


def _forecast_issues(forecast: ForecastHealthSummary) -> tuple[MonitorIssue, ...]:
    issues: list[MonitorIssue] = []
    if not forecast.collector.healthy:
        issues.append(
            MonitorIssue(
                _collector_issue_severity(forecast.collector.status),
                "collector",
                f"{forecast.collector.name} status={forecast.collector.status} message={forecast.collector.message}",
            )
        )
    if not forecast.canary.healthy:
        issues.append(
            MonitorIssue(
                MONITOR_WARNING,
                "yandex_canary",
                f"status={forecast.canary.status} reason={forecast.canary.risk_reason}",
            )
        )
    for window in forecast.windows:
        if not window.ready:
            issues.append(
                MonitorIssue(
                    MONITOR_WARNING,
                    f"forecast_{window.profile_key}",
                    f"window={window.window_key} status={window.status} reason={window.reason}",
                    profile_key=window.profile_key,
                )
            )
        if window.truth_status in {"insufficient", "warming_up", "stale"}:
            issues.append(
                MonitorIssue(
                    MONITOR_WARNING,
                    f"truth_{window.profile_key}",
                    f"status={window.truth_status} reason={window.truth_reason}",
                    profile_key=window.profile_key,
                )
            )
    return tuple(issues)


def _collector_issue_severity(status: str) -> str:
    if status in CRITICAL_COLLECTOR_STATUSES:
        return MONITOR_CRITICAL
    return MONITOR_WARNING


def _profile_readiness_summary(
    connection: sqlite3.Connection,
    *,
    profile_key: str | None,
    current_time: datetime,
) -> ForecastReadinessSummary | None:
    if profile_key is None:
        return None
    window = report_window_for_profile(profile_key)
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


def _history_readiness_issues(
    readiness: ForecastReadinessSummary | None,
) -> tuple[MonitorIssue, ...]:
    if readiness is None or readiness.ready:
        return ()
    latest = readiness.latest_sampled_at.strftime("%Y-%m-%d %H:%M") if readiness.latest_sampled_at else "-"
    return (
        MonitorIssue(
            MONITOR_WARNING,
            "history_readiness",
            (
                f"window={readiness.report_window_key or '-'} "
                f"bucket=+/-{readiness.selected_bucket_minutes}m "
                f"samples={readiness.selected_sample_count}/{readiness.min_samples} "
                f"days={readiness.selected_distinct_days}/{readiness.min_distinct_days} "
                f"fresh_eta={readiness.fresh_eta_samples} latest={latest}"
            ),
            profile_key=readiness.profile_key,
        ),
    )


def _profile_backtest_summary(
    connection: sqlite3.Connection,
    *,
    profile_key: str | None,
) -> ForecastBacktestSummary | None:
    if profile_key is None:
        return None
    window = report_window_for_profile(profile_key)
    return summarize_yandex_forecast_backtest(
        connection,
        profile_key=profile_key,
        report_window_key=window.key,
        history_days=DEFAULT_HISTORY_DAYS,
        bucket_minutes=DEFAULT_PRIMARY_BUCKET_MINUTES,
        min_samples=DEFAULT_MIN_OBSERVATIONS,
        min_distinct_days=DEFAULT_MIN_HISTORY_DAYS,
        percentiles=DEFAULT_FORECAST_BACKTEST_PERCENTILES,
        max_age_seconds=DEFAULT_HISTORY_MAX_AGE_SECONDS,
    )


def _history_backtest_issues(
    backtest: ForecastBacktestSummary | None,
    *,
    min_evaluated: int,
    warn_miss_rate_percent: int,
) -> tuple[MonitorIssue, ...]:
    result = selected_forecast_backtest_result(backtest) if backtest is not None else None
    if backtest is None or result is None or result.evaluated_cases < min_evaluated:
        return ()
    if result.miss_rate_percent < warn_miss_rate_percent:
        return ()
    best = best_forecast_backtest_result(backtest)
    best_text = ""
    if best is not None and best.percentile != result.percentile and best.evaluated_cases > 0:
        best_text = (
            f" best=p{best.percentile} "
            f"miss={best.miss_cases}/{best.evaluated_cases}({best.miss_rate_percent}%) "
            f"mae={best.mean_absolute_error:.1f}"
        )
    return (
        MonitorIssue(
            MONITOR_WARNING,
            "history_backtest",
            (
                f"window={backtest.report_window_key} p{result.percentile} "
                f"miss={result.miss_cases}/{result.evaluated_cases}({result.miss_rate_percent}%) "
                f"bucket_accuracy={result.bucket_accurate_cases}/{result.evaluated_cases}"
                f"({result.bucket_accuracy_percent}%) "
                f"mae={result.mean_absolute_error:.1f} "
                f"miss_minutes={result.miss_minutes} extra_wait={result.extra_wait_minutes}"
                f"{best_text}"
            ),
            profile_key=backtest.profile_key,
        ),
    )


def _latency_issues(
    latency: BotLatencySummary,
    *,
    warn_latency_ms: int,
    critical_latency_ms: int,
    warn_error_rate_percent: int,
    critical_error_rate_percent: int,
    min_no_eta_events: int,
    min_latency_events: int,
    warn_no_eta_rate_percent: int,
    critical_no_eta_rate_percent: int,
) -> tuple[MonitorIssue, ...]:
    profile_key = latency.profile_key or ""
    if latency.total_events == 0:
        if latency.latest_received_at is not None:
            return (
                MonitorIssue(
                    MONITOR_WARNING,
                    "bot_latency_stale",
                    f"no events in last {latency.hours}h latest={latency.latest_received_at.isoformat()}",
                    profile_key=profile_key,
                ),
            )
        return ()
    issues: list[MonitorIssue] = []
    if latency.invalid_duration_events:
        issues.append(
            MonitorIssue(
                MONITOR_WARNING,
                "bot_latency_malformed",
                f"invalid_durations={latency.invalid_duration_events}/{latency.total_events}",
                profile_key=profile_key,
            )
        )
    if latency.error_events and latency.error_rate_percent >= critical_error_rate_percent:
        severity = MONITOR_CRITICAL
    elif latency.error_events and latency.error_rate_percent >= warn_error_rate_percent:
        severity = MONITOR_WARNING
    else:
        severity = ""
    if severity:
        issues.append(
            MonitorIssue(
                severity,
                "bot_latency_errors",
                (
                    f"errors={latency.error_events}/{latency.total_events}({latency.error_rate_percent}%) "
                    f"top_error={_top_count_key(latency.error_categories or latency.error_reasons)}"
                ),
                profile_key=profile_key,
            )
        )
    if latency.no_eta_events >= min_no_eta_events:
        if latency.no_eta_rate_percent >= critical_no_eta_rate_percent:
            severity = MONITOR_CRITICAL
        elif latency.no_eta_rate_percent >= warn_no_eta_rate_percent:
            severity = MONITOR_WARNING
        else:
            severity = ""
        if severity:
            issues.append(
                MonitorIssue(
                    severity,
                    "bot_no_eta_replies",
                    (
                        f"no_eta={latency.no_eta_events}/{latency.total_events}({latency.no_eta_rate_percent}%) "
                        f"top_reason={_top_count_key(latency.no_eta_reasons)}"
                    ),
                    profile_key=profile_key,
                )
            )
    if latency.p95_total_ms is not None:
        if latency.total_events < min_latency_events:
            return tuple(issues)
        if latency.p95_total_ms >= critical_latency_ms:
            severity = MONITOR_CRITICAL
        elif latency.p95_total_ms >= warn_latency_ms:
            severity = MONITOR_WARNING
        else:
            severity = ""
        if severity:
            issues.append(
                MonitorIssue(
                    severity,
                    "bot_latency_p95",
                    f"p95_total={latency.p95_total_ms}ms events={latency.total_events}",
                    profile_key=profile_key,
                )
            )
    return tuple(issues)


def _runtime_issues(
    runtime: BotRuntimePredictionQuality,
    *,
    min_evaluated: int,
    warn_miss_rate_percent: int,
    critical_miss_rate_percent: int,
    warn_p50_abs_error_minutes: int,
    critical_p50_abs_error_minutes: int,
    warn_pending_age_minutes: int,
    critical_pending_age_minutes: int,
    warn_guardrail_unavailable: int,
    critical_guardrail_unavailable: int,
    current_time: datetime,
) -> tuple[MonitorIssue, ...]:
    issues: list[MonitorIssue] = []
    issues.extend(
        _runtime_guardrail_issues(
            runtime,
            warn_guardrail_unavailable=warn_guardrail_unavailable,
            critical_guardrail_unavailable=critical_guardrail_unavailable,
        )
    )
    issues.extend(
        _runtime_pending_issues(
            runtime,
            warn_pending_age_minutes=warn_pending_age_minutes,
            critical_pending_age_minutes=critical_pending_age_minutes,
            current_time=current_time,
        )
    )
    issues.extend(
        _runtime_miss_issues(
            runtime,
            min_evaluated=min_evaluated,
            warn_miss_rate_percent=warn_miss_rate_percent,
            critical_miss_rate_percent=critical_miss_rate_percent,
        )
    )
    issues.extend(
        _runtime_p50_error_issues(
            runtime,
            min_evaluated=min_evaluated,
            warn_p50_abs_error_minutes=warn_p50_abs_error_minutes,
            critical_p50_abs_error_minutes=critical_p50_abs_error_minutes,
        )
    )
    return tuple(issues)


def _runtime_guardrail_issues(
    runtime: BotRuntimePredictionQuality,
    *,
    warn_guardrail_unavailable: int,
    critical_guardrail_unavailable: int,
) -> tuple[MonitorIssue, ...]:
    issues: list[MonitorIssue] = []
    for group in _runtime_profile_groups(runtime.by_profile):
        if group.guardrail_unavailable < warn_guardrail_unavailable:
            continue
        severity = (
            MONITOR_CRITICAL if group.guardrail_unavailable >= critical_guardrail_unavailable else MONITOR_WARNING
        )
        issues.append(
            MonitorIssue(
                severity,
                "bot_runtime_guardrail_unavailable",
                (
                    f"profile={group.key} "
                    f"guardrail_unavailable={group.guardrail_unavailable}/{group.total}"
                    f"({group.guardrail_unavailable_percent}%) "
                    f"top_source={_top_profile_source_group(runtime.by_profile_source, group.key, 'guardrail_unavailable')}"
                ),
                profile_key=group.key,
            )
        )
    return tuple(issues)


def _runtime_pending_issues(
    runtime: BotRuntimePredictionQuality,
    *,
    warn_pending_age_minutes: int,
    critical_pending_age_minutes: int,
    current_time: datetime,
) -> tuple[MonitorIssue, ...]:
    issues: list[MonitorIssue] = []
    for group in _runtime_profile_groups(runtime.by_profile):
        if group.pending <= 0 or group.oldest_pending_sampled_at is None:
            continue
        age_minutes = _age_minutes(current_time, group.oldest_pending_sampled_at)
        if age_minutes is None:
            continue
        if age_minutes >= critical_pending_age_minutes:
            severity = MONITOR_CRITICAL
        elif age_minutes >= warn_pending_age_minutes:
            severity = MONITOR_WARNING
        else:
            continue
        issues.append(
            MonitorIssue(
                severity,
                "bot_runtime_pending",
                (
                    f"profile={group.key} pending={group.pending}/{group.total}({group.pending_percent}%) "
                    f"oldest_pending={age_minutes}m evaluated={group.evaluated}/{group.total} "
                    f"latest_eval={_optional_datetime(group.latest_evaluated_at)}"
                ),
                profile_key=group.key,
            )
        )
    return tuple(issues)


def _runtime_miss_issues(
    runtime: BotRuntimePredictionQuality,
    *,
    min_evaluated: int,
    warn_miss_rate_percent: int,
    critical_miss_rate_percent: int,
) -> tuple[MonitorIssue, ...]:
    issues: list[MonitorIssue] = []
    for group in _runtime_profile_groups(runtime.by_profile):
        if group.evaluated < min_evaluated:
            continue
        if group.misses and group.miss_rate_percent >= critical_miss_rate_percent:
            severity = MONITOR_CRITICAL
        elif group.misses and group.miss_rate_percent >= warn_miss_rate_percent:
            severity = MONITOR_WARNING
        else:
            continue
        issues.append(
            MonitorIssue(
                severity,
                "bot_runtime_misses",
                (
                    f"profile={group.key} misses={group.misses}/{group.evaluated}"
                    f"({group.miss_rate_percent}%) "
                    f"p50_abs={_optional_minutes(group.p50_abs_error_minutes)} "
                    f"top_source={_top_profile_source_group(runtime.by_profile_source, group.key, 'misses')}"
                ),
                profile_key=group.key,
            )
        )
    return tuple(issues)


def _runtime_p50_error_issues(
    runtime: BotRuntimePredictionQuality,
    *,
    min_evaluated: int,
    warn_p50_abs_error_minutes: int,
    critical_p50_abs_error_minutes: int,
) -> tuple[MonitorIssue, ...]:
    issues: list[MonitorIssue] = []
    for group in _runtime_profile_groups(runtime.by_profile):
        if group.evaluated < min_evaluated or group.p50_abs_error_minutes is None:
            continue
        if group.p50_abs_error_minutes >= critical_p50_abs_error_minutes:
            severity = MONITOR_CRITICAL
        elif group.p50_abs_error_minutes >= warn_p50_abs_error_minutes:
            severity = MONITOR_WARNING
        else:
            continue
        issues.append(
            MonitorIssue(
                severity,
                "bot_runtime_p50_error",
                (
                    f"profile={group.key} p50_abs={_optional_minutes(group.p50_abs_error_minutes)} "
                    f"evaluated={group.evaluated} "
                    f"top_source={_top_profile_source_group(runtime.by_profile_source, group.key, 'p50_abs_error_minutes')}"
                ),
                profile_key=group.key,
            )
        )
    return tuple(issues)


def _runtime_calibration_issues(
    calibration: BotRuntimeCalibration,
) -> tuple[MonitorIssue, ...]:
    late_risk_groups = tuple(group for group in calibration.by_profile if group.status == "late_risk")
    late_risk_profiles = frozenset(str(getattr(group, "key", "") or "") for group in late_risk_groups)
    source_issues = tuple(
        _runtime_source_calibration_issue(group)
        for group in _runtime_source_late_risk_groups(calibration.by_profile_source)
        if _profile_source_key(getattr(group, "key", ""))[0] not in late_risk_profiles
    )
    return (
        *(tuple(_runtime_calibration_issue(group, calibration) for group in late_risk_groups)),
        *source_issues,
    )


def _runtime_calibration_issue(
    group: object,
    calibration: BotRuntimeCalibration,
) -> MonitorIssue:
    profile_key = str(getattr(group, "key", "") or "")
    return MonitorIssue(
        MONITOR_WARNING,
        "bot_runtime_late_risk",
        (
            f"profile={profile_key or '-'} "
            f"eval={getattr(group, 'evaluated', 0)}/{getattr(group, 'total', 0)} "
            f"miss={getattr(group, 'misses', 0)}({getattr(group, 'miss_rate_percent', 0)}%) "
            f"p80_early={_optional_minutes(getattr(group, 'p80_early_minutes', None))} "
            f"suggested=+{getattr(group, 'suggested_buffer_minutes', 0)}m "
            f"top_source={_top_calibration_source_group(calibration.by_profile_source, profile_key)}"
        ),
        profile_key=profile_key,
    )


def _runtime_source_late_risk_groups(groups: object) -> tuple[object, ...]:
    if not isinstance(groups, tuple):
        return ()
    by_profile: dict[str, list[object]] = {}
    for group in groups:
        profile_key, source_key = _profile_source_key(getattr(group, "key", ""))
        if not profile_key or not source_key or getattr(group, "status", "") != "late_risk":
            continue
        by_profile.setdefault(profile_key, []).append(group)
    return tuple(max(profile_groups, key=_source_calibration_priority) for profile_groups in by_profile.values())


def _runtime_source_calibration_issue(group: object) -> MonitorIssue:
    profile_key, source_key = _profile_source_key(getattr(group, "key", ""))
    return MonitorIssue(
        MONITOR_WARNING,
        "bot_runtime_source_late_risk",
        (
            f"profile={profile_key or '-'} source={source_key or '-'} "
            f"eval={getattr(group, 'evaluated', 0)}/{getattr(group, 'total', 0)} "
            f"miss={getattr(group, 'misses', 0)}({getattr(group, 'miss_rate_percent', 0)}%) "
            f"p80_early={_optional_minutes(getattr(group, 'p80_early_minutes', None))} "
            f"suggested=+{getattr(group, 'suggested_buffer_minutes', 0)}m"
        ),
        profile_key=profile_key,
    )


def _top_calibration_source_group(groups: object, profile_key: str) -> str:
    if not isinstance(groups, tuple):
        return "-"
    candidates = [
        group
        for group in groups
        if _profile_source_key(getattr(group, "key", ""))[0] == profile_key
        and getattr(group, "status", "") == "late_risk"
    ]
    if not candidates:
        return "-"
    top = max(
        candidates,
        key=lambda group: (
            getattr(group, "suggested_buffer_minutes", 0),
            getattr(group, "miss_rate_percent", 0),
            getattr(group, "evaluated", 0),
        ),
    )
    _profile, source = _profile_source_key(getattr(top, "key", ""))
    return f"{source or '-'}:{getattr(top, 'suggested_buffer_minutes', 0)}m/{getattr(top, 'miss_rate_percent', 0)}%"


def _source_calibration_priority(group: object) -> tuple[int, int, int]:
    return (
        getattr(group, "suggested_buffer_minutes", 0),
        getattr(group, "miss_rate_percent", 0),
        getattr(group, "evaluated", 0),
    )


def _profile_source_key(value: object) -> tuple[str, str]:
    text = str(value)
    if "/" not in text:
        return text, ""
    profile, source = text.split("/", 1)
    return profile, source


def _runtime_profile_groups(
    groups: object,
) -> tuple[BotRuntimePredictionQualityGroup, ...]:
    if not isinstance(groups, tuple):
        return ()
    return tuple(
        group
        for group in groups
        if isinstance(group, BotRuntimePredictionQualityGroup) and _plain_profile_key(group.key)
    )


def _top_profile_source_group(groups: object, profile_key: str, metric: str) -> str:
    if not isinstance(groups, tuple):
        return "-"
    candidates = [
        (group, value)
        for group in groups
        if _profile_source_key(getattr(group, "key", ""))[0] == profile_key
        for value in (_positive_metric_value(getattr(group, metric, None)),)
        if value is not None
    ]
    if not candidates:
        return "-"
    top, value = max(candidates, key=lambda item: (item[1], getattr(item[0], "total", 0)))
    _profile, source = _profile_source_key(getattr(top, "key", ""))
    if metric == "p50_abs_error_minutes":
        return f"{source or '-'}:{_optional_minutes(value)}"
    return f"{source or '-'}:{value}"


def _positive_metric_value(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _plain_profile_key(value: object) -> str:
    text = str(value)
    if not text or not text.isascii():
        return ""
    if not all(char.isalnum() or char == "_" for char in text):
        return ""
    return text


def _age_minutes(current_time: datetime, sampled_at: datetime) -> int | None:
    try:
        delta = current_time - sampled_at
    except TypeError:
        return None
    return max(0, round(delta.total_seconds() / 60))


def _top_count_key(items: tuple[CountByKey, ...]) -> str:
    if not items:
        return "-"
    return items[0].key or "-"


def _optional_minutes(value: int | None) -> str:
    return "-" if value is None else f"{value}m"


def _optional_datetime(value: datetime | None) -> str:
    return "-" if value is None else value.isoformat()


def _positive_int(name: str, value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _optional_profile_key(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or value not in PROFILE_KEYS:
        expected = ", ".join(PROFILE_KEYS)
        raise ValueError(f"profile_key must be one of {expected}")
    return value


def _percent_int(name: str, value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 100:
        raise ValueError(f"{name} must be an integer from 0 to 100")
    return value


def _ensure_threshold_order(label: str, warn_value: int, critical_value: int) -> None:
    if critical_value < warn_value:
        raise ValueError(f"{label} critical threshold must be greater than or equal to warning threshold")


def _ensure_text(label: str, value: object) -> None:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{label} is required")


def _ensure_optional_plain_key(label: str, value: object) -> None:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be text")
    if not value:
        return
    if value != value.strip() or not value.isascii() or not all(char.isalnum() or char == "_" for char in value):
        raise ValueError(f"{label} must be a plain ASCII key")
