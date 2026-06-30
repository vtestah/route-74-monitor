from __future__ import annotations

from route74.diagnostics import sanitize_command_text, sanitize_diagnostic_text


def main() -> None:
    _assert_prefixed_assignment_secrets_are_redacted()
    _assert_prefixed_json_secrets_are_redacted()
    _assert_command_text_preserves_paths_and_redacts_secrets()
    print("OK | diagnostics smoke passed")


def _assert_prefixed_assignment_secrets_are_redacted() -> None:
    text = sanitize_diagnostic_text(
        (
            "PUSHOVER_APP_TOKEN=env-secret "
            "YANDEX_API_KEY=api-secret "
            "route74_session_id=session-secret "
            "route74_secret:colon-secret"
        ),
        limit=400,
    )
    _assert_contains(text, "PUSHOVER_APP_TOKEN=<redacted>")
    _assert_contains(text, "YANDEX_API_KEY=<redacted>")
    _assert_contains(text, "route74_session_id=<redacted>")
    _assert_contains(text, "route74_secret:<redacted>")
    for secret in ("env-secret", "api-secret", "session-secret", "colon-secret"):
        _assert_not_contains(text, secret)


def _assert_prefixed_json_secrets_are_redacted() -> None:
    text = sanitize_diagnostic_text(
        ('{"pushover_app_token":"json-token","YANDEX_API_KEY":"json-api","csrfToken":"csrf-secret"}'),
        limit=400,
    )
    _assert_contains(text, '"pushover_app_token":"<redacted>"')
    _assert_contains(text, '"YANDEX_API_KEY":"<redacted>"')
    _assert_contains(text, '"csrfToken":"<redacted>"')
    for secret in ("json-token", "json-api", "csrf-secret"):
        _assert_not_contains(text, secret)


def _assert_command_text_preserves_paths_and_redacts_secrets() -> None:
    text = sanitize_command_text(
        "route74 watch-state --path /opt/route74/data/web_watches.json token=secret-value",
        limit=400,
    )
    _assert_contains(text, "/opt/route74/data/web_watches.json")
    _assert_contains(text, "token=<redacted>")
    _assert_not_contains(text, "secret-value")


def _assert_contains(actual: str, expected: str) -> None:
    if expected not in actual:
        raise AssertionError(f"expected {expected!r} in {actual!r}")


def _assert_not_contains(actual: str, unexpected: str) -> None:
    if unexpected in actual:
        raise AssertionError(f"did not expect {unexpected!r} in {actual!r}")


if __name__ == "__main__":
    main()
