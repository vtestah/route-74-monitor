from __future__ import annotations

import os
import shutil
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs, urlparse

from route74.domain.commute import CommuteProfile
from route74.sources.yandex.browser_rate_limit import run_with_browser_slot
from route74.sources.yandex.constants import YANDEX_USER_AGENT, expected_thread_ids, route_map_url, stop_map_url
from route74.sources.yandex.models import YandexRawResponse, YandexSourceStatus
from route74.sources.yandex.parser.containers import find_vehicles
from route74.sources.yandex.parser.coordinates import coordinates
from route74.sources.yandex.parser.vehicle import thread_id, vehicle_id


CHROMIUM_EXECUTABLE_ENV = "ROUTE74_PLAYWRIGHT_CHROMIUM_EXECUTABLE"


class YandexBrowserClient:
    def __init__(
        self,
        timeout_seconds: float = 12.0,
        min_interval_seconds: float = 1.0,
        persistent_browser: bool = False,
        browser_session: ReusableChromium | None = None,
    ) -> None:
        self._timeout_seconds = timeout_seconds
        self._min_interval_seconds = min_interval_seconds
        self._session = browser_session or (ReusableChromium() if persistent_browser else None)
        self._line_payloads: list[dict[str, object]] = []

    def close(self) -> None:
        if self._session is not None:
            self._session.close()

    def get_vehicles_info(self, profile: CommuteProfile) -> YandexRawResponse:
        return self._capture_json_response(
            page_url=route_map_url(profile),
            response_token="getVehiclesInfo",
            empty_reason="browser_no_vehicles_response",
            invalid_json_reason="browser_vehicles_json_invalid",
            non_object_json_reason="browser_vehicles_json_not_object",
        )

    def get_stop_info(self, profile: CommuteProfile) -> YandexRawResponse:
        return self._capture_json_response(
            page_url=stop_map_url(profile),
            response_token="getStopInfo",
            empty_reason="browser_no_stop_info_response",
            invalid_json_reason="browser_stop_info_json_invalid",
            non_object_json_reason="browser_stop_info_json_not_object",
        )

    def get_vehicle_predictions(self, profile: CommuteProfile) -> YandexRawResponse:
        return run_with_browser_slot(
            lambda: self._get_vehicle_predictions_unlimited(profile),
            self._min_interval_seconds,
        )

    def consume_line_payloads(self) -> tuple[dict[str, object], ...]:
        payloads = tuple(self._line_payloads)
        self._line_payloads.clear()
        return payloads

    def _get_vehicle_predictions_unlimited(self, profile: CommuteProfile) -> YandexRawResponse:
        payloads: list[dict[str, object]] = []
        vehicle_threads: dict[str, str] = {}
        route_vehicles: list[dict[str, object]] = []
        parse_errors: list[str] = []

        def action(page: Any) -> YandexRawResponse:
            page.on(
                "response",
                lambda response: _capture_prediction_session_response(
                    response,
                    payloads,
                    vehicle_threads,
                    route_vehicles,
                    self._line_payloads,
                    parse_errors,
                ),
            )
            page.goto(
                route_map_url(profile),
                wait_until="domcontentloaded",
                timeout=self._timeout_seconds * 1000,
            )
            click_vehicle_markers(
                page,
                payloads,
                self._timeout_seconds,
                route_vehicles=route_vehicles,
                expected_threads=expected_thread_ids(profile),
            )
            if not payloads:
                if parse_errors:
                    return YandexRawResponse(None, YandexSourceStatus.PARSE_ERROR, parse_errors[-1])
                return YandexRawResponse(None, YandexSourceStatus.EMPTY, "browser_no_prediction_response")
            return YandexRawResponse({"predictions": payloads}, YandexSourceStatus.OK)

        return self._run_with_page(
            page_options={
                "locale": "ru-RU",
                "user_agent": YANDEX_USER_AGENT,
                "viewport": {"width": 1280, "height": 900},
            },
            action=action,
            timeout_reason="prediction_browser_timeout",
        )

    def _capture_json_response(
        self,
        *,
        page_url: str,
        response_token: str,
        empty_reason: str,
        invalid_json_reason: str,
        non_object_json_reason: str,
    ) -> YandexRawResponse:
        return run_with_browser_slot(
            lambda: self._capture_json_response_unlimited(
                page_url=page_url,
                response_token=response_token,
                empty_reason=empty_reason,
                invalid_json_reason=invalid_json_reason,
                non_object_json_reason=non_object_json_reason,
            ),
            self._min_interval_seconds,
        )

    def _capture_json_response_unlimited(
        self,
        *,
        page_url: str,
        response_token: str,
        empty_reason: str,
        invalid_json_reason: str,
        non_object_json_reason: str,
    ) -> YandexRawResponse:
        payloads: list[dict[str, object]] = []
        parse_errors: list[str] = []

        def action(page: Any) -> YandexRawResponse:
            page.on(
                "response",
                lambda response: _capture_json_session_response(
                    response,
                    payloads,
                    response_token,
                    self._line_payloads,
                    parse_errors,
                    invalid_json_reason,
                    non_object_json_reason,
                ),
            )
            page.goto(
                page_url,
                wait_until="domcontentloaded",
                timeout=self._timeout_seconds * 1000,
            )
            _wait_for_payload(page, payloads, self._timeout_seconds)
            return _captured_payload_response(payloads, parse_errors, empty_reason)

        return self._run_with_page(
            page_options={"locale": "ru-RU", "user_agent": YANDEX_USER_AGENT},
            action=action,
            timeout_reason="browser_timeout",
        )

    def _run_with_page(
        self,
        *,
        page_options: dict[str, object],
        action: Callable[[Any], YandexRawResponse],
        timeout_reason: str,
    ) -> YandexRawResponse:
        try:
            from playwright.sync_api import Error as PlaywrightError
            from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
            from playwright.sync_api import sync_playwright
        except ImportError:
            return YandexRawResponse(None, YandexSourceStatus.UNAVAILABLE, "playwright_not_installed")

        try:
            if self._session is not None:
                page = self._session.new_page(**page_options)
                try:
                    return action(page)
                finally:
                    _close_page(page)
            with sync_playwright() as playwright:
                browser = launch_chromium(playwright)
                try:
                    page = browser.new_page(**page_options)
                    try:
                        return action(page)
                    finally:
                        _close_page(page)
                finally:
                    browser.close()
        except PlaywrightTimeoutError:
            return YandexRawResponse(None, YandexSourceStatus.TIMEOUT, timeout_reason)
        except PlaywrightError as exc:
            if self._session is not None:
                self._session.close()
            return YandexRawResponse(None, YandexSourceStatus.UNAVAILABLE, str(exc))


