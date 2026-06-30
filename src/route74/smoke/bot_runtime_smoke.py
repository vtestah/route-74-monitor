from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from route74.cli.bot_runtime import format_bot_runtime_summary
from route74.domain.commute_change import DepartureChange
from route74.domain.prediction_sources import SOURCE_HISTORY_HEADWAY, SOURCE_TARGET_STOP_LIVE
from route74.domain.profiles import EVENING, MORNING
from route74.domain.runtime_sources import (
    BOT_EVENT_USER_REPLY,
    BOT_EVENT_WATCH_EARLY,
    RUNTIME_SOURCE_WEB_APP,
)
from route74.models import NOVOSIBIRSK_TZ
from route74.services.commute_change import build_runtime_prediction_change_map
from route74.storage import (
    connect,
    init_db,
    load_recent_bot_runtime_predictions,
    summarize_bot_runtime_calibration,
    summarize_bot_runtime_predictions,
)


def main() -> None:
    _assert_bot_runtime_diagnostics_include_quality_and_recent_events()
    _assert_bot_runtime_calibration_flags_late_risk()
    _assert_bot_runtime_profile_filter_focuses_section()
    _assert_bot_runtime_event_kind_filter_focuses_user_replies()
    _assert_bot_runtime_change_lines_compare_recent_user_replies()
    print("OK | bot runtime smoke passed")


