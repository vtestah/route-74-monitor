from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi.testclient import TestClient

from route74.domain.commute import DepartureDecision, DepartureSource, DepartureUrgency
from route74.domain.eta import (
    EtaConfidence,
    EtaConsensus,
    EtaEstimate,
    EtaExplanation,
    EtaExplanationAction,
    EtaExplanationCode,
    EtaSource,
)
from route74.domain.profiles import MORNING
from route74.domain.yandex_history import YandexHistoryPrediction
from route74.models import now_local
from route74.notifications.base import NotificationStatus
from route74.sources.yandex.models import YandexLiveForecast, YandexSourceMethod, YandexSourceStatus, YandexVehicle
from route74.storage import connect, init_db
from route74.web.app import create_app
from route74.web.decision_ui import decision_ui_payload


def main() -> None:
    with TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "route74.sqlite"
        with connect(db_path) as connection:
            init_db(connection)
        app = create_app(db_path, watch_state_path=Path(temp_dir) / "web-watches.json")
        with TestClient(app) as client:
            app.state.commute_service = _FakeCommuteService()
            app.state.clock = app.state.commute_service.clock
            app.state.decision_recorder = _NullRecorder()
            app.state.latency_recorder = _NullRecorder()
            app.state.notifier = _NullNotifier()
            app.state.watch_manager = _FakeWatchManager()

            page = client.get("/")
            _assert_ok(page)
            _assert_contains(page.text, 'id="status-strip"')
            _assert_contains(page.text, 'id="backend-status"')
            _assert_contains(page.text, 'id="result-action"')
            _assert_contains(page.text, 'id="result-priority"')
            _assert_contains(page.text, "Буферы сохраняются только в этом браузере")
            _assert_contains(page.text, "route74.morningWalkMinutes")
            _assert_contains(page.text, "<summary>Диагностика</summary>")
            health = client.get("/healthz")
            _assert_ok(health)
            _assert_equal(health.json()["status"], "ok")
            watch = client.get("/api/watch")
            _assert_ok(watch)
            _assert_equal(watch.json()["watches"], [])
            response = client.post(
                "/api/catch",
                json={
                    "profile": "morning",
                    "morning_walk_minutes": 12,
                    "evening_walk_minutes": 17,
                    "start_watch": True,
                },
            )
            _assert_ok(response)
            payload = response.json()
            _assert_contains(payload["message"], "Сейчас")
            _assert_equal(payload["profile_key"], "morning")
            _assert_equal(payload["watch_started"], False)
            _assert_equal(payload["notification"]["configured"], False)
            datetime.fromisoformat(payload["decision_ui"]["current_time"])
            datetime.fromisoformat(payload["decision_ui"]["leave_at"])
            datetime.fromisoformat(payload["decision_ui"]["arrival_at"])
            _assert_equal(
                payload["decision_ui"],
                {
                    "status": "catch",
                    "headline": "🧥 СОБИРАЙСЯ",
                    "eta_state": "history",
                    "eta_state_label": "ETA по истории",
                    "profile_key": "morning",
                    "profile_label": "Утро",
                    "current_time": payload["decision_ui"]["current_time"],
                    "leave_at": payload["decision_ui"]["leave_at"],
                    "leave_in_minutes": 5,
                    "arrival_at": payload["decision_ui"]["arrival_at"],
                    "arrival_in_minutes": 20,
                    "wait_minutes": 3,
                    "source_label": "📈 История Яндекса",
                    "eta_reason_code": "history_fallback",
                    "eta_action_code": "check_map",
                    "eta_explanation_label": "live ETA нет, беру историю Яндекса",
                    "eta_action_label": "лучше сверить карту",
                    "eta_explanations": [
                        {
                            "code": "history_fallback",
                            "action": "check_map",
                            "label": "live ETA нет, беру историю Яндекса",
                            "action_label": "лучше сверить карту",
                        },
                    ],
                },
            )
            _assert_no_eta_payload()
            _assert_stale_eta_payload()
    print("OK | web runtime smoke passed")


class _FakeCommuteService:
    def __init__(self) -> None:
        self._clock = now_local

    @property
    def clock(self):
        return self._clock

    def build_decision(self, profile, walk_minutes: int) -> DepartureDecision:
        current_time = self._clock()
        arrival_in_minutes = 20
        leave_in_minutes = arrival_in_minutes - walk_minutes - 3
        return DepartureDecision(
            profile=profile,
            current_time=current_time,
            walk_minutes=walk_minutes,
            source=DepartureSource.YANDEX_HISTORY,
            urgency=DepartureUrgency.GET_READY,
            arrival_in_minutes=arrival_in_minutes,
            arrival_at=current_time + timedelta(minutes=arrival_in_minutes),
            leave_in_minutes=leave_in_minutes,
            leave_at=current_time + timedelta(minutes=leave_in_minutes),
            next_live_minutes=(),
            eta_consensus=EtaConsensus(
                selected_source=EtaSource.YANDEX_HISTORY,
                arrival_minutes=arrival_in_minutes,
                confidence=EtaConfidence.MEDIUM,
                target_wait_minutes=3,
                spread_minutes=None,
                warning="",
                estimates=(EtaEstimate(EtaSource.YANDEX_HISTORY, arrival_in_minutes),),
                explanations=(
                    EtaExplanation(
                        EtaExplanationCode.HISTORY_FALLBACK,
                        EtaExplanationAction.CHECK_MAP,
                        detail=EtaSource.YANDEX_HISTORY.value,
                    ),
                ),
            ),
            yandex_forecast=YandexLiveForecast.disabled(),
            yandex_history=YandexHistoryPrediction(
                available=True,
                arrival_minutes=arrival_in_minutes,
                sample_count=24,
                bucket_minutes=10,
                window_days=14,
                percentile=80,
                fallback_reason="",
            ),
        )


