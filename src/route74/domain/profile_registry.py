from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import time
from types import MappingProxyType

from route74.domain.commute import CommuteProfile


@dataclass(frozen=True)
class ProfileRegistry:
    profiles: tuple[CommuteProfile, ...]
    keys: tuple[str, ...]
    selectors: tuple[str, ...]
    by_key: Mapping[str, CommuteProfile]


def build_profile_registry(
    profiles: Iterable[CommuteProfile],
    *,
    all_profiles_key: str,
) -> ProfileRegistry:
    if not isinstance(all_profiles_key, str) or not all_profiles_key:
        raise ValueError("all profiles selector is required")
    if (
        all_profiles_key != all_profiles_key.strip()
        or not all_profiles_key.isascii()
        or any(not (char.isalnum() or char == "_") for char in all_profiles_key)
    ):
        raise ValueError("all profiles selector must be a plain key")
    profiles_tuple = _profiles_tuple(profiles)
    if not profiles_tuple:
        raise ValueError("profile registry needs profiles")

    by_key = _index_profiles(profiles_tuple, all_profiles_key)
    _validate_non_overlapping_windows(profiles_tuple)
    keys = tuple(profile.key for profile in profiles_tuple)
    selectors = (*keys, all_profiles_key)
    return ProfileRegistry(
        profiles=profiles_tuple,
        keys=keys,
        selectors=selectors,
        by_key=MappingProxyType(by_key),
    )


def profile_for_time(
    profiles: Iterable[CommuteProfile],
    current_time: time,
) -> CommuteProfile | None:
    profiles_tuple = _profiles_tuple(profiles)
    _validate_non_overlapping_windows(profiles_tuple)
    current = _profile_lookup_time(current_time)
    for profile in profiles_tuple:
        if profile.window_start <= current <= profile.window_end:
            return profile
    return None


def _profiles_tuple(profiles: Iterable[CommuteProfile]) -> tuple[CommuteProfile, ...]:
    try:
        profiles_tuple = tuple(profiles)
    except TypeError as exc:
        raise ValueError("profile registry needs iterable profiles") from exc
    for profile in profiles_tuple:
        if not isinstance(profile, CommuteProfile):
            raise ValueError("profile registry items must be CommuteProfile")
    return profiles_tuple


def _profile_lookup_time(current_time: time) -> time:
    if not isinstance(current_time, time):
        raise ValueError("profile lookup time must be a time")
    if current_time.tzinfo is not None:
        raise ValueError("profile lookup time must be timezone-naive")
    return current_time.replace(second=0, microsecond=0)


def _index_profiles(
    profiles: tuple[CommuteProfile, ...],
    all_profiles_key: str,
) -> dict[str, CommuteProfile]:
    indexed: dict[str, CommuteProfile] = {}
    stop_ids: dict[str, str] = {}
    for profile in profiles:
        if profile.key == all_profiles_key:
            raise ValueError(f"profile key conflicts with all selector: {profile.key}")
        if profile.key in indexed:
            raise ValueError(f"duplicate profile key: {profile.key}")
        owner = stop_ids.get(profile.live_stop_id)
        if owner is not None:
            raise ValueError(
                f"duplicate live stop id: {profile.live_stop_id} ({owner}, {profile.key})"
            )
        indexed[profile.key] = profile
        stop_ids[profile.live_stop_id] = profile.key
    return indexed


def _validate_non_overlapping_windows(profiles: tuple[CommuteProfile, ...]) -> None:
    for index, profile in enumerate(profiles):
        for other in profiles[index + 1 :]:
            if (
                profile.window_start <= other.window_end
                and other.window_start <= profile.window_end
            ):
                raise ValueError(
                    f"profile windows overlap: {profile.key} and {other.key}",
                )