def _assert_bot_runtime_diagnostics_include_quality_and_recent_events() -> None:
    sampled_at = datetime(2026, 6, 4, 8, 20, tzinfo=NOVOSIBIRSK_TZ)
    current_time = sampled_at + timedelta(minutes=10)
    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "bot-runtime.sqlite"
        with connect(db_path) as connection:
            init_db(connection)
            user_reply_id = _insert_runtime_prediction(
                connection,
                sampled_at=sampled_at,
                source=SOURCE_TARGET_STOP_LIVE,
                source_method="vehicle_prediction",
                predicted_minutes=14,
                event_kind=BOT_EVENT_USER_REPLY,
                urgency="go_now",
                selected_departure_source="yandex",
                history_scope="",
                warning="координатный прогноз, держу запас 2 мин",
                eta_factors=(
                    {
                        "kind": "safety_buffer",
                        "minutes": 2,
                        "sample_count": 12,
                        "percent": 35,
                        "scope": "source",
                    },
                    {
                        "kind": "ignored_weak_progress",
                        "minutes": 1,
                        "sample_count": 2,
                        "percent": 0,
                        "scope": "vehicle_progress",
                    },
                ),
            )
            _insert_evaluation(
                connection,
                prediction_id=user_reply_id,
                sampled_at=sampled_at,
                predicted_minutes=14,
                error_minutes=-2,
            )
            _insert_runtime_prediction(
                connection,
                sampled_at=sampled_at + timedelta(minutes=3),
                source=SOURCE_HISTORY_HEADWAY,
                source_method="history",
                predicted_minutes=24,
                event_kind=BOT_EVENT_WATCH_EARLY,
                urgency="relax",
                selected_departure_source="yandex_history",
                history_scope="profile_time",
                eta_factors=(
                    {"kind": "history_sample", "sample_count": 24},
                    {"kind": "guardrail_unavailable"},
                ),
            )
            _insert_runtime_prediction(
                connection,
                sampled_at=sampled_at - timedelta(hours=25),
                source=SOURCE_TARGET_STOP_LIVE,
                source_method="vehicle_prediction",
                predicted_minutes=18,
                event_kind=BOT_EVENT_USER_REPLY,
                urgency="wait",
                selected_departure_source="yandex",
                history_scope="",
                eta_factors=(),
            )
            _insert_runtime_prediction(
                connection,
                sampled_at=current_time + timedelta(minutes=5),
                source=SOURCE_TARGET_STOP_LIVE,
                source_method="vehicle_prediction",
                predicted_minutes=99,
                event_kind=BOT_EVENT_USER_REPLY,
                urgency="relax",
                selected_departure_source="yandex",
                history_scope="",
                eta_factors=(),
            )
            connection.commit()

            quality = summarize_bot_runtime_predictions(
                connection,
                current_time=current_time,
                hours=24,
            )
            calibration = summarize_bot_runtime_calibration(
                connection,
                current_time=current_time,
                hours=24,
            )
            recent = load_recent_bot_runtime_predictions(
                connection,
                current_time=current_time,
                hours=24,
                limit=2,
            )
            user_reply_quality = summarize_bot_runtime_predictions(
                connection,
                current_time=current_time,
                hours=24,
                event_kind=BOT_EVENT_USER_REPLY,
            )
            user_reply_calibration = summarize_bot_runtime_calibration(
                connection,
                current_time=current_time,
                hours=24,
                event_kind=BOT_EVENT_USER_REPLY,
            )
            user_reply_recent = load_recent_bot_runtime_predictions(
                connection,
                current_time=current_time,
                hours=24,
                limit=2,
                event_kind=BOT_EVENT_USER_REPLY,
            )
        output = format_bot_runtime_summary(
            quality,
            recent,
            db_path,
            calibration=calibration,
        )
        user_reply_output = format_bot_runtime_summary(
            user_reply_quality,
            user_reply_recent,
            db_path,
            calibration=user_reply_calibration,
            event_kind=BOT_EVENT_USER_REPLY,
        )
        changed_user_reply_output = format_bot_runtime_summary(
            user_reply_quality,
            user_reply_recent,
            db_path,
            calibration=user_reply_calibration,
            event_kind=BOT_EVENT_USER_REPLY,
            changes={
                user_reply_recent[0].id: DepartureChange(
                    previous_sampled_at=sampled_at - timedelta(minutes=6),
                    current_sampled_at=sampled_at,
                    previous_arrival_at=sampled_at + timedelta(minutes=9),
                    current_arrival_at=sampled_at + timedelta(minutes=14),
                    arrival_shift_minutes=5,
                    previous_source="yandex_history",
                    current_source="yandex",
                )
            },
        )

    _assert_equal(quality.total, 2)
    _assert_equal(quality.evaluated, 1)
    _assert_equal(quality.pending, 1)
    _assert_equal(quality.misses, 1)
    _assert_equal(quality.guardrail_unavailable, 1)
    _assert_equal(quality.evaluated_percent, 50)
    _assert_equal(quality.pending_percent, 50)
    _assert_equal(quality.miss_rate_percent, 100)
    _assert_equal(quality.guardrail_unavailable_percent, 50)
    _assert_equal(quality.by_profile[0].key, MORNING.key)
    _assert_equal(quality.by_profile[0].guardrail_unavailable, 1)
    _assert_equal(quality.by_profile_source[0].key, f"{MORNING.key}/{SOURCE_HISTORY_HEADWAY}")
    _assert_equal(recent[0].event_kind, BOT_EVENT_WATCH_EARLY)
    _assert_equal(recent[1].warning, "координатный прогноз, держу запас 2 мин")
    _assert_equal(tuple(item.predicted_minutes for item in recent), (24, 14))
    _assert_contains(output, "runtime events hours=24 predictions=2 evaluated=1(50%) pending=1(50%) misses=1(100%)")
    _assert_contains(output, "guardrail_unavailable=1(50%)")
    _assert_contains(output, "latest_eval=2026-06-04 08:20 oldest_pending=2026-06-04 08:23")
    _assert_contains(output, "calibration=status=insufficient")
    _assert_contains(output, "profiles=morning:2 eval=1(50%) pending=1(50%) miss=1(100%) guardrail=1(50%)")
    _assert_contains(output, "sources=history_headway:1 eval=0(0%) pending=1(100%)")
    _assert_contains(output, "profile_sources=morning/history_headway:1 eval=0(0%) pending=1(100%)")
    _assert_contains(output, "event_kinds=user_reply:1 eval=1(100%) pending=0(0%)")
    _assert_contains(output, "event=watch_early")
    _assert_contains(output, "source=history_headway/history")
    _assert_contains(output, "history=profile_time,window=weekday_morning_09_12,n=24,bucket=30m,p80")
    _assert_contains(output, "eval=actual=12m,error=-2m")
    _assert_contains(output, "why=история p80: 24 замера")
    _assert_contains(output, "warning=координатный прогноз, держу запас 2 мин")
    _assert_contains(output, "прошлые поправки недоступны")
    _assert_contains(output, "слабая координата на 1 мин раньше не выбрана, 2 замера")
    _assert_equal(user_reply_quality.total, 1)
    _assert_equal(user_reply_quality.pending, 0)
    _assert_equal(user_reply_quality.misses, 1)
    _assert_equal(tuple(item.event_kind for item in user_reply_recent), (BOT_EVENT_USER_REPLY,))
    _assert_contains(
        user_reply_output,
        "runtime events event_kind=user_reply hours=24 predictions=1 evaluated=1(100%) pending=0(0%)",
    )
    _assert_contains(user_reply_output, "event_kinds=user_reply:1 eval=1(100%) pending=0(0%)")
    assert_not_contains(user_reply_output, "event=watch_early")
    _assert_contains(changed_user_reply_output, "change=74-й позже на 5 мин · источник история Яндекса -> Яндекс live")


