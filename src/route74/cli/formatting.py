from __future__ import annotations

from pathlib import Path
from typing import Iterable

from route74.services.yandex_telemetry import YandexTelemetryResult
from route74.sources.yandex.line import YandexLineTopology
from route74.storage import CountByKey, ReportWindowSummary, YandexTelemetrySummary


def format_line_topology(topology: YandexLineTopology) -> str:
    lines = [
        f"line_id={_diagnostic_text(topology.line_id)} active_thread={_diagnostic_text(topology.active_thread_id)} "
        f"threads={len(topology.threads)}"
    ]
    for thread in topology.threads:
        active = " active" if thread.thread_id == topology.active_thread_id else ""
        stops = ", ".join(f"{_diagnostic_text(stop.name)}({_diagnostic_text(stop.stop_id)})" for stop in thread.stops[:4])
        suffix = " ..." if len(thread.stops) > 4 else ""
        lines.append(
            f"- thread={_diagnostic_text(thread.thread_id)}{active} "
            f"{_diagnostic_text(thread.start_stop_name)} -> {_diagnostic_text(thread.end_stop_name)} "
            f"start_id={_diagnostic_text(thread.start_stop_id)} end_id={_diagnostic_text(thread.end_stop_id)} "
            f"stops={len(thread.stops)} points={thread.segment_point_count}"
        )
        if stops:
            lines.append(f"  first_stops={stops}{suffix}")
    return "\n".join(lines)


def format_yandex_collect_result(result: YandexTelemetryResult, db_path: Path) -> str:
    eta = ",".join(str(item) for item in result.arrival_minutes) or "-"
    reason = _optional_field("reason", result.fallback_reason)
    traffic_reason = _optional_field("traffic_reason", result.traffic_reason)
    geometry_reason = _optional_field("geometry_reason", result.route_geometry_reason)
    return (
        f"yandex snapshot profile={_diagnostic_text(result.profile_key)} "
        f"method={_diagnostic_text(result.source_method)} "
        f"status={_diagnostic_text(result.source_status)} available={int(result.available)} "
        f"vehicles={result.vehicle_count} eta=[{eta}] "
        f"traffic={_diagnostic_text(result.traffic_provider)}/{_diagnostic_text(result.traffic_status)} "
        f"geometry={_diagnostic_text(result.route_geometry_status)} "
        f"prediction_lab=p{result.prediction_events_created}/a{result.arrival_events_created}/"
        f"e{result.evaluations_created} "
        f"total_snapshots={result.total_snapshots} total_observations={result.total_observations} "
        f"db={_diagnostic_text(db_path)}{reason}{traffic_reason}{geometry_reason}"
    )


def format_yandex_telemetry_summary(summary: YandexTelemetrySummary, db_path: Path) -> str:
    profile = _diagnostic_text(summary.profile_key or "all")
    latest = summary.latest_sampled_at.strftime("%H:%M") if summary.latest_sampled_at else "-"
    lines = [
        f"yandex stats profile={profile} hours={summary.hours} db={_diagnostic_text(db_path)}",
        (
            f"snapshots={summary.total_snapshots} eta={summary.eta_snapshots}"
            f"({summary.eta_coverage_percent}%) vehicles={summary.vehicle_snapshots}"
            f"({summary.vehicle_coverage_percent}%) observations={summary.total_observations} "
            f"eta_observations={summary.eta_observations} latest={latest}"
        ),
        f"statuses={counts_text(summary.statuses)}",
        f"methods={counts_text(summary.methods)}",
    ]
    if summary.heartbeat is None:
        lines.append("heartbeat=-")
    else:
        lines.append(
            f"heartbeat={summary.heartbeat.updated_at:%H:%M} "
            f"pid={summary.heartbeat.pid} status={_diagnostic_text(summary.heartbeat.last_status)} "
            f"profiles={_diagnostic_text(summary.heartbeat.profile_filter)} "
            f"message={_diagnostic_text(summary.heartbeat.last_message)}"
        )
    latest_run = summary.collector_runs.latest_started_at
    latest_run_text = latest_run.strftime("%H:%M") if latest_run else "-"
    lines.extend(
        [
            (
                f"collector_runs={summary.collector_runs.total_runs} "
                f"result_runs={summary.collector_runs.result_runs} "
                f"eta_runs={summary.collector_runs.eta_runs}"
                f"({summary.collector_runs.eta_run_percent}%) "
                f"traffic_ok_runs={summary.collector_runs.traffic_ok_runs}"
                f"({summary.collector_runs.traffic_ok_run_percent}%) "
                f"skipped={summary.collector_runs.skipped_runs} latest={latest_run_text}"
            ),
            f"run_statuses={counts_text(summary.collector_runs.statuses)}",
        ]
    )
    return "\n".join(lines)


def format_report_window_summary(summary: ReportWindowSummary, db_path: Path) -> str:
    profile = _diagnostic_text(summary.profile_key or "all")
    window = _diagnostic_text(summary.report_window_key or "all")
    latest = summary.latest_sampled_at.strftime("%Y-%m-%d %H:%M") if summary.latest_sampled_at else "-"
    return "\n".join(
        [
            f"report stats window={window} profile={profile} days={summary.days} db={_diagnostic_text(db_path)}",
            (
                f"samples={summary.total_samples} eta={summary.eta_samples}"
                f"({summary.eta_coverage_percent}%) traffic={summary.traffic_samples}"
                f"({summary.traffic_coverage_percent}%) latest={latest}"
            ),
            f"statuses={counts_text(summary.statuses)}",
        ]
    )


def counts_text(items: Iterable[CountByKey]) -> str:
    return ", ".join(f"{_diagnostic_text(item.key)}:{item.count}" for item in items) or "-"

def _optional_field(label: str, value: object) -> str:
    return f" {label}={text}" if (text := _diagnostic_text(value, fallback="")) else ""

def _diagnostic_text(value: object, *, fallback: str = "-", limit: int = 120) -> str:
    if value is None:
        return fallback
    printable = "".join(character if character.isprintable() else " " for character in str(value))
    normalized = " ".join(printable.split())
    return normalized[:limit] if normalized else fallback
