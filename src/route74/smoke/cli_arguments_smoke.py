from __future__ import annotations

import argparse
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory

from route74.cli.main import build_parser, dispatch_command
from route74.cli.yandex import cmd_yandex_line
from route74.domain.profiles import ALL_PROFILES_KEY, PROFILE_KEYS, PROFILE_SELECTORS


def main() -> None:
    parser = build_parser()
    _assert_profile_choices_follow_registry(parser)
    _assert_rejects(parser, (), "the following arguments are required: command")
    _assert_accepts(parser, ("explain", "--morning-walk", "0", "--evening-walk", "60"))
    _assert_accepts(parser, ("commute", "morning", "--walk", "12"))
    _assert_accepts(parser, ("stats", "morning", "--watch-state-path", "data/web_watches.json"))
    _assert_accepts(parser, ("report-stats", "--window", "weekday_morning_09_12"))
    _assert_accepts(parser, ("prediction-calibration", "--window", "weekday_morning_09_12"))
    _assert_accepts(parser, ("support-report", "--window", "weekday_morning_09_12", "--hours", "24"))
    _assert_accepts(parser, ("support-report", "--profile", "morning", "--watch-state-path", "data/web_watches.json"))
    _assert_accepts(parser, ("support-report", "--profile", "morning", "--limit", "3"))
    _assert_accepts(parser, ("support-report", "--window", "weekday_evening_19_22", "--profile", "evening"))
    _assert_accepts(parser, ("support-report", "--profile", "morning", "--hours", "24"))
    _assert_accepts(parser, ("runtime-latency", "--profile", "morning", "--hours", "24"))
    _assert_accepts(parser, ("runtime-latency", "--event-kind", "watch_early", "--hours", "24"))
    _assert_accepts(parser, ("monitor-tick", "--profile", "morning", "--fail-on", "warning"))
    _assert_accepts(parser, ("watch-state", "--path", "data/web_watches.json"))
    _assert_accepts(parser, ("forecast-backtest", "--window", "weekday_morning_09_12", "--percentiles", "70,80,90"))
    _assert_accepts(
        parser,
        (
            "monitor-tick",
            "--warn-error-rate",
            "0",
            "--critical-error-rate",
            "100",
            "--min-no-eta-events",
            "3",
            "--warn-no-eta-rate",
            "0",
            "--critical-no-eta-rate",
            "100",
            "--warn-bot-miss-rate",
            "0",
            "--critical-bot-miss-rate",
            "100",
            "--warn-bot-p50-error-minutes",
            "1",
            "--critical-bot-p50-error-minutes",
            "10",
            "--warn-bot-pending-age-minutes",
            "10",
            "--critical-bot-pending-age-minutes",
            "60",
            "--warn-bot-guardrail-unavailable",
            "1",
            "--critical-bot-guardrail-unavailable",
            "3",
            "--min-bot-evaluated",
            "1",
            "--watch-state-path",
            "data/web_watches.json",
        ),
    )
    _assert_rejects(parser, ("commute", "morning", "--walk", "-1"), "must be an integer from 0 to 60")
    _assert_rejects(parser, ("commute", "morning", "--walk", "1_2"), "must be an integer from 0 to 60")
    _assert_rejects(parser, ("stats", "evening", "--walk", "61"), "must be an integer from 0 to 60")
    _assert_rejects(parser, ("predict", "morning", "--walk", "many"), "must be an integer from 0 to 60")
    _assert_rejects(parser, ("report-stats", "--window", "night"), "invalid choice")
    _assert_rejects(parser, ("yandex-stats", "--hours", "1_000"), "must be a positive integer")
    _assert_rejects(
        parser,
        ("yandex-dump", "--profile", "morning", "--timeout", "1_000"),
        "must be a positive number",
    )
    _assert_rejects(parser, ("monitor-tick", "--warn-error-rate", "101"), "must be an integer from 0 to 100")
    _assert_rejects(parser, ("monitor-tick", "--critical-error-rate", "-1"), "must be an integer from 0 to 100")
    _assert_rejects(parser, ("monitor-tick", "--min-no-eta-events", "0"), "must be a positive integer")
    _assert_rejects(parser, ("monitor-tick", "--warn-no-eta-rate", "101"), "must be an integer from 0 to 100")
    _assert_rejects(parser, ("monitor-tick", "--critical-no-eta-rate", "-1"), "must be an integer from 0 to 100")
    _assert_rejects(parser, ("monitor-tick", "--profile", "night"), "invalid choice")
    _assert_rejects(parser, ("runtime-latency", "--profile", "night"), "invalid choice")
    _assert_rejects(parser, ("runtime-latency", "--event-kind", "night"), "invalid choice")
    _assert_rejects(parser, ("monitor-tick", "--runtime-hours", "0"), "must be a positive integer")
    _assert_rejects(parser, ("monitor-tick", "--min-bot-evaluated", "0"), "must be a positive integer")
    _assert_rejects(parser, ("monitor-tick", "--warn-bot-miss-rate", "101"), "must be an integer from 0 to 100")
    _assert_rejects(parser, ("monitor-tick", "--critical-bot-miss-rate", "-1"), "must be an integer from 0 to 100")
    _assert_rejects(parser, ("monitor-tick", "--warn-bot-p50-error-minutes", "0"), "must be a positive integer")
    _assert_rejects(parser, ("monitor-tick", "--critical-bot-p50-error-minutes", "0"), "must be a positive integer")
    _assert_rejects(parser, ("monitor-tick", "--warn-bot-pending-age-minutes", "0"), "must be a positive integer")
    _assert_rejects(
        parser,
        ("monitor-tick", "--critical-bot-pending-age-minutes", "0"),
        "must be a positive integer",
    )
    _assert_rejects(
        parser,
        ("monitor-tick", "--warn-bot-guardrail-unavailable", "0"),
        "must be a positive integer",
    )
    _assert_rejects(
        parser,
        ("monitor-tick", "--critical-bot-guardrail-unavailable", "0"),
        "must be a positive integer",
    )
    _assert_rejects_directory_db_path(parser)
    _assert_rejects_control_character_db_path(parser)
    _assert_rejects_blank_db_path(parser)
    _assert_rejects_sqlite_sidecar_db_path(parser)
    _assert_rejects_file_db_parent(parser)
    _assert_rejects_inaccessible_db_path(parser)
    _assert_command_without_handler_fails_cleanly()
    _assert_rejects(
        parser,
        ("forecast-backtest", "--window", "weekday_morning_09_12", "--percentiles", "70,,90"),
        "expected comma-separated integers",
    )
    _assert_rejects(
        parser,
        ("forecast-backtest", "--window", "weekday_morning_09_12", "--percentiles", "70,"),
        "expected comma-separated integers",
    )
    _assert_yandex_line_rejects_bad_json()
    print("OK | CLI argument smoke passed")