def _assert_bot_runtime_calibration_flags_late_risk() -> None:
    sampled_at = datetime(2026, 6, 4, 8, 20, tzinfo=NOVOSIBIRSK_TZ)
    current_time = sampled_at + timedelta(minutes=10)
    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "bot-runtime-calibration.sqlite"
        with connect(db_path) as connection:
            init_db(connection)
            for offset, error_minutes in enumerate((-2, -3, 1)):
                prediction_id = _insert_runtime_prediction(
                    connection,
                    sampled_at=sampled_at + timedelta(minutes=offset),
                    source=SOURCE_TARGET_STOP_LIVE,
                    source_method="vehicle_prediction",
                    predicted_minutes=14,
                    event_kind=BOT_EVENT_USER_REPLY,
                    urgency="go_now",
                    selected_departure_source="yandex",
                    history_scope="",
                    eta_factors=(),
                )
                _insert_evaluation(
                    connection,
                    prediction_id=prediction_id,
                    sampled_at=sampled_at + timedelta(minutes=offset),
                    predicted_minutes=14,
                    error_minutes=error_minutes,
                )
            connection.commit()
            quality = summarize_bot_runtime_predictions(
                connection,
                current_time=current_time,
                hours=24,
                event_kind=BOT_EVENT_USER_REPLY,
            )
            calibration = summarize_bot_runtime_calibration(
                connection,
                current_time=current_time,
                hours=24,
                event_kind=BOT_EVENT_USER_REPLY,
            )
            recent = load_recent_bot_runtime_predictions(connection, current_time=current_time, hours=24, limit=2)
        output = format_bot_runtime_summary(
            quality,
            recent,
            db_path,
            calibration=calibration,
            profile_key=MORNING.key,
        )

    _assert_equal(calibration.status, "late_risk")
    _assert_equal(calibration.suggested_buffer_minutes, 3)
    _assert_equal(calibration.by_profile[0].key, MORNING.key)
    _assert_equal(calibration.by_source[0].key, SOURCE_TARGET_STOP_LIVE)
    _assert_equal(calibration.by_profile_source[0].key, f"{MORNING.key}/{SOURCE_TARGET_STOP_LIVE}")
    _assert_contains(output, "calibration=status=late_risk suggested_buffer=+3m p80_early=3m")
    _assert_contains(output, "calibration_profiles=morning:late_risk eval=3/3 miss=2(67%) suggested=+3m")
    _assert_contains(output, "calibration_sources=target_stop_live:late_risk eval=3/3 miss=2(67%) suggested=+3m")
    _assert_contains(output, "calibration_profile_sources=morning/target_stop_live:late_risk")
    _assert_contains(
        output,
        "source_risk=Яндекс live eval=3/3 miss=2(67%) p80_early=3m suggested=+3m "
        "command=route74 prediction-calibration --window weekday_morning_09_12",
    )
    _assert_contains(output, "source_risk=Яндекс live")
    _assert_contains(output, "p80_early=3m")


