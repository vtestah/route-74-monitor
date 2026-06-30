from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from route74.domain.commute import DepartureDecision, DepartureSource, DepartureUrgency
from route74.domain.eta import EtaConfidence, EtaConsensus, EtaEstimate, EtaSource
from route74.domain.profiles import MORNING
from route74.domain.yandex_history import YandexHistoryPrediction
from route74.domain.runtime_sources import BOT_EVENT_USER_REPLY, BOT_EVENT_WATCH_EARLY
from route74.models import NOVOSIBIRSK_TZ
from route74.presenters.commute import format_action_message
from route74.presenters.commute_change import format_departure_change_details, format_departure_change_line
from route74.services.commute_change import BotDecisionChangeService, build_runtime_prediction_change_map
from route74.storage.connection import connect, init_db
from route74.storage.runtime_quality import load_recent_bot_runtime_predictions
from route74.storage.runtime_predictions import insert_bot_decision_prediction_event
from route74.sources.yandex.models import YandexLiveForecast


def main() -> None:
    _assert_absolute_arrival_comparison()
    _assert_later_arrival_and_source_change_are_visible()
    _assert_watch_alerts_do_not_replace_previous_user_reply()
    _assert_runtime_prediction_change_map_skips_watch_alerts()
    _assert_no_eta_can_explain_lost_eta()
    print("OK | commute change smoke passed")


def _assert_absolute_arrival_comparison() -> None:
    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "route74.sqlite"
        previous = _decision(_time(7, 0), arrival_in=10)
        current = _decision(_time(7, 5), arrival_in=5)
        _record(db_path, previous)

        change = BotDecisionChangeService(db_path).build(current)
        line = format_departure_change_line(change)

    _assert_contains(line, "время 74-го почти без изменений")
    _assert_not_contains(line, "раньше на")
    _assert_not_contains(line, "позже на")
    message = format_action_message(current, include_follow_up=True, change=change)
    _assert_contains(message, "🔁 С прошлого ответа: время 74-го почти без изменений")


def _assert_later_arrival_and_source_change_are_visible() -> None:
    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "route74.sqlite"
        previous = _decision(_time(7, 0), arrival_in=10, source=DepartureSource.YANDEX_HISTORY)
        current = _decision(_time(7, 5), arrival_in=9, source=DepartureSource.YANDEX)
        _record(db_path, previous)

        line = format_departure_change_line(BotDecisionChangeService(db_path).build(current))

    _assert_contains(line, "74-й позже на 4 мин")
    _assert_contains(line, "источник история Яндекса -> Яндекс live")


def _assert_watch_alerts_do_not_replace_previous_user_reply() -> None:
    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "route74.sqlite"
        previous_reply = _decision(_time(7, 0), arrival_in=10)
        watch_alert = _decision(_time(7, 4), arrival_in=20)
        current = _decision(_time(7, 6), arrival_in=7)
        _record(db_path, previous_reply)
        _record(db_path, watch_alert, event_kind=BOT_EVENT_WATCH_EARLY)

        line = format_departure_change_line(BotDecisionChangeService(db_path).build(current))

    _assert_contains(line, "74-й позже на 3 мин")
    _assert_not_contains(line, "раньше на")


def _assert_runtime_prediction_change_map_skips_watch_alerts() -> None:
    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "route74.sqlite"
        previous_reply = _decision(_time(7, 0), arrival_in=10)
        watch_alert = _decision(_time(7, 5), arrival_in=1)
        current = _decision(_time(7, 7), arrival_in=12)
        _record(db_path, previous_reply)
        _record(db_path, watch_alert, event_kind=BOT_EVENT_WATCH_EARLY)
        _record(db_path, current)

        with connect(db_path) as connection:
            recent = load_recent_bot_runtime_predictions(
                connection,
                current_time=_time(7, 10),
                hours=3,
                limit=8,
            )
        current_prediction = next(item for item in recent if item.sampled_at == current.current_time)
        change = build_runtime_prediction_change_map(recent)[current_prediction.id]
        details = format_departure_change_details(change)

    _assert_contains(details, "74-й позже на 9 мин")
    _assert_not_contains(details, "раньше на")


