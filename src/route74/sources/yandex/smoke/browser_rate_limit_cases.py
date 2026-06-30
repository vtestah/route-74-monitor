from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from tempfile import TemporaryDirectory

from route74.sources.yandex.browser_rate_limit import (
    _LOCK_PATH_ENV,
    run_with_browser_slot,
)
from route74.sources.yandex.smoke.assertions import assert_equal


def run_browser_rate_limit_smoke() -> None:
    with TemporaryDirectory() as temp_dir:
        lock_path = Path(temp_dir) / "browser.lock"
        with _temporary_lock_path(str(lock_path)):
            calls: list[str] = []
            result = run_with_browser_slot(lambda: calls.append("ok") or "done", 0)
            assert_equal(result, "done")
            assert_equal(calls, ["ok"])
            assert_equal(lock_path.exists(), True)

        with _temporary_lock_path(temp_dir):
            _assert_value_error(lambda: run_with_browser_slot(lambda: "bad", 0), "browser lock path")

    _assert_value_error(lambda: run_with_browser_slot(lambda: "bad", -1), "browser min interval")
    _assert_value_error(
        lambda: run_with_browser_slot(lambda: "bad", float("nan")),
        "browser min interval",
    )
    _assert_value_error(
        lambda: run_with_browser_slot(lambda: "bad", float("inf")),
        "browser min interval",
    )
    _assert_value_error(lambda: run_with_browser_slot(lambda: "bad", True), "browser min interval")


def _assert_value_error(action: Callable[[], object], expected: str) -> None:
    try:
        action()
    except ValueError as exc:
        if expected not in str(exc):
            raise AssertionError(f"expected {expected!r} in {str(exc)!r}") from exc
        return
    raise AssertionError(f"expected ValueError containing {expected!r}")


class _temporary_lock_path:
    def __init__(self, value: str) -> None:
        self._value = value
        self._previous: str | None = None

    def __enter__(self) -> None:
        self._previous = os.environ.get(_LOCK_PATH_ENV)
        os.environ[_LOCK_PATH_ENV] = self._value

    def __exit__(self, *_exc_info: object) -> None:
        if self._previous is None:
            os.environ.pop(_LOCK_PATH_ENV, None)
        else:
            os.environ[_LOCK_PATH_ENV] = self._previous
