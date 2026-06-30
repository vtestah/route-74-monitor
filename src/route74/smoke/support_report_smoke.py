from __future__ import annotations

import argparse
import json
import sqlite3
from collections.abc import Callable
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from route74.cli.support_report import (
    SUPPORT_REPORT_DB_LABEL,
    build_support_report,
    cmd_support_report,
    format_support_report,
)
from route74.domain.prediction_sources import SOURCE_HISTORY_HEADWAY, SOURCE_TARGET_STOP_LIVE
from route74.domain.profiles import EVENING, MORNING
from route74.domain.runtime_sources import BOT_EVENT_USER_REPLY, BOT_EVENT_WATCH_EARLY, RUNTIME_SOURCE_WEB_APP
from route74.models import NOVOSIBIRSK_TZ
from route74.storage import BotInteractionEvent, connect, init_db, insert_bot_interaction_event
from route74.storage.forecast_health import ForecastCollectorHealth, ForecastHealthSummary, ForecastWindowHealth
from route74.storage.yandex_canary import YandexCanaryHealth
from route74.support_actions import watch_state_command_for_path
from route74.support_triage import TRIAGE_WARNING, SupportTriage, SupportTriageItem
from route74.watch_state import DEFAULT_WATCH_STATE_PATH


def main() -> None:
    current_time = datetime(2026, 6, 4, 9, 0, tzinfo=NOVOSIBIRSK_TZ)
    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "support-report.sqlite"
        watch_state_path = Path(temp_dir) / "web-watches.json"
        report = build_support_report(
            db_path,
            window_key="weekday_morning_09_12",
            hours=24,
            limit=3,
            watch_state_path=watch_state_path,
            current_time=current_time,
        )
        output = format_support_report(report)
        profile_output = _support_report_cli_output(db_path, profile="morning", watch_state_path=watch_state_path)

    _assert_equal(report.window_key, "weekday_morning_09_12")
    _assert_equal(report.profile_key, "morning")
    _assert_equal(report.event_kind, BOT_EVENT_USER_REPLY)
    _assert_contains(output, "support report window=weekday_morning_09_12 profile=morning")
    _assert_contains(output, f"db={SUPPORT_REPORT_DB_LABEL}")
    _assert_not_contains(output, str(db_path))
    _assert_contains(output, 'next="route74 forecast-health"')
    _assert_contains(output, "section=triage")
    _assert_contains(output, 'triage status=critical primary="route74 forecast-health"')
    _assert_contains(output, "- critical collector:")
    _assert_contains(output, 'action="route74 forecast-health"')
    _assert_contains(output, "warning forecast_window: window=weekday_morning_09_12 status=no_collector_runs")
    _assert_contains(output, "warning truth_window: window=weekday_morning_09_12 truth=insufficient")
    _assert_contains(output, "info bot_runtime_profile: profile=morning has no bot runtime predictions in 24h")
    _assert_contains(output, "section=monitor")
    monitor_text = _section_text(output, "monitor")
    _assert_contains(monitor_text, "monitor profile=morning status=critical")
    _assert_contains(monitor_text, "forecast_morning profile=morning")
    _assert_contains(monitor_text, "truth_morning profile=morning")
    _assert_not_contains(monitor_text, "forecast_evening")
    _assert_not_contains(monitor_text, "truth_evening")
    _assert_contains(output, "watch:ok")
    _assert_contains(output, "section=forecast-health")
    _assert_contains(output, "forecast health status=not_ready")
    _assert_contains(output, "section=forecast-readiness")
    _assert_contains(output, "forecast readiness profile=morning window=weekday_morning_09_12 at=09:00")
    _assert_contains(output, 'action="route74 forecast-readiness --window weekday_morning_09_12"')
    _assert_contains(output, "section=forecast-backtest")
    _assert_contains(output, "forecast backtest window=weekday_morning_09_12 profile=morning")
    _assert_contains(output, 'action="route74 forecast-backtest --window weekday_morning_09_12"')
    _assert_contains(output, "section=bot-latency")
    _assert_contains(output, "runtime latency profile=morning hours=24 events=0")
    _assert_contains(output, "section=watch-state")
    _assert_contains(output, "watch-state status=ok")
    _assert_contains(output, "info watch_state_file: file not created yet")
    _assert_contains(output, "section=bot-runtime")
    _assert_contains(output, "runtime events profile=morning event_kind=user_reply hours=24 predictions=0")
    _assert_contains(output, "section=prediction-calibration")
    _assert_contains(output, "prediction calibration window=weekday_morning_09_12")
    _assert_contains(profile_output, "support report window=weekday_morning_09_12 profile=morning")
    _assert_contains(profile_output, f"db={SUPPORT_REPORT_DB_LABEL}")
    _assert_contains(profile_output, "runtime events profile=morning event_kind=user_reply hours=24 predictions=0")
    _assert_unavailable_db_prints_sanitized_report(current_time)
    _assert_watch_state_failure_becomes_triage_action(current_time)
    _assert_watch_state_expired_is_reported(current_time)
    _assert_watch_state_runtime_error_is_reported(current_time)
    _assert_support_report_bot_runtime_stays_profile_scoped(current_time)
    _assert_support_report_bot_runtime_shows_reply_change(current_time)
    _assert_support_report_bot_runtime_honors_event_kind(current_time)
    _assert_support_report_bot_latency_stays_profile_scoped(current_time)
    _assert_support_report_triage_ignores_cross_profile_runtime_monitor_issue(current_time)
    _assert_failed_section_priority_is_shared_by_header_and_triage(current_time)
    _assert_section_failure_keeps_other_sections(current_time)
    _assert_triage_primary_controls_report_header(current_time)
    _assert_operator_triage_controls_report_header(current_time)
    _assert_support_report_forecast_coverage_action(current_time)
    _assert_support_report_integrity_gap_action(current_time)
    _assert_triage_failure_marks_report_critical(current_time)
    _assert_rejects(
        lambda: _support_report_cli_output(Path("unused.sqlite")),
        "support-report needs --window or --profile",
    )
    _assert_rejects(
        lambda: _support_report_cli_output(
            Path("unused.sqlite"),
            window="weekday_evening_19_22",
            profile="morning",
        ),
        "--profile morning conflicts with --window weekday_evening_19_22",
    )
    print("OK | support report smoke passed")