def _assert_no_eta_can_explain_lost_eta() -> None:
    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "route74.sqlite"
        previous = _decision(_time(7, 0), arrival_in=12)
        current = _no_eta_decision(previous.walk_minutes)
        current = replace(current, current_time=_time(7, 8))
        _record(db_path, previous)

        line = format_departure_change_line(BotDecisionChangeService(db_path).build(current))

    _assert_contains(line, "ETA пропал · было 07:12")
    _assert_contains(line, "источник Яндекс live -> нет ETA")


def _record(db_path: Path, decision: DepartureDecision, *, event_kind: str = BOT_EVENT_USER_REPLY) -> None:
    with connect(db_path) as connection:
        init_db(connection)
        result = insert_bot_decision_prediction_event(connection, decision, event_kind=event_kind)
    if not result.created:
        raise AssertionError(f"expected prediction event, got {result.reason}")


def _decision(
    current_time: datetime,
    *,
    arrival_in: int,
    source: DepartureSource = DepartureSource.YANDEX,
) -> DepartureDecision:
    base = _fake_decision(5)
    eta_source = EtaSource(source.value)
    leave_in = arrival_in - base.walk_minutes - 3
    return replace(
        base,
        current_time=current_time,
        source=source,
        arrival_in_minutes=arrival_in,
        arrival_at=current_time + timedelta(minutes=arrival_in),
        leave_in_minutes=leave_in,
        leave_at=current_time + timedelta(minutes=leave_in),
        eta_consensus=EtaConsensus(
            eta_source,
            arrival_in,
            EtaConfidence.MEDIUM,
            3,
            None,
            "",
            estimates=(EtaEstimate(eta_source, arrival_in),),
        ),
        next_live_minutes=(),
    )


def _fake_decision(walk_minutes: int) -> DepartureDecision:
    current_time = _time(7, 0)
    arrival_in = walk_minutes + 8
    return DepartureDecision(
        profile=MORNING,
        current_time=current_time,
        walk_minutes=walk_minutes,
        source=DepartureSource.YANDEX,
        urgency=DepartureUrgency.RELAX,
        arrival_in_minutes=arrival_in,
        arrival_at=current_time + timedelta(minutes=arrival_in),
        leave_in_minutes=5,
        leave_at=current_time + timedelta(minutes=5),
        next_live_minutes=(),
        eta_consensus=EtaConsensus(
            EtaSource.YANDEX,
            arrival_in,
            EtaConfidence.MEDIUM,
            3,
            None,
            "",
            estimates=(EtaEstimate(EtaSource.YANDEX, arrival_in),),
        ),
        yandex_forecast=YandexLiveForecast.disabled(),
        yandex_history=YandexHistoryPrediction.unavailable(reason="history_unavailable"),
    )


def _no_eta_decision(walk_minutes: int) -> DepartureDecision:
    current_time = _time(7, 0)
    return DepartureDecision(
        profile=MORNING,
        current_time=current_time,
        walk_minutes=walk_minutes,
        source=DepartureSource.NONE,
        urgency=DepartureUrgency.NO_DATA,
        arrival_in_minutes=None,
        arrival_at=None,
        leave_in_minutes=None,
        leave_at=None,
        next_live_minutes=(),
        eta_consensus=EtaConsensus.disabled(),
        yandex_forecast=YandexLiveForecast.disabled(),
        yandex_history=YandexHistoryPrediction.unavailable(reason="history_unavailable"),
    )


def _time(hour: int, minute: int) -> datetime:
    return datetime(2026, 6, 4, hour, minute, tzinfo=NOVOSIBIRSK_TZ)


def _assert_contains(text: str, expected: str) -> None:
    if expected not in text:
        raise AssertionError(f"expected {expected!r} in {text!r}")


def _assert_not_contains(text: str, unexpected: str) -> None:
    if unexpected in text:
        raise AssertionError(f"did not expect {unexpected!r} in {text!r}")


if __name__ == "__main__":
    main()
