from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from route74.domain.commute import CommuteProfile
from route74.domain.traffic import RouteTrafficSnapshot
from route74.sources.yandex.browser_client import ReusableChromium, launch_chromium
from route74.sources.yandex.browser_rate_limit import run_with_browser_slot
from route74.sources.yandex.constants import YANDEX_USER_AGENT, route_traffic_url


MAX_ROUTE_DURATION_SECONDS = 6 * 60 * 60
MAX_ROUTE_DISTANCE_METERS = 100_000


@dataclass(frozen=True)
class YandexRouteSummary:
    text: str
    active: bool = False


class YandexRouteTrafficSource:
    def __init__(
        self,
        *,
        timeout_seconds: float = 8.0,
        min_interval_seconds: float = 1.0,
        persistent_browser: bool = False,
        browser_session: ReusableChromium | None = None,
    ) -> None:
        self._timeout_seconds = timeout_seconds
        self._min_interval_seconds = min_interval_seconds
        self._session = browser_session or (ReusableChromium() if persistent_browser else None)

    def close(self) -> None:
        if self._session is not None:
            self._session.close()

    def __call__(self, profile: CommuteProfile, sampled_at: datetime) -> RouteTrafficSnapshot:
        return self.get_traffic(profile, sampled_at)

    def get_traffic(self, profile: CommuteProfile, sampled_at: datetime) -> RouteTrafficSnapshot:
        try:
            return run_with_browser_slot(
                lambda: self._get_traffic_unlimited(profile, sampled_at),
                self._min_interval_seconds,
            )
        except Exception as exc:
            return _unavailable(_error_reason(exc), profile.key, sampled_at, route_traffic_url(profile))

    def _get_traffic_unlimited(self, profile: CommuteProfile, sampled_at: datetime) -> RouteTrafficSnapshot:
        try:
            from playwright.sync_api import Error as PlaywrightError
            from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
            from playwright.sync_api import sync_playwright
        except ImportError:
            return _unavailable("playwright_not_installed", profile.key, sampled_at)

        summaries: tuple[YandexRouteSummary, ...] = ()
        page_url = route_traffic_url(profile)
        try:
            if self._session is not None:
                page = self._session.new_page(
                    locale="ru-RU",
                    user_agent=YANDEX_USER_AGENT,
                    viewport={"width": 1280, "height": 900},
                )
                try:
                    page.goto(page_url, wait_until="domcontentloaded", timeout=self._timeout_seconds * 1000)
                    summaries = _wait_for_route_summaries(page, self._timeout_seconds)
                finally:
                    _close_page(page)
                return traffic_from_route_summaries(profile.key, sampled_at, summaries, page_url)

            with sync_playwright() as playwright:
                browser = launch_chromium(playwright)
                try:
                    page = browser.new_page(
                        locale="ru-RU",
                        user_agent=YANDEX_USER_AGENT,
                        viewport={"width": 1280, "height": 900},
                    )
                    page.goto(page_url, wait_until="domcontentloaded", timeout=self._timeout_seconds * 1000)
                    summaries = _wait_for_route_summaries(page, self._timeout_seconds)
                finally:
                    browser.close()
        except PlaywrightTimeoutError:
            return _unavailable("browser_timeout", profile.key, sampled_at, page_url)
        except PlaywrightError as exc:
            if self._session is not None:
                self._session.close()
            return _unavailable(str(exc), profile.key, sampled_at, page_url)

        return traffic_from_route_summaries(profile.key, sampled_at, summaries, page_url)


def traffic_from_route_summaries(
    profile_key: str,
    sampled_at: datetime,
    summaries: tuple[YandexRouteSummary, ...],
    page_url: str = "",
) -> RouteTrafficSnapshot:
    summary = _selected_summary(summaries)
    if summary is None:
        return _unavailable("route_summary_not_found", profile_key, sampled_at, page_url)
    duration_seconds = _duration_seconds(summary.text)
    if duration_seconds is None:
        return _unavailable("route_duration_not_found", profile_key, sampled_at, page_url, summary.text)
    distance_meters = _distance_meters(summary.text)
    return RouteTrafficSnapshot(
        provider="yandex_route_dom",
        status="ok",
        route_duration_seconds=duration_seconds,
        route_duration_in_traffic_seconds=duration_seconds,
        distance_meters=distance_meters,
        raw={
            "profile_key": profile_key,
            "sampled_at": sampled_at.isoformat(),
            "page_url": page_url,
            "active_text": summary.text,
            "alternatives": [item.text for item in summaries],
        },
    )


