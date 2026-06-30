from __future__ import annotations

import os
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory


REPO_ROOT = Path(__file__).resolve().parents[3]


def main() -> None:
    with TemporaryDirectory() as temp_dir:
        home_dir = Path(temp_dir)
        env = os.environ.copy()
        env["HOME"] = str(home_dir)
        env["ROUTE74_ENV_FILE"] = "/dev/null"

        result = subprocess.run(
            (str(REPO_ROOT / "bin" / "install-launcher"),),
            check=False,
            cwd=REPO_ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        _assert_equal(result.returncode, 0)

        web_launcher = home_dir / ".local" / "bin" / "route74-web-open"
        dashboard_launcher = home_dir / ".local" / "bin" / "route74-dashboard-open"
        _assert_executable(web_launcher)
        _assert_executable(dashboard_launcher)

        web_content = web_launcher.read_text(encoding="utf-8")
        dashboard_content = dashboard_launcher.read_text(encoding="utf-8")
        _assert_contains(web_content, "exec ")
        _assert_contains(web_content, "bin/web")
        _assert_contains(dashboard_content, "exec ")
        _assert_contains(dashboard_content, "bin/dashboard")

        web_result = subprocess.run(
            (str(web_launcher), "--help"),
            check=False,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        _assert_equal(web_result.returncode, 0)
        _assert_contains(web_result.stdout + web_result.stderr, "route74-web")

        dashboard_result = subprocess.run(
            (str(dashboard_launcher), "--help"),
            check=False,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        _assert_equal(dashboard_result.returncode, 0)
        _assert_contains(dashboard_result.stdout + dashboard_result.stderr, "операторского dashboard")

        _assert_dashboard_rejects_bad_port(env)
        _assert_web_remote_rejects_bad_port(env)

    print("OK | launcher smoke passed")


def _assert_dashboard_rejects_bad_port(env: dict[str, str]) -> None:
    bad_env = env.copy()
    bad_env["ROUTE74_DASHBOARD_LOCAL_PORT"] = "70000"
    result = subprocess.run(
        (str(REPO_ROOT / "bin" / "dashboard"),),
        check=False,
        cwd=REPO_ROOT,
        env=bad_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    _assert_equal(result.returncode, 2)
    _assert_contains(
        result.stdout + result.stderr,
        "ROUTE74_DASHBOARD_LOCAL_PORT must be an integer from 1 to 65535",
    )


def _assert_web_remote_rejects_bad_port(env: dict[str, str]) -> None:
    bad_env = env.copy()
    bad_env["ROUTE74_WEB_LOCAL_PORT"] = "70000"
    result = subprocess.run(
        (str(REPO_ROOT / "bin" / "web-remote"),),
        check=False,
        cwd=REPO_ROOT,
        env=bad_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    _assert_equal(result.returncode, 2)
    _assert_contains(
        result.stdout + result.stderr,
        "ROUTE74_WEB_LOCAL_PORT must be an integer from 1 to 65535",
    )


def _assert_executable(path: Path) -> None:
    if not path.is_file() or not os.access(path, os.X_OK):
        raise AssertionError(f"expected executable file: {path}")


def _assert_equal(actual: object, expected: object) -> None:
    if actual != expected:
        raise AssertionError(f"expected {expected!r}, got {actual!r}")


def _assert_contains(text: str, expected: str) -> None:
    if expected not in text:
        raise AssertionError(f"expected {expected!r} in {text!r}")


if __name__ == "__main__":
    main()
