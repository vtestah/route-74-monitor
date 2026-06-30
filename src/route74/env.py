from __future__ import annotations

import os
from collections.abc import Mapping
from math import isfinite
from pathlib import Path


DEFAULT_ENV_PATH = Path(".env")
ENV_FILE = "ROUTE74_ENV_FILE"
ENV_DB_PATH = "ROUTE74_DB_PATH"


def env_value(name: str, file_env: Mapping[str, object], default: str | None = None) -> str | None:
    if name in os.environ:
        return os.environ[name]
    value = file_env.get(name)
    if isinstance(value, str):
        return value
    return default


def defaulted_env_value(name: str, file_env: Mapping[str, object], default: str) -> str:
    value = env_value(name, file_env, default)
    if value is None or not value.strip():
        raise SystemExit(f"{name} must not be empty.")
    return value.strip()


def parse_int_env(name: str, value: str | None, default: str) -> int:
    raw_value = default if value is None else value
    try:
        return int(raw_value)
    except ValueError as exc:
        raise SystemExit(f"{name} must be an integer.") from exc


def parse_float_env(name: str, value: str | None, default: float) -> float:
    raw_value: str | float = default if value is None else value
    try:
        parsed = float(raw_value)
    except ValueError as exc:
        raise SystemExit(f"{name} must be a number.") from exc
    if not isfinite(parsed):
        raise SystemExit(f"{name} must be a finite number.")
    return parsed


def parse_bool_env(name: str, value: str | None, default: bool) -> bool:
    if value is None:
        return default
    normalized = value.strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise SystemExit(f"{name} must be one of: 1, 0, true, false, yes, no, on, off.")


def load_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}

    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        values[key] = _strip_env_quotes(value.strip())
    return values


def _strip_env_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value
