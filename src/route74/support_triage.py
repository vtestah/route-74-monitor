from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from route74.diagnostics import sanitize_command_text, sanitize_diagnostic_text
from route74.domain.runtime_sources import BOT_EVENT_USER_REPLY
from route74.storage.forecast_health import ForecastHealthSummary, ForecastWindowHealth
from route74.storage.models import CountByKey
from route74.storage.monitoring import (
    MONITOR_CRITICAL,
    MONITOR_WARNING,
    MonitorIssue,
    MonitorSummary,
)
from route74.storage.runtime_quality import (
    BotRuntimeCalibration,
    BotRuntimePredictionQuality,
)
from route74.support_actions import (
    bot_latency_command,
    bot_runtime_command,
    forecast_backtest_command_for_profile,
    forecast_readiness_command_for_profile,
    prediction_calibration_command_for_profile,
    prediction_calibration_command_for_window,
    prediction_evaluate_command_for_profile,
    support_report_command_for_profile,
    watch_state_command_for_path,
)
from route74.watch_state import WatchStateSummary

TRIAGE_OK = "ok"
TRIAGE_WARNING = "warning"
TRIAGE_CRITICAL = "critical"
TRIAGE_INFO = "info"
TRIAGE_SEVERITIES = {TRIAGE_OK, TRIAGE_WARNING, TRIAGE_CRITICAL, TRIAGE_INFO}
TRIAGE_STATUS_ORDER = {TRIAGE_OK: 0, TRIAGE_WARNING: 1, TRIAGE_CRITICAL: 2}
# Shared operator ordering for warning/critical issues.
TRIAGE_KEY_PRIORITY = {
    "db_integrity": 90,
    "watch_state_file": 85,
    "watch_state_overdue": 82,
    "collector": 80,
    "watch_state_runtime_error": 79,
    "yandex_api_risk": 75,
    "yandex_canary": 70,
    "integrity_gap": 60,
    "forecast_window": 46,
    "forecast_window_missing": 47,
    "bot_runtime_guardrail_unavailable": 58,
    "bot_runtime_pending": 53,
    "bot_runtime_misses": 52,
    "bot_runtime_late_risk": 50,
    "bot_runtime_source_late_risk": 50,
    "history_backtest": 49,
    "bot_runtime_p50_error": 48,
    "watch_state_invalid": 45,
    "history_readiness": 29,
    "truth_window": 35,
    "bot_latency_errors": 34,
    "bot_no_eta_replies": 33,
    "bot_latency_p95": 32,
    "bot_latency_stale": 31,
    "bot_latency_malformed": 30,
    "watch_state_expired": 20,
}
DEFAULT_TRIAGE_ACTION = "route74 monitor-tick --fail-on critical"
OPERATOR_WATCH_KEYS = frozenset(
    {
        "watch_state_file",
        "watch_state_overdue",
        "watch_state_runtime_error",
        "watch_state_invalid",
    }
)
OPERATOR_RUNTIME_KEYS = frozenset(
    {
        "bot_runtime_guardrail_unavailable",
        "bot_runtime_pending",
        "bot_runtime_misses",
        "bot_runtime_late_risk",
        "bot_runtime_source_late_risk",
        "bot_runtime_p50_error",
    }
)
OPERATOR_FORECAST_KEYS = frozenset(
    {
        "forecast_window",
        "forecast_window_missing",
        "truth_window",
    }
)


@dataclass(frozen=True)
class SupportTriageItem:
    severity: str
    key: str
    message: str
    action: str

    def __post_init__(self) -> None:
        if self.severity not in TRIAGE_SEVERITIES:
            raise ValueError("support triage severity is unknown")
        object.__setattr__(
            self,
            "key",
            sanitize_diagnostic_text(self.key, fallback="unknown", limit=80),
        )
        object.__setattr__(
            self,
            "message",
            sanitize_diagnostic_text(self.message, fallback="-", limit=220),
        )
        object.__setattr__(
            self,
            "action",
            sanitize_command_text(self.action, fallback=DEFAULT_TRIAGE_ACTION, limit=160),
        )