def _assert_bot_runtime_profile_filter_focuses_section() -> None:
    sampled_at = datetime(2026, 6, 4, 8, 20, tzinfo=NOVOSIBIRSK_TZ)
    current_time = sampled_at + timedelta(minutes=10)
    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "bot-runtime-profile.sqlite"
        with connect(db_path) as connection:
            init_db(connection)
            _insert_runtime_prediction(
                connection,
                sampled_at=sampled_at,
                profile_key=MORNING.key,
                source=SOURCE_TARGET_STOP_LIVE,
                source_method="vehicle_prediction",
                predicted_minutes=14,
                event_kind=BOT_EVENT_USER_REPLY,
                urgency="go_now",
                selected_departure_source="yandex",
                history_scope="",
                eta_factors=(),
            )
            _insert_runtime_prediction(
                connection,
                sampled_at=sampled_at + timedelta(minutes=1),
                profile_key=EVENING.key,
                source=SOURCE_HISTORY_HEADWAY,
                source_method="history",
                predicted_minutes=26,
                event_kind=BOT_EVENT_USER_REPLY,
                urgency="relax",
                selected_departure_source="yandex_history",
                history_scope="profile_time",
                eta_factors=(),
            )
            connection.commit()

            quality = summarize_bot_runtime_predictions(
                connection,
                current_time=current_time,
                hours=24,
                profile_key=MORNING.key,
            )
            calibration = summarize_bot_runtime_calibration(
                connection,
                current_time=current_time,
                hours=24,
                profile_key=MORNING.key,
            )
            recent = load_recent_bot_runtime_predictions(
                connection,
                current_time=current_time,
                hours=24,
                limit=2,
                profile_key=MORNING.key,
            )
        output = format_bot_runtime_summary(
            quality,
            recent,
            db_path,
            calibration=calibration,
            profile_key=MORNING.key,
        )

    _assert_equal(quality.total, 1)
    _assert_equal(quality.by_profile[0].key, MORNING.key)
    _assert_equal(recent[0].profile_key, MORNING.key)
    _assert_contains(output, "runtime events profile=morning hours=24 predictions=1")
    assert_not_contains(output, "profile=evening")


def _assert_bot_runtime_event_kind_filter_focuses_user_replies() -> None:
    sampled_at = datetime(2026, 6, 4, 8, 20, tzinfo=NOVOSIBIRSK_TZ)
    current_time = sampled_at + timedelta(minutes=10)
    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "bot-runtime-event-kind.sqlite"
        with connect(db_path) as connection:
            init_db(connection)
            for offset, event_kind in enumerate((BOT_EVENT_USER_REPLY, BOT_EVENT_WATCH_EARLY)):
                prediction_id = _insert_runtime_prediction(
                    connection,
                    sampled_at=sampled_at + timedelta(minutes=offset),
                    source=SOURCE_TARGET_STOP_LIVE,
                    source_method="vehicle_prediction",
                    predicted_minutes=14,
                    event_kind=event_kind,
                    urgency="go_now",
                    selected_departure_source="yandex",
                    history_scope="",
                    eta_factors=(),
                )
                _insert_evaluation(
                    connection,
                    prediction_id=prediction_id,
                    sampled_at=sampled_at + timedelta(minutes=offset),
                    predicted_minutes=14,
                    error_minutes=-1,
                )
            connection.commit()
            quality = summarize_bot_runtime_predictions(
                connection,
                current_time=current_time,
                hours=24,
                event_kind=BOT_EVENT_USER_REPLY,
            )
            calibration = summarize_bot_runtime_calibration(
                connection,
                current_time=current_time,
                hours=24,
                min_evaluated=1,
                event_kind=BOT_EVENT_USER_REPLY,
            )
            recent = load_recent_bot_runtime_predictions(
                connection,
                current_time=current_time,
                hours=24,
                limit=4,
                event_kind=BOT_EVENT_USER_REPLY,
            )

    _assert_equal(quality.total, 1)
    _assert_equal(quality.by_event_kind[0].key, BOT_EVENT_USER_REPLY)
    _assert_equal(calibration.total, 1)
    _assert_equal(recent[0].event_kind, BOT_EVENT_USER_REPLY)


