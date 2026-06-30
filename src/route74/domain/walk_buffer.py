from __future__ import annotations

MIN_WALK_MINUTES = 0
MAX_WALK_MINUTES = 60


def is_valid_walk_minutes(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and MIN_WALK_MINUTES <= value <= MAX_WALK_MINUTES