@dataclass(frozen=True)
class SupportTriage:
    status: str
    primary_action: str
    items: tuple[SupportTriageItem, ...]

    def __post_init__(self) -> None:
        if self.status not in {TRIAGE_OK, TRIAGE_WARNING, TRIAGE_CRITICAL}:
            raise ValueError("support triage status is unknown")
        if not isinstance(self.items, tuple) or any(not isinstance(item, SupportTriageItem) for item in self.items):
            raise ValueError("support triage items need tuple of SupportTriageItem")


def build_support_triage(
    *,
    window_key: str,
    profile_key: str,
    hours: int,
    monitor: MonitorSummary,
    forecast: ForecastHealthSummary,
    runtime_quality: BotRuntimePredictionQuality,
    runtime_calibration: BotRuntimeCalibration,
    runtime_event_kind: str = BOT_EVENT_USER_REPLY,
    watch_state: WatchStateSummary | None = None,
) -> SupportTriage:
    items = [
        *_monitor_items(monitor, hours=hours, profile_key=profile_key),
        *_watch_state_items(watch_state),
        *_forecast_items(forecast, window_key=window_key),
        *_runtime_items(
            runtime_quality,
            runtime_calibration,
            profile_key=profile_key,
            hours=hours,
            runtime_event_kind=runtime_event_kind,
        ),
    ]
    status = _triage_status(tuple(items))
    primary_action = _primary_action(tuple(items))
    return SupportTriage(status=status, primary_action=primary_action, items=tuple(items))


def _monitor_items(
    summary: MonitorSummary,
    *,
    hours: int,
    profile_key: str,
) -> tuple[SupportTriageItem, ...]:
    return tuple(
        SupportTriageItem(
            _triage_severity(issue.severity),
            issue.key,
            issue.message,
            _monitor_action(issue, hours, profile_key=profile_key),
        )
        for issue in summary.issues
        if _issue_matches_profile(issue, profile_key)
        and not issue.key.startswith(("forecast_", "truth_"))
        and issue.key not in {"bot_runtime_late_risk", "watch_state_runtime_error"}
    )


def _issue_matches_profile(issue: MonitorIssue, profile_key: str) -> bool:
    return not issue.profile_key or issue.profile_key == profile_key


def _watch_state_items(
    summary: WatchStateSummary | None,
) -> tuple[SupportTriageItem, ...]:
    if summary is None:
        return ()
    action = watch_state_command_for_path(summary.path)
    items: list[SupportTriageItem] = []
    if summary.status == "critical":
        detail = f" type={summary.error_type}" if summary.error_type else ""
        items.append(
            SupportTriageItem(
                TRIAGE_CRITICAL,
                "watch_state_file",
                f"file={summary.file_status}{detail}",
                action,
            )
        )
    if summary.overdue_count:
        items.append(
            SupportTriageItem(
                TRIAGE_CRITICAL,
                "watch_state_overdue",
                (
                    f"active={summary.active_count} due={summary.due_count} "
                    f"overdue={summary.overdue_count} max_overdue={_seconds(summary.max_overdue_seconds)}s"
                ),
                action,
            )
        )
    if summary.runtime_error_count:
        items.append(
            SupportTriageItem(
                TRIAGE_WARNING,
                "watch_state_runtime_error",
                _watch_state_runtime_error_message(summary),
                action,
            )
        )
    if summary.invalid_records:
        items.append(
            SupportTriageItem(
                TRIAGE_WARNING,
                "watch_state_invalid",
                f"invalid={summary.invalid_records} total={summary.total_records}",
                action,
            )
        )
    if summary.expired_records:
        items.append(
            SupportTriageItem(
                TRIAGE_INFO,
                "watch_state_expired",
                f"expired={summary.expired_records} total={summary.total_records}",
                action,
            )
        )
    return tuple(items)


