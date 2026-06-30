from __future__ import annotations

import os
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]


def main() -> None:
    _assert_rejects_app_user("bad user")
    _assert_rejects_app_user("-route74")
    _assert_rejects_python_bin("python bad")
    _assert_rejects_python_bin("-python3")
    _assert_rejects_python_bin("python3;echo")
    print("OK | server update smoke passed")


def _assert_rejects_app_user(value: str) -> None:
    env = os.environ.copy()
    env["ROUTE74_APP_USER"] = value
    result = subprocess.run(
        (str(REPO_ROOT / "bin" / "server-update"),),
        check=False,
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    _assert_equal(result.returncode, 2)
    _assert_contains(
        result.stdout + result.stderr,
        "ROUTE74_APP_USER must be a system user name",
    )


def _assert_rejects_python_bin(value: str) -> None:
    env = os.environ.copy()
    env["ROUTE74_PYTHON_BIN"] = value
    result = subprocess.run(
        (str(REPO_ROOT / "bin" / "server-update"),),
        check=False,
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    _assert_equal(result.returncode, 2)
    _assert_contains(
        result.stdout + result.stderr,
        "ROUTE74_PYTHON_BIN must be a command name or path without whitespace",
    )


def _assert_equal(actual: object, expected: object) -> None:
    if actual != expected:
        raise AssertionError(f"expected {expected!r}, got {actual!r}")


def _assert_contains(text: str, expected: str) -> None:
    if expected not in text:
        raise AssertionError(f"expected {expected!r} in {text!r}")


if __name__ == "__main__":
    main()
