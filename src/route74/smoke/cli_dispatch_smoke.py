from __future__ import annotations

import argparse
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO

from route74.cli.main import build_parser


def main() -> None:
    parser = build_parser()
    command_parsers = _command_parsers(parser)
    missing_handlers = _commands_without_handlers(command_parsers)
    if missing_handlers:
        joined = ", ".join(missing_handlers)
        raise AssertionError(f"CLI commands missing callable handlers: {joined}")
    root_help_failure = _root_help_failure(parser, command_parsers)
    if root_help_failure is not None:
        raise AssertionError(f"CLI root help must render cleanly: {root_help_failure}")
    broken_help = _commands_with_broken_help(parser, command_parsers)
    if broken_help:
        details = "\n".join(broken_help)
        raise AssertionError(f"CLI command help must render cleanly:\n{details}")
    print("OK | CLI dispatch smoke passed")


def _commands_without_handlers(command_parsers: dict[str, argparse.ArgumentParser]) -> list[str]:
    missing: list[str] = []
    for command, command_parser in sorted(command_parsers.items()):
        handler = command_parser.get_default("func")
        if not callable(handler):
            missing.append(command)
    return missing


def _root_help_failure(
    parser: argparse.ArgumentParser,
    command_parsers: dict[str, argparse.ArgumentParser],
) -> str | None:
    stdout = StringIO()
    stderr = StringIO()
    try:
        with redirect_stdout(stdout), redirect_stderr(stderr):
            parser.parse_args(("--help",))
    except SystemExit as exc:
        if exc.code != 0:
            return f"--help exited with {exc.code!r}"
    else:
        return "--help did not exit"
    output = stdout.getvalue()
    error_output = stderr.getvalue()
    if "usage:" not in output or "command" not in output:
        return "--help output is incomplete"
    missing_commands = sorted(command for command in command_parsers if command not in output)
    if missing_commands:
        return f"--help omits commands: {', '.join(missing_commands)}"
    if error_output:
        return f"--help wrote to stderr: {error_output!r}"
    return None


def _commands_with_broken_help(
    parser: argparse.ArgumentParser,
    command_parsers: dict[str, argparse.ArgumentParser],
) -> list[str]:
    failures: list[str] = []
    for command in sorted(command_parsers):
        stdout = StringIO()
        stderr = StringIO()
        try:
            with redirect_stdout(stdout), redirect_stderr(stderr):
                parser.parse_args((command, "--help"))
        except SystemExit as exc:
            if exc.code != 0:
                failures.append(f"{command}: --help exited with {exc.code!r}")
        else:
            failures.append(f"{command}: --help did not exit")
        output = stdout.getvalue()
        error_output = stderr.getvalue()
        if "usage:" not in output or command not in output:
            failures.append(f"{command}: --help output is incomplete")
        if error_output:
            failures.append(f"{command}: --help wrote to stderr: {error_output!r}")
    return failures


def _command_parsers(parser: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return dict(action.choices)
    raise AssertionError("missing command subparser")


if __name__ == "__main__":
    main()