def _assert_bot_runtime_change_lines_compare_recent_user_replies() -> None:
    sampled_at = datetime(2026, 6, 4, 8, 20, tzinfo=NOVOSIBIRSK_TZ)
    current_time = sampled_at + timedelta(minutes=10)
    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "bot-runtime-change.sqlite"
        with connect(db_path) as connection:
            init_db(connection)
            _insert_runtime_prediction(
                connection,
                sampled_at=sampled_at - timedelta(minutes=10),
                source=SOURCE_HISTORY_HEADWAY,
                source_method="history",
                predicted_minutes=16,
                event_kind=BOT_EVENT_USER_REPLY,
                urgency="relax",
                selected_departure_source="yandex_history",
                history_scope="profile_time",
                eta_factors=(),
            )
            _insert_runtime_prediction(
                connection,
                sampled_at=sampled_at - timedelta(minutes=2),
                source=SOURCE_HISTORY_HEADWAY,
                source_method="history",
                predicted_minutes=30,
                event_kind=BOT_EVENT_WATCH_EARLY,
                urgency="relax",
                selected_departure_source="yandex_history",
                history_scope="profile_time",
                eta_factors=(),
            )
            _insert_runtime_prediction(
                connection,
                sampled_at=sampled_at,
                source=SOURCE_TARGET_STOP_LIVE,
                source_method="vehicle_prediction",
                predicted_minutes=14,
                event_kind=BOT_EVENT_USER_REPLY,
                urgency="go_now",
                selected_departure_source="yandex",
                history_scope="",
                eta_factors=(),
            )
            _insert_runtime_prediction(
                connection,
                sampled_at=sampled_at - timedelta(minutes=5),
                source=SOURCE_TARGET_STOP_LIVE,
                source_method="vehicle_prediction",
                predicted_minutes=2,
                event_kind=BOT_EVENT_WATCH_EARLY,
                urgency="go_now",
                selected_departure_source="yandex",
                history_scope="",
                eta_factors=(),
            )
            connection.commit()
            quality = summarize_bot_runtime_predictions(
                connection,
                current_time=current_time,
                hours=24,
                event_kind=BOT_EVENT_USER_REPLY,
            )
            calibration = summarize_bot_runtime_calibration(
                connection,
                current_time=current_time,
                hours=24,
                event_kind=BOT_EVENT_USER_REPLY,
            )
            watch_quality = summarize_bot_runtime_predictions(
                connection,
                current_time=current_time,
                hours=24,
                event_kind=BOT_EVENT_WATCH_EARLY,
            )
            watch_calibration = summarize_bot_runtime_calibration(
                connection,
                current_time=current_time,
                hours=24,
                event_kind=BOT_EVENT_WATCH_EARLY,
            )
            recent = load_recent_bot_runtime_predictions(
                connection,
                current_time=current_time,
                hours=24,
                limit=1,
                event_kind=BOT_EVENT_USER_REPLY,
            )
            history = load_recent_bot_runtime_predictions(
                connection,
                current_time=current_time,
                hours=24,
                limit=8,
            )
            watch_recent = load_recent_bot_runtime_predictions(
                connection,
                current_time=current_time,
                hours=24,
                limit=1,
                event_kind=BOT_EVENT_WATCH_EARLY,
            )
            watch_history = load_recent_bot_runtime_predictions(
                connection,
                current_time=current_time,
                hours=24,
                limit=8,
                event_kind=BOT_EVENT_WATCH_EARLY,
            )
        changes = build_runtime_prediction_change_map(recent, history_predictions=history)
        watch_changes = build_runtime_prediction_change_map(
            watch_recent,
            history_predictions=watch_history,
            event_kind=BOT_EVENT_WATCH_EARLY,
        )
        output = format_bot_runtime_summary(
            quality,
            recent,
            db_path,
            calibration=calibration,
            event_kind=BOT_EVENT_USER_REPLY,
            changes=changes,
        )
        watch_output = format_bot_runtime_summary(
            watch_quality,
            watch_recent,
            db_path,
            calibration=watch_calibration,
            event_kind=BOT_EVENT_WATCH_EARLY,
            changes=watch_changes,
        )

    _assert_equal(len(changes), 1)
    _assert_contains(output, "change=74-й позже на 8 мин · источник история Яндекса -> Яндекс live")
    _assert_contains(output, "event=user_reply")
    assert_not_contains(output, "event=watch_early")
    assert_not_contains(output, "раньше на")
    _assert_equal(len(watch_changes), 1)
    _assert_contains(watch_output, "runtime events event_kind=watch_early hours=24 predictions=2")
    _assert_contains(watch_output, "event_kinds=watch_early:2")
    _assert_contains(watch_output, "event=watch_early")
    _assert_contains(watch_output, "change=74-й позже на 31 мин · источник Яндекс live -> история Яндекса")


