from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from route74.sources.yandex.browser_client import capture_prediction_response, click_vehicle_markers, launch_chromium
from route74.sources.yandex.browser_rate_limit import run_with_browser_slot
from route74.sources.yandex.constants import YANDEX_USER_AGENT


@dataclass(frozen=True)
class YandexDumpEntry:
    method: str
    status: int
    url: str
    payload: dict[str, Any] | None

    def to_jsonable(self) -> dict[str, Any]:
        return {
            "method": self.method,
            "status": self.status,
            "url": self.url,
            "payload": self.payload,
        }


@dataclass(frozen=True)
class YandexDumpResult:
    url: str
    entries: tuple[YandexDumpEntry, ...]

    def to_jsonable(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "entries": [entry.to_jsonable() for entry in self.entries],
        }


def capture_masstransit_dump(
    url: str,
    *,
    timeout_seconds: float = 12.0,
    click_vehicles: bool = True,
    min_interval_seconds: float = 1.0,
) -> YandexDumpResult:
    return run_with_browser_slot(
        lambda: _capture_masstransit_dump_unlimited(
            url,
            timeout_seconds=timeout_seconds,
            click_vehicles=click_vehicles,
        ),
        min_interval_seconds,
    )


def _capture_masstransit_dump_unlimited(
    url: str,
    *,
    timeout_seconds: float,
    click_vehicles: bool,
) -> YandexDumpResult:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError("playwright_not_installed") from exc

    entries: list[YandexDumpEntry] = []
    prediction_payloads: list[dict[str, object]] = []
    deadline = time.monotonic() + timeout_seconds
    with sync_playwright() as playwright:
        browser = launch_chromium(playwright)
        try:
            page = browser.new_page(
                locale="ru-RU",
                user_agent=YANDEX_USER_AGENT,
                viewport={"width": 1280, "height": 900},
            )
            page.on(
                "response",
                lambda response: _capture_dump_response(response, entries, prediction_payloads),
            )
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_seconds * 1000)
            _wait_for_entries(page, entries, deadline)
            if click_vehicles and _remaining(deadline) > 0:
                click_vehicle_markers(page, prediction_payloads, _remaining(deadline))
            if _remaining(deadline) > 0:
                page.wait_for_timeout(min(1000, int(_remaining(deadline) * 1000)))
        finally:
            browser.close()
    return YandexDumpResult(url=url, entries=tuple(entries))


def _capture_dump_response(
    response: Any,
    entries: list[YandexDumpEntry],
    prediction_payloads: list[dict[str, object]],
) -> None:
    capture_prediction_response(response, prediction_payloads)
    if "/maps/api/masstransit/" not in response.url:
        return
    payload: dict[str, Any] | None = None
    try:
        raw_payload = response.json()
    except Exception:
        raw_payload = None
    if isinstance(raw_payload, dict):
        payload = raw_payload
    entries.append(
        YandexDumpEntry(
            method=_method_name(response.url),
            status=int(getattr(response, "status", 0) or 0),
            url=response.url,
            payload=payload,
        )
    )


def _method_name(url: str) -> str:
    path = urlparse(url).path.rstrip("/")
    return path.rsplit("/", maxsplit=1)[-1] if path else ""


def _wait_for_entries(page: Any, entries: list[YandexDumpEntry], deadline: float) -> None:
    while time.monotonic() < deadline and not entries:
        page.wait_for_timeout(300)


def _remaining(deadline: float) -> float:
    return max(0.0, deadline - time.monotonic())
