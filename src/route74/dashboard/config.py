from __future__ import annotations

import argparse
from dataclasses import dataclass
from ipaddress import ip_address
from pathlib import Path

from route74.env import (
    DEFAULT_ENV_PATH,
    ENV_DB_PATH,
    ENV_FILE,
    env_value,
    load_env_file,
    parse_bool_env,
)
from route74.storage import DEFAULT_DB

ENV_DASHBOARD_HOST = "ROUTE74_DASHBOARD_HOST"
ENV_DASHBOARD_PORT = "ROUTE74_DASHBOARD_PORT"
ENV_DASHBOARD_ALLOW_PUBLIC = "ROUTE74_DASHBOARD_ALLOW_PUBLIC"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8074


@dataclass(frozen=True)
class DashboardConfig:
    host: str
    port: int
    db_path: Path
    env_file: Path

    def __post_init__(self) -> None:
        _parse_host(self.host)
        _validate_port(self.port)
        if not isinstance(self.db_path, Path):
            raise ValueError("dashboard db_path needs Path")
        if not isinstance(self.env_file, Path):
            raise ValueError("dashboard env_file needs Path")


def parse_dashboard_config(argv: list[str] | None = None) -> DashboardConfig:
    parser = argparse.ArgumentParser(prog="route74-dashboard")
    parser.add_argument("--host", type=_host_arg, default=None)
    parser.add_argument("--port", type=_port_arg, default=None)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--env-file", type=Path, default=None)
    args = parser.parse_args(argv)

    env_file_value = env_value(ENV_FILE, {}, str(DEFAULT_ENV_PATH))
    env_file = args.env_file or Path(env_file_value if env_file_value is not None else str(DEFAULT_ENV_PATH))
    file_env = load_env_file(env_file)
    host = args.host if args.host is not None else _env_host(file_env)
    port = args.port if args.port is not None else _env_port(file_env)
    db_path = args.db or Path(_env_value(ENV_DB_PATH, file_env, str(DEFAULT_DB)))
    allow_public = parse_bool_env(
        ENV_DASHBOARD_ALLOW_PUBLIC,
        _optional_env_value(ENV_DASHBOARD_ALLOW_PUBLIC, file_env),
        False,
    )
    if not allow_public and not _is_loopback(host):
        raise SystemExit(f"{ENV_DASHBOARD_ALLOW_PUBLIC}=1 is required for non-loopback host {host!r}.")
    return DashboardConfig(host=host, port=port, db_path=db_path, env_file=env_file)


def _env_value(name: str, file_env: dict[str, str], default: str | None) -> str:
    value = env_value(name, {}, None) or file_env.get(name) or default
    if value is None:
        raise SystemExit(f"{name} is required.")
    return value


def _optional_env_value(name: str, file_env: dict[str, str]) -> str | None:
    return env_value(name, {}, None) or file_env.get(name)


def _env_port(file_env: dict[str, str]) -> int:
    value = _raw_env_value(ENV_DASHBOARD_PORT, file_env, str(DEFAULT_PORT))
    try:
        return _parse_port(value)
    except ValueError as exc:
        raise SystemExit(f"{ENV_DASHBOARD_PORT} {exc}") from exc


def _env_host(file_env: dict[str, str]) -> str:
    value = _raw_env_value(ENV_DASHBOARD_HOST, file_env, DEFAULT_HOST)
    try:
        return _parse_host(value)
    except ValueError as exc:
        raise SystemExit(f"{ENV_DASHBOARD_HOST} {exc}") from exc


def _raw_env_value(name: str, file_env: dict[str, str], default: str) -> str:
    environment_value = env_value(name, {}, None)
    if environment_value is not None:
        return environment_value
    if name in file_env:
        return file_env[name]
    return default


def _host_arg(value: str) -> str:
    try:
        return _parse_host(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _port_arg(value: str) -> int:
    try:
        return _parse_port(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _parse_host(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("must be a host name or IP address.")
    if any(not char.isprintable() for char in value):
        raise ValueError("must be a host name or IP address without control characters.")
    if not value or value != value.strip() or any(char.isspace() for char in value):
        raise ValueError("must be a host name or IP address without whitespace.")
    if "://" in value or "/" in value:
        raise ValueError("must be a host name or IP address, not a URL.")
    return value


def _parse_port(value: str) -> int:
    if not value.isascii() or not value.isdecimal():
        raise ValueError("must be an integer from 1 to 65535.")
    try:
        port = int(value)
    except ValueError as exc:
        raise ValueError("must be an integer from 1 to 65535.") from exc
    if not 1 <= port <= 65535:
        raise ValueError("must be an integer from 1 to 65535.")
    return port


def _validate_port(value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535:
        raise ValueError("dashboard port must be an integer from 1 to 65535")


def _is_loopback(host: str) -> bool:
    normalized = host.strip().casefold()
    if normalized == "localhost":
        return True
    try:
        return ip_address(normalized).is_loopback
    except ValueError:
        return False
