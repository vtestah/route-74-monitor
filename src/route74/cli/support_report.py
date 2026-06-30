from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from route74.cli.bot_latency import format_bot_latency_summary
from route74.cli.bot_runtime import format_bot_runtime_summary
from route74.cli.common import positive_int
from route74.cli.forecast_backtest import format_forecast_backtest_summary
from route74.cli.forecast_health import format_forecast_health_summary
from route74.cli.forecast_formatting import format_forecast_readiness_summary
from route74.cli.forecast_readiness import WINDOWS_BY_KEY
from route74.cli.monitor import format_monitor_summary
from route74.cli.prediction_lab import format_prediction_lab_calibration
from route74.diagnostics import sanitize_diagnostic_text
from route74.domain.profiles import PROFILE_KEYS
from route74.domain.reporting import report_window_for_profile
from route74.domain.runtime_sources import BOT_EVENT_KINDS, BOT_EVENT_USER_REPLY
from route74.models import now_local
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
    BotRuntimePredictionQuality,
    connect,
    init_db,
    load_recent_bot_runtime_predictions,
    summarize_bot_latency,
    summarize_bot_runtime_calibration,
    summarize_bot_runtime_predictions,
    summarize_forecast_health,
    summarize_yandex_forecast_readiness,
    summarize_prediction_lab_calibration,
)
from route74.storage.forecast_backtest import (
    DEFAULT_FORECAST_BACKTEST_PERCENTILES,
    ForecastBacktestSummary,
    summarize_yandex_forecast_backtest,
)
from route74.storage.forecast_health import ForecastHealthSummary
from route74.storage.helpers import WEEKDAYS
from route74.storage.monitoring import MonitorSummary, summarize_monitor
from route74.support_triage import SupportTriage, build_support_triage, operator_primary_action
from route74.support_actions import (
    bot_latency_command,
    bot_runtime_command,
    forecast_backtest_command_for_profile,
    forecast_coverage_command_for_window,
    forecast_readiness_command_for_profile,
    prediction_calibration_command_for_window,
    support_report_command_for_window,
    watch_state_command_for_path,
)
from route74.watch_state import DEFAULT_WATCH_STATE_PATH, WatchStateSummary, format_watch_state_summary, summarize_watch_state


SUPPORT_REPORT_DB_LABEL = "<db>"
SUPPORT_REPORT_CRITICAL = "critical"
FAILED_SECTION_PRIORITY = {
    "db": 100,
    "monitor": 90,
    "watch-state": 85,
    "bot-latency": 80,
    "forecast-health": 70,
    "forecast-readiness": 65,
    "forecast-backtest": 64,
    "bot-runtime": 60,
    "prediction-calibration": 50,
}


@dataclass(frozen=True)
class SupportReportSection:
    key: str
    text: str
    failed: bool = False

    def __post_init__(self) -> None:
        if not self.key:
            raise ValueError("support report section key is required")
        if not isinstance(self.text, str):
            raise ValueError("support report section text needs text")


@dataclass(frozen=True)
class SupportReport:
    window_key: str
    profile_key: str
    event_kind: str
    hours: int
    limit: int
    current_time: datetime
    status: str
    next_action: str
    sections: tuple[SupportReportSection, ...]
    db_label: str


@dataclass(frozen=True)
class _TriageBuildResult:
    section: SupportReportSection
    status: str
    primary_action: str


def register_support_report_command(subparsers: argparse._SubParsersAction) -> None:
    report = subparsers.add_parser("support-report", help="Print one sanitized operational diagnostic report.")
    report.add_argument("--window", choices=tuple(WINDOWS_BY_KEY), default=None, help="Report window to diagnose.")
    report.add_argument("--profile", choices=PROFILE_KEYS, default=None, help="Shortcut for the profile report window.")
    report.add_argument(
        "--event-kind",
        choices=sorted(BOT_EVENT_KINDS),
        default=BOT_EVENT_USER_REPLY,
        help="Focus the runtime-events slice on one web runtime event kind.",
    )
    report.add_argument("--hours", type=positive_int, default=24, help="Bot diagnostics window in hours.")
    report.add_argument("--limit", type=positive_int, default=8, help="Recent runtime decisions to show.")
    report.add_argument(
        "--watch-state-path",
        type=Path,
        default=DEFAULT_WATCH_STATE_PATH,
        help="Persisted watch state JSON path.",
    )
    report.set_defaults(func=cmd_support_report)


