from __future__ import annotations

from collections.abc import Callable


def assert_equal(actual: object, expected: object) -> None:
    if actual != expected:
        raise AssertionError(f"expected {expected!r}, got {actual!r}")


def assert_rejects(factory: Callable[[], object], expected: str) -> None:
    try:
        factory()
    except ValueError as error:
        if expected not in str(error):
            raise AssertionError(f"expected {expected!r} in {str(error)!r}") from error
    else:
        raise AssertionError(f"expected validation error: {expected}")