def _assert_contains(text: str, expected: str) -> None:
    if expected not in text:
        raise AssertionError(f"expected {expected!r} in {text!r}")


def _assert_not_contains(text: str, unexpected: str) -> None:
    if unexpected in text:
        raise AssertionError(f"did not expect {unexpected!r} in {text!r}")


def _assert_equal(actual: object, expected: object) -> None:
    if actual != expected:
        raise AssertionError(f"expected {expected!r}, got {actual!r}")


def _support_report_cli_output(
    db_path: Path,
    *,
    window: str | None = None,
    profile: str | None = None,
    event_kind: str = BOT_EVENT_USER_REPLY,
    watch_state_path: Path = DEFAULT_WATCH_STATE_PATH,
) -> str:
    output = StringIO()
    args = argparse.Namespace(
        db=db_path,
        window=window,
        profile=profile,
        event_kind=event_kind,
        hours=24,
        limit=3,
        watch_state_path=watch_state_path,
    )
    with redirect_stdout(output):
        cmd_support_report(args)
    return output.getvalue()


def _assert_unavailable_db_prints_sanitized_report(current_time: datetime) -> None:
    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "not-a-sqlite-file"
        db_path.mkdir()
        report = build_support_report(
            db_path,
            window_key="weekday_morning_09_12",
            hours=24,
            limit=3,
            watch_state_path=Path(temp_dir) / "web-watches.json",
            current_time=current_time,
        )
        output = format_support_report(report)

    _assert_equal(report.status, "critical")
    _assert_contains(output, 'next="route74 db-health"')
    _assert_contains(output, "section=triage")
    _assert_contains(output, 'triage status=critical primary="route74 db-health"')
    _assert_contains(output, "- critical support_report_db: section=db failed")
    _assert_contains(output, "section=db")
    _assert_contains(output, "section_error section=db")
    _assert_not_contains(output, str(db_path))
    _assert_not_contains(output, str(Path(temp_dir)))


def _assert_watch_state_failure_becomes_triage_action(current_time: datetime) -> None:
    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "support-report.sqlite"
        watch_state_path = Path(temp_dir) / "bot watches.json"
        watch_state_path.write_text("{not-json", encoding="utf-8")
        report = build_support_report(
            db_path,
            window_key="weekday_morning_09_12",
            hours=24,
            limit=3,
            watch_state_path=watch_state_path,
            current_time=current_time,
        )
        output = format_support_report(report)

    _assert_equal(report.status, "critical")
    expected_action = watch_state_command_for_path(watch_state_path)
    _assert_equal(report.next_action, expected_action)
    _assert_contains(output, f'next="{expected_action}"')
    _assert_contains(output, f'triage status=critical primary="{expected_action}"')
    _assert_contains(output, "watch:critical")
    _assert_contains(output, "- critical support_report_watch-state: section=watch-state failed")
    _assert_contains(output, "- critical watch_state_file: file=unreadable type=JSONDecodeError")
    _assert_contains(output, f'action="{expected_action}"')