def cmd_support_report(args: argparse.Namespace) -> None:
    report = build_support_report(
        args.db,
        window_key=_target_window_key(args.window, args.profile),
        event_kind=args.event_kind,
        hours=args.hours,
        limit=args.limit,
        watch_state_path=args.watch_state_path,
    )
    print(format_support_report(report))


def build_support_report(
    db_path: Path,
    *,
    window_key: str,
    event_kind: str = BOT_EVENT_USER_REPLY,
    hours: int = 24,
    limit: int = 8,
    watch_state_path: Path = DEFAULT_WATCH_STATE_PATH,
    current_time: datetime | None = None,
) -> SupportReport:
    window = WINDOWS_BY_KEY[window_key]
    current_time = current_time or now_local()
    db_label = SUPPORT_REPORT_DB_LABEL
    try:
        connection_context = connect(db_path)
        with connection_context as connection:
            init_db(connection)
            sections: list[SupportReportSection] = []
            monitor: MonitorSummary | None = None
            forecast: ForecastHealthSummary | None = None
            latency: BotLatencySummary | None = None
            runtime_quality: BotRuntimePredictionQuality | None = None
            runtime_calibration: BotRuntimeCalibration | None = None
            watch_state_summary: WatchStateSummary | None = None
            watch_state_section: SupportReportSection | None = None

            try:
                watch_state_summary = summarize_watch_state(watch_state_path, current_time)
            except Exception as exc:
                watch_state_section = _failed_section("watch-state", exc)

            try:
                monitor = summarize_monitor(
                    connection,
                    db_path=db_path,
                    latency_hours=hours,
                    runtime_hours=hours,
                    min_no_eta_events=1,
                    profile_key=window.profile_key,
                    current_time=current_time,
                )
                forecast = monitor.forecast
                latency = monitor.latency
                sections.append(
                    SupportReportSection(
                        "monitor",
                        format_monitor_summary(
                            monitor,
                            db_label,
                            watch_state=watch_state_summary,
                            watch_state_path=watch_state_path,
                            profile_key=window.profile_key,
                        ),
                    )
                )
            except Exception as exc:
                sections.append(_failed_section("monitor", exc))

            try:
                if forecast is None:
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
                sections.append(
                    SupportReportSection("forecast-health", format_forecast_health_summary(forecast, db_label))
                )
            except Exception as exc:
                sections.append(_failed_section("forecast-health", exc))

            try:
                forecast_readiness = summarize_yandex_forecast_readiness(
                    connection,
                    profile_key=window.profile_key,
                    current_time=current_time,
                    days=DEFAULT_HISTORY_DAYS,
                    min_samples=DEFAULT_MIN_OBSERVATIONS,
                    min_distinct_days=DEFAULT_MIN_HISTORY_DAYS,
                    primary_bucket_minutes=DEFAULT_PRIMARY_BUCKET_MINUTES,
                    fallback_bucket_minutes=DEFAULT_FALLBACK_BUCKET_MINUTES,
                    max_age_seconds=DEFAULT_HISTORY_MAX_AGE_SECONDS,
                    weekdays=WEEKDAYS,
                    report_window_key=window.key,
                )
                readiness_text = format_forecast_readiness_summary(forecast_readiness, Path(db_label))
                readiness_action = forecast_readiness_command_for_profile(window.profile_key)
                readiness_lines = [readiness_text, f'action="{readiness_action}"']
                if not forecast_readiness.ready:
                    coverage_window = _forecast_window(forecast, window.key)
                    if coverage_window is not None and coverage_window.status == "insufficient_bucket_coverage":
                        coverage_action = forecast_coverage_command_for_window(window.key)
                        readiness_lines.append(f'coverage_action="{coverage_action}"')
                sections.append(
                    SupportReportSection(
                        "forecast-readiness",
                        "\n".join(readiness_lines),
                    )
                )
            except Exception as exc:
                sections.append(_failed_section("forecast-readiness", exc))

            try:
                forecast_backtest = _forecast_backtest_summary(connection, monitor, window.profile_key, window.key)
                backtest_text = format_forecast_backtest_summary(forecast_backtest, Path(db_label))
                backtest_action = forecast_backtest_command_for_profile(window.profile_key)
                sections.append(
                    SupportReportSection(
                        "forecast-backtest",
                        "\n".join((backtest_text, f'action="{backtest_action}"')),
                    )
                )
            except Exception as exc:
                sections.append(_failed_section("forecast-backtest", exc))

            try:
                if latency is None:
                    latency = summarize_bot_latency(
                        connection,
                        hours=hours,
                        current_time=current_time,
                        profile_key=window.profile_key,
                        event_kind=BOT_EVENT_USER_REPLY,
                    )
                sections.append(SupportReportSection("bot-latency", format_bot_latency_summary(latency, db_label)))
            except Exception as exc:
                sections.append(_failed_section("bot-latency", exc))

            if watch_state_summary is not None:
                sections.append(
                    SupportReportSection(
                        "watch-state",
                        format_watch_state_summary(watch_state_summary, str(watch_state_path)),
                        failed=watch_state_summary.status == "critical",
                    )
                )
            elif watch_state_section is not None:
                sections.append(watch_state_section)

            try:
                runtime_quality = summarize_bot_runtime_predictions(
                    connection,
                    current_time=current_time,
                    hours=hours,
                    profile_key=window.profile_key,
                    event_kind=event_kind,
                )
                runtime_calibration = summarize_bot_runtime_calibration(
                    connection,
                    current_time=current_time,
                    hours=hours,
                    profile_key=window.profile_key,
                    event_kind=event_kind,
                )
                runtime_recent = load_recent_bot_runtime_predictions(
                    connection,
                    current_time=current_time,
                    hours=hours,
                    limit=limit,
                    profile_key=window.profile_key,
                    event_kind=event_kind,
                )
                runtime_change_history = load_recent_bot_runtime_predictions(
                    connection,
                    current_time=current_time,
                    hours=hours,
                    limit=max(16, limit + 8),
                    profile_key=window.profile_key,
                    event_kind=event_kind,
                )
                runtime_changes = build_runtime_prediction_change_map(
                    runtime_recent,
                    history_predictions=runtime_change_history,
                    event_kind=event_kind,
                )
                sections.append(
                    SupportReportSection(
                        "bot-runtime",
                        format_bot_runtime_summary(
                            runtime_quality,
                            runtime_recent,
                            db_label,
                            calibration=runtime_calibration,
                            profile_key=window.profile_key,
                            event_kind=event_kind,
                            changes=runtime_changes,
                        ),
                    )
                )
            except Exception as exc:
                sections.append(_failed_section("bot-runtime", exc))

            try:
                prediction_calibration = summarize_prediction_lab_calibration(
                    connection,
                    profile_key=window.profile_key,
                    report_window_key=window.key,
                    current_time=current_time,
                )
                sections.append(
                    SupportReportSection(
                        "prediction-calibration",
                        format_prediction_lab_calibration(prediction_calibration, db_label),
                    )
                )
            except Exception as exc:
                sections.append(_failed_section("prediction-calibration", exc))
    except Exception as exc:
        db_section = _failed_section("db", exc)
        return SupportReport(
            window_key=window.key,
            profile_key=window.profile_key,
            event_kind=event_kind,
            hours=hours,
            limit=limit,
            current_time=current_time,
            status=SUPPORT_REPORT_CRITICAL,
            next_action="route74 db-health",
            sections=(
                SupportReportSection(
                    "triage",
                    _format_failed_section_triage(
                        (db_section,),
                        window_key=window.key,
                        event_kind=event_kind,
                        hours=hours,
                        limit=limit,
                        watch_state_path=watch_state_path,
                    ),
                ),
                db_section,
            ),
            db_label=db_label,
        )

    report_sections = tuple(sections)
    failed_sections = tuple(section for section in report_sections if section.failed)
    triage_result = _triage_result(
        window_key=window.key,
        profile_key=window.profile_key,
        event_kind=event_kind,
        hours=hours,
        limit=limit,
        monitor=monitor,
        forecast=forecast,
        runtime_quality=runtime_quality,
        runtime_calibration=runtime_calibration,
        watch_state_summary=watch_state_summary,
        failed_sections=failed_sections,
        watch_state_path=watch_state_path,
    )
    triage_section = triage_result.section
    all_failed_sections = failed_sections + ((triage_section,) if triage_section.failed else ())
    status = SUPPORT_REPORT_CRITICAL if all_failed_sections else triage_result.status
    primary_failed_section = _primary_failed_section(all_failed_sections)
    next_action = (
        _section_action(
            primary_failed_section.key,
            window_key=window.key,
            event_kind=event_kind,
            hours=hours,
            limit=limit,
            watch_state_path=watch_state_path,
        )
        if primary_failed_section is not None
        else triage_result.primary_action
    )
    return SupportReport(
        window_key=window.key,
        profile_key=window.profile_key,
        event_kind=event_kind,
        hours=hours,
        limit=limit,
        current_time=current_time,
        status=status,
        next_action=next_action,
        sections=(triage_section, *report_sections),
        db_label=db_label,
    )


