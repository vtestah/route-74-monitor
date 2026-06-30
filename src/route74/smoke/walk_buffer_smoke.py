from __future__ import annotations

from route74.domain.walk_buffer import MAX_WALK_MINUTES, MIN_WALK_MINUTES, is_valid_walk_minutes


def main() -> None:
    _assert_equal(is_valid_walk_minutes(MIN_WALK_MINUTES), True)
    _assert_equal(is_valid_walk_minutes(MAX_WALK_MINUTES), True)
    _assert_equal(is_valid_walk_minutes(MIN_WALK_MINUTES - 1), False)
    _assert_equal(is_valid_walk_minutes(MAX_WALK_MINUTES + 1), False)
    _assert_equal(is_valid_walk_minutes(True), False)
    _assert_equal(is_valid_walk_minutes(12.5), False)
    _assert_equal(is_valid_walk_minutes("12"), False)
    print("OK | walk buffer smoke passed")


def _assert_equal(actual: object, expected: object) -> None:
    if actual != expected:
        raise AssertionError(f"expected {expected!r}, got {actual!r}")


if __name__ == "__main__":
    main()