def _forecast_items(summary: ForecastHealthSummary, *, window_key: str) -> tuple[SupportTriageItem, ...]:
    window = _window(summary, window_key)
    if window is None:
        return (
            SupportTriageItem(
                TRIAGE_WARNING,
                "forecast_window_missing",
                f"window={window_key} is absent from forecast health summary",
                "route74 forecast-health",
            ),
        )
    items: list[SupportTriageItem] = []
    if window.api_risk_samples:
        items.append(
            SupportTriageItem(
                TRIAGE_CRITICAL if window.status == "api_contract_risk" else TRIAGE_WARNING,
                "yandex_api_risk",
                (
                    f"window={window.window_key} api_risk={window.api_risk_samples}({window.api_risk_percent}%) "
                    f"top={_counts_text(window.api_risk_reasons)}"
                ),
                "./bin/smoke-yandex",
            )
        )
    if window.status == "integrity_gap":
        items.append(
            SupportTriageItem(
                TRIAGE_WARNING,
                "integrity_gap",
                f"forecast_only={window.forecast_without_report_samples} report_only={window.report_without_forecast_samples}",
                "route74 forecast-health",
            )
        )
    elif not window.ready and window.status != "api_contract_risk":
        items.append(
            SupportTriageItem(
                TRIAGE_WARNING,
                "forecast_window",
                _forecast_window_message(window),
                _forecast_window_action(window),
            )
        )
    if window.truth_status in {"insufficient", "warming_up", "stale"}:
        items.append(
            SupportTriageItem(
                TRIAGE_WARNING,
                "truth_window",
                f"window={window.window_key} truth={window.truth_status} reason={window.truth_reason}",
                prediction_calibration_command_for_window(window.window_key),
            )
        )
    return tuple(items)


def _runtime_items(
    quality: BotRuntimePredictionQuality,
    calibration: BotRuntimeCalibration,
    *,
    profile_key: str,
    hours: int,
    runtime_event_kind: str,
) -> tuple[SupportTriageItem, ...]:
    items: list[SupportTriageItem] = []
    quality_group = _group(quality.by_profile, profile_key)
    calibration_group = _group(calibration.by_profile, profile_key)
    action_event_kind = None if runtime_event_kind == BOT_EVENT_USER_REPLY else runtime_event_kind
    action = bot_runtime_command(hours=hours, limit=8, profile_key=profile_key, event_kind=action_event_kind)
    scope = _runtime_scope_text(profile_key, runtime_event_kind)
    if quality_group is None:
        items.append(
            SupportTriageItem(
                TRIAGE_INFO,
                "bot_runtime_profile",
                f"{scope} has no bot runtime predictions in {hours}h",
                action,
            )
        )
        return tuple(items)
    if calibration_group is None:
        return tuple(items)
    if calibration_group.status == "late_risk":
        source_detail = _source_calibration_detail(calibration.by_profile_source, profile_key)
        source_action = _source_calibration_action(calibration.by_profile_source, profile_key)
        items.append(
            SupportTriageItem(
                TRIAGE_WARNING,
                "bot_runtime_late_risk",
                (
                    f"{scope} eval={calibration_group.evaluated}/{calibration_group.total} "
                    f"miss={calibration_group.misses}({calibration_group.miss_rate_percent}%) "
                    f"p80_early={_minutes(calibration_group.p80_early_minutes)} "
                    f"suggested=+{calibration_group.suggested_buffer_minutes}m"
                    f"{source_detail}"
                ),
                source_action or action,
            )
        )
    elif calibration_group.status == "extra_wait":
        items.append(
            SupportTriageItem(
                TRIAGE_INFO,
                "bot_runtime_extra_wait",
                (
                    f"{scope} eval={calibration_group.evaluated}/{calibration_group.total} "
                    f"p50_extra_wait={_minutes(calibration_group.p50_extra_wait_minutes)}"
                ),
                action,
            )
        )
    elif calibration_group.status == "insufficient":
        items.append(
            SupportTriageItem(
                TRIAGE_INFO,
                "bot_runtime_insufficient",
                f"{scope} eval={calibration_group.evaluated}/{calibration_group.total}",
                action,
            )
        )
    return tuple(items)


