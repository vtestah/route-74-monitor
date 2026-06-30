from __future__ import annotations

import argparse
import json
import sqlite3
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from route74.cli.support_snapshot import build_support_snapshot, cmd_support_snapshot
from route74.domain.commute import DepartureSource
from route74.domain.prediction_sources import SOURCE_HISTORY_HEADWAY, SOURCE_TARGET_STOP_LIVE
from route74.domain.profiles import MORNING
from route74.domain.reporting import report_window_for_profile
from route74.domain.runtime_sources import BOT_EVENT_USER_REPLY, RUNTIME_SOURCE_WEB_APP
from route74.models import NOVOSIBIRSK_TZ
from route74.presenters.support_snapshot import format_support_snapshot
from route74.services.support_snapshot import SupportSnapshotService
from route74.storage import connect, init_db


def main() -> None:
    current_time = datetime(2026, 6, 4, 9, 0, tzinfo=NOVOSIBIRSK_TZ)
    _assert_empty_db_snapshot(current_time)
    _assert_snapshot_shows_latest_reply_change(current_time)
    _assert_watch_state_failure_controls_primary_action(current_time)
    _assert_watch_runtime_error_controls_primary_action(current_time)
    _assert_db_failure_is_sanitized(current_time)
    _assert_cli_support_snapshot_matches_service(current_time)
    print("OK | support snapshot smoke passed")


def _assert_empty_db_snapshot(current_time: datetime) -> None:
    with TemporaryDirectory() as temp_dir:
        snapshot = SupportSnapshotService(
            db_path=Path(temp_dir) / "support-snapshot.sqlite",
            watch_state_path=Path(temp_dir) / "bot-watches.json",
        ).build(MORNING, current_time=current_time)
        message = format_support_snapshot(snapshot)

    _assert_equal(snapshot.profile_key, "morning")
    _assert_equal(snapshot.window_key, "weekday_morning_09_12")
    _assert_equal(snapshot.status, "critical")
    _assert_equal(snapshot.primary_action, "route74 forecast-health")
    _assert_equal(snapshot.primary_issue.key if snapshot.primary_issue else "", "collector")
    _assert_equal(snapshot.snapshot_command, "route74 support-snapshot --profile morning")
    _assert_contains(message, "🧰 Разбор 74")
    _assert_contains(message, "📌 Статус: критично")
    _assert_contains(message, "🎯 Следующий шаг: route74 forecast-health")
    _assert_contains(message, "🔎 Почему: collector")
    _assert_contains(message, "collector")
    _assert_contains(message, "Быстрый снимок: route74 support-snapshot --profile morning")
    _assert_contains(message, "Полный отчёт: route74 support-report --profile morning")
    _assert_short_runtime_message(message)


def _assert_snapshot_shows_latest_reply_change(current_time: datetime) -> None:
    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "support-snapshot.sqlite"
        watch_state_path = Path(temp_dir) / "bot-watches.json"
        with connect(db_path) as connection:
            init_db(connection)
            _insert_runtime_prediction(
                connection,
                sampled_at=current_time - timedelta(minutes=10),
                source=SOURCE_HISTORY_HEADWAY,
                source_method="history",
                predicted_minutes=19,
                selected_departure_source=DepartureSource.YANDEX_HISTORY.value,
            )
            _insert_runtime_prediction(
                connection,
                sampled_at=current_time,
                source=SOURCE_TARGET_STOP_LIVE,
                source_method="getVehiclePredictionInfo",
                predicted_minutes=17,
                selected_departure_source=DepartureSource.YANDEX.value,
            )
        snapshot = SupportSnapshotService(db_path=db_path, watch_state_path=watch_state_path).build(
            MORNING,
            current_time=current_time,
        )
        message = format_support_snapshot(snapshot)

    _assert_contains(
        message,
        "🔁 С прошлого ответа: 74-й позже на 8 мин · источник история Яндекса -> Яндекс live",
    )
    _assert_short_runtime_message(message)


def _assert_watch_state_failure_controls_primary_action(current_time: datetime) -> None:
    with TemporaryDirectory() as temp_dir:
        watch_state_path = Path(temp_dir) / "bot-watches.json"
        watch_state_path.write_text("{bad-json", encoding="utf-8")
        snapshot = SupportSnapshotService(
            db_path=Path(temp_dir) / "support-snapshot.sqlite",
            watch_state_path=watch_state_path,
        ).build(MORNING, current_time=current_time)
        message = format_support_snapshot(snapshot)

    _assert_equal(snapshot.status, "critical")
    _assert_contains(snapshot.primary_action, "route74 watch-state --path")
    _assert_equal(snapshot.primary_issue.key if snapshot.primary_issue else "", "watch_state_file")
    _assert_contains(message, "🔎 Почему: watch_state_file")
    _assert_contains(message, "watch_state_file")
    _assert_contains(message, "file=unreadable")
    _assert_short_runtime_message(message)


