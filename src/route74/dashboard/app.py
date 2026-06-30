from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse, Response

from route74.build_info import load_build_info
from route74.dashboard.assets import DASHBOARD_HTML, FAVICON_SVG
from route74.dashboard.data import (
    build_dashboard_summary,
    build_dashboard_support_snapshot,
    load_recent_samples,
    load_window_series,
)
from route74.dashboard.preview import (
    PreviewCaptureFn,
    dashboard_preview_cache_dir,
    load_dashboard_preview,
    load_dashboard_preview_image,
    refresh_dashboard_preview,
)
from route74.diagnostics import sanitize_diagnostic_text
from route74.storage import DEFAULT_DB, summarize_db_health_readonly
from route74.watch_state import DEFAULT_WATCH_STATE_PATH

HTTP_ERROR_DETAIL_LIMIT = 160


def create_app(
    db_path: Path = DEFAULT_DB,
    *,
    watch_state_path: Path = DEFAULT_WATCH_STATE_PATH,
    preview_cache_path: Path | None = None,
    preview_capture_fn: PreviewCaptureFn | None = None,
) -> FastAPI:
    app = FastAPI(title="Дашборд 74", version="0.1.0")
    app.state.db_path = Path(db_path)
    app.state.watch_state_path = Path(watch_state_path)
    app.state.preview_cache_path = (
        Path(preview_cache_path) if preview_cache_path is not None else dashboard_preview_cache_dir(app.state.db_path)
    )
    app.state.preview_capture_fn = preview_capture_fn

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return DASHBOARD_HTML

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon() -> Response:
        return Response(FAVICON_SVG, media_type="image/svg+xml")

    @app.get("/healthz")
    def healthz() -> dict[str, object]:
        try:
            health = summarize_db_health_readonly(app.state.db_path)
        except Exception as exc:
            raise _service_unavailable(exc) from exc
        return {
            "status": "ok" if health.healthy else "bad",
            "integrity_check": health.integrity_check,
            "quick_check": health.quick_check,
            "version": load_build_info().to_jsonable(),
        }

    @app.get("/api/summary")
    def api_summary() -> dict[str, object]:
        try:
            return build_dashboard_summary(
                app.state.db_path,
                watch_state_path=app.state.watch_state_path,
                preview_cache_path=app.state.preview_cache_path,
            )
        except Exception as exc:
            raise _service_unavailable(exc) from exc

    @app.get("/api/support-snapshot/{profile_key}")
    def api_support_snapshot(profile_key: str) -> dict[str, object]:
        try:
            return build_dashboard_support_snapshot(
                app.state.db_path,
                profile_key,
                watch_state_path=app.state.watch_state_path,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"Неизвестный профиль: {profile_key}") from exc
        except Exception as exc:
            raise _service_unavailable(exc) from exc

    @app.get("/api/preview/{profile_key}")
    def api_preview(profile_key: str) -> dict[str, object]:
        try:
            return {
                "preview": load_dashboard_preview(
                    app.state.preview_cache_path,
                    profile_key,
                )
            }
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"Неизвестный профиль: {profile_key}") from exc
        except Exception as exc:
            raise _service_unavailable(exc) from exc

    @app.post("/api/preview/{profile_key}/refresh")
    def api_preview_refresh(profile_key: str) -> dict[str, object]:
        try:
            return {
                "preview": refresh_dashboard_preview(
                    app.state.preview_cache_path,
                    profile_key,
                    capture_fn=app.state.preview_capture_fn,
                )
            }
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"Неизвестный профиль: {profile_key}") from exc
        except Exception as exc:
            raise _service_unavailable(exc) from exc

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
    def api_window_series(
        window_key: str,
        days: int = Query(default=30, ge=1, le=120),
    ) -> dict[str, object]:
        try:
            return load_window_series(app.state.db_path, window_key, days=days)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"Неизвестное отчётное окно: {window_key}") from exc
        except Exception as exc:
            raise _service_unavailable(exc) from exc

    @app.get("/api/recent-samples")
    def api_recent_samples(
        window: str | None = None,
        limit: int = Query(default=50, ge=1, le=200),
    ) -> dict[str, object]:
        try:
            return load_recent_samples(app.state.db_path, window_key=window, limit=limit)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"Неизвестное отчётное окно: {window}") from exc
        except Exception as exc:
            raise _service_unavailable(exc) from exc

    return app


def _service_unavailable(error: Exception) -> HTTPException:
    return HTTPException(
        status_code=503,
        detail=sanitize_diagnostic_text(
            error,
            fallback=type(error).__name__,
            limit=HTTP_ERROR_DETAIL_LIMIT,
        ),
    )
