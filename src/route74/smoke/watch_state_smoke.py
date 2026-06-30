from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from route74.domain.commute import DepartureDecision, DepartureSource, DepartureUrgency
from route74.domain.eta import EtaConfidence, EtaConsensus, EtaEstimate, EtaSource
from route74.domain.profiles import MORNING
from route74.domain.yandex_history import YandexHistoryPrediction
from route74.models import NOVOSIBIRSK_TZ
from route74.notifications.base import NotificationMessage, NotificationSendResult
from route74.sources.yandex.models import YandexLiveForecast
from route74.storage.bot_latency import BotLatencyRecorder
from route74.storage.runtime_predictions import BotDecisionRecorder
from route74.watch_state import WatchState, format_watch_state_summary, summarize_watch_state
from route74.web.watch_runtime import POLL_INTERVAL, WebWatchManager, WebWatchStore


def main() -> None:
    base = datetime(2026, 6, 4, 7, 0, tzinfo=NOVOSIBIRSK_TZ)
    with TemporaryDirectory() as temp_dir:
        path = Path(temp_dir) / "web-watches.json"
        _assert_watch_store_roundtrip(path, base)
        _assert_watch_store_skips_invalid_and_expired(path, base)
        _assert_watch_state_summary_reports_runtime_errors(path, base)
        _assert_watch_state_summary(path, base)
        _assert_watch_manager_sends_final_signal(path, base)
        _assert_watch_manager_persists_next_check(path, base)
        _assert_watch_manager_persists_runtime_errors(path, base)
    print("OK | watch state smoke passed")


def _assert_watch_store_roundtrip(path: Path, base: datetime) -> None:
    store = WebWatchStore(path)
    store.save(
        (
            WatchState(
                watch_key="morning-main",
                profile=MORNING,
                walk_minutes=12,
                started_at=base,
                next_poll_at=base + timedelta(seconds=10),
                early_sent=True,
                last_error_type="Timeout\nError",
                last_error_at=base + timedelta(seconds=5),
                error_count=2,
            ),
        )
    )
    loaded = store.load(base + timedelta(minutes=1))

    _assert_equal(len(loaded), 1)
    _assert_equal(loaded[0].watch_key, "morning-main")
    _assert_equal(loaded[0].profile.key, MORNING.key)
    _assert_equal(loaded[0].walk_minutes, 12)
    _assert_equal(loaded[0].early_sent, True)
    _assert_equal(loaded[0].last_error_type, "Timeout Error")
    _assert_equal(loaded[0].last_error_at, base + timedelta(seconds=5))
    _assert_equal(loaded[0].error_count, 2)