def format_support_report(report: SupportReport) -> str:
    event_kind = f" event_kind={report.event_kind}" if report.event_kind != BOT_EVENT_USER_REPLY else ""
    header = (
        f"support report window={report.window_key} profile={report.profile_key} "
        f"hours={report.hours} limit={report.limit}{event_kind} at={report.current_time:%Y-%m-%d %H:%M} "
        f"status={report.status} next=\"{report.next_action}\" db={report.db_label}"
    )
    lines = [header]
    for section in report.sections:
        lines.extend((f"section={section.key}", section.text))
    return "\n".join(lines)


def _triage_result(
    *,
    window_key: str,
    profile_key: str,
    event_kind: str,
    hours: int,
    limit: int,
    monitor: MonitorSummary | None,
    forecast: ForecastHealthSummary | None,
    runtime_quality: BotRuntimePredictionQuality | None,
    runtime_calibration: BotRuntimeCalibration | None,
    watch_state_summary: WatchStateSummary | None,
    failed_sections: tuple[SupportReportSection, ...],
    watch_state_path: Path,
) -> _TriageBuildResult:
    if (
        monitor is None
        or forecast is None
        or runtime_quality is None
        or runtime_calibration is None
    ):
        return _TriageBuildResult(
            section=SupportReportSection(
                "triage",
                _format_failed_section_triage(
                    failed_sections,
                    window_key=window_key,
                    event_kind=event_kind,
                    hours=hours,
                    limit=limit,
                    watch_state_path=watch_state_path,
                ),
            ),
            status=SUPPORT_REPORT_CRITICAL,
            primary_action=_failed_section_primary_action(
                failed_sections,
                window_key=window_key,
                event_kind=event_kind,
                hours=hours,
                limit=limit,
                watch_state_path=watch_state_path,
            ),
        )
    try:
        triage = build_support_triage(
            window_key=window_key,
            profile_key=profile_key,
            hours=hours,
            monitor=monitor,
            forecast=forecast,
            runtime_quality=runtime_quality,
            runtime_calibration=runtime_calibration,
            runtime_event_kind=event_kind,
            watch_state=watch_state_summary,
        )
        return _TriageBuildResult(
            section=SupportReportSection(
                "triage",
                _format_triage(
                    triage,
                    failed_sections=failed_sections,
                    window_key=window_key,
                    event_kind=event_kind,
                    hours=hours,
                    limit=limit,
                    watch_state_path=watch_state_path,
                ),
            ),
            status=SUPPORT_REPORT_CRITICAL if failed_sections else triage.status,
            primary_action=(
                _failed_section_primary_action(
                    failed_sections,
                    window_key=window_key,
                    event_kind=event_kind,
                    hours=hours,
                    limit=limit,
                    watch_state_path=watch_state_path,
                )
                if failed_sections
                else operator_primary_action(triage)
            ),
        )
    except Exception as exc:
        section = _failed_section("triage", exc)
        return _TriageBuildResult(
            section=section,
            status=SUPPORT_REPORT_CRITICAL,
            primary_action=_section_action(
                section.key,
                window_key=window_key,
                event_kind=event_kind,
                hours=hours,
                limit=limit,
                watch_state_path=watch_state_path,
            ),
        )


