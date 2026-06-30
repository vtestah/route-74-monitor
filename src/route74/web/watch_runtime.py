from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from route74.diagnostics import sanitize_diagnostic_text
from route74.domain.commute import CommuteProfile, DepartureDecision
from route74.domain.runtime_sources import BOT_EVENT_WATCH_EARLY, BOT_EVENT_WATCH_FINAL
from route74.domain.watch_policy import WATCH_DURATION_MINUTES, WATCH_POLL_INTERVAL_SECONDS
from route74.models import now_local
from route74.notifications import NotificationMessage, Notifier
from route74.presenters.commute import format_watch_alert
from route74.storage.bot_latency import BotLatencyRecorder
from route74.storage.runtime_predictions import BotDecisionRecorder
from route74.watch_state import WatchState, load_watch_states, watch_state_json
from route74.web.runtime_metrics import elapsed_ms, now_perf, watch_notification_event
from route74.web.watch_rules import is_early, is_final


WATCH_DURATION = timedelta(minutes=WATCH_DURATION_MINUTES)
POLL_INTERVAL = timedelta(seconds=WATCH_POLL_INTERVAL_SECONDS)
Clock = Callable[[], datetime]
DecisionBuilder = Callable[[CommuteProfile, int], DepartureDecision]
LOGGER = logging.getLogger(__name__)


class WebWatchStore:
    def __init__(self, path: Path) -> None:
        self._path = path

    @property
    def path(self) -> Path:
        return self._path

    def load(self, current_time: datetime) -> tuple[WatchState, ...]:
        return load_watch_states(self._path, current_time).states

    def save(self, states: tuple[WatchState, ...]) -> None:
        data = {state.watch_key: watch_state_json(state) for state in states}
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


class WebWatchManager:
    def __init__(
        self,
        *,
        store: WebWatchStore,
        decision_builder: DecisionBuilder,
        notifier: Notifier,
        decision_recorder: BotDecisionRecorder,
        latency_recorder: BotLatencyRecorder,
        clock: Clock = now_local,
    ) -> None:
        self._store = store
        self._decision_builder = decision_builder
        self._notifier = notifier
        self._decision_recorder = decision_recorder
        self._latency_recorder = latency_recorder
        self._clock = clock
        self._watches = {state.watch_key: state for state in store.load(clock())}

    def list_states(self) -> tuple[WatchState, ...]:
        return tuple(sorted(self._watches.values(), key=lambda state: state.profile.key))

    def start(self, profile: CommuteProfile, walk_minutes: int, initial_decision: DepartureDecision) -> WatchState | None:
        watch_key = profile.key
        if is_final(initial_decision):
            self._watches.pop(watch_key, None)
            self._persist()
            return None
        state = WatchState(
            watch_key=watch_key,
            profile=profile,
            walk_minutes=walk_minutes,
            started_at=self._clock(),
            next_poll_at=self._clock() + POLL_INTERVAL,
            early_sent=is_early(initial_decision),
        )
        self._watches[watch_key] = state
        self._persist()
        return state

    def stop(self, profile_key: str) -> bool:
        removed = self._watches.pop(profile_key, None)
        if removed is not None:
            self._persist()
        return removed is not None

    def tick(self) -> None:
        now = self._clock()
        for watch_key, state in list(self._watches.items()):
            if now - state.started_at >= WATCH_DURATION:
                self._watches.pop(watch_key, None)
                self._persist()
                continue
            if now < state.next_poll_at:
                continue
            updated = self._tick_one(state, now)
            if updated is None:
                self._watches.pop(watch_key, None)
            else:
                self._watches[watch_key] = updated
            self._persist()

    def summary_payload(self) -> list[dict[str, object]]:
        current_time = self._clock()
        payload: list[dict[str, object]] = []
        for state in self.list_states():
            expires_at = state.started_at + WATCH_DURATION
            payload.append(
                {
                    "profile_key": state.profile.key,
                    "walk_minutes": state.walk_minutes,
                    "started_at": state.started_at.isoformat(),
                    "next_poll_at": state.next_poll_at.isoformat(),
                    "early_sent": state.early_sent,
                    "expires_at": expires_at.isoformat(),
                    "expires_in_minutes": max(0, int((expires_at - current_time).total_seconds() // 60)),
                    "error_count": state.error_count,
                    "last_error_type": state.last_error_type,
                    "last_error_at": state.last_error_at.isoformat() if state.last_error_at is not None else None,
                }
            )
        return payload

    def _tick_one(self, state: WatchState, now: datetime) -> WatchState | None:
        tick_started = now_perf()
        try:
            decision_started = now_perf()
            decision = self._decision_builder(state.profile, state.walk_minutes)
            forecast_ms = elapsed_ms(decision_started)
        except Exception as exc:
            return replace(
                state,
                last_error_type=type(exc).__name__,
                last_error_at=now,
                error_count=state.error_count + 1,
                next_poll_at=now + POLL_INTERVAL,
            )

        updated = replace(state, last_error_type="", last_error_at=None, error_count=0)
        alert_kind, event_kind = _alert_for(decision, updated)
        if event_kind is None:
            return replace(updated, next_poll_at=now + POLL_INTERVAL)

        title = f"Route 74 · {decision.profile.key}"
        message = NotificationMessage(title=title, body=_watch_message(decision, alert_kind), priority=1 if alert_kind == "final" else 0)
        send_started = now_perf()
        result = self._notifier.send(message)
        send_ms = elapsed_ms(send_started)
        total_ms = elapsed_ms(tick_started)
        self._latency_recorder.record(
            watch_notification_event(
                decision=decision,
                event_kind=event_kind,
                forecast_ms=forecast_ms,
                total_ms=total_ms,
                send_ms=send_ms,
                notification=result,
            )
        )
        if result.delivered:
            self._decision_recorder.record_watch_alert(decision, event_kind)
        else:
            return replace(
                updated,
                last_error_type=result.error_type or "NotificationError",
                last_error_at=now,
                error_count=updated.error_count + 1,
                next_poll_at=now + POLL_INTERVAL,
            )
        if event_kind == BOT_EVENT_WATCH_FINAL:
            return None
        return replace(updated, early_sent=True, next_poll_at=now + POLL_INTERVAL)

    def _persist(self) -> None:
        self._store.save(tuple(self._watches.values()))


class WatchLoop:
    def __init__(self, manager: WebWatchManager) -> None:
        self._manager = manager
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="route74-web-watch-loop")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    async def _run(self) -> None:
        while True:
            try:
                await asyncio.to_thread(self._manager.tick)
            except Exception as exc:
                LOGGER.warning(
                    "watch loop tick failed: %s",
                    sanitize_diagnostic_text(exc, fallback=type(exc).__name__, limit=160),
                )
            await asyncio.sleep(POLL_INTERVAL.total_seconds())


def _alert_for(decision: DepartureDecision, state: WatchState) -> tuple[str, str | None]:
    if is_final(decision):
        return "final", BOT_EVENT_WATCH_FINAL
    if is_early(decision) and not state.early_sent:
        return "early", BOT_EVENT_WATCH_EARLY
    return "", None


def _watch_message(decision: DepartureDecision, alert_kind: str) -> str:
    return format_watch_alert(decision, "final" if alert_kind == "final" else "early")