def _assert_watch_runtime_error_controls_primary_action(current_time: datetime) -> None:
    with TemporaryDirectory() as temp_dir:
        watch_state_path = Path(temp_dir) / "bot-watches.json"
        watch_state_path.write_text(
            json.dumps(
                {
                    "1001": {
                        "profile_key": MORNING.key,
                        "walk_minutes": MORNING.default_walk_minutes,
                        "started_at": (current_time - timedelta(minutes=1)).isoformat(),
                        "next_poll_at": (current_time + timedelta(minutes=5)).isoformat(),
                        "early_sent": False,
                        "error_count": 2,
                        "last_error_type": "RuntimeError",
                        "last_error_at": (current_time - timedelta(seconds=30)).isoformat(),
                    }
                }
            ),
            encoding="utf-8",
        )
        snapshot = SupportSnapshotService(
            db_path=Path(temp_dir) / "support-snapshot.sqlite",
            watch_state_path=watch_state_path,
        ).build(MORNING, current_time=current_time)
        message = format_support_snapshot(snapshot)

    _assert_equal(snapshot.status, "critical")
    _assert_contains(snapshot.primary_action, "route74 watch-state --path")
    _assert_equal(snapshot.primary_issue.key if snapshot.primary_issue else "", "watch_state_runtime_error")
    _assert_contains(message, "🔎 Почему: watch_state_runtime_error")
    _assert_contains(message, "errors=2 watches=1")
    _assert_contains(message, "route74 watch-state --path")
    _assert_short_runtime_message(message)


def _assert_db_failure_is_sanitized(current_time: datetime) -> None:
    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "not-a-db"
        db_path.mkdir()
        snapshot = SupportSnapshotService(db_path=db_path).build(MORNING, current_time=current_time)
        message = format_support_snapshot(snapshot)

    _assert_equal(snapshot.status, "critical")
    _assert_equal(snapshot.primary_action, "route74 db-health")
    _assert_equal(snapshot.primary_issue.key if snapshot.primary_issue else "", "db_integrity")
    _assert_contains(message, "🔎 Почему: db_integrity")
    _assert_contains(message, "db_integrity")
    _assert_contains(message, "support snapshot failed")
    _assert_not_contains(message, str(db_path))
    _assert_short_runtime_message(message)


def _assert_cli_support_snapshot_matches_service(current_time: datetime) -> None:
    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "support-snapshot.sqlite"
        watch_state_path = Path(temp_dir) / "bot-watches.json"
        snapshot = build_support_snapshot(
            db_path,
            profile_key=MORNING.key,
            watch_state_path=watch_state_path,
            current_time=current_time,
        )
        expected = format_support_snapshot(snapshot)
        output = _support_snapshot_cli_output(
            db_path,
            profile=MORNING.key,
            watch_state_path=watch_state_path,
            current_time=current_time,
        )

    _assert_equal(snapshot.profile_key, MORNING.key)
    _assert_contains(expected, "🧰 Разбор 74")
    _assert_contains(output, "🧰 Разбор 74")
    _assert_contains(output, "🎯 Следующий шаг:")
    _assert_contains(output, "Быстрый снимок: route74 support-snapshot --profile morning")
    _assert_contains(output, "Полный отчёт: route74 support-report --profile morning")
    _assert_equal(output.strip(), expected)


def _assert_short_runtime_message(message: str) -> None:
    if len(message) >= 4000:
        raise AssertionError(f"support snapshot is too long for the runtime UI: {len(message)}")


def _support_snapshot_cli_output(
    db_path: Path,
    *,
    profile: str,
    watch_state_path: Path,
    current_time: datetime,
) -> str:
    output = StringIO()
    args = argparse.Namespace(
        db=db_path,
        profile=profile,
        hours=24,
        watch_state_path=watch_state_path,
    )
    with patch("route74.cli.support_snapshot.now_local", return_value=current_time):
        with redirect_stdout(output):
            cmd_support_snapshot(args)
    return output.getvalue()


def _insert_runtime_prediction(
    connection: sqlite3.Connection,
    *,
    sampled_at: datetime,
    source: str,
    source_method: str,
    predicted_minutes: int,
    selected_departure_source: str,
) -> None:
    raw_json = json.dumps(
        {
            "runtime_source": RUNTIME_SOURCE_WEB_APP,
            "event_kind": BOT_EVENT_USER_REPLY,
            "selected_departure_source": selected_departure_source,
            "urgency": "relax",
            "leave_in_minutes": max(0, predicted_minutes - MORNING.default_walk_minutes),
            "target_wait_minutes": 3 if source == SOURCE_TARGET_STOP_LIVE else 6,
            "history_scope": "profile_time" if source == SOURCE_HISTORY_HEADWAY else "",
            "history_report_window_key": _report_window_key() if source == SOURCE_HISTORY_HEADWAY else "",
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
            MORNING.key,
            sampled_at.isoformat(),
            _report_window_key(),
            source,
            source_method,
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


def _report_window_key() -> str:
    return report_window_for_profile(MORNING.key).key


def _assert_contains(text: str, expected: str) -> None:
    if expected not in text:
        raise AssertionError(f"expected {expected!r} in {text!r}")


def _assert_not_contains(text: str, unexpected: str) -> None:
    if unexpected in text:
        raise AssertionError(f"did not expect {unexpected!r} in {text!r}")


def _assert_equal(actual: object, expected: object) -> None:
    if actual != expected:
        raise AssertionError(f"expected {expected!r}, got {actual!r}")


if __name__ == "__main__":
    main()
