from __future__ import annotations

from collections.abc import Callable
from datetime import time, timezone

from route74.cli.common import profile_from_name, profiles_from_name
from route74.domain.commute import CommuteProfile
from route74.domain.profile_registry import build_profile_registry, profile_for_time
from route74.domain.profiles import (
    ALL_PROFILES_KEY,
    EVENING,
    MORNING,
    PROFILES,
    PROFILES_BY_KEY,
    PROFILE_KEYS,
    PROFILE_SELECTORS,
    profile_by_key,
    profiles_for_selector,
)


def main() -> None:
    _assert_equal(PROFILES, (MORNING, EVENING))
    _assert_equal(PROFILE_KEYS, ("morning", "evening"))
    _assert_equal(PROFILE_SELECTORS, ("morning", "evening", ALL_PROFILES_KEY))
    _assert_equal(PROFILES_BY_KEY["morning"], MORNING)
    _assert_equal(PROFILES_BY_KEY["evening"], EVENING)
    _assert_equal(profile_by_key("morning"), MORNING)
    _assert_equal(profile_by_key("evening"), EVENING)
    _assert_equal(profiles_for_selector("morning"), (MORNING,))
    _assert_equal(profiles_for_selector("evening"), (EVENING,))
    _assert_equal(profiles_for_selector("all"), (MORNING, EVENING))
    _assert_profile_time_lookup()
    _assert_equal(profile_from_name("morning"), MORNING)
    _assert_equal(profile_from_name("evening"), EVENING)
    _assert_equal(profiles_from_name("all"), (MORNING, EVENING))
    _assert_rejects(lambda: profile_by_key("night"), "unknown profile")
    _assert_rejects(lambda: profile_by_key(" morning "), "unknown profile")
    _assert_rejects(lambda: profile_by_key(["morning"]), "unknown profile")
    _assert_rejects(lambda: profiles_for_selector("night"), "unknown profile")
    _assert_rejects(lambda: profiles_for_selector(" all "), "unknown profile")
    _assert_rejects(lambda: profiles_for_selector(None), "unknown profile")
    _assert_rejects(lambda: profile_from_name("night"), "unknown profile")
    _assert_rejects(lambda: profiles_from_name("night"), "unknown profile")
    _exercise_profile_guards()
    _exercise_registry_guards()
    print("OK | profiles smoke passed")


def _exercise_profile_guards() -> None:
    _assert_rejects(
        lambda: _profile("padded-key ", time(6, 0), time(7, 0)),
        "key",
    )
    _assert_rejects(
        lambda: _profile("bad key", time(6, 0), time(7, 0)),
        "plain key",
    )
    _assert_rejects(
        lambda: _profile("утро", time(6, 0), time(7, 0)),
        "plain key",
    )
    _assert_rejects(
        lambda: _profile("bad-key", time(6, 0), time(7, 0)),
        "plain key",
    )
    _assert_rejects(
        lambda: _profile("bad_stop", time(6, 0), time(7, 0), live_stop_id="stop 740"),
        "plain key",
    )
    _assert_rejects(
        lambda: _profile("bad_stop", time(6, 0), time(7, 0), live_stop_id="stop-740"),
        "plain key",
    )
    _assert_rejects(
        lambda: _profile("bad_title", time(6, 0), time(7, 0), title="Bad\nTitle"),
        "compact single-line",
    )
    _assert_rejects(
        lambda: _profile("bad_destination", time(6, 0), time(7, 0), destination="Stop  name"),
        "compact single-line",
    )
    _assert_rejects(
        lambda: _profile("bad_start", "06:00", time(7, 0)),
        "window",
    )
    _assert_rejects(
        lambda: _profile("bad_end", time(6, 0), "07:00"),
        "window",
    )
    _assert_rejects(
        lambda: _profile("tz_window", time(6, 0, tzinfo=timezone.utc), time(7, 0)),
        "timezone-naive",
    )
    _assert_rejects(
        lambda: _profile("second_window", time(6, 0, 1), time(7, 0)),
        "minute precision",
    )
    _assert_rejects(
        lambda: _profile("bad_note", time(6, 0), time(7, 0), walk_note=object()),
        "walk note",
    )
    _assert_rejects(
        lambda: _profile("multiline_note", time(6, 0), time(7, 0), walk_note="подъезд\nлифт"),
        "compact single-line",
    )
    _assert_rejects(
        lambda: _profile("padded_note", time(6, 0), time(7, 0), walk_note="  подъезд  "),
        "compact single-line",
    )
    _assert_rejects(
        lambda: _profile("long_note", time(6, 0), time(7, 0), walk_note="x" * 121),
        "compact single-line",
    )


