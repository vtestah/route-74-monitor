from __future__ import annotations

import fcntl
import os
import threading
import time
from collections.abc import Callable
from math import isfinite
from pathlib import Path
from typing import TextIO, TypeVar

T = TypeVar("T")

_BROWSER_LOCK = threading.Lock()
_LOCK_PATH_ENV = "ROUTE74_YANDEX_BROWSER_LOCK"
_DEFAULT_LOCK_PATH = Path("data/yandex-browser.lock")


def run_with_browser_slot(action: Callable[[], T], min_interval_seconds: float) -> T:
    min_interval_seconds = _non_negative_finite_number("browser min interval", min_interval_seconds)
    lock_path = _browser_lock_path()
    with _BROWSER_LOCK:
        return _run_with_process_lock(action, min_interval_seconds, lock_path)


def _run_with_process_lock(action: Callable[[], T], min_interval_seconds: float, lock_path: Path) -> T:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    if lock_path.exists() and lock_path.is_dir():
        raise ValueError("browser lock path must be a file, got directory")
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            now = time.time()
            last_start = _read_last_start(lock_file, current_time=now)
            wait_seconds = max(0.0, min_interval_seconds - (now - last_start))
            if wait_seconds > 0:
                time.sleep(wait_seconds)
            _write_last_start(lock_file, time.time())
            return action()
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _read_last_start(lock_file: TextIO, *, current_time: float | None = None) -> float:
    lock_file.seek(0)
    raw_value = lock_file.read().strip()
    try:
        value = float(raw_value)
    except ValueError:
        return 0.0
    if not isfinite(value):
        return 0.0
    if current_time is not None and value > current_time:
        return 0.0
    return value


def _write_last_start(lock_file: TextIO, value: float) -> None:
    lock_file.seek(0)
    lock_file.truncate()
    lock_file.write(str(value))
    lock_file.flush()
    os.fsync(lock_file.fileno())


def _non_negative_finite_number(name: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not isfinite(value) or value < 0:
        raise ValueError(f"{name} must be a non-negative finite number")
    return float(value)


def _browser_lock_path() -> Path:
    raw_path = os.getenv(_LOCK_PATH_ENV)
    if raw_path is None or not raw_path.strip():
        return _DEFAULT_LOCK_PATH
    return Path(raw_path).expanduser()
