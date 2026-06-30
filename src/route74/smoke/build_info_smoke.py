from __future__ import annotations

import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory

import route74.build_info as build_info_module
from route74.build_info import (
    BUILD_INFO_FILENAME,
    GIT_COMMAND_TIMEOUT_SECONDS,
    BuildInfo,
    _candidate_paths,
    _git_value,
    format_build_status,
    load_build_info,
)
from route74.cli.version import format_cli_version
from route74.presenters.version import format_version_message


def main() -> None:
    _assert_candidate_path_precedence()
    _assert_runtime_git_timeout_is_safe()
    with TemporaryDirectory() as temp_dir:
        path = Path(temp_dir) / ".route74-build.json"
        path.write_text(
            json.dumps(
                {
                    "package_version": "0.1.0",
                    "commit": "abcdef123456",
                    "short_commit": "abcdef1",
                    "branch": "main",
                    "dirty": False,
                    "built_at": "2026-06-06T09:55:00+07:00",
                    "deployed_at": "2026-06-06T10:00:00+07:00",
                    "source": "server-sync",
                }
            ),
            encoding="utf-8",
        )
        info = load_build_info(path)
    _assert_equal(info.label, "abcdef1")
    _assert_equal(info.short_commit, "abcdef1")
    _assert_equal(info.built_at, "2026-06-06T09:55:00+07:00")
    _assert_equal(info.deployed_at, "2026-06-06T10:00:00+07:00")
    _assert_equal(format_build_status(info), "clean")
    _assert_contains(format_cli_version(info), "built_at=2026-06-06T09:55:00+07:00")

    fallback = _load_from_payload(
        {
            "package_version": "0.1.0",
            "commit": "1234567890abcdef",
            "dirty": "clean",
        }
    )
    _assert_equal(fallback.short_commit, "1234567")
    _assert_equal(fallback.label, "1234567")
    _assert_equal(format_build_status(fallback), "clean")

    multiline = _load_from_payload(
        {
            "package_version": "0.1.0\nspoofed",
            "commit": "abcdef1\nspoofed",
            "short_commit": "abcdef2\nspoofed",
            "branch": "main\nspoofed",
            "built_at": "2026-06-06T09:55:00+07:00\nspoofed",
            "deployed_at": "2026-06-06T10:00:00+07:00\nspoofed",
            "source": "server-sync\nspoofed",
        }
    )
    _assert_equal(multiline.package_version, "0.1.0 spoofed")
    _assert_equal(multiline.commit, None)
    _assert_equal(multiline.short_commit, None)
    _assert_equal(multiline.branch, "main spoofed")
    _assert_equal(multiline.built_at, None)
    _assert_equal(multiline.deployed_at, None)
    _assert_equal(multiline.source, "server-sync spoofed")
    _assert_equal(multiline.label, "0.1.0 spoofed")
    _assert_single_line(format_cli_version(multiline))
    _assert_contains(format_version_message(multiline), "Пакет: 0.1.0 spoofed")
    _assert_not_contains(format_version_message(multiline), "\nspoofed")

    malformed_optional_strings = _load_from_payload(
        {
            "package_version": True,
            "commit": True,
            "short_commit": 1234567,
            "branch": False,
            "built_at": True,
            "deployed_at": 42,
            "source": ["server-sync"],
        }
    )
    _assert_equal(malformed_optional_strings.package_version, "0.1.0")
    _assert_equal(malformed_optional_strings.commit, None)
    _assert_equal(malformed_optional_strings.short_commit, None)
    _assert_equal(malformed_optional_strings.branch, None)
    _assert_equal(malformed_optional_strings.built_at, None)
    _assert_equal(malformed_optional_strings.deployed_at, None)
    _assert_equal(malformed_optional_strings.source, "build-file")
    _assert_equal(malformed_optional_strings.label, "0.1.0")

    malformed_timestamps = _load_from_payload(
        {
            "package_version": "0.1.0",
            "built_at": "2026-06-06T09:55:00",
            "deployed_at": "not-a-date",
        }
    )
    _assert_equal(malformed_timestamps.built_at, None)
    _assert_equal(malformed_timestamps.deployed_at, None)

    oversized_optional_strings = _load_from_payload(
        {
            "package_version": "1" * 121,
            "branch": "main-" + "x" * 121,
            "built_at": "2026-" + "0" * 121,
            "deployed_at": "2026-" + "1" * 121,
            "source": "sync-" + "x" * 121,
        }
    )
    _assert_equal(oversized_optional_strings.package_version, "0.1.0")
    _assert_equal(oversized_optional_strings.branch, None)
    _assert_equal(oversized_optional_strings.built_at, None)
    _assert_equal(oversized_optional_strings.deployed_at, None)
    _assert_equal(oversized_optional_strings.source, "build-file")
    _assert_equal(oversized_optional_strings.label, "0.1.0")

    manual = BuildInfo(
        "0.1.0",
        commit="FEDCBA9876543210",
        branch=" main\nprod ",
        dirty="dirty",  # type: ignore[arg-type]
        source=" runtime\nmanual ",
    )
    _assert_equal(manual.label, "fedcba9")
    _assert_equal(manual.commit, "fedcba9876543210")
    _assert_equal(manual.branch, "main prod")
    _assert_equal(manual.dirty, True)
    _assert_equal(manual.source, "runtime manual")
    _assert_contains(format_cli_version(manual), "commit=fedcba9")
    _assert_contains(format_cli_version(manual), "branch=main prod")
    _assert_contains(format_version_message(manual), "Коммит: fedcba9")
    print("OK | build info smoke passed")


