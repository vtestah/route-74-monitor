from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from route74.diagnostics import sanitize_diagnostic_text
from route74.domain.commute import CommuteProfile
from route74.domain.profiles import profile_by_key
from route74.models import now_local
from route74.sources.yandex.browser_client import launch_chromium
from route74.sources.yandex.browser_rate_limit import run_with_browser_slot
from route74.sources.yandex.constants import YANDEX_USER_AGENT, route_map_url


PREVIEW_DIRNAME = "dashboard-previews"
PREVIEW_IMAGE_NAME = "map.png"
PREVIEW_META_NAME = "map.json"
PREVIEW_FRESHNESS_FRESH_MINUTES = 30
PREVIEW_TIMEOUT_SECONDS = 18.0
LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class DashboardPreviewRecord:
    profile_key: str
    captured_at: datetime
    image_path: Path
    source_url: str

    def to_jsonable(self) -> dict[str, object]:
        return {
            "profile_key": self.profile_key,
            "captured_at": _dt(self.captured_at),
            "image_path": str(self.image_path),
            "source_url": self.source_url,
        }


PreviewCaptureFn = Callable[[CommuteProfile, Path], DashboardPreviewRecord]


def dashboard_preview_cache_dir(db_path: Path) -> Path:
    return Path(db_path).parent / PREVIEW_DIRNAME


def dashboard_preview_image_path(cache_dir: Path, profile_key: str) -> Path:
    return Path(cache_dir) / profile_key / PREVIEW_IMAGE_NAME


def dashboard_preview_meta_path(cache_dir: Path, profile_key: str) -> Path:
    return Path(cache_dir) / profile_key / PREVIEW_META_NAME


def load_dashboard_preview(
    cache_dir: Path,
    profile_key: str,
    *,
    current_time: datetime | None = None,
) -> dict[str, object]:
    profile = profile_by_key(profile_key)
    record = _read_dashboard_preview_record(cache_dir, profile_key)
    return _dashboard_preview_payload(profile, record, current_time=current_time, refresh_reason=None)


def refresh_dashboard_preview(
    cache_dir: Path,
    profile_key: str,
    *,
    current_time: datetime | None = None,
    capture_fn: PreviewCaptureFn | None = None,
) -> dict[str, object]:
    profile = profile_by_key(profile_key)
    current_time = current_time or now_local()
    capture = capture_fn or _capture_dashboard_preview
    image_path = dashboard_preview_image_path(cache_dir, profile_key)
    try:
        record = run_with_browser_slot(
            lambda: capture(profile, image_path),
            0.0,
        )
    except Exception as exc:
        return _dashboard_preview_payload(
            profile,
            None,
            current_time=current_time,
            refresh_reason=sanitize_diagnostic_text(exc, fallback=type(exc).__name__, limit=120),
            refresh_failed=True,
        )

    if not isinstance(record, DashboardPreviewRecord):
        raise TypeError("dashboard preview capture must return DashboardPreviewRecord")
    _write_dashboard_preview_record(cache_dir, record)
    return _dashboard_preview_payload(profile, record, current_time=current_time, refresh_reason=None)


def load_dashboard_preview_image(cache_dir: Path, profile_key: str) -> Path | None:
    record = _read_dashboard_preview_record(cache_dir, profile_key)
    if record is None or not record.image_path.exists():
        return None
    return record.image_path


def _capture_dashboard_preview(profile: CommuteProfile, image_path: Path) -> DashboardPreviewRecord:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError("playwright_not_installed") from exc

    image_path.parent.mkdir(parents=True, exist_ok=True)
    temp_image_path = image_path.with_name(f"{image_path.stem}.tmp{image_path.suffix}")
    with sync_playwright() as playwright:
        browser = launch_chromium(playwright)
        try:
            page = browser.new_page(
                locale="ru-RU",
                user_agent=YANDEX_USER_AGENT,
                viewport={"width": 1280, "height": 900},
            )
            try:
                page.goto(
                    route_map_url(profile),
                    wait_until="domcontentloaded",
                    timeout=PREVIEW_TIMEOUT_SECONDS * 1000,
                )
                page.wait_for_timeout(2200)
                page.screenshot(path=str(temp_image_path), full_page=False)
            finally:
                _close_page(page)
        finally:
            browser.close()
    temp_image_path.replace(image_path)
    return DashboardPreviewRecord(
        profile_key=profile.key,
        captured_at=now_local(),
        image_path=image_path,
        source_url=route_map_url(profile),
    )


