from __future__ import annotations

import argparse
from collections.abc import Mapping
from datetime import datetime

from route74.cli.common import positive_int
from route74.diagnostics import sanitize_diagnostic_text
from route74.domain.commute_change import DepartureChange
from route74.domain.profiles import PROFILE_KEYS
from route74.domain.runtime_sources import BOT_EVENT_KINDS, BOT_EVENT_USER_REPLY
from route74.models import now_local
from route74.presenters.commute_change import format_departure_change_details
from route74.presenters.eta_factors import format_eta_factor_payload_texts
from route74.presenters.runtime import runtime_prediction_source_text
from route74.services.commute_change import build_runtime_prediction_change_map
from route74.storage import (
    BotRuntimeCalibration,
    BotRuntimeCalibrationGroup,
    BotRuntimePrediction,
    BotRuntimePredictionQuality,
    BotRuntimePredictionQualityGroup,
    connect,
    init_db,
    load_recent_bot_runtime_predictions,
    summarize_bot_runtime_calibration,
    summarize_bot_runtime_predictions,
)
from route74.support_actions import prediction_calibration_command_for_profile


def register_bot_runtime_command(subparsers: argparse._SubParsersAction) -> None:
    runtime = subparsers.add_parser("runtime-events", help="Show web runtime prediction quality and recent events.")
    runtime.add_argument("--hours", type=positive_int, default=24, help="Summary window in hours.")
    runtime.add_argument(
        "--limit",
        type=positive_int,
        default=8,
        help="Recent runtime decisions to show.",
    )
    runtime.add_argument(
        "--profile",
        choices=PROFILE_KEYS,
        default=None,
        help="Focus diagnostics on one commute profile.",
    )
    runtime.add_argument(
        "--event-kind",
        choices=sorted(BOT_EVENT_KINDS),
        default=None,
        help="Focus quality, calibration and recent events on one web runtime event kind.",
    )
    runtime.set_defaults(func=cmd_bot_runtime)


def cmd_bot_runtime(args: argparse.Namespace) -> None:
    current_time = now_local()
    with connect(args.db) as connection:
        init_db(connection)
        quality = summarize_bot_runtime_predictions(
            connection,
            current_time=current_time,
            hours=args.hours,
            profile_key=args.profile,
            event_kind=args.event_kind,
        )
        calibration = summarize_bot_runtime_calibration(
            connection,
            current_time=current_time,
            hours=args.hours,
            profile_key=args.profile,
            event_kind=args.event_kind,
        )
        recent = load_recent_bot_runtime_predictions(
            connection,
            current_time=current_time,
            hours=args.hours,
            limit=args.limit,
            profile_key=args.profile,
            event_kind=args.event_kind,
        )
        change_history = _load_change_history(connection, args=args, current_time=current_time)
    changes = build_runtime_prediction_change_map(
        recent,
        history_predictions=change_history,
        event_kind=args.event_kind or BOT_EVENT_USER_REPLY,
    )
    print(
        format_bot_runtime_summary(
            quality,
            recent,
            args.db,
            calibration=calibration,
            profile_key=args.profile,
            event_kind=args.event_kind,
            changes=changes,
        )
    )