def _assert_candidate_path_precedence() -> None:
    repo_build_file = Path(__file__).resolve().parents[3] / BUILD_INFO_FILENAME
    _assert_equal(_candidate_paths(repo_build_file), (repo_build_file,))
    with TemporaryDirectory() as temp_dir:
        previous_cwd = Path.cwd()
        os.chdir(temp_dir)
        try:
            paths = _candidate_paths(None)
        finally:
            os.chdir(previous_cwd)
        _assert_equal(paths[0], repo_build_file)
        _assert_equal(paths[1], Path(temp_dir) / BUILD_INFO_FILENAME)


def _assert_runtime_git_timeout_is_safe() -> None:
    original_run = build_info_module.subprocess.run
    calls: list[dict[str, object]] = []

    def timed_out_run(*args: object, **kwargs: object) -> object:
        calls.append(dict(kwargs))
        raise build_info_module.subprocess.TimeoutExpired(
            cmd=args[0] if args else (),
            timeout=kwargs.get("timeout"),
        )

    build_info_module.subprocess.run = timed_out_run  # type: ignore[assignment]
    try:
        _assert_equal(_git_value("rev-parse", "HEAD"), None)
    finally:
        build_info_module.subprocess.run = original_run  # type: ignore[assignment]
    expected_kwargs = {
        "cwd": Path(__file__).resolve().parents[3],
        "check": True,
        "stdout": build_info_module.subprocess.PIPE,
        "stderr": build_info_module.subprocess.DEVNULL,
        "text": True,
        "timeout": GIT_COMMAND_TIMEOUT_SECONDS,
    }
    _assert_equal(calls, [expected_kwargs])


def _load_from_payload(payload: dict[str, object]):
    with TemporaryDirectory() as temp_dir:
        path = Path(temp_dir) / ".route74-build.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return load_build_info(path)


def _assert_equal(actual: object, expected: object) -> None:
    if actual != expected:
        raise AssertionError(f"expected {expected!r}, got {actual!r}")


def _assert_contains(text: str, expected: str) -> None:
    if expected not in text:
        raise AssertionError(f"expected {expected!r} in {text!r}")


def _assert_not_contains(text: str, unexpected: str) -> None:
    if unexpected in text:
        raise AssertionError(f"did not expect {unexpected!r} in {text!r}")


def _assert_single_line(text: str) -> None:
    if "\n" in text:
        raise AssertionError(f"expected single-line text, got {text!r}")


if __name__ == "__main__":
    main()
