from __future__ import annotations

from collections.abc import Mapping
from datetime import time

from route74.domain.commute import CommuteProfile
from route74.domain.profile_registry import build_profile_registry

MORNING = CommuteProfile(
    key="morning",
    title="Утром: Медицинский центр -> Академгородок / Цветной проезд",
    live_stop_id="740",
    destination="Цветной проезд",
    window_start=time(6, 0),
    window_end=time(10, 59),
    default_walk_minutes=12,
    walk_note="дом -> улица + пешком + запас",
)

EVENING = CommuteProfile(
    key="evening",
    title="Вечером: ВЦ -> Ул. Твардовского",
    live_stop_id="623",
    destination="Ул. Твардовского",
    window_start=time(17, 0),
    window_end=time(22, 59),
    default_walk_minutes=17,
    walk_note="2ГИС 8 + выйти из здания + запас",
)

ALL_PROFILES_KEY = "all"
_PROFILE_REGISTRY = build_profile_registry((MORNING, EVENING), all_profiles_key=ALL_PROFILES_KEY)
PROFILES: tuple[CommuteProfile, ...] = _PROFILE_REGISTRY.profiles
PROFILE_KEYS: tuple[str, ...] = _PROFILE_REGISTRY.keys
PROFILE_SELECTORS: tuple[str, ...] = _PROFILE_REGISTRY.selectors
PROFILES_BY_KEY: Mapping[str, CommuteProfile] = _PROFILE_REGISTRY.by_key


def profile_by_key(key: object) -> CommuteProfile:
    lookup_key = _plain_selector(key, PROFILE_KEYS)
    try:
        return PROFILES_BY_KEY[lookup_key]
    except KeyError as exc:
        raise _unknown_profile(key, PROFILE_KEYS) from exc


def profiles_for_selector(selector: object) -> tuple[CommuteProfile, ...]:
    lookup_key = _plain_selector(selector, PROFILE_SELECTORS)
    return PROFILES if lookup_key == ALL_PROFILES_KEY else (profile_by_key(lookup_key),)


def _plain_selector(value: object, expected: tuple[str, ...]) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise _unknown_profile(value, expected)
    return value


def _unknown_profile(value: object, expected: tuple[str, ...]) -> ValueError:
    return ValueError(f"unknown profile: {value!r} (expected {', '.join(expected)})")
