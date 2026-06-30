from __future__ import annotations

import argparse
import math
import os
from datetime import time
from pathlib import Path

from route74.domain.commute import CommuteProfile
from route74.domain.profiles import profile_by_key, profiles_for_selector
from route74.domain.walk_buffer import MAX_WALK_MINUTES, MIN_WALK_MINUTES, is_valid_walk_minutes
from route74.services.factory import commute_service as commute_service  # noqa: F401 — re-export


SQLITE_SIDECAR_SUFFIXES = (
    ".sqlite-journal",
    ".sqlite-wal",
    ".sqlite-shm",
    ".sqlite3-journal",
    ".sqlite3-wal",
    ".sqlite3-shm",
    ".db-journal",
    ".db-wal",
    ".db-shm",
)


def profile_from_name(name: str) -> CommuteProfile:
    return profile_by_key(name)


def profiles_from_name(name: str) -> tuple[CommuteProfile, ...]:
    return profiles_for_selector(name)


def local_time_hhmm(value: str) -> time:
    parts = value.split(":")
    if len(parts) != 2 or any(len(part) != 2 or not _ascii_digits(part) for part in parts):
        raise argparse.ArgumentTypeError("expected HH:MM")
    hours, minutes = (int(part) for part in parts)
    if not 0 <= hours <= 23 or not 0 <= minutes <= 59:
        raise argparse.ArgumentTypeError("expected HH:MM")
    return time(hour=hours, minute=minutes)


def positive_int(value: str) -> int:
    result = _parse_ascii_int(value, "must be a positive integer")
    if result <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return result


def positive_float(value: str) -> float:
    if not _ascii_decimal_number(value):
        raise argparse.ArgumentTypeError("must be a positive number")
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise argparse.ArgumentTypeError("must be a positive number")
    return result


def percent_int(value: str) -> int:
    result = _parse_ascii_int(value, "must be an integer from 0 to 100")
    if not 0 <= result <= 100:
        raise argparse.ArgumentTypeError("must be an integer from 0 to 100")
    return result


def sqlite_db_path(value: str) -> Path:
    if not value.strip():
        raise argparse.ArgumentTypeError("SQLite database path must not be blank")
    if any(not character.isprintable() for character in value):
        raise argparse.ArgumentTypeError("SQLite database path must not contain control characters")
    try:
        path = Path(value).expanduser()
    except RuntimeError as exc:
        raise argparse.ArgumentTypeError(
            "SQLite database path uses unknown home directory"
        ) from exc
    if path.name.lower().endswith(SQLITE_SIDECAR_SUFFIXES):
        raise argparse.ArgumentTypeError(
            "SQLite database path must point to the main database file, not WAL/SHM sidecar"
        )
    if _path_exists(path, "SQLite database path is not accessible") and _path_is_dir(
        path,
        "SQLite database path is not accessible",
    ):
        raise argparse.ArgumentTypeError("SQLite database path must be a file, got directory")
    parent_exists = _path_exists(
        path.parent,
        "SQLite database path parent is not accessible",
    )
    if parent_exists:
        if not _path_is_dir(
            path.parent,
            "SQLite database path parent is not accessible",
        ):
            raise argparse.ArgumentTypeError("SQLite database path parent must be a directory")
        if not _path_allows(path.parent, os.W_OK | os.X_OK, "SQLite database path is not accessible"):
            raise argparse.ArgumentTypeError("SQLite database path is not accessible")
    return path


def walk_minutes_arg(value: str) -> int:
    result = _parse_ascii_int(value, _walk_minutes_error())
    if not is_valid_walk_minutes(result):
        raise argparse.ArgumentTypeError(_walk_minutes_error())
    return result


def _parse_ascii_int(value: str, error: str) -> int:
    if not _ascii_digits(value):
        raise argparse.ArgumentTypeError(error)
    try:
        return int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(error) from exc


def _ascii_decimal_number(value: str) -> bool:
    parts = value.split(".")
    if len(parts) > 2:
        return False
    if len(parts) == 1:
        return _ascii_digits(value)
    whole, fraction = parts
    if not whole and not fraction:
        return False
    return (not whole or _ascii_digits(whole)) and (not fraction or _ascii_digits(fraction))


def _ascii_digits(value: str) -> bool:
    return bool(value) and value.isascii() and value.isdecimal()


def _path_exists(path: Path, error: str) -> bool:
    try:
        return path.exists()
    except (OSError, ValueError) as exc:
        raise argparse.ArgumentTypeError(error) from exc


def _path_is_dir(path: Path, error: str) -> bool:
    try:
        return path.is_dir()
    except (OSError, ValueError) as exc:
        raise argparse.ArgumentTypeError(error) from exc


def _path_allows(path: Path, mode: int, error: str) -> bool:
    try:
        return os.access(path, mode)
    except (OSError, ValueError) as exc:
        raise argparse.ArgumentTypeError(error) from exc


def _walk_minutes_error() -> str:
    return f"must be an integer from {MIN_WALK_MINUTES} to {MAX_WALK_MINUTES}"