def _format_triage(
    triage: SupportTriage,
    *,
    failed_sections: tuple[SupportReportSection, ...] = (),
    window_key: str,
    event_kind: str,
    hours: int,
    limit: int,
    watch_state_path: Path,
) -> str:
    status = SUPPORT_REPORT_CRITICAL if failed_sections else triage.status
    primary_failed_section = _primary_failed_section(failed_sections)
    primary_action = (
        _section_action(
            primary_failed_section.key,
            window_key=window_key,
            event_kind=event_kind,
            hours=hours,
            limit=limit,
            watch_state_path=watch_state_path,
        )
        if primary_failed_section is not None
        else operator_primary_action(triage)
    )
    lines = [f"triage status={status} primary=\"{primary_action}\""]
    lines.extend(
        _failed_section_triage_line(
            section,
            window_key=window_key,
            event_kind=event_kind,
            hours=hours,
            limit=limit,
            watch_state_path=watch_state_path,
        )
        for section in failed_sections
    )
    if not triage.items:
        if not failed_sections:
            lines.append("- ok: no active issues")
        return "\n".join(lines)
    lines.extend(
        f"- {item.severity} {item.key}: {item.message} action=\"{item.action}\""
        for item in triage.items
    )
    return "\n".join(lines)


def _format_failed_section_triage(
    failed_sections: tuple[SupportReportSection, ...],
    *,
    window_key: str,
    event_kind: str,
    hours: int,
    limit: int,
    watch_state_path: Path,
) -> str:
    primary_failed_section = _primary_failed_section(failed_sections)
    primary_action = (
        _section_action(
            primary_failed_section.key,
            window_key=window_key,
            event_kind=event_kind,
            hours=hours,
            limit=limit,
            watch_state_path=watch_state_path,
        )
        if primary_failed_section is not None
        else "route74 db-health"
    )
    lines = [f"triage status=critical primary=\"{primary_action}\""]
    if not failed_sections:
        lines.append('- critical support_report: report dependencies are unavailable action="route74 db-health"')
        return "\n".join(lines)
    lines.extend(
        _failed_section_triage_line(
            section,
            window_key=window_key,
            event_kind=event_kind,
            hours=hours,
            limit=limit,
            watch_state_path=watch_state_path,
        )
        for section in failed_sections
    )
    return "\n".join(lines)


