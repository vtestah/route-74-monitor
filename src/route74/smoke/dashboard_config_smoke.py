from __future__ import annotations

from contextlib import redirect_stderr
from io import StringIO
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from route74.dashboard.config import (
    ENV_DASHBOARD_ALLOW_PUBLIC,
    ENV_DASHBOARD_HOST,
    ENV_DASHBOARD_PORT,
    DashboardConfig,
    parse_dashboard_config,
)


def main() -> None:
    with patch.dict(os.environ, {}, clear=True), TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        db_path = temp_path / "route74.sqlite"
        env_path = temp_path / ".env"
        _assert_loopback_hosts(db_path)
        _assert_public_host_rejected(db_path, "0.0.0.0")
        _assert_public_host_rejected(db_path, "127.evil")
        _assert_public_opt_in(db_path, env_path)
        _assert_host_guards(db_path, env_path)
        _assert_port_guards(db_path, env_path)
        _assert_dataclass_guards(db_path, env_path)
    print("OK | dashboard config smoke passed")


def _assert_loopback_hosts(db_path: Path) -> None:
    for host in ("localhost", "127.0.0.1", "::1", "0:0:0:0:0:0:0:1"):
        config = parse_dashboard_config(["--db", str(db_path), "--host", host])
        _assert_equal(config.host, host)


def _assert_public_host_rejected(db_path: Path, host: str) -> None:
    try:
        parse_dashboard_config(["--db", str(db_path), "--host", host])
    except SystemExit as exc:
        _assert_contains(str(exc), ENV_DASHBOARD_ALLOW_PUBLIC)
        return
    raise AssertionError(f"expected public dashboard host {host!r} to fail")


def _assert_public_opt_in(db_path: Path, env_path: Path) -> None:
    env_path.write_text(f"{ENV_DASHBOARD_ALLOW_PUBLIC}=1\n", encoding="utf-8")
    config = parse_dashboard_config(
        [
            "--db",
            str(db_path),
            "--host",
            "0.0.0.0",
            "--env-file",
            str(env_path),
        ],
    )
    _assert_equal(config.host, "0.0.0.0")


def _assert_host_guards(db_path: Path, env_path: Path) -> None:
    _assert_cli_rejects(
        ["--db", str(db_path), "--host", "", "--env-file", str(env_path)],
        "host name or IP address",
    )
    _assert_cli_rejects(
        ["--db", str(db_path), "--host", "http://localhost", "--env-file", str(env_path)],
        "not a URL",
    )
    _assert_cli_rejects(
        ["--db", str(db_path), "--host", "localhost\x00", "--env-file", str(env_path)],
        "control characters",
    )
    with patch.dict(os.environ, {ENV_DASHBOARD_HOST: " 127.0.0.1"}, clear=True):
        _assert_system_exit(
            lambda: parse_dashboard_config(["--db", str(db_path), "--env-file", str(env_path)]),
            ENV_DASHBOARD_HOST,
        )
    with patch.dict(os.environ, {ENV_DASHBOARD_HOST: ""}, clear=True):
        _assert_system_exit(
            lambda: parse_dashboard_config(["--db", str(db_path), "--env-file", str(env_path)]),
            ENV_DASHBOARD_HOST,
        )
    with patch.dict(os.environ, {ENV_DASHBOARD_HOST: "localhost\x1b"}, clear=True):
        _assert_system_exit(
            lambda: parse_dashboard_config(["--db", str(db_path), "--env-file", str(env_path)]),
            ENV_DASHBOARD_HOST,
        )


def _assert_port_guards(db_path: Path, env_path: Path) -> None:
    for value in ("+8074", " 8074", "８０７４"):
        _assert_cli_rejects(
            ["--db", str(db_path), "--port", value, "--env-file", str(env_path)],
            "integer from 1 to 65535",
        )
    with patch.dict(os.environ, {ENV_DASHBOARD_PORT: "+8074"}, clear=True):
        _assert_system_exit(
            lambda: parse_dashboard_config(["--db", str(db_path), "--env-file", str(env_path)]),
            ENV_DASHBOARD_PORT,
        )
    with patch.dict(os.environ, {ENV_DASHBOARD_PORT: ""}, clear=True):
        _assert_system_exit(
            lambda: parse_dashboard_config(["--db", str(db_path), "--env-file", str(env_path)]),
            ENV_DASHBOARD_PORT,
        )


def _assert_dataclass_guards(db_path: Path, env_path: Path) -> None:
    DashboardConfig("127.0.0.1", 8074, db_path, env_path)
    _assert_value_error(lambda: DashboardConfig("127.0.0.1 ", 8074, db_path, env_path), "host")
    _assert_value_error(lambda: DashboardConfig("127.0.0.1", True, db_path, env_path), "port")
    _assert_value_error(lambda: DashboardConfig("127.0.0.1", 8074, "db", env_path), "db_path")
    _assert_value_error(lambda: DashboardConfig("127.0.0.1", 8074, db_path, "env"), "env_file")


def _assert_cli_rejects(argv: list[str], expected: str) -> None:
    stderr = StringIO()
    try:
        with redirect_stderr(stderr):
            parse_dashboard_config(argv)
    except SystemExit as exc:
        if exc.code == 0:
            raise AssertionError(f"expected {argv!r} to fail")
    else:
        raise AssertionError(f"expected {argv!r} to fail")
    _assert_contains(stderr.getvalue(), expected)


def _assert_system_exit(call, expected: str) -> None:
    try:
        call()
    except SystemExit as exc:
        _assert_contains(str(exc), expected)
        return
    raise AssertionError(f"expected SystemExit containing {expected!r}")


def _assert_value_error(call, expected: str) -> None:
    try:
        call()
    except ValueError as exc:
        _assert_contains(str(exc), expected)
        return
    raise AssertionError(f"expected ValueError containing {expected!r}")


def _assert_equal(actual: object, expected: object) -> None:
    if actual != expected:
        raise AssertionError(f"expected {expected!r}, got {actual!r}")


def _assert_contains(haystack: str, needle: str) -> None:
    if needle not in haystack:
        raise AssertionError(f"expected {needle!r} in output")


if __name__ == "__main__":
    main()