def _assert_watch_state_runtime_error_is_reported(current_time: datetime) -> None:
    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "support-report.sqlite"
        watch_state_path = Path(temp_dir) / "bot watches.json"
        watch_state_path.write_text(
            json.dumps(
                {
                    "101": {
                        "profile_key": "morning",
                        "walk_minutes": 12,
                        "started_at": (current_time - timedelta(minutes=1)).isoformat(),
                        "next_poll_at": (current_time + timedelta(minutes=1)).isoformat(),
                        "early_sent": False,
                        "error_count": 2,
                        "last_error_type": "RuntimeError",
                        "last_error_at": current_time.isoformat(),
                    }
                }
            ),
            encoding="utf-8",
        )
        report = build_support_report(
            db_path,
            window_key="weekday_morning_09_12",
            hours=24,
            limit=3,
            watch_state_path=watch_state_path,
            current_time=current_time,
        )
        output = format_support_report(report)

    expected_action = watch_state_command_for_path(watch_state_path)
    _assert_contains(output, "watch-state status=warning")
    _assert_contains(output, "runtime_errors=2")
    _assert_contains(output, "- warning watch_state_runtime_error: errors=2 watches=1")
    _assert_contains(output, f'action="{expected_action}"')
    _assert_not_contains(output, "101")


def _assert_watch_state_expired_is_reported(current_time: datetime) -> None:
    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "support-report.sqlite"
        watch_state_path = Path(temp_dir) / "bot watches.json"
        watch_state_path.write_text(
            json.dumps(
                {
                    "202": {
                        "profile_key": "morning",
                        "walk_minutes": 12,
                        "started_at": (current_time - timedelta(minutes=31)).isoformat(),
                        "next_poll_at": current_time.isoformat(),
                        "early_sent": False,
                    }
                }
            ),
            encoding="utf-8",
        )
        report = build_support_report(
            db_path,
            window_key="weekday_morning_09_12",
            hours=24,
            limit=3,
            watch_state_path=watch_state_path,
            current_time=current_time,
        )
        output = format_support_report(report)

    _assert_contains(output, "watch-state status=degraded")
    _assert_contains(output, "- info watch_state_expired: 1 stale records ignored")
    _assert_contains(output, "- degraded watch_state_empty: no active watches")


def _assert_support_report_bot_runtime_stays_profile_scoped(current_time: datetime) -> None:
    sampled_at = current_time - timedelta(minutes=20)
    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "support-report.sqlite"
        watch_state_path = Path(temp_dir) / "web-watches.json"
        with connect(db_path) as connection:
            init_db(connection)
            _insert_runtime_prediction(
                connection,
                sampled_at=sampled_at,
                profile_key=MORNING.key,
                report_window_key="weekday_morning_09_12",
                source=SOURCE_TARGET_STOP_LIVE,
                source_method="vehicle_prediction",
                selected_departure_source="yandex",
            )
            _insert_runtime_prediction(
                connection,
                sampled_at=sampled_at + timedelta(minutes=1),
                profile_key=EVENING.key,
                report_window_key="weekday_evening_19_22",
                source=SOURCE_HISTORY_HEADWAY,
                source_method="history",
                selected_departure_source="yandex_history",
            )
            connection.commit()
        report = build_support_report(
            db_path,
            window_key="weekday_morning_09_12",
            hours=24,
            limit=3,
            watch_state_path=watch_state_path,
            current_time=current_time,
        )
        output = format_support_report(report)

    runtime_text = _section_text(output, "bot-runtime")
    _assert_contains(runtime_text, "runtime events profile=morning event_kind=user_reply hours=24 predictions=1")
    _assert_contains(runtime_text, "profiles=morning:1")
    _assert_contains(runtime_text, "source=target_stop_live/vehicle_prediction")
    _assert_not_contains(runtime_text, "profile=evening")
    _assert_not_contains(runtime_text, "source=history_headway/history")


def _assert_support_report_bot_runtime_shows_reply_change(current_time: datetime) -> None:
    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "support-report.sqlite"
        watch_state_path = Path(temp_dir) / "web-watches.json"
        sampled_at = current_time - timedelta(minutes=20)
        with connect(db_path) as connection:
            init_db(connection)
            _insert_runtime_prediction(
                connection,
                sampled_at=sampled_at - timedelta(minutes=10),
                profile_key=MORNING.key,
                report_window_key="weekday_morning_09_12",
                source=SOURCE_HISTORY_HEADWAY,
                source_method="history",
                selected_departure_source="yandex_history",
                predicted_minutes=19,
            )
            _insert_runtime_prediction(
                connection,
                sampled_at=sampled_at,
                profile_key=MORNING.key,
                report_window_key="weekday_morning_09_12",
                source=SOURCE_TARGET_STOP_LIVE,
                source_method="vehicle_prediction",
                selected_departure_source="yandex",
                predicted_minutes=17,
            )
            _insert_runtime_prediction(
                connection,
                sampled_at=sampled_at + timedelta(minutes=1),
                profile_key=MORNING.key,
                report_window_key="weekday_morning_09_12",
                source=SOURCE_HISTORY_HEADWAY,
                source_method="history",
                selected_departure_source="yandex_history",
                event_kind=BOT_EVENT_WATCH_EARLY,
                predicted_minutes=24,
            )
            connection.commit()
        report = build_support_report(
            db_path,
            window_key="weekday_morning_09_12",
            hours=24,
            limit=2,
            watch_state_path=watch_state_path,
            current_time=current_time,
        )
        output = format_support_report(report)

    runtime_text = _section_text(output, "bot-runtime")
    _assert_contains(runtime_text, "runtime events profile=morning event_kind=user_reply hours=24 predictions=2")
    _assert_contains(runtime_text, "event_kinds=user_reply:2")
    _assert_not_contains(runtime_text, "event=watch_early")
    _assert_contains(runtime_text, "change=74-й позже на 8 мин · источник история Яндекса -> Яндекс live")