def format_bot_runtime_summary(
    quality: BotRuntimePredictionQuality,
    recent: tuple[BotRuntimePrediction, ...],
    db_path: object,
    *,
    calibration: BotRuntimeCalibration | None = None,
    profile_key: str | None = None,
    event_kind: str | None = None,
    changes: Mapping[int, DepartureChange] | None = None,
) -> str:
    change_map = build_runtime_prediction_change_map(recent) if changes is None else changes
    latest = _datetime(quality.latest_sampled_at)
    latest_evaluated = _datetime(quality.latest_evaluated_at)
    oldest_pending = _datetime(quality.oldest_pending_sampled_at)
    scope_parts = []
    if profile_key:
        scope_parts.append(f"profile={_text(profile_key)}")
    if event_kind:
        scope_parts.append(f"event_kind={_text(event_kind)}")
    scope = f" {' '.join(scope_parts)}" if scope_parts else ""
    lines = [
        (
            f"runtime events{scope} hours={quality.hours} predictions={quality.total} "
            f"evaluated={quality.evaluated}({quality.evaluated_percent}%) "
            f"pending={quality.pending}({quality.pending_percent}%) "
            f"misses={quality.misses}({quality.miss_rate_percent}%) "
            f"guardrail_unavailable={quality.guardrail_unavailable}"
            f"({quality.guardrail_unavailable_percent}%) "
            f"avg_error={_signed_minutes(quality.average_error_minutes)} "
            f"p50_abs_error={_minutes(quality.p50_abs_error_minutes)} "
            f"latest={latest} latest_eval={latest_evaluated} "
            f"oldest_pending={oldest_pending} db={_text(db_path)}"
        ),
        f"profiles={_quality_groups_text(quality.by_profile)}",
        f"sources={_quality_groups_text(quality.by_source)}",
        f"profile_sources={_quality_groups_text(quality.by_profile_source)}",
        f"event_kinds={_quality_groups_text(quality.by_event_kind)}",
    ]
    if calibration is not None:
        lines.extend(_calibration_lines(calibration))
        source_line = _source_calibration_line(calibration, profile_key)
        if source_line:
            lines.append(source_line)
    if not recent:
        lines.append("recent=-")
        return "\n".join(lines)
    lines.append("recent:")
    lines.extend(_recent_line(item, change_map.get(item.id)) for item in recent)
    return "\n".join(lines)


def _load_change_history(
    connection,
    *,
    args: argparse.Namespace,
    current_time: datetime,
) -> tuple[BotRuntimePrediction, ...]:
    event_kind = args.event_kind or BOT_EVENT_USER_REPLY
    return load_recent_bot_runtime_predictions(
        connection,
        current_time=current_time,
        hours=args.hours,
        limit=max(16, args.limit + 8),
        profile_key=args.profile,
        event_kind=event_kind,
    )


def _quality_groups_text(groups: tuple[BotRuntimePredictionQualityGroup, ...]) -> str:
    return ", ".join(_quality_group_text(group) for group in groups) or "-"


def _quality_group_text(group: BotRuntimePredictionQualityGroup) -> str:
    return (
        f"{_text(group.key)}:{group.total} "
        f"eval={group.evaluated}({group.evaluated_percent}%) "
        f"pending={group.pending}({group.pending_percent}%) "
        f"miss={group.misses}({group.miss_rate_percent}%) "
        f"guardrail={group.guardrail_unavailable}({group.guardrail_unavailable_percent}%) "
        f"p50={_minutes(group.p50_abs_error_minutes)}"
    )


def _calibration_lines(calibration: BotRuntimeCalibration) -> list[str]:
    return [
        (
            f"calibration=status={_text(calibration.status)} "
            f"suggested_buffer={_signed_minutes(calibration.suggested_buffer_minutes)} "
            f"p80_early={_minutes(calibration.p80_early_minutes)} "
            f"p50_extra_wait={_minutes(calibration.p50_extra_wait_minutes)} "
            f"action={_text(calibration.action)}"
        ),
        f"calibration_profiles={_calibration_groups_text(calibration.by_profile)}",
        f"calibration_sources={_calibration_groups_text(calibration.by_source)}",
        f"calibration_profile_sources={_calibration_groups_text(calibration.by_profile_source)}",
    ]


def _calibration_groups_text(groups: tuple[BotRuntimeCalibrationGroup, ...]) -> str:
    return ", ".join(_calibration_group_text(group) for group in groups) or "-"


def _calibration_group_text(group: BotRuntimeCalibrationGroup) -> str:
    return (
        f"{_text(group.key)}:{_text(group.status)} "
        f"eval={group.evaluated}/{group.total} "
        f"miss={group.misses}({group.miss_rate_percent}%) "
        f"suggested={_signed_minutes(group.suggested_buffer_minutes)}"
    )


