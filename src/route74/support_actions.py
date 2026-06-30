from __future__ import annotations

import shlex
from pathlib import Path

from route74.domain.reporting import report_window_by_key, report_window_for_profile
from route74.domain.runtime_sources import BOT_EVENT_KINDS, BOT_EVENT_USER_REPLY

DEFAULT_BOT_RUNTIME_HOURS = 24
DEFAULT_BOT_RUNTIME_LIMIT = 8
DEFAULT_BOT_LATENCY_HOURS = 24


def bot_latency_command(
    *,
    hours: int = DEFAULT_BOT_LATENCY_HOURS,
    profile_key: str | None = None,
    event_kind: str | None = None,
) -> str:
    _validate_positive_int("hours", hours)
    command = f"route74 runtime-latency --hours {hours}"
    if profile_key is None:
        if event_kind is None:
            return command
    else:
        profile = report_window_for_profile(profile_key).profile_key
        command = f"{command} --profile {profile}"
    if event_kind is not None:
        _validate_event_kind(event_kind)
        command = f"{command} --event-kind {event_kind}"
    return command


def support_report_command_for_profile(profile_key: str, event_kind: str | None = None) -> str:
    report_window_for_profile(profile_key)
    command = f"route74 support-report --profile {profile_key}"
    if event_kind in {None, BOT_EVENT_USER_REPLY}:
        return command
    _validate_event_kind(event_kind)
    return f"{command} --event-kind {event_kind}"


def support_snapshot_command_for_profile(profile_key: str) -> str:
    profile = report_window_for_profile(profile_key).profile_key
    return f"route74 support-snapshot --profile {profile}"


def support_report_command_for_window(window_key: str, event_kind: str | None = None) -> str:
    window = report_window_by_key(window_key)
    command = f"route74 support-report --window {window.key}"
    if event_kind in {None, BOT_EVENT_USER_REPLY}:
        return command
    _validate_event_kind(event_kind)
    return f"{command} --event-kind {event_kind}"


def forecast_readiness_command_for_profile(profile_key: str) -> str:
    window = report_window_for_profile(profile_key)
    return f"route74 forecast-readiness --window {window.key}"


def forecast_coverage_command_for_profile(profile_key: str) -> str:
    window = report_window_for_profile(profile_key)
    return f"route74 forecast-coverage --window {window.key}"


def forecast_coverage_command_for_window(window_key: str) -> str:
    window = report_window_by_key(window_key)
    return f"route74 forecast-coverage --window {window.key}"


def forecast_backtest_command_for_profile(profile_key: str) -> str:
    window = report_window_for_profile(profile_key)
    return f"route74 forecast-backtest --window {window.key}"


def forecast_backtest_command_for_window(window_key: str) -> str:
    window = report_window_by_key(window_key)
    return f"route74 forecast-backtest --window {window.key}"


def watch_state_command_for_path(path: Path) -> str:
    return f"route74 watch-state --path {shlex.quote(str(path))}"


def prediction_evaluate_command_for_profile(profile_key: str) -> str:
    window = report_window_for_profile(profile_key)
    return f"route74 prediction-evaluate --window {window.key}"


def prediction_evaluate_command_for_window(window_key: str) -> str:
    window = report_window_by_key(window_key)
    return f"route74 prediction-evaluate --window {window.key}"


def prediction_calibration_command_for_profile(profile_key: str) -> str:
    window = report_window_for_profile(profile_key)
    return f"route74 prediction-calibration --window {window.key}"


def prediction_calibration_command_for_window(window_key: str) -> str:
    window = report_window_by_key(window_key)
    return f"route74 prediction-calibration --window {window.key}"


def bot_runtime_command(
    *,
    hours: int = DEFAULT_BOT_RUNTIME_HOURS,
    limit: int = DEFAULT_BOT_RUNTIME_LIMIT,
    profile_key: str | None = None,
    event_kind: str | None = None,
) -> str:
    _validate_positive_int("hours", hours)
    _validate_positive_int("limit", limit)
    command = f"route74 runtime-events --hours {hours} --limit {limit}"
    if profile_key is not None:
        profile = report_window_for_profile(profile_key).profile_key
        command = f"{command} --profile {profile}"
    if event_kind is not None:
        _validate_event_kind(event_kind)
        command = f"{command} --event-kind {event_kind}"
    return command


def _validate_positive_int(label: str, value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label} must be a positive integer")


def _validate_event_kind(value: str) -> None:
    if not isinstance(value, str) or value not in BOT_EVENT_KINDS:
        allowed = ", ".join(sorted(BOT_EVENT_KINDS))
        raise ValueError(f"event kind must be one of: {allowed}")
