from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory


REPO_ROOT = Path(__file__).resolve().parents[3]


def main() -> None:
    _assert_install_rejects_old_python()
    _assert_install_rejects_stale_venv()
    print("OK | install smoke passed")


def _assert_install_rejects_old_python() -> None:
    with TemporaryDirectory() as temp_dir:
        fake_python = Path(temp_dir) / "python3"
        fake_python.write_text(
            "\n".join(
                [
                    "#!/usr/bin/env bash",
                    'if [ "${1:-}" = "-" ]; then',
                    "  cat >/dev/null",
                    "  echo '3.10.13'",
                    "  exit 1",
                    "fi",
                    "echo 'unexpected fake python invocation' >&2",
                    "exit 99",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        fake_python.chmod(0o700)
        env = os.environ.copy()
        env["ROUTE74_PYTHON_BIN"] = str(fake_python)

        result = subprocess.run(
            (str(REPO_ROOT / "bin" / "install"),),
            check=False,
            cwd=REPO_ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    _assert_equal(result.returncode, 1)
    output = result.stdout + result.stderr
    _assert_contains(output, "Route74 requires Python 3.11+")
    _assert_contains(output, "3.10.13")
    _assert_not_contains(output, "unexpected fake python invocation")


def _assert_install_rejects_stale_venv() -> None:
    with TemporaryDirectory() as temp_dir:
        venv_dir = Path(temp_dir) / "stale-venv"
        fake_python = venv_dir / "bin" / "python"
        fake_python.parent.mkdir(parents=True)
        fake_python.write_text(
            "\n".join(
                [
                    "#!/usr/bin/env bash",
                    'if [ "${1:-}" = "-" ]; then',
                    "  cat >/dev/null",
                    "  echo '3.10.13'",
                    "  exit 1",
                    "fi",
                    "echo 'unexpected stale venv invocation' >&2",
                    "exit 99",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        fake_python.chmod(0o700)
        env = os.environ.copy()
        env["ROUTE74_PYTHON_BIN"] = sys.executable
        env["ROUTE74_VENV_DIR"] = str(venv_dir)

        result = subprocess.run(
            (str(REPO_ROOT / "bin" / "install"),),
            check=False,
            cwd=REPO_ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    _assert_equal(result.returncode, 1)
    output = result.stdout + result.stderr
    _assert_contains(output, "Route74 requires Python 3.11+")
    _assert_contains(output, "3.10.13")
    _assert_contains(output, "Remove")
    _assert_contains(output, "rerun ./bin/install")
    _assert_not_contains(output, "unexpected stale venv invocation")


def _assert_equal(actual: object, expected: object) -> None:
    if actual != expected:
        raise AssertionError(f"expected {expected!r}, got {actual!r}")


def _assert_contains(text: str, expected: str) -> None:
    if expected not in text:
        raise AssertionError(f"expected {expected!r} in {text!r}")


def _assert_not_contains(text: str, unexpected: str) -> None:
    if unexpected in text:
        raise AssertionError(f"did not expect {unexpected!r} in {text!r}")


if __name__ == "__main__":
    main()