def _assert_profile_choices_follow_registry(parser: argparse.ArgumentParser) -> None:
    _assert_equal(_argument_choices(parser, "commute", "profile"), PROFILE_KEYS)
    _assert_equal(_argument_choices(parser, "stats", "profile"), PROFILE_KEYS)
    _assert_equal(_argument_choices(parser, "predict", "profile"), PROFILE_KEYS)
    _assert_equal(_argument_choices(parser, "forecast-readiness", "profile"), PROFILE_KEYS)
    _assert_equal(_argument_choices(parser, "yandex-dump", "profile"), PROFILE_KEYS)
    _assert_equal(_argument_choices(parser, "yandex-line", "save_profile"), PROFILE_KEYS)
    _assert_equal(_argument_choices(parser, "yandex-stats", "profile"), PROFILE_SELECTORS)
    _assert_equal(_argument_choices(parser, "report-stats", "profile"), PROFILE_SELECTORS)
    _assert_equal(_argument_choices(parser, "yandex-collect", "profile"), PROFILE_SELECTORS)
    _assert_equal(_argument_choices(parser, "yandex-canary", "profile"), PROFILE_SELECTORS)
    _assert_equal(_argument_choices(parser, "prediction-backfill", "profile"), PROFILE_SELECTORS)
    _assert_equal(_argument_choices(parser, "monitor-tick", "profile"), PROFILE_KEYS)
    _assert_equal(_argument_default(parser, "yandex-stats", "profile"), ALL_PROFILES_KEY)
    _assert_equal(_argument_default(parser, "report-stats", "profile"), ALL_PROFILES_KEY)
    _assert_equal(_argument_default(parser, "prediction-backfill", "profile"), ALL_PROFILES_KEY)


def _argument_choices(parser: argparse.ArgumentParser, command: str, dest: str) -> tuple[str, ...]:
    choices = _argument_action(parser, command, dest).choices
    return tuple(choices or ())


def _argument_default(parser: argparse.ArgumentParser, command: str, dest: str) -> object:
    return _argument_action(parser, command, dest).default


def _argument_action(parser: argparse.ArgumentParser, command: str, dest: str) -> argparse.Action:
    command_parser = _command_parser(parser, command)
    for action in command_parser._actions:
        if action.dest == dest:
            return action
    raise AssertionError(f"missing {command} argument {dest}")