def _failed_section_primary_action(
    failed_sections: tuple[SupportReportSection, ...],
    *,
    window_key: str,
    event_kind: str,
    hours: int,
    limit: int,
    watch_state_path: Path,
) -> str:
    primary_failed_section = _primary_failed_section(failed_sections)
    if primary_failed_section is not None:
        return _section_action(
            primary_failed_section.key,
            window_key=window_key,
            event_kind=event_kind,
            hours=hours,
            limit=limit,
            watch_state_path=watch_state_path,
        )
    return "route74 db-health"


def _primary_failed_section(failed_sections: tuple[SupportReportSection, ...]) -> SupportReportSection | None:
    if not failed_sections:
        return None
    return max(
        failed_sections,
        key=lambda section: FAILED_SECTION_PRIORITY.get(section.key, 10),
    )


def _failed_section_triage_line(
    section: SupportReportSection,
    *,
    window_key: str,
    event_kind: str,
    hours: int,
    limit: int,
    watch_state_path: Path,
) -> str:
    action = _section_action(
        section.key,
        window_key=window_key,
        event_kind=event_kind,
        hours=hours,
        limit=limit,
        watch_state_path=watch_state_path,
    )
    return f'- critical support_report_{section.key}: section={section.key} failed action="{action}"'


def _failed_section(key: str, error: Exception) -> SupportReportSection:
    message = sanitize_diagnostic_text(error, fallback="failed", limit=240)
    error_type = sanitize_diagnostic_text(type(error).__name__, fallback="Exception", limit=80)
    return SupportReportSection(
        key,
        f"section_error section={key} type={error_type} message=\"{message}\"",
        failed=True,
    )