def _assert_watch_store_skips_invalid_and_expired(path: Path, base: datetime) -> None:
    payload = {
        "bad watch": {
            "profile_key": MORNING.key,
            "walk_minutes": 12,
            "started_at": base.isoformat(),
            "next_poll_at": base.isoformat(),
        },
        "morning-main": {
            "profile_key": MORNING.key,
            "walk_minutes": 12,
            "started_at": base.isoformat(),
            "next_poll_at": base.isoformat(),
        },
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    _assert_equal(WebWatchStore(path).load(base + timedelta(minutes=31)), ())

    path.write_text("{not-json", encoding="utf-8")
    _assert_equal(WebWatchStore(path).load(base), ())
    summary = summarize_watch_state(path, base)
    _assert_equal(summary.status, "critical")
    _assert_equal(summary.file_status, "unreadable")
    _assert_equal(summary.active_count, 0)
    output = format_watch_state_summary(summary, str(path))
    _assert_contains(output, "watch-state status=critical")
    _assert_contains(output, "file=unreadable")
    _assert_equal("watch_state_empty" in output, False)


def _assert_watch_state_summary(path: Path, base: datetime) -> None:
    missing_path = path.parent / "missing.json"
    missing_summary = summarize_watch_state(missing_path, base)
    _assert_equal(missing_summary.status, "ok")
    _assert_equal(missing_summary.file_status, "missing")
    _assert_equal(missing_summary.active_count, 0)
    missing_output = format_watch_state_summary(missing_summary, str(missing_path))
    _assert_contains(missing_output, "watch-state status=ok")
    _assert_contains(missing_output, "file=missing")
    _assert_contains(missing_output, "info watch_state_file: file not created yet")
    _assert_contains(missing_output, "watch_state_empty")

    store = WebWatchStore(path)
    store.save((WatchState("morning-main", MORNING, 12, base, base, False),))
    summary = summarize_watch_state(path, base + timedelta(seconds=1))
    output = format_watch_state_summary(summary, str(path))
    _assert_equal(summary.status, "ok")
    _assert_equal(summary.active_count, 1)
    _assert_equal(summary.due_count, 1)
    _assert_equal(summary.expires_at, base + timedelta(minutes=30))
    _assert_equal(summary.expires_in_minutes, 30)
    _assert_contains(output, "active=1")
    _assert_contains(output, "expires_in=30")
    _assert_contains(output, f"expires_at={(base + timedelta(minutes=30)).isoformat()}")


def _assert_watch_state_summary_reports_runtime_errors(path: Path, base: datetime) -> None:
    store = WebWatchStore(path)
    store.save(
        (
            WatchState(
                watch_key="morning-main",
                profile=MORNING,
                walk_minutes=12,
                started_at=base,
                next_poll_at=base,
                early_sent=False,
                last_error_type="RuntimeError",
                last_error_at=base + timedelta(seconds=5),
                error_count=2,
            ),
        )
    )
    summary = summarize_watch_state(path, base + timedelta(seconds=10))
    output = format_watch_state_summary(summary, str(path))
    _assert_equal(summary.status, "warning")
    _assert_equal(summary.runtime_error_count, 2)
    _assert_equal(summary.runtime_error_records, 1)
    _assert_contains(output, "runtime_errors=2")
    _assert_contains(output, "watch_state_runtime_error")
    _assert_contains(output, "types=RuntimeError")


def _assert_watch_manager_sends_final_signal(path: Path, base: datetime) -> None:
    path = path.parent / "watch-final.json"
    notifier = _CollectingNotifier()
    now = [base]
    manager = WebWatchManager(
        store=WebWatchStore(path),
        decision_builder=lambda _profile, _walk: _decision(now[0], leave_in=0),
        notifier=notifier,
        decision_recorder=_NullDecisionRecorder(Path(path.parent) / "watch.sqlite"),
        latency_recorder=_NullLatencyRecorder(Path(path.parent) / "watch.sqlite"),
        clock=lambda: now[0],
    )
    manager.start(MORNING, 12, _decision(base, leave_in=5))
    now[0] += POLL_INTERVAL
    manager.tick()

    _assert_equal(manager.list_states(), ())
    _assert_equal(len(notifier.messages), 1)
    _assert_contains(notifier.messages[-1].body, "ВЫХОДИ СЕЙЧАС")


def _assert_watch_manager_persists_next_check(path: Path, base: datetime) -> None:
    path = path.parent / "watch-next.json"
    now = [base]
    manager = WebWatchManager(
        store=WebWatchStore(path),
        decision_builder=lambda _profile, _walk: _decision(now[0], leave_in=8),
        notifier=_CollectingNotifier(),
        decision_recorder=_NullDecisionRecorder(Path(path.parent) / "next.sqlite"),
        latency_recorder=_NullLatencyRecorder(Path(path.parent) / "next.sqlite"),
        clock=lambda: now[0],
    )
    manager.start(MORNING, 12, _decision(base, leave_in=8))
    now[0] += POLL_INTERVAL
    manager.tick()
    loaded = WebWatchStore(path).load(now[0])

    _assert_equal(len(loaded), 1)
    _assert_equal(loaded[0].next_poll_at, now[0] + POLL_INTERVAL)


def _assert_watch_manager_persists_runtime_errors(path: Path, base: datetime) -> None:
    path = path.parent / "watch-errors.json"
    now = [base]
    calls = [0]

    def build_decision(_profile: object, _walk_minutes: object) -> DepartureDecision:
        calls[0] += 1
        if calls[0] == 1:
            raise RuntimeError("local\nfailure")
        return _decision(now[0], leave_in=8)

    manager = WebWatchManager(
        store=WebWatchStore(path),
        decision_builder=build_decision,
        notifier=_CollectingNotifier(),
        decision_recorder=_NullDecisionRecorder(Path(path.parent) / "error.sqlite"),
        latency_recorder=_NullLatencyRecorder(Path(path.parent) / "error.sqlite"),
        clock=lambda: now[0],
    )
    manager.start(MORNING, 12, _decision(base, leave_in=8))
    now[0] += POLL_INTERVAL
    manager.tick()
    loaded = WebWatchStore(path).load(now[0])

    _assert_equal(len(loaded), 1)
    _assert_equal(loaded[0].error_count, 1)
    _assert_equal(loaded[0].last_error_type, "RuntimeError")

    now[0] += POLL_INTERVAL
    manager.tick()
    loaded = WebWatchStore(path).load(now[0])
    _assert_equal(len(loaded), 1)
    _assert_equal(loaded[0].error_count, 0)
    _assert_equal(loaded[0].last_error_type, "")


def _decision(current_time: datetime, *, leave_in: int) -> DepartureDecision:
    walk_minutes = 12
    target_wait = 3
    arrival_in = walk_minutes + target_wait + leave_in
    return DepartureDecision(
        profile=MORNING,
        current_time=current_time,
        walk_minutes=walk_minutes,
        source=DepartureSource.YANDEX_HISTORY,
        urgency=DepartureUrgency.GO_NOW
        if leave_in <= 0
        else DepartureUrgency.GET_READY
        if leave_in <= 7
        else DepartureUrgency.RELAX,
        arrival_in_minutes=arrival_in,
        arrival_at=current_time + timedelta(minutes=arrival_in),
        leave_in_minutes=leave_in,
        leave_at=current_time + timedelta(minutes=leave_in),
        next_live_minutes=(),
        eta_consensus=EtaConsensus(
            selected_source=EtaSource.YANDEX_HISTORY,
            arrival_minutes=arrival_in,
            confidence=EtaConfidence.MEDIUM,
            target_wait_minutes=target_wait,
            spread_minutes=None,
            warning="",
            estimates=(EtaEstimate(EtaSource.YANDEX_HISTORY, arrival_in),),
        ),
        yandex_forecast=YandexLiveForecast.disabled(),
        yandex_history=YandexHistoryPrediction(
            available=True,
            arrival_minutes=arrival_in,
            sample_count=12,
            bucket_minutes=10,
            window_days=14,
            percentile=80,
            fallback_reason="",
        ),
    )


class _CollectingNotifier:
    def __init__(self) -> None:
        self.messages: list[NotificationMessage] = []

    def status(self):
        raise AssertionError("status not used in watch state smoke")

    def send(self, message: NotificationMessage) -> NotificationSendResult:
        self.messages.append(message)
        return NotificationSendResult(provider="pushover", delivered=True, detail="ok")


class _NullDecisionRecorder(BotDecisionRecorder):
    def record_watch_alert(self, *_args, **_kwargs):
        return None


class _NullLatencyRecorder(BotLatencyRecorder):
    def record(self, *_args, **_kwargs):
        return 0


def _assert_contains(text: str, expected: str) -> None:
    if expected not in text:
        raise AssertionError(f"expected {expected!r} in {text!r}")


def _assert_equal(actual: object, expected: object) -> None:
    if actual != expected:
        raise AssertionError(f"expected {expected!r}, got {actual!r}")


if __name__ == "__main__":
    main()
