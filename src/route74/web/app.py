from __future__ import annotations

from collections.abc import Iterator
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse

from route74.build_info import load_build_info
from route74.services.factory import commute_service
from route74.diagnostics import sanitize_diagnostic_text
from route74.domain.profiles import EVENING, MORNING, profile_by_key
from route74.dashboard.assets import DASHBOARD_HTML, FAVICON_SVG
from route74.dashboard.data import build_dashboard_summary, build_dashboard_support_snapshot, load_recent_samples, load_window_series
from route74.dashboard.preview import (
    dashboard_preview_cache_dir,
    load_dashboard_preview,
    load_dashboard_preview_image,
    refresh_dashboard_preview,
)
from route74.notifications import build_notifier, load_pushover_config
from route74.presenters.commute import format_action_message
from route74.presenters.stats import format_stats_message
from route74.presenters.support_snapshot import format_support_snapshot
from route74.services.departure import choose_profile_for_time
from route74.services.stats import StatsService
from route74.services.support_snapshot import SupportSnapshotService
from route74.storage import DEFAULT_DB, summarize_db_health_readonly
from route74.storage.bot_latency import BotLatencyRecorder
from route74.storage.runtime_predictions import BotDecisionRecorder
from route74.watch_state import DEFAULT_WATCH_STATE_PATH
from route74.web.assets import WEB_HTML
from route74.web.decision_ui import decision_ui_payload
from route74.web.models import CatchRequest, WatchStopRequest
from route74.web.runtime_metrics import elapsed_ms, now_perf, web_interaction_event
from route74.web.watch_runtime import WatchLoop, WebWatchManager, WebWatchStore


HTTP_ERROR_DETAIL_LIMIT = 160


def create_app(
    db_path: Path = DEFAULT_DB,
    *,
    watch_state_path: Path = DEFAULT_WATCH_STATE_PATH,
    env_file: Path | None = None,
) -> FastAPI:
    app = FastAPI(
        title="Route 74",
        version="0.1.0",
        lifespan=_lifespan(Path(db_path), Path(watch_state_path), Path(env_file) if env_file is not None else None),
    )
    app.state.preview_cache_path = dashboard_preview_cache_dir(Path(db_path))

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return WEB_HTML

    @app.get("/dashboard", response_class=HTMLResponse)
    def dashboard() -> str:
        return DASHBOARD_HTML

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon() -> HTMLResponse:
        return HTMLResponse(FAVICON_SVG, media_type="image/svg+xml")

    @app.get("/healthz")
    def healthz() -> dict[str, object]:
        with _service_errors():
            health = summarize_db_health_readonly(app.state.db_path)
        return {
            "status": "ok" if health.healthy else "bad",
            "integrity_check": health.integrity_check,
            "quick_check": health.quick_check,
            "notification": _notification_payload(app),
            "version": load_build_info().to_jsonable(),
        }

    @app.get("/api/summary")
    def api_summary() -> dict[str, object]:
        with _service_errors():
            return build_dashboard_summary(
                app.state.db_path,
                watch_state_path=app.state.watch_state_path,
                preview_cache_path=app.state.preview_cache_path,
            )

    @app.get("/api/support-snapshot/{profile_key}")
    def api_support_snapshot(profile_key: str) -> dict[str, object]:
        with _service_errors(not_found_detail=f"Неизвестный профиль: {profile_key}"):
            return build_dashboard_support_snapshot(
                app.state.db_path,
                profile_key,
                watch_state_path=app.state.watch_state_path,
            )

    @app.get("/api/preview/{profile_key}")
    def api_preview(profile_key: str) -> dict[str, object]:
        with _service_errors(not_found_detail=f"Неизвестный профиль: {profile_key}"):
            return {
                "preview": load_dashboard_preview(
                    app.state.preview_cache_path,
                    profile_key,
                )
            }

    @app.post("/api/preview/{profile_key}/refresh")
    def api_preview_refresh(profile_key: str) -> dict[str, object]:
        with _service_errors(not_found_detail=f"Неизвестный профиль: {profile_key}"):
            return {
                "preview": refresh_dashboard_preview(
                    app.state.preview_cache_path,
                    profile_key,
                )
            }

    @app.get("/api/preview/{profile_key}/image", include_in_schema=False)
    def api_preview_image(profile_key: str) -> FileResponse:
        try:
            image_path = load_dashboard_preview_image(app.state.preview_cache_path, profile_key)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"Неизвестный профиль: {profile_key}") from exc
        if image_path is None:
            raise HTTPException(status_code=404, detail="preview not found")
        return FileResponse(image_path, media_type="image/png")

    @app.get("/api/windows/{window_key}/series")
    def api_window_series(window_key: str, days: int = Query(default=30, ge=1, le=120)) -> dict[str, object]:
        with _service_errors(not_found_detail=f"Неизвестное отчётное окно: {window_key}"):
            return load_window_series(app.state.db_path, window_key, days=days)

    @app.get("/api/recent-samples")
    def api_recent_samples(window: str | None = None, limit: int = Query(default=50, ge=1, le=200)) -> dict[str, object]:
        with _service_errors(not_found_detail=f"Неизвестное отчётное окно: {window}"):
            return load_recent_samples(app.state.db_path, window_key=window, limit=limit)

    @app.get("/api/watch")
    def api_watch() -> dict[str, object]:
        return {
            "notification": _notification_payload(app),
            "watches": app.state.watch_manager.summary_payload(),
        }

    @app.post("/api/watch/stop")
    def api_watch_stop(request: WatchStopRequest) -> dict[str, object]:
        return {"stopped": app.state.watch_manager.stop(request.profile_key)}

    @app.post("/api/catch")
    def api_catch(request: CatchRequest) -> dict[str, object]:
        service = app.state.commute_service
        started = now_perf()
        profile = _selected_profile(request.profile, app.state.clock())
        walk_minutes = request.morning_walk_minutes if profile.key == MORNING.key else request.evening_walk_minutes
        if walk_minutes is None:
            walk_minutes = profile.default_walk_minutes
        decision = service.build_decision(profile, walk_minutes)
        app.state.decision_recorder.record_user_reply(decision)
        total_ms = elapsed_ms(started)
        app.state.latency_recorder.record(
            web_interaction_event(
                decision=decision,
                command=f"catch:{request.profile}",
                forecast_ms=total_ms,
                total_ms=total_ms,
            )
        )
        watch_started = False
        if request.start_watch and app.state.notifier.status().configured:
            watch_state = app.state.watch_manager.start(profile, walk_minutes, decision)
            watch_started = watch_state is not None
        return {
            "profile_key": profile.key,
            "walk_minutes": walk_minutes,
            "watch_started": watch_started,
            "notification": _notification_payload(app),
            "decision_ui": decision_ui_payload(decision),
            "message": format_action_message(decision, include_follow_up=True),
        }

    @app.get("/api/stats/{profile_key}")
    def api_stats(profile_key: str, walk_minutes: int = Query(default=0, ge=0, le=120)) -> dict[str, object]:
        profile = profile_by_key(profile_key)
        minutes = walk_minutes or profile.default_walk_minutes
        stats_service = StatsService(
            app.state.commute_service,
            db_path=app.state.db_path,
            watch_state_path=app.state.watch_state_path,
        )
        return {"message": format_stats_message(stats_service.build(profile, minutes))}

    @app.get("/api/support/{profile_key}")
    def api_support(profile_key: str) -> dict[str, object]:
        profile = profile_by_key(profile_key)
        service = SupportSnapshotService(
            db_path=app.state.db_path,
            watch_state_path=app.state.watch_state_path,
        )
        return {"message": format_support_snapshot(service.build(profile))}

    return app