def _selected_summary(summaries: tuple[YandexRouteSummary, ...]) -> YandexRouteSummary | None:
    for summary in summaries:
        if summary.active and _duration_seconds(summary.text) is not None:
            return summary
    for summary in summaries:
        if _duration_seconds(summary.text) is not None:
            return summary
    return summaries[0] if summaries else None


def _wait_for_route_summaries(page: Any, timeout_seconds: float) -> tuple[YandexRouteSummary, ...]:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        summaries = _route_summaries(page)
        if summaries:
            return summaries
        page.wait_for_timeout(300)
    return ()


def _close_page(page: Any) -> None:
    try:
        page.close()
    except Exception:
        pass


def _route_summaries(page: Any) -> tuple[YandexRouteSummary, ...]:
    raw_items = page.evaluate(
        """() => {
            const textOf = (el) => {
                const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
                const parts = [];
                while (walker.nextNode()) {
                    const text = (walker.currentNode.textContent || '').replace(/\\s+/g, ' ').trim();
                    if (text) parts.push(text);
                }
                return parts.join(' ');
            };
            return Array.from(document.querySelectorAll('.route-snippet-view._type_auto, [role="listitem"]'))
            .map((el) => ({
                text: textOf(el),
                active: el.classList.contains('_active') || /Активный маршрут/.test(el.getAttribute('aria-label') || '')
            }))
            .filter((item) => /\\d+\\s*(мин|ч)/.test(item.text) && /(км|\\d+\\s*м(?!ин))/.test(item.text))
            .slice(0, 5);
        }"""
    )
    if not isinstance(raw_items, list):
        return ()
    summaries: list[YandexRouteSummary] = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        text = item.get("text")
        if isinstance(text, str) and text:
            summaries.append(YandexRouteSummary(text=text, active=bool(item.get("active"))))
    return tuple(summaries)


def _duration_seconds(text: str) -> int | None:
    hours = 0
    hour_match = re.search(r"(\d+)\s*ч", text)
    if hour_match:
        hours = int(hour_match.group(1))
    minute_match = re.search(r"(\d+)\s*мин", text)
    minutes = int(minute_match.group(1)) if minute_match else 0
    if hours == 0 and minute_match is None:
        return None
    duration = hours * 3600 + minutes * 60
    return duration if 0 < duration <= MAX_ROUTE_DURATION_SECONDS else None


def _distance_meters(text: str) -> int | None:
    for kilometer_match in re.finditer(r"(\d+(?:[,.]\d+)?)\s*км", text):
        kilometers = float(kilometer_match.group(1).replace(",", "."))
        if 0 < kilometers < 100:
            return round(kilometers * 1000)
    compact_kilometer_match = re.search(r"(\d{1,2}(?:[,.]\d+)?)\s*км", text)
    if compact_kilometer_match:
        meters = round(float(compact_kilometer_match.group(1).replace(",", ".")) * 1000)
        return meters if 0 < meters < MAX_ROUTE_DISTANCE_METERS else None
    meter_match = re.search(r"(\d+)\s*м(?!ин)", text)
    if meter_match:
        meters = int(meter_match.group(1))
        return meters if 0 < meters < MAX_ROUTE_DISTANCE_METERS else None
    return None


def _unavailable(
    reason: str,
    profile_key: str,
    sampled_at: datetime,
    page_url: str = "",
    summary_text: str = "",
) -> RouteTrafficSnapshot:
    return RouteTrafficSnapshot(
        provider="yandex_route_dom",
        status="unavailable",
        raw={
            "profile_key": profile_key,
            "sampled_at": sampled_at.isoformat(),
            "page_url": page_url,
            "reason": reason[:240],
            "summary_text": summary_text,
        },
    )


def _error_reason(error: Exception) -> str:
    detail = str(error).strip() or type(error).__name__
    return f"{type(error).__name__}:{detail}"[:240]