def _assert_support_report_bot_runtime_honors_event_kind(current_time: datetime) -> None:
    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "support-report.sqlite"
        watch_state_path = Path(temp_dir) / "web-watches.json"
        sampled_at = current_time - timedelta(minutes=20)
        with connect(db_path) as connection:
            init_db(connection)
            _insert_runtime_prediction(
                connection,
                sampled_at=sampled_at,
                profile_key=MORNING.key,
                report_window_key="weekday_morning_09_12",
                source=SOURCE_TARGET_STOP_LIVE,
                source_method="vehicle_prediction",
                selected_departure_source="yandex",
                predicted_minutes=17,
            )
            _insert_runtime_prediction(
                connection,
                sampled_at=sampled_at + timedelta(minutes=1),
                profile_key=MORNING.key,
                report_window_key="weekday_morning_09_12",
                source=SOURCE_HISTORY_HEADWAY,
                source_method="history",
                selected_departure_source="yandex_history",
                event_kind=BOT_EVENT_WATCH_EARLY,
                predicted_minutes=24,
            )
            connection.commit()
        report = build_support_report(
            db_path,
            window_key="weekday_morning_09_12",
            event_kind=BOT_EVENT_WATCH_EARLY,
            hours=24,
            limit=3,
            watch_state_path=watch_state_path,
            current_time=current_time,
        )
        output = format_support_report(report)
        with patch("route74.cli.support_report.now_local", return_value=current_time):
            cli_output = _support_report_cli_output(
                db_path,
                profile="morning",
                event_kind=BOT_EVENT_WATCH_EARLY,
                watch_state_path=watch_state_path,
            )

    _assert_equal(report.event_kind, BOT_EVENT_WATCH_EARLY)
    _assert_contains(output, "event_kind=watch_early")
    runtime_text = _section_text(output, "bot-runtime")
    _assert_contains(runtime_text, "runtime events profile=morning event_kind=watch_early hours=24 predictions=1")
    _assert_contains(runtime_text, "event_kinds=watch_early:1")
    _assert_contains(runtime_text, "source=history_headway/history")
    _assert_not_contains(runtime_text, "event=user_reply")
    _assert_not_contains(runtime_text, "source=target_stop_live/vehicle_prediction")
    _assert_contains(cli_output, "event_kind=watch_early")
    _assert_contains(cli_output, "runtime events profile=morning event_kind=watch_early hours=24 predictions=1")


def _assert_support_report_bot_latency_stays_profile_scoped(current_time: datetime) -> None:
    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "support-report.sqlite"
        watch_state_path = Path(temp_dir) / "web-watches.json"
        with connect(db_path) as connection:
            init_db(connection)
            for offset, chat_id in enumerate((201, 202, 203)):
                insert_bot_interaction_event(
                    connection,
                    _bot_latency_event(
                        current_time + timedelta(minutes=offset),
                        chat_id=chat_id,
                        profile_key=MORNING.key,
                        reply_source="no_eta",
                        yandex_source_method="none",
                    ),
                )
            insert_bot_interaction_event(
                connection,
                _bot_latency_event(
                    current_time + timedelta(minutes=3),
                    chat_id=204,
                    profile_key=EVENING.key,
                    reply_source="yandex",
                    yandex_source_method="vehicle_prediction",
                ),
            )
            connection.commit()
        report = build_support_report(
            db_path,
            window_key="weekday_morning_09_12",
            hours=24,
            limit=3,
            watch_state_path=watch_state_path,
            current_time=current_time + timedelta(minutes=4),
        )
        output = format_support_report(report)

    _assert_equal(report.next_action, "route74 runtime-latency --hours 24 --profile morning --event-kind user_reply")
    _assert_contains(output, 'next="route74 runtime-latency --hours 24 --profile morning --event-kind user_reply"')
    _assert_contains(
        output,
        'triage status=critical primary="route74 runtime-latency --hours 24 --profile morning --event-kind user_reply"',
    )
    latency_text = _section_text(output, "bot-latency")
    monitor_text = _section_text(output, "monitor")
    _assert_contains(latency_text, "runtime latency profile=morning hours=24 events=3")
    _assert_contains(latency_text, "no_eta=3(100%)")
    _assert_contains(latency_text, "no_eta_reasons=yandex_no_target+history_insufficient:3")
    _assert_contains(monitor_text, "bot_events:3")
    _assert_contains(monitor_text, "bot_no_eta:3(100%)")
    _assert_contains(monitor_text, "top_reason=yandex_no_target+history_insufficient")
    _assert_not_contains(latency_text, "vehicle_prediction:1")