def _runtime_scope_text(profile_key: str, runtime_event_kind: str) -> str:
    if runtime_event_kind == BOT_EVENT_USER_REPLY:
        return f"profile={profile_key}"
    return f"profile={profile_key} event_kind={runtime_event_kind}"


def _window(summary: ForecastHealthSummary, window_key: str) -> ForecastWindowHealth | None:
    return next((window for window in summary.windows if window.window_key == window_key), None)


def _group(groups: tuple[object, ...], key: str) -> object | None:
    return next((group for group in groups if getattr(group, "key", "") == key), None)


def _source_calibration_detail(groups: tuple[object, ...], profile_key: str) -> str:
    group = _top_source_calibration_group(groups, profile_key)
    if group is None:
        return ""
    _profile, source = _profile_source_key(getattr(group, "key", ""))
    return (
        f" source={source or '-'} "
        f"source_eval={getattr(group, 'evaluated', 0)}/{getattr(group, 'total', 0)} "
        f"source_miss={getattr(group, 'misses', 0)}({getattr(group, 'miss_rate_percent', 0)}%) "
        f"source_p80_early={_minutes(getattr(group, 'p80_early_minutes', None))} "
        f"source_suggested=+{getattr(group, 'suggested_buffer_minutes', 0)}m"
    )


def _top_source_calibration_group(groups: tuple[object, ...], profile_key: str) -> object | None:
    candidates = [
        group
        for group in groups
        if _profile_source_key(getattr(group, "key", ""))[0] == profile_key
        and getattr(group, "status", "") == "late_risk"
    ]
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda group: (
            getattr(group, "suggested_buffer_minutes", 0),
            getattr(group, "miss_rate_percent", 0),
            getattr(group, "evaluated", 0),
        ),
    )


def _source_calibration_action(groups: tuple[object, ...], profile_key: str) -> str:
    group = _top_source_calibration_group(groups, profile_key)
    if group is None or getattr(group, "status", "") != "late_risk":
        return ""
    return prediction_calibration_command_for_profile(profile_key)


def _profile_source_key(value: object) -> tuple[str, str]:
    text = str(value)
    if "/" not in text:
        return text, ""
    profile, source = text.split("/", 1)
    return profile, source


def _triage_status(items: tuple[SupportTriageItem, ...]) -> str:
    status_items = tuple(item for item in items if item.severity in {TRIAGE_WARNING, TRIAGE_CRITICAL})
    if not status_items:
        return TRIAGE_OK
    return max(status_items, key=lambda item: TRIAGE_STATUS_ORDER[item.severity]).severity


def _primary_action(items: tuple[SupportTriageItem, ...]) -> str:
    item = primary_triage_item_for_items(items)
    if item is None:
        return DEFAULT_TRIAGE_ACTION
    return item.action


def primary_triage_item(triage: SupportTriage) -> SupportTriageItem | None:
    return primary_triage_item_for_items(triage.items)


def primary_triage_item_for_items(
    items: tuple[SupportTriageItem, ...],
) -> SupportTriageItem | None:
    actionable = tuple(item for item in items if item.severity in {TRIAGE_WARNING, TRIAGE_CRITICAL})
    if not actionable:
        return None
    return max(actionable, key=_item_priority)