def _insert_runtime_prediction(
    connection: sqlite3.Connection,
    *,
    sampled_at: datetime,
    profile_key: str = MORNING.key,
    source: str,
    source_method: str,
    predicted_minutes: int,
    event_kind: str,
    urgency: str,
    selected_departure_source: str,
    history_scope: str,
    eta_factors: tuple[dict[str, object], ...],
    warning: str = "",
) -> int:
    raw_json = json.dumps(
        {
            "runtime_source": RUNTIME_SOURCE_WEB_APP,
            "event_kind": event_kind,
            "selected_departure_source": selected_departure_source,
            "urgency": urgency,
            "leave_in_minutes": max(0, predicted_minutes - 12),
            "target_wait_minutes": 3 if source != SOURCE_HISTORY_HEADWAY else 6,
            "history_scope": history_scope,
            "history_report_window_key": _report_window_key(profile_key) if history_scope else "",
            "history_sample_count": 24 if history_scope else None,
            "history_bucket_minutes": 30 if history_scope else None,
            "history_percentile": 80 if history_scope else None,
            "yandex_status": "ok",
            "eta_factors": list(eta_factors),
            "warning": warning,
        },
        ensure_ascii=False,
    )
    cursor = connection.execute(
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
            _report_window_key(profile_key),
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
    return int(cursor.lastrowid)


def _insert_evaluation(
    connection: sqlite3.Connection,
    *,
    prediction_id: int,
    sampled_at: datetime,
    profile_key: str = MORNING.key,
    predicted_minutes: int,
    error_minutes: int,
) -> None:
    actual_minutes = predicted_minutes + error_minutes
    arrival = connection.execute(
        """
        INSERT INTO arrival_events(
            yandex_snapshot_id, profile_key, vehicle_id, thread_id, stop_id,
            arrived_at, source, confidence, lat, lng, raw_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            None,
            profile_key,
            "",
            "",
            _profile_stop_id(profile_key),
            (sampled_at + timedelta(minutes=actual_minutes)).isoformat(),
            "smoke",
            "high",
            None,
            None,
            "{}",
        ),
    )
    connection.execute(
        """
        INSERT INTO prediction_evaluations(
            prediction_event_id, arrival_event_id, profile_key, evaluated_at,
            actual_minutes, predicted_minutes, error_minutes, bucket, source, raw_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            prediction_id,
            int(arrival.lastrowid),
            profile_key,
            sampled_at.isoformat(),
            actual_minutes,
            predicted_minutes,
            error_minutes,
            "10_14",
            SOURCE_TARGET_STOP_LIVE,
            "{}",
        ),
    )


def _report_window_key(profile_key: str) -> str:
    return "weekday_evening_19_22" if profile_key == EVENING.key else "weekday_morning_09_12"


def _profile_stop_id(profile_key: str) -> str:
    return EVENING.live_stop_id if profile_key == EVENING.key else MORNING.live_stop_id


def _assert_contains(text: str, expected: str) -> None:
    if expected not in text:
        raise AssertionError(f"expected {expected!r} in {text!r}")


def _assert_equal(actual: object, expected: object) -> None:
    if actual != expected:
        raise AssertionError(f"expected {expected!r}, got {actual!r}")


def assert_not_contains(text: str, unexpected: str) -> None:
    if unexpected in text:
        raise AssertionError(f"did not expect {unexpected!r} in {text!r}")


if __name__ == "__main__":
    main()