def _assert_support_report_triage_ignores_cross_profile_runtime_monitor_issue(current_time: datetime) -> None:
    sampled_at = current_time - timedelta(minutes=180)
    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "support-report.sqlite"
        watch_state_path = Path(temp_dir) / "web-watches.json"
        with connect(db_path) as connection:
            init_db(connection)
            for offset in range(3):
                _insert_runtime_prediction(
                    connection,
                    sampled_at=sampled_at + timedelta(minutes=offset),
                    profile_key=MORNING.key,
                    report_window_key="weekday_morning_09_12",
                    source=SOURCE_TARGET_STOP_LIVE,
                    source_method="vehicle_prediction",
                    selected_departure_source="yandex",
                )
            connection.commit()
        report = build_support_report(
            db_path,
            window_key="weekday_evening_19_22",
            hours=24,
            limit=3,
            watch_state_path=watch_state_path,
            current_time=current_time,
        )
        output = format_support_report(report)

    monitor_text = _section_text(output, "monitor")
    triage_text = _section_text(output, "triage")
    _assert_contains(monitor_text, "monitor profile=evening")
    _assert_not_contains(monitor_text, "bot_runtime_pending profile=morning")
    _assert_not_contains(triage_text, "bot_runtime_pending")
    _assert_contains(triage_text, "bot_runtime_profile: profile=evening has no bot runtime predictions in 24h")


def _assert_failed_section_priority_is_shared_by_header_and_triage(current_time: datetime) -> None:
    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "support-report.sqlite"
        expected_action = "route74 runtime-latency --hours 24 --profile morning --event-kind user_reply"
        expected_readiness_action = "route74 forecast-readiness --window weekday_morning_09_12"
        with (
            patch(
                "route74.cli.support_report.format_forecast_readiness_summary",
                side_effect=RuntimeError("forecast readiness formatter failed"),
            ),
            patch(
                "route74.cli.support_report.format_bot_latency_summary",
                side_effect=RuntimeError("bot latency formatter failed"),
            ),
        ):
            report = build_support_report(
                db_path,
                window_key="weekday_morning_09_12",
                hours=24,
                limit=3,
                watch_state_path=Path(temp_dir) / "web-watches.json",
                current_time=current_time,
            )
        output = format_support_report(report)

    _assert_equal(report.status, "critical")
    _assert_equal(report.next_action, expected_action)
    _assert_contains(output, f'next="{expected_action}"')
    _assert_contains(output, f'triage status=critical primary="{expected_action}"')
    _assert_contains(
        output,
        f'- critical support_report_forecast-readiness: section=forecast-readiness failed action="{expected_readiness_action}"',
    )
    _assert_contains(
        output,
        f'- critical support_report_bot-latency: section=bot-latency failed action="{expected_action}"',
    )


def _assert_section_failure_keeps_other_sections(current_time: datetime) -> None:
    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "support-report.sqlite"
        raw_path = Path(temp_dir) / "token-leak"
        token = "local-secret-value"
        error = RuntimeError(f"failed at {raw_path} token={token}")
        with patch("route74.cli.support_report.format_bot_latency_summary", side_effect=error):
            report = build_support_report(
                db_path,
                window_key="weekday_morning_09_12",
                hours=24,
                limit=3,
                watch_state_path=Path(temp_dir) / "web-watches.json",
                current_time=current_time,
            )
        output = format_support_report(report)

    _assert_equal(report.status, "critical")
    _assert_contains(output, 'next="route74 runtime-latency --hours 24 --profile morning --event-kind user_reply"')
    _assert_contains(output, "section=monitor")
    _assert_contains(output, "section=forecast-health")
    _assert_contains(output, "section=bot-latency")
    _assert_contains(output, "section_error section=bot-latency type=RuntimeError")
    _assert_contains(output, "- critical support_report_bot-latency: section=bot-latency failed")
    _assert_contains(output, 'action="route74 runtime-latency --hours 24 --profile morning --event-kind user_reply"')
    _assert_contains(output, "section=watch-state")
    _assert_contains(output, "section=bot-runtime")
    _assert_contains(output, "section=prediction-calibration")
    _assert_not_contains(output, str(raw_path))
    _assert_not_contains(output, token)
    _assert_contains(output, "<path>")
    _assert_contains(output, "token=<redacted>")