class ReusableChromium:
    def __init__(self) -> None:
        self._playwright: Any | None = None
        self._browser: Any | None = None

    def new_page(self, **kwargs: object) -> Any:
        return self._browser_instance().new_page(**kwargs)

    def close(self) -> None:
        browser = self._browser
        self._browser = None
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass
        playwright = self._playwright
        self._playwright = None
        if playwright is not None:
            try:
                playwright.stop()
            except Exception:
                pass

    def _browser_instance(self) -> Any:
        if self._browser is not None and _browser_connected(self._browser):
            return self._browser
        self.close()
        from playwright.sync_api import sync_playwright

        self._playwright = sync_playwright().start()
        self._browser = launch_chromium(self._playwright)
        return self._browser


def launch_chromium(playwright: Any) -> Any:
    executable_path = os.getenv(CHROMIUM_EXECUTABLE_ENV) or _system_chromium_executable()
    if executable_path:
        return playwright.chromium.launch(headless=True, executable_path=executable_path)
    return playwright.chromium.launch(headless=True)


def _system_chromium_executable() -> str | None:
    for command in ("google-chrome", "chromium", "chromium-browser"):
        path = shutil.which(command)
        if path:
            return path
    return None


def _browser_connected(browser: Any) -> bool:
    try:
        return bool(browser.is_connected())
    except Exception:
        return False


def _close_page(page: Any) -> None:
    try:
        page.close()
    except Exception:
        pass


def _capture_response(
    response: Any,
    payloads: list[dict[str, object]],
    response_token: str,
    parse_errors: list[str],
    invalid_json_reason: str,
    non_object_json_reason: str,
) -> None:
    if response_token not in response.url:
        return
    try:
        payload = response.json()
    except Exception:
        parse_errors.append(invalid_json_reason)
        return
    if isinstance(payload, dict):
        payloads.append(payload)
    else:
        parse_errors.append(non_object_json_reason)