def _lifespan(db_path: Path, watch_state_path: Path, env_file: Path | None):
    @asynccontextmanager
    async def manager(app: FastAPI):
        app.state.db_path = db_path
        app.state.watch_state_path = watch_state_path
        app.state.commute_service = commute_service(db_path)
        app.state.clock = app.state.commute_service.clock
        app.state.decision_recorder = BotDecisionRecorder(db_path)
        app.state.latency_recorder = BotLatencyRecorder(db_path)
        app.state.notifier = build_notifier(load_pushover_config(env_file))
        app.state.watch_manager = WebWatchManager(
            store=WebWatchStore(watch_state_path),
            decision_builder=app.state.commute_service.build_decision,
            notifier=app.state.notifier,
            decision_recorder=app.state.decision_recorder,
            latency_recorder=app.state.latency_recorder,
        )
        loop = WatchLoop(app.state.watch_manager)
        app.state.watch_loop = loop
        await loop.start()
        try:
            yield
        finally:
            await loop.stop()

    return manager


def _selected_profile(selector: str, current_time: datetime) -> object:
    if selector == MORNING.key:
        return MORNING
    if selector == EVENING.key:
        return EVENING
    profile = choose_profile_for_time(current_time)
    if profile is None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Авто-выбор сейчас вне окна: утро {MORNING.window_start:%H:%M}-{MORNING.window_end:%H:%M}, "
                f"вечер {EVENING.window_start:%H:%M}-{EVENING.window_end:%H:%M}."
            ),
        )
    return profile


@contextmanager
def _service_errors(*, not_found_detail: str | None = None) -> Iterator[None]:
    try:
        yield
    except KeyError as exc:
        if not_found_detail is None:
            raise _service_unavailable(exc) from exc
        raise HTTPException(status_code=404, detail=not_found_detail) from exc
    except Exception as exc:
        raise _service_unavailable(exc) from exc


def _notification_payload(app: FastAPI) -> dict[str, object]:
    status = app.state.notifier.status()
    return {
        "provider": status.provider,
        "configured": status.configured,
        "detail": status.detail,
    }


def _service_unavailable(error: Exception) -> HTTPException:
    return HTTPException(
        status_code=503,
        detail=sanitize_diagnostic_text(error, fallback=type(error).__name__, limit=HTTP_ERROR_DETAIL_LIMIT),
    )