def _assert_triage_primary_controls_report_header(current_time: datetime) -> None:
    primary_action = "route74 prediction-calibration --window weekday_morning_09_12"
    triage = SupportTriage(
        status=TRIAGE_WARNING,
        primary_action=primary_action,
        items=(
            SupportTriageItem(
                TRIAGE_WARNING,
                "truth_window",
                "window=weekday_morning_09_12 truth=warming_up",
                primary_action,
            ),
        ),
    )
    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "support-report.sqlite"
        with patch("route74.cli.support_report.build_support_triage", return_value=triage):
            report = build_support_report(
                db_path,
                window_key="weekday_morning_09_12",
                hours=24,
                limit=3,
                watch_state_path=Path(temp_dir) / "web-watches.json",
                current_time=current_time,
            )
        output = format_support_report(report)

    _assert_equal(report.status, TRIAGE_WARNING)
    _assert_equal(report.next_action, primary_action)
    _assert_contains(output, f'next="{primary_action}"')
    _assert_contains(output, f'triage status=warning primary="{primary_action}"')


def _assert_operator_triage_controls_report_header(current_time: datetime) -> None:
    watch_state_path = Path("data/web_watches.json")
    primary_action = watch_state_command_for_path(watch_state_path)
    triage = SupportTriage(
        status=TRIAGE_WARNING,
        primary_action="route74 forecast-health",
        items=(
            SupportTriageItem(
                TRIAGE_WARNING,
                "collector",
                "collector has no recent runs",
                "route74 forecast-health",
            ),
            SupportTriageItem(
                TRIAGE_WARNING,
                "watch_state_runtime_error",
                "errors=1 watches=1",
                primary_action,
            ),
        ),
    )
    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "support-report.sqlite"
        with patch("route74.cli.support_report.build_support_triage", return_value=triage):
            report = build_support_report(
                db_path,
                window_key="weekday_morning_09_12",
                hours=24,
                limit=3,
                watch_state_path=watch_state_path,
                current_time=current_time,
            )
        output = format_support_report(report)

    _assert_equal(report.status, TRIAGE_WARNING)
    _assert_equal(report.next_action, primary_action)
    _assert_contains(output, f'next="{primary_action}"')
    _assert_contains(output, f'triage status=warning primary="{primary_action}"')
    _assert_contains(output, f'- warning watch_state_runtime_error: errors=1 watches=1 action="{primary_action}"')


def _assert_support_report_forecast_coverage_action(current_time: datetime) -> None:
    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "support-report.sqlite"
        window_key = "weekday_morning_09_12"
        coverage_action = f"route74 forecast-coverage --window {window_key}"
        fake_forecast = ForecastHealthSummary(
            days=7,
            min_samples=3,
            min_distinct_days=2,
            collector=ForecastCollectorHealth(
                name="yandex-collect",
                status="ok",
                message="collector ok",
                updated_at=None,
                age_seconds=None,
                max_age_seconds=120,
            ),
            canary=YandexCanaryHealth(
                status="warning", latest_checked_at=None, risk_reason="canary warning", risky_runs=1
            ),
            windows=(
                ForecastWindowHealth(
                    window_key=window_key,
                    profile_key="morning",
                    status="insufficient_bucket_coverage",
                    reason="missing samples",
                    total_samples=0,
                    eta_samples=0,
                    fresh_eta_samples=0,
                    traffic_samples=0,
                    ready_buckets=0,
                    total_buckets=1,
                    forecast_without_report_samples=0,
                    report_without_forecast_samples=0,
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
                    truth_status="insufficient",
                    truth_reason="missing samples",
                    latest_arrival_at=None,
                    collector_latest_started_at=None,
                    missing_bucket_labels=("bucket",),
                    bucket_gaps=(),
                    latest_sampled_at=None,
                ),
            ),
        )
        fake_triage = SupportTriage(
            status=TRIAGE_WARNING,
            primary_action=coverage_action,
            items=(
                SupportTriageItem(
                    TRIAGE_WARNING,
                    "forecast_window",
                    f"window={window_key} status=insufficient_bucket_coverage",
                    coverage_action,
                ),
            ),
        )
        with (
            patch(
                "route74.cli.support_report.summarize_monitor",
                return_value=SimpleNamespace(forecast=fake_forecast, latency=None, runtime=None),
            ),
            patch("route74.cli.support_report.format_monitor_summary", return_value="monitor mock"),
            patch("route74.cli.support_report.summarize_forecast_health", return_value=fake_forecast),
            patch(
                "route74.cli.support_report.summarize_yandex_forecast_readiness",
                return_value=SimpleNamespace(ready=False),
            ),
            patch(
                "route74.cli.support_report.format_forecast_health_summary",
                return_value="forecast health mock",
            ),
            patch(
                "route74.cli.support_report.format_forecast_readiness_summary", return_value="forecast readiness mock"
            ),
            patch("route74.cli.support_report.build_support_triage", return_value=fake_triage),
            patch(
                "route74.cli.support_report.summarize_watch_state",
                return_value=SimpleNamespace(
                    status="ok",
                    file_status="missing",
                    path=Path(temp_dir) / "web-watches.json",
                    active_count=0,
                    due_count=0,
                    overdue_count=0,
                    expired_records=0,
                    invalid_records=0,
                    total_records=0,
                    early_sent_count=0,
                    oldest_age_minutes=None,
                    next_poll_at=None,
                    max_overdue_seconds=None,
                    runtime_error_count=0,
                    runtime_error_records=0,
                    latest_error_at=None,
                    runtime_error_types=(),
                    profiles=(),
                ),
            ),
            patch("route74.cli.support_report.format_watch_state_summary", return_value="watch-state mock"),
        ):
            report = build_support_report(
                db_path,
                window_key=window_key,
                hours=24,
                limit=3,
                watch_state_path=Path(temp_dir) / "web-watches.json",
                current_time=current_time,
            )
        output = format_support_report(report)

    _assert_contains(output, f'coverage_action="{coverage_action}"')
    _assert_contains(output, f'triage status=warning primary="{coverage_action}"')