def _source_calibration_line(calibration: BotRuntimeCalibration, profile_key: str | None) -> str:
    group = _profile_source_group(calibration.by_profile_source, profile_key)
    if group is None:
        return ""
    group_profile, group_source = _profile_source_key(group.key)
    source = runtime_prediction_source_text(group_source)
    command = _source_calibration_command(profile_key or group_profile, group.status)
    if group.status == "late_risk":
        return (
            f"source_risk={source} "
            f"eval={group.evaluated}/{group.total} "
            f"miss={group.misses}({group.miss_rate_percent}%) "
            f"p80_early={_minutes(group.p80_early_minutes)} "
            f"suggested={_signed_minutes(group.suggested_buffer_minutes)}"
            f"{command}"
        )
    if group.status == "extra_wait":
        return (
            f"source_wait={source} "
            f"eval={group.evaluated}/{group.total} "
            f"p50_extra_wait={_minutes(group.p50_extra_wait_minutes)} "
            f"suggested={_signed_minutes(group.suggested_buffer_minutes)}"
            f"{command}"
        )
    return ""


def _source_calibration_command(profile_key: str, status: str) -> str:
    if status not in {"late_risk", "extra_wait"}:
        return ""
    try:
        command = prediction_calibration_command_for_profile(profile_key)
    except ValueError:
        return ""
    return f" command={_text(command)}"


def _profile_source_group(
    groups: tuple[BotRuntimeCalibrationGroup, ...],
    profile_key: str | None,
) -> BotRuntimeCalibrationGroup | None:
    profile_groups = tuple(
        group
        for group in groups
        if profile_key is None or _profile_source_key(getattr(group, "key", ""))[0] == profile_key
    )
    if not profile_groups:
        return None
    return max(profile_groups, key=_source_calibration_priority)


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


def _recent_line(item: BotRuntimePrediction, change: DepartureChange | None) -> str:
    return (
        f"- {_datetime(item.sampled_at)} profile={_text(item.profile_key)} "
        f"window={_text(item.report_window_key)} event={_text(item.event_kind)} "
        f"selected={_text(item.selected_departure_source)} "
        f"source={_text(item.source)}/{_text(item.source_method)} "
        f"eta={_minutes(item.predicted_minutes)} confidence={_text(item.confidence)} "
        f"urgency={_text(item.urgency)} leave={_minutes(item.leave_in_minutes)} "
        f"wait={_minutes(item.target_wait_minutes)} history={_history_text(item)} "
        f"yandex={_text(item.yandex_status)} eval={_evaluation_text(item)} "
        f"{_change_text(change)}why={_explanation_text(item)}{_warning_suffix(item)}"
    )


def _history_text(item: BotRuntimePrediction) -> str:
    parts = []
    if item.history_scope:
        parts.append(_text(item.history_scope))
    if item.history_report_window_key:
        parts.append(f"window={_text(item.history_report_window_key)}")
    if item.history_sample_count is not None:
        parts.append(f"n={item.history_sample_count}")
    if item.history_bucket_minutes is not None:
        parts.append(f"bucket={item.history_bucket_minutes}m")
    if item.history_percentile is not None:
        parts.append(f"p{item.history_percentile}")
    return ",".join(parts) or "-"


def _evaluation_text(item: BotRuntimePrediction) -> str:
    if item.error_minutes is None:
        return "-"
    return (
        f"actual={_minutes(item.actual_minutes)},"
        f"error={_signed_minutes(item.error_minutes)},"
        f"at={_datetime(item.evaluated_at)}"
    )


def _explanation_text(item: BotRuntimePrediction) -> str:
    texts = format_eta_factor_payload_texts(item.eta_factors)
    return _text("; ".join(texts), limit=180)


def _warning_suffix(item: BotRuntimePrediction) -> str:
    if not item.warning:
        return ""
    return f" warning={_text(item.warning, limit=180)}"


def _change_text(change: DepartureChange | None) -> str:
    details = format_departure_change_details(change)
    if not details:
        return ""
    return f"change={_text(details, limit=160)} "


def _minutes(value: int | None) -> str:
    return "-" if value is None else f"{value}m"


def _signed_minutes(value: int | None) -> str:
    if value is None:
        return "-"
    prefix = "" if value < 0 else "+"
    return f"{prefix}{value}m"


def _datetime(value: datetime | None) -> str:
    return "-" if value is None else value.strftime("%Y-%m-%d %H:%M")


def _text(value: object, *, limit: int = 120) -> str:
    return sanitize_diagnostic_text(value, limit=limit)
