from __future__ import annotations

from typing import Any

import httpx

from route74.domain.profiles import MORNING
from route74.sources.yandex.http_client import YandexHttpClient
from route74.sources.yandex.models import YandexSourceStatus


def main() -> None:
    _run_invalid_page_csrf_smoke()
    _run_invalid_session_is_ignored_smoke()
    _run_invalid_refreshed_csrf_smoke()
    _run_http_error_reason_redacts_tokens_smoke()
    print("OK | yandex http token smoke passed")


def _run_invalid_page_csrf_smoke() -> None:
    client, fake = _client_with((_FakeResponse(text='{"csrfToken":"bad token"}'),))

    raw = client.get_vehicles_info(MORNING)

    _assert_equal(raw.status, YandexSourceStatus.NEEDS_SIGNATURE)
    _assert_equal(raw.reason, "csrf_invalid")
    _assert_equal(fake.request_count, 1)


def _run_invalid_session_is_ignored_smoke() -> None:
    client, fake = _client_with(
        (
            _FakeResponse(text='{"csrfToken":"good/token","sessionId":"bad session"}'),
            _FakeResponse(payload={"data": {"vehicles": []}}),
        )
    )

    raw = client.get_vehicles_info(MORNING)

    _assert_equal(raw.status, YandexSourceStatus.OK)
    _assert_equal(fake.params_history[0]["csrfToken"], "good/token")
    _assert_equal("sessionId" in fake.params_history[0], False)


def _run_invalid_refreshed_csrf_smoke() -> None:
    client, fake = _client_with(
        (
            _FakeResponse(text='{"csrfToken":"initial"}'),
            _FakeResponse(payload={"csrfToken": "bad\ncsrf"}),
            _FakeResponse(payload={"data": {"vehicles": []}}),
        )
    )

    raw = client.get_vehicles_info(MORNING)

    _assert_equal(raw.status, YandexSourceStatus.PARSE_ERROR)
    _assert_equal(raw.reason, "refreshed_csrf_token_invalid")
    _assert_equal(fake.request_count, 2)


def _run_http_error_reason_redacts_tokens_smoke() -> None:
    client = YandexHttpClient(timeout_seconds=0.1)
    client.close()
    setattr(
        client,
        "_client",
        _RaisingHttpClient(
            httpx.ConnectError(
                'boom csrfToken=query-secret&sessionId=session-secret {"csrfToken":"json-secret"}',
            ),
        ),
    )

    raw = client.get_vehicles_info(MORNING)

    _assert_equal(raw.status, YandexSourceStatus.UNAVAILABLE)
    _assert_contains(raw.reason, "http_error:ConnectError")
    _assert_contains(raw.reason, "csrfToken=<redacted>")
    _assert_contains(raw.reason, "sessionId=<redacted>")
    _assert_not_contains(raw.reason, "query-secret")
    _assert_not_contains(raw.reason, "session-secret")
    _assert_not_contains(raw.reason, "json-secret")


def _client_with(responses: tuple["_FakeResponse", ...]) -> tuple[YandexHttpClient, "_FakeHttpClient"]:
    client = YandexHttpClient(timeout_seconds=0.1)
    client.close()
    fake = _FakeHttpClient(responses)
    setattr(client, "_client", fake)
    return client, fake


class _FakeHttpClient:
    def __init__(self, responses: tuple["_FakeResponse", ...]) -> None:
        self._responses = list(responses)
        self.params_history: list[dict[str, Any]] = []
        self.request_count = 0

    def get(self, _url: str, **kwargs: object) -> "_FakeResponse":
        self.request_count += 1
        params = kwargs.get("params")
        if isinstance(params, dict):
            self.params_history.append(params)
        if not self._responses:
            raise AssertionError("unexpected HTTP request")
        return self._responses.pop(0)

    def close(self) -> None:
        return None


class _RaisingHttpClient:
    def __init__(self, error: Exception) -> None:
        self._error = error

    def get(self, *_args: object, **_kwargs: object) -> "_FakeResponse":
        raise self._error

    def close(self) -> None:
        return None


class _FakeResponse:
    status_code = 200

    def __init__(
        self,
        *,
        text: str = "",
        payload: dict[str, object] | None = None,
    ) -> None:
        self.text = text
        self._payload = payload if payload is not None else {}

    def json(self) -> dict[str, object]:
        return self._payload

    def raise_for_status(self) -> None:
        return None


def _assert_equal(actual: object, expected: object) -> None:
    if actual != expected:
        raise AssertionError(f"expected {expected!r}, got {actual!r}")


def _assert_contains(actual: str, expected: str) -> None:
    if expected not in actual:
        raise AssertionError(f"expected {expected!r} in {actual!r}")


def _assert_not_contains(actual: str, unexpected: str) -> None:
    if unexpected in actual:
        raise AssertionError(f"did not expect {unexpected!r} in {actual!r}")


if __name__ == "__main__":
    main()