def incident_triage_item_for_items(
    items: tuple[SupportTriageItem, ...],
) -> SupportTriageItem | None:
    primary = primary_triage_item_for_items(items)
    if primary is not None and primary.key in {
        "db_integrity",
        "watch_state_file",
        "watch_state_overdue",
        "watch_state_runtime_error",
        "watch_state_invalid",
    }:
        return primary
    bot_latency_items = tuple(
        item
        for item in items
        if item.key
        in {
            "bot_latency_errors",
            "bot_no_eta_replies",
            "bot_latency_p95",
            "bot_latency_stale",
            "bot_latency_malformed",
        }
        and item.severity in {TRIAGE_WARNING, TRIAGE_CRITICAL}
    )
    if bot_latency_items:
        return max(bot_latency_items, key=_item_priority)
    return primary


def incident_primary_triage_item(triage: SupportTriage) -> SupportTriageItem | None:
    return incident_triage_item_for_items(triage.items)


def incident_primary_action(triage: SupportTriage) -> str:
    item = incident_primary_triage_item(triage)
    if item is None:
        return triage.primary_action
    return item.action


def operator_triage_item_for_items(
    items: tuple[SupportTriageItem, ...],
) -> SupportTriageItem | None:
    watch_item = _actionable_item_by_keys(items, OPERATOR_WATCH_KEYS)
    if watch_item is not None:
        return watch_item
    primary = incident_triage_item_for_items(items)
    if primary is None:
        return None
    if primary.key in OPERATOR_FORECAST_KEYS:
        runtime_item = _actionable_item_by_keys(items, OPERATOR_RUNTIME_KEYS)
        if runtime_item is not None:
            return runtime_item
    return primary


def operator_primary_triage_item(triage: SupportTriage) -> SupportTriageItem | None:
    return operator_triage_item_for_items(triage.items)


def operator_primary_action(triage: SupportTriage) -> str:
    item = operator_primary_triage_item(triage)
    if item is None:
        return triage.primary_action
    return item.action


def _actionable_item_by_keys(
    items: tuple[SupportTriageItem, ...],
    keys: frozenset[str],
) -> SupportTriageItem | None:
    actionable = tuple(
        item for item in items if item.key in keys and item.severity in {TRIAGE_WARNING, TRIAGE_CRITICAL}
    )
    if not actionable:
        return None
    return max(actionable, key=_item_priority)


def _item_priority(item: SupportTriageItem) -> tuple[int, int]:
    severity_rank = TRIAGE_STATUS_ORDER[item.severity]
    key_rank = TRIAGE_KEY_PRIORITY.get(item.key, 10)
    return key_rank, severity_rank


def _triage_severity(severity: str) -> str:
    if severity == MONITOR_CRITICAL:
        return TRIAGE_CRITICAL
    if severity == MONITOR_WARNING:
        return TRIAGE_WARNING
    return TRIAGE_INFO


def _monitor_action(issue: MonitorIssue, hours: int, *, profile_key: str) -> str:
    if issue.key == "db_integrity":
        return "route74 db-health"
    if issue.key == "collector":
        return "route74 forecast-health"
    if issue.key == "yandex_canary":
        return "./bin/smoke-yandex"
    if issue.key == "bot_no_eta_replies":
        return _bot_latency_action(profile_key, hours=hours)
    if issue.key == "bot_runtime_guardrail_unavailable":
        return _support_report_action(
            issue.profile_key,
            fallback=bot_runtime_command(hours=hours, limit=8, event_kind=BOT_EVENT_USER_REPLY),
        )
    if issue.key.startswith("bot_latency_"):
        return _bot_latency_action(profile_key, hours=hours)
    if issue.key == "bot_runtime_pending":
        return _prediction_evaluate_action(
            issue.profile_key,
            fallback=bot_runtime_command(hours=hours, limit=8, event_kind=BOT_EVENT_USER_REPLY),
        )
    if issue.key == "bot_runtime_source_late_risk":
        return _prediction_calibration_action(
            issue.profile_key,
            fallback=bot_runtime_command(hours=hours, limit=8, event_kind=BOT_EVENT_USER_REPLY),
        )
    if issue.key == "history_readiness":
        return _forecast_readiness_action(issue.profile_key, fallback="route74 forecast-health")
    if issue.key == "history_backtest":
        return _forecast_backtest_action(issue.profile_key, fallback="route74 forecast-health")
    if issue.key.startswith("bot_runtime_"):
        return _bot_runtime_action(issue.profile_key, hours=hours)
    return "route74 forecast-health"