def _assert_support_report_integrity_gap_action(current_time: datetime) -> None:
    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "support-report.sqlite"
        window_key = "weekday_morning_09_12"
        forecast_action = "route74 forecast-health"
        fake_forecast = ForecastHealthSummary(
            days=7,
            min_samples=3,
            min_distinct_days=2,
            collector=ForecastCollectorHealth(
                name="yandex-collect",
                status="ok",
                message="collector ok",
                updated_at=current_time,
                age_seconds=0,
                max_age_seconds=120,
            ),
            canary=YandexCanaryHealth(
                status="ok",
                latest_checked_at=current_time,
                risk_reason="latest canary runs are ok",
                risky_runs=0,
            ),
            windows=(
                ForecastWindowHealth(
                    window_key=window_key,
                    profile_key="morning",
                    status="integrity_gap",
                    reason="forecast/report-window tables disagree: forecast_only=3, report_only=1",
                    total_samples=12,
                    eta_samples=12,
                    fresh_eta_samples=12,
                    traffic_samples=12,
                    ready_buckets=2,
                    total_buckets=2,
                    forecast_without_report_samples=3,
                    report_without_forecast_samples=1,
                    collector_runs=1,
                    collector_eta_runs=1,
                    collector_traffic_ok_runs=1,
                    collector_run_statuses=(),
                    api_risk_samples=0,
                    api_risk_reasons=(),
                    coordinate_fallback_samples=0,
                    coordinate_fallback_reasons=(),
                    arrival_events=10,
                    prediction_events=10,
                    prediction_evaluations=10,
                    prediction_miss_cases=0,
                    bot_prediction_events=10,
                    bot_prediction_evaluations=10,
                    bot_prediction_miss_cases=0,
                    truth_status="ready",
                    truth_reason="enough truth events",
                    latest_arrival_at=current_time,
                    collector_latest_started_at=current_time,
                    missing_bucket_labels=(),
                    bucket_gaps=(),
                    latest_sampled_at=current_time,
                ),
            ),
        )
        fake_triage = SupportTriage(
            status=TRIAGE_WARNING,
            primary_action=forecast_action,
            items=(
                SupportTriageItem(
                    TRIAGE_WARNING,
                    "integrity_gap",
                    "forecast_only=3 report_only=1",
                    forecast_action,
                ),
            ),
        )
        with (
            patch(
                "route74.cli.support_report.summarize_monitor",
                return_value=SimpleNamespace(forecast=fake_forecast, latency=None, runtime=None),
            ),
            patch("route74.cli.support_report.format_monitor_summary", return_value="monitor mock"),
            patch(
                "route74.cli.support_report.summarize_yandex_forecast_readiness",
                return_value=SimpleNamespace(ready=False),
            ),
            patch(
                "route74.cli.support_report.format_forecast_readiness_summary", return_value="forecast readiness mock"
            ),
            patch("route74.cli.support_report.build_support_triage", return_value=fake_triage),
            patch(
                "route74.cli.support_report.summarize_watch_state",
                return_value=SimpleNamespace(
                    status="ok",
                    file_status="missing",
                    path=Path(temp_dir) / "web-watches.json",
                    active_count=0,
                    due_count=0,
                    overdue_count=0,
                    expired_records=0,
                    invalid_records=0,
                    total_records=0,
                    early_sent_count=0,
                    oldest_age_minutes=None,
                    next_poll_at=None,
                    max_overdue_seconds=None,
                    runtime_error_count=0,
                    runtime_error_records=0,
                    latest_error_at=None,
                    runtime_error_types=(),
                    profiles=(),
                ),
            ),
            patch("route74.cli.support_report.format_watch_state_summary", return_value="watch-state mock"),
        ):
            report = build_support_report(
                db_path,
                window_key=window_key,
                hours=24,
                limit=3,
                watch_state_path=Path(temp_dir) / "web-watches.json",
                current_time=current_time,
            )
        output = format_support_report(report)

    _assert_contains(output, f'next="{forecast_action}"')
    _assert_contains(output, f'triage status=warning primary="{forecast_action}"')
    _assert_contains(output, '- warning integrity_gap: forecast_only=3 report_only=1 action="route74 forecast-health"')
    _assert_contains(output, "forecast health status=not_ready")
    _assert_contains(output, "status=integrity_gap")
    _assert_contains(output, "integrity=3/1")


