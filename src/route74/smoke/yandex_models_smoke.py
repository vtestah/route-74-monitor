from __future__ import annotations

from collections.abc import Callable

from route74.sources.yandex.models import (
    MAX_YANDEX_DIAGNOSTIC_LENGTH,
    YandexLiveForecast,
    YandexRawResponse,
    YandexSourceMethod,
    YandexSourceStatus,
)


def main() -> None:
    _assert_forecast_diagnostics_are_compact()
    _assert_raw_reason_strips_control_characters()
    print("OK | yandex models smoke passed")


def _assert_forecast_diagnostics_are_compact() -> None:
    forecast = YandexLiveForecast(
        enabled=True,
        available=False,
        source_method=YandexSourceMethod.BROWSER,
        status=YandexSourceStatus.PARSE_ERROR,
        diagnostics=(
            " browser:parse_error\nblocked\tbad ",
            "",
            "x" * (MAX_YANDEX_DIAGNOSTIC_LENGTH + 10),
            "\x1b[31mansi\x1b[0m",
        ),
    )

    _assert_equal(forecast.diagnostics[0], "browser:parse_error blocked bad")
    _assert_equal(forecast.diagnostics[1], "x" * MAX_YANDEX_DIAGNOSTIC_LENGTH)
    _assert_equal(forecast.diagnostics[2], "[31mansi [0m")
    _assert_equal(len(forecast.diagnostics), 3)
    _assert_rejects(
        lambda: YandexLiveForecast(
            enabled=True,
            available=False,
            source_method=YandexSourceMethod.BROWSER,
            status=YandexSourceStatus.PARSE_ERROR,
            diagnostics=("ok", object()),  # type: ignore[arg-type]
        ),
        "diagnostics",
    )


def _assert_raw_reason_strips_control_characters() -> None:
    raw = YandexRawResponse(
        None,
        YandexSourceStatus.UNAVAILABLE,
        "\x1b[31mblocked\nby upstream\x00\x1b[0m",
    )

    _assert_equal(raw.reason, "[31mblocked by upstream [0m")


def _assert_equal(actual: object, expected: object) -> None:
    if actual != expected:
        raise AssertionError(f"expected {expected!r}, got {actual!r}")


def _assert_rejects(action: Callable[[], object], expected: str) -> None:
    try:
        action()
    except ValueError as error:
        if expected not in str(error):
            raise AssertionError(f"expected {expected!r} in {str(error)!r}") from error
    else:
        raise AssertionError(f"expected ValueError containing {expected!r}")


if __name__ == "__main__":
    main()