def _capture_json_session_response(
    response: Any,
    payloads: list[dict[str, object]],
    response_token: str,
    line_payloads: list[dict[str, object]],
    parse_errors: list[str],
    invalid_json_reason: str,
    non_object_json_reason: str,
) -> None:
    _capture_line_response(response, line_payloads)
    _capture_response(
        response,
        payloads,
        response_token,
        parse_errors,
        invalid_json_reason,
        non_object_json_reason,
    )


def _capture_line_response(response: Any, line_payloads: list[dict[str, object]]) -> None:
    if "getLine" not in response.url:
        return
    try:
        payload = response.json()
    except Exception:
        return
    if isinstance(payload, dict):
        line_payloads.append(payload)


def capture_prediction_response(
    response: Any,
    payloads: list[dict[str, object]],
    vehicle_threads: dict[str, str] | None = None,
    parse_errors: list[str] | None = None,
) -> None:
    if "getVehiclePredictionInfo" not in response.url:
        return
    try:
        payload = response.json()
    except Exception:
        _append_parse_error(parse_errors, "vehicle_prediction_json_invalid")
        return
    if not isinstance(payload, dict):
        _append_parse_error(parse_errors, "vehicle_prediction_json_not_object")
        return
    data = payload.get("data")
    if not isinstance(data, dict):
        _append_parse_error(parse_errors, "vehicle_prediction_data_not_object")
        return
    vehicle_id = _query_value(response.url, "id")
    if vehicle_id:
        data = {**data, "vehicleId": vehicle_id}
        if vehicle_threads is not None and (thread := vehicle_threads.get(vehicle_id)):
            data = {**data, "threadId": thread}
    payloads.append(data)


def _capture_prediction_session_response(
    response: Any,
    payloads: list[dict[str, object]],
    vehicle_threads: dict[str, str],
    route_vehicles: list[dict[str, object]],
    line_payloads: list[dict[str, object]],
    parse_errors: list[str],
) -> None:
    _capture_line_response(response, line_payloads)
    _capture_route_vehicles(response, vehicle_threads, route_vehicles, parse_errors)
    capture_prediction_response(response, payloads, vehicle_threads, parse_errors)


def _capture_route_vehicles(
    response: Any,
    vehicle_threads: dict[str, str],
    route_vehicles: list[dict[str, object]],
    parse_errors: list[str],
) -> None:
    if "getVehiclesInfo" not in response.url:
        return
    try:
        payload = response.json()
    except Exception:
        parse_errors.append("browser_route_vehicles_json_invalid")
        return
    if not isinstance(payload, dict):
        return
    vehicles = find_vehicles(payload)
    if not vehicles:
        return
    route_vehicles.clear()
    for index, item in enumerate(vehicles):
        if not isinstance(item, dict):
            continue
        item_vehicle_id = vehicle_id(item, index)
        item_thread_id = thread_id(item)
        lat, lng = coordinates(item)
        if item_vehicle_id and item_thread_id:
            vehicle_threads[item_vehicle_id] = item_thread_id
        route_vehicles.append(
            {
                "vehicleId": item_vehicle_id,
                "threadId": item_thread_id,
                "lat": lat,
                "lng": lng,
            }
        )


def click_vehicle_markers(
    page: Any,
    payloads: list[dict[str, object]],
    timeout_seconds: float,
    *,
    route_vehicles: list[dict[str, object]] | None = None,
    expected_threads: tuple[str, ...] = (),
) -> None:
    deadline = time.monotonic() + timeout_seconds
    _wait_for_visible_markers(page, deadline)
    markers = _visible_vehicle_marker_centers(page)
    if route_vehicles and expected_threads:
        projected = _project_route_vehicles(page, route_vehicles)
        targets = _prediction_click_targets(markers, projected, expected_threads)
        if targets:
            markers = targets
        elif markers and projected:
            return
    for marker in markers:
        if time.monotonic() >= deadline:
            return
        before = len(payloads)
        page.mouse.click(marker["x"], marker["y"])
        _wait_for_new_payload(page, payloads, before, min(0.7, max(0.1, deadline - time.monotonic())))


