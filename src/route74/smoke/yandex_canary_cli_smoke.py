from __future__ import annotations

from datetime import datetime
from pathlib import Path

from route74.cli.yandex_canary import format_yandex_canary_runs, strict_yandex_canary_message
from route74.models import NOVOSIBIRSK_TZ
from route74.storage.yandex_canary import YandexCanaryRun


def main() -> None:
    current_time = datetime(2026, 6, 4, 9, 15, tzinfo=NOVOSIBIRSK_TZ)
    raw_token = "123456:" + "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdef"
    run = YandexCanaryRun(
        id=1,
        checked_at=current_time,
        status="warning",
        source_method="vehicle_prediction",
        profile_key="morning",
        schema_hash="a" * 16,
        risk_reason=f"failed /home/vladimir/work-projects/74/.env token={raw_token} \x1b[31mboom\nnext",
        changed_keys=("vehicle_fields",),
    )

    formatted = format_yandex_canary_runs((run,), Path("/home/vladimir/work-projects/74/data/canary.sqlite"))
    strict = strict_yandex_canary_message((run,))

    _assert_contains(formatted, "db=<path>")
    _assert_contains(formatted, "reason=failed <path> token=<redacted> boom next")
    _assert_contains(strict, "morning:warning:failed <path> token=<redacted> boom next")
    for output in (formatted, strict):
        _assert_not_contains(output, raw_token)
        _assert_not_contains(output, "/home/vladimir")
        _assert_not_contains(output, "\x1b")
        _assert_not_contains(output, "\nnext")
    print("OK | yandex canary CLI smoke passed")


def _assert_contains(haystack: str, needle: str) -> None:
    if needle not in haystack:
        raise AssertionError(f"expected {needle!r} in output")


def _assert_not_contains(haystack: str, needle: str) -> None:
    if needle in haystack:
        raise AssertionError(f"did not expect {needle!r} in output")


if __name__ == "__main__":
    main()