def _dashboard_preview_payload(
    profile: CommuteProfile,
    record: DashboardPreviewRecord | None,
    *,
    current_time: datetime | None,
    refresh_reason: str | None,
    refresh_failed: bool = False,
) -> dict[str, object]:
    current_time = current_time or now_local()
    if record is None:
        return {
            "profile_key": profile.key,
            "status": "no_confirmation",
            "fallback_text": "нет подтверждения",
            "captured_at": None,
            "captured_at_label": "",
            "freshness_label": "нет подтверждения",
            "age_minutes": None,
            "image_url": "",
            "source_url": route_map_url(profile),
            "reason": refresh_reason or "preview_missing",
            "refresh_failed": refresh_failed,
        }
    age_minutes = max(0, int((current_time - record.captured_at).total_seconds() // 60))
    freshness_label = "свежий" if age_minutes < PREVIEW_FRESHNESS_FRESH_MINUTES else "устарел"
    return {
        "profile_key": profile.key,
        "status": "ready",
        "fallback_text": "",
        "captured_at": _dt(record.captured_at),
        "captured_at_label": record.captured_at.strftime("%d.%m %H:%M"),
        "freshness_label": "свежий · только что"
        if age_minutes == 0
        else f"{freshness_label} · {age_minutes} мин назад",
        "age_minutes": age_minutes,
        "image_url": f"/api/preview/{profile.key}/image?v={record.captured_at.isoformat()}",
        "source_url": record.source_url,
        "reason": refresh_reason or "",
        "refresh_failed": refresh_failed,
    }


def _read_dashboard_preview_record(cache_dir: Path, profile_key: str) -> DashboardPreviewRecord | None:
    meta_path = dashboard_preview_meta_path(cache_dir, profile_key)
    image_path = dashboard_preview_image_path(cache_dir, profile_key)
    if not meta_path.exists() or not image_path.exists():
        return None
    try:
        payload = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    captured_at_value = payload.get("captured_at")
    if not isinstance(captured_at_value, str) or not captured_at_value:
        return None
    try:
        captured_at = datetime.fromisoformat(captured_at_value)
    except ValueError:
        return None
    source_url = payload.get("source_url")
    if not isinstance(source_url, str) or not source_url:
        source_url = route_map_url(profile_by_key(profile_key))
    return DashboardPreviewRecord(
        profile_key=profile_key,
        captured_at=captured_at,
        image_path=image_path,
        source_url=source_url,
    )


def _write_dashboard_preview_record(cache_dir: Path, record: DashboardPreviewRecord) -> None:
    record.image_path.parent.mkdir(parents=True, exist_ok=True)
    meta_path = dashboard_preview_meta_path(cache_dir, record.profile_key)
    temp_meta_path = meta_path.with_suffix(".json.tmp")
    temp_meta_path.write_text(json.dumps(record.to_jsonable(), ensure_ascii=False, indent=2), encoding="utf-8")
    temp_meta_path.replace(meta_path)


def _dt(value: datetime) -> str:
    return value.isoformat()


def _close_page(page: object) -> None:
    try:
        close = getattr(page, "close")
    except Exception as exc:
        LOGGER.debug(
            "dashboard preview page close unavailable: %s",
            sanitize_diagnostic_text(exc, fallback=type(exc).__name__, limit=120),
        )
        return
    try:
        close()
    except Exception as exc:
        LOGGER.debug(
            "dashboard preview page close failed: %s",
            sanitize_diagnostic_text(exc, fallback=type(exc).__name__, limit=120),
        )