def _command_parser(parser: argparse.ArgumentParser, command: str) -> argparse.ArgumentParser:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action.choices[command]
    raise AssertionError("missing command subparser")


def _assert_accepts(parser: argparse.ArgumentParser, args: tuple[str, ...]) -> None:
    parser.parse_args(args)


def _assert_rejects(parser: argparse.ArgumentParser, args: tuple[str, ...], expected: str) -> None:
    stderr = StringIO()
    try:
        with redirect_stderr(stderr):
            parser.parse_args(args)
    except SystemExit as exc:
        if exc.code == 0:
            raise AssertionError(f"expected {args!r} to fail")
    else:
        raise AssertionError(f"expected {args!r} to fail")
    output = stderr.getvalue()
    if expected not in output:
        raise AssertionError(f"expected {expected!r} in {output!r}")


def _assert_yandex_line_rejects_bad_json() -> None:
    with TemporaryDirectory() as temp_dir:
        dump_path = Path(temp_dir) / "bad-dump.json"
        dump_path.write_text("{bad-json", encoding="utf-8")
        try:
            cmd_yandex_line(argparse.Namespace(dump=dump_path, save_profile=None))
        except SystemExit as error:
            message = str(error)
        else:
            raise AssertionError("expected bad Yandex dump JSON to fail")
    if "not valid JSON" not in message or "Traceback" in message:
        raise AssertionError(f"expected clean invalid JSON error, got {message!r}")


def _assert_command_without_handler_fails_cleanly() -> None:
    parser = argparse.ArgumentParser(prog="route74-test")
    subparsers = parser.add_subparsers(dest="command", metavar="command", required=True)
    subparsers.add_parser("broken")
    args = parser.parse_args(("broken",))

    stderr = StringIO()
    try:
        with redirect_stderr(stderr):
            dispatch_command(parser, args)
    except SystemExit as exc:
        if exc.code == 0:
            raise AssertionError("expected unwired command to fail")
    else:
        raise AssertionError("expected unwired command to fail")
    output = stderr.getvalue()
    if "command 'broken' is missing a handler" not in output or "Traceback" in output:
        raise AssertionError(f"expected clean missing-handler error, got {output!r}")


def _assert_rejects_directory_db_path(parser: argparse.ArgumentParser) -> None:
    with TemporaryDirectory() as temp_dir:
        _assert_rejects(
            parser,
            ("--db", temp_dir, "db-health"),
            "SQLite database path must be a file",
        )


def _assert_rejects_control_character_db_path(parser: argparse.ArgumentParser) -> None:
    _assert_rejects(
        parser,
        ("--db", "data/route74\x00.sqlite", "db-health"),
        "SQLite database path must not contain control characters",
    )


def _assert_rejects_blank_db_path(parser: argparse.ArgumentParser) -> None:
    _assert_rejects(
        parser,
        ("--db", "   ", "db-health"),
        "SQLite database path must not be blank",
    )


def _assert_rejects_sqlite_sidecar_db_path(parser: argparse.ArgumentParser) -> None:
    for suffix in ("-wal", "-shm", "-journal"):
        _assert_rejects(
            parser,
            ("--db", f"data/route74.sqlite{suffix}", "db-health"),
            "SQLite database path must point to the main database file",
        )
    for path in ("data/route74.sqlite3-journal", "data/route74.db-journal"):
        _assert_rejects(
            parser,
            ("--db", path, "db-health"),
            "SQLite database path must point to the main database file",
        )


def _assert_rejects_file_db_parent(parser: argparse.ArgumentParser) -> None:
    with TemporaryDirectory() as temp_dir:
        parent_path = Path(temp_dir) / "not-a-directory"
        parent_path.write_text("", encoding="utf-8")
        _assert_rejects(
            parser,
            ("--db", str(parent_path / "route74.sqlite"), "db-health"),
            "SQLite database path parent must be a directory",
        )


def _assert_rejects_inaccessible_db_path(parser: argparse.ArgumentParser) -> None:
    with TemporaryDirectory() as temp_dir:
        blocked_dir = Path(temp_dir) / "blocked"
        blocked_dir.mkdir()
        blocked_dir.chmod(0)
        try:
            _assert_rejects(
                parser,
                ("--db", str(blocked_dir / "route74.sqlite"), "db-health"),
                "SQLite database path is not accessible",
            )
        finally:
            blocked_dir.chmod(0o700)


def _assert_equal(actual: object, expected: object) -> None:
    if actual != expected:
        raise AssertionError(f"expected {expected!r}, got {actual!r}")


if __name__ == "__main__":
    main()