def _section_action(
    section_key: str,
    *,
    window_key: str,
    event_kind: str,
    hours: int,
    limit: int,
    watch_state_path: Path,
) -> str:
    if section_key in {"db", "monitor"}:
        return "route74 db-health" if section_key == "db" else "route74 monitor-tick --fail-on critical"
    if section_key == "forecast-health":
        return "route74 forecast-health"
    if section_key == "forecast-readiness":
        return forecast_readiness_command_for_profile(WINDOWS_BY_KEY[window_key].profile_key)
    if section_key == "forecast-backtest":
        return forecast_backtest_command_for_profile(WINDOWS_BY_KEY[window_key].profile_key)
    if section_key == "bot-latency":
        return bot_latency_command(
            hours=hours,
            profile_key=WINDOWS_BY_KEY[window_key].profile_key,
            event_kind=BOT_EVENT_USER_REPLY,
        )
    if section_key == "watch-state":
        return watch_state_command_for_path(watch_state_path)
    if section_key == "bot-runtime":
        return bot_runtime_command(
            hours=hours,
            limit=limit,
            profile_key=WINDOWS_BY_KEY[window_key].profile_key,
            event_kind=None if event_kind == BOT_EVENT_USER_REPLY else event_kind,
        )
    if section_key == "prediction-calibration":
        return prediction_calibration_command_for_window(window_key)
    return support_report_command_for_window(
        window_key,
        event_kind=None if event_kind == BOT_EVENT_USER_REPLY else event_kind,
    )


def _target_window_key(window_key: str | None, profile_key: str | None) -> str:
    if window_key is None and profile_key is None:
        raise SystemExit("support-report needs --window or --profile")
    if window_key is None:
        return report_window_for_profile(str(profile_key)).key
    window = WINDOWS_BY_KEY[window_key]
    if profile_key is not None and profile_key != window.profile_key:
        raise SystemExit(f"--profile {profile_key} conflicts with --window {window.key}")
    return window.key


def _forecast_window(summary: ForecastHealthSummary | None, window_key: str) -> object | None:
    if summary is None:
        return None
    return next((window for window in summary.windows if window.window_key == window_key), None)


def _forecast_backtest_summary(
    connection: object,
    monitor: MonitorSummary | None,
    profile_key: str,
    window_key: str,
) -> ForecastBacktestSummary:
    monitor_backtest = getattr(monitor, "backtest", None)
    if monitor_backtest is not None:
        return monitor_backtest
    return summarize_yandex_forecast_backtest(
        connection,
        profile_key=profile_key,
        report_window_key=window_key,
        history_days=DEFAULT_HISTORY_DAYS,
        bucket_minutes=DEFAULT_PRIMARY_BUCKET_MINUTES,
        min_samples=DEFAULT_MIN_OBSERVATIONS,
        min_distinct_days=DEFAULT_MIN_HISTORY_DAYS,
        percentiles=DEFAULT_FORECAST_BACKTEST_PERCENTILES,
        max_age_seconds=DEFAULT_HISTORY_MAX_AGE_SECONDS,
    )
