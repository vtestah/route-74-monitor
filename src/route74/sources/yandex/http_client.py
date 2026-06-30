from __future__ import annotations

from math import isfinite
import re
from typing import Any

import httpx

from route74.domain.commute import CommuteProfile
from route74.sources.yandex.constants import (
    YANDEX_LINE_ID,
    YANDEX_USER_AGENT,
    YANDEX_VEHICLES_URL,
    route_map_url,
    viewport_params,
)
from route74.sources.yandex.models import YandexRawResponse, YandexSourceStatus


CSRF_PATTERNS = (
    re.compile(r'"csrfToken"\s*:\s*"([^"]+)"'),
    re.compile(r"csrfToken=([^&\"']+)"),
)
SESSION_PATTERNS = (
    re.compile(r'"sessionId"\s*:\s*"([^"]+)"'),
    re.compile(r"sessionId=([^&\"']+)"),
)
DIAGNOSTIC_TOKEN_PATTERNS = (
    re.compile(r"(?i)(csrfToken=)[^&\s\"'<>]+"),
    re.compile(r"(?i)(sessionId=)[^&\s\"'<>]+"),
    re.compile(r'(?i)("csrfToken"\s*:\s*")[^"]+(")'),
    re.compile(r'(?i)("sessionId"\s*:\s*")[^"]+(")'),
)
MAX_YANDEX_TOKEN_LENGTH = 512
MAX_HTTP_ERROR_REASON_LENGTH = 160


class YandexHttpClient:
    def __init__(self, timeout_seconds: float = 8.0) -> None:
        timeout_seconds = _validate_timeout_seconds(timeout_seconds)
        self._client = httpx.Client(
            timeout=timeout_seconds,
            headers={
                "User-Agent": YANDEX_USER_AGENT,
                "Accept": "application/json,text/plain,*/*",
                "Referer": "https://yandex.ru/maps/",
            },
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "YandexHttpClient":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def get_vehicles_info(self, profile: CommuteProfile) -> YandexRawResponse:
        try:
            route_url = route_map_url(profile)
            page = self._client.get(route_url)
            page.raise_for_status()
            raw_csrf = _first_match(CSRF_PATTERNS, page.text)
            if raw_csrf is None:
                return YandexRawResponse(None, YandexSourceStatus.NEEDS_SIGNATURE, "csrf_not_found")
            csrf = _request_token(raw_csrf)
            if csrf is None:
                return YandexRawResponse(None, YandexSourceStatus.NEEDS_SIGNATURE, "csrf_invalid")
            session_id = _request_token(_first_match(SESSION_PATTERNS, page.text))
            return self._get_with_token(profile, csrf, session_id)
        except httpx.TimeoutException:
            return YandexRawResponse(None, YandexSourceStatus.TIMEOUT, "http_timeout")
        except httpx.HTTPStatusError as exc:
            return _status_response(exc.response.status_code)
        except httpx.HTTPError as exc:
            return YandexRawResponse(
                None,
                YandexSourceStatus.UNAVAILABLE,
                _http_error_reason(exc),
            )

    def _get_with_token(
        self,
        profile: CommuteProfile,
        csrf: str,
        session_id: str | None,
    ) -> YandexRawResponse:
        params = {
            "ajax": "1",
            "csrfToken": csrf,
            "lang": "ru",
            "lineId": YANDEX_LINE_ID,
            "locale": "ru_RU",
            "type": "minibus",
            **viewport_params(profile),
        }
        if session_id:
            params["sessionId"] = session_id
        response = self._client.get(YANDEX_VEHICLES_URL, params=params)
        if response.status_code == 400:
            return YandexRawResponse(None, YandexSourceStatus.NEEDS_SIGNATURE, "bad_request_maybe_s")
        if response.status_code in {403, 429}:
            return YandexRawResponse(None, YandexSourceStatus.BLOCKED, f"http_{response.status_code}")
        response.raise_for_status()
        payload, parse_error = _json_payload(response, "vehicles_json_invalid")
        if parse_error is not None:
            return parse_error
        if _token_only(payload):
            refreshed = _refreshed_csrf_token(payload)
            if refreshed is None:
                return YandexRawResponse(
                    None,
                    YandexSourceStatus.PARSE_ERROR,
                    "refreshed_csrf_token_invalid",
                )
            response = self._client.get(YANDEX_VEHICLES_URL, params={**params, "csrfToken": refreshed})
            if response.status_code == 400:
                return YandexRawResponse(None, YandexSourceStatus.NEEDS_SIGNATURE, "bad_request_maybe_s")
            response.raise_for_status()
            payload, parse_error = _json_payload(response, "refreshed_vehicles_json_invalid")
            if parse_error is not None:
                return parse_error
        dict_payload = _dict_payload(payload)
        if dict_payload is None:
            return YandexRawResponse(None, YandexSourceStatus.PARSE_ERROR, "vehicles_json_not_object")
        return YandexRawResponse(dict_payload, YandexSourceStatus.OK)


def _first_match(patterns: tuple[re.Pattern[str], ...], text: str) -> str | None:
    for pattern in patterns:
        match = pattern.search(text)
        if match:
            return match.group(1).replace("\\/", "/")
    return None


def _token_only(payload: Any) -> bool:
    return isinstance(payload, dict) and set(payload) == {"csrfToken"}


def _refreshed_csrf_token(payload: dict[str, Any]) -> str | None:
    return _request_token(payload.get("csrfToken"))


def _request_token(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    token = value.strip()
    if (
        not token
        or token != value
        or len(token) > MAX_YANDEX_TOKEN_LENGTH
        or any(character.isspace() or ord(character) < 32 or ord(character) == 127 for character in token)
    ):
        return None
    return token


def _json_payload(response: httpx.Response, reason: str) -> tuple[Any | None, YandexRawResponse | None]:
    try:
        return response.json(), None
    except ValueError:
        return None, YandexRawResponse(None, YandexSourceStatus.PARSE_ERROR, reason)


def _dict_payload(payload: Any) -> dict[str, object] | None:
    return payload if isinstance(payload, dict) else None


def _status_response(status_code: int) -> YandexRawResponse:
    if status_code == 400:
        return YandexRawResponse(None, YandexSourceStatus.NEEDS_SIGNATURE, "http_400")
    if status_code in {403, 429}:
        return YandexRawResponse(None, YandexSourceStatus.BLOCKED, f"http_{status_code}")
    return YandexRawResponse(None, YandexSourceStatus.UNAVAILABLE, f"http_{status_code}")


def _http_error_reason(error: httpx.HTTPError) -> str:
    error_type = type(error).__name__
    message = _redact_http_diagnostic(str(error))
    if not message:
        return f"http_error:{error_type}"
    return f"http_error:{error_type}:{message}"[:MAX_HTTP_ERROR_REASON_LENGTH]


def _redact_http_diagnostic(value: str) -> str:
    text = " ".join(value.split())
    for pattern in DIAGNOSTIC_TOKEN_PATTERNS:
        text = pattern.sub(_redacted_token_match, text)
    return text


def _redacted_token_match(match: re.Match[str]) -> str:
    if len(match.groups()) == 2:
        return f"{match.group(1)}<redacted>{match.group(2)}"
    return f"{match.group(1)}<redacted>"


def _validate_timeout_seconds(timeout_seconds: float) -> float:
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, int | float)
        or not isfinite(timeout_seconds)
        or timeout_seconds <= 0
    ):
        raise ValueError("Yandex HTTP timeout must be a positive finite number")
    return float(timeout_seconds)