def _assert_profile_time_lookup() -> None:
    night = _profile("night", time(23, 0), time(23, 30))
    profiles = (MORNING, EVENING, night)
    _assert_equal(profile_for_time(profiles, time(6, 0)), MORNING)
    _assert_equal(profile_for_time(profiles, time(10, 59, 59)), MORNING)
    _assert_equal(profile_for_time(profiles, time(16, 59, 59)), None)
    _assert_equal(profile_for_time(profiles, time(17, 0)), EVENING)
    _assert_equal(profile_for_time(profiles, time(23, 15)), night)
    _assert_rejects(lambda: profile_for_time(profiles, "23:15"), "profile lookup time")
    _assert_rejects(lambda: profile_for_time(profiles, time(23, 15, tzinfo=timezone.utc)), "timezone-naive")
    _assert_rejects(lambda: profile_for_time((MORNING, object()), time(7, 0)), "CommuteProfile")
    _assert_rejects(
        lambda: profile_for_time(None, time(7, 0)),  # type: ignore[arg-type]
        "iterable profiles",
    )


def _exercise_registry_guards() -> None:
    overlap = _profile("overlap", time(10, 59), time(11, 30))
    selector_collision = _profile("all", time(11, 0), time(11, 30))
    duplicate_stop = _profile(
        "duplicate_stop",
        time(11, 0),
        time(11, 30),
        live_stop_id=MORNING.live_stop_id,
    )

    _assert_rejects(
        lambda: build_profile_registry((), all_profiles_key="all"),
        "needs profiles",
    )
    _assert_rejects(
        lambda: build_profile_registry(None, all_profiles_key="all"),  # type: ignore[arg-type]
        "iterable profiles",
    )
    _assert_rejects(
        lambda: build_profile_registry((MORNING,), all_profiles_key=" all "),
        "all profiles selector",
    )
    _assert_rejects(
        lambda: build_profile_registry((MORNING,), all_profiles_key="all profiles"),
        "plain key",
    )
    _assert_rejects(
        lambda: build_profile_registry((MORNING,), all_profiles_key="all-profiles"),
        "plain key",
    )
    _assert_rejects(
        lambda: build_profile_registry((MORNING,), all_profiles_key="все"),
        "plain key",
    )
    _assert_rejects(
        lambda: build_profile_registry((MORNING, MORNING), all_profiles_key="all"),
        "duplicate profile key",
    )
    _assert_rejects(
        lambda: build_profile_registry((MORNING, duplicate_stop), all_profiles_key="all"),
        "duplicate live stop id",
    )
    _assert_rejects(
        lambda: build_profile_registry((selector_collision,), all_profiles_key="all"),
        "all selector",
    )
    _assert_rejects(
        lambda: build_profile_registry((object(),), all_profiles_key="all"),
        "CommuteProfile",
    )
    _assert_rejects(
        lambda: build_profile_registry((MORNING, overlap), all_profiles_key="all"),
        "windows overlap",
    )


def _profile(
    key: str,
    start: object,
    end: object,
    *,
    live_stop_id: str | None = None,
    title: str | None = None,
    destination: str | None = None,
    walk_note: object = "",
) -> CommuteProfile:
    return CommuteProfile(
        key=key,
        title=title or f"Test profile {key}",
        live_stop_id=live_stop_id or f"stop_{key}",
        destination=destination or "Test stop",
        window_start=start,  # type: ignore[arg-type]
        window_end=end,  # type: ignore[arg-type]
        default_walk_minutes=12,
        walk_note=walk_note,  # type: ignore[arg-type]
    )


def _assert_equal(actual: object, expected: object) -> None:
    if actual != expected:
        raise AssertionError(f"expected {expected!r}, got {actual!r}")


def _assert_rejects(call: Callable[[], object], expected_message: str) -> None:
    try:
        call()
    except ValueError as exc:
        if expected_message not in str(exc):
            raise AssertionError(f"expected {expected_message!r} in {exc!s}") from exc
        return
    raise AssertionError("expected ValueError")


if __name__ == "__main__":
    main()