def _assert_no_eta_payload() -> None:
    current_time = now_local()
    payload = decision_ui_payload(
        DepartureDecision(
            profile=MORNING,
            current_time=current_time,
            walk_minutes=12,
            source=DepartureSource.NONE,
            urgency=DepartureUrgency.NO_DATA,
            arrival_in_minutes=None,
            arrival_at=None,
            leave_in_minutes=None,
            leave_at=None,
            next_live_minutes=(),
        )
    )
    _assert_equal(payload["status"], "no_eta")
    _assert_equal(payload["eta_state"], "no_eta")
    _assert_equal(payload["eta_state_label"], "Нет ETA")
    _assert_equal(payload["arrival_at"], None)
    _assert_equal(payload["wait_minutes"], None)
    _assert_equal(payload["source_label"], "Нет ETA")
    _assert_equal(payload["eta_reason_code"], "no_eta")
    _assert_equal(payload["eta_action_code"], "wait_for_data")
    _assert_equal(payload["eta_explanation_label"], "точного ETA нет")
    _assert_equal(payload["eta_action_label"], "обнови прогноз через минуту")


def _assert_stale_eta_payload() -> None:
    current_time = now_local()
    payload = decision_ui_payload(
        DepartureDecision(
            profile=MORNING,
            current_time=current_time,
            walk_minutes=12,
            source=DepartureSource.YANDEX,
            urgency=DepartureUrgency.GET_READY,
            arrival_in_minutes=18,
            arrival_at=current_time + timedelta(minutes=18),
            leave_in_minutes=3,
            leave_at=current_time + timedelta(minutes=3),
            next_live_minutes=(),
            eta_consensus=EtaConsensus(
                selected_source=EtaSource.YANDEX,
                arrival_minutes=18,
                confidence=EtaConfidence.HIGH,
                target_wait_minutes=3,
                spread_minutes=None,
                warning="",
                estimates=(EtaEstimate(EtaSource.YANDEX, 18),),
                explanations=(
                    EtaExplanation(
                        EtaExplanationCode.LIVE_ETA,
                        EtaExplanationAction.TRUST_ETA,
                        detail=EtaSource.YANDEX.value,
                    ),
                ),
            ),
            yandex_forecast=YandexLiveForecast(
                enabled=True,
                available=True,
                source_method=YandexSourceMethod.VEHICLE_PREDICTION,
                status=YandexSourceStatus.STALE,
                arrival_minutes=(18,),
                vehicles=(YandexVehicle("stale-route74", lat=54.85, lng=83.1, arrival_minutes=18, age_seconds=600),),
                vehicle_count=1,
                newest_age_seconds=600,
                confidence=EtaConfidence.HIGH,
            ),
        )
    )
    _assert_equal(payload["status"], "catch")
    _assert_equal(payload["eta_state"], "stale")
    _assert_equal(payload["eta_state_label"], "ETA устарел")
    _assert_equal(payload["source_label"], "🟡 Яндекс 74")
    _assert_equal(payload["eta_reason_code"], "live_eta")
    _assert_equal(payload["eta_action_code"], "trust_eta")


class _NullRecorder:
    def record(self, *_args, **_kwargs) -> None:
        return None

    def record_user_reply(self, *_args, **_kwargs) -> None:
        return None


class _NullNotifier:
    def status(self) -> NotificationStatus:
        return NotificationStatus(provider="pushover", configured=False, detail="Pushover не настроен")


class _FakeWatchManager:
    def summary_payload(self) -> list[dict[str, object]]:
        return []

    def stop(self, _profile_key: str) -> bool:
        return False


def _assert_ok(response) -> None:
    if response.status_code != 200:
        raise AssertionError(f"expected 200, got {response.status_code}: {response.text}")


def _assert_contains(text: str, fragment: str) -> None:
    if fragment not in text:
        raise AssertionError(f"expected to find {fragment!r} in {text!r}")


def _assert_equal(actual, expected) -> None:
    if actual != expected:
        raise AssertionError(f"expected {expected!r}, got {actual!r}")


if __name__ == "__main__":
    main()