def _wait_for_visible_markers(page: Any, deadline: float) -> None:
    while time.monotonic() < deadline:
        if _visible_vehicle_marker_centers(page):
            return
        page.wait_for_timeout(300)


def _visible_vehicle_marker_centers(page: Any) -> list[dict[str, float]]:
    markers = page.evaluate(
        """() => Array.from(document.querySelectorAll('ymaps.ymaps3x0--marker'))
            .map((el) => {
                const r = el.getBoundingClientRect();
                return {x: r.x + 5, y: r.y + 5, style: el.getAttribute('style') || ''};
            })
            .filter((item) => item.x > 420 && item.x < window.innerWidth && item.y > 0 && item.y < window.innerHeight)
            .slice(0, 32)"""
    )
    vehicle_markers = [marker for marker in markers if "z-index: 500" in str(marker.get("style", ""))]
    selected = vehicle_markers if vehicle_markers else markers
    return [
        {"x": float(marker["x"]), "y": float(marker["y"])}
        for marker in selected
        if isinstance(marker.get("x"), int | float) and isinstance(marker.get("y"), int | float)
    ]


def _project_route_vehicles(page: Any, route_vehicles: list[dict[str, object]]) -> list[dict[str, object]]:
    if not route_vehicles:
        return []
    return page.evaluate(
        """(vehicles) => {
            const map = window.yandex_map;
            if (!map || !map._projection || !map._camera || !map._size || !map._offset) {
                return [];
            }
            const projection = map._projection;
            const center = map._camera.worldCenter;
            const zoom = map._camera.zoom;
            const size = map._size;
            const offset = map._offset;
            const scale = 256 * Math.pow(2, zoom);
            return vehicles
                .filter((vehicle) => Number.isFinite(vehicle?.lat) && Number.isFinite(vehicle?.lng))
                .map((vehicle) => {
                    const world = projection.toWorldCoordinates([vehicle.lng, vehicle.lat]);
                    const x = offset.left + size.x / 2 + (world.x - center.x) * scale;
                    const y = offset.top + size.y / 2 + (center.y - world.y) * scale;
                    return {...vehicle, x, y};
                });
        }""",
        route_vehicles,
    )


def _prediction_click_targets(
    markers: list[dict[str, float]],
    projected_vehicles: list[dict[str, object]],
    expected_threads: tuple[str, ...],
) -> list[dict[str, float]]:
    if not markers or not projected_vehicles or not expected_threads:
        return []
    expected = set(expected_threads)
    ordered_markers = sorted(markers, key=lambda marker: (-marker["y"], marker["x"]))
    ordered_vehicles = sorted(
        (
            vehicle
            for vehicle in projected_vehicles
            if isinstance(vehicle.get("x"), int | float)
            and isinstance(vehicle.get("y"), int | float)
            and isinstance(vehicle.get("threadId"), str)
        ),
        key=lambda vehicle: (-float(vehicle["y"]), float(vehicle["x"])),
    )
    targets: list[dict[str, float]] = []
    for marker, vehicle in zip(ordered_markers, ordered_vehicles, strict=False):
        if str(vehicle["threadId"]) in expected:
            targets.append(marker)
    return targets


def _wait_for_new_payload(page: Any, payloads: list[dict[str, object]], before: int, timeout_seconds: float) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline and len(payloads) == before:
        page.wait_for_timeout(100)


def _query_value(url: str, key: str) -> str:
    values = parse_qs(urlparse(url).query).get(key, [])
    return values[0] if values else ""


def _wait_for_payload(page: Any, payloads: list[dict[str, object]], timeout_seconds: float) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline and not payloads:
        page.wait_for_timeout(500)


def _captured_payload_response(
    payloads: list[dict[str, object]],
    parse_errors: list[str],
    empty_reason: str,
) -> YandexRawResponse:
    if payloads:
        return YandexRawResponse(payloads[-1], YandexSourceStatus.OK)
    if parse_errors:
        return YandexRawResponse(None, YandexSourceStatus.PARSE_ERROR, parse_errors[-1])
    return YandexRawResponse(None, YandexSourceStatus.EMPTY, empty_reason)


def _append_parse_error(parse_errors: list[str] | None, reason: str) -> None:
    if parse_errors is not None:
        parse_errors.append(reason)