def _bot_runtime_action(profile_key: str, *, hours: int) -> str:
    try:
        return bot_runtime_command(
            hours=hours,
            limit=8,
            profile_key=profile_key,
            event_kind=BOT_EVENT_USER_REPLY,
        )
    except ValueError:
        return bot_runtime_command(hours=hours, limit=8, event_kind=BOT_EVENT_USER_REPLY)


def _bot_latency_action(profile_key: str, *, hours: int) -> str:
    try:
        return bot_latency_command(hours=hours, profile_key=profile_key, event_kind=BOT_EVENT_USER_REPLY)
    except ValueError:
        return bot_latency_command(hours=hours, event_kind=BOT_EVENT_USER_REPLY)


def _prediction_evaluate_action(profile_key: str, *, fallback: str) -> str:
    try:
        return prediction_evaluate_command_for_profile(profile_key)
    except ValueError:
        return fallback


def _prediction_calibration_action(profile_key: str, *, fallback: str) -> str:
    try:
        return prediction_calibration_command_for_profile(profile_key)
    except ValueError:
        return fallback


def _support_report_action(profile_key: str, *, fallback: str) -> str:
    try:
        return support_report_command_for_profile(profile_key)
    except ValueError:
        return fallback


def _forecast_readiness_action(profile_key: str, *, fallback: str) -> str:
    try:
        return forecast_readiness_command_for_profile(profile_key)
    except ValueError:
        return fallback


def _forecast_backtest_action(profile_key: str, *, fallback: str) -> str:
    try:
        return forecast_backtest_command_for_profile(profile_key)
    except ValueError:
        return fallback


def _seconds(value: int | None) -> str:
    return "-" if value is None else str(value)


def _watch_state_runtime_error_message(summary: WatchStateSummary) -> str:
    return (
        f"errors={summary.runtime_error_count} watches={summary.runtime_error_records} "
        f"latest={_datetime_text(summary.latest_error_at)} types={_runtime_error_types_text(summary.runtime_error_types)}"
    )


def _datetime_text(value: object) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return "n/a"


def _runtime_error_types_text(values: tuple[str, ...]) -> str:
    return "n/a" if not values else ", ".join(values)


def _forecast_window_message(window: ForecastWindowHealth) -> str:
    missing = _limited_text(window.missing_bucket_labels, limit=4)
    return (
        f"window={window.window_key} status={window.status} reason={window.reason} "
        f"ready_buckets={window.ready_buckets}/{window.total_buckets} missing={missing}"
    )


def _forecast_window_action(window: ForecastWindowHealth) -> str:
    if window.status == "insufficient_bucket_coverage":
        return f"route74 forecast-coverage --window {window.window_key}"
    if window.status in {"no_eta", "stale_eta"}:
        return f"route74 forecast-readiness --window {window.window_key}"
    return "route74 forecast-health"


def _counts_text(counts: tuple[CountByKey, ...]) -> str:
    return ", ".join(f"{sanitize_diagnostic_text(item.key, fallback='-')}:{item.count}" for item in counts) or "-"


def _limited_text(values: tuple[str, ...], *, limit: int) -> str:
    if not values:
        return "-"
    visible = values[:limit]
    suffix = f",+{len(values) - limit}" if len(values) > limit else ""
    return ",".join(visible) + suffix


def _minutes(value: int | None) -> str:
    return "-" if value is None else f"{value}m"