def _assert_triage_failure_marks_report_critical(current_time: datetime) -> None:
    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "support-report.sqlite"
        error = RuntimeError("triage formatter failed")
        with patch("route74.cli.support_report.build_support_triage", side_effect=error):
            report = build_support_report(
                db_path,
                window_key="weekday_morning_09_12",
                hours=24,
                limit=3,
                watch_state_path=Path(temp_dir) / "web-watches.json",
                current_time=current_time,
            )
        output = format_support_report(report)

    _assert_equal(report.status, "critical")
    _assert_contains(output, 'next="route74 support-report --window weekday_morning_09_12"')
    _assert_contains(output, "section=triage")
    _assert_contains(output, "section_error section=triage type=RuntimeError")
    _assert_contains(output, "section=monitor")
    _assert_contains(output, "section=forecast-health")


def _assert_rejects(factory: Callable[[], object], expected: str) -> None:
    try:
        factory()
    except SystemExit as error:
        message = str(error)
    else:
        raise AssertionError(f"expected {expected!r} failure")
    _assert_contains(message, expected)


def _bot_latency_event(
    received_at: datetime,
    *,
    chat_id: int,
    profile_key: str,
    reply_source: str,
    yandex_source_method: str,
    event_kind: str = BOT_EVENT_USER_REPLY,
) -> BotInteractionEvent:
    return BotInteractionEvent(
        received_at=received_at,
        chat_id=chat_id,
        update_type="message",
        command="🎯 Поймать 74",
        reply_source=reply_source,
        yandex_source_method=yandex_source_method,
        forecast_ms=20,
        render_ms=1,
        send_ms=1,
        total_ms=22,
        status="ok",
        event_kind=event_kind,
        profile_key=profile_key,
        no_eta_reason="yandex_no_target+history_insufficient" if reply_source == "no_eta" else "",
    )


def _insert_runtime_prediction(
    connection: sqlite3.Connection,
    *,
    sampled_at: datetime,
    profile_key: str,
    report_window_key: str,
    source: str,
    source_method: str,
    selected_departure_source: str,
    event_kind: str = BOT_EVENT_USER_REPLY,
    predicted_minutes: int | None = None,
) -> None:
    if predicted_minutes is None:
        predicted_minutes = 14 if source == SOURCE_TARGET_STOP_LIVE else 26
    raw_json = json.dumps(
        {
            "runtime_source": RUNTIME_SOURCE_WEB_APP,
            "event_kind": event_kind,
            "selected_departure_source": selected_departure_source,
            "urgency": "relax",
            "leave_in_minutes": max(0, predicted_minutes - 12),
            "target_wait_minutes": 3 if source == SOURCE_TARGET_STOP_LIVE else 6,
            "history_scope": "profile_time" if source == SOURCE_HISTORY_HEADWAY else "",
            "history_report_window_key": report_window_key if source == SOURCE_HISTORY_HEADWAY else "",
            "history_sample_count": 24 if source == SOURCE_HISTORY_HEADWAY else None,
            "history_bucket_minutes": 30 if source == SOURCE_HISTORY_HEADWAY else None,
            "history_percentile": 80 if source == SOURCE_HISTORY_HEADWAY else None,
            "yandex_status": "ok",
            "eta_factors": [],
            "warning": "",
        },
        ensure_ascii=False,
    )
    connection.execute(
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
            report_window_key,
            source,
            source_method,
            predicted_minutes,
            (sampled_at + timedelta(minutes=predicted_minutes)).isoformat(),
            "medium",
            "",
            "",
            "none",
            "not_collected",
            None,
            RUNTIME_SOURCE_WEB_APP,
            raw_json,
        ),
    )


def _section_text(output: str, key: str) -> str:
    marker = f"section={key}"
    start = output.find(marker)
    if start < 0:
        raise AssertionError(f"expected {marker!r} section")
    next_section = output.find("\nsection=", start + len(marker))
    return output[start:] if next_section < 0 else output[start:next_section]


if __name__ == "__main__":
    main()
